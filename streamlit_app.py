from __future__ import annotations
from datetime import date, timedelta
import pandas as pd
import streamlit as st
from app.data.db import initialize_database, rows, execute
from app.importers.csv_xlsx import TABLE_COLUMNS, read_upload, validate, import_dataframe
from app.services.alerts import save_alerts
from app.services.forecasting import project_forecasts, save_forecasts

st.set_page_config(page_title='Production Capacity Planner', layout='wide')
initialize_database()

@st.cache_data(ttl=5)
def q(sql, params=()):
    return pd.DataFrame(rows(sql, params))

def week_start(d):
    return d - timedelta(days=d.weekday())

def user_header():
    users = q('SELECT user_name, role FROM user_roles ORDER BY role,user_name')
    selected = st.sidebar.selectbox('Local user', users['user_name'].tolist())
    role = users.loc[users.user_name == selected, 'role'].iloc[0]
    st.sidebar.info(f'Role: {role}. PM is view/export only.')
    return selected, role

user, role = user_header(); can_edit = role in ('CDO','Manager')
page = st.sidebar.radio('View', ['Dashboard','Team capacity','Project view','People allocation','Allocation editor','Import / export','Audit log','Architecture & roadmap'])
start = st.sidebar.date_input('Start date', week_start(date.today()))
end = st.sidebar.date_input('End date', start + timedelta(days=6))

if page == 'Dashboard':
    alerts = save_alerts(str(start), str(end)); save_forecasts(str(end))
    c = q('''SELECT COALESCE(SUM(available_hours),0) available, COALESCE((SELECT SUM(allocated_hours) FROM daily_allocations WHERE allocation_date BETWEEN ? AND ?),0) allocated FROM availability_calendar WHERE work_date BETWEEN ? AND ?''', (str(start),str(end),str(start),str(end)))
    available=float(c.available.iloc[0]); allocated=float(c.allocated.iloc[0]); over=max(allocated-available,0)
    cols=st.columns(4); cols[0].metric('Available hours', f'{available:.1f}'); cols[1].metric('Allocated hours', f'{allocated:.1f}'); cols[2].metric('Unallocated hours', f'{max(available-allocated,0):.1f}'); cols[3].metric('Overallocated hours', f'{over:.1f}')
    st.subheader('Key alerts'); st.dataframe(pd.DataFrame(alerts), use_container_width=True)

elif page == 'Team capacity':
    df=q('''SELECT d.code discipline, substr(ac.work_date,1,10) date, COALESCE(SUM(ac.available_hours),0) available_hours,
    COALESCE((SELECT SUM(wd.demand_hours) FROM weekly_demand wd WHERE wd.discipline_id=d.id AND wd.week_start BETWEEN ? AND ?),0) planned_demand,
    COALESCE(SUM(da.allocated_hours),0) allocated_hours
    FROM disciplines d JOIN people p ON p.discipline_id=d.id LEFT JOIN availability_calendar ac ON ac.person_id=p.id AND ac.work_date BETWEEN ? AND ? LEFT JOIN daily_allocations da ON da.person_id=p.id AND da.allocation_date=ac.work_date GROUP BY d.code, ac.work_date''',(str(start),str(end),str(start),str(end)))
    if not df.empty:
        summary=df.groupby('discipline', as_index=False).agg({'available_hours':'sum','planned_demand':'max','allocated_hours':'sum'}); summary['remaining_capacity']=summary.available_hours-summary.allocated_hours; summary['over_under_vs_demand']=summary.available_hours-summary.planned_demand
        st.dataframe(summary, use_container_width=True)

elif page == 'Project view':
    f=pd.DataFrame(project_forecasts(str(end)))
    budgets=q('''SELECT p.project_name,d.code, b.planned_hours FROM project_discipline_budgets b JOIN projects p ON p.id=b.project_id JOIN disciplines d ON d.id=b.discipline_id''')
    st.subheader('Forecasts and deadline risk'); st.dataframe(f, use_container_width=True)
    st.subheader('Planned hours by discipline'); st.dataframe(budgets.pivot_table(index='project_name', columns='code', values='planned_hours', aggfunc='sum').fillna(0), use_container_width=True)

elif page == 'People allocation':
    df=q('''SELECT pe.name, ac.work_date, ac.available_hours, COALESCE(pr.project_name,cat.name,'Unallocated') assigned_to, da.split_slot, COALESCE(da.allocated_hours,0) allocated_hours,
    CASE WHEN COALESCE((SELECT SUM(allocated_hours) FROM daily_allocations x WHERE x.person_id=pe.id AND x.allocation_date=ac.work_date),0)>ac.available_hours THEN 'Overallocated' WHEN COALESCE((SELECT SUM(allocated_hours) FROM daily_allocations x WHERE x.person_id=pe.id AND x.allocation_date=ac.work_date),0)=0 THEN 'Underallocated' ELSE 'OK' END flag
    FROM people pe JOIN availability_calendar ac ON ac.person_id=pe.id LEFT JOIN daily_allocations da ON da.person_id=pe.id AND da.allocation_date=ac.work_date LEFT JOIN projects pr ON pr.id=da.project_id LEFT JOIN non_billable_categories cat ON cat.id=da.category_id WHERE ac.work_date BETWEEN ? AND ? ORDER BY pe.name, ac.work_date, da.split_slot''',(str(start),str(end)))
    st.dataframe(df, use_container_width=True)

