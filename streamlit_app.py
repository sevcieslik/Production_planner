from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import streamlit as st

try:
    import plotly.graph_objects as go
except ImportError:  # pragma: no cover
    go = None

from app.data.db import execute, initialize_database, rows
from app.services.mvp import (
    DISCIPLINES,
    LOADING_TYPES,
    PROJECT_DATE_COLUMNS,
    PROJECT_FIELDS,
    RESOURCE_DATE_COLUMNS,
    RESOURCE_FIELDS,
    RESOURCE_STATUSES,
    capacity_balance,
    capacity_summary_cards,
    ensure_mvp_schema,
    get_data_version,
    get_last_updated_at,
    get_projects,
    get_holidays,
    get_resources,
    gantt_timeline_rows,
    import_approved_holidays,
    import_default_projects,
    import_sample_roster,
    increment_data_version,
    prepare_date_columns_for_editor,
    recalculate_holiday_totals,
    save_holidays,
    save_projects,
    save_resources,
    seed_resources_from_people,
    setting_float,
    summary_rows_from_capacity_balance,
    week_starts,
    weekly_department_capacity,
    weekly_project_demand,
)

st.set_page_config(page_title="Production Planner MVP", layout="wide")

initialize_database()
ensure_mvp_schema()
seed_resources_from_people()


def monday(d: date) -> date:
    return d - timedelta(days=d.weekday())




@st.cache_data(show_spinner=False)
def cached_weekly_project_demand(start_iso: str, end_iso: str, data_version: int) -> pd.DataFrame:
    return weekly_project_demand()


@st.cache_data(show_spinner=False)
def cached_weekly_department_capacity(week_values: tuple[str, ...], data_version: int) -> pd.DataFrame:
    return weekly_department_capacity([date.fromisoformat(w) for w in week_values])


@st.cache_data(show_spinner=False)
def cached_capacity_balance(week_values: tuple[str, ...], data_version: int) -> pd.DataFrame:
    return capacity_balance([date.fromisoformat(w) for w in week_values])


def clear_and_rerun() -> None:
    st.cache_data.clear()
    st.rerun()


st.sidebar.title("Production Planner")
st.sidebar.caption("Local-first three-tab MVP")

start = st.sidebar.date_input("Planning start", monday(date.today()))
end = st.sidebar.date_input("Planning end", date(2026, 12, 31))
weeks = week_starts(start, end)

tab_projects, tab_resources, tab_allocations = st.tabs(
    ["Projects", "Resources", "Allocations"]
)

with tab_projects:
    st.title("Projects")
    st.caption("Editable project register and demand input table.")

    c1, c2 = st.columns([1, 4])
    if c1.button("Import sample projects"):
        n = import_default_projects()
        st.success(f"Imported {n} projects from sample-data/projects.csv")
        clear_and_rerun()

    df = get_projects(True)
    if df.empty:
        df = pd.DataFrame(columns=PROJECT_FIELDS + ["archived"])

    project_editor_df = prepare_date_columns_for_editor(
        df[PROJECT_FIELDS], PROJECT_DATE_COLUMNS
    )

    with st.form("projects_form"):
        edited = st.data_editor(
            project_editor_df,
            num_rows="dynamic",
            hide_index=True,
            use_container_width=True,
            key="projects_editor",
            column_config={
                "loading_type": st.column_config.SelectboxColumn(options=LOADING_TYPES),
                "status": st.column_config.SelectboxColumn(options=["active", "archived"]),
                **{
                    c: st.column_config.DateColumn()
                    for c in [
                        "start_date",
                        "end_date",
                        "rs_start_date",
                        "gis_start_date",
                        "pls_start_date",
                    ]
                },
            },
        )
        save_projects_submit = st.form_submit_button("Save project changes", type="primary")

    col_save, col_delete = st.columns(2)

    if save_projects_submit:
        save_projects(edited.to_dict("records"))
        st.success("Projects saved. Allocations updated.")
        clear_and_rerun()

    delete_code = (
        col_delete.selectbox(
            "Delete project", [""] + df["project_code"].dropna().astype(str).tolist()
        )
        if not df.empty
        else ""
    )
    confirm = col_delete.checkbox("Confirm project delete")

    if col_delete.button("Delete selected project", disabled=not (delete_code and confirm)):
        execute("DELETE FROM mvp_projects WHERE project_code=?", (delete_code,))
        increment_data_version()
        st.warning(f"Deleted {delete_code}.")
        clear_and_rerun()


