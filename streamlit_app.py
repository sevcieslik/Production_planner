from __future__ import annotations

from datetime import date, timedelta
import pandas as pd
import altair as alt
import streamlit as st

from app.auth import AuthenticationConfigurationError, authenticate, load_users, navigation_for_role
from app.data.db import connect, initialize_database, rows, write_audit
from app.services.mvp import (
    DISCIPLINES, RESOURCE_DATE_COLUMNS, allocation_timeline, apply_holiday_snapshot,
    apply_quick_allocation, capacity_balance, clear_future_allocation, create_escalation,
    ensure_mvp_schema, get_holidays, get_internal_activities, get_projects, get_resources,
    import_sample_roster, internal_activity_by_week, load_roster_csv, manager_plan,
    monthly_allocation_matrix, move_allocation, prepare_date_columns_for_editor,
    preview_holiday_snapshot, project_remaining_hours, project_health_plans, quick_allocation_values,
    save_internal_activities, save_manager_plan, save_projects, save_resources,
    validate_project_demand, week_starts, weekly_department_capacity, get_issues, update_issue,
    parse_audit_timestamps,
    TEMPORARY_ADJUSTMENT_TYPES, get_capacity_adjustments, save_capacity_adjustment,
    sequence_analysis, future_project_allocation, resource_availability_matrix,
    manager_allocations, project_capacity_statuses, ALLOCATION_FUTURE_HORIZON_WEEKS,
)
from app.services.setup_transfer import (
    apply_planner_setup, apply_project_import, export_planner_setup,
    export_projects_csv, preview_planner_setup, preview_project_import,
)
from app.ui.visuals import (
    AVAILABILITY_COLOURS, CAPACITY_COLOURS, DEPARTMENT_COLOURS,
    DEPARTMENT_TINTS, HEALTH_COLOURS, INTERNAL_ACTIVITY_COLOUR,
    availability_label, availability_style, project_colour, style_planning_table,
)

st.set_page_config(page_title="Production Capacity Planner", layout="wide")


def render_visual_legend() -> None:
    """Small shared legend; labels ensure colour is never the only signal."""
    items = [(d, DEPARTMENT_TINTS[d], DEPARTMENT_COLOURS[d]) for d in DISCIPLINES]
    items += [
        ("Partial", *AVAILABILITY_COLOURS["partial"]),
        ("Unavailable", *AVAILABILITY_COLOURS["unavailable"]),
        ("Temporary reassignment (→)", *AVAILABILITY_COLOURS["temporary"]),
    ]
    html = " ".join(
        f'<span style="display:inline-block;padding:3px 9px;margin:2px;border-radius:12px;'
        f'background:{bg};color:{fg};font-size:.82rem;font-weight:600">{label}</span>'
        for label, bg, fg in items
    )
    st.markdown(html, unsafe_allow_html=True)


def render_capacity_chart(balance: pd.DataFrame, department: str) -> None:
    """Explicit manager allocations and internal work as stacks, capacity as a line."""
    filtered_balance = balance if department == "All" else balance[balance.department == department]
    summary = filtered_balance.groupby("week_start", as_index=False).agg(
        **{"Available Capacity": ("available_capacity", "sum"),
           "Total Planned": ("total_allocated", "sum")}
    )
    summary["Week"] = pd.to_datetime(summary.week_start)
    summary["Balance"] = summary["Available Capacity"] - summary["Total Planned"]
    summary["Capacity position"] = summary.Balance.apply(
        lambda value: f"Spare: {value:,.1f} h" if value >= 0 else f"Shortage: {abs(value):,.1f} h"
    )

    allocations = manager_allocations(weeks)
    if department != "All" and not allocations.empty:
        allocations = allocations[allocations.department == department]
    projects = get_projects(False)
    metadata = projects[["project_code", "project_name", "priority"]] if not projects.empty else pd.DataFrame()
    stacks = allocations.merge(metadata, on="project_code", how="left") if not allocations.empty else pd.DataFrame()
    if not stacks.empty:
        stacks = stacks.rename(columns={"project_code": "Project Code", "project_name": "Project Name",
                                        "priority": "Priority", "planned_hours": "Allocated Hours", "department": "Department"})
        stacks["Series"] = stacks["Project Code"]
        stacks["Detail"] = stacks["Project Code"] + " · " + stacks["Project Name"].fillna("")
    activities = get_internal_activities()
    activity_rows = []
    if not activities.empty:
        activities = activities[activities.active.astype(bool)]
        if department != "All": activities = activities[activities.department == department]
        for activity in activities.itertuples():
            for week in weeks:
                if activity.start_week <= week.isoformat() <= activity.end_week and float(activity.planned_hours_per_week) > 0:
                    activity_rows.append({"week_start": week.isoformat(), "Department": activity.department,
                        "Project Code": "Internal", "Project Name": activity.activity_name, "Priority": "—",
                        "Allocated Hours": float(activity.planned_hours_per_week), "Series": "Internal Activities",
                        "Detail": f"Internal · {activity.activity_name}"})
    stacks = pd.concat([stacks, pd.DataFrame(activity_rows)], ignore_index=True)
    if stacks.empty:
        st.info("No explicit manager allocation or internal activity in this period.")
        return
    stacks = stacks.merge(summary[["week_start", "Available Capacity", "Total Planned", "Balance", "Capacity position"]], on="week_start", how="left")
    stacks["Week"] = pd.to_datetime(stacks.week_start)
    domain = stacks.Series.drop_duplicates().tolist()
    colours = [INTERNAL_ACTIVITY_COLOUR if item == "Internal Activities" else project_colour(item) for item in domain]
    tooltips = [alt.Tooltip("Week:T", title="Week"), "Available Capacity:Q", "Total Planned:Q",
                alt.Tooltip("Capacity position:N"), "Department:N", "Project Code:N", "Project Name:N",
                "Allocated Hours:Q", "Priority:N"]
    bars = alt.Chart(stacks).mark_bar(cornerRadiusTopLeft=2, cornerRadiusTopRight=2).encode(
        x=alt.X("Week:T", title="Week", timeUnit="yearmonthdate"), y=alt.Y("Allocated Hours:Q", title="Hours"),
        color=alt.Color("Series:N", scale=alt.Scale(domain=domain, range=colours), title="Project / activity"),
        order=alt.Order("Series:N"), tooltip=tooltips,
    )
    line = alt.Chart(summary).mark_line(color=CAPACITY_COLOURS["within"], strokeWidth=3, point=True).encode(
        x=alt.X("Week:T", timeUnit="yearmonthdate"), y=alt.Y("Available Capacity:Q"),
        tooltip=[alt.Tooltip("Week:T", title="Week"), "Available Capacity:Q", "Total Planned:Q", "Capacity position:N"],
    )
    overload = summary[summary.Balance < 0]
    markers = alt.Chart(overload).mark_point(shape="triangle-up", size=90, filled=True, color=CAPACITY_COLOURS["shortage"]).encode(
        x=alt.X("Week:T", timeUnit="yearmonthdate"), y=alt.Y("Total Planned:Q"),
        tooltip=[alt.Tooltip("Week:T", title="Overloaded week"), "Capacity position:N"])
    st.altair_chart(alt.layer(bars, line, markers).resolve_scale(y="shared").properties(height=390), use_container_width=True)
    st.caption("Stack = explicit manager allocation plus Internal Activities. Dark line = Available Capacity. Red triangle = shortage.")


