from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from typing import Iterable

import pandas as pd

from app.data.db import connect, rows
from app.services.planning import spread_hours, week_starts, capacity_status

DISCIPLINES = ["RS", "GIS", "PLS"]
LOADING_TYPES = ["even", "front_loaded", "back_loaded", "manual"]
RESOURCE_STATUSES = ["active", "suspended", "maternity", "secondment", "out_of_business", "left_business"]
PROJECT_FIELDS = [
    "project_code", "project_name", "row_km", "cct_km", "spus", "rs_hours", "gis_hours", "pls_hours",
    "start_date", "end_date", "loading_type", "rs_start_date", "gis_start_date", "pls_start_date", "status",
]
RESOURCE_FIELDS = [
    "person_name", "department", "weekly_hours", "holiday_booked_hours", "holiday_remaining_hours",
    "active_status", "status_reason", "status_start_date", "status_end_date",
]

PROJECT_DATE_COLUMNS = ["start_date", "end_date", "rs_start_date", "gis_start_date", "pls_start_date"]
RESOURCE_DATE_COLUMNS = ["status_start_date", "status_end_date", "department_change_start_date", "department_change_end_date"]


def ensure_mvp_schema() -> None:
    with connect() as conn:
        conn.executescript('''
        CREATE TABLE IF NOT EXISTS mvp_projects (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          project_code TEXT NOT NULL UNIQUE,
          project_name TEXT NOT NULL,
          row_km REAL NOT NULL DEFAULT 0,
          cct_km REAL NOT NULL DEFAULT 0,
          spus REAL NOT NULL DEFAULT 0,
          rs_hours REAL NOT NULL DEFAULT 0,
          gis_hours REAL NOT NULL DEFAULT 0,
          pls_hours REAL NOT NULL DEFAULT 0,
          start_date TEXT NOT NULL,
          end_date TEXT NOT NULL,
          loading_type TEXT NOT NULL DEFAULT 'even',
          rs_start_date TEXT,
          gis_start_date TEXT,
          pls_start_date TEXT,
          status TEXT NOT NULL DEFAULT 'active',
          archived INTEGER NOT NULL DEFAULT 0,
          updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS mvp_resources (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          person_name TEXT NOT NULL UNIQUE,
          department TEXT NOT NULL CHECK(department IN ('RS','GIS','PLS')),
          weekly_hours REAL NOT NULL DEFAULT 37.5,
          holiday_booked_hours REAL NOT NULL DEFAULT 0,
          holiday_remaining_hours REAL NOT NULL DEFAULT 0,
          active_status TEXT NOT NULL DEFAULT 'active',
          status_reason TEXT,
          status_start_date TEXT,
          status_end_date TEXT,
          updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS resource_department_assignments (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          resource_id INTEGER NOT NULL REFERENCES mvp_resources(id) ON DELETE CASCADE,
          department TEXT NOT NULL CHECK(department IN ('RS','GIS','PLS')),
          start_date TEXT NOT NULL,
          end_date TEXT,
          UNIQUE(resource_id, department, start_date, end_date)
        );
        CREATE TABLE IF NOT EXISTS resource_status_periods (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          resource_id INTEGER NOT NULL REFERENCES mvp_resources(id) ON DELETE CASCADE,
          active_status TEXT NOT NULL,
          status_reason TEXT,
          start_date TEXT NOT NULL,
          end_date TEXT
        );
        CREATE TABLE IF NOT EXISTS holidays (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          resource_id INTEGER REFERENCES mvp_resources(id) ON DELETE CASCADE,
          person_name TEXT,
          holiday_date TEXT NOT NULL,
          hours REAL NOT NULL DEFAULT 0,
          source TEXT DEFAULT 'manual',
          UNIQUE(person_name, holiday_date, source)
        );
        CREATE TABLE IF NOT EXISTS settings (
          key TEXT PRIMARY KEY,
          value TEXT NOT NULL
        );
        INSERT OR IGNORE INTO settings(key,value) VALUES ('diminished_capacity_factor','0.85');
        ''')


