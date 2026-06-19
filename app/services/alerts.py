from __future__ import annotations
from datetime import date, timedelta
from app.data.db import rows, connect
from app.services.forecasting import project_forecasts


def generate_alerts(start: str, end: str) -> list[dict]:
    alerts=[]
    for r in rows('''SELECT p.id,p.project_name,SUM(w.demand_hours) demand,COALESCE((SELECT SUM(allocated_hours) FROM daily_allocations da WHERE da.project_id=p.id AND da.allocation_date BETWEEN ? AND ?),0) allocated FROM projects p JOIN weekly_demand w ON w.project_id=p.id WHERE w.week_start BETWEEN ? AND ? GROUP BY p.id''', (start,end,start,end)):
        if r['demand'] and not r['allocated']:
            alerts.append(dict(severity='Warning', alert_type='Demand without allocation', object_type='Project', object_id=r['id'], message=f"{r['project_name']} has planned demand but no allocation."))
    for r in rows('''SELECT p.id,p.name,p.work_date,p.available_hours,COALESCE(SUM(da.allocated_hours),0) allocated FROM (SELECT pe.id,pe.name,ac.work_date,ac.available_hours FROM people pe JOIN availability_calendar ac ON ac.person_id=pe.id WHERE ac.work_date BETWEEN ? AND ?) p LEFT JOIN daily_allocations da ON da.person_id=p.id AND da.allocation_date=p.work_date GROUP BY p.id,p.work_date''', (start,end)):
        if r['available_hours'] > 0 and r['allocated'] == 0:
            alerts.append(dict(severity='Info', alert_type='Available unallocated', object_type='Person', object_id=r['id'], message=f"{r['name']} has available time on {r['work_date']} with no allocation."))
        if r['allocated'] > r['available_hours']:
            alerts.append(dict(severity='Critical', alert_type='Person overallocated', object_type='Person', object_id=r['id'], message=f"{r['name']} is overallocated on {r['work_date']}: {r['allocated']}h / {r['available_hours']}h."))
    for f in project_forecasts(date.today().isoformat()):
        if f['variance_hours'] and f['variance_hours'] > 0:
            alerts.append(dict(severity='Critical', alert_type='Forecast EAC exceeds planned', object_type='Project', object_id=f['project_id'], message=f"{f['project_name']} EAC exceeds planned by {f['variance_hours']:.1f}h."))
        if f['deadline_risk'] == 'High':
            alerts.append(dict(severity='Critical', alert_type='Deadline risk', object_type='Project', object_id=f['project_id'], message=f"{f['project_name']} has insufficient future allocation before deadline."))
        if (f['actual_hours_to_date'] or 0) > (f['planned_hours'] or 0):
            alerts.append(dict(severity='Critical', alert_type='Actual exceeds planned', object_type='Project', object_id=f['project_id'], message=f"{f['project_name']} actual hours exceed planned hours."))
    return alerts


def save_alerts(start: str, end: str) -> list[dict]:
    alerts = generate_alerts(start,end)
    with connect() as conn:
        conn.execute('DELETE FROM alerts WHERE alert_date=?', (date.today().isoformat(),))
        for a in alerts:
            conn.execute('INSERT INTO alerts(alert_date,severity,alert_type,object_type,object_id,message) VALUES (?,?,?,?,?,?)', (date.today().isoformat(), a['severity'], a['alert_type'], a['object_type'], a['object_id'], a['message']))
    return alerts
