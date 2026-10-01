from __future__ import annotations

from datetime import date
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
from app.services.high_level_sync import sync_high_level_projects
from app.services.ptm_planner_grid import (
    allocation_grid_summary,
    build_department_grid,
    changed_grid_cells,
    project_workload_card,
    save_grid_changes,
)
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
        if c3.button("Sync High Level now", key="ptm_high_level_sync"):
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
    default_start = today - pd.Timedelta(days=today.weekday())
    start_week = top3.date_input("Planning start", default_start)
    if isinstance(start_week, pd.Timestamp):
        start_week = start_week.date()
    start_week = start_week - pd.Timedelta(days=start_week.weekday())
    if isinstance(start_week, pd.Timestamp):
        start_week = start_week.date()

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


def render_ptm_v2(user: str, *, is_admin: bool = False) -> None:
    init_planner_store()
    st.header("PTM Planner")
    st.caption(
        "Multi-user Planner foundation: source-controlled High Level facts, "
        "row-level manager writes, optimistic conflict protection and live presence."
    )
    render_presence_bar()
    queue_tab, planning_tab, teams_tab = st.tabs(
        ["Projects / Work Queue", "RS / GIS / PLS", "Teams Breakdown"]
    )
    with queue_tab:
        _work_queue(user, is_admin=is_admin)
    with planning_tab:
        _planning_grid(user)
    with teams_tab:
        st.info(
            "Teams Breakdown will be read from the same PostgreSQL allocation records, "
            "not from a second planning table."
        )
