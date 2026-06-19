from __future__ import annotations
from datetime import date, timedelta
import pandas as pd
import streamlit as st
from app.data.db import initialize_database, rows, execute, clear_demo_data, reset_database
from app.importers.csv_xlsx import TABLE_COLUMNS, read_upload, validate, import_dataframe
from app.importers.workbook import profile_workbook, profiles_to_frame, stage_workbook, commit_workbook_batch, generated_teams_breakdown, generated_capacity
from app.services.alerts import save_alerts
from app.services.forecasting import project_forecasts, save_forecasts
from app.services.planning import week_starts, spread_hours, save_weekly_demand, gap_analysis, discipline_metrics

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
page = st.sidebar.radio('View', ['Manager dashboard','Planning mode','Allocation mode','Gap analysis','Dashboard','Team capacity','Project view','People allocation','Allocation editor','Capacity settings','Import / export','Admin','Audit log','Architecture & roadmap'])
start = st.sidebar.date_input('Start date', week_start(date.today()))
end = st.sidebar.date_input('End date', start + timedelta(days=6))

if page == 'Manager dashboard':
    st.title('Manager dashboard')
    discipline = st.selectbox('Discipline page', ['RS','GIS','PLS'])
    today = date.today(); periods = {'This week': (week_start(today), week_start(today)+timedelta(days=6)), 'Next week': (week_start(today)+timedelta(days=7), week_start(today)+timedelta(days=13)), 'Next 4 weeks': (week_start(today), week_start(today)+timedelta(days=27))}
    cols = st.columns(3)
    for col, (label, (ps, pe)) in zip(cols, periods.items()):
        m = discipline_metrics(discipline, str(ps), str(pe))
        with col:
            st.subheader(label)
            st.metric('Available', f"{m['available']:.1f}h")
            st.metric('Allocated', f"{m['allocated']:.1f}h")
            st.metric('Unallocated', f"{m['unallocated']:.1f}h")
            st.metric('Overallocated', f"{m['overallocated']:.1f}h")
    risks = pd.DataFrame([f for f in project_forecasts(str(periods['Next 4 weeks'][1])) if f['deadline_risk'] != 'Low' or (f['variance_hours'] and f['variance_hours'] > 0)])
    st.subheader(f'{discipline} projects at risk')
    st.dataframe(risks, use_container_width=True)

elif page == 'Planning mode':
    st.title('Planning mode')
    if not can_edit: st.warning('PM users are view/export only.')
    projects=q('SELECT id,project_name FROM projects ORDER BY project_name'); disciplines=q('SELECT id,code FROM disciplines ORDER BY code')
    with st.form('demand_spread'):
        project_name=st.selectbox('Project', projects.project_name)
        disc_code=st.selectbox('Discipline', disciplines.code)
        total=st.number_input('Total planned demand hours to spread', min_value=0.0, step=7.5, value=37.5)
        ds=st.date_input('Demand start', start); de=st.date_input('Demand end', end)
        mode=st.radio('Spread method', ['Even spread','Front-loaded','Back-loaded','Manual weekly spread'], horizontal=True)
        weeks=week_starts(ds,de); manual=[]
        if mode == 'Manual weekly spread':
            st.caption('Enter hours per week. Total can intentionally differ when managers are revising demand.')
            for w in weeks:
                manual.append(st.number_input(f'Week {w.isoformat()}', min_value=0.0, step=1.0, key=f'manual_{w}'))
        reason=st.text_input('Reason/comment', key='planning_reason')
        submitted=st.form_submit_button('Save weekly demand', disabled=not can_edit)
    preview=spread_hours(total,weeks,mode,manual if mode == 'Manual weekly spread' else None)
    st.subheader('Demand preview')
    st.dataframe(pd.DataFrame({'week_start':[w.isoformat() for w in weeks], 'demand_hours': preview}), use_container_width=True)
    if submitted:
        save_weekly_demand(int(projects.loc[projects.project_name==project_name,'id'].iloc[0]), int(disciplines.loc[disciplines.code==disc_code,'id'].iloc[0]), weeks, preview, user, reason)
        st.success('Weekly demand saved.')