def record_access_event(identity: str, action: str) -> None:
    with connect() as conn:
        write_audit(conn, identity, "Authentication", None, action, None, None, "Streamlit session")


def require_authentication() -> None:
    if st.session_state.get("authenticated"):
        return
    st.title("Production Planner")
    try:
        users = load_users()
    except AuthenticationConfigurationError as exc:
        st.error(f"Authentication configuration error: {exc}")
        st.stop()
    with st.form("login_form"):
        email = st.text_input("Email")
        password = st.text_input("Password", type="password")
        submitted = st.form_submit_button("Sign in", type="primary")
    if submitted:
        authenticated_user = authenticate(email, password, users)
        if authenticated_user is None:
            st.error("Invalid email or password.")
        else:
            try:
                initialize_database(seed=False)
                ensure_mvp_schema()
                record_access_event(authenticated_user.audit_identity, "Login")
            except Exception as exc:
                st.error(f"Database startup error: {exc}")
                st.stop()
            st.session_state.update(
                authenticated=True,
                user_email=authenticated_user.email,
                display_name=authenticated_user.name,
                role=authenticated_user.role,
            )
            st.rerun()
    st.stop()


require_authentication()
try:
    initialize_database(seed=False)
    ensure_mvp_schema()
except Exception as exc:
    st.error(f"Database startup error: {exc}")
    st.stop()

user = f"{st.session_state.display_name} <{st.session_state.user_email}>"
title_col, logout_col = st.columns([8, 1])
title_col.title("Production Planner")
title_col.caption(f"Signed in as {st.session_state.display_name} · {st.session_state.user_email}")
if logout_col.button("Logout"):
    record_access_event(user, "Logout")
    for key in ("authenticated", "user_email", "display_name", "role"):
        st.session_state.pop(key, None)
    st.rerun()


def monday(value: date) -> date:
    return value - timedelta(days=value.weekday())


def refresh() -> None:
    st.rerun()


def render_gantt_chart(gantt: pd.DataFrame, planning_start: date, planning_end: date) -> None:
    """Render the Gantt from its existing rows without changing allocation dates."""
    chart_data = gantt.copy()
    chart_data["Start"] = pd.to_datetime(chart_data["Start"], errors="coerce").dt.tz_localize(None)
    chart_data["End"] = pd.to_datetime(chart_data["End"], errors="coerce").dt.tz_localize(None)
    chart_data["Deadline"] = pd.to_datetime(chart_data["Required by"], errors="coerce").dt.tz_localize(None)

    invalid_periods = int((chart_data["End"] <= chart_data["Start"]).fillna(False).sum())
    diagnostics = {
        "Start dtype": str(chart_data["Start"].dtype),
        "End dtype": str(chart_data["End"].dtype),
        "Minimum Start": chart_data["Start"].min(),
        "Maximum End": chart_data["End"].max(),
        "Rows": len(chart_data),
        "Start nulls": int(chart_data["Start"].isna().sum()),
        "End nulls": int(chart_data["End"].isna().sum()),
        "Rows where End <= Start": invalid_periods,
    }
    with st.expander("Gantt rendering diagnostics"):
        st.dataframe(pd.DataFrame([diagnostics]), hide_index=True, use_container_width=True)

    # Start with the proven primitive: one temporal interval and one categorical row.
    # The later encodings retain these same x/x2 channels and Vega-derived x scale.
    chart_data["Row"] = chart_data["Project"] + "  ·  " + chart_data["Discipline"]
    base_bars = alt.Chart(chart_data).mark_bar().encode(
        x=alt.X("Start:T", title="Calendar date"),
        x2="End:T",
        y=alt.Y("Row:N", title="Project / department"),
    )

    # Clip only the displayed values. End is inclusive in the planning data, so the
    # exclusive visual end is one day later and gives a one-day/one-week period width.
    period_start = pd.Timestamp(planning_start)
    period_end = pd.Timestamp(planning_end)
    chart_data["display_start"] = chart_data["Start"].clip(lower=period_start)
    clipped_inclusive_end = chart_data["End"].clip(upper=period_end)
    chart_data["display_end"] = clipped_inclusive_end + pd.Timedelta(days=1)
    chart_data = chart_data[~(clipped_inclusive_end < chart_data["display_start"])].copy()
    order = chart_data["Row"].drop_duplicates().tolist()
    tips = ["Project", "project_code", "Discipline", "Start:T", "End:T", "Plan source",
            "Remaining hours:Q", "Allocated hours:Q", "Required by:T", "Health status",
            "Shortfall / surplus:Q"]

    # Add colour, source opacity, clipping and tooltips incrementally to the base
    # interval encoding. No layer supplies its own temporal domain.
    bars = alt.Chart(chart_data).mark_bar(cornerRadius=2).encode(
        x=alt.X("display_start:T", title="Calendar date"),
        x2="display_end:T",
        y=alt.Y("Row:N", sort=order, title="Project / department"),
        color=alt.Color("Discipline:N", scale=alt.Scale(
            domain=DISCIPLINES, range=["#72a5d3", "#76b77b", "#c9ad6a"]), title="Discipline"),
        opacity=alt.Opacity("Plan source:N", scale=alt.Scale(
            domain=["Manager allocation", "Forecast baseline"], range=[1, .28]), title="Plan source"),
        tooltip=tips,
    )
    deadlines = alt.Chart(chart_data).mark_tick(color="#b23a48", thickness=2, size=18).encode(
        x=alt.X("Deadline:T", title="Calendar date"),
        y=alt.Y("Row:N", sort=order, title="Project / department"),
        tooltip=["Project", "Required by:T", "Late"],
    )
    chart = alt.layer(bars, deadlines).resolve_scale(x="shared", y="shared").properties(
        height=max(240, len(chart_data) * 28)
    )
    st.altair_chart(chart, use_container_width=True)
    st.caption("Solid = Manager allocation; translucent = Forecast baseline. Red ticks = Required By. Department colour is not health.")
    st.dataframe(style_planning_table(gantt), hide_index=True, use_container_width=True)


