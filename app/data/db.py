from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

DB_PATH = Path('data/production_planner.sqlite')
SCHEMA_PATH = Path(__file__).with_name('schema.sql')


def get_connection() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys = ON')
    return conn


@contextmanager
def connect():
    conn = get_connection()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def initialize_database(seed: bool = True) -> None:
    with connect() as conn:
        conn.executescript(SCHEMA_PATH.read_text())
        migrate_database(conn)
        if seed and conn.execute('SELECT COUNT(*) FROM disciplines').fetchone()[0] == 0:
            seed_database(conn)
        seed_capacity_rules(conn)


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    return any(r['name'] == column for r in conn.execute(f'PRAGMA table_info({table})').fetchall())


def migrate_database(conn: sqlite3.Connection) -> None:
    if not _has_column(conn, 'daily_allocations', 'work_item_id'):
        conn.execute('ALTER TABLE daily_allocations ADD COLUMN work_item_id INTEGER REFERENCES work_items(id)')


def seed_capacity_rules(conn: sqlite3.Connection) -> None:
    conn.execute('''INSERT OR IGNORE INTO capacity_settings(scope_type,scope_id,diminished_capacity_factor,notes)
                    VALUES ('global',0,0.85,'Default workbook diminished capacity factor')''')
    types = [
        ('Project', 1, 0, 1), ('QA', 1, 0, 0), ('FLOW', 1, 0, 0), ('Training', 1, 0, 0),
        ('Admin', 1, 0, 0), ('Leave', 0, 1, 0), ('Unavailable', 0, 1, 0), ('Other', 1, 0, 0),
    ]
    conn.executemany('''INSERT OR IGNORE INTO work_item_types(name,consumes_capacity,removes_availability,project_backed)
                        VALUES (?,?,?,?)''', types)
    for name in ['QA', 'FLOW', 'Training', 'Admin', 'Leave', 'Unavailable', 'Other']:
        tid = conn.execute('SELECT id FROM work_item_types WHERE name=?', (name,)).fetchone()['id']
        conn.execute('INSERT OR IGNORE INTO work_items(name,work_item_type_id,active) VALUES (?,?,1)', (name, tid))
    project_type = conn.execute('SELECT id FROM work_item_types WHERE name="Project"').fetchone()['id']
    for project in conn.execute('SELECT id, project_name FROM projects').fetchall():
        conn.execute('''INSERT OR IGNORE INTO work_items(name,work_item_type_id,project_id,active)
                        VALUES (?,?,?,1)''', (project['project_name'], project_type, project['id']))


