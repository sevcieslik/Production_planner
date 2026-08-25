from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import streamlit as st

from app.data.db import initialize_database, rows
from app.services.mvp import (
    DISCIPLINES,
    PROJECT_DATE_COLUMNS,
    RESOURCE_DATE_COLUMNS,
    complete_planning_review,
    create_escalation,
    ensure_mvp_schema,
    get_projects,
    get_resources,
    import_sample_roster,
    load_roster_csv,
    manager_plan,
    prepare_date_columns_for_editor,
    save_manager_plan,
    save_projects,
    save_resources,
    validate_project_demand,
    week_starts,
    weekly_department_capacity,
)

st.set_page_config(page_title="Production Capacity Planner", layout="wide")
# Production must start empty. Demo data remains available only to tests/development code.
initialize_database(seed=False)
ensure_mvp_schema()


def monday(value: date) -> date:
    return value - timedelta(days=value.weekday())


def rerun() -> None:
    st.cache_data.clear()
    st.rerun()


st.sidebar.title("Production Planner")
st.sidebar.caption("Demand → capacity plan → escalation")
user = st.sidebar.text_input("Your name", help="Recorded against manager planning changes and reviews.")
planning_start = st.sidebar.date_input("Planning start", monday(date.today()))
planning_end = st.sidebar.date_input("Planning end", monday(date.today()) + timedelta(weeks=12))
weeks = week_starts(planning_start, planning_end) if planning_end >= planning_start else []

demand_tab, capacity_tab = st.tabs(["1 · Project demand", "2 · Capacity plan"])

with demand_tab:
    st.title("Project demand")
    st.caption("PM/CDO input: record the scope, data availability and forecast hours. The system does not invent missing dates or hours.")

    existing = get_projects(True)
    options = ["Create new project"]
    if not existing.empty:
        options += [f"{r.project_code} · {r.project_name}" for r in existing.itertuples()]
    selected = st.selectbox("Project", options)
    current = {}
    if selected != options[0]:
        code = selected.split(" · ", 1)[0]
        current = existing[existing.project_code == code].iloc[0].to_dict()

    with st.form("project_demand_form"):
        st.subheader("Project and contractual facts")
        a, b, c = st.columns(3)
        project_code = a.text_input("Project code *", value=str(current.get("project_code") or ""), disabled=bool(current))
        project_name = b.text_input("Project name *", value=str(current.get("project_name") or ""))
        client = c.text_input("Client *", value=str(current.get("client") or ""))
        pm = a.text_input("Project manager *", value=str(current.get("project_manager") or ""))
        priority = b.selectbox("Priority *", ["P1", "P2", "P3"], index=["P1", "P2", "P3"].index(current.get("priority") or "P3"))
        penalty = c.selectbox("Late-delivery penalty", ["None", "Potential", "Active"], index=["None", "Potential", "Active"].index(current.get("penalty_exposure") or "None"))
        start_date = a.date_input("Production start *", value=pd.to_datetime(current.get("start_date"), errors="coerce").date() if current.get("start_date") else None)
        end_date = b.date_input("Required completion *", value=pd.to_datetime(current.get("end_date"), errors="coerce").date() if current.get("end_date") else None)
        status = c.selectbox("Status", ["draft", "active", "on_hold", "completed", "archived"], index=["draft", "active", "on_hold", "completed", "archived"].index(current.get("status") or "draft"))

        st.subheader("Scope quantities")
        q1, q2, q3 = st.columns(3)
        row_km = q1.number_input("ROW length (km)", min_value=0.0, value=float(current.get("row_km") or 0))
        cct_km = q2.number_input("Circuit length (km)", min_value=0.0, value=float(current.get("cct_km") or 0))
        spus = q3.number_input("SPUs", min_value=0.0, value=float(current.get("spus") or 0))

        st.subheader("Discipline demand and data availability")
        st.caption("Hours are the current forecast totals. Actuals are source-controlled and reduce the remaining demand shown below.")
        discipline_values = {}
        for discipline in DISCIPLINES:
            d1, d2, d3 = st.columns([1, 1, 2])
            key = discipline.lower()
            hours = d1.number_input(f"{discipline} forecast hours", min_value=0.0, value=float(current.get(f"{key}_hours") or 0), key=f"{key}_hours")
            available = d2.date_input(f"{discipline} data available", value=pd.to_datetime(current.get(f"{key}_start_date"), errors="coerce").date() if current.get(f"{key}_start_date") else None, key=f"{key}_available")
            actual = float(current.get(f"actual_{key}_hours") or 0)
            d3.metric(f"{discipline} remaining", f"{max(hours - actual, 0):,.1f} h", help=f"{hours:,.1f} forecast − {actual:,.1f} actual")
            discipline_values[key] = (hours, available, actual)
        assumptions = st.text_area("Assumptions / evidence", value=str(current.get("assumptions") or ""), help="Explain forecast-hour deviations, uncertain dates or material planning assumptions.")
        submitted = st.form_submit_button("Save project demand", type="primary")

    if submitted:
        record = {
            "project_code": project_code, "project_name": project_name, "client": client,
            "project_manager": pm, "priority": priority, "penalty_exposure": penalty,
            "row_km": row_km, "cct_km": cct_km, "spus": spus,
            "start_date": start_date, "end_date": end_date, "loading_type": "even",
            "status": status, "assumptions": assumptions,
        }
        for key, (hours, available, actual) in discipline_values.items():
            record[f"{key}_hours"] = hours
            record[f"{key}_start_date"] = available
            record[f"actual_{key}_hours"] = actual
        errors = validate_project_demand(record)
        if errors:
            for error in errors:
                st.error(error)
        else:
            save_projects([record])
            st.success("Project demand saved. It is now available for manager planning.")
            rerun()

    st.subheader("Demand register")
    register = get_projects(False)
    if register.empty:
        st.info("No active project demand has been entered.")
    else:
        display = register[["priority", "project_code", "project_name", "client", "project_manager", "spus", "row_km", "cct_km", "rs_hours", "gis_hours", "pls_hours", "end_date", "penalty_exposure", "status"]]
        st.dataframe(display, use_container_width=True, hide_index=True)

