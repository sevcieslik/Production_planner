from __future__ import annotations
from datetime import date, timedelta
import pandas as pd
import streamlit as st
from app.data.db import initialize_database, rows, execute, clear_demo_data, reset_database
from app.importers.csv_xlsx import TABLE_COLUMNS, read_upload, validate, import_dataframe
from app.importers.workbook import profile_workbook, profiles_to_frame, stage_workbook, commit_workbook_batch, generated_teams_breakdown, generated_capacity
from app.services.alerts import save_alerts
from app.services.forecasting import project_forecasts, save_forecasts
from app.services.planning import week_starts, spread_hours, save_weekly_demand, gap_analysis, discipline_metrics, build_loading_preview, fit_hours_to_capacity, manual_loading_difference, save_loading_profile, overall_capacity_summary

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
page = st.sidebar.radio('View', ['Manager dashboard','Project loading wizard','Planning mode','Allocation mode','Gap analysis','Dashboard','Overall capacity view','Timeline view','Team capacity','Project view','People allocation','Allocation editor','Capacity settings','Import / export','Admin','Audit log','Architecture & roadmap'])
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

elif page == 'Project loading wizard':
    st.title('Project loading wizard')
    st.caption('Preferred workflow: preview capacity-aware weekly loading before saving.')
    if not can_edit: st.warning('PM users are view/export only.')
    projects=q('SELECT id,project_name,client,start_date,end_date,deadline,status FROM projects ORDER BY project_name')
    disciplines=q('SELECT id,code FROM disciplines ORDER BY code')
    create_new = st.checkbox('Create a new project')
    with st.form('loading_wizard'):
        if create_new:
            new_name=st.text_input('Project name'); new_client=st.text_input('Client', value='TBC'); project_name=None
        else:
            project_name=st.selectbox('Project', projects.project_name.tolist()) if not projects.empty else None; new_name=''; new_client=''
        disc_code=st.selectbox('Discipline', disciplines.code.tolist())
        ds=st.date_input('Discipline loading start', start, key='lw_start'); de=st.date_input('Discipline loading end', end, key='lw_end')
        total=st.number_input('Total planned hours', min_value=0.0, step=7.5, value=37.5)
        mode=st.radio('Loading method', ['Even spread','Front-loaded','Back-loaded','Manual weekly spread'], horizontal=True)
        fit=st.checkbox('Fit to available capacity')
        weeks=week_starts(ds,de); manual=[]
        if mode == 'Manual weekly spread':
            for w in weeks:
                manual.append(st.number_input(f'Week {w.isoformat()}', min_value=0.0, step=1.0, key=f'lw_manual_{w}'))
            st.info(f'Manual total variance: {manual_loading_difference(total, manual):+.1f}h.')
        reason=st.text_input('Reason/comment', key='lw_reason')
        preview_clicked=st.form_submit_button('Preview loading', disabled=not can_edit)
    if preview_clicked:
        st.session_state['loading_wizard']={'create':create_new,'new_name':new_name,'new_client':new_client,'project_name':project_name,'disc_code':disc_code,'ds':ds,'de':de,'total':total,'mode':mode,'fit':fit,'manual':manual,'reason':reason}
    cfg=st.session_state.get('loading_wizard')
    if cfg:
        pid = None if cfg['create'] else int(projects.loc[projects.project_name==cfg['project_name'],'id'].iloc[0])
        did = int(disciplines.loc[disciplines.code==cfg['disc_code'],'id'].iloc[0])
        weeks=week_starts(cfg['ds'], cfg['de'])
        proposed = spread_hours(cfg['total'], weeks, cfg['mode'], cfg['manual'] if cfg['mode']=='Manual weekly spread' else None)
        remaining=0.0; completion=None
        if cfg['fit']:
            proposed, remaining, completion = fit_hours_to_capacity(cfg['total'], weeks, did, pid)
        preview_df=pd.DataFrame(build_loading_preview(pid, did, weeks, proposed))
        def color_status(row):
            colors={'green':'background-color:#d8f3dc','amber':'background-color:#fff3bf','red':'background-color:#ffc9c9','grey':'background-color:#e9ecef'}
            return [colors.get(row.get('status'), '') for _ in row]
        st.subheader('Capacity-aware preview')
        st.dataframe(preview_df.style.apply(color_status, axis=1), use_container_width=True)
        if cfg['fit'] and remaining > 0:
            st.error(f"Capacity fit cannot deliver within selected dates. Remaining unallocated hours: {remaining:.1f}h. First possible completion week: {completion.isoformat() if completion else 'not found'}.")
        if st.button('Confirm and save weekly demand', disabled=not can_edit or (cfg['mode']=='Manual weekly spread' and abs(manual_loading_difference(cfg['total'], proposed))>=0.01)):
            if cfg['create']:
                pid=execute("INSERT INTO projects(project_name,client,start_date,end_date,deadline,status,notes,imported_at) VALUES (?,?,?,?,?,?,?,datetime('now'))", (cfg['new_name'],cfg['new_client'],str(cfg['ds']),str(cfg['de']),str(cfg['de']),'Active','Created in project loading wizard'), user=user, audit={'object_type':'Project','action':'insert','new':{'project_name':cfg['new_name']},'reason':cfg['reason']})
            profile_id=save_loading_profile(int(pid), did, cfg['ds'], cfg['de'], cfg['total'], cfg['mode'], cfg['fit'], user, cfg['reason'])
            save_weekly_demand(int(pid), did, weeks, proposed, user, cfg['reason'], profile_id)
            st.cache_data.clear(); st.success('Project loading profile and weekly demand saved.')

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
    disc_filter=st.selectbox('Dashboard discipline filter', ['All','RS','GIS','PLS'])
    status_filter=st.selectbox('Project status filter', ['Active','Archived','All'])
    c = q('''SELECT COALESCE(SUM(ac.available_hours),0) available,
             COALESCE((SELECT SUM(da.allocated_hours) FROM daily_allocations da JOIN people p2 ON p2.id=da.person_id JOIN disciplines d2 ON d2.id=p2.discipline_id WHERE da.allocation_date BETWEEN ? AND ? AND (?='All' OR d2.code=?)),0) allocated
             FROM availability_calendar ac JOIN people p ON p.id=ac.person_id JOIN disciplines d ON d.id=p.discipline_id
             WHERE ac.work_date BETWEEN ? AND ? AND (?='All' OR d.code=?)''', (str(start),str(end),disc_filter,disc_filter,str(start),str(end),disc_filter,disc_filter))
    factor_rows=q('SELECT diminished_capacity_factor FROM capacity_settings WHERE scope_type="global" ORDER BY id LIMIT 1')
    factor=float(factor_rows.diminished_capacity_factor.iloc[0]) if not factor_rows.empty else 0.85
    available=float(c.available.iloc[0]); allocated=float(c.allocated.iloc[0]); effective=available*factor
    planned=float(q('''SELECT COALESCE(SUM(wd.demand_hours),0) planned FROM weekly_demand wd JOIN disciplines d ON d.id=wd.discipline_id JOIN projects p ON p.id=wd.project_id WHERE wd.week_start BETWEEN ? AND ? AND (?='All' OR d.code=?) AND (?='All' OR p.status=?)''',(str(start),str(end),disc_filter,disc_filter,status_filter,status_filter)).planned.iloc[0])
    overcap=max(planned-effective,0); unallocated=max(effective-planned,0)
    risk_df=q('''SELECT p.id, SUM(wd.demand_hours) demand FROM projects p JOIN weekly_demand wd ON wd.project_id=p.id JOIN disciplines d ON d.id=wd.discipline_id WHERE wd.week_start BETWEEN ? AND ? AND (?='All' OR d.code=?) AND (?='All' OR p.status=?) GROUP BY p.id HAVING demand > 0''',(str(start),str(end),disc_filter,disc_filter,status_filter,status_filter))
    cols=st.columns(6); cols[0].metric('Effective capacity', f'{effective:.1f}h'); cols[1].metric('Planned demand', f'{planned:.1f}h'); cols[2].metric('Allocated hours', f'{allocated:.1f}h'); cols[3].metric('Unallocated capacity', f'{unallocated:.1f}h'); cols[4].metric('Overcapacity hours', f'{overcap:.1f}h'); cols[5].metric('Projects at risk', len(risk_df))
    st.subheader('Key alerts'); st.dataframe(pd.DataFrame(alerts), use_container_width=True)