def rows(query: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
    with connect() as conn:
        return [dict(r) for r in conn.execute(query, tuple(params)).fetchall()]


def execute(query: str, params: Iterable[Any] = (), *, user: str = 'System', audit: dict[str, Any] | None = None) -> int:
    with connect() as conn:
        cur = conn.execute(query, tuple(params))
        object_id = cur.lastrowid
        if audit:
            write_audit(conn, user, audit.get('object_type', 'Unknown'), audit.get('object_id') or object_id,
                        audit.get('action', 'change'), audit.get('previous'), audit.get('new'), audit.get('reason'))
        return object_id


def write_audit(conn: sqlite3.Connection, user: str, object_type: str, object_id: int | None, action: str,
                previous: Any, new: Any, reason: str | None = None) -> None:
    conn.execute(
        'INSERT INTO audit_log(timestamp,user_name,object_type,object_id,action,previous_value,new_value,reason) VALUES (?,?,?,?,?,?,?,?)',
        (datetime.utcnow().isoformat(timespec='seconds'), user, object_type, object_id, action,
         json.dumps(previous, default=str) if previous is not None else None,
         json.dumps(new, default=str) if new is not None else None, reason),
    )


def seed_database(conn: sqlite3.Connection) -> None:
    for code in ['RS', 'GIS', 'PLS']:
        conn.execute('INSERT INTO disciplines(code,name) VALUES (?,?)', (code, code))
    disc = {r['code']: r['id'] for r in conn.execute('SELECT * FROM disciplines')}
    for name, code in [('Remote Sensing', 'RS'), ('GIS Production', 'GIS'), ('Survey / PLS', 'PLS')]:
        conn.execute('INSERT INTO teams(name,discipline_id) VALUES (?,?)', (name, disc[code]))
    teams = {r['name']: r['id'] for r in conn.execute('SELECT * FROM teams')}
    people = [('Avery RS', 'Remote Sensing', 'RS'), ('Blake RS', 'Remote Sensing', 'RS'), ('Casey GIS', 'GIS Production', 'GIS'), ('Devon GIS', 'GIS Production', 'GIS'), ('Elliot PLS', 'Survey / PLS', 'PLS'), ('Finley PLS', 'Survey / PLS', 'PLS')]
    for name, team, code in people:
        conn.execute('INSERT INTO people(name,team_id,discipline_id,daily_hours,weekly_hours) VALUES (?,?,?,?,?)', (name, teams[team], disc[code], 7.5, 37.5))
    for user, role, code in [('Chief Delivery Officer', 'CDO', None), ('RS Manager', 'Manager', 'RS'), ('GIS Manager', 'Manager', 'GIS'), ('PLS Manager', 'Manager', 'PLS'), ('PM Viewer', 'PM', None)]:
        conn.execute('INSERT INTO user_roles(user_name,role,discipline_id) VALUES (?,?,?)', (user, role, disc.get(code) if code else None))
    cats = [('FLOW', 'workflow', 1, 0, 0), ('QA', 'quality', 1, 0, 0), ('Admin', 'admin', 1, 0, 0), ('Training', 'training', 1, 0, 0), ('Absence', 'absence', 0, 0, 1), ('Unavailable', 'unavailable', 0, 0, 1)]
    conn.executemany('INSERT INTO non_billable_categories(name,category_type,consumes_capacity,billable_project_work,removes_availability) VALUES (?,?,?,?,?)', cats)
    today = date.today(); monday = today - timedelta(days=today.weekday())
    projects = [('Orion Mapping', 'Northwind', monday.isoformat(), (monday+timedelta(days=45)).isoformat(), (monday+timedelta(days=38)).isoformat(), 'Active', 'Seeded project', 'SEED-001'), ('River Corridor', 'Contoso', monday.isoformat(), (monday+timedelta(days=30)).isoformat(), (monday+timedelta(days=25)).isoformat(), 'Active', 'OSR linked in mock import', 'SEED-002'), ('Harbour Update', 'Fabrikam', (monday-timedelta(days=14)).isoformat(), (monday+timedelta(days=10)).isoformat(), (monday+timedelta(days=8)).isoformat(), 'Active', 'Deadline risk example', 'SEED-003')]
    for p in projects:
        conn.execute('INSERT INTO projects(project_name,client,start_date,end_date,deadline,status,notes,source_reference_id,imported_at) VALUES (?,?,?,?,?,?,?,?,?)', (*p, datetime.utcnow().isoformat(timespec='seconds')))
    prows = conn.execute('SELECT * FROM projects').fetchall()
    budgets = {'Orion Mapping': {'RS': 120, 'GIS': 90, 'PLS': 30}, 'River Corridor': {'RS': 40, 'GIS': 150, 'PLS': 80}, 'Harbour Update': {'RS': 20, 'GIS': 55, 'PLS': 110}}
    for p in prows:
        for code, hrs in budgets[p['project_name']].items():
            conn.execute('INSERT INTO project_discipline_budgets(project_id,discipline_id,planned_hours) VALUES (?,?,?)', (p['id'], disc[code], hrs))
            for w in range(4):
                conn.execute('INSERT INTO weekly_demand(project_id,discipline_id,week_start,demand_hours,source) VALUES (?,?,?,?,?)', (p['id'], disc[code], (monday+timedelta(days=7*w)).isoformat(), round(hrs/4, 1), 'seed'))
    people_rows = conn.execute('SELECT * FROM people').fetchall()
    for person in people_rows:
        for offset in range(30):
            d = monday + timedelta(days=offset)
            if d.weekday() < 5:
                conn.execute('INSERT INTO availability_calendar(person_id,work_date,available_hours,source) VALUES (?,?,?,?)', (person['id'], d.isoformat(), person['daily_hours'], 'seed'))
    # allocations and OSR progress
    for i, person in enumerate(people_rows):
        project = prows[i % len(prows)]
        for offset in range(0, 10):
            d = monday + timedelta(days=offset)
            if d.weekday() < 5:
                conn.execute('INSERT INTO daily_allocations(person_id,allocation_date,project_id,split_slot,allocated_hours,notes,source) VALUES (?,?,?,?,?,?,?)', (person['id'], d.isoformat(), project['id'], 1, 7.5, 'Seed allocation', 'seed'))
    now = datetime.utcnow().isoformat(timespec='seconds')
    progress = [('Orion Mapping', 35, 110), ('River Corridor', 45, 140), ('Harbour Update', 40, 95)]
    for name, pct, actual in progress:
        pid = next(p['id'] for p in prows if p['project_name'] == name)
        conn.execute('INSERT INTO osr_progress(project_id,progress_date,percent_complete,actual_hours_to_date,imported_at) VALUES (?,?,?,?,?)', (pid, today.isoformat(), pct, actual, now))
        conn.execute('INSERT INTO actual_hours(project_id,work_date,hours,imported_at) VALUES (?,?,?,?)', (pid, today.isoformat(), actual, now))
    write_audit(conn, 'System', 'Database', None, 'seed', None, 'Seed data created', 'Initial MVP seed')
