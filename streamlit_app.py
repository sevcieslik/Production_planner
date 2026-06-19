from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import streamlit as st

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
    ensure_mvp_schema,
    get_projects,
    get_resources,
    import_default_projects,
    prepare_date_columns_for_editor,
    save_projects,
    save_resources,
    seed_resources_from_people,
    setting_float,
    week_starts,
    weekly_project_demand,
)

st.set_page_config(page_title="Production Planner MVP", layout="wide")

initialize_database()
ensure_mvp_schema()
seed_resources_from_people()


def monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def rerun() -> None:
    st.cache_data.clear()
    st.rerun()


st.sidebar.title("Production Planner")
st.sidebar.caption("Local-first three-tab MVP")

start = st.sidebar.date_input("Planning start", monday(date.today()))
end = st.sidebar.date_input("Planning end", start + timedelta(days=84))
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
        rerun()

    df = get_projects(True)
    if df.empty:
        df = pd.DataFrame(columns=PROJECT_FIELDS + ["archived"])

    project_editor_df = prepare_date_columns_for_editor(
        df[PROJECT_FIELDS], PROJECT_DATE_COLUMNS
    )

    edited = st.data_editor(
        project_editor_df,
        num_rows="dynamic",
        hide_index=True,
        use_container_width=True,
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

    col_save, col_delete = st.columns(2)

    if col_save.button("Save project changes", type="primary"):
        save_projects(edited.to_dict("records"))
        st.success("Projects saved. Allocations updated.")
        rerun()

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
        st.warning(f"Deleted {delete_code}.")
        rerun()


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
        rerun()

    rdf = get_resources()
    if rdf.empty:
        rdf = pd.DataFrame(columns=RESOURCE_FIELDS)

    resource_editor_df = prepare_date_columns_for_editor(rdf, RESOURCE_DATE_COLUMNS)

    redited = st.data_editor(
        resource_editor_df,
        num_rows="dynamic",
        hide_index=True,
        use_container_width=True,
        column_config={
            "department": st.column_config.SelectboxColumn(options=DISCIPLINES),
            "active_status": st.column_config.SelectboxColumn(options=RESOURCE_STATUSES),
            "status_start_date": st.column_config.DateColumn(),
            "status_end_date": st.column_config.DateColumn(),
        },
    )

    if st.button("Save resource changes", type="primary"):
        save_resources(redited.to_dict("records"))
        st.success("Resources saved. Allocations updated.")
        rerun()

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
        rerun()


with tab_allocations:
    st.title("Allocations")
    st.caption("Generated weekly demand and department capacity balance.")

    demand = weekly_project_demand()
    week_cols = [w.isoformat() for w in weeks]
    rows_out = []
    projects = get_projects(False)

    for p in projects.to_dict("records") if not projects.empty else []:
        for d in DISCIPLINES:
            if not demand.empty:
                sub = demand[
                    (demand.project_code == p["project_code"])
                    & (demand.department == d)
                ]
            else:
                sub = pd.DataFrame()

            row = {
                "Project Code": p["project_code"],
                "Project Name": p["project_name"],
                "Department": d,
                "Required Hours": p[f"{d.lower()}_hours"],
                "Start Date": p.get(f"{d.lower()}_start_date") or p["start_date"],
                "End Date": p["end_date"],
                "Loading Type": p["loading_type"],
            }

            for wc in week_cols:
                row[wc] = (
                    float(sub.loc[sub.week_start == wc, "demand_hours"].sum())
                    if not sub.empty
                    else 0.0
                )

            if p["loading_type"] == "manual":
                row["Manual Required"] = "Yes"

            rows_out.append(row)

    alloc_df = pd.DataFrame(rows_out)

    st.subheader("Project demand")
    st.dataframe(alloc_df, use_container_width=True, hide_index=True)

    bal = capacity_balance(weeks)
    summary = []

    for d in DISCIPLINES:
        for label, col in [
            ("available capacity", "available_capacity"),
            ("allocated demand", "allocated_demand"),
            ("over/under capacity", "over_under_capacity"),
        ]:
            row = {"Summary": f"{d} {label}"}

            for wc in week_cols:
                row[wc] = float(
                    bal.loc[
                        (bal.department == d) & (bal.week_start == wc),
                        col,
                    ].sum()
                )

            summary.append(row)

    sdf = pd.DataFrame(summary)

    st.subheader("Department summary")

    def colour(v):
        if not isinstance(v, (int, float)):
            return ""
        if v < 0:
            return "background-color:#ffc9c9"
        if v == 0:
            return "background-color:#e9ecef"
        return "background-color:#d8f3dc"

    st.dataframe(
        sdf.style.map(colour, subset=week_cols),
        use_container_width=True,
        hide_index=True,
    )