elif page == 'Overall capacity view':
    st.title('Overall capacity view')
    weeks=week_starts(start,end); week_cols=[w.isoformat() for w in weeks]
    base=q("""SELECT p.id,p.source_reference_id project_code,p.project_name,COALESCE(MAX(op.percent_complete),0) percent_complete,
              COALESCE((SELECT SUM(allocated_hours) FROM daily_allocations da WHERE da.project_id=p.id AND da.allocation_date BETWEEN ? AND ?),0) hrs_assigned,
              COALESCE((SELECT SUM(planned_hours) FROM project_discipline_budgets b WHERE b.project_id=p.id),0) planned_hrs
              FROM projects p LEFT JOIN osr_progress op ON op.project_id=p.id GROUP BY p.id ORDER BY p.project_name""",(str(start),str(end)))
    demand=q('SELECT p.id project_id, wd.week_start, SUM(wd.demand_hours) hours FROM weekly_demand wd JOIN projects p ON p.id=wd.project_id WHERE wd.week_start BETWEEN ? AND ? GROUP BY p.id, wd.week_start',(week_cols[0] if week_cols else str(start), week_cols[-1] if week_cols else str(end)))
    if not base.empty:
        pivot=demand.pivot_table(index='project_id', columns='week_start', values='hours', aggfunc='sum').fillna(0) if not demand.empty else pd.DataFrame(index=base.id)
        table=base.set_index('id').join(pivot, how='left').fillna(0).reset_index(drop=True)
        table['HRS remaining']=table['planned_hrs']-table['hrs_assigned']; table['% effort spent']=(table['hrs_assigned']/table['planned_hrs'].replace(0, pd.NA)*100).fillna(0).round(1)
        table=table.rename(columns={'project_code':'Project code','project_name':'Project name','percent_complete':'% complete','hrs_assigned':'HRS assigned','planned_hrs':'Planned HRS'})
        st.dataframe(table, use_container_width=True)
    summary=overall_capacity_summary(str(start), str(end))
    st.subheader('Summary rows')
    st.dataframe(pd.DataFrame([{'row':'CAPACITY', **{k:v['capacity'] for k,v in summary.items()}},{'row':'ALLOCATED', **{k:v['allocated'] for k,v in summary.items()}},{'row':'RS Shortage','RS':summary.get('RS',{}).get('shortage',0)},{'row':'GIS Shortage','GIS':summary.get('GIS',{}).get('shortage',0)},{'row':'PLS Shortage','PLS':summary.get('PLS',{}).get('shortage',0)}]), use_container_width=True)

elif page == 'Timeline view':
    st.title('Project timeline / Gantt view')
    df=q("""SELECT p.project_name,d.code discipline,COALESCE(pdd.start_date,p.start_date) start_date,COALESCE(pdd.end_date,p.end_date) end_date,p.status
            FROM projects p CROSS JOIN disciplines d LEFT JOIN project_discipline_dates pdd ON pdd.project_id=p.id AND pdd.discipline_id=d.id
            WHERE p.status!='Archived' ORDER BY p.project_name,d.code""")
    if not df.empty:
        st.caption(f"Today marker: {date.today().isoformat()}")
        st.bar_chart(df.assign(duration=(pd.to_datetime(df.end_date)-pd.to_datetime(df.start_date)).dt.days.clip(lower=1)).pivot_table(index='project_name', columns='discipline', values='duration', aggfunc='sum').fillna(0))
        st.dataframe(df, use_container_width=True)

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