elif page == 'Allocation mode':
    st.title('Allocation mode')
    if not can_edit: st.warning('PM users are view/export only.'); st.stop()
    disc=st.selectbox('Discipline', ['RS','GIS','PLS'])
    gaps=gap_analysis(str(start), str(end), disc)
    st.subheader('Project demand to fill')
    demand_df=pd.DataFrame(gaps['demand']); st.dataframe(demand_df, use_container_width=True)
    st.subheader('Available people in discipline')
    people=q('''SELECT pe.id,pe.name,pe.daily_hours FROM people pe JOIN disciplines d ON d.id=pe.discipline_id WHERE d.code=? AND pe.active=1 ORDER BY pe.name''',(disc,))
    st.dataframe(people, use_container_width=True)
    projects=q('SELECT id,project_name FROM projects WHERE status="Active" ORDER BY project_name')
    with st.form('assign_demand'):
        person=st.selectbox('Assign person', people.name)
        project=st.selectbox('To project demand', projects.project_name)
        full=st.radio('Allocation size', ['Full day','50/50 split'], horizontal=True)
        astart=st.date_input('Assign start', start, key='am_start'); aend=st.date_input('Assign end', end, key='am_end')
        overwrite=st.checkbox('Confirm overwrite of existing allocations', key='am_overwrite')
        reason=st.text_input('Reason/comment', key='am_reason')
        go=st.form_submit_button('Assign')
    if go:
        pid=int(people.loc[people.name==person,'id'].iloc[0]); daily=float(people.loc[people.name==person,'daily_hours'].iloc[0]); pr=int(projects.loc[projects.project_name==project,'id'].iloc[0]); hrs=daily if full=='Full day' else daily/2; cur=astart; saved=0
        while cur<=aend:
            if cur.weekday()<5:
                existing=rows('SELECT * FROM daily_allocations WHERE person_id=? AND allocation_date=? AND split_slot=1',(pid,str(cur)))
                if existing and not overwrite: st.error(f'Skipped {cur}: existing allocation.'); cur+=timedelta(days=1); continue
                if overwrite: execute('DELETE FROM daily_allocations WHERE person_id=? AND allocation_date=? AND split_slot=1',(pid,str(cur)),user=user,audit={'object_type':'DailyAllocation','action':'delete','previous':existing,'reason':reason})
                wi=rows('SELECT id FROM work_items WHERE project_id=?',(pr,)); wid=wi[0]['id'] if wi else None
                execute('INSERT OR REPLACE INTO daily_allocations(person_id,allocation_date,project_id,work_item_id,split_slot,allocated_hours,notes) VALUES (?,?,?,?,?,?,?)',(pid,str(cur),pr,wid,1,hrs,reason),user=user,audit={'object_type':'DailyAllocation','action':'assign_to_demand','new':{'person':person,'project':project,'date':str(cur),'hours':hrs},'reason':reason}); saved+=1
            cur+=timedelta(days=1)
        st.success(f'Assigned {saved} working days.')

elif page == 'Gap analysis':
    st.title('Gap analysis')
    disc=st.selectbox('Discipline filter', ['All','RS','GIS','PLS'])
    gaps=gap_analysis(str(start), str(end), None if disc=='All' else disc)
    st.subheader('Demand vs assigned capacity and unfilled demand')
    st.dataframe(pd.DataFrame(gaps['demand']), use_container_width=True)
    st.subheader('People without work')
    st.dataframe(pd.DataFrame(gaps['unassigned_people']), use_container_width=True)
    st.subheader('Overallocated people')
    st.dataframe(pd.DataFrame(gaps['overallocated_people']), use_container_width=True)

