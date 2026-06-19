from __future__ import annotations
from datetime import date, timedelta
from app.data.db import rows, connect, write_audit

LOADING_METHODS = ['Even spread', 'Front-loaded', 'Back-loaded', 'Manual weekly spread']
DISCIPLINES = ['RS', 'GIS', 'PLS']


def monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def week_starts(start: date, end: date) -> list[date]:
    cur = monday(start); out=[]
    while cur <= end:
        out.append(cur); cur += timedelta(days=7)
    return out


def spread_hours(total: float, weeks: list[date], mode: str, manual: list[float] | None = None) -> list[float]:
    if not weeks:
        return []
    if mode in ('Manual weekly spread', 'manual') and manual is not None:
        return [round(float(v), 2) for v in manual]
    n=len(weeks)
    if mode in ('Front-loaded', 'front_loaded'):
        weights=list(range(n,0,-1))
    elif mode in ('Back-loaded', 'back_loaded'):
        weights=list(range(1,n+1))
    else:
        weights=[1]*n
    s=sum(weights)
    values=[round(float(total)*w/s, 2) for w in weights]
    if values:
        values[-1]=round(values[-1] + float(total) - sum(values), 2)
    return values


def diminished_capacity_factor() -> float:
    setting = rows('SELECT diminished_capacity_factor FROM capacity_settings WHERE scope_type="global" ORDER BY id LIMIT 1')
    return float(setting[0]['diminished_capacity_factor']) if setting else 0.85


def effective_capacity_by_week(discipline_id: int, weeks: list[date]) -> dict[str, float]:
    if not weeks:
        return {}
    start, end = weeks[0].isoformat(), (weeks[-1] + timedelta(days=6)).isoformat()
    data = rows('''SELECT date(ac.work_date, '-' || ((strftime('%w', ac.work_date)+6) % 7) || ' days') week_start,
             COALESCE(SUM(ac.available_hours),0) available_hours
             FROM availability_calendar ac JOIN people p ON p.id=ac.person_id
             WHERE p.active=1 AND p.discipline_id=? AND ac.work_date BETWEEN ? AND ?
             GROUP BY week_start''', (discipline_id, start, end))
    factor = diminished_capacity_factor()
    found = {r['week_start']: round((r['available_hours'] or 0) * factor, 2) for r in data}
    return {w.isoformat(): found.get(w.isoformat(), 0.0) for w in weeks}


def existing_weekly_demand(discipline_id: int, weeks: list[date], exclude_project_id: int | None = None) -> dict[str, float]:
    if not weeks:
        return {}
    params: list = [discipline_id, weeks[0].isoformat(), weeks[-1].isoformat()]
    project_filter = ''
    if exclude_project_id is not None:
        project_filter = 'AND project_id<>?'
        params.append(exclude_project_id)
    data = rows(f'''SELECT week_start, COALESCE(SUM(demand_hours),0) demand_hours FROM weekly_demand
                    WHERE discipline_id=? AND week_start BETWEEN ? AND ? {project_filter} GROUP BY week_start''', params)
    found = {r['week_start']: float(r['demand_hours'] or 0) for r in data}
    return {w.isoformat(): found.get(w.isoformat(), 0.0) for w in weeks}


def manual_loading_difference(total: float, weekly_hours: list[float]) -> float:
    return round(float(total) - sum(float(v) for v in weekly_hours), 2)


def capacity_status(utilisation: float | None, effective_capacity: float) -> str:
    if effective_capacity <= 0:
        return 'grey'
    if utilisation is None:
        return 'grey'
    if utilisation > 1:
        return 'red'
    if utilisation >= 0.85:
        return 'amber'
    return 'green'


def build_loading_preview(project_id: int | None, discipline_id: int, weeks: list[date], proposed_hours: list[float]) -> list[dict]:
    caps = effective_capacity_by_week(discipline_id, weeks)
    existing = existing_weekly_demand(discipline_id, weeks, project_id)
    preview=[]
    for w, proposed in zip(weeks, proposed_hours):
        key=w.isoformat(); cap=caps.get(key, 0.0); old=existing.get(key, 0.0); total=round(old + float(proposed), 2)
        surplus=round(cap-total, 2); util=round(total/cap, 4) if cap > 0 else None
        preview.append({'week_start': key, 'available_effective_capacity': cap, 'already_allocated_project_demand': old,
                        'new_proposed_demand': round(float(proposed), 2), 'total_demand_after_loading': total,
                        'surplus_or_shortage': surplus, 'utilisation_pct': round(util*100, 1) if util is not None else None,
                        'status': capacity_status(util, cap)})
    return preview