def prepare_date_columns_for_editor(df: pd.DataFrame, date_columns: list[str]) -> pd.DataFrame:
    """Return a copy with Streamlit DateColumn-compatible datetime64 columns."""
    prepared = df.copy()
    for column in date_columns:
        if column in prepared.columns:
            prepared[column] = pd.to_datetime(prepared[column], errors='coerce')
    return prepared


def normalise_date_for_db(value, default: date | None = None) -> str | None:
    """Parse editor/CSV date values and return ISO YYYY-MM-DD strings for SQLite."""
    if value is None or value is pd.NA:
        return default.isoformat() if default else None
    if isinstance(value, str) and not value.strip():
        return default.isoformat() if default else None
    parsed = pd.to_datetime(value, errors='coerce', dayfirst=False)
    if pd.isna(parsed):
        parsed = pd.to_datetime(value, errors='coerce', dayfirst=True)
    if pd.isna(parsed):
        return default.isoformat() if default else None
    return parsed.date().isoformat()


def _date(value, default: date) -> str:
    parsed = normalise_date_for_db(value, default)
    return parsed if parsed is not None else default.isoformat()


def normalize_loading_type(value) -> str:
    v = str(value or 'even').strip().lower().replace('-', '_').replace(' ', '_')
    aliases = {'even_spread': 'even', 'front_loaded': 'front_loaded', 'back_loaded': 'back_loaded', 'manual_weekly_spread': 'manual'}
    return aliases.get(v, v if v in LOADING_TYPES else 'even')


def load_projects_csv(path: str | Path = 'sample-data/projects.csv') -> pd.DataFrame:
    df = pd.read_csv(path)
    today = date.today()
    out = pd.DataFrame()
    out['project_code'] = df.get('project_code', df.get('Project Code', '')).astype(str).str.strip()
    out['project_name'] = df.get('project_name', df.get('Project Name', '')).astype(str).str.strip()
    out['row_km'] = pd.to_numeric(df.get('row_km', df.get('ROW (km)', 0)), errors='coerce').fillna(0)
    out['cct_km'] = pd.to_numeric(df.get('cct_km', df.get('Circuit Length (km)', 0)), errors='coerce').fillna(0)
    out['spus'] = pd.to_numeric(df.get('spus', df.get('Total SPUs', 0)), errors='coerce').fillna(0)
    out['rs_hours'] = pd.to_numeric(df.get('rs_hours', df.get('RS Total', 0)), errors='coerce').fillna(0)
    out['gis_hours'] = pd.to_numeric(df.get('gis_hours', df.get('GIS Total', 0)), errors='coerce').fillna(0)
    out['pls_hours'] = pd.to_numeric(df.get('pls_hours', df.get('PLS Total', 0)), errors='coerce').fillna(0)
    out['start_date'] = df.get('start_date', df.get('Production Start date', today)).apply(lambda v: _date(v, today))
    out['end_date'] = df.get('end_date', df.get('Production Estimated Completion Date', today + timedelta(days=28))).apply(lambda v: _date(v, today + timedelta(days=28)))
    out['loading_type'] = df.get('loading_type', 'even')
    out['loading_type'] = out['loading_type'].apply(normalize_loading_type)
    out['rs_start_date'] = df.get('rs_start_date', df.get('RS (Luke)', out['start_date'])).apply(lambda v: _date(v, today))
    out['gis_start_date'] = df.get('gis_start_date', df.get('GIS (Dom)', out['start_date'])).apply(lambda v: _date(v, today))
    out['pls_start_date'] = df.get('pls_start_date', df.get('PLS (Carlos)', out['start_date'])).apply(lambda v: _date(v, today))
    out['status'] = df.get('status', 'active')
    out['status'] = out['status'].fillna('active').astype(str).str.lower().replace({'archived': 'archived'})
    return out[PROJECT_FIELDS]


