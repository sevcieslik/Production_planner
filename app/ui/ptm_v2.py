from __future__ import annotations

from datetime import date, timedelta
from uuid import uuid4

import pandas as pd
import streamlit as st

from app.data.planner_store import (
    AllocationConflict,
    StageInputConflict,
    get_stage_input,
    heartbeat_session,
    init_planner_store,
    list_active_sessions,
    list_projects,
    purge_stale_sessions,
    save_stage_input,
)
from app.integrations.google_sheets import GoogleSheetsConfigurationError
from app.services.high_level_export import export_to_high_level
from app.services.high_level_sync import sync_high_level_projects
from app.services.ptm_capacity import weekly_person_capacity
from app.services.ptm_migration import migrate_ptm_snapshot
from app.services.ptm_planner_grid import (
    allocation_grid_summary,
    build_department_grid,
    changed_grid_cells,
    project_workload_card,
    save_grid_changes,
)
from app.services.ptm_resources import (
    ResourceConflict,
    list_assignments,
    list_people,
    list_time_off,
    save_assignment,
    save_person,
    save_time_off,
)
from app.services.ptm_teams_breakdown import build_teams_breakdown
from app.services.ptm_work_queue import build_work_queue

DEPARTMENTS = ("RS", "GIS", "PLS")


def _presence_session_id() -> str:
    key = "ptm_presence_session_id"
    if key not in st.session_state:
        st.session_state[key] = uuid4().hex
    return st.session_state[key]


def _presence_context() -> dict:
    return {
        "current_view": st.session_state.get("ptm_presence_view") or "PTM Planner",
        "department": st.session_state.get("ptm_presence_department"),
        "project_code": st.session_state.get("ptm_presence_project"),
        "editing_scope": st.session_state.get("ptm_presence_scope"),
    }


@st.fragment(run_every="15s")
def render_presence_bar() -> None:
    session_id = _presence_session_id()
    context = _presence_context()
    heartbeat_session(
        session_id,
        user_email=st.session_state.user_email,
        display_name=st.session_state.display_name,
        role=st.session_state.role,
        **context,
    )
    purge_stale_sessions(older_than_seconds=300)
    active = list_active_sessions(max_age_seconds=45, exclude_session_id=session_id)
    if not active:
        st.caption("Active now: only you")
        return

    labels = []
    for row in active:
        location = row.get("department") or row.get("current_view") or "Planner"
        if row.get("project_code"):
            location += f" / {row['project_code']}"
        labels.append(f"● {row['display_name']} — {location}")
    st.caption("Active now: " + "   ".join(labels))

    current_department = context.get("department")
    current_project = context.get("project_code")
    if current_department and current_project:
        same_scope = [
            row for row in active
            if row.get("department") == current_department
            and row.get("project_code") == current_project
            and row.get("editing_scope") in {"weekly allocations", "manager inputs"}
        ]
        if same_scope:
            names = ", ".join(row["display_name"] for row in same_scope)
            st.warning(
                f"Also editing {current_department} / {current_project}: {names}. "
                "You can continue working; stale writes are blocked by version checks."
            )


def _parse_optional_number(value: str, label: str) -> float | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        number = float(text.replace(",", ""))
    except ValueError as exc:
        raise ValueError(f"{label} must be a number or blank.") from exc
    if number < 0:
        raise ValueError(f"{label} cannot be negative.")
    return number


def _queue_frame(department: str) -> pd.DataFrame:
    queue = build_work_queue()
    frame = pd.DataFrame(queue[department])
    if frame.empty:
        return frame
    shown = [
        "Project Code",
        "Project",
        "Priority",
        "PM Deadline",
        "Upstream Ready",
        "Bid h",
        "Estimate h",
        "Actual h",
        "Remaining h",
        "Planned h",
        "Forecast",
        "Action",
    ]
    return frame[shown]