with tab_resources:
    st.title("Resources")
    st.caption("Editable resource pool. Suspensions, holidays and department moves flow into capacity.")

    factor = st.number_input(
        "Diminished capacity factor",
        min_value=0.0,
        max_value=1.0,
        value=setting_float("diminished_capacity_factor", 0.85),
        step=0.01,
    )

    if st.button("Save capacity factor"):
        execute(
            "INSERT OR REPLACE INTO settings(key,value) VALUES ('diminished_capacity_factor',?)",
            (str(factor),),
        )
        increment_data_version()
        clear_and_rerun()

    c_roster, c_holidays, c_recalc = st.columns(3)
    if c_roster.button("Import sample roster"):
        result = import_sample_roster()
        st.success(f"Imported {result.imported_people_count} new people and updated {result.updated_people_count} people.")
        if result.validation_issues:
            st.warning("Validation issues: " + "; ".join(result.validation_issues[:10]))
        if result.skipped_rows:
            st.warning(f"Skipped {result.skipped_rows} roster rows.")
        clear_and_rerun()

    if c_holidays.button("Import approved holidays"):
        result = import_approved_holidays()
        st.success(f"Imported {result.imported_holiday_records_count} holiday records.")
        if result.unmatched_holiday_names:
            st.warning("Unmatched holiday names: " + ", ".join(result.unmatched_holiday_names[:20]))
        if result.validation_issues:
            st.warning("Validation issues: " + "; ".join(result.validation_issues[:10]))
        if result.skipped_rows:
            st.warning(f"Skipped {result.skipped_rows} holiday rows.")
        clear_and_rerun()

    if c_recalc.button("Recalculate holiday totals"):
        updated = recalculate_holiday_totals()
        st.success(f"Recalculated holiday totals for {updated} resources.")
        clear_and_rerun()

    rdf = get_resources()
    if rdf.empty:
        rdf = pd.DataFrame(columns=RESOURCE_FIELDS)

    resource_editor_df = prepare_date_columns_for_editor(rdf, RESOURCE_DATE_COLUMNS)

    with st.form("resources_form"):
        redited = st.data_editor(
            resource_editor_df,
            num_rows="dynamic",
            hide_index=True,
            use_container_width=True,
            key="resources_editor",
            column_config={
                "department": st.column_config.SelectboxColumn(options=DISCIPLINES),
                "active_status": st.column_config.SelectboxColumn(options=RESOURCE_STATUSES),
                "status_start_date": st.column_config.DateColumn(),
                "status_end_date": st.column_config.DateColumn(),
            },
        )
        save_resources_submit = st.form_submit_button("Save resource changes", type="primary")

    if save_resources_submit:
        save_resources(redited.to_dict("records"))
        st.success("Resources saved. Allocations updated.")
        clear_and_rerun()

    st.subheader("Department changes")

    names = rdf["person_name"].dropna().astype(str).tolist() if not rdf.empty else []

    with st.form("department_change"):
        person = st.selectbox("Person", names) if names else ""
        dept = st.selectbox("Department", DISCIPLINES)
        ds = st.date_input("From date", start, key="dept_from")
        de = st.date_input("To date (optional range end)", end, key="dept_to")
        apply_change = st.form_submit_button("Save department date range")

    if apply_change and person:
        rid = rows("SELECT id FROM mvp_resources WHERE person_name=?", (person,))[0]["id"]
        execute(
            """
            INSERT INTO resource_department_assignments(
                resource_id,
                department,
                start_date,
                end_date
            )
            VALUES (?,?,?,?)
            """,
            (rid, dept, ds.isoformat(), de.isoformat()),
        )
        increment_data_version()
        clear_and_rerun()

    st.subheader("Holiday log")
    fc1, fc2, fc3, fc4 = st.columns(4)
    dept_filter = fc1.selectbox("Holiday department", ["All"] + DISCIPLINES)
    person_filter = fc2.selectbox("Holiday person", ["All"] + names) if names else "All"
    holiday_start = fc3.date_input("Holiday from", start, key="holiday_from")
    holiday_end = fc4.date_input("Holiday to", date(2026, 12, 31), key="holiday_to")
    holidays = get_holidays(dept_filter, person_filter, holiday_start.isoformat(), holiday_end.isoformat())
    if holidays.empty:
        holidays = pd.DataFrame(columns=["id", "person_name", "department", "holiday_date", "hours", "source", "notes"])
    holiday_editor_df = prepare_date_columns_for_editor(holidays, ["holiday_date"])
    hedited = st.data_editor(
        holiday_editor_df,
        num_rows="dynamic",
        hide_index=True,
        use_container_width=True,
        column_config={"holiday_date": st.column_config.DateColumn()},
    )
    hc1, hc2 = st.columns(2)
    if hc1.button("Save holiday changes"):
        save_holidays(hedited.to_dict("records"))
        st.success("Holiday records saved.")
        clear_and_rerun()
    delete_holiday_id = hc2.number_input("Delete holiday record id", min_value=0, step=1)
    if hc2.button("Delete holiday record", disabled=delete_holiday_id <= 0):
        execute("DELETE FROM holidays WHERE id=?", (int(delete_holiday_id),))
        recalculate_holiday_totals()
        increment_data_version()
        st.warning(f"Deleted holiday record {int(delete_holiday_id)}.")
        clear_and_rerun()