planning_start = st.sidebar.date_input("Planning start", monday(date.today()))
planning_end = st.sidebar.date_input("Planning end", monday(date.today()) + timedelta(weeks=12))
weeks = week_starts(planning_start, planning_end) if planning_end >= planning_start else []


@st.dialog("Allocate capacity", width="large")
def allocation_dialog(default_department: str = "RS") -> None:
    projects = get_projects(False)
    if projects.empty or not weeks:
        st.info("Create active project demand and select a valid planning period first.")
        return
    labels = {f"{r.project_code} · {r.project_name}": r.project_code for r in projects.itertuples()}
    project_label = st.selectbox("Project", labels)
    project_code = labels[project_label]
    department = st.selectbox("Department", DISCIPLINES, index=DISCIPLINES.index(default_department))
    mode = st.segmented_control("Allocation method", ["People", "Hours/week", "Spread remaining"], default="People")
    # Allocation dates are deliberately independent of the visible planning end.
    replace_clear_boundary = monday(planning_start)
    allocation_week_options = [replace_clear_boundary + timedelta(weeks=i)
                               for i in range(ALLOCATION_FUTURE_HORIZON_WEEKS + 1)]
    start_week = st.selectbox("Allocation start week", allocation_week_options,
                              format_func=lambda d: d.strftime("%d %b %Y"),
                              help=f"May be outside the visible Planning range (up to {ALLOCATION_FUTURE_HORIZON_WEEKS} weeks ahead).")
    selected_weeks: list[date]
    people = hours_person = hours_week = 0.0
    if mode == "Spread remaining":
        end_options = [w for w in allocation_week_options if w >= start_week]
        default_end = min(3, len(end_options) - 1)
        end_week = st.selectbox("Allocation end week", end_options, index=default_end,
                                format_func=lambda d: d.strftime("%d %b %Y"))
        selected_weeks = week_starts(start_week, end_week)
    else:
        max_weeks = max(len([w for w in allocation_week_options if w >= start_week]), 1)
        count = st.number_input("Number of weeks", 1, max_weeks, min(4, max_weeks))
        selected_weeks = [start_week + timedelta(weeks=i) for i in range(int(count))]
        if mode == "People":
            c1, c2 = st.columns(2)
            people = c1.number_input("Number of people", min_value=0.25, value=3.0, step=0.25)
            hours_person = c2.number_input("Hours per person per week", min_value=0.0, value=37.5, step=0.5)
        else:
            hours_week = st.number_input("Hours per week", min_value=0.0, value=120.0, step=1.0)
    remaining = project_remaining_hours(project_code, department)
    values = quick_allocation_values(mode, selected_weeks, people=people, hours_per_person=hours_person,
                                     hours_per_week=hours_week, remaining_hours=remaining)
    operation_label = st.radio("Write mode", ["Add to existing allocation", "Replace full future allocation"], horizontal=True,
                               help="Replace clears only this project's department plan from Planning start onwards, including weeks beyond Planning end.")
    operation = "add" if operation_label.startswith("Add") else "replace_future"
    cap = weekly_department_capacity(selected_weeks)
    capacity_map = cap[cap.department == department].set_index("week_start").available_capacity.to_dict() if not cap.empty else {}
    existing = rows("SELECT week_start,planned_hours FROM manager_weekly_plan WHERE department=? AND week_start BETWEEN ? AND ?",
                    (department, selected_weeks[0].isoformat(), selected_weeks[-1].isoformat()))
    all_existing = {w.isoformat(): 0.0 for w in selected_weeks}
    for row in existing: all_existing[row["week_start"]] += float(row["planned_hours"])
    project_existing = {(r["week_start"]): float(r["planned_hours"]) for r in rows(
        "SELECT week_start,planned_hours FROM manager_weekly_plan WHERE project_code=? AND department=?", (project_code, department))}
    future_existing = sum(value for week, value in project_existing.items() if week >= monday(planning_start).isoformat())
    removed_total = future_existing if operation == "replace_future" else 0.0
    preview = []
    for week, added in zip(selected_weeks, values):
        old_project = project_existing.get(week.isoformat(), 0.0)
        delta = added if operation == "add" else added - old_project
        new_total = all_existing[week.isoformat()] + delta
        preview.append({"Week": week.strftime("%d %b %Y"), "Available Capacity": capacity_map.get(week.isoformat(), 0),
                        "Existing Total Allocation": all_existing[week.isoformat()], "Allocation Being Added": added,
                        "Allocation Being Removed": old_project if operation == "replace_future" else 0,
                        "Net Allocation Change": delta, "New Total Allocation": new_total,
                        "Balance": capacity_map.get(week.isoformat(), 0) - new_total})
    added_total = sum(values)
    net_change = added_total if operation == "add" else added_total - removed_total
    new_future_total = future_existing + added_total if operation == "add" else added_total
    metric_values = [
        ("Current remaining demand", remaining), ("Existing future project allocation", future_existing),
        ("Allocation being added", added_total), ("Allocation being removed", removed_total),
        ("Net allocation change", net_change), ("Remaining after", max(remaining-new_future_total, 0)),
    ]
    for column, (label, value) in zip(st.columns(6), metric_values): column.metric(label, f"{value:,.2f} h")
    if mode == "Spread remaining":
        st.caption(f"{len(values)} weeks · required average {values[0] if values else 0:,.2f} h/week · approx. {(values[0] / 37.5 if values else 0):,.2f} FTE at 37.5 h")
    st.dataframe(pd.DataFrame(preview), hide_index=True, use_container_width=True)
    if any(r["Balance"] < 0 for r in preview):
        st.warning("This allocation creates a capacity shortage. It may still be applied and escalated.")
    over = new_future_total > remaining + 0.005
    override = st.checkbox("I explicitly approve planning more than Remaining Hours", disabled=not over)
    if over: st.error("The request exceeds remaining project demand. Explicit approval is required.")
    if st.button("Apply allocation", type="primary", disabled=not user or (over and not override)):
        try:
            apply_quick_allocation(project_code, department, selected_weeks, values, user, operation, override, planning_start)
            allocation_end = selected_weeks[-1]
            outside_view = selected_weeks[0] < monday(planning_start) or allocation_end > monday(planning_end)
            st.session_state["allocation_save_message"] = (
                f"Allocation saved through {allocation_end:%d %b %Y}. "
                + ("Some allocated weeks are outside the current Planning range. Extend the Planning view to see all allocated weeks."
                   if outside_view else "All allocated weeks are inside the current Planning range.")
            )
            refresh()
        except ValueError as exc: st.error(str(exc))
    st.divider()
    if st.button("Clear current allocation", type="secondary"):
        st.session_state[f"confirm_clear_{project_code}_{department}"] = True
    if st.session_state.get(f"confirm_clear_{project_code}_{department}"):
        st.warning(f"Clear all future {department} allocation for {project_code} from {monday(planning_start):%d %b %Y} onwards?")
        cancel, confirm = st.columns(2)
        if cancel.button("Cancel", use_container_width=True):
            st.session_state.pop(f"confirm_clear_{project_code}_{department}", None); refresh()
        if confirm.button("Clear allocation", type="primary", use_container_width=True):
            clear_future_allocation(project_code, department, planning_start, user)
            st.session_state.pop(f"confirm_clear_{project_code}_{department}", None); refresh()