def _work_queue(user: str, *, is_admin: bool) -> None:
    st.subheader("Projects / Work Queue")
    st.caption(
        "High Level owns project facts and bid hours. Planner owns PTM estimates, "
        "remaining overrides, handovers and allocations."
    )

    projects = list_projects(active_only=True)
    c1, c2, c3 = st.columns([2, 2, 5])
    c1.metric("Active projects", len(projects))
    department = c2.segmented_control("Department", DEPARTMENTS, default="RS", key="ptm_queue_department")
    st.session_state["ptm_presence_department"] = department
    st.session_state["ptm_presence_view"] = "Projects / Work Queue"

    if is_admin:
        sync_col, migrate_col, export_col = c3.columns(3)
        if sync_col.button("Sync High Level now", key="ptm_high_level_sync"):
            try:
                result = sync_high_level_projects()
            except GoogleSheetsConfigurationError as exc:
                st.error(str(exc))
            except Exception as exc:
                st.error(f"High Level sync failed: {exc}")
            else:
                st.success(
                    f"High Level synced: {result['inserted']} new, "
                    f"{result['updated']} updated, {result['unchanged']} unchanged."
                )
                st.rerun()
        if migrate_col.button("Import current PTM snapshot", key="ptm_legacy_migration"):
            try:
                sync_high_level_projects()
                result = migrate_ptm_snapshot(user=user)
            except GoogleSheetsConfigurationError as exc:
                st.error(str(exc))
            except Exception as exc:
                st.error(f"PTM migration failed: {exc}")
            else:
                st.success(
                    "PTM snapshot imported without overwriting existing Planner edits. "
                    f"People: {result['people']['inserted']} new / {result['people']['updated']} updated; "
                    f"project allocations: {result['project_allocations_inserted']} new."
                )
                if result["skipped_people"]:
                    st.warning(
                        "Skipped roster records without a valid RS/GIS/PLS home department: "
                        + ", ".join(result["skipped_people"])
                    )
                st.rerun()
        if export_col.button("Publish to High Level", key="ptm_high_level_export"):
            try:
                result = export_to_high_level()
            except GoogleSheetsConfigurationError as exc:
                st.error(str(exc))
            except Exception as exc:
                st.error(f"High Level export failed: {exc}")
            else:
                st.success(
                    "High Level updated: "
                    + ", ".join(f"{sheet} {rows} rows" for sheet, rows in result.items())
                    + ". Projects tab was not modified."
                )

    try:
        frame = _queue_frame(department)
    except Exception as exc:
        st.error(f"Work Queue could not be built: {exc}")
        return

    if frame.empty:
        st.info(
            "No High Level projects have been synced into the new Planner store yet. "
            "An admin can use 'Sync High Level now' once the Google service account is configured."
        )
        return

    st.dataframe(
        frame,
        hide_index=True,
        use_container_width=True,
        column_config={
            "PM Deadline": st.column_config.DateColumn(format="DD MMM YYYY"),
            "Upstream Ready": st.column_config.TextColumn(),
            "Bid h": st.column_config.NumberColumn(format="%.1f"),
            "Estimate h": st.column_config.NumberColumn(format="%.1f"),
            "Actual h": st.column_config.NumberColumn(format="%.1f"),
            "Remaining h": st.column_config.NumberColumn(format="%.1f"),
            "Planned h": st.column_config.NumberColumn(format="%.1f"),
            "Forecast": st.column_config.DateColumn(format="DD MMM YYYY"),
        },
    )

    st.markdown("#### Manager inputs")
    choices = {
        f"{row['Project Code']} · {row['Project']}": row["Project Code"]
        for row in build_work_queue()[department]
    }
    selected_label = st.selectbox("Project", list(choices), key="ptm_manager_input_project")
    code = choices[selected_label]
    st.session_state["ptm_presence_project"] = code
    st.session_state["ptm_presence_scope"] = "manager inputs"
    heartbeat_session(
        _presence_session_id(),
        user_email=st.session_state.user_email,
        display_name=st.session_state.display_name,
        role=st.session_state.role,
        **_presence_context(),
    )

    current = get_stage_input(code, department)
    version = int(current["version"]) if current else 0
    updated_text = ""
    if current:
        updated_text = f"Version {version} · last saved by {current['updated_by']}"
    st.caption(updated_text or "No manager inputs saved yet.")

    with st.form(f"ptm_stage_input_{department}_{code}"):
        a, b, c = st.columns(3)
        estimate_text = a.text_input(
            "PTM Estimate h",
            "" if not current or current.get("estimate_hours") is None else str(current["estimate_hours"]),
            help="Blank = use Bid hours as the planning baseline.",
        )
        override_text = b.text_input(
            "Remaining Override h",
            "" if not current or current.get("remaining_override_hours") is None else str(current["remaining_override_hours"]),
            help="Blank = Remaining is calculated from Estimate/Bid minus Actual.",
        )
        handover = c.date_input(
            "Handover start",
            value=None if not current else current.get("handover_start"),
            help="Manual downstream readiness date. Leave blank to derive from the upstream plan.",
        )
        notes = st.text_area("Manager notes", "" if not current else str(current.get("notes") or ""))
        submitted = st.form_submit_button("Save manager inputs", type="primary")

    if submitted:
        try:
            estimate = _parse_optional_number(estimate_text, "PTM Estimate")
            override = _parse_optional_number(override_text, "Remaining Override")
            save_stage_input(
                code,
                department,
                estimate_hours=estimate,
                remaining_override_hours=override,
                handover_start=handover,
                notes=notes,
                user=user,
                expected_version=version,
            )
        except StageInputConflict as exc:
            current_row = exc.current or {}
            st.error(
                "Conflict: another user changed these manager inputs before your save. "
                f"Current version is {current_row.get('version', '?')} "
                f"by {current_row.get('updated_by', 'another user')}. Reload before saving again."
            )
        except ValueError as exc:
            st.error(str(exc))
        else:
            st.success("Manager inputs saved.")
            st.rerun()