with tab_allocations:
    st.title("Project Processing Planner – Allocation Timeline")
    st.caption("Gantt-style processing timeline with detailed weekly tables hidden by default.")
    st.markdown(
        " ".join(
            [
                "<span style='display:inline-block;width:12px;height:12px;background:#2563eb;border-radius:3px'></span> RS = Remote Sensing",
                "<span style='display:inline-block;width:12px;height:12px;background:#16a34a;border-radius:3px;margin-left:1rem'></span> GIS = GIS Processing",
                "<span style='display:inline-block;width:12px;height:12px;background:#7c3aed;border-radius:3px;margin-left:1rem'></span> PLS = PLS-CADD",
            ]
        ),
        unsafe_allow_html=True,
    )

    data_version = get_data_version()
    last_updated = get_last_updated_at() or date.today().isoformat()
    st.caption(f"Last updated: {last_updated}")
    week_key = tuple(w.isoformat() for w in weeks)
    week_cols = list(week_key)

    with st.expander("Allocation controls", expanded=False):
        selected_departments = st.multiselect("Departments", DISCIPLINES, default=DISCIPLINES)
        active_only = st.checkbox("Show active projects only", value=True)

    with st.spinner("Calculating capacity timeline..."):
        demand = cached_weekly_project_demand(start.isoformat(), end.isoformat(), data_version)
        bal = cached_capacity_balance(week_key, data_version)

    projects = get_projects(not active_only)
    if selected_departments and not bal.empty and "department" in bal.columns:
        summary_bal = bal[bal.department.isin(selected_departments)]
    else:
        summary_bal = bal

    summary = capacity_summary_cards(summary_bal, demand, projects, start, end)
    c1, c2, c3, c4, c5 = st.columns(5)
    for col, dept in zip([c1, c2, c3], DISCIPLINES):
        value = summary.get(f"{dept} over_under_hours", 0.0)
        col.metric(f"{dept} over/under", f"{value:,.1f} h")
    c4.metric("Active projects", f"{summary.get('total_active_projects', 0):,.0f}")
    c5.metric("Required hours", f"{summary.get('total_required_hours', 0):,.1f} h")

    gantt_df = gantt_timeline_rows(projects, demand, summary_bal, start, end, selected_departments)
    st.subheader("Processing timeline")
    status_colours = {"green": "#22c55e", "amber": "#f59e0b", "red": "#ef4444", "grey": "#9ca3af"}
    discipline_colours = {"RS": "#2563eb", "GIS": "#16a34a", "PLS": "#7c3aed"}
    status_labels = {"green": "OK", "amber": "Near capacity", "red": "Over capacity", "grey": "No/missing capacity"}

    if gantt_df.empty:
        st.info("No non-zero project discipline demand falls within the selected planning range.")
    elif go is not None:
        fig = go.Figure()
        y_values = [f"{r.project_label}  ·  {r.discipline}" for r in gantt_df.itertuples()]
        for row, y in zip(gantt_df.to_dict("records"), y_values):
            start_ts = pd.Timestamp(row["start"])
            end_ts = pd.Timestamp(row["end"])
            duration_ms = max((end_ts - start_ts).days + 1, 1) * 24 * 60 * 60 * 1000
            status = row["capacity_status"]
            fig.add_trace(
                go.Bar(
                    x=[duration_ms],
                    y=[y],
                    base=[start_ts],
                    orientation="h",
                    marker={"color": discipline_colours[row["discipline"]], "line": {"color": status_colours[status], "width": 3}},
                    text=[f"{row['discipline']} · {row['required_hours']:,.0f}h · {status_labels[status]}"],
                    textposition="inside",
                    hovertemplate=(
                        "<b>%{y}</b><br>Discipline: " + row["discipline"] +
                        f"<br>Required hours: {row['required_hours']:,.1f}" +
                        f"<br>Start date: {row['source_start']}" +
                        f"<br>End date: {row['source_end']}" +
                        f"<br>Loading type: {row['loading_type']}" +
                        f"<br>Total weekly demand: {row['total_weekly_demand']:,.1f} h" +
                        f"<br>Capacity status: {status_labels[status]}<extra></extra>"
                    ),
                    showlegend=False,
                )
            )
        fig.update_layout(
            height=max(420, min(1100, 42 * len(gantt_df.index))),
            margin={"l": 20, "r": 20, "t": 20, "b": 20},
            xaxis={"type": "date", "tickformat": "%b %Y", "dtick": "M1", "range": [pd.Timestamp(start), pd.Timestamp(end)]},
            yaxis={"autorange": "reversed"},
            barmode="overlay",
        )
        st.plotly_chart(fig, use_container_width=True)
    else:
        st.dataframe(gantt_df[["project_label", "discipline", "start", "end", "required_hours", "capacity_status"]], use_container_width=True, hide_index=True)

    rows_out = []
    if not projects.empty and selected_departments:
        for p in projects.to_dict("records"):
            for d in selected_departments:
                sub = demand[(demand.project_code == p["project_code"]) & (demand.department == d)] if not demand.empty and {"project_code", "department"}.issubset(demand.columns) else pd.DataFrame()
                row = {"Project Code": p["project_code"], "Project Name": p["project_name"], "Department": d, "Required Hours": p[f"{d.lower()}_hours"], "Start Date": p.get(f"{d.lower()}_start_date") or p["start_date"], "End Date": p["end_date"], "Loading Type": p["loading_type"]}
                for wc in week_cols:
                    row[wc] = float(sub.loc[sub.week_start == wc, "demand_hours"].sum()) if not sub.empty else 0.0
                rows_out.append(row)
    alloc_df = pd.DataFrame(rows_out)
    sdf = summary_rows_from_capacity_balance(summary_bal, week_cols)
    if selected_departments:
        sdf = sdf[sdf["Summary"].str.split().str[0].isin(selected_departments)]

    with st.expander("Show weekly project demand table", expanded=False):
        st.dataframe(alloc_df, use_container_width=True, hide_index=True)

    with st.expander("Show department capacity summary", expanded=False):
        def colour(v):
            if not isinstance(v, (int, float)):
                return ""
            if v < 0:
                return "background-color:#ffc9c9"
            if v == 0:
                return "background-color:#e9ecef"
            return "background-color:#d8f3dc"
        st.dataframe(sdf.style.map(colour, subset=week_cols), use_container_width=True, hide_index=True)

    with st.expander("Data status", expanded=False):
        active_projects = get_projects(False)
        resources = get_resources()
        holiday_count = rows("SELECT COUNT(*) c FROM holidays")[0]["c"]
        st.write({"active_projects": 0 if active_projects.empty else len(active_projects), "resources": 0 if resources.empty else len(resources), "holiday_records": holiday_count, "planning_weeks": len(weeks), "data_version": data_version, "last_updated_at": last_updated})