elif page == 'Dashboard':
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
    df=q('''SELECT pe.name, COALESCE(ac.work_date, '') work_date, COALESCE(ac.available_hours, pe.daily_hours, 0) available_hours, COALESCE(wi.name,pr.project_name,cat.name,'Unallocated') assigned_to, da.split_slot, COALESCE(da.allocated_hours,0) allocated_hours,
    CASE WHEN ac.work_date IS NULL THEN 'No availability record' WHEN COALESCE((SELECT SUM(allocated_hours) FROM daily_allocations x WHERE x.person_id=pe.id AND x.allocation_date=ac.work_date),0)>COALESCE(ac.available_hours,0) THEN 'Overallocated' WHEN COALESCE((SELECT SUM(allocated_hours) FROM daily_allocations x WHERE x.person_id=pe.id AND x.allocation_date=ac.work_date),0)=0 THEN 'Underallocated' ELSE 'OK' END flag
    FROM people pe LEFT JOIN availability_calendar ac ON ac.person_id=pe.id AND ac.work_date BETWEEN ? AND ? LEFT JOIN daily_allocations da ON da.person_id=pe.id AND da.allocation_date=ac.work_date LEFT JOIN projects pr ON pr.id=da.project_id LEFT JOIN work_items wi ON wi.id=da.work_item_id LEFT JOIN non_billable_categories cat ON cat.id=da.category_id WHERE pe.active=1 ORDER BY pe.name, ac.work_date, da.split_slot''',(str(start),str(end)))
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
                wi1=rows('SELECT id FROM work_items WHERE project_id=?',(p1,)); wid1=wi1[0]['id'] if wi1 else None
                execute('INSERT INTO daily_allocations(person_id,allocation_date,project_id,work_item_id,split_slot,allocated_hours,notes) VALUES (?,?,?,?,?,?,?)',(pid,str(cur),p1,wid1,1,hrs,reason),user=user,audit={'object_type':'DailyAllocation','action':'insert','new':{'person':person,'date':str(cur),'project':primary,'hours':hrs},'reason':reason})
                if split:
                    wi2=rows('SELECT id FROM work_items WHERE project_id=?',(p2,)); wid2=wi2[0]['id'] if wi2 else None
                    execute('INSERT INTO daily_allocations(person_id,allocation_date,project_id,work_item_id,split_slot,allocated_hours,notes) VALUES (?,?,?,?,?,?,?)',(pid,str(cur),p2,wid2,2,hrs,reason),user=user,audit={'object_type':'DailyAllocation','action':'insert','new':{'person':person,'date':str(cur),'project':second,'hours':hrs},'reason':reason})
                saved+=1
            cur+=timedelta(days=1)
        st.success(f'Saved allocations for {saved} working days.')

elif page == 'Capacity settings':
    st.title('Capacity settings')
    st.caption('Diminished Capacity converts available hours into effective planning hours: Available Hours × Diminished Capacity Factor.')
    settings = q('''SELECT cs.id, cs.scope_type, cs.scope_id, cs.diminished_capacity_factor, cs.notes,
                    COALESCE(pe.name,t.name,d.code,'Global default') scope_name
                    FROM capacity_settings cs
                    LEFT JOIN people pe ON cs.scope_type='person' AND pe.id=cs.scope_id
                    LEFT JOIN teams t ON cs.scope_type='team' AND t.id=cs.scope_id
                    LEFT JOIN disciplines d ON cs.scope_type='discipline' AND d.id=cs.scope_id
                    ORDER BY CASE cs.scope_type WHEN 'global' THEN 1 WHEN 'discipline' THEN 2 WHEN 'team' THEN 3 ELSE 4 END, scope_name''')
    st.dataframe(settings, use_container_width=True)
    if not can_edit:
        st.warning('PM users are view/export only.')
    scope_type = st.selectbox('Override scope', ['global','discipline','team','person'])
    scope_id = 0 if scope_type == 'global' else None
    if scope_type == 'discipline':
        d = q('SELECT id,code FROM disciplines ORDER BY code')
        selected = st.selectbox('Discipline', d.code)
        scope_id = int(d.loc[d.code == selected, 'id'].iloc[0])
    elif scope_type == 'team':
        t = q('SELECT id,name FROM teams ORDER BY name')
        selected = st.selectbox('Team', t.name)
        scope_id = int(t.loc[t.name == selected, 'id'].iloc[0])
    elif scope_type == 'person':
        p = q('SELECT id,name FROM people WHERE active=1 ORDER BY name')
        selected = st.selectbox('Person', p.name)
        scope_id = int(p.loc[p.name == selected, 'id'].iloc[0])
    factor = st.number_input('Diminished capacity factor', min_value=0.0, max_value=1.0, value=0.85, step=0.01)
    st.info(f'Example: 8.0 available hours × {factor:.2f} = {8.0 * factor:.2f} effective planning hours.')
    notes = st.text_input('Notes')
    if st.button('Save capacity setting', disabled=not can_edit):
        execute('''INSERT INTO capacity_settings(scope_type,scope_id,diminished_capacity_factor,notes) VALUES (?,?,?,?)
                   ON CONFLICT(scope_type,scope_id) DO UPDATE SET diminished_capacity_factor=excluded.diminished_capacity_factor,notes=excluded.notes''',
                (scope_type, scope_id, factor, notes), user=user,
                audit={'object_type':'CapacitySetting','action':'upsert','new':{'scope_type':scope_type,'scope_id':scope_id,'factor':factor},'reason':notes})
        st.success('Capacity setting saved.')