def _planning_grid(user: str) -> None:
    st.subheader("RS / GIS / PLS Planner")
    projects = list_projects(active_only=True)
    if not projects:
        st.info("Sync High Level projects before planning.")
        return

    top1, top2, top3 = st.columns([2, 2, 3])
    department = top1.segmented_control(
        "Department", DEPARTMENTS, default="RS", key="ptm_grid_department"
    )
    horizon = int(
        top2.selectbox("Horizon", [4, 8, 12, 16, 26], index=2, format_func=lambda n: f"{n} weeks")
    )
    today = date.today()
    default_start = today - timedelta(days=today.weekday())
    start_week = top3.date_input("Planning start", default_start)
    if isinstance(start_week, pd.Timestamp):
        start_week = start_week.date()
    start_week = start_week - timedelta(days=start_week.weekday())

    queue = build_work_queue()[department]
    if not queue:
        st.info(f"No active {department} project demand.")
        return
    choices = {
        f"{row['Project Code']} · {row['Project']}": row["Project Code"] for row in queue
    }
    selected_label = st.selectbox("Project", list(choices), key="ptm_grid_project")
    project_code = choices[selected_label]

    st.session_state["ptm_presence_view"] = f"Planner {department}"
    st.session_state["ptm_presence_department"] = department
    st.session_state["ptm_presence_project"] = project_code
    st.session_state["ptm_presence_scope"] = "weekly allocations"
    heartbeat_session(
        _presence_session_id(),
        user_email=st.session_state.user_email,
        display_name=st.session_state.display_name,
        role=st.session_state.role,
        **_presence_context(),
    )

    workload = project_workload_card(project_code, department)
    metrics = st.columns(6)
    metrics[0].metric("Bid", "—" if workload["Bid h"] is None else f"{workload['Bid h']:,.1f} h")
    metrics[1].metric("Estimate", "—" if workload["Estimate h"] is None else f"{workload['Estimate h']:,.1f} h")
    metrics[2].metric("Actual", f"{workload['Actual h']:,.1f} h")
    metrics[3].metric("Remaining", "—" if workload["Remaining h"] is None else f"{workload['Remaining h']:,.1f} h")
    metrics[4].metric("Planned", f"{workload['Planned h']:,.1f} h")
    metrics[5].metric("Action", workload["Action"])

    snapshot_key = (
        f"ptm_grid_snapshot::{department}::{project_code}::"
        f"{start_week.isoformat()}::{horizon}"
    )
    active_snapshot_key = "ptm_active_grid_snapshot"
    if st.session_state.get(active_snapshot_key) != snapshot_key:
        st.session_state[active_snapshot_key] = snapshot_key

    if snapshot_key not in st.session_state:
        frame, versions, weeks = build_department_grid(
            department,
            project_code,
            start_week=start_week,
            horizon_weeks=horizon,
        )
        st.session_state[snapshot_key] = {
            "frame": frame.to_dict("records"),
            "versions": versions,
            "weeks": [week.isoformat() for week in weeks],
        }

    snapshot = st.session_state[snapshot_key]
    original = pd.DataFrame(snapshot["frame"])
    weeks = [date.fromisoformat(value) for value in snapshot["weeks"]]
    versions = snapshot["versions"]

    if original.empty:
        st.warning(
            "No processors are available in this department yet. "
            "Roster / temporary assignments can be added in the Resource migration step."
        )
        return

    week_columns = [week.isoformat() for week in weeks]
    disabled = ["Person", "Role", "H Available", "H Left", "Time Off"]
    column_config = {
        "H Available": st.column_config.NumberColumn(format="%.1f"),
        "H Left": st.column_config.NumberColumn(format="%.1f"),
        "Time Off": st.column_config.CheckboxColumn(),
    }
    for week in weeks:
        column_config[week.isoformat()] = st.column_config.NumberColumn(
            week.strftime("%d %b"),
            min_value=0.0,
            step=0.5,
            format="%.1f",
        )

    st.caption(
        "Edits stay in your session until Save changes. Only changed cells are written. "
        "If another manager saves the same cell first, your save is rejected rather than overwriting it."
    )
    edited = st.data_editor(
        original,
        hide_index=True,
        use_container_width=True,
        disabled=disabled,
        column_config=column_config,
        key=f"ptm_grid_editor::{snapshot_key}",
    )

    changes = changed_grid_cells(
        original,
        edited,
        project_code=project_code,
        department=department,
        weeks=weeks,
        versions=versions,
    )
    save_col, reload_col, info_col = st.columns([2, 2, 6])
    save_clicked = save_col.button(
        f"Save changes ({len(changes)})",
        type="primary",
        disabled=not changes,
        key=f"ptm_grid_save::{snapshot_key}",
    )
    if reload_col.button("Reload latest", key=f"ptm_grid_reload::{snapshot_key}"):
        st.session_state.pop(snapshot_key, None)
        st.session_state.pop(f"ptm_grid_editor::{snapshot_key}", None)
        st.rerun()
    info_col.caption(
        "Snapshot is intentionally held stable while you edit; Reload latest discards unsaved local edits."
    )

    if save_clicked:
        try:
            save_grid_changes(changes, user=user)
        except AllocationConflict as exc:
            current = exc.current or {}
            st.error(
                "Conflict: another user saved one of the same allocation cells first. "
                f"Current value: {current.get('hours', '?')} h, "
                f"version {current.get('version', '?')}, by {current.get('updated_by', 'another user')}. "
                "Your batch was rolled back. Reload latest before retrying."
            )
        except ValueError as exc:
            st.error(str(exc))
        else:
            st.session_state.pop(snapshot_key, None)
            st.session_state.pop(f"ptm_grid_editor::{snapshot_key}", None)
            st.success(f"Saved {len(changes)} changed allocation cell(s).")
            st.rerun()

    st.markdown("#### Capacity summary")
    summary = allocation_grid_summary(
        department,
        project_code,
        start_week=start_week,
        horizon_weeks=horizon,
    )
    if not summary.empty:
        summary_config = {
            week.isoformat(): st.column_config.NumberColumn(
                week.strftime("%d %b"), format="%.1f"
            )
            for week in weeks
        }
        st.dataframe(
            summary,
            hide_index=True,
            use_container_width=True,
            column_config=summary_config,
        )


