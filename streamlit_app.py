from __future__ import annotations

from datetime import date, timedelta
import pandas as pd
import altair as alt
import streamlit as st

from app.data.db import initialize_database, rows
from app.services.mvp import (
    DISCIPLINES, RESOURCE_DATE_COLUMNS, allocation_timeline, apply_holiday_snapshot,
    apply_quick_allocation, capacity_balance, clear_future_allocation, create_escalation,
    ensure_mvp_schema, get_holidays, get_internal_activities, get_projects, get_resources,
    import_sample_roster, internal_activity_by_week, load_roster_csv, manager_plan,
    monthly_allocation_matrix, move_allocation, prepare_date_columns_for_editor,
    preview_holiday_snapshot, project_remaining_hours, project_health_plans, quick_allocation_values,
    save_internal_activities, save_manager_plan, save_projects, save_resources,
    validate_project_demand, week_starts, weekly_department_capacity, get_issues, update_issue,
)

st.set_page_config(page_title="Production Capacity Planner", layout="wide")
initialize_database(seed=False)
ensure_mvp_schema()


def monday(value: date) -> date:
    return value - timedelta(days=value.weekday())


def refresh() -> None:
    st.rerun()


st.sidebar.title("Production Planner")
user = st.sidebar.text_input("Your name", help="Recorded against planning and import changes.")
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
    if st.button("Apply allocation", type="primary", disabled=not user.strip() or (over and not override)):
        try:
            apply_quick_allocation(project_code, department, selected_weeks, values, user.strip(), operation, override)
            st.success("Weekly manager allocation updated.")
            refresh()
        except ValueError as exc: st.error(str(exc))


def project_view() -> None:
    st.header("Projects")
    st.caption("Project demand remains the contractual and forecast register; allocation is managed separately.")
    existing = get_projects(True)
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
        else: save_projects([record], user.strip()); st.success("Project demand saved."); refresh()
    st.subheader("Demand register")
    register=get_projects(False)
    st.dataframe(register[["priority","project_code","project_name","client","project_manager","spus","row_km","cct_km","rs_hours","gis_hours","pls_hours","end_date","status"]] if not register.empty else register,hide_index=True,use_container_width=True)


def planning_view() -> None:
    st.header("Planning")
    if not weeks: st.error("Planning end must not be before planning start."); return
    department=st.segmented_control("Department",["All",*DISCIPLINES],default="All")
    overview,gantt_tab,weekly,issues_tab=st.tabs(["Overview","Gantt","Weekly allocation","Issues"])
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
        st.line_chart(chart.set_index("week_start")); st.caption("Available capacity = contracted roster − absence. Balance = available capacity − project allocations − internal activities.")
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
                if st.button("Save manager plan",disabled=not user.strip(),key=f"save_{d}"):
                    try: save_manager_plan(edited,weeks,d,user.strip()); refresh()
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
                c1,c2=st.columns(2); code=c1.selectbox("Project",codes); dept=c2.selectbox("Department",DISCIPLINES,index=DISCIPLINES.index(department) if department in DISCIPLINES else 0); kind=st.selectbox("Issue type",["Capacity shortage","Data delay","Priority conflict","Skills gap"]); impact=st.number_input("Impact hours",0.); decision=st.text_area("Decision required"); owner_new=st.text_input("Owner"); due=st.date_input("Required by",date.today()); submit=st.form_submit_button("Create issue",disabled=not user.strip())
            if submit:
                try: create_escalation(code,dept,kind,impact,decision,owner_new,due,user.strip()); refresh()
                except ValueError as exc: st.error(str(exc))
        if not issue_data.empty:
            with st.expander("Update / close / reopen issue"):
                issue_id=st.selectbox("Issue",issue_data.id.tolist(),format_func=lambda i:f"#{i} · "+str(issue_data.loc[issue_data.id==i,"decision_required"].iloc[0])[:70]); current=issue_data[issue_data.id==issue_id].iloc[0]
                new_owner=st.text_input("Owner",str(current.owner)); new_due=st.date_input("Required by",pd.to_datetime(current.required_by).date()); resolution=st.text_area("Resolution / outcome",str(current.resolution or "")); new_status=st.radio("Status",["Open","Closed"],index=0 if current.status=="Open" else 1,horizontal=True)
                if st.button("Save issue",disabled=not user.strip()):
                    try: update_issue(int(issue_id),user.strip(),owner=new_owner,required_by=new_due,resolution=resolution,status=new_status); refresh()
                    except ValueError as exc: st.error(str(exc))