def project_view() -> None:
    st.header("Projects")
    st.caption("Project demand remains the contractual and forecast register; allocation is managed separately.")
    existing = get_projects(True)
    st.subheader("Demand register")
    register=existing[existing.archived == 0] if not existing.empty else existing
    st.dataframe(register[["priority","project_code","project_name","client","project_manager","spus","row_km","cct_km","rs_hours","gis_hours","pls_hours","end_date","status"]] if not register.empty else register,hide_index=True,use_container_width=True)
    with st.expander("+ Add or edit project", expanded=False):
      choices = ["Create new project"] + ([f"{r.project_code} · {r.project_name}" for r in existing.itertuples()] if not existing.empty else [])
      selected = st.selectbox("Create or edit project", choices)
      current = {} if selected == choices[0] else existing[existing.project_code == selected.split(" · ", 1)[0]].iloc[0].to_dict()
      with st.form("project_form"):
        st.subheader("Project and contractual facts")
        a,b,c = st.columns(3); original = str(current.get("project_code") or "")
        code=a.text_input("Project code *", original); name=b.text_input("Project name *", str(current.get("project_name") or "")); client=c.text_input("Client *", str(current.get("client") or ""))
        pm=a.text_input("Project manager *", str(current.get("project_manager") or "")); priority=b.selectbox("Priority *", ["P1","P2","P3"], index=["P1","P2","P3"].index(current.get("priority") or "P3")); penalty=c.selectbox("Late-delivery penalty", ["None","Potential","Active"], index=["None","Potential","Active"].index(current.get("penalty_exposure") or "None"))
        start=a.date_input("Production start *", pd.to_datetime(current.get("start_date"), errors="coerce").date() if current.get("start_date") else None); end=b.date_input("Required completion *", pd.to_datetime(current.get("end_date"), errors="coerce").date() if current.get("end_date") else None); status=c.selectbox("Status", ["draft","active","on_hold","completed","archived"], index=["draft","active","on_hold","completed","archived"].index(current.get("status") or "draft"))
        st.subheader("Scope quantities"); q1,q2,q3=st.columns(3); row_km=q1.number_input("ROW length (km)",0.0,value=float(current.get("row_km") or 0)); cct_km=q2.number_input("Circuit length (km)",0.0,value=float(current.get("cct_km") or 0)); spus=q3.number_input("SPUs",0.0,value=float(current.get("spus") or 0))
        vals={}; st.subheader("Discipline forecast, actuals and data availability")
        for disc in DISCIPLINES:
            x,y,z=st.columns(3); key=disc.lower(); hrs=x.number_input(f"{disc} forecast hours",0.0,value=float(current.get(f"{key}_hours") or 0),key=f"p_{key}_h"); available=y.date_input(f"{disc} data available",pd.to_datetime(current.get(f"{key}_start_date"),errors="coerce").date() if current.get(f"{key}_start_date") else None,key=f"p_{key}_d"); actual=float(current.get(f"actual_{key}_hours") or 0); z.metric(f"{disc} remaining",f"{max(hrs-actual,0):,.1f} h"); vals[key]=(hrs,available,actual)
        assumptions=st.text_area("Assumptions / evidence",str(current.get("assumptions") or "")); submitted=st.form_submit_button("Save project demand",type="primary")
      if submitted:
        record={"project_code":code,"_original_project_code":original or code,"project_name":name,"client":client,"project_manager":pm,"priority":priority,"penalty_exposure":penalty,"row_km":row_km,"cct_km":cct_km,"spus":spus,"start_date":start,"end_date":end,"loading_type":"even","status":status,"assumptions":assumptions}
        for key,(hrs,available,actual) in vals.items(): record.update({f"{key}_hours":hrs,f"{key}_start_date":available,f"actual_{key}_hours":actual})
        errors=validate_project_demand(record)
        if errors:
            for error in errors: st.error(error)
        else: save_projects([record], user); st.success("Project demand saved."); refresh()