def _teams_breakdown() -> None:
    st.subheader("Teams Breakdown")
    c1, c2, c3, c4, c5 = st.columns([2, 2, 2, 2, 2])
    view_mode = c1.selectbox(
        "View Mode", ["By Department", "By Project"], key="ptm_tb_view_mode"
    )
    department = c2.selectbox(
        "Department", ["All", *DEPARTMENTS], key="ptm_tb_department"
    )
    scope = c3.selectbox(
        "Project Scope",
        ["All", "Projects Only", "Activities Only", "Selected Projects"],
        key="ptm_tb_scope",
    )
    today = date.today()
    default_start = today - timedelta(days=today.weekday())
    start_week = c4.date_input("Start Week", default_start, key="ptm_tb_start")
    start_week = start_week - timedelta(days=start_week.weekday())
    horizon = int(
        c5.selectbox(
            "Horizon", [4, 8, 12, 16, 26], index=2,
            format_func=lambda n: f"{n} weeks", key="ptm_tb_horizon",
        )
    )

    selected_projects: list[str] = []
    if scope == "Selected Projects":
        projects = list_projects(active_only=True)
        options = {
            f"{row['project_code']} · {row['project_name']}": row["project_code"]
            for row in projects
        }
        selected_labels = st.multiselect(
            "Selected projects",
            list(options),
            key="ptm_tb_selected_projects",
        )
        selected_projects = [options[label] for label in selected_labels]

    st.session_state["ptm_presence_view"] = "Teams Breakdown"
    st.session_state["ptm_presence_department"] = None if department == "All" else department
    st.session_state["ptm_presence_project"] = None
    st.session_state["ptm_presence_scope"] = "read only"

    frame, summary = build_teams_breakdown(
        start_week=start_week,
        horizon_weeks=horizon,
        view_mode=view_mode,
        department=department,
        scope=scope,
        selected_projects=selected_projects,
    )
    if frame.empty:
        st.info("No rows match the current Teams Breakdown filters.")
    else:
        week_columns = [
            column for column in frame.columns
            if len(str(column)) == 10 and str(column)[4] == "-" and str(column)[7] == "-"
        ]
        config = {
            "Remaining h": st.column_config.NumberColumn(format="%.1f"),
            "Assigned h": st.column_config.NumberColumn(format="%.1f"),
            "Gap vs Remaining": st.column_config.NumberColumn(format="%.1f"),
            "Actual h": st.column_config.NumberColumn(format="%.1f"),
        }
        for column in week_columns:
            config[column] = st.column_config.NumberColumn(
                date.fromisoformat(column).strftime("%d %b"), format="%.1f"
            )
        st.dataframe(
            frame,
            hide_index=True,
            use_container_width=True,
            column_config=config,
        )

    st.markdown("#### Department capacity")
    if not summary.empty:
        summary_config = {}
        for column in summary.columns:
            if len(str(column)) == 10 and str(column)[4] == "-" and str(column)[7] == "-":
                summary_config[column] = st.column_config.NumberColumn(
                    date.fromisoformat(column).strftime("%d %b"), format="%.1f"
                )
        st.dataframe(
            summary,
            hide_index=True,
            use_container_width=True,
            column_config=summary_config,
        )