with capacity_tab:
    st.title("Capacity plan")
    st.caption("Processing managers smooth remaining demand against real recorded capacity, or escalate what cannot be resolved.")
    if not weeks:
        st.error("Planning end must not be before planning start.")
    else:
        department = st.segmented_control(
            "Capacity view", ["All", *DISCIPLINES], default="All",
            help="View the combined roster capacity or select a department to edit its project plan.",
        )
        capacity = weekly_department_capacity(weeks)
        if department == "All":
            week_columns = [w.isoformat() for w in weeks]
            summary_rows = []
            total_capacity = {week: 0.0 for week in week_columns}
            total_demand = {week: 0.0 for week in week_columns}
            remaining = unplanned = 0.0
            for discipline in DISCIPLINES:
                discipline_plan = manager_plan(weeks, discipline)
                discipline_capacity = (
                    capacity[capacity.department == discipline]
                    .set_index("week_start")["available_capacity"].to_dict()
                    if not capacity.empty else {}
                )
                demand = {week: float(discipline_plan[week].sum()) if not discipline_plan.empty else 0.0 for week in week_columns}
                remaining += float(discipline_plan["Remaining Hours"].sum()) if not discipline_plan.empty else 0.0
                unplanned += float(discipline_plan["Unplanned Hours"].sum()) if not discipline_plan.empty else 0.0
                for week in week_columns:
                    total_capacity[week] += discipline_capacity.get(week, 0.0)
                    total_demand[week] += demand[week]
                summary_rows.extend([
                    {"Measure": f"{discipline} available capacity", **{w: discipline_capacity.get(w, 0.0) for w in week_columns}},
                    {"Measure": f"{discipline} planned demand", **demand},
                    {"Measure": f"{discipline} over / under", **{w: discipline_capacity.get(w, 0.0) - demand[w] for w in week_columns}},
                ])
            shortage = sum(max(total_demand[w] - total_capacity[w], 0) for w in week_columns)
            m1, m2, m3 = st.columns(3)
            m1.metric("All roster capacity", f"{sum(total_capacity.values()):,.1f} h")
            m2.metric("All remaining demand", f"{remaining:,.1f} h")
            m3.metric("Combined weekly shortage", f"{shortage:,.1f} h")
            summary_rows.extend([
                {"Measure": "TOTAL available capacity", **total_capacity},
                {"Measure": "TOTAL planned demand", **total_demand},
                {"Measure": "TOTAL over / under", **{w: total_capacity[w] - total_demand[w] for w in week_columns}},
            ])
            st.dataframe(pd.DataFrame(summary_rows), use_container_width=True, hide_index=True)
            if unplanned:
                st.caption(f"{unplanned:,.1f} hours are not yet assigned to a week.")
            st.info("Select RS, GIS or PLS above to edit and save that department's project plan.")
        if department != "All":
            plan = manager_plan(weeks, department)
            cap = capacity[capacity.department == department].set_index("week_start")["available_capacity"].to_dict() if not capacity.empty else {}

            if plan.empty:
                st.info(f"No active {department} demand is available. Enter it on Project demand first.")
            else:
                week_columns = [w.isoformat() for w in weeks]
                totals = {week: float(plan[week].sum()) for week in week_columns}
                summary = pd.DataFrame([
                    {"Measure": "Available capacity", **{w: cap.get(w, 0.0) for w in week_columns}},
                    {"Measure": "Planned demand", **totals},
                    {"Measure": "Over / under", **{w: cap.get(w, 0.0) - totals[w] for w in week_columns}},
                ])
                shortage = sum(max(totals[w] - cap.get(w, 0.0), 0) for w in week_columns)
                unplanned = float(plan["Unplanned Hours"].sum())
                m1, m2, m3 = st.columns(3)
                m1.metric("Remaining project demand", f"{plan['Remaining Hours'].sum():,.1f} h")
                m2.metric("Unplanned", f"{unplanned:,.1f} h")
                m3.metric("Weekly capacity shortage", f"{shortage:,.1f} h")
                st.dataframe(summary, use_container_width=True, hide_index=True)

                disabled = [c for c in plan.columns if c not in week_columns]
                edited = st.data_editor(plan, hide_index=True, use_container_width=True, disabled=disabled,
                                        column_config={w: st.column_config.NumberColumn(w, min_value=0.0, step=1.0) for w in week_columns})
                # Recalculate the explicit unresolved balance after edits.
                edited["Unplanned Hours"] = (edited["Remaining Hours"] - edited[week_columns].sum(axis=1)).clip(lower=0).round(2)
                if st.button("Save manager plan", type="primary", disabled=not user.strip()):
                    try:
                        save_manager_plan(edited, weeks, department, user.strip())
                        st.success("Manager plan saved.")
                        rerun()
                    except ValueError as exc:
                        st.error(str(exc))
                if not user.strip():
                    st.caption("Enter your name in the sidebar to save or complete a review.")

                with st.expander("Escalate an unresolved issue", expanded=unplanned > 0 or shortage > 0):
                    codes = plan["Project Code"].tolist()
                    with st.form("escalation_form"):
                        esc_project = st.selectbox("Project", codes)
                        issue = st.selectbox("Issue type", ["Capacity shortage", "Data delay", "Estimate uncertainty", "Priority conflict", "Skills gap", "Dependency issue"])
                        impact = st.number_input("Impact / shortage hours", min_value=0.0, value=round(max(unplanned, shortage), 1))
                        decision = st.text_area("Decision required *", placeholder="State the decision or trade-off required from leadership.")
                        owner = st.text_input("Escalation owner *")
                        required_by = st.date_input("Decision required by", date.today())
                        escalate = st.form_submit_button("Create escalation")
                    if escalate:
                        try:
                            create_escalation(esc_project, department, issue, impact, decision, owner, required_by)
                            st.success("Escalation created and linked to this department plan.")
                            rerun()
                        except ValueError as exc:
                            st.error(str(exc))

                if st.button("Complete planning review", disabled=not user.strip()):
                    try:
                        complete_planning_review(edited, department, planning_start, planning_end, user.strip())
                        st.success("Planning review completed.")
                        rerun()
                    except ValueError as exc:
                        st.error(str(exc))

            escalations = pd.DataFrame(rows("SELECT id,project_code,department,issue_type,impact_hours,decision_required,owner,required_by,status FROM planning_escalations ORDER BY status,required_by"))
            st.subheader("Open decisions and escalations")
            if escalations.empty:
                st.caption("No escalations have been recorded.")
            else:
                st.dataframe(escalations, use_container_width=True, hide_index=True)

