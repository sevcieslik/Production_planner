from __future__ import annotations
from datetime import date, timedelta
from app.data.db import rows, connect, write_audit


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
    if mode == 'Manual weekly spread' and manual is not None:
        return manual
    n=len(weeks)
    if mode == 'Front-loaded':
        weights=list(range(n,0,-1))
    elif mode == 'Back-loaded':
        weights=list(range(1,n+1))
    else:
        weights=[1]*n
    s=sum(weights)
    values=[round(total*w/s, 2) for w in weights]
    if values:
        values[-1]=round(values[-1] + total - sum(values), 2)
    return values


def save_weekly_demand(project_id: int, discipline_id: int, weeks: list[date], hours: list[float], user: str, reason: str | None = None) -> None:
    with connect() as conn:
        previous=[dict(r) for r in conn.execute('SELECT week_start,demand_hours FROM weekly_demand WHERE project_id=? AND discipline_id=?', (project_id, discipline_id)).fetchall()]
        for w,h in zip(weeks,hours):
            conn.execute('INSERT OR REPLACE INTO weekly_demand(project_id,discipline_id,week_start,demand_hours,source) VALUES (?,?,?,?,?)', (project_id, discipline_id, w.isoformat(), float(h), 'planning_mode'))
        write_audit(conn,user,'WeeklyDemand',project_id,'upsert',previous,{'discipline_id':discipline_id,'weeks':[w.isoformat() for w in weeks],'hours':hours},reason)


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
      COALESCE((SELECT SUM(da.allocated_hours) FROM daily_allocations da JOIN people p2 ON p2.id=da.person_id JOIN disciplines d2 ON d2.id=p2.discipline_id WHERE d2.code=? AND da.allocation_date BETWEEN ? AND ?),0) allocated
      FROM availability_calendar ac JOIN people p ON p.id=ac.person_id JOIN disciplines d ON d.id=p.discipline_id WHERE d.code=? AND ac.work_date BETWEEN ? AND ?''', (discipline_code,start,end,discipline_code,start,end))[0]
    over=rows('''SELECT COALESCE(SUM(x.over_hours),0) overallocated FROM (SELECT MAX(SUM(da.allocated_hours)-ac.available_hours,0) over_hours FROM availability_calendar ac JOIN people p ON p.id=ac.person_id JOIN disciplines d ON d.id=p.discipline_id LEFT JOIN daily_allocations da ON da.person_id=p.id AND da.allocation_date=ac.work_date WHERE d.code=? AND ac.work_date BETWEEN ? AND ? GROUP BY ac.id) x''', (discipline_code,start,end))[0]['overallocated'] or 0
    effective_available = (r['available'] or 0) * diminished_capacity_factor()
    return {'available': r['available'] or 0, 'effective_available': effective_available, 'allocated': r['allocated'] or 0, 'unallocated': max(effective_available-(r['allocated'] or 0),0), 'overallocated': over}


def diminished_capacity_factor() -> float:
    setting = rows('SELECT diminished_capacity_factor FROM capacity_settings WHERE scope_type="global" ORDER BY id LIMIT 1')
    return float(setting[0]['diminished_capacity_factor']) if setting else 0.85