def planning_view() -> None:
    st.header("Planning")
    if not weeks: st.error("Planning end must not be before planning start."); return
    department=st.segmented_control("Department",["All",*DISCIPLINES],default="All")
    if st.session_state.get("allocation_save_message"):
        st.success(st.session_state.pop("allocation_save_message"))
    overview,gantt_tab,sequence_tab,weekly,issues_tab=st.tabs(["Overview","Gantt","Sequence","Weekly allocation","Issues"])
    bal=capacity_balance(weeks); selected=bal if department=="All" else bal[bal.department==department]
    health=project_health_plans(planning_start,department)
    health=project_capacity_statuses(health, selected, planning_start, planning_end)
    with overview:
        remaining=float(health["Remaining Hours"].sum()) if not health.empty else 0
        unplanned=float(health["Unplanned Hours"].sum()) if not health.empty else 0
        shortage=selected[selected.over_under_capacity<0]
        open_issues=len(get_issues("Open",department))
        metrics=st.columns(6)
        metrics[0].metric("Remaining demand",f"{remaining:,.1f} h"); metrics[1].metric("Unplanned demand",f"{unplanned:,.1f} h")
        metrics[2].metric("Unallocated capacity in period",f"{max(float(selected.over_under_capacity.sum()),0):,.1f} h")
        metrics[3].metric("Weeks over capacity",str(len(shortage))); metrics[4].metric("Peak weekly shortage",f"{abs(float(shortage.over_under_capacity.min())) if not shortage.empty else 0:,.1f} h")
        metrics[5].metric("Open issues",str(open_issues))
        if not shortage.empty:
            st.caption("Earliest capacity gap: "+str(shortage.sort_values("week_start").iloc[0].week_start))
            well = health[(health["Health"] == "Well-resourced") &
                          (health["Capacity Status"] == "Over capacity")] if not health.empty else pd.DataFrame()
            for disc, gaps in shortage.groupby("department"):
                affected_well = well[well["Department"] == disc]["Project Code"].nunique() if not well.empty else 0
                peak = abs(float(gaps.over_under_capacity.min()))
                st.error(f"{affected_well} project(s) are individually well-resourced, but {disc} exceeds available capacity "
                         f"in {gaps.week_start.nunique()} week(s). Peak shortage: {peak:,.1f} h.")
        st.subheader("Project-discipline plan health")
        counts=health.Health.value_counts() if not health.empty else pd.Series(dtype=int)
        cols=st.columns(4)
        for col,label in zip(cols,["Unplanned","Under-resourced","Well-resourced","Over-resourced"]): col.metric(label,int(counts.get(label,0)))
        render_capacity_chart(bal, department)
        st.caption("Available capacity = contracted roster − approved absence − temporary unavailability, with temporary assignments moved between departments. Balance remains available capacity − project allocations − internal activities.")
        if department == "All" and not shortage.empty:
            affected = shortage.groupby("department").size().to_dict()
            st.warning("The combined view can mask a department shortage with spare capacity elsewhere. " +
                       "; ".join(f"{disc}: {affected.get(disc, 0)} shortage week(s)" for disc in DISCIPLINES))
        if not health.empty: st.dataframe(style_planning_table(health),hide_index=True,use_container_width=True)
    with gantt_tab:
        gantt=allocation_timeline(weeks,department)
        if gantt.empty: st.info("No allocation or forecast baseline in this range.")
        else:
            render_gantt_chart(gantt, planning_start, planning_end)
    with sequence_tab:
        st.subheader("RS → GIS → PLS sequence analysis")
        st.caption("Advisory only: findings use explicit manager allocations and never move work or change project health.")
        findings=sequence_analysis(weeks)
        if findings.empty: st.info("No material sequence review items were found in the selected planning period.")
        else:
            counts=findings.Category.value_counts(); cols=st.columns(4)
            for col,label in zip(cols,["Gap","Possible overlap","Downstream starvation","Pull-forward opportunity"]): col.metric(label,int(counts.get(label,0)))
            st.dataframe(style_planning_table(findings),hide_index=True,use_container_width=True)
    with weekly:
        def show_department(d: str, editable: bool=False):
            st.subheader(d)
            plan=manager_plan(weeks,d); week_cols=[w.isoformat() for w in weeks]
            if plan.empty: st.info(f"No active {d} demand."); return
            h=project_capacity_statuses(project_health_plans(planning_start,d), bal[bal.department==d],
                                        planning_start, planning_end)[["Project Code","Health","Capacity Status"]]
            plan=h.merge(plan,on="Project Code",how="right"); display=plan[["Health","Capacity Status","Project Code","Project","Remaining Hours",*week_cols]]
            if editable:
                if st.button("+ Allocate capacity",type="primary",key=f"allocate_{d}"): allocation_dialog(d)
                disabled=[c for c in plan.columns if c not in week_cols]
                edited=st.data_editor(plan,hide_index=True,use_container_width=True,disabled=disabled,column_config={w:st.column_config.NumberColumn(w,min_value=0.) for w in week_cols},key=f"plan_{d}")
                if st.button("Save manager plan",disabled=not user,key=f"save_{d}"):
                    try: save_manager_plan(edited,weeks,d,user); refresh()
                    except ValueError as exc: st.error(str(exc))
            else: st.dataframe(style_planning_table(display),hide_index=True,use_container_width=True)
            d_bal=bal[bal.department==d].set_index("week_start")
            totals=pd.DataFrame([{ "Summary":f"{d} Allocated",**{w:float(d_bal.loc[w,"allocated_demand"]) for w in week_cols}}, {"Summary":f"{d} Capacity",**{w:float(d_bal.loc[w,"available_capacity"]) for w in week_cols}}, {"Summary":f"{d} Balance",**{w:float(d_bal.loc[w,"over_under_capacity"]) for w in week_cols}}])
            def balance_style(row):
                is_balance = str(row.get("Summary", "")).endswith("Balance")
                return ["background-color:#FDE8E7;color:#A52622;font-weight:700" if is_balance and isinstance(value,(int,float)) and value < 0 else "" for value in row]
            st.dataframe(totals.style.apply(balance_style,axis=1).format(precision=1),hide_index=True,use_container_width=True)
            activities=get_internal_activities(); activities=activities[(activities.department==d)&(activities.active.astype(bool))] if not activities.empty else activities
            if not activities.empty:
                internal=[]
                for a in activities.itertuples(): internal.append({"Internal activity":a.activity_name,**{w:(a.planned_hours_per_week if a.start_week<=w<=a.end_week else 0) for w in week_cols}})
                st.caption("INTERNAL — included in Balance, but stored separately from projects"); st.dataframe(pd.DataFrame(internal),hide_index=True,use_container_width=True)
        if department=="All":
            st.caption("Portfolio scan view. Select a department above for precise editing and Quick Allocation.")
            for d in DISCIPLINES: show_department(d)
        else: show_department(department,True)
    with issues_tab:
        st.subheader("Issues register")
        all_issues=get_issues("All",department); f1,f2,f3=st.columns(3)
        status=f1.selectbox("Status",["Open","Closed","All"]); owners=["All"]+(sorted(all_issues.owner.dropna().unique()) if not all_issues.empty else []); owner=f2.selectbox("Owner",owners); types=["All"]+(sorted(all_issues.issue_type.dropna().unique()) if not all_issues.empty else []); kind=f3.selectbox("Issue type",types)
        issue_data=get_issues(status,department,owner=owner,issue_type=kind)
        st.dataframe(issue_data,hide_index=True,use_container_width=True)
        with st.expander("+ Create issue"):
            projects=get_projects(False); codes=projects.project_code.tolist() if not projects.empty else []
            with st.form("create_issue"):
                c1,c2=st.columns(2); code=c1.selectbox("Project",codes); dept=c2.selectbox("Department",DISCIPLINES,index=DISCIPLINES.index(department) if department in DISCIPLINES else 0); kind=st.selectbox("Issue type",["Capacity shortage","Data delay","Priority conflict","Skills gap"]); impact=st.number_input("Impact hours",0.); decision=st.text_area("Decision required"); owner_new=st.text_input("Owner"); due=st.date_input("Required by",date.today()); submit=st.form_submit_button("Create issue",disabled=not user)
            if submit:
                try: create_escalation(code,dept,kind,impact,decision,owner_new,due,user); refresh()
                except ValueError as exc: st.error(str(exc))
        if not issue_data.empty:
            with st.expander("Update / close / reopen issue"):
                issue_id=st.selectbox("Issue",issue_data.id.tolist(),format_func=lambda i:f"#{i} · "+str(issue_data.loc[issue_data.id==i,"decision_required"].iloc[0])[:70]); current=issue_data[issue_data.id==issue_id].iloc[0]
                new_owner=st.text_input("Owner",str(current.owner)); new_due=st.date_input("Required by",pd.to_datetime(current.required_by).date()); resolution=st.text_area("Resolution / outcome",str(current.resolution or "")); new_status=st.radio("Status",["Open","Closed"],index=0 if current.status=="Open" else 1,horizontal=True)
                if st.button("Save issue",disabled=not user):
                    try: update_issue(int(issue_id),user,owner=new_owner,required_by=new_due,resolution=resolution,status=new_status); refresh()
                    except ValueError as exc: st.error(str(exc))