def _resource_management(user: str) -> None:
    st.subheader("People / Time Off")
    st.caption(
        "All edits are row-level. Filtering this view never deletes hidden roster records."
    )
    people_tab, assignment_tab, time_off_tab = st.tabs(
        ["People", "Temporary assignments", "Time Off"]
    )

    with people_tab:
        people = list_people(active_only=False)
        today = date.today()
        current_week = today - timedelta(days=today.weekday())
        capacity_rows = weekly_person_capacity(current_week, 1)
        capacity_by_person: dict[str, float] = {}
        for row in capacity_rows:
            capacity_by_person[row["person_name"]] = (
                capacity_by_person.get(row["person_name"], 0.0)
                + float(row.get("available_hours") or 0)
            )

        show = st.segmented_control(
            "Roster view",
            ["Active", "Inactive", "All"],
            default="Active",
            key="ptm_people_filter",
        )
        filtered = [
            row for row in people
            if show == "All"
            or (show == "Active" and bool(row["active"]))
            or (show == "Inactive" and not bool(row["active"]))
        ]
        display = pd.DataFrame(
            [
                {
                    "Name": row["person_name"],
                    "Home Department": row["home_department"],
                    "Primary Role": row.get("primary_role"),
                    "Standard h/day": row["standard_hours_day"],
                    "Active": bool(row["active"]),
                    "Active From": row.get("active_from"),
                    "Active To": row.get("active_to"),
                    "Available this week": round(capacity_by_person.get(row["person_name"], 0), 1),
                    "Version": row["version"],
                    "Updated by": row["updated_by"],
                }
                for row in filtered
            ]
        )
        if display.empty:
            st.info("No people match the selected roster filter.")
        else:
            st.dataframe(
                display,
                hide_index=True,
                use_container_width=True,
                column_config={
                    "Standard h/day": st.column_config.NumberColumn(format="%.1f"),
                    "Available this week": st.column_config.NumberColumn(format="%.1f"),
                    "Active From": st.column_config.DateColumn(format="DD MMM YYYY"),
                    "Active To": st.column_config.DateColumn(format="DD MMM YYYY"),
                },
            )

        st.markdown("#### Add or edit person")
        choices = ["+ New person"] + [row["person_name"] for row in people]
        selected = st.selectbox("Person", choices, key="ptm_people_edit")
        current = None if selected == "+ New person" else next(
            row for row in people if row["person_name"] == selected
        )
        version = 0 if current is None else int(current["version"])
        with st.form("ptm_person_form"):
            a, b, c1, d = st.columns(4)
            name = a.text_input(
                "Name",
                "" if current is None else current["person_name"],
                disabled=current is not None,
                help="Existing names are stable record keys during the migration.",
            )
            home = b.selectbox(
                "Home Department",
                DEPARTMENTS,
                index=0 if current is None else DEPARTMENTS.index(current["home_department"]),
            )
            standard = c1.number_input(
                "Standard h/day",
                min_value=0.5,
                max_value=24.0,
                step=0.5,
                value=7.5 if current is None else float(current["standard_hours_day"]),
            )
            active = d.checkbox("Active", value=True if current is None else bool(current["active"]))
            e, f = st.columns(2)
            primary = e.text_input(
                "Primary Role", "" if current is None else str(current.get("primary_role") or "")
            )
            secondary = f.text_input(
                "Secondary Role", "" if current is None else str(current.get("secondary_role") or "")
            )
            g, h = st.columns(2)
            active_from = g.date_input(
                "Active From",
                value=None if current is None else current.get("active_from"),
            )
            active_to = h.date_input(
                "Active To",
                value=None if current is None else current.get("active_to"),
            )
            submitted = st.form_submit_button("Save person", type="primary")

        if submitted:
            try:
                save_person(
                    name,
                    home_department=home,
                    primary_role=primary,
                    secondary_role=secondary,
                    standard_hours_day=standard,
                    active=active,
                    active_from=active_from,
                    active_to=active_to,
                    user=user,
                    expected_version=version,
                )
            except ResourceConflict as exc:
                current_row = exc.current or {}
                st.error(
                    "Conflict: another user changed this roster row first. "
                    f"Current version: {current_row.get('version', '?')} "
                    f"by {current_row.get('updated_by', 'another user')}. Reload before retrying."
                )
            except ValueError as exc:
                st.error(str(exc))
            else:
                st.success("Person saved.")
                st.rerun()

    with assignment_tab:
        people = list_people(active_only=False)
        assignments = list_assignments()
        display = pd.DataFrame(
            [
                {
                    "ID": row["id"],
                    "Name": row["person_name"],
                    "From": row["from_date"],
                    "To": row.get("to_date"),
                    "Target Department": row["department"],
                    "Allocation %": float(row["allocation_percent"]),
                    "Assignment Type": row["assignment_type"],
                    "Notes": row.get("notes"),
                    "Version": row["version"],
                    "Updated by": row["updated_by"],
                }
                for row in assignments
            ]
        )
        if not display.empty:
            st.dataframe(
                display,
                hide_index=True,
                use_container_width=True,
                column_config={
                    "From": st.column_config.DateColumn(format="DD MMM YYYY"),
                    "To": st.column_config.DateColumn(format="DD MMM YYYY"),
                    "Allocation %": st.column_config.NumberColumn(format="%.0f%%"),
                },
            )
        else:
            st.info("No temporary assignments recorded.")

        st.markdown("#### Add or edit assignment")
        assignment_choices = ["+ New assignment"] + [
            f"{row['id']} · {row['person_name']} · {row['department']} · {row['from_date']}"
            for row in assignments
        ]
        selected_assignment = st.selectbox(
            "Assignment", assignment_choices, key="ptm_assignment_edit"
        )
        current_assignment = None
        if selected_assignment != "+ New assignment":
            assignment_id = int(selected_assignment.split(" · ", 1)[0])
            current_assignment = next(
                row for row in assignments if int(row["id"]) == assignment_id
            )
        person_names = [row["person_name"] for row in people]
        if not person_names:
            st.warning("Add roster people before creating temporary assignments.")
        else:
            with st.form("ptm_assignment_form"):
                a, b, c1 = st.columns(3)
                selected_person = a.selectbox(
                    "Person",
                    person_names,
                    index=0 if current_assignment is None else person_names.index(current_assignment["person_name"]),
                )
                from_date = b.date_input(
                    "From",
                    value=date.today() if current_assignment is None else current_assignment["from_date"],
                )
                to_date = c1.date_input(
                    "To",
                    value=None if current_assignment is None else current_assignment.get("to_date"),
                )
                d, e, f = st.columns(3)
                target = d.selectbox(
                    "Target Department",
                    DEPARTMENTS,
                    index=0 if current_assignment is None else DEPARTMENTS.index(current_assignment["department"]),
                )
                allocation_pct = e.number_input(
                    "Allocation %",
                    min_value=1,
                    max_value=100,
                    step=5,
                    value=100 if current_assignment is None else int(round(float(current_assignment["allocation_percent"]) * 100)),
                )
                assignment_type = f.selectbox(
                    "Assignment Type",
                    ["TEMP_SUPPORT", "SECONDMENT"],
                    index=0 if current_assignment is None else (
                        1 if current_assignment["assignment_type"] == "SECONDMENT" else 0
                    ),
                )
                notes = st.text_area(
                    "Notes",
                    "" if current_assignment is None else str(current_assignment.get("notes") or ""),
                )
                submitted_assignment = st.form_submit_button(
                    "Save assignment", type="primary"
                )

            if submitted_assignment:
                try:
                    save_assignment(
                        assignment_id=None if current_assignment is None else int(current_assignment["id"]),
                        person_name=selected_person,
                        from_date=from_date,
                        to_date=to_date,
                        department=target,
                        allocation_percent=float(allocation_pct) / 100,
                        assignment_type=assignment_type,
                        notes=notes,
                        user=user,
                        expected_version=0 if current_assignment is None else int(current_assignment["version"]),
                    )
                except ResourceConflict as exc:
                    current_row = exc.current or {}
                    st.error(
                        "Conflict: another user changed this assignment first. "
                        f"Current version: {current_row.get('version', '?')}."
                    )
                except ValueError as exc:
                    st.error(str(exc))
                else:
                    st.success("Temporary assignment saved.")
                    st.rerun()

    with time_off_tab:
        people = list_people(active_only=False)
        time_off_rows = list_time_off()
        display = pd.DataFrame(
            [
                {
                    "ID": row["id"],
                    "Name": row["person_name"],
                    "From": row["from_date"],
                    "To": row.get("to_date"),
                    "Type": row["time_off_type"],
                    "Available h/day": row["available_hours_day"],
                    "Notes": row.get("notes"),
                    "Version": row["version"],
                    "Updated by": row["updated_by"],
                }
                for row in time_off_rows
            ]
        )
        if not display.empty:
            st.dataframe(
                display,
                hide_index=True,
                use_container_width=True,
                column_config={
                    "From": st.column_config.DateColumn(format="DD MMM YYYY"),
                    "To": st.column_config.DateColumn(format="DD MMM YYYY"),
                    "Available h/day": st.column_config.NumberColumn(format="%.1f"),
                },
            )
        else:
            st.info("No Time Off records.")

        st.markdown("#### Add or edit Time Off")
        time_off_choices = ["+ New Time Off"] + [
            f"{row['id']} · {row['person_name']} · {row['from_date']}"
            for row in time_off_rows
        ]
        selected_time_off = st.selectbox(
            "Time Off record", time_off_choices, key="ptm_time_off_edit"
        )
        current_time_off = None
        if selected_time_off != "+ New Time Off":
            time_off_id = int(selected_time_off.split(" · ", 1)[0])
            current_time_off = next(
                row for row in time_off_rows if int(row["id"]) == time_off_id
            )
        person_names = [row["person_name"] for row in people]
        if not person_names:
            st.warning("Add roster people before creating Time Off.")
        else:
            with st.form("ptm_time_off_form"):
                a, b, c1 = st.columns(3)
                selected_person = a.selectbox(
                    "Person",
                    person_names,
                    index=0 if current_time_off is None else person_names.index(current_time_off["person_name"]),
                    key="ptm_time_off_person",
                )
                from_date = b.date_input(
                    "From",
                    value=date.today() if current_time_off is None else current_time_off["from_date"],
                    key="ptm_time_off_from",
                )
                to_date = c1.date_input(
                    "To",
                    value=None if current_time_off is None else current_time_off.get("to_date"),
                    key="ptm_time_off_to",
                )
                d, e = st.columns(2)
                types = ["HOLIDAY", "SICK", "TRAINING", "APPOINTMENT", "OTHER"]
                off_type = d.selectbox(
                    "Type",
                    types,
                    index=0 if current_time_off is None or current_time_off["time_off_type"] not in types else types.index(current_time_off["time_off_type"]),
                )
                available = e.number_input(
                    "Available h/day",
                    min_value=0.0,
                    max_value=24.0,
                    step=0.5,
                    value=0.0 if current_time_off is None else float(current_time_off["available_hours_day"]),
                    help="0 = unavailable. 4 = half-day availability for an 8 h/day person.",
                )
                notes = st.text_area(
                    "Notes",
                    "" if current_time_off is None else str(current_time_off.get("notes") or ""),
                    key="ptm_time_off_notes",
                )
                submitted_time_off = st.form_submit_button("Save Time Off", type="primary")

            if submitted_time_off:
                try:
                    save_time_off(
                        time_off_id=None if current_time_off is None else int(current_time_off["id"]),
                        person_name=selected_person,
                        from_date=from_date,
                        to_date=to_date or from_date,
                        time_off_type=off_type,
                        available_hours_day=available,
                        notes=notes,
                        user=user,
                        expected_version=0 if current_time_off is None else int(current_time_off["version"]),
                    )
                except ResourceConflict as exc:
                    current_row = exc.current or {}
                    st.error(
                        "Conflict: another user changed this Time Off record first. "
                        f"Current version: {current_row.get('version', '?')}."
                    )
                except ValueError as exc:
                    st.error(str(exc))
                else:
                    st.success("Time Off saved. Capacity will reflect the change immediately.")
                    st.rerun()


def render_ptm_v2(user: str, *, is_admin: bool = False) -> None:
    init_planner_store()
    st.header("PTM Planner")
    st.caption(
        "Multi-user Planner foundation: source-controlled High Level facts, "
        "row-level manager writes, optimistic conflict protection and live presence."
    )
    section = st.segmented_control(
        "Planner area",
        ["Projects / Work Queue", "RS / GIS / PLS", "Teams Breakdown", "People / Time Off"],
        default="Projects / Work Queue",
        key="ptm_v2_section",
    )
    # Render one PTM area only. This keeps grid edits fast and also makes presence
    # reflect the screen the user is actually working in.
    if section == "Projects / Work Queue":
        _work_queue(user, is_admin=is_admin)
    elif section == "RS / GIS / PLS":
        _planning_grid(user)
    elif section == "Teams Breakdown":
        _teams_breakdown()
    else:
        st.session_state["ptm_presence_view"] = "People / Time Off"
        st.session_state["ptm_presence_department"] = None
        st.session_state["ptm_presence_project"] = None
        st.session_state["ptm_presence_scope"] = "resources"
        _resource_management(user)

    render_presence_bar()