elif page == 'Import / export':
    st.subheader('Imports'); import_type=st.selectbox('Import type', list(TABLE_COLUMNS)); st.caption('Expected columns: '+', '.join(TABLE_COLUMNS[import_type])); file=st.file_uploader('CSV/XLSX file', type=['csv','xlsx','xls'])
    if file and can_edit:
        df=read_upload(file); missing=validate(df, import_type); st.dataframe(df.head(20), use_container_width=True)
        if missing: st.error('Missing required columns: '+', '.join(missing))
        elif st.button('Import validated file'):
            result=import_dataframe(df, import_type, user)
            st.cache_data.clear()
            st.success(f"Imported {result.imported_rows} rows into {result.affected_table}; skipped {result.skipped_rows} rows. Refresh/reload if another browser tab still shows cached data.")
            if result.validation_errors:
                st.subheader('Validation errors / skipped rows')
                st.dataframe(pd.DataFrame(result.validation_errors), use_container_width=True)
    elif file: st.warning('PM users can preview and export only.')
    st.subheader('Workbook structure support')
    wb_file=st.file_uploader('Analyse current capacity workbook (.xlsx)', type=['xlsx'], key='workbook_profile')
    if wb_file:
        profiles=profiles_to_frame(profile_workbook(wb_file))
        st.dataframe(profiles, use_container_width=True)
        st.info('Projects, Roster Daily and Allocation Daily are staged as sources. Teams Breakdown and Capacity are validation targets only.')
    st.subheader('Workbook parity import')
    parity_file=st.file_uploader('Stage capacity workbook for import/reconciliation (.xlsx)', type=['xlsx'], key='workbook_stage')
    if parity_file and can_edit:
        if st.button('Stage workbook'):
            result=stage_workbook(parity_file, parity_file.name, user)
            st.session_state['workbook_batch_id']=result.batch_id
            st.success(f"Staged workbook batch {result.batch_id}: {result.summary}")
            st.subheader('Validation issues')
            st.dataframe(pd.DataFrame(result.validation_issues), use_container_width=True)
            st.subheader('Teams Breakdown reconciliation preview')
            st.dataframe(pd.DataFrame(result.teams_reconciliation), use_container_width=True)
            st.subheader('Capacity reconciliation preview')
            st.dataframe(pd.DataFrame(result.capacity_reconciliation), use_container_width=True)
    elif parity_file:
        st.warning('PM users can preview and export only.')
    batch_id = st.number_input('Workbook batch to commit', min_value=0, value=int(st.session_state.get('workbook_batch_id', 0)))
    if batch_id and st.button('Commit staged workbook', disabled=not can_edit):
        st.success(f'Committed workbook batch {batch_id}: {commit_workbook_batch(int(batch_id), user)}')
    st.subheader('Generated workbook outputs')
    if st.button('Generate Teams Breakdown and Capacity previews'):
        from app.data.db import connect
        with connect() as conn:
            st.dataframe(pd.DataFrame(generated_teams_breakdown(conn)), use_container_width=True)
            st.dataframe(pd.DataFrame(generated_capacity(conn)), use_container_width=True)
    st.subheader('Exports'); export=q('SELECT * FROM projects')
    st.download_button('Download projects CSV', export.to_csv(index=False), 'projects_export.csv')

elif page == 'Admin':
    st.title('Admin')
    if not can_edit:
        st.warning('PM users are view/export only.'); st.stop()
    st.warning('These controls change local data. Use them before loading real production data or after taking a backup.')
    col1, col2 = st.columns(2)
    with col1:
        if st.button('Clear seed/demo data'):
            clear_demo_data(); st.cache_data.clear(); st.success('Seed/demo people, projects, allocations, availability and linked records were cleared.')
    with col2:
        reload_seed=st.checkbox('Reload seed data after reset', value=False)
        if st.button('Reset database'):
            reset_database(reload_seed=reload_seed); st.cache_data.clear(); st.success('Database reset complete.')

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