def fit_hours_to_capacity(total: float, weeks: list[date], discipline_id: int, project_id: int | None = None) -> tuple[list[float], float, date | None]:
    caps = effective_capacity_by_week(discipline_id, weeks)
    existing = existing_weekly_demand(discipline_id, weeks, project_id)
    remaining = float(total); allocation=[]
    for w in weeks:
        room=max(caps[w.isoformat()] - existing[w.isoformat()], 0.0)
        h=round(min(room, remaining), 2); allocation.append(h); remaining=round(remaining-h, 2)
    completion = weeks[-1] if remaining <= 0 and weeks else None
    if remaining > 0 and weeks:
        cur = weeks[-1] + timedelta(days=7); guard=0
        while remaining > 0 and guard < 260:
            ext = effective_capacity_by_week(discipline_id, [cur]).get(cur.isoformat(), 0.0)
            old = existing_weekly_demand(discipline_id, [cur], project_id).get(cur.isoformat(), 0.0)
            room=max(ext-old, 0.0)
            if room > 0:
                remaining=round(remaining-min(room, remaining), 2)
                completion=cur
            cur += timedelta(days=7); guard += 1
    return allocation, remaining, completion


def save_weekly_demand(project_id: int, discipline_id: int, weeks: list[date], hours: list[float], user: str, reason: str | None = None,
                       loading_profile_id: int | None = None) -> None:
    with connect() as conn:
        previous=[dict(r) for r in conn.execute('SELECT week_start,demand_hours FROM weekly_demand WHERE project_id=? AND discipline_id=?', (project_id, discipline_id)).fetchall()]
        for w,h in zip(weeks,hours):
            conn.execute('INSERT OR REPLACE INTO weekly_demand(project_id,discipline_id,week_start,demand_hours,source) VALUES (?,?,?,?,?)', (project_id, discipline_id, w.isoformat(), float(h), 'planning_wizard' if loading_profile_id else 'planning_mode'))
            conn.execute('''INSERT OR REPLACE INTO weekly_project_demand(project_id,discipline_id,week_start,demand_hours,loading_profile_id,source)
                            VALUES (?,?,?,?,?,?)''', (project_id, discipline_id, w.isoformat(), float(h), loading_profile_id, 'planning_wizard' if loading_profile_id else 'planning_mode'))
        write_audit(conn,user,'WeeklyDemand',project_id,'upsert',previous,{'discipline_id':discipline_id,'weeks':[w.isoformat() for w in weeks],'hours':hours},reason)


def save_loading_profile(project_id: int, discipline_id: int, start_date: date, end_date: date, total_hours: float, method: str,
                         fit_to_capacity: bool, user: str, reason: str | None = None) -> int:
    with connect() as conn:
        cur=conn.execute('''INSERT INTO project_loading_profiles(project_id,discipline_id,start_date,end_date,total_planned_hours,loading_method,fit_to_capacity,created_by,created_at)
                            VALUES (?,?,?,?,?,?,?,?,datetime('now'))''', (project_id,discipline_id,start_date.isoformat(),end_date.isoformat(),float(total_hours),method,int(fit_to_capacity),user))
        conn.execute('''INSERT INTO project_discipline_dates(project_id,discipline_id,start_date,end_date) VALUES (?,?,?,?)
                        ON CONFLICT(project_id,discipline_id) DO UPDATE SET start_date=excluded.start_date,end_date=excluded.end_date''',
                     (project_id,discipline_id,start_date.isoformat(),end_date.isoformat()))
        write_audit(conn,user,'ProjectLoadingProfile',cur.lastrowid,'insert',None,{'project_id':project_id,'discipline_id':discipline_id,'total':total_hours,'method':method},reason)
        return int(cur.lastrowid)


def overall_capacity_summary(start: str, end: str) -> dict:
    factor = diminished_capacity_factor()
    cap = rows('''SELECT d.code, COALESCE(SUM(ac.available_hours),0) available FROM disciplines d
                  LEFT JOIN people p ON p.discipline_id=d.id AND p.active=1
                  LEFT JOIN availability_calendar ac ON ac.person_id=p.id AND ac.work_date BETWEEN ? AND ?
                  GROUP BY d.code''', (start, end))
    demand = rows('''SELECT d.code, COALESCE(SUM(wd.demand_hours),0) demand FROM disciplines d
                     LEFT JOIN weekly_demand wd ON wd.discipline_id=d.id AND wd.week_start BETWEEN ? AND ? GROUP BY d.code''', (start, end))
    out = {r['code']: {'capacity': round((r['available'] or 0)*factor, 2), 'allocated': 0.0, 'shortage': 0.0} for r in cap}
    for r in demand:
        code=r['code']; out.setdefault(code, {'capacity':0.0,'allocated':0.0,'shortage':0.0}); out[code]['allocated']=round(r['demand'] or 0, 2); out[code]['shortage']=round(max(out[code]['allocated']-out[code]['capacity'],0),2)
    return out