def save_projects(records: Iterable[dict]) -> None:
    ensure_mvp_schema()
    with connect() as conn:
        keep = []
        for r in records:
            code = str(r.get('project_code') or '').strip(); name = str(r.get('project_name') or '').strip()
            if not code or not name: continue
            vals = {k: r.get(k) for k in PROJECT_FIELDS}
            vals['loading_type'] = normalize_loading_type(vals.get('loading_type'))
            vals['status'] = str(vals.get('status') or 'active').lower()
            archived = 1 if vals['status'] == 'archived' else 0
            conn.execute('''INSERT INTO mvp_projects(project_code,project_name,row_km,cct_km,spus,rs_hours,gis_hours,pls_hours,start_date,end_date,loading_type,rs_start_date,gis_start_date,pls_start_date,status,archived,updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now')) ON CONFLICT(project_code) DO UPDATE SET project_name=excluded.project_name,row_km=excluded.row_km,cct_km=excluded.cct_km,spus=excluded.spus,rs_hours=excluded.rs_hours,gis_hours=excluded.gis_hours,pls_hours=excluded.pls_hours,start_date=excluded.start_date,end_date=excluded.end_date,loading_type=excluded.loading_type,rs_start_date=excluded.rs_start_date,gis_start_date=excluded.gis_start_date,pls_start_date=excluded.pls_start_date,status=excluded.status,archived=excluded.archived,updated_at=datetime('now')''',
            (code,name,float(vals.get('row_km') or 0),float(vals.get('cct_km') or 0),float(vals.get('spus') or 0),float(vals.get('rs_hours') or 0),float(vals.get('gis_hours') or 0),float(vals.get('pls_hours') or 0),normalise_date_for_db(vals.get('start_date'), date.today()),normalise_date_for_db(vals.get('end_date'), date.today() + timedelta(days=28)),vals['loading_type'],normalise_date_for_db(vals.get('rs_start_date'), date.today()),normalise_date_for_db(vals.get('gis_start_date'), date.today()),normalise_date_for_db(vals.get('pls_start_date'), date.today()),vals['status'],archived))
            keep.append(code)


def import_default_projects() -> int:
    df = load_projects_csv()
    save_projects(df.to_dict('records'))
    return len(df)


def get_projects(include_archived=True) -> pd.DataFrame:
    ensure_mvp_schema(); where = '' if include_archived else 'WHERE archived=0'
    return pd.DataFrame(rows(f'SELECT {",".join(PROJECT_FIELDS)}, archived FROM mvp_projects {where} ORDER BY project_code'))


def save_resources(records: Iterable[dict]) -> None:
    ensure_mvp_schema()
    with connect() as conn:
        for r in records:
            name = str(r.get('person_name') or '').strip(); dept = str(r.get('department') or 'RS').strip().upper()
            if not name or dept not in DISCIPLINES: continue
            status = str(r.get('active_status') or 'active').lower()
            conn.execute('''INSERT INTO mvp_resources(person_name,department,weekly_hours,holiday_booked_hours,holiday_remaining_hours,active_status,status_reason,status_start_date,status_end_date,updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,datetime('now')) ON CONFLICT(person_name) DO UPDATE SET department=excluded.department,weekly_hours=excluded.weekly_hours,holiday_booked_hours=excluded.holiday_booked_hours,holiday_remaining_hours=excluded.holiday_remaining_hours,active_status=excluded.active_status,status_reason=excluded.status_reason,status_start_date=excluded.status_start_date,status_end_date=excluded.status_end_date,updated_at=datetime('now')''',
            (name,dept,float(r.get('weekly_hours') or 0),float(r.get('holiday_booked_hours') or 0),float(r.get('holiday_remaining_hours') or 0),status,r.get('status_reason'),normalise_date_for_db(r.get('status_start_date')),normalise_date_for_db(r.get('status_end_date'))))


def get_resources() -> pd.DataFrame:
    ensure_mvp_schema(); return pd.DataFrame(rows(f'SELECT {",".join(RESOURCE_FIELDS)} FROM mvp_resources ORDER BY department, person_name'))


def seed_resources_from_people() -> int:
    ensure_mvp_schema(); existing = rows('SELECT COUNT(*) c FROM mvp_resources')[0]['c']
    if existing: return 0
    people = rows('SELECT p.name person_name,d.code department,p.weekly_hours FROM people p JOIN disciplines d ON d.id=p.discipline_id ORDER BY p.name')
    save_resources([{**p, 'holiday_booked_hours': 0, 'holiday_remaining_hours': 0, 'active_status': 'active'} for p in people])
    return len(people)