elif page == 'Allocation editor':
    if not can_edit: st.warning('PM users are view/export only.'); st.stop()
    st.write('Safe form-based editor. Existing allocations are not overwritten unless confirmed.')
    people=q('SELECT id,name,daily_hours FROM people WHERE active=1'); projects=q('SELECT id,project_name FROM projects WHERE status="Active"')
    with st.form('alloc'):
        person=st.selectbox('Person', people.name); project_names=projects.project_name.tolist(); primary=st.selectbox('Primary project/task', project_names); split=st.checkbox('Equal 50/50 split with second project/task'); second=st.selectbox('Second project/task', project_names) if split else None
        astart=st.date_input('Allocation start', start); aend=st.date_input('Allocation end', end); overwrite=st.checkbox('Confirm overwrite of existing allocations'); reason=st.text_input('Reason/comment'); submitted=st.form_submit_button('Save allocation')
    if submitted:
        pid=int(people.loc[people.name==person,'id'].iloc[0]); daily=float(people.loc[people.name==person,'daily_hours'].iloc[0]); p1=int(projects.loc[projects.project_name==primary,'id'].iloc[0]); p2=int(projects.loc[projects.project_name==second,'id'].iloc[0]) if split else None
        cur=astart; saved=0
        while cur<=aend:
            if cur.weekday()<5:
                existing=rows('SELECT * FROM daily_allocations WHERE person_id=? AND allocation_date=?',(pid,str(cur)))
                if existing and not overwrite: st.error(f'Skipped {cur}: existing allocation. Tick confirmation to overwrite.'); cur+=timedelta(days=1); continue
                if overwrite: execute('DELETE FROM daily_allocations WHERE person_id=? AND allocation_date=?',(pid,str(cur)),user=user,audit={'object_type':'DailyAllocation','action':'delete','previous':existing,'reason':reason})
                hrs=daily/2 if split else daily
                execute('INSERT INTO daily_allocations(person_id,allocation_date,project_id,split_slot,allocated_hours,notes) VALUES (?,?,?,?,?,?)',(pid,str(cur),p1,1,hrs,reason),user=user,audit={'object_type':'DailyAllocation','action':'insert','new':{'person':person,'date':str(cur),'project':primary,'hours':hrs},'reason':reason})
                if split: execute('INSERT INTO daily_allocations(person_id,allocation_date,project_id,split_slot,allocated_hours,notes) VALUES (?,?,?,?,?,?)',(pid,str(cur),p2,2,hrs,reason),user=user,audit={'object_type':'DailyAllocation','action':'insert','new':{'person':person,'date':str(cur),'project':second,'hours':hrs},'reason':reason})
                saved+=1
            cur+=timedelta(days=1)
        st.success(f'Saved allocations for {saved} working days.')

elif page == 'Import / export':
    st.subheader('Imports'); import_type=st.selectbox('Import type', list(TABLE_COLUMNS)); st.caption('Required columns: '+', '.join(TABLE_COLUMNS[import_type])); file=st.file_uploader('CSV/XLSX file', type=['csv','xlsx','xls'])
    if file and can_edit:
        df=read_upload(file); missing=validate(df, import_type); st.dataframe(df.head(20), use_container_width=True)
        if missing: st.error('Missing columns: '+', '.join(missing))
        elif st.button('Import validated file'):
            st.success(f'Imported {import_dataframe(df, import_type, user)} rows.')
    elif file: st.warning('PM users can preview and export only.')
    st.subheader('Exports'); export=q('SELECT * FROM projects')
    st.download_button('Download projects CSV', export.to_csv(index=False), 'projects_export.csv')

elif page == 'Audit log':
    st.dataframe(q('SELECT * FROM audit_log ORDER BY timestamp DESC LIMIT 500'), use_container_width=True)

else:
    st.markdown('''### Architecture
Local Streamlit UI backed by SQLite, with separated data schema, import adapters, forecasting service and alert engine. The database file is `data/production_planner.sqlite` and can be placed in a synced Google Drive folder for a simple central-file deployment.

### Roadmap
1. Add service-account Google Sheets/Drive adapters behind the import abstraction.
2. Add proper authentication and role-based permissions.
3. Add richer drag/drop calendar editing and approval workflows.
4. Add stale-source monitoring, import reconciliation and conflict reports.
5. Add deployment packaging and backup/restore automation.
''')
