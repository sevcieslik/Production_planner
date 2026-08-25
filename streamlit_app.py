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
    sequence_analysis,
)
from app.services.setup_transfer import (
    apply_planner_setup, apply_project_import, export_planner_setup,
    export_projects_csv, preview_planner_setup, preview_project_import,
)

st.set_page_config(page_title="Production Capacity Planner", layout="wide")


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
    start_week = st.selectbox("Start week", weeks, format_func=lambda d: d.strftime("%d %b %Y"))
    selected_weeks: list[date]
    people = hours_person = hours_week = 0.0
    if mode == "Spread remaining":
        end_options = [w for w in weeks if w >= start_week]
        end_week = st.selectbox("End week", end_options, index=len(end_options) - 1, format_func=lambda d: d.strftime("%d %b %Y"))
        selected_weeks = [w for w in weeks if start_week <= w <= end_week]
    else:
        max_weeks = max(len([w for w in weeks if w >= start_week]), 1)
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
    operation_label = st.radio("Write mode", ["Add to existing allocation", "Replace allocation in selected period"], horizontal=True)
    operation = "add" if operation_label.startswith("Add") else "replace"
    cap = weekly_department_capacity(selected_weeks)
    capacity_map = cap[cap.department == department].set_index("week_start").available_capacity.to_dict() if not cap.empty else {}
    existing = rows("SELECT week_start,planned_hours FROM manager_weekly_plan WHERE department=? AND week_start BETWEEN ? AND ?",
                    (department, selected_weeks[0].isoformat(), selected_weeks[-1].isoformat()))
    all_existing = {w.isoformat(): 0.0 for w in selected_weeks}
    for row in existing: all_existing[row["week_start"]] += float(row["planned_hours"])
    project_existing = {(r["week_start"]): float(r["planned_hours"]) for r in rows(
        "SELECT week_start,planned_hours FROM manager_weekly_plan WHERE project_code=? AND department=?", (project_code, department))}
    preview = []
    for week, added in zip(selected_weeks, values):
        old_project = project_existing.get(week.isoformat(), 0.0)
        delta = added if operation == "add" else added - old_project
        new_total = all_existing[week.isoformat()] + delta
        preview.append({"Week": week.strftime("%d %b %Y"), "Available Capacity": capacity_map.get(week.isoformat(), 0),
                        "Existing Allocation": all_existing[week.isoformat()], "Added Allocation": delta,
                        "New Total": new_total, "Balance": capacity_map.get(week.isoformat(), 0) - new_total})
    total_effect = sum(r["Added Allocation"] for r in preview)
    c1, c2, c3 = st.columns(3)
    c1.metric("Allocation change", f"{total_effect:,.2f} h")
    c2.metric("Current remaining demand", f"{remaining:,.2f} h")
    c3.metric("Remaining after", f"{remaining - (sum(project_existing.values()) + total_effect):,.2f} h")
    if mode == "Spread remaining":
        st.caption(f"{len(values)} weeks · required average {values[0] if values else 0:,.2f} h/week · approx. {(values[0] / 37.5 if values else 0):,.2f} FTE at 37.5 h")
    st.dataframe(pd.DataFrame(preview), hide_index=True, use_container_width=True)
    if any(r["Balance"] < 0 for r in preview):
        st.warning("This allocation creates a capacity shortage. It may still be applied and escalated.")
    current_total = sum(project_existing.values())
    over = current_total + total_effect > remaining + 0.005
    override = st.checkbox("I explicitly approve planning more than Remaining Hours", disabled=not over)
    if over: st.error("The request exceeds remaining project demand. Explicit approval is required.")
    if st.button("Apply allocation", type="primary", disabled=not user or (over and not override)):
        try:
            apply_quick_allocation(project_code, department, selected_weeks, values, user, operation, override)
            st.success("Weekly manager allocation updated.")
            refresh()
        except ValueError as exc: st.error(str(exc))


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
    overview,gantt_tab,sequence_tab,weekly,issues_tab=st.tabs(["Overview","Gantt","Sequence","Weekly allocation","Issues"])
    bal=capacity_balance(weeks); selected=bal if department=="All" else bal[bal.department==department]
    health=project_health_plans(planning_start,department)
    with overview:
        remaining=float(health["Remaining Hours"].sum()) if not health.empty else 0
        unplanned=float(health.loc[health.Health=="Unplanned","Remaining Hours"].sum()) if not health.empty else 0
        shortage=selected[selected.over_under_capacity<0]
        open_issues=len(get_issues("Open",department))
        metrics=st.columns(6)
        metrics[0].metric("Remaining demand",f"{remaining:,.1f} h"); metrics[1].metric("Unplanned demand",f"{unplanned:,.1f} h")
        metrics[2].metric("Unallocated capacity in period",f"{max(float(selected.over_under_capacity.sum()),0):,.1f} h")
        metrics[3].metric("Weeks over capacity",str(len(shortage))); metrics[4].metric("Peak weekly shortage",f"{abs(float(shortage.over_under_capacity.min())) if not shortage.empty else 0:,.1f} h")
        metrics[5].metric("Open issues",str(open_issues))
        if not shortage.empty: st.caption("Earliest capacity gap: "+str(shortage.sort_values("week_start").iloc[0].week_start))
        st.subheader("Project-discipline plan health")
        counts=health.Health.value_counts() if not health.empty else pd.Series(dtype=int)
        cols=st.columns(4)
        for col,label in zip(cols,["Unplanned","Under-resourced","Well-resourced","Over-resourced"]): col.metric(label,int(counts.get(label,0)))
        chart=selected.groupby("week_start",as_index=False)[["available_capacity","total_allocated","over_under_capacity"]].sum(); chart["week_start"]=pd.to_datetime(chart.week_start)
        st.line_chart(chart.set_index("week_start")); st.caption("Available capacity = contracted roster − approved absence − temporary unavailability, with temporary assignments moved between departments. Balance = available capacity − project allocations − internal activities.")
        if not health.empty: st.dataframe(health,hide_index=True,use_container_width=True)
    with gantt_tab:
        gantt=allocation_timeline(weeks,department)
        if gantt.empty: st.info("No allocation or forecast baseline in this range.")
        else:
            gantt["Row"]=gantt["Project"]+"  ·  "+gantt["Discipline"]; gantt["Start"]=pd.to_datetime(gantt.Start); gantt["End"]=pd.to_datetime(gantt.End); gantt["Deadline"]=pd.to_datetime(gantt["Required by"])
            order=gantt["Row"].drop_duplicates().tolist()
            tips=["Project","project_code","Discipline","Start:T","End:T","Plan source","Remaining hours:Q","Allocated hours:Q","Required by:T","Health status","Shortfall / surplus:Q"]
            bars=alt.Chart(gantt).mark_bar(cornerRadius=2).encode(x=alt.X("Start:T",scale=alt.Scale(domain=[planning_start,planning_end+timedelta(days=6)]),title="Calendar date"),x2="End:T",y=alt.Y("Row:N",sort=order,title="Project / department"),color=alt.Color("Discipline:N",scale=alt.Scale(domain=DISCIPLINES,range=["#72a5d3","#76b77b","#c9ad6a"])),opacity=alt.Opacity("Plan source:N",scale=alt.Scale(domain=["Manager allocation","Forecast baseline"],range=[1,.28])),tooltip=tips)
            deadlines=alt.Chart(gantt).mark_tick(color="#b23a48",thickness=2,size=18).encode(x="Deadline:T",y=alt.Y("Row:N",sort=order),tooltip=["Project","Required by:T","Late"])
            st.altair_chart((bars+deadlines).properties(height=max(240,len(gantt)*28)),use_container_width=True)
            st.caption("Solid = Manager allocation; translucent = Forecast baseline. Red ticks = Required By. Department colour is not health.")
            st.dataframe(gantt.drop(columns=["Row","Deadline"]),hide_index=True,use_container_width=True)
    with sequence_tab:
        st.subheader("RS → GIS → PLS sequence analysis")
        st.caption("Advisory only: findings use explicit manager allocations and never move work or change project health.")
        findings=sequence_analysis(weeks)
        if findings.empty: st.info("No material sequence review items were found in the selected planning period.")
        else:
            counts=findings.Category.value_counts(); cols=st.columns(4)
            for col,label in zip(cols,["Gap","Possible overlap","Downstream starvation","Pull-forward opportunity"]): col.metric(label,int(counts.get(label,0)))
            st.dataframe(findings,hide_index=True,use_container_width=True)
    with weekly:
        def show_department(d: str, editable: bool=False):
            st.subheader(d)
            plan=manager_plan(weeks,d); week_cols=[w.isoformat() for w in weeks]
            if plan.empty: st.info(f"No active {d} demand."); return
            h=project_health_plans(planning_start,d)[["Project Code","Health"]]
            plan=h.merge(plan,on="Project Code",how="right"); display=plan[["Health","Project Code","Project","Remaining Hours",*week_cols]]
            if editable:
                if st.button("+ Allocate capacity",type="primary",key=f"allocate_{d}"): allocation_dialog(d)
                disabled=[c for c in plan.columns if c not in week_cols]
                edited=st.data_editor(plan,hide_index=True,use_container_width=True,disabled=disabled,column_config={w:st.column_config.NumberColumn(w,min_value=0.) for w in week_cols},key=f"plan_{d}")
                if st.button("Save manager plan",disabled=not user,key=f"save_{d}"):
                    try: save_manager_plan(edited,weeks,d,user); refresh()
                    except ValueError as exc: st.error(str(exc))
            else: st.dataframe(display,hide_index=True,use_container_width=True)
            d_bal=bal[bal.department==d].set_index("week_start")
            totals=pd.DataFrame([{ "Summary":f"{d} Allocated",**{w:float(d_bal.loc[w,"allocated_demand"]) for w in week_cols}}, {"Summary":f"{d} Capacity",**{w:float(d_bal.loc[w,"available_capacity"]) for w in week_cols}}, {"Summary":f"{d} Balance",**{w:float(d_bal.loc[w,"over_under_capacity"]) for w in week_cols}}])
            st.dataframe(totals,hide_index=True,use_container_width=True)
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
    resources_tab,holidays_tab,adjustments_tab=st.tabs(["Resources / roster","Absence & Holidays","Temporary assignments"])
    with resources_tab:
        resources=get_resources(); editor=prepare_date_columns_for_editor(resources,RESOURCE_DATE_COLUMNS) if not resources.empty else pd.DataFrame(columns=["person_name","department","weekly_hours","active_status"])
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