with st.sidebar.expander("Administration", expanded=False):
    st.caption("Operational roster maintenance is separated from project planning.")
    if message := st.session_state.pop("roster_import_message", None):
        st.success(message)
    st.markdown("**Import roster**")
    st.caption("Upload a CSV or Excel roster. Required columns: Employee, Department and Hrs (or person_name, department and weekly_hours). Existing people are updated by name.")
    st.download_button(
        "Download CSV template",
        data="person_name,department,weekly_hours\nJane Smith,RS,37.5\n",
        file_name="roster-template.csv",
        mime="text/csv",
        use_container_width=True,
    )
    roster_upload = st.file_uploader("Roster file", type=["csv", "xlsx", "xls"], key="roster_upload")
    if roster_upload is not None:
        try:
            roster_preview = load_roster_csv(roster_upload)
            st.dataframe(roster_preview, hide_index=True, use_container_width=True)
            valid_rows = int(
                (roster_preview["person_name"].fillna("").astype(str).str.strip().ne("") &
                 roster_preview["department"].isin(DISCIPLINES)).sum()
            )
            st.caption(f"{valid_rows} of {len(roster_preview)} rows are ready to import.")
            if st.button("Import and save roster", disabled=not user.strip(), type="primary", use_container_width=True):
                roster_upload.seek(0)
                result = import_sample_roster(roster_upload)
                st.session_state["roster_import_message"] = (
                    f"Roster saved: {result.imported_people_count} added, "
                    f"{result.updated_people_count} updated, {result.skipped_rows} skipped."
                )
                if result.validation_issues:
                    st.warning("\n".join(result.validation_issues))
                rerun()
            if not user.strip():
                st.caption("Enter your name above to import and save the roster.")
        except (ValueError, TypeError, KeyError) as exc:
            st.error(f"Could not read roster: {exc}")
    st.divider()
    st.markdown("**Edit roster manually**")
    resources = get_resources()
    if resources.empty:
        resources = pd.DataFrame(columns=["person_name", "department", "weekly_hours", "holiday_booked_hours", "holiday_remaining_hours", "active_status", "status_reason", "status_start_date", "status_end_date"])
    resource_editor = prepare_date_columns_for_editor(resources, RESOURCE_DATE_COLUMNS)
    edited_resources = st.data_editor(resource_editor, num_rows="dynamic", hide_index=True, key="admin_resources")
    if st.button("Save resources", disabled=not user.strip()):
        save_resources(edited_resources.to_dict("records"))
        st.success("Resource capacity saved.")
        rerun()