def resource_management_view() -> None:
    st.header("Resource Management")
    st.caption("Operational availability changes preserve each employee's home department and contracted hours.")
    availability_tab,resources_tab,holidays_tab,adjustments_tab=st.tabs(["Availability","Resources / roster","Absence & Holidays","Temporary assignments"])
    with availability_tab:
        st.subheader("Workforce availability")
        a,b,c,d=st.columns([1,2,1,1])
        availability_department=a.selectbox("Department",["All",*DISCIPLINES],key="availability_department")
        employee_search=b.text_input("Employee search",key="availability_employee_search")
        availability_start=monday(c.date_input("Planning start",planning_start,key="availability_start"))
        default_end=min(planning_end,availability_start+timedelta(weeks=11))
        availability_end=d.date_input("Planning end",default_end,key="availability_end")
        availability_weeks=week_starts(availability_start,availability_end) if availability_end>=availability_start else []
        detail=resource_availability_matrix(availability_weeks)
        if detail.empty: st.info("No resources match this availability period.")
        else:
            if employee_search: detail=detail[detail.Employee.str.contains(employee_search,case=False,na=False)]
            if availability_department!="All":
                detail=detail[detail.apply(lambda row: row["Home Department"]==availability_department or float(row["Department Contributions"].get(availability_department,0))>0,axis=1)]
            identities=detail[["resource_id","Employee","Home Department","Weekly Hours"]].drop_duplicates()
            current=detail[detail.week_start==availability_weeks[0].isoformat()] if availability_weeks else detail.iloc[0:0]
            current_map={r.resource_id:availability_label(r["Home Department"],r["Department Contributions"]) for _,r in current.iterrows()}
            matrix=identities.copy(); matrix["Current Assignment"]=matrix.resource_id.map(current_map).fillna("0")
            style_matrix=pd.DataFrame("",index=matrix.index,columns=matrix.columns)
            for week in availability_weeks:
                week_rows=detail[detail.week_start==week.isoformat()].set_index("resource_id")
                labels={rid:availability_label(row["Home Department"],row["Department Contributions"]) for rid,row in week_rows.iterrows()}
                column=week.strftime("%d %b"); matrix[column]=matrix.resource_id.map(labels).fillna("0 · Unavailable")
                style_matrix[column]=matrix.apply(lambda row: availability_style(
                    row["Home Department"],row["Weekly Hours"],
                    week_rows.loc[row.resource_id,"Department Contributions"] if row.resource_id in week_rows.index else {}),axis=1)
            if availability_weeks:
                first_column=availability_weeks[0].strftime("%d %b")
                style_matrix["Current Assignment"]=style_matrix[first_column]
            shown=matrix.drop(columns="resource_id"); shown_styles=style_matrix[shown.columns]
            styled=shown.style.apply(lambda _frame: shown_styles,axis=None)
            st.dataframe(styled,hide_index=True,use_container_width=True)
            render_visual_legend()
            st.caption("Cells show usable hours by contributing department. An arrow marks capacity moved from the home department; split hours are shown once and are never duplicated.")
            with st.expander("Availability reductions and explanations"):
                explanation=detail[(detail["Holiday Hours"]>0)|(detail["Unavailable Hours"]>0)|(detail["Other Reduction Hours"]>0)|(detail.Reasons!="")]
                st.dataframe(explanation[["Employee","week_start","Holiday Hours","Unavailable Hours","Other Reduction Hours","Reasons"]],hide_index=True,use_container_width=True)
    with resources_tab:
        resources=get_resources()
        if not resources.empty:
            current_detail=resource_availability_matrix([monday(date.today())])
            current_hours=current_detail.set_index("resource_id")["Availability"].to_dict() if not current_detail.empty else {}
            roster=resources[["id","person_name","department","weekly_hours","active_status"]].copy()
            roster["Current Availability"]=roster.id.map(current_hours).fillna("0")
            roster=roster.rename(columns={"person_name":"Employee","department":"Home Department","weekly_hours":"Weekly Hours","active_status":"Active Status"})
            st.dataframe(roster.drop(columns="id"),hide_index=True,use_container_width=True)
        with st.expander("Edit detailed roster",expanded=False):
            editor=prepare_date_columns_for_editor(resources,RESOURCE_DATE_COLUMNS) if not resources.empty else pd.DataFrame(columns=["person_name","department","weekly_hours","active_status"])
            edited=st.data_editor(editor,num_rows="dynamic",hide_index=True,use_container_width=True,key="operational_resources")
            if st.button("Save resources",disabled=not user,key="save_operational_resources"): save_resources(edited.to_dict("records"),user); refresh()
    with holidays_tab:
        upload=st.file_uploader("Approved holiday snapshot",type=["csv","xlsx","xls"],key="operational_holidays")
        if upload:
            preview=preview_holiday_snapshot(upload); st.write({"New":len(preview["new"]),"Changed":len(preview["changed"]),"Removed/cancelled":len(preview["removed"]),"Unmatched":len(preview["unmatched"])})
            if preview["unmatched"]: st.warning("Unmatched employees: "+", ".join(preview["unmatched"]))
            if st.button("Apply holiday snapshot",type="primary"): apply_holiday_snapshot(preview,upload.name,user); refresh()
        st.dataframe(get_holidays(),hide_index=True,use_container_width=True)
    with adjustments_tab:
        resources=get_resources(); adjustments=get_capacity_adjustments()
        if not adjustments.empty:
            display=adjustments[["id","person_name","home_department","weekly_hours","adjustment_type","destination_department","start_date","end_date","capacity_percent","hours_per_week","reason","active","period_status"]]
            st.dataframe(display,hide_index=True,use_container_width=True)
        else: st.info("No temporary capacity adjustments recorded.")
        with st.expander("+ Add or edit temporary adjustment",expanded=False):
            ids=[None]+(adjustments.id.astype(int).tolist() if not adjustments.empty else [])
            edit_id=st.selectbox("Record",ids,format_func=lambda value:"Create new adjustment" if value is None else f"#{value} · "+adjustments.loc[adjustments.id==value,"person_name"].iloc[0])
            current={} if edit_id is None else adjustments[adjustments.id==edit_id].iloc[0].to_dict()
            resource_options={f"{r.person_name} · {r.department} · {r.weekly_hours:g} h":int(r.id) for r in resources.itertuples()}
            selected_label=next((label for label,rid in resource_options.items() if rid==current.get("resource_id")),next(iter(resource_options),None))
            with st.form("capacity_adjustment_form"):
                resource_label=st.selectbox("Employee / home department",list(resource_options),index=list(resource_options).index(selected_label) if selected_label else 0)
                c1,c2=st.columns(2); kind=c1.selectbox("Adjustment type",TEMPORARY_ADJUSTMENT_TYPES,index=TEMPORARY_ADJUSTMENT_TYPES.index(current.get("adjustment_type")) if current.get("adjustment_type") in TEMPORARY_ADJUSTMENT_TYPES else 0)
                destination_options=["No destination",*DISCIPLINES]; destination_value=current.get("destination_department") or "No destination"; destination=c2.selectbox("Destination department (assignments only)",destination_options,index=destination_options.index(destination_value))
                start=c1.date_input("Start date",_date_value(current.get("start_date"),date.today())); end=c2.date_input("End date",_date_value(current.get("end_date"),date.today()))
                mode=st.radio("Capacity measure",["Percentage","Hours per week"],index=1 if current.get("hours_per_week") is not None and not pd.isna(current.get("hours_per_week")) else 0,horizontal=True)
                amount=st.number_input("Capacity percentage" if mode=="Percentage" else "Hours per week",min_value=0.01,max_value=100.0 if mode=="Percentage" else None,value=float(current.get("capacity_percent") if mode=="Percentage" and pd.notna(current.get("capacity_percent")) else current.get("hours_per_week") if mode!="Percentage" and pd.notna(current.get("hours_per_week")) else 100 if mode=="Percentage" else 37.5))
                reason=st.text_area("Reason / notes",str(current.get("reason") or "")); active=st.checkbox("Active",value=bool(current.get("active",True))); submit=st.form_submit_button("Save temporary adjustment",type="primary")
            if submit:
                try:
                    save_capacity_adjustment({"id":edit_id,"resource_id":resource_options[resource_label],"adjustment_type":kind,"destination_department":None if destination=="No destination" else destination,"start_date":start,"end_date":end,"capacity_percent":amount if mode=="Percentage" else None,"hours_per_week":amount if mode!="Percentage" else None,"reason":reason,"active":active},user); refresh()
                except ValueError as exc: st.error(str(exc))