def administration_view() -> None:
    st.header("Administration")
    resources_tab,holidays_tab,internal_tab,imports_tab,audit_tab=st.tabs(["Resources","Absence & Holidays","Internal Activities","Imports","Audit log"])
    with resources_tab:
        upload=st.file_uploader("Roster CSV / Excel",type=["csv","xlsx","xls"],key="roster")
        if upload:
            preview=load_roster_csv(upload); st.dataframe(preview,hide_index=True)
            if st.button("Import roster",disabled=not user.strip()): upload.seek(0); import_sample_roster(upload); refresh()
        resources=get_resources(); editor=prepare_date_columns_for_editor(resources,RESOURCE_DATE_COLUMNS) if not resources.empty else pd.DataFrame(columns=["person_name","department","weekly_hours","active_status"])
        edited=st.data_editor(editor,num_rows="dynamic",hide_index=True,use_container_width=True)
        if st.button("Save resources",disabled=not user.strip()): save_resources(edited.to_dict("records"),user.strip()); refresh()
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
            if st.button("Apply holiday snapshot",type="primary",disabled=not user.strip()): apply_holiday_snapshot(preview,upload.name,user.strip()); refresh()
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
        if st.button("Save internal activities",disabled=not user.strip()): save_internal_activities(edited.to_dict("records"),user.strip()); refresh()
    with imports_tab:
        history=pd.DataFrame(rows("SELECT id,filename,imported_by,imported_at,record_count,unmatched_count,summary_json FROM holiday_imports ORDER BY id DESC")); st.subheader("Import history"); st.dataframe(history,hide_index=True,use_container_width=True)
    with audit_tab:
        st.subheader("Append-only audit log")
        audit=pd.DataFrame(rows("SELECT id,timestamp,user_name,action,object_type,object_id,project_code,department,field_name,details FROM audit_log ORDER BY timestamp DESC,id DESC"))
        if audit.empty: st.info("No audited changes yet.")
        else:
            dates=pd.to_datetime(audit.timestamp).dt.date; a1,a2,a3=st.columns(3); period=a1.date_input("Date range",(dates.min(),dates.max()),key="audit_dates"); users=["All",*sorted(audit.user_name.dropna().unique())]; au=a2.selectbox("User",users); entities=["All",*sorted(audit.object_type.dropna().unique())]; entity=a3.selectbox("Entity type",entities)
            b1,b2,b3=st.columns(3); projects=["All",*sorted(audit.project_code.dropna().unique())]; ap=b1.selectbox("Project",projects,key="audit_project"); departments=["All",*sorted(audit.department.dropna().unique())]; ad=b2.selectbox("Department",departments,key="audit_department"); actions=["All",*sorted(audit.action.dropna().unique())]; aa=b3.selectbox("Action",actions)
            filtered=audit.copy()
            if isinstance(period,(tuple,list)) and len(period)==2: filtered=filtered[(dates>=period[0])&(dates<=period[1])]
            for col,value in [("user_name",au),("object_type",entity),("project_code",ap),("department",ad),("action",aa)]:
                if value!="All": filtered=filtered[filtered[col]==value]
            st.dataframe(filtered.rename(columns={"timestamp":"When","user_name":"User","action":"Action","object_type":"Entity","project_code":"Project","department":"Department","details":"Summary"}),hide_index=True,use_container_width=True)


projects_tab,planning_tab,administration_tab=st.tabs(["Projects","Planning","Administration"])
with projects_tab: project_view()
with planning_tab: planning_view()
with administration_tab: administration_view()