def gap_analysis(start: str, end: str, discipline_code: str | None = None) -> dict[str, list[dict]]:
    disc_filter = 'AND d.code=?' if discipline_code else ''
    params = [start,end] + ([discipline_code] if discipline_code else [])
    demand = rows(f'''SELECT wd.week_start,d.code discipline,p.id project_id,p.project_name,SUM(wd.demand_hours) demand_hours,
      COALESCE((SELECT SUM(da.allocated_hours) FROM daily_allocations da JOIN people pe ON pe.id=da.person_id WHERE da.project_id=p.id AND pe.discipline_id=d.id AND da.allocation_date BETWEEN wd.week_start AND date(wd.week_start,'+6 days')),0) allocated_hours
      FROM weekly_demand wd JOIN projects p ON p.id=wd.project_id JOIN disciplines d ON d.id=wd.discipline_id
      WHERE wd.week_start BETWEEN ? AND ? {disc_filter} GROUP BY wd.week_start,d.code,p.id ORDER BY wd.week_start,d.code,p.project_name''', params)
    for r in demand:
        r['unfilled_hours'] = round((r['demand_hours'] or 0) - (r['allocated_hours'] or 0), 2)
    people = rows(f'''SELECT substr(ac.work_date,1,10) work_date,d.code discipline,pe.id person_id,pe.name,ac.available_hours,COALESCE(SUM(da.allocated_hours),0) allocated_hours
      FROM availability_calendar ac JOIN people pe ON pe.id=ac.person_id JOIN disciplines d ON d.id=pe.discipline_id
      LEFT JOIN daily_allocations da ON da.person_id=pe.id AND da.allocation_date=ac.work_date
      WHERE ac.work_date BETWEEN ? AND ? {disc_filter} GROUP BY ac.id ORDER BY ac.work_date,d.code,pe.name''', params)
    unassigned=[]; over=[]
    for r in people:
        r['effective_capacity_hours'] = round((r['available_hours'] or 0) * diminished_capacity_factor(), 2)
        if r['available_hours'] > 0 and r['allocated_hours'] == 0:
            unassigned.append(r)
        if r['allocated_hours'] > r['effective_capacity_hours']:
            over.append(r)
    return {'demand': demand, 'unassigned_people': unassigned, 'overallocated_people': over}


def discipline_metrics(discipline_code: str, start: str, end: str) -> dict:
    r=rows('''SELECT COALESCE(SUM(ac.available_hours),0) available,
      COALESCE((SELECT SUM(da.allocated_hours) FROM daily_allocations da JOIN people p2 ON p2.id=da.person_id JOIN disciplines d2 ON d2.id=p2.discipline_id WHERE d2.code=? AND da.allocation_date BETWEEN ? AND ?),0) allocated,
      COALESCE((SELECT SUM(wd.demand_hours) FROM weekly_demand wd JOIN disciplines d3 ON d3.id=wd.discipline_id WHERE d3.code=? AND wd.week_start BETWEEN ? AND ?),0) planned
      FROM availability_calendar ac JOIN people p ON p.id=ac.person_id JOIN disciplines d ON d.id=p.discipline_id WHERE d.code=? AND ac.work_date BETWEEN ? AND ?''', (discipline_code,start,end,discipline_code,start,end,discipline_code,start,end))[0]
    over=rows('''SELECT COALESCE(SUM(x.over_hours),0) overallocated FROM (SELECT MAX(SUM(da.allocated_hours)-ac.available_hours,0) over_hours FROM availability_calendar ac JOIN people p ON p.id=ac.person_id JOIN disciplines d ON d.id=p.discipline_id LEFT JOIN daily_allocations da ON da.person_id=p.id AND da.allocation_date=ac.work_date WHERE d.code=? AND ac.work_date BETWEEN ? AND ? GROUP BY ac.id) x''', (discipline_code,start,end))[0]['overallocated'] or 0
    effective_available = (r['available'] or 0) * diminished_capacity_factor()
    return {'available': r['available'] or 0, 'effective_available': effective_available, 'planned': r['planned'] or 0, 'allocated': r['allocated'] or 0, 'unallocated': max(effective_available-(r['allocated'] or 0),0), 'overallocated': over}
