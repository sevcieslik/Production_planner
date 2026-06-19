from __future__ import annotations
from datetime import date, datetime
from app.data.db import rows, connect


def project_forecasts(as_of: str | None = None) -> list[dict]:
    as_of = as_of or date.today().isoformat()
    data = rows('''
      SELECT p.id project_id,p.project_name,p.deadline,COALESCE(SUM(b.planned_hours),0) planned_hours,
             op.percent_complete,op.actual_hours_to_date
      FROM projects p
      LEFT JOIN project_discipline_budgets b ON b.project_id=p.id
      LEFT JOIN osr_progress op ON op.project_id=p.id AND op.progress_date=(SELECT MAX(progress_date) FROM osr_progress WHERE project_id=p.id)
      GROUP BY p.id
    ''')
    out=[]
    for r in data:
        pct = r['percent_complete'] or 0; actual = r['actual_hours_to_date'] or 0; planned = r['planned_hours'] or 0
        eac = actual / (pct/100) if pct > 0 else None
        variance = eac - planned if eac is not None else None
        future = rows('SELECT COALESCE(SUM(allocated_hours),0) h FROM daily_allocations WHERE project_id=? AND allocation_date BETWEEN ? AND ?', (r['project_id'], as_of, r['deadline']))[0]['h'] or 0
        remaining = max((eac if eac is not None else planned) - actual, 0)
        risk = 'High' if future < remaining else ('Medium' if variance and variance > 0 else 'Low')
        out.append({**r, 'eac_hours': eac, 'variance_hours': variance, 'future_allocated_hours': future, 'remaining_forecast_hours': remaining, 'deadline_risk': risk})
    return out


def save_forecasts(as_of: str | None = None) -> None:
    as_of = as_of or date.today().isoformat()
    with connect() as conn:
        conn.execute('DELETE FROM forecasts WHERE forecast_date=?', (as_of,))
        for f in project_forecasts(as_of):
            conn.execute('''INSERT INTO forecasts(project_id,forecast_date,planned_hours,actual_hours_to_date,percent_complete,eac_hours,variance_hours,deadline_risk,created_at)
            VALUES (?,?,?,?,?,?,?,?,?)''', (f['project_id'], as_of, f['planned_hours'], f['actual_hours_to_date'] or 0, f['percent_complete'] or 0, f['eac_hours'], f['variance_hours'], f['deadline_risk'], datetime.utcnow().isoformat(timespec='seconds')))