def _date_value(value, default):
    parsed=pd.to_datetime(value,errors="coerce")
    return default if pd.isna(parsed) else parsed.date()


def administration_view() -> None:
    st.header("Administration")
    resources_tab,holidays_tab,internal_tab,imports_tab,audit_tab=st.tabs(["Resources","Absence & Holidays","Internal Activities","Imports","Audit log"])
    with resources_tab:
        upload=st.file_uploader("Roster CSV / Excel",type=["csv","xlsx","xls"],key="roster")
        if upload:
            preview=load_roster_csv(upload); st.dataframe(preview,hide_index=True)
            if st.button("Import roster",disabled=not user): upload.seek(0); import_sample_roster(upload, user); refresh()
        resources=get_resources(); editor=prepare_date_columns_for_editor(resources,RESOURCE_DATE_COLUMNS) if not resources.empty else pd.DataFrame(columns=["person_name","department","weekly_hours","active_status"])
        edited=st.data_editor(editor,num_rows="dynamic",hide_index=True,use_container_width=True)
        if st.button("Save resources",disabled=not user): save_resources(edited.to_dict("records"),user); refresh()
    with holidays_tab:
        last=rows("SELECT imported_at,record_count,unmatched_count FROM holiday_imports ORDER BY id DESC LIMIT 1")
        if last:
            age=(date.today()-pd.to_datetime(last[0]["imported_at"]).date()).days; st.metric("Last holiday import",f"{age} days ago",f"{last[0]['record_count']} records · {last[0]['unmatched_count']} unmatched")
            if age>31: st.warning("Holiday data is older than 31 days; consider importing a fresh approved snapshot.")
        else: st.info("No approved-holiday snapshot has been imported.")
        upload=st.file_uploader("Approved holiday snapshot",type=["csv","xlsx","xls"],key="holidays")
        if upload:
            preview=preview_holiday_snapshot(upload); st.write({"New":len(preview["new"]),"Changed":len(preview["changed"]),"Removed/cancelled":len(preview["removed"]),"Unmatched":len(preview["unmatched"])})
            if preview["unmatched"]: st.warning("Unmatched employees: "+", ".join(preview["unmatched"]))
            st.dataframe(pd.DataFrame(preview["new"]+preview["changed"]),hide_index=True)
            if st.button("Apply holiday snapshot",type="primary",disabled=not user): apply_holiday_snapshot(preview,upload.name,user); refresh()
        h=get_holidays(); c1,c2,c3=st.columns(3); dept=c1.selectbox("Department",["All",*DISCIPLINES],key="hdept"); names=["All"]+(sorted(h.person_name.dropna().unique()) if not h.empty else []); person=c2.selectbox("Employee",names); period=c3.selectbox("Period",["Future","Past","All"])
        if not h.empty:
            filtered=h if dept=="All" else h[h.department==dept]
            if person!="All": filtered=filtered[filtered.person_name==person]
            dates=pd.to_datetime(filtered.holiday_date); today=pd.Timestamp(date.today()); filtered=filtered[(dates>=today) if period=="Future" else (dates<today) if period=="Past" else pd.Series(True,index=filtered.index)]
            st.dataframe(filtered.rename(columns={"person_name":"Employee","department":"Department","holiday_date":"Date","hours":"Hours","source":"Source","status":"Status"}),hide_index=True,use_container_width=True)
            active=filtered[filtered.status=="active"].copy(); active["Date"]=pd.to_datetime(active.holiday_date); active["Duration"]=active.hours
            if not active.empty: st.subheader("Absence timeline"); st.bar_chart(active,x="person_name",y="Duration",horizontal=True)
    with internal_tab:
        activities=get_internal_activities(); base=activities if not activities.empty else pd.DataFrame(columns=["id","activity_name","department","start_week","end_week","planned_hours_per_week","active","notes"])
        edited=st.data_editor(base,num_rows="dynamic",hide_index=True,use_container_width=True)
        if st.button("Save internal activities",disabled=not user): save_internal_activities(edited.to_dict("records"),user); refresh()
    with imports_tab:
        st.subheader("Planner setup")
        st.caption("Move projects, resources, manager weekly allocations, internal activities and non-sensitive Employee ID mappings. Holidays, audit and authentication are not included.")
        today_name=date.today().isoformat()
        st.download_button("Download planner setup",export_planner_setup(),f"production_planner_setup_{today_name}.zip","application/zip")
        setup_upload=st.file_uploader("Upload planner setup",type=["zip"],key="planner_setup")
        if setup_upload:
            setup_preview=preview_planner_setup(setup_upload)
            if not setup_preview.get("valid"):
                for error in setup_preview.get("errors",[]): st.error(error)
            else:
                st.markdown("#### Planner setup preview")
                p,r,a,i=st.columns(4)
                p.metric("Projects",f"{setup_preview['projects']['new']} new · {setup_preview['projects']['updated']} updated")
                r.metric("Resources",f"{setup_preview['resources']['new']} new · {setup_preview['resources']['updated']} updated")
                a.metric("Weekly allocations",setup_preview["allocations"]["rows"])
                i.metric("Internal activities",f"{setup_preview['activities']['new']} new · {setup_preview['activities']['updated']} updated")
                st.caption(f"Allocation projects matched: {setup_preview['allocations']['projects_matched']}. Existing data absent from the ZIP is retained.")
                if st.button("Apply planner setup",type="primary"):
                    try: apply_planner_setup(setup_preview,user); st.success("Planner setup imported."); refresh()
                    except ValueError as exc: st.error(str(exc))
        st.divider(); st.subheader("Project import")
        st.download_button("Download projects CSV",export_projects_csv(),f"production_planner_projects_{today_name}.csv","text/csv")
        project_upload=st.file_uploader("Upload projects CSV",type=["csv"],key="project_csv")
        if project_upload:
            project_preview=preview_project_import(project_upload)
            c1,c2,c3,c4=st.columns(4); c1.metric("New projects",project_preview["new"]); c2.metric("Updated projects",project_preview["updated"]); c3.metric("Unchanged projects",project_preview["unchanged"]); c4.metric("Invalid records",project_preview["invalid"])
            for error in project_preview["errors"]: st.error(error)
            for change in project_preview["changes"]:
                with st.expander(f"{change['project_code']} | {change['project_name']}"):
                    st.dataframe(pd.DataFrame(change["differences"]).rename(columns={"field":"Field","current":"Current","import":"Import"}),hide_index=True)
            if st.button("Apply project import",type="primary",disabled=bool(project_preview["errors"])):
                try: apply_project_import(project_preview,user); st.success("Project CSV imported."); refresh()
                except ValueError as exc: st.error(str(exc))
        with st.expander("Holiday import history"):
            history=pd.DataFrame(rows("SELECT id,filename,imported_by,imported_at,record_count,unmatched_count,summary_json FROM holiday_imports ORDER BY id DESC")); st.dataframe(history,hide_index=True,use_container_width=True)
    with audit_tab:
        st.subheader("Append-only audit log")
        audit=pd.DataFrame(rows("SELECT id,timestamp,user_name,action,object_type,object_id,project_code,department,field_name,details FROM audit_log ORDER BY timestamp DESC,id DESC"))
        if audit.empty: st.info("No audited changes yet.")
        else:
            parsed=parse_audit_timestamps(audit.timestamp); valid_dates=parsed.dropna().dt.date
            a1,a2,a3=st.columns(3); period=a1.date_input("Date range",(valid_dates.min(),valid_dates.max()),key="audit_dates") if not valid_dates.empty else None; users=["All",*sorted(audit.user_name.dropna().unique())]; au=a2.selectbox("User",users); entities=["All",*sorted(audit.object_type.dropna().unique())]; entity=a3.selectbox("Entity type",entities)
            b1,b2,b3=st.columns(3); projects=["All",*sorted(audit.project_code.dropna().unique())]; ap=b1.selectbox("Project",projects,key="audit_project"); departments=["All",*sorted(audit.department.dropna().unique())]; ad=b2.selectbox("Department",departments,key="audit_department"); actions=["All",*sorted(audit.action.dropna().unique())]; aa=b3.selectbox("Action",actions)
            filtered=audit.copy()
            if isinstance(period,(tuple,list)) and len(period)==2: filtered=filtered[(parsed.dt.date>=period[0])&(parsed.dt.date<=period[1])]
            for col,value in [("user_name",au),("object_type",entity),("project_code",ap),("department",ad),("action",aa)]:
                if value!="All": filtered=filtered[filtered[col]==value]
            filtered=filtered.assign(_parsed_timestamp=parsed.loc[filtered.index]).sort_values(["_parsed_timestamp","id"],ascending=False,na_position="last").drop(columns="_parsed_timestamp")
            st.dataframe(filtered.rename(columns={"timestamp":"When","user_name":"User","action":"Action","object_type":"Entity","project_code":"Project","department":"Department","details":"Summary"}),hide_index=True,use_container_width=True)