def setting_float(key: str, default: float) -> float:
    ensure_mvp_schema(); r = rows('SELECT value FROM settings WHERE key=?', (key,)); return float(r[0]['value']) if r else default


def department_for_resource(resource_id: int, default: str, week: date) -> str:
    r = rows('''SELECT department FROM resource_department_assignments WHERE resource_id=? AND start_date<=? AND (end_date IS NULL OR end_date>=?) ORDER BY start_date DESC LIMIT 1''', (resource_id, week.isoformat(), week.isoformat()))
    return r[0]['department'] if r else default


def resource_active_for_week(resource: dict, week: date) -> bool:
    status = resource.get('active_status') or 'active'
    if status == 'active': return True
    start = pd.to_datetime(resource.get('status_start_date'), errors='coerce')
    end = pd.to_datetime(resource.get('status_end_date'), errors='coerce')
    ws, we = pd.Timestamp(week), pd.Timestamp(week + timedelta(days=6))
    return not ((pd.isna(start) or start <= we) and (pd.isna(end) or end >= ws))


def weekly_department_capacity(weeks: list[date]) -> pd.DataFrame:
    ensure_mvp_schema(); factor = setting_float('diminished_capacity_factor', 0.85); resources = rows('SELECT * FROM mvp_resources')
    holiday_rows = rows('SELECT person_name, holiday_date, hours FROM holidays')
    out = []
    for w in weeks:
        totals = {d: 0.0 for d in DISCIPLINES}
        for r in resources:
            if not resource_active_for_week(r, w): continue
            dept = department_for_resource(r['id'], r['department'], w)
            hol = sum(float(h['hours'] or 0) for h in holiday_rows if h['person_name'] == r['person_name'] and w <= pd.to_datetime(h['holiday_date']).date() <= w + timedelta(days=6))
            totals[dept] += max(float(r['weekly_hours'] or 0) - float(r.get('holiday_booked_hours') or 0) - hol, 0)
        for d, h in totals.items(): out.append({'week_start': w.isoformat(), 'department': d, 'available_capacity': round(h * factor, 2)})
    return pd.DataFrame(out)


def weekly_project_demand() -> pd.DataFrame:
    projects = get_projects(False); out=[]
    if projects.empty: return pd.DataFrame()
    for p in projects.to_dict('records'):
        for d in DISCIPLINES:
            hrs = float(p[f'{d.lower()}_hours'] or 0); s = pd.to_datetime(p.get(f'{d.lower()}_start_date') or p['start_date']).date(); e = pd.to_datetime(p['end_date']).date(); weeks = week_starts(s, e)
            vals = [0.0] * len(weeks) if p['loading_type'] == 'manual' else spread_hours(hrs, weeks, p['loading_type'])
            for w, v in zip(weeks, vals): out.append({'project_code': p['project_code'], 'project_name': p['project_name'], 'department': d, 'week_start': w.isoformat(), 'demand_hours': v, 'manual_required': p['loading_type'] == 'manual'})
    return pd.DataFrame(out)


def capacity_balance(weeks: list[date]) -> pd.DataFrame:
    cap = weekly_department_capacity(weeks); dem = weekly_project_demand()
    if dem.empty: demand = pd.DataFrame([{'week_start': w.isoformat(), 'department': d, 'allocated_demand': 0.0} for w in weeks for d in DISCIPLINES])
    else: demand = dem.groupby(['week_start','department'], as_index=False)['demand_hours'].sum().rename(columns={'demand_hours':'allocated_demand'})
    merged = cap.merge(demand, on=['week_start','department'], how='left').fillna({'allocated_demand': 0.0})
    merged['over_under_capacity'] = (merged['available_capacity'] - merged['allocated_demand']).round(2)
    merged['status'] = merged.apply(lambda r: capacity_status((r['allocated_demand']/r['available_capacity']) if r['available_capacity'] else None, r['available_capacity']), axis=1)
    return merged