def principles_view() -> None:
    st.header("Production Capacity Planning Core Principles")
    principles = [
        ("Planning is an Escalation Tool", "Capacity planning is a continuous activity that production team managers do at least twice a week. It must produce an updated capacity plan with no projects carrying unplanned hours, plus questions, scenarios, or challenges for escalation to department heads."),
        ("All Time is Accounted For", "Capacity is finite and deadlines are contractual. Administrative, QA, training, and other time must be accurately accounted for alongside the corresponding reduction in planned project hours, so true production capacity remains visible."),
        ("Priority Dictates Planning", "Project prioritisation determines planning frequency and accuracy. Late-delivery penalties are the least flexible commitments; trade-offs made to protect them must remain visible in the plan."),
        ("Base Plans on Realistic, Evidenced Rates", "Use realistic scenarios supported by historical evidence, automation improvements, and bid-model budget hours. Material deviations and assumptions must be explicit before planning is complete."),
        ("The Sequence of Our Work Is Non-negotiable", "The workflow is sequential: Capture delays shift downstream work, and RS or GIS changes must flow into the PLS plan. Failing to update downstream dependencies makes the capacity plan unreliable."),
        ("Dynamic Response to Bottlenecks", "Do not force full utilisation within rigid team silos. Cross-trained staff should move dynamically to the stage constraining delivery, including shifts from RS to GIS or PLS as automation changes the bottleneck."),
    ]
    for number, (heading, detail) in enumerate(principles, 1):
        st.markdown(f"### {number}. **{heading}.**")
        st.write(detail)
    st.markdown("**Required outputs from every planning activity:**")
    st.markdown("- An updated capacity plan, leaving no projects with unplanned hours.\n"
                "- A list of questions, scenarios, or challenges to escalate to department heads.")


labels = navigation_for_role(st.session_state.role)
tabs = st.tabs(labels)
with tabs[0]: project_view()
with tabs[1]: planning_view()
with tabs[2]: principles_view()
with tabs[3]: resource_management_view()
if st.session_state.role == "admin":
    with tabs[4]: administration_view()
