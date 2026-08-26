from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from html.parser import HTMLParser
import json
import re
from pathlib import Path
from typing import Any, Iterable
from collections import defaultdict

import pandas as pd

from app.data.db import connect, execute, rows
from app.services.planning import CAPACITY_RISK_UTILISATION, capacity_status, spread_hours, week_starts

DISCIPLINES = ["RS", "GIS", "PLS"]
TEMPORARY_ADJUSTMENT_TYPES = [
    "Temporary assignment", "Secondment", "Unavailable", "Training",
    "Internal activity", "Other",
]
SEQUENCE_GAP_THRESHOLD_DAYS = 7
PROJECT_HEALTH_TOLERANCE_HOURS = 0.5
ALLOCATION_FUTURE_HORIZON_WEEKS = 78

CAPACITY_STATUS_LABELS = {
    "green": "Within capacity",
    "amber": "Capacity risk",
    "red": "Over capacity",
    "grey": "Within capacity",
}
LOADING_TYPES = ["even", "front_loaded", "back_loaded", "manual"]
RESOURCE_STATUSES = [
    "active",
    "suspended",
    "maternity",
    "secondment",
    "out_of_business",
    "left_business",
]

PROJECT_FIELDS = [
    "project_code",
    "project_name",
    "client",
    "project_manager",
    "priority",
    "penalty_exposure",
    "row_km",
    "cct_km",
    "spus",
    "rs_hours",
    "gis_hours",
    "pls_hours",
    "actual_rs_hours",
    "actual_gis_hours",
    "actual_pls_hours",
    "start_date",
    "end_date",
    "loading_type",
    "rs_start_date",
    "gis_start_date",
    "pls_start_date",
    "status",
    "assumptions",
]

RESOURCE_FIELDS = [
    "person_name",
    "department",
    "weekly_hours",
    "holiday_booked_hours",
    "holiday_remaining_hours",
    "active_status",
    "status_reason",
    "status_start_date",
    "status_end_date",
]

PROJECT_DATE_COLUMNS = [
    "start_date",
    "end_date",
    "rs_start_date",
    "gis_start_date",
    "pls_start_date",
]

RESOURCE_DATE_COLUMNS = [
    "status_start_date",
    "status_end_date",
    "department_change_start_date",
    "department_change_end_date",
]


def parse_audit_timestamps(values: pd.Series) -> pd.Series:
    """Parse legacy SQLite/Python audit timestamps without rejecting bad rows."""
    try:
        return pd.to_datetime(values, format="mixed", errors="coerce")
    except TypeError:  # pandas < 2 has no ``format='mixed'`` support.
        return values.apply(lambda value: pd.to_datetime(value, errors="coerce"))


def ensure_mvp_schema() -> None:
    with connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS mvp_projects (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              project_code TEXT NOT NULL UNIQUE,
              project_name TEXT NOT NULL,
              client TEXT,
              project_manager TEXT,
              priority TEXT NOT NULL DEFAULT 'P3',
              penalty_exposure TEXT NOT NULL DEFAULT 'None',
              row_km REAL NOT NULL DEFAULT 0,
              cct_km REAL NOT NULL DEFAULT 0,
              spus REAL NOT NULL DEFAULT 0,
              rs_hours REAL NOT NULL DEFAULT 0,
              gis_hours REAL NOT NULL DEFAULT 0,
              pls_hours REAL NOT NULL DEFAULT 0,
              actual_rs_hours REAL NOT NULL DEFAULT 0,
              actual_gis_hours REAL NOT NULL DEFAULT 0,
              actual_pls_hours REAL NOT NULL DEFAULT 0,
              start_date TEXT NOT NULL,
              end_date TEXT NOT NULL,
              loading_type TEXT NOT NULL DEFAULT 'even',
              rs_start_date TEXT,
              gis_start_date TEXT,
              pls_start_date TEXT,
              status TEXT NOT NULL DEFAULT 'active',
              archived INTEGER NOT NULL DEFAULT 0,
              assumptions TEXT,
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
              notes TEXT,
              UNIQUE(person_name, holiday_date, source)
            );

            CREATE TABLE IF NOT EXISTS settings (
              key TEXT PRIMARY KEY,
              value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS manager_weekly_plan (
              project_code TEXT NOT NULL REFERENCES mvp_projects(project_code) ON DELETE CASCADE,
              department TEXT NOT NULL CHECK(department IN ('RS','GIS','PLS')),
              week_start TEXT NOT NULL,
              planned_hours REAL NOT NULL CHECK(planned_hours >= 0),
              updated_by TEXT NOT NULL,
              updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
              PRIMARY KEY(project_code, department, week_start)
            );

            CREATE TABLE IF NOT EXISTS planning_escalations (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              project_code TEXT REFERENCES mvp_projects(project_code) ON DELETE SET NULL,
              department TEXT NOT NULL CHECK(department IN ('RS','GIS','PLS')),
              issue_type TEXT NOT NULL,
              impact_hours REAL NOT NULL DEFAULT 0,
              decision_required TEXT NOT NULL,
              owner TEXT NOT NULL,
              required_by TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'Open',
              created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS planning_reviews (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              department TEXT NOT NULL CHECK(department IN ('RS','GIS','PLS')),
              period_start TEXT NOT NULL,
              period_end TEXT NOT NULL,
              status TEXT NOT NULL,
              completed_by TEXT NOT NULL,
              completed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
              open_escalations INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS holiday_imports (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              filename TEXT NOT NULL,
              imported_by TEXT NOT NULL,
              imported_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
              record_count INTEGER NOT NULL DEFAULT 0,
              unmatched_count INTEGER NOT NULL DEFAULT 0,
              summary_json TEXT
            );

            CREATE TABLE IF NOT EXISTS resource_employee_ids (
              employee_id TEXT PRIMARY KEY,
              resource_id INTEGER NOT NULL REFERENCES mvp_resources(id) ON DELETE CASCADE,
              employee_name TEXT,
              updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS internal_activities (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              activity_name TEXT NOT NULL,
              department TEXT NOT NULL CHECK(department IN ('RS','GIS','PLS')),
              start_week TEXT NOT NULL,
              end_week TEXT NOT NULL,
              planned_hours_per_week REAL NOT NULL DEFAULT 0 CHECK(planned_hours_per_week >= 0),
              active INTEGER NOT NULL DEFAULT 1,
              notes TEXT,
              updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS resource_capacity_adjustments (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              resource_id INTEGER NOT NULL REFERENCES mvp_resources(id) ON DELETE RESTRICT,
              adjustment_type TEXT NOT NULL,
              destination_department TEXT CHECK(destination_department IN ('RS','GIS','PLS')),
              start_date TEXT NOT NULL,
              end_date TEXT NOT NULL,
              capacity_percent REAL,
              hours_per_week REAL,
              reason TEXT,
              active INTEGER NOT NULL DEFAULT 1,
              created_by TEXT,
              updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
              CHECK(capacity_percent IS NOT NULL OR hours_per_week IS NOT NULL)
            );

            CREATE INDEX IF NOT EXISTS idx_capacity_adjustments_resource_dates
            ON resource_capacity_adjustments(resource_id,start_date,end_date,active);

            -- harmless when created on a fresh database; ignored below for existing DBs

            INSERT OR IGNORE INTO settings(key,value)
            VALUES ('diminished_capacity_factor','1.0');

            INSERT OR IGNORE INTO settings(key,value)
            VALUES ('data_version','0');

            INSERT OR IGNORE INTO settings(key,value)
            VALUES ('last_updated_at',datetime('now'));
            """
        )
        try:
            conn.execute("ALTER TABLE holidays ADD COLUMN notes TEXT")
        except Exception:
            pass
        additions = {
            "client": "TEXT", "project_manager": "TEXT", "priority": "TEXT NOT NULL DEFAULT 'P3'",
            "penalty_exposure": "TEXT NOT NULL DEFAULT 'None'", "actual_rs_hours": "REAL NOT NULL DEFAULT 0",
            "actual_gis_hours": "REAL NOT NULL DEFAULT 0", "actual_pls_hours": "REAL NOT NULL DEFAULT 0",
            "assumptions": "TEXT",
        }
        existing = {r[1] for r in conn.execute("PRAGMA table_info(mvp_projects)")}
        for column, definition in additions.items():
            if column not in existing:
                conn.execute(f"ALTER TABLE mvp_projects ADD COLUMN {column} {definition}")
        holiday_columns = {r[1] for r in conn.execute("PRAGMA table_info(holidays)")}
        for column, definition in {
            "employee_id": "TEXT", "status": "TEXT NOT NULL DEFAULT 'active'",
            "import_id": "INTEGER REFERENCES holiday_imports(id)",
        }.items():
            if column not in holiday_columns:
                conn.execute(f"ALTER TABLE holidays ADD COLUMN {column} {definition}")
        escalation_columns = {r[1] for r in conn.execute("PRAGMA table_info(planning_escalations)")}
        for column, definition in {
            "created_by": "TEXT", "updated_at": "TEXT", "resolved_at": "TEXT",
            "resolved_by": "TEXT", "resolution": "TEXT",
        }.items():
            if column not in escalation_columns:
                conn.execute(f"ALTER TABLE planning_escalations ADD COLUMN {column} {definition}")
        audit_columns = {r[1] for r in conn.execute("PRAGMA table_info(audit_log)")}
        for column, definition in {
            "project_code": "TEXT", "department": "TEXT", "field_name": "TEXT",
            "details": "TEXT",
        }.items():
            if column not in audit_columns:
                conn.execute(f"ALTER TABLE audit_log ADD COLUMN {column} {definition}")


def _audit(conn, user: str, action: str, entity_type: str, entity_id=None, *,
           project_code=None, department=None, field_name=None, old=None, new=None,
           details=None) -> None:
    """Append one human-readable audit event; audit rows are never mutated."""
    conn.execute(
        """INSERT INTO audit_log(timestamp,user_name,object_type,object_id,action,
           previous_value,new_value,reason,project_code,department,field_name,details)
           VALUES (CURRENT_TIMESTAMP,?,?,?,?,?,?,?,?,?,?,?)""",
        (user, entity_type, entity_id, action,
         json.dumps(old, default=str) if old is not None else None,
         json.dumps(new, default=str) if new is not None else None, details,
         project_code, department, field_name, details),
    )



def get_setting(key: str, default: str = "") -> str:
    ensure_mvp_schema()
    r = rows("SELECT value FROM settings WHERE key=?", (key,))
    return str(r[0]["value"]) if r else default


def get_data_version() -> int:
    value = get_setting("data_version", "0")
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def get_last_updated_at() -> str:
    return get_setting("last_updated_at", "")


def increment_data_version(conn=None) -> int:
    """Increment the MVP data version used to invalidate derived UI caches."""
    if conn is None:
        with connect() as inner:
            return increment_data_version(inner)

    row = conn.execute("SELECT value FROM settings WHERE key='data_version'").fetchone()
    try:
        current = int(row["value"]) if row else 0
    except (TypeError, ValueError):
        current = 0
    new_version = current + 1
    conn.execute(
        "INSERT OR REPLACE INTO settings(key,value) VALUES ('data_version',?)",
        (str(new_version),),
    )
    conn.execute(
        "INSERT OR REPLACE INTO settings(key,value) VALUES ('last_updated_at',datetime('now'))"
    )
    return new_version

def prepare_date_columns_for_editor(
    df: pd.DataFrame, date_columns: list[str]
) -> pd.DataFrame:
    """Return a copy with Streamlit DateColumn-compatible datetime columns."""
    prepared = df.copy()
    for column in date_columns:
        if column in prepared.columns:
            prepared[column] = pd.to_datetime(prepared[column], errors="coerce")
    return prepared


def normalise_date_for_db(value, default: date | None = None) -> str | None:
    """Parse editor/CSV date values and return ISO YYYY-MM-DD strings for SQLite."""
    if value is None or value is pd.NA:
        return default.isoformat() if default else None

    if isinstance(value, str) and not value.strip():
        return default.isoformat() if default else None

    parsed = pd.to_datetime(value, errors="coerce", dayfirst=False)
    if pd.isna(parsed):
        parsed = pd.to_datetime(value, errors="coerce", dayfirst=True)

    if pd.isna(parsed):
        return default.isoformat() if default else None

    return parsed.date().isoformat()


def _date(value, default: date) -> str:
    parsed = normalise_date_for_db(value, default)
    return parsed if parsed is not None else default.isoformat()


def normalize_loading_type(value) -> str:
    v = str(value or "even").strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "even_spread": "even",
        "front_loaded": "front_loaded",
        "back_loaded": "back_loaded",
        "manual_weekly_spread": "manual",
    }
    return aliases.get(v, v if v in LOADING_TYPES else "even")


def load_projects_csv(path: str | Path = "sample-data/projects.csv") -> pd.DataFrame:
    df = pd.read_csv(path)
    today = date.today()

    out = pd.DataFrame()
    out["project_code"] = (
        df.get("project_code", df.get("Project Code", "")).astype(str).str.strip()
    )
    out["project_name"] = (
        df.get("project_name", df.get("Project Name", "")).astype(str).str.strip()
    )
    out["client"] = df.get("client", "")
    out["project_manager"] = df.get("project_manager", "")
    out["priority"] = df.get("priority", "P3")
    out["penalty_exposure"] = df.get("penalty_exposure", "None")
    out["row_km"] = pd.to_numeric(
        df.get("row_km", df.get("ROW (km)", 0)), errors="coerce"
    ).fillna(0)
    out["cct_km"] = pd.to_numeric(
        df.get("cct_km", df.get("Circuit Length (km)", 0)), errors="coerce"
    ).fillna(0)
    out["spus"] = pd.to_numeric(
        df.get("spus", df.get("Total SPUs", 0)), errors="coerce"
    ).fillna(0)
    out["rs_hours"] = pd.to_numeric(
        df.get("rs_hours", df.get("RS Total", 0)), errors="coerce"
    ).fillna(0)
    out["gis_hours"] = pd.to_numeric(
        df.get("gis_hours", df.get("GIS Total", 0)), errors="coerce"
    ).fillna(0)
    out["pls_hours"] = pd.to_numeric(
        df.get("pls_hours", df.get("PLS Total", 0)), errors="coerce"
    ).fillna(0)
    for discipline in DISCIPLINES:
        out[f"actual_{discipline.lower()}_hours"] = 0.0

    out["start_date"] = df.get(
        "start_date", df.get("Production Start date", today)
    ).apply(lambda v: _date(v, today))
    out["end_date"] = df.get(
        "end_date", df.get("Production Estimated Completion Date", today + timedelta(days=28))
    ).apply(lambda v: _date(v, today + timedelta(days=28)))

    out["loading_type"] = df.get("loading_type", "even")
    out["loading_type"] = out["loading_type"].apply(normalize_loading_type)

    out["rs_start_date"] = df.get(
        "rs_start_date", df.get("RS (Luke)", out["start_date"])
    ).apply(lambda v: _date(v, today))
    out["gis_start_date"] = df.get(
        "gis_start_date", df.get("GIS (Dom)", out["start_date"])
    ).apply(lambda v: _date(v, today))
    out["pls_start_date"] = df.get(
        "pls_start_date", df.get("PLS (Carlos)", out["start_date"])
    ).apply(lambda v: _date(v, today))

    out["status"] = df.get("status", "active")
    out["status"] = (
        out["status"].fillna("active").astype(str).str.lower().replace({"archived": "archived"})
    )
    out["assumptions"] = df.get("assumptions", "")

    return out[PROJECT_FIELDS]


def save_projects(records: Iterable[dict], user: str = "System") -> None:
    ensure_mvp_schema()
    with connect() as conn:
        for r in records:
            code = str(r.get("project_code") or "").strip()
            name = str(r.get("project_name") or "").strip()
            original_code = str(r.get("_original_project_code") or code).strip()

            if not code or not name:
                continue
            previous_row = conn.execute("SELECT * FROM mvp_projects WHERE project_code=?", (original_code,)).fetchone()

            if original_code != code:
                if conn.execute(
                    "SELECT 1 FROM mvp_projects WHERE project_code=?", (code,)
                ).fetchone():
                    raise ValueError(f"Project code {code} is already in use.")
                # The project code is the legacy key used by manager planning.
                # Defer FK checks while renaming it and its related records as one
                # atomic operation.
                conn.execute("PRAGMA defer_foreign_keys=ON")
                conn.execute(
                    "UPDATE mvp_projects SET project_code=? WHERE project_code=?",
                    (code, original_code),
                )
                conn.execute(
                    "UPDATE manager_weekly_plan SET project_code=? WHERE project_code=?",
                    (code, original_code),
                )
                conn.execute(
                    "UPDATE planning_escalations SET project_code=? WHERE project_code=?",
                    (code, original_code),
                )

            vals = {k: r.get(k) for k in PROJECT_FIELDS}
            vals["loading_type"] = normalize_loading_type(vals.get("loading_type"))
            vals["status"] = str(vals.get("status") or "active").lower()
            archived = 1 if vals["status"] == "archived" else 0

            conn.execute(
                """
                INSERT INTO mvp_projects(
                    project_code,
                    project_name,
                    client, project_manager, priority, penalty_exposure,
                    row_km,
                    cct_km,
                    spus,
                    rs_hours,
                    gis_hours,
                    pls_hours,
                    actual_rs_hours, actual_gis_hours, actual_pls_hours,
                    start_date,
                    end_date,
                    loading_type,
                    rs_start_date,
                    gis_start_date,
                    pls_start_date,
                    status,
                    archived,
                    assumptions,
                    updated_at
                )
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'))
                ON CONFLICT(project_code) DO UPDATE SET
                    project_name=excluded.project_name,
                    client=excluded.client,
                    project_manager=excluded.project_manager,
                    priority=excluded.priority,
                    penalty_exposure=excluded.penalty_exposure,
                    row_km=excluded.row_km,
                    cct_km=excluded.cct_km,
                    spus=excluded.spus,
                    rs_hours=excluded.rs_hours,
                    gis_hours=excluded.gis_hours,
                    pls_hours=excluded.pls_hours,
                    actual_rs_hours=excluded.actual_rs_hours,
                    actual_gis_hours=excluded.actual_gis_hours,
                    actual_pls_hours=excluded.actual_pls_hours,
                    start_date=excluded.start_date,
                    end_date=excluded.end_date,
                    loading_type=excluded.loading_type,
                    rs_start_date=excluded.rs_start_date,
                    gis_start_date=excluded.gis_start_date,
                    pls_start_date=excluded.pls_start_date,
                    status=excluded.status,
                    archived=excluded.archived,
                    assumptions=excluded.assumptions,
                    updated_at=datetime('now')
                """,
                (
                    code,
                    name,
                    vals.get("client"), vals.get("project_manager"), vals.get("priority") or "P3",
                    vals.get("penalty_exposure") or "None",
                    float(vals.get("row_km") or 0),
                    float(vals.get("cct_km") or 0),
                    float(vals.get("spus") or 0),
                    float(vals.get("rs_hours") or 0),
                    float(vals.get("gis_hours") or 0),
                    float(vals.get("pls_hours") or 0),
                    float(vals.get("actual_rs_hours") or 0), float(vals.get("actual_gis_hours") or 0),
                    float(vals.get("actual_pls_hours") or 0),
                    normalise_date_for_db(vals.get("start_date")),
                    normalise_date_for_db(vals.get("end_date")),
                    vals["loading_type"],
                    normalise_date_for_db(vals.get("rs_start_date")) or normalise_date_for_db(vals.get("start_date")),
                    normalise_date_for_db(vals.get("gis_start_date")) or normalise_date_for_db(vals.get("start_date")),
                    normalise_date_for_db(vals.get("pls_start_date")) or normalise_date_for_db(vals.get("start_date")),
                    vals["status"],
                    archived,
                    vals.get("assumptions"),
                ),
            )
            current = dict(conn.execute("SELECT * FROM mvp_projects WHERE project_code=?", (code,)).fetchone())
            previous = dict(previous_row) if previous_row else None
            if previous is None:
                _audit(conn,user,"Project created","Project",current["id"],project_code=code,new=current,details=f"Created {code} · {name}")
            else:
                changed=[k for k in PROJECT_FIELDS if str(previous.get(k) or "") != str(current.get(k) or "")]
                if changed:
                    _audit(conn,user,"Project edited","Project",current["id"],project_code=code,old={k:previous.get(k) for k in changed},new={k:current.get(k) for k in changed},details="Changed: "+", ".join(changed))
        increment_data_version(conn)


def import_default_projects() -> int:
    df = load_projects_csv()
    save_projects(df.to_dict("records"))
    return len(df)


def get_projects(include_archived: bool = True) -> pd.DataFrame:
    ensure_mvp_schema()
    where = "" if include_archived else "WHERE archived=0"
    return pd.DataFrame(
        rows(
            f"""
            SELECT {",".join(PROJECT_FIELDS)}, archived
            FROM mvp_projects
            {where}
            ORDER BY project_code
            """
        )
    )


def validate_project_demand(record: dict) -> list[str]:
    """Return business-input errors without inventing dates, quantities or hours."""
    errors = []
    labels = {
        "project_code": "Project code", "project_name": "Project name", "client": "Client",
        "project_manager": "Project manager", "start_date": "Production start",
        "end_date": "Required completion",
    }
    for field, label in labels.items():
        if not record.get(field):
            errors.append(f"{label} is required.")
    start = normalise_date_for_db(record.get("start_date"))
    end = normalise_date_for_db(record.get("end_date"))
    if start and end and start > end:
        errors.append("Required completion must be on or after production start.")
    total = 0.0
    for discipline in DISCIPLINES:
        hours = float(record.get(f"{discipline.lower()}_hours") or 0)
        actual = float(record.get(f"actual_{discipline.lower()}_hours") or 0)
        total += hours
        if actual > hours:
            errors.append(f"{discipline} actual hours cannot exceed its current forecast hours.")
        if hours > 0 and not record.get(f"{discipline.lower()}_start_date"):
            errors.append(f"{discipline} data-available date is required when {discipline} has hours.")
    if total <= 0:
        errors.append("At least one discipline must have forecast hours.")
    return errors


def manager_plan(weeks: list[date], department: str) -> pd.DataFrame:
    """Return one project row with editable weekly manager-plan columns."""
    projects = get_projects(False)
    if projects.empty:
        return pd.DataFrame()
    saved = rows(
        "SELECT project_code,week_start,planned_hours FROM manager_weekly_plan WHERE department=?",
        (department,),
    )
    saved_map = {(r["project_code"], r["week_start"]): float(r["planned_hours"]) for r in saved}
    baseline = weekly_project_demand()
    output = []
    for project in projects.to_dict("records"):
        forecast = float(project.get(f"{department.lower()}_hours") or 0)
        actual = float(project.get(f"actual_{department.lower()}_hours") or 0)
        if forecast <= 0:
            continue
        row = {
            "Priority": project.get("priority") or "P3",
            "Project Code": project["project_code"],
            "Project": project["project_name"],
            "Forecast Hours": forecast,
            "Actual Hours": actual,
            "Remaining Hours": max(forecast - actual, 0),
            "Data Available": project.get(f"{department.lower()}_start_date"),
            "Required By": project.get("end_date"),
        }
        for week in weeks:
            key = week.isoformat()
            if (project["project_code"], key) in saved_map:
                value = saved_map[(project["project_code"], key)]
            elif not baseline.empty:
                match = baseline[(baseline.project_code == project["project_code"]) &
                                 (baseline.department == department) & (baseline.week_start == key)]
                # Baseline demand represents forecast totals. Scale it to the
                # remaining work so imported actuals are never planned twice.
                value = (float(match.demand_hours.sum()) * max(forecast - actual, 0) / forecast) if not match.empty else 0.0
            else:
                value = 0.0
            row[key] = round(value, 2)
        # Unplanned demand is deliberately independent of the visible end date.
        # Only allocations before the operational planning start are historical.
        row["Unplanned Hours"] = unplanned_hours(
            row["Remaining Hours"], project["project_code"], department, weeks[0]
        )
        output.append(row)
    frame = pd.DataFrame(output).sort_values(["Priority", "Required By", "Project Code"])
    # Keep the unresolved balance beside the project facts rather than beyond
    # what can be a long sequence of weekly columns.
    week_columns = [week.isoformat() for week in weeks]
    leading = [
        "Priority", "Project Code", "Project", "Forecast Hours", "Actual Hours",
        "Remaining Hours", "Unplanned Hours", "Data Available", "Required By",
    ]
    return frame[leading + week_columns]


def save_manager_plan(frame: pd.DataFrame, weeks: list[date], department: str, user: str) -> None:
    with connect() as conn:
        for record in frame.to_dict("records"):
            code = record["Project Code"]
            remaining = float(record["Remaining Hours"] or 0)
            values = [max(float(record.get(w.isoformat()) or 0), 0) for w in weeks]
            if round(sum(values), 2) > round(remaining, 2):
                raise ValueError(f"{code} plans more hours than its {remaining:.1f} remaining hours.")
            for week, hours in zip(weeks, values):
                prior = conn.execute("SELECT planned_hours FROM manager_weekly_plan WHERE project_code=? AND department=? AND week_start=?",
                                     (code, department, week.isoformat())).fetchone()
                old = float(prior["planned_hours"]) if prior else 0.0
                if abs(old - hours) <= 0.005:
                    continue
                conn.execute(
                    """INSERT INTO manager_weekly_plan(project_code,department,week_start,planned_hours,updated_by)
                       VALUES (?,?,?,?,?) ON CONFLICT(project_code,department,week_start) DO UPDATE SET
                       planned_hours=excluded.planned_hours,updated_by=excluded.updated_by,updated_at=CURRENT_TIMESTAMP""",
                    (code, department, week.isoformat(), hours, user),
                )
                _audit(conn, user, "Allocation updated", "Weekly allocation", code,
                       project_code=code, department=department, field_name=week.isoformat(),
                       old=old, new=hours, details=f"{week.isoformat()}: {old:g} h → {hours:g} h")
        increment_data_version(conn)


def quick_allocation_values(mode: str, weeks: list[date], *, people: float = 0,
                            hours_per_person: float = 37.5, hours_per_week: float = 0,
                            remaining_hours: float = 0) -> list[float]:
    """Turn a manager-friendly allocation request into weekly source values."""
    if not weeks:
        return []
    if mode == "People":
        value = max(float(people), 0) * max(float(hours_per_person), 0)
        return [round(value, 2)] * len(weeks)
    if mode == "Hours/week":
        return [round(max(float(hours_per_week), 0), 2)] * len(weeks)
    if mode == "Spread remaining":
        return spread_hours(max(float(remaining_hours), 0), weeks, "even")
    raise ValueError(f"Unknown allocation mode: {mode}")


def project_remaining_hours(project_code: str, department: str) -> float:
    projects = get_projects(True)
    match = projects[projects.project_code == project_code] if not projects.empty else pd.DataFrame()
    if match.empty:
        raise ValueError(f"Unknown project: {project_code}")
    project = match.iloc[0]
    return round(max(float(project[f"{department.lower()}_hours"] or 0) -
                     float(project[f"actual_{department.lower()}_hours"] or 0), 0), 2)


def future_project_allocation(project_code: str, department: str, planning_start: date) -> float:
    """Return authoritative manager allocation from planning start, with no end bound."""
    start = planning_start - timedelta(days=planning_start.weekday())
    result = rows("""SELECT COALESCE(SUM(planned_hours),0) AS hours
                     FROM manager_weekly_plan
                     WHERE project_code=? AND department=? AND week_start>=?""",
                  (project_code, department, start.isoformat()))
    return round(float(result[0]["hours"] or 0), 2)


def unplanned_hours(remaining_hours: float, project_code: str, department: str,
                    planning_start: date) -> float:
    """Demand not covered by future manager allocation (planning end is not a limit)."""
    return round(max(float(remaining_hours or 0) -
                     future_project_allocation(project_code, department, planning_start), 0), 2)


def apply_quick_allocation(project_code: str, department: str, weeks: list[date], values: list[float],
                           user: str, operation: str = "add", allow_overallocation: bool = False,
                           planning_start: date | None = None) -> None:
    """Add to or replace only one project's selected manager-plan cells."""
    if len(weeks) != len(values) or operation not in {"add", "replace", "replace_future"}:
        raise ValueError("Invalid allocation request.")
    remaining = project_remaining_hours(project_code, department)
    with connect() as conn:
        existing_total = float(conn.execute(
            "SELECT COALESCE(SUM(planned_hours),0) total FROM manager_weekly_plan WHERE project_code=? AND department=?",
            (project_code, department)).fetchone()["total"] or 0)
        selected = {r["week_start"]: float(r["planned_hours"]) for r in conn.execute(
            "SELECT week_start,planned_hours FROM manager_weekly_plan WHERE project_code=? AND department=?",
            (project_code, department)).fetchall()}
        planning_start = (planning_start or min(weeks))
        planning_start = planning_start - timedelta(days=planning_start.weekday())
        future_existing = sum(v for key, v in selected.items() if key >= planning_start.isoformat())
        selected_old = (sum(v for key, v in selected.items() if key >= planning_start.isoformat())
                        if operation == "replace_future"
                        else sum(selected.get(w.isoformat(), 0) for w in weeks))
        new_selected = sum((selected.get(w.isoformat(), 0) + max(float(v), 0)) if operation == "add"
                           else max(float(v), 0) for w, v in zip(weeks, values))
        new_total = ((new_selected if operation == "replace_future" else future_existing - selected_old + new_selected)
                     if operation in {"add", "replace_future"} else existing_total - selected_old + new_selected)
        if new_total > remaining + 0.005 and not allow_overallocation:
            raise ValueError(f"Allocation would plan {new_total:.2f} h against {remaining:.2f} remaining hours. Confirm override to continue.")
        if operation == "replace_future":
            removed = [dict(r) for r in conn.execute(
                "SELECT week_start,planned_hours FROM manager_weekly_plan WHERE project_code=? AND department=? AND week_start>=? AND planned_hours<>0",
                (project_code, department, planning_start.isoformat()))]
            conn.execute("UPDATE manager_weekly_plan SET planned_hours=0,updated_by=?,updated_at=CURRENT_TIMESTAMP WHERE project_code=? AND department=? AND week_start>=? AND planned_hours<>0",
                         (user, project_code, department, planning_start.isoformat()))
            if removed:
                _audit(conn, user, "Full future allocation replaced", "Weekly allocation", project_code,
                       project_code=project_code, department=department, old=removed, new=[],
                       details=f"Removed {sum(r['planned_hours'] for r in removed):g} h from {planning_start.isoformat()} before replacement")
            selected = {key: value for key, value in selected.items() if key < planning_start.isoformat()}
        for week, value in zip(weeks, values):
            old = selected.get(week.isoformat(), 0)
            hours = old + max(float(value), 0) if operation == "add" else max(float(value), 0)
            conn.execute("""INSERT INTO manager_weekly_plan(project_code,department,week_start,planned_hours,updated_by)
                            VALUES (?,?,?,?,?) ON CONFLICT(project_code,department,week_start) DO UPDATE SET
                            planned_hours=excluded.planned_hours,updated_by=excluded.updated_by,updated_at=CURRENT_TIMESTAMP""",
                         (project_code, department, week.isoformat(), round(hours, 2), user))
            if abs(old - hours) > 0.005:
                _audit(conn, user, "Quick Allocation", "Weekly allocation", project_code,
                       project_code=project_code, department=department, field_name=week.isoformat(),
                       old=old, new=round(hours, 2), details=f"{operation.replace('_', ' ').title()} · {week.isoformat()}: {old:g} h → {hours:g} h")
        increment_data_version(conn)


def clear_future_allocation(project_code: str, department: str, from_week: date, user: str) -> int:
    from_week = from_week - timedelta(days=from_week.weekday())
    with connect() as conn:
        old = [dict(r) for r in conn.execute("SELECT week_start,planned_hours FROM manager_weekly_plan WHERE project_code=? AND department=? AND week_start>=? AND planned_hours<>0", (project_code,department,from_week.isoformat()))]
        cur = conn.execute("UPDATE manager_weekly_plan SET planned_hours=0,updated_by=?,updated_at=CURRENT_TIMESTAMP "
                           "WHERE project_code=? AND department=? AND week_start>=? AND planned_hours<>0",
                           (user, project_code, department, from_week.isoformat()))
        _audit(conn,user,"Future allocation cleared","Weekly allocation",project_code,
               project_code=project_code,department=department,old=old,new=[],
               details=f"Cleared {len(old)} weeks from {from_week.isoformat()} ({sum(r['planned_hours'] for r in old):g} h)")
        increment_data_version(conn)
        return cur.rowcount


def move_allocation(project_code: str, department: str, offset_weeks: int, planning_start: date,
                    planning_end: date, user: str) -> dict[str, float | int]:
    """Move all in-range cells atomically; collisions are combined and hours preserved."""
    with connect() as conn:
        range_start = planning_start - timedelta(days=planning_start.weekday())
        range_end = planning_end - timedelta(days=planning_end.weekday())
        source = conn.execute("SELECT week_start,planned_hours FROM manager_weekly_plan WHERE project_code=? AND department=? "
                              "AND week_start BETWEEN ? AND ? AND planned_hours<>0",
                              (project_code, department, range_start.isoformat(), range_end.isoformat())).fetchall()
        moved: dict[str, float] = defaultdict(float); outside = 0.0
        for row in source:
            target = date.fromisoformat(row["week_start"]) + timedelta(weeks=offset_weeks)
            if target < range_start or target > range_end:
                outside += float(row["planned_hours"]); continue
            moved[target.isoformat()] += float(row["planned_hours"])
        if outside:
            return {"moved_hours": 0.0, "outside_hours": round(outside, 2), "rows": 0}
        conn.execute("UPDATE manager_weekly_plan SET planned_hours=0,updated_by=? WHERE project_code=? AND department=? "
                     "AND week_start BETWEEN ? AND ?", (user, project_code, department,
                     range_start.isoformat(), range_end.isoformat()))
        for target, hours in moved.items():
            conn.execute("""INSERT INTO manager_weekly_plan(project_code,department,week_start,planned_hours,updated_by)
                            VALUES (?,?,?,?,?) ON CONFLICT(project_code,department,week_start) DO UPDATE SET
                            planned_hours=excluded.planned_hours,updated_by=excluded.updated_by,updated_at=CURRENT_TIMESTAMP""",
                         (project_code, department, target, round(hours, 2), user))
        if source:
            _audit(conn,user,"Allocation moved","Weekly allocation",project_code,project_code=project_code,department=department,old=[dict(r) for r in source],new=moved,details=f"Moved {sum(moved.values()):g} h by {offset_weeks} weeks")
        increment_data_version(conn)
        return {"moved_hours": round(sum(moved.values()), 2), "outside_hours": 0.0, "rows": len(moved)}


def monthly_allocation_matrix(start: date, end: date, department: str | None = None) -> pd.DataFrame:
    """Read-only monthly aggregation of manager_weekly_plan (never another plan)."""
    params: list[Any] = [(start - timedelta(days=start.weekday())).isoformat(),
                         (end - timedelta(days=end.weekday())).isoformat()]
    where = ""
    if department and department != "All":
        where = "AND department=?"; params.append(department)
    data = pd.DataFrame(rows(f"SELECT project_code,week_start,planned_hours FROM manager_weekly_plan "
                             f"WHERE week_start BETWEEN ? AND ? {where}", params))
    months = pd.period_range(start=start, end=end, freq="M").strftime("%b %Y").tolist()
    if data.empty:
        return pd.DataFrame(columns=["Project", *months])
    data["Month"] = pd.to_datetime(data.week_start).dt.strftime("%b %Y")
    result = data.pivot_table(index="project_code", columns="Month", values="planned_hours", aggfunc="sum", fill_value=0)
    return result.reindex(columns=months, fill_value=0).reset_index().rename(columns={"project_code": "Project"})


def create_escalation(project_code: str, department: str, issue_type: str, impact_hours: float,
                      decision_required: str, owner: str, required_by: date, user: str = "") -> int:
    ensure_mvp_schema()
    if not decision_required.strip() or not owner.strip():
        raise ValueError("Decision required and owner are mandatory.")
    with connect() as conn:
        cur = conn.execute(
            """INSERT INTO planning_escalations(project_code,department,issue_type,impact_hours,
               decision_required,owner,required_by,created_by,updated_at) VALUES (?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)""",
            (project_code or None, department, issue_type, float(impact_hours), decision_required.strip(),
             owner.strip(), required_by.isoformat(), user or None),
        )
        _audit(conn, user, "Issue created", "Issue", cur.lastrowid, project_code=project_code,
               department=department, new={"owner": owner, "required_by": required_by.isoformat()},
               details=f"{issue_type}: {decision_required.strip()}")
        increment_data_version(conn)
        return int(cur.lastrowid)


def get_issues(status: str = "Open", department: str = "All", project_code: str = "All",
               owner: str = "All", issue_type: str = "All") -> pd.DataFrame:
    ensure_mvp_schema(); clauses=[]; params=[]
    for column, value in [("status", status), ("department", department), ("project_code", project_code),
                          ("owner", owner), ("issue_type", issue_type)]:
        if value and value != "All": clauses.append(f"{column}=?"); params.append(value)
    where = "WHERE " + " AND ".join(clauses) if clauses else ""
    return pd.DataFrame(rows(f"SELECT * FROM planning_escalations {where} ORDER BY CASE status WHEN 'Open' THEN 0 ELSE 1 END,required_by,created_at DESC", params))


def update_issue(issue_id: int, user: str, *, owner: str | None = None,
                 required_by: date | str | None = None, resolution: str | None = None,
                 status: str | None = None) -> None:
    ensure_mvp_schema()
    with connect() as conn:
        old = conn.execute("SELECT * FROM planning_escalations WHERE id=?", (issue_id,)).fetchone()
        if not old: raise ValueError("Issue not found.")
        new_status = status or old["status"]
        if new_status == "Closed" and not str(resolution if resolution is not None else old["resolution"] or "").strip():
            raise ValueError("A resolution is required to close an issue.")
        values = {"owner": owner if owner is not None else old["owner"],
                  "required_by": normalise_date_for_db(required_by) if required_by is not None else old["required_by"],
                  "resolution": resolution if resolution is not None else old["resolution"], "status": new_status}
        conn.execute("""UPDATE planning_escalations SET owner=?,required_by=?,resolution=?,status=?,updated_at=CURRENT_TIMESTAMP,
                     resolved_at=CASE WHEN ?='Closed' THEN COALESCE(resolved_at,CURRENT_TIMESTAMP) ELSE NULL END,
                     resolved_by=CASE WHEN ?='Closed' THEN ? ELSE NULL END WHERE id=?""",
                     (values["owner"],values["required_by"],values["resolution"],new_status,new_status,new_status,user,issue_id))
        for field in ("owner","required_by","resolution","status"):
            if str(old[field] or "") != str(values[field] or ""):
                action = "Issue closed" if field == "status" and new_status == "Closed" else "Issue reopened" if field == "status" else f"Issue {field.replace('_',' ')} changed"
                _audit(conn,user,action,"Issue",issue_id,project_code=old["project_code"],department=old["department"],field_name=field,old=old[field],new=values[field],details=f"{field.replace('_',' ').title()}: {old[field] or '—'} → {values[field] or '—'}")
        increment_data_version(conn)


def complete_planning_review(frame: pd.DataFrame, department: str, start: date, end: date, user: str) -> int:
    ensure_mvp_schema()
    unplanned = round(float(frame.get("Unplanned Hours", pd.Series(dtype=float)).sum()), 2)
    open_count = rows("SELECT COUNT(*) c FROM planning_escalations WHERE department=? AND status='Open'", (department,))[0]["c"]
    if unplanned > 0 and open_count == 0:
        raise ValueError(f"{unplanned:.1f} hours remain unplanned. Create an escalation before completing the review.")
    status = "Complete with escalations" if open_count else "Complete"
    return execute(
        """INSERT INTO planning_reviews(department,period_start,period_end,status,completed_by,open_escalations)
           VALUES (?,?,?,?,?,?)""",
        (department, start.isoformat(), end.isoformat(), status, user, open_count),
    )



@dataclass
class MvpImportResult:
    imported_people_count: int = 0
    updated_people_count: int = 0
    imported_holiday_records_count: int = 0
    unmatched_holiday_names: list[str] = field(default_factory=list)
    skipped_rows: int = 0
    validation_issues: list[str] = field(default_factory=list)
    new_absences: int = 0
    changed_absences: int = 0
    removed_absences: int = 0


def _column_map(columns: Iterable[object]) -> dict[str, str]:
    aliases = {
        "person_name": ["person_name", "name", "employee", "employee_name"],
        "department": ["department", "team", "discipline_code", "discipline"],
        "weekly_hours": ["weekly_hours", "weekly hours", "hrs", "hours", "contracted_hours"],
        "daily_hours": ["daily_hours", "daily hours"],
        "holiday_remaining_hours": ["holiday_remaining_hours", "holiday remaining hours", "remaining_holiday_hours"],
        "start_date": ["start_date", "date_from", "date from", "from", "holiday_date", "date"],
        "end_date": ["end_date", "date_to", "date to", "to"],
        "hours": ["hours", "hours_of_absence", "hours of absence", "duration_hours"],
        "days": ["days", "days_of_absence", "days of absence", "duration_days"],
        "notes": ["notes", "note", "reason"],
    }
    norm = {re.sub(r"[^a-z0-9]+", "_", str(c).strip().lower()).strip("_"): str(c) for c in columns}
    out = {}
    for target, names in aliases.items():
        for name in names:
            key = re.sub(r"[^a-z0-9]+", "_", name.strip().lower()).strip("_")
            if key in norm:
                out[target] = norm[key]
                break
    return out


def _department(value: Any) -> str | None:
    v = str(value or "").strip().upper()
    if v in DISCIPLINES:
        return v
    if "GIS" in v:
        return "GIS"
    if "PLS" in v or v.startswith("P"):
        return "PLS"
    if "RS" in v or "REMOTE" in v or "SURVEY" in v:
        return "RS"
    return None


def _number(value: Any, default: float = 0.0) -> float:
    n = pd.to_numeric(value, errors="coerce")
    return default if pd.isna(n) else float(n)


def _read_sample_table(path: str | Path | Any) -> pd.DataFrame:
    """Read a roster/holiday table from a path or Streamlit uploaded file."""
    is_upload = hasattr(path, "read")
    name = str(getattr(path, "name", path))
    suffix = Path(name).suffix.lower()
    if suffix == ".csv":
        return pd.read_csv(path)
    if is_upload:
        position = path.tell() if hasattr(path, "tell") else 0
        content = path.read()
        if hasattr(path, "seek"):
            path.seek(position)
    else:
        content = Path(path).read_bytes()
    if content.lstrip().lower().startswith(b"<table"):
        class Parser(HTMLParser):
            def __init__(self):
                super().__init__(); self.rows=[]; self.row=[]; self.buf=[]; self.cell=False
            def handle_starttag(self, tag, attrs):
                if tag == "tr": self.row=[]
                if tag in ("td", "th"): self.cell=True; self.buf=[]
            def handle_data(self, data):
                if self.cell: self.buf.append(data)
            def handle_endtag(self, tag):
                if tag in ("td", "th") and self.cell:
                    self.row.append(" ".join("".join(self.buf).split())); self.cell=False
                if tag == "tr" and self.row: self.rows.append(self.row)
        parser = Parser(); parser.feed(content.decode(errors="ignore"))
        header = parser.rows[0]
        data = [r for r in parser.rows[1:] if any(c.strip() for c in r)]
        width = len(header)
        return pd.DataFrame([r[:width] + [""] * max(width - len(r), 0) for r in data], columns=header)
    return pd.read_excel(path)


def load_roster_csv(path: str | Path | Any = "sample-data/roster.csv") -> pd.DataFrame:
    df = _read_sample_table(path)
    cmap = _column_map(df.columns)
    out = []
    for _, row in df.iterrows():
        name = str(row.get(cmap.get("person_name", ""), "")).strip()
        dept = _department(row.get(cmap.get("department", ""), ""))
        weekly = _number(row.get(cmap.get("weekly_hours", ""), None), None)
        daily = _number(row.get(cmap.get("daily_hours", ""), None), None)
        if weekly is None and daily is not None:
            weekly = daily * 5
        out.append({"person_name": name, "department": dept, "weekly_hours": weekly, "holiday_booked_hours": 0, "holiday_remaining_hours": _number(row.get(cmap.get("holiday_remaining_hours", ""), 0)), "active_status": "active"})
    return pd.DataFrame(out)


def import_sample_roster(path: str | Path | Any = "sample-data/roster.csv", user: str = "System") -> MvpImportResult:
    ensure_mvp_schema(); result = MvpImportResult(); records=[]
    existing = {r["person_name"] for r in rows("SELECT person_name FROM mvp_resources")}
    for i, r in load_roster_csv(path).iterrows():
        if not r.get("person_name"):
            result.skipped_rows += 1; result.validation_issues.append(f"row {i+2}: missing person name"); continue
        if pd.isna(r.get("department")) or r.get("department") not in DISCIPLINES:
            result.skipped_rows += 1; result.validation_issues.append(f"{r.get('person_name')}: missing department"); continue
        if pd.isna(r.get("weekly_hours")) or r.get("weekly_hours") is None or float(r.get("weekly_hours") or 0) <= 0:
            result.validation_issues.append(f"{r.get('person_name')}: missing weekly hours")
        records.append(r.to_dict())
        if r["person_name"] in existing: result.updated_people_count += 1
        else: result.imported_people_count += 1
    save_resources(records, user)
    return result


def _parse_date(value: Any) -> date | None:
    # ISO database dates are unambiguous and must not be reinterpreted as
    # day-first (for example 2026-09-10 becoming 9 October).
    if isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value.strip()):
        return date.fromisoformat(value.strip())
    parsed = pd.to_datetime(value, errors="coerce", dayfirst=True)
    return None if pd.isna(parsed) else parsed.date()


def _working_days(start: date, end: date) -> list[date]:
    out=[]; cur=start
    while cur <= end:
        if cur.weekday() < 5: out.append(cur)
        cur += timedelta(days=1)
    return out


def parse_employee_identity(value: Any) -> tuple[str | None, str]:
    raw = " ".join(str(value or "").split())
    found = re.search(r"\((\d+)\)\s*$", raw)
    employee_id = found.group(1) if found else None
    name = re.sub(r"\s*\(\d+\)\s*$", "", raw).strip()
    if "," in name:
        family, given = [part.strip() for part in name.split(",", 1)]
        name = f"{given} {family}".strip()
    return employee_id, name


def _normalized_employee_name(value: Any) -> str:
    """Return a comparable employee name, excluding a trailing employee ID."""
    _, name = parse_employee_identity(value)
    return " ".join(name.casefold().split())


def resolve_resource_for_employee(
    employee_value: Any,
    resources: list[Any],
    employee_id_mappings: dict[str, Any],
) -> Any | None:
    """Resolve an HR employee using saved ID, embedded ID, then exact name.

    Embedded IDs and normalized names only resolve when they identify exactly one
    resource.  This keeps the name fallback deterministic and avoids silently
    assigning holidays where roster data is duplicated.
    """
    employee_id, _ = parse_employee_identity(employee_value)
    if employee_id and employee_id in employee_id_mappings:
        return employee_id_mappings[employee_id]

    if employee_id:
        id_matches = [
            resource for resource in resources
            if parse_employee_identity(resource["person_name"])[0] == employee_id
        ]
        if len(id_matches) == 1:
            return id_matches[0]

    normalized_name = _normalized_employee_name(employee_value)
    if not normalized_name:
        return None
    name_matches = [
        resource for resource in resources
        if _normalized_employee_name(resource["person_name"]) == normalized_name
    ]
    return name_matches[0] if len(name_matches) == 1 else None


def preview_holiday_snapshot(path: str | Path | Any) -> dict[str, Any]:
    """Parse and diff an approved-HR snapshot without changing SQLite."""
    ensure_mvp_schema(); df = _read_sample_table(path); cmap = _column_map(df.columns)
    resources = rows("SELECT * FROM mvp_resources")
    id_map = {r["employee_id"]: r for r in rows(
        "SELECT m.employee_id,r.* FROM resource_employee_ids m JOIN mvp_resources r ON r.id=m.resource_id")}
    desired: dict[tuple[int, str], dict] = {}; unmatched=[]; issues=[]; mappings=[]
    for i, row in df.iterrows():
        raw = row.get(cmap.get("person_name", ""), "")
        employee_id, canonical_name = parse_employee_identity(raw)
        person = resolve_resource_for_employee(raw, resources, id_map)
        if not person:
            unmatched.append(str(raw).strip()); continue
        if employee_id:
            mappings.append((employee_id, person["id"], canonical_name))
        start = _parse_date(row.get(cmap.get("start_date", "")))
        end = _parse_date(row.get(cmap.get("end_date", ""))) or start
        if not start:
            issues.append(f"row {i + 2}: missing holiday date"); continue
        workdays = _working_days(start, end)
        duration_days = _number(row.get(cmap.get("days", ""), None), None)
        total_hours = _number(row.get(cmap.get("hours", ""), None), None)
        if total_hours is None:
            total_hours = (duration_days if duration_days is not None else len(workdays)) * float(person["weekly_hours"] or 0) / 5
        per_day = round(total_hours / max(len(workdays), 1), 2)
        for day in workdays:
            desired[(person["id"], day.isoformat())] = {
                "resource_id": person["id"], "person_name": person["person_name"], "employee_id": employee_id,
                "holiday_date": day.isoformat(), "hours": per_day,
                "notes": str(row.get(cmap.get("notes", ""), "")).strip(),
            }
    current = {(r["resource_id"], r["holiday_date"]): dict(r) for r in rows(
        "SELECT * FROM holidays WHERE source='hr-approved' AND status='active'")}
    new = [v for k, v in desired.items() if k not in current]
    changed = [v for k, v in desired.items() if k in current and
               (round(float(current[k]["hours"]), 2) != v["hours"] or (current[k].get("employee_id") or None) != v["employee_id"])]
    removed = [v for k, v in current.items() if k not in desired]
    return {"records": list(desired.values()), "new": new, "changed": changed, "removed": removed,
            "unmatched": sorted(set(unmatched)), "issues": issues, "mappings": mappings}


def apply_holiday_snapshot(preview: dict[str, Any], filename: str, user: str) -> MvpImportResult:
    """Synchronise active HR holidays and retain removed rows as cancelled audit data."""
    import json
    result = MvpImportResult(unmatched_holiday_names=preview["unmatched"], skipped_rows=len(preview["unmatched"]),
                             validation_issues=preview["issues"], new_absences=len(preview["new"]),
                             changed_absences=len(preview["changed"]), removed_absences=len(preview["removed"]))
    with connect() as conn:
        cur = conn.execute("INSERT INTO holiday_imports(filename,imported_by,record_count,unmatched_count,summary_json) VALUES (?,?,?,?,?)",
                           (filename, user, len(preview["records"]), len(preview["unmatched"]), json.dumps({k: len(preview[k]) for k in ("new","changed","removed")})))
        import_id = cur.lastrowid
        for employee_id, resource_id, name in preview["mappings"]:
            conn.execute("INSERT INTO resource_employee_ids(employee_id,resource_id,employee_name) VALUES (?,?,?) "
                         "ON CONFLICT(employee_id) DO UPDATE SET resource_id=excluded.resource_id,employee_name=excluded.employee_name,updated_at=CURRENT_TIMESTAMP",
                         (employee_id, resource_id, name))
        for old in preview["removed"]:
            conn.execute("UPDATE holidays SET status='cancelled',import_id=? WHERE id=?", (import_id, old["id"]))
        for record in preview["records"]:
            existing = conn.execute("SELECT id FROM holidays WHERE resource_id=? AND holiday_date=? AND source='hr-approved' ORDER BY id DESC LIMIT 1",
                                    (record["resource_id"], record["holiday_date"])).fetchone()
            if existing:
                conn.execute("UPDATE holidays SET person_name=?,employee_id=?,hours=?,notes=?,status='active',import_id=? WHERE id=?",
                             (record["person_name"], record["employee_id"], record["hours"], record["notes"], import_id, existing["id"]))
            else:
                conn.execute("INSERT INTO holidays(resource_id,person_name,employee_id,holiday_date,hours,source,notes,status,import_id) VALUES (?,?,?,?,?,'hr-approved',?,'active',?)",
                             (record["resource_id"], record["person_name"], record["employee_id"], record["holiday_date"], record["hours"], record["notes"], import_id))
        result.imported_holiday_records_count = len(preview["records"])
        _audit(conn,user,"Holiday snapshot imported","Holiday import",import_id,
               new={"records":len(preview["records"]),"new":len(preview["new"]),"changed":len(preview["changed"]),"cancelled":len(preview["removed"]),"unmatched":len(preview["unmatched"])},
               details=f"{len(preview['records'])} records · {len(preview['new'])} new · {len(preview['changed'])} changed · {len(preview['removed'])} cancelled · {len(preview['unmatched'])} unmatched")
        increment_data_version(conn)
    recalculate_holiday_totals()
    return result


def import_approved_holidays(path: str | Path | Any = "sample-data/Employee Holiday - Approved - From 01_01_2026 to 31_12_2026 .xls") -> MvpImportResult:
    preview = preview_holiday_snapshot(path)
    return apply_holiday_snapshot(preview, str(getattr(path, "name", path)), "System")


def recalculate_holiday_totals() -> int:
    ensure_mvp_schema()
    with connect() as conn:
        conn.execute("UPDATE mvp_resources SET holiday_booked_hours=COALESCE((SELECT SUM(hours) FROM holidays h WHERE h.resource_id=mvp_resources.id AND COALESCE(h.status,'active')='active'),0), updated_at=datetime('now')")
        changed = conn.execute("SELECT changes() c").fetchone()["c"]
        if changed:
            increment_data_version(conn)
        return changed


def get_holidays(department: str | None = None, person_name: str | None = None, start_date: str | None = None, end_date: str | None = None) -> pd.DataFrame:
    ensure_mvp_schema(); wh=[]; params=[]
    if department and department != "All": wh.append("r.department=?"); params.append(department)
    if person_name and person_name != "All": wh.append("h.person_name=?"); params.append(person_name)
    if start_date: wh.append("h.holiday_date>=?"); params.append(start_date)
    if end_date: wh.append("h.holiday_date<=?"); params.append(end_date)
    where = "WHERE " + " AND ".join(wh) if wh else ""
    return pd.DataFrame(rows(f"SELECT h.id,h.person_name,r.department,h.holiday_date,h.hours,h.source,COALESCE(h.status,'active') status,h.notes FROM holidays h LEFT JOIN mvp_resources r ON r.id=h.resource_id {where} ORDER BY h.holiday_date,h.person_name", tuple(params)))


def save_holidays(records: Iterable[dict]) -> None:
    ensure_mvp_schema()
    with connect() as conn:
        for r in records:
            name=str(r.get("person_name") or "").strip(); hdate=normalise_date_for_db(r.get("holiday_date"))
            if not name or not hdate: continue
            person=conn.execute("SELECT id FROM mvp_resources WHERE lower(person_name)=lower(?)", (name,)).fetchone()
            conn.execute("INSERT OR REPLACE INTO holidays(id,resource_id,person_name,holiday_date,hours,source,notes) VALUES (?,?,?,?,?,?,?)", (r.get("id"), person["id"] if person else None, name, hdate, float(r.get("hours") or 0), r.get("source") or "manual", r.get("notes")))
    recalculate_holiday_totals()
    increment_data_version()

def save_resources(records: Iterable[dict], user: str = "System") -> None:
    ensure_mvp_schema()
    with connect() as conn:
        for r in records:
            name = str(r.get("person_name") or "").strip()
            dept = str(r.get("department") or "RS").strip().upper()

            if not name or dept not in DISCIPLINES:
                continue

            status = str(r.get("active_status") or "active").lower()
            old = conn.execute("SELECT * FROM mvp_resources WHERE person_name=?", (name,)).fetchone()

            conn.execute(
                """
                INSERT INTO mvp_resources(
                    person_name,
                    department,
                    weekly_hours,
                    holiday_booked_hours,
                    holiday_remaining_hours,
                    active_status,
                    status_reason,
                    status_start_date,
                    status_end_date,
                    updated_at
                )
                VALUES (?,?,?,?,?,?,?,?,?,datetime('now'))
                ON CONFLICT(person_name) DO UPDATE SET
                    department=excluded.department,
                    weekly_hours=excluded.weekly_hours,
                    holiday_booked_hours=excluded.holiday_booked_hours,
                    holiday_remaining_hours=excluded.holiday_remaining_hours,
                    active_status=excluded.active_status,
                    status_reason=excluded.status_reason,
                    status_start_date=excluded.status_start_date,
                    status_end_date=excluded.status_end_date,
                    updated_at=datetime('now')
                """,
                (
                    name,
                    dept,
                    float(r.get("weekly_hours") or 0),
                    float(r.get("holiday_booked_hours") or 0),
                    float(r.get("holiday_remaining_hours") or 0),
                    status,
                    r.get("status_reason"),
                    normalise_date_for_db(r.get("status_start_date")),
                    normalise_date_for_db(r.get("status_end_date")),
                ),
            )
            current=conn.execute("SELECT * FROM mvp_resources WHERE person_name=?",(name,)).fetchone()
            changed = not old or any(str(old[k] or "") != str(current[k] or "") for k in RESOURCE_FIELDS)
            if changed:
                _audit(conn,user,"Resource created" if not old else "Resource updated","Resource",current["id"],department=dept,old=dict(old) if old else None,new=dict(current),details=f"{'Created' if not old else 'Updated'} {name}")
        increment_data_version(conn)


def get_resources() -> pd.DataFrame:
    ensure_mvp_schema()
    return pd.DataFrame(
        rows(
            f"""
            SELECT id,{",".join(RESOURCE_FIELDS)}
            FROM mvp_resources
            ORDER BY department, person_name
            """
        )
    )


def seed_resources_from_people() -> int:
    ensure_mvp_schema()

    existing = rows("SELECT COUNT(*) c FROM mvp_resources")[0]["c"]
    if existing:
        return 0

    people = rows(
        """
        SELECT
            p.name person_name,
            d.code department,
            p.weekly_hours
        FROM people p
        JOIN disciplines d ON d.id=p.discipline_id
        ORDER BY p.name
        """
    )

    save_resources(
        [
            {
                **p,
                "holiday_booked_hours": 0,
                "holiday_remaining_hours": 0,
                "active_status": "active",
            }
            for p in people
        ]
    )
    return len(people)


def setting_float(key: str, default: float) -> float:
    ensure_mvp_schema()
    r = rows("SELECT value FROM settings WHERE key=?", (key,))
    return float(r[0]["value"]) if r else default


def department_for_resource(resource_id: int, default: str, week: date) -> str:
    r = rows(
        """
        SELECT department
        FROM resource_department_assignments
        WHERE resource_id=?
          AND start_date<=?
          AND (end_date IS NULL OR end_date>=?)
        ORDER BY start_date DESC
        LIMIT 1
        """,
        (resource_id, week.isoformat(), week.isoformat()),
    )
    return r[0]["department"] if r else default


def resource_active_for_week(resource: dict, week: date) -> bool:
    status = resource.get("active_status") or "active"
    if status == "active":
        return True

    start = pd.to_datetime(resource.get("status_start_date"), errors="coerce")
    end = pd.to_datetime(resource.get("status_end_date"), errors="coerce")
    ws = pd.Timestamp(week)
    we = pd.Timestamp(week + timedelta(days=6))

    return not ((pd.isna(start) or start <= we) and (pd.isna(end) or end >= ws))


def resource_availability_matrix(weeks: list[date]) -> pd.DataFrame:
    """Calculate employee availability and departmental contributions in one batched pass."""
    ensure_mvp_schema()
    resources = rows("SELECT * FROM mvp_resources")
    adjustments = rows("SELECT * FROM resource_capacity_adjustments WHERE active=1")
    assignments = rows("SELECT * FROM resource_department_assignments")
    holiday_rows = rows("SELECT resource_id, holiday_date, SUM(hours) hours FROM holidays "
                        "WHERE COALESCE(status,'active')='active' GROUP BY resource_id,holiday_date")
    holiday_by_resource_day: dict[tuple[int, str], float] = defaultdict(float)
    for h in holiday_rows:
        parsed = pd.to_datetime(h["holiday_date"], errors="coerce")
        if not pd.isna(parsed): holiday_by_resource_day[(h["resource_id"], parsed.date().isoformat())] += float(h["hours"] or 0)

    adjustments_by_resource: dict[int, list[dict]] = defaultdict(list)
    for adjustment in adjustments:
        adjustments_by_resource[adjustment["resource_id"]].append(dict(adjustment))
    assignments_by_resource: dict[int, list[dict]] = defaultdict(list)
    for assignment in assignments: assignments_by_resource[assignment["resource_id"]].append(dict(assignment))
    out=[]
    for raw_resource in resources:
        r=dict(raw_resource)
        weekly_hours=max(float(r["weekly_hours"] or 0),0); daily_baseline=weekly_hours/5
        for w in weeks:
            contributions=defaultdict(float); holiday_total=unavailable_total=reduction_total=0.0; reasons=[]
            for day_index in range(5):
                day=w+timedelta(days=day_index)
                current_department=r["department"]
                for assignment in assignments_by_resource.get(r["id"],[]):
                    if _parse_date(assignment["start_date"]) <= day and (not assignment["end_date"] or day <= _parse_date(assignment["end_date"])):
                        current_department=assignment["department"]
                status=str(r.get("active_status") or "active")
                status_start=_parse_date(r.get("status_start_date")); status_end=_parse_date(r.get("status_end_date"))
                unavailable=status != "active" and (not status_start or day >= status_start) and (not status_end or day <= status_end)
                if unavailable:
                    unavailable_total += daily_baseline
                    continue
                available=daily_baseline
                holiday=min(holiday_by_resource_day.get((r["id"],day.isoformat()),0),available)
                holiday_total+=holiday; available-=holiday
                applicable=[a for a in adjustments_by_resource.get(r["id"],[]) if _parse_date(a["start_date"])<=day<=_parse_date(a["end_date"])]
                requests=[]
                for adjustment in applicable:
                    requested=(weekly_hours*float(adjustment["capacity_percent"])/100/5 if adjustment["capacity_percent"] is not None else float(adjustment["hours_per_week"] or 0)/5)
                    requests.append((adjustment,max(requested,0)))
                    if adjustment.get("reason"): reasons.append(str(adjustment["reason"]))
                requested_total=sum(amount for _,amount in requests); scale=min(1.0,available/requested_total) if requested_total else 1.0
                moved=0.0
                for adjustment,requested in requests:
                    amount=requested*scale
                    if adjustment["destination_department"]:
                        contributions[adjustment["destination_department"]]+=amount; moved+=amount
                    else: reduction_total+=amount
                contributions[current_department]+=max(available-min(requested_total,available),0)
            assignment_parts=[f"{hours:g} {dept}" for dept,hours in contributions.items() if hours>0.005]
            out.append({"resource_id":r["id"],"Employee":r["person_name"],"Home Department":r["department"],
                        "Weekly Hours":weekly_hours,"week_start":w.isoformat(),"Available Hours":round(sum(contributions.values()),2),
                        "Department Contributions":dict(contributions),"Availability": " / ".join(assignment_parts) if assignment_parts else "0",
                        "Holiday Hours":round(holiday_total,2),"Unavailable Hours":round(unavailable_total,2),
                        "Other Reduction Hours":round(reduction_total,2),"Reasons":", ".join(sorted(set(reasons)))})
    return pd.DataFrame(out)


def weekly_department_capacity(weeks: list[date]) -> pd.DataFrame:
    """Aggregate the same employee-level engine used by Resource Management."""
    detail=resource_availability_matrix(weeks); totals=[]
    for week in weeks:
        week_rows=detail[detail.week_start==week.isoformat()] if not detail.empty else pd.DataFrame()
        for department in DISCIPLINES:
            hours=sum(float(row.get("Department Contributions",{}).get(department,0)) for row in week_rows.to_dict("records"))
            totals.append({"week_start":week.isoformat(),"department":department,"available_capacity":round(hours,2)})
    return pd.DataFrame(totals)


def get_capacity_adjustments(include_inactive: bool = True) -> pd.DataFrame:
    """Return operational adjustments with baseline roster facts and display status."""
    ensure_mvp_schema()
    where = "" if include_inactive else "WHERE a.active=1"
    frame = pd.DataFrame(rows(f"""SELECT a.*,r.person_name,r.department AS home_department,
        r.weekly_hours FROM resource_capacity_adjustments a JOIN mvp_resources r ON r.id=a.resource_id
        {where} ORDER BY a.start_date DESC,a.id DESC"""))
    if not frame.empty:
        today = date.today().isoformat()
        frame["period_status"] = frame.apply(
            lambda r: "Inactive" if not bool(r.active) else "Upcoming" if r.start_date > today
            else "Expired" if r.end_date < today else "Active", axis=1)
    return frame


def save_capacity_adjustment(record: dict, user: str) -> int:
    """Create/update an audited adjustment; ambiguous overlapping capacity is rejected."""
    ensure_mvp_schema()
    resource_id = int(record.get("resource_id") or 0)
    start = normalise_date_for_db(record.get("start_date")); end = normalise_date_for_db(record.get("end_date"))
    kind = str(record.get("adjustment_type") or "").strip()
    destination = str(record.get("destination_department") or "").strip().upper() or None
    percent = record.get("capacity_percent"); hours = record.get("hours_per_week")
    percent = None if percent in (None, "") else float(percent)
    hours = None if hours in (None, "") else float(hours)
    if not resource_id or not start or not end or start > end: raise ValueError("Resource and a valid date range are required.")
    if not kind: raise ValueError("Adjustment type is required.")
    if destination and destination not in DISCIPLINES: raise ValueError("Destination department is invalid.")
    if (percent is None) == (hours is None): raise ValueError("Enter either capacity percentage or hours per week, not both.")
    resource = rows("SELECT * FROM mvp_resources WHERE id=?", (resource_id,))
    if not resource: raise ValueError("Resource not found.")
    if destination == resource[0]["department"]: raise ValueError("Destination must differ from the home department.")
    requested_percent = percent if percent is not None else hours / max(float(resource[0]["weekly_hours"]), .001) * 100
    if requested_percent <= 0 or requested_percent > 100: raise ValueError("Adjustment must be greater than 0 and no more than 100% of contracted capacity.")
    adjustment_id = record.get("id")
    overlaps = rows("""SELECT * FROM resource_capacity_adjustments WHERE resource_id=? AND active=1
        AND id<>? AND start_date<=? AND end_date>=?""", (resource_id, int(adjustment_id or 0), end, start))
    for day in _working_days(date.fromisoformat(start), date.fromisoformat(end)) if record.get("active", True) else []:
        total = requested_percent
        for overlap in overlaps:
            if _parse_date(overlap["start_date"]) <= day <= _parse_date(overlap["end_date"]):
                total += (float(overlap["capacity_percent"]) if overlap["capacity_percent"] is not None
                          else float(overlap["hours_per_week"] or 0) / max(float(resource[0]["weekly_hours"]), .001) * 100)
        if total > 100.0001: raise ValueError(f"Active adjustments exceed 100% of capacity on {day.isoformat()}.")
    values = (resource_id,kind,destination,start,end,percent,hours,str(record.get("reason") or "").strip(),
              1 if record.get("active", True) else 0,user)
    with connect() as conn:
        old = conn.execute("SELECT * FROM resource_capacity_adjustments WHERE id=?", (adjustment_id,)).fetchone() if adjustment_id else None
        if old:
            conn.execute("""UPDATE resource_capacity_adjustments SET resource_id=?,adjustment_type=?,destination_department=?,
                start_date=?,end_date=?,capacity_percent=?,hours_per_week=?,reason=?,active=?,created_by=?,updated_at=CURRENT_TIMESTAMP WHERE id=?""", (*values, adjustment_id))
            row_id = int(adjustment_id); action = "Temporary adjustment edited"
        else:
            cur = conn.execute("""INSERT INTO resource_capacity_adjustments(resource_id,adjustment_type,destination_department,
                start_date,end_date,capacity_percent,hours_per_week,reason,active,created_by) VALUES (?,?,?,?,?,?,?,?,?,?)""", values)
            row_id = int(cur.lastrowid); action = "Temporary adjustment created"
        new = dict(conn.execute("SELECT * FROM resource_capacity_adjustments WHERE id=?", (row_id,)).fetchone())
        details = f"{resource[0]['person_name']} · {kind} · {start} to {end} · {resource[0]['department']} → {destination or 'unavailable'} · {percent if percent is not None else str(hours)+' h/week'} · {new['reason'] or 'No reason'}"
        _audit(conn,user,action,"Resource capacity adjustment",row_id,department=resource[0]["department"],old=dict(old) if old else None,new=new,details=details)
        increment_data_version(conn)
        return row_id


def sequence_analysis(weeks: list[date], gap_threshold_days: int = SEQUENCE_GAP_THRESHOLD_DAYS) -> pd.DataFrame:
    """Conservative diagnostics based only on explicit manager allocations."""
    columns = ["Category","Project","Upstream","Downstream","Upstream last week","Downstream first week",
               "Gap days","Data available","Required By","Remaining Hours","Spare capacity in gap","Suggestion"]
    if not weeks: return pd.DataFrame(columns=columns)
    projects = get_projects(False)
    allocations = pd.DataFrame(rows("SELECT project_code,department,week_start,planned_hours FROM manager_weekly_plan WHERE planned_hours>0"))
    if projects.empty or allocations.empty: return pd.DataFrame(columns=columns)
    balance = capacity_balance(weeks)
    findings=[]
    for project in projects.to_dict("records"):
        pa = allocations[allocations.project_code == project["project_code"]]
        for upstream, downstream in (("RS","GIS"),("GIS","PLS")):
            up=pa[pa.department==upstream]; down=pa[pa.department==downstream]
            if up.empty or down.empty: continue
            up_last=date.fromisoformat(up.week_start.max()); down_first=date.fromisoformat(down.week_start.min())
            data_available=_parse_date(project.get(f"{downstream.lower()}_start_date")); gap=(down_first-(up_last+timedelta(days=7))).days
            remaining=max(float(project.get(f"{downstream.lower()}_hours") or 0)-float(project.get(f"actual_{downstream.lower()}_hours") or 0),0)
            gap_weeks=[w for w in weeks if up_last < w < down_first and (not data_available or w>=monday_date(data_available))]
            spare=sum(max(float(balance[(balance.department==downstream)&(balance.week_start==w.isoformat())].over_under_capacity.sum()),0) for w in gap_weeks)
            base={"Project":f"{project['project_code']} · {project['project_name']}","Upstream":upstream,"Downstream":downstream,
                  "Upstream last week":up_last,"Downstream first week":down_first,"Data available":data_available,
                  "Required By":_parse_date(project.get("end_date")),"Remaining Hours":round(remaining,2),"Spare capacity in gap":round(spare,2)}
            if gap >= gap_threshold_days:
                findings.append({**base,"Category":"Gap","Gap days":gap,"Suggestion":f"Review whether {downstream} can start earlier."})
                if spare > 0 and remaining > 0:
                    findings.append({**base,"Category":"Downstream starvation","Gap days":gap,"Suggestion":f"{downstream} has spare capacity while waiting for the planned hand-off."})
                    findings.append({**base,"Category":"Pull-forward opportunity","Gap days":gap,"Suggestion":f"Consider pulling ready {downstream} work forward; no allocation has been changed."})
            overlap_days=(up_last-down_first).days
            if overlap_days >= gap_threshold_days and (not data_available or data_available > down_first):
                findings.append({**base,"Category":"Possible overlap","Gap days":-overlap_days,"Suggestion":f"Review whether {downstream} can begin before the upstream hand-off/data date."})
    return pd.DataFrame(findings,columns=columns)


def monday_date(value: date) -> date:
    return value - timedelta(days=value.weekday())


def weekly_project_demand() -> pd.DataFrame:
    projects = get_projects(False)
    out = []

    if projects.empty:
        return pd.DataFrame()

    for p in projects.to_dict("records"):
        end_date = pd.to_datetime(p.get("end_date"), errors="coerce")
        if pd.isna(end_date):
            continue

        for d in DISCIPLINES:
            hours_key = f"{d.lower()}_hours"
            start_key = f"{d.lower()}_start_date"

            hrs = float(p.get(hours_key) or 0)
            start_date = pd.to_datetime(
                p.get(start_key) or p.get("start_date"), errors="coerce"
            )

            if pd.isna(start_date):
                continue

            weeks = week_starts(start_date.date(), end_date.date())
            loading_type = normalize_loading_type(p.get("loading_type"))

            if loading_type == "manual":
                vals = [0.0] * len(weeks)
            else:
                vals = spread_hours(hrs, weeks, loading_type)

            for w, v in zip(weeks, vals):
                out.append(
                    {
                        "project_code": p["project_code"],
                        "project_name": p["project_name"],
                        "department": d,
                        "week_start": w.isoformat(),
                        "demand_hours": v,
                        "manual_required": loading_type == "manual",
                    }
                )

    return pd.DataFrame(out)



def project_timeline_label(project: dict) -> str:
    """Return the Allocations timeline label for a project."""
    parts = [
        str(project.get("project_code") or "").strip(),
        str(project.get("client") or "").strip(),
        str(project.get("project_name") or "").strip(),
    ]
    return " | ".join(part for part in parts if part)


def gantt_capacity_status(department: str, start_date: date, end_date: date, bal: pd.DataFrame) -> str:
    if bal.empty or not {"week_start", "department", "available_capacity", "allocated_demand"}.issubset(bal.columns):
        return "grey"
    sub = bal[(bal["department"] == department) & (pd.to_datetime(bal["week_start"]).dt.date >= (start_date - timedelta(days=start_date.weekday()))) & (pd.to_datetime(bal["week_start"]).dt.date <= end_date)]
    if (sub["allocated_demand"] > sub["available_capacity"]).any():
        return "red"
    if sub.empty or float(sub["available_capacity"].sum() or 0) <= 0:
        return "grey"
    utilisation = float(sub["allocated_demand"].sum() or 0) / float(sub["available_capacity"].sum() or 1)
    if utilisation >= CAPACITY_RISK_UTILISATION:
        return "amber"
    return "green"


def gantt_timeline_rows(projects: pd.DataFrame, demand: pd.DataFrame, bal: pd.DataFrame, planning_start: date, planning_end: date, selected_departments: list[str] | None = None) -> pd.DataFrame:
    """Build one Gantt row per project/discipline with non-zero required hours."""
    departments = selected_departments or DISCIPLINES
    out = []
    if projects.empty:
        return pd.DataFrame()
    for p in projects.to_dict("records"):
        project_end = pd.to_datetime(p.get("end_date"), errors="coerce")
        if pd.isna(project_end):
            continue
        project_end_date = project_end.date()
        label = project_timeline_label(p)
        for d in departments:
            hours = float(p.get(f"{d.lower()}_hours") or 0)
            if hours <= 0:
                continue
            discipline_start = pd.to_datetime(p.get(f"{d.lower()}_start_date") or p.get("start_date"), errors="coerce")
            if pd.isna(discipline_start):
                continue
            start_date = discipline_start.date()
            if project_end_date < planning_start or start_date > planning_end:
                continue
            clipped_start = max(start_date, planning_start)
            clipped_end = min(project_end_date, planning_end)
            if clipped_end < clipped_start:
                continue
            weekly_demand = 0.0
            if not demand.empty and {"project_code", "department", "week_start", "demand_hours"}.issubset(demand.columns):
                sub = demand[(demand["project_code"] == p.get("project_code")) & (demand["department"] == d)]
                sub = sub[(pd.to_datetime(sub["week_start"]).dt.date >= (planning_start - timedelta(days=planning_start.weekday()))) & (pd.to_datetime(sub["week_start"]).dt.date <= planning_end)]
                active_weeks = max(len(sub.index), 1)
                weekly_demand = round(float(sub["demand_hours"].sum() or 0) / active_weeks, 2)
            status = gantt_capacity_status(d, clipped_start, clipped_end, bal)
            out.append({
                "project_label": label,
                "project_code": p.get("project_code"),
                "project_name": p.get("project_name"),
                "discipline": d,
                "start": clipped_start,
                "end": clipped_end,
                "source_start": start_date,
                "source_end": project_end_date,
                "required_hours": hours,
                "loading_type": p.get("loading_type"),
                "total_weekly_demand": weekly_demand,
                "capacity_status": status,
            })
    return pd.DataFrame(out)


def capacity_summary_cards(bal: pd.DataFrame, demand: pd.DataFrame, projects: pd.DataFrame, planning_start: date, planning_end: date) -> dict[str, float]:
    summary: dict[str, float] = {}
    for d in DISCIPLINES:
        if bal.empty or not {"department", "over_under_capacity"}.issubset(bal.columns):
            value = 0.0
        else:
            sub = bal[bal["department"] == d]
            value = round(float(sub["over_under_capacity"].sum() or 0), 2)
        summary[f"{d} over_under_hours"] = value
    active = 0 if projects.empty else len(projects.index)
    total_required = 0.0
    if not demand.empty and {"week_start", "demand_hours"}.issubset(demand.columns):
        sub = demand[(pd.to_datetime(demand["week_start"]).dt.date >= (planning_start - timedelta(days=planning_start.weekday()))) & (pd.to_datetime(demand["week_start"]).dt.date <= planning_end)]
        total_required = round(float(sub["demand_hours"].sum() or 0), 2)
    summary["total_active_projects"] = active
    summary["total_required_hours"] = total_required
    return summary


def summary_rows_from_capacity_balance(bal: pd.DataFrame, week_cols: list[str]) -> pd.DataFrame:
    required = {
        "week_start",
        "department",
        "available_capacity",
        "allocated_demand",
        "over_under_capacity",
    }
    if bal.empty or not required.issubset(bal.columns):
        bal = pd.DataFrame(
            [
                {
                    "week_start": wc,
                    "department": d,
                    "available_capacity": 0.0,
                    "allocated_demand": 0.0,
                    "over_under_capacity": 0.0,
                }
                for d in DISCIPLINES
                for wc in week_cols
            ]
        )

    summary = []
    for d in DISCIPLINES:
        for label, col in [
            ("available capacity", "available_capacity"),
            ("allocated demand", "allocated_demand"),
            ("over/under capacity", "over_under_capacity"),
        ]:
            row = {"Summary": f"{d} {label}"}
            for wc in week_cols:
                row[wc] = float(
                    bal.loc[(bal.department == d) & (bal.week_start == wc), col].sum()
                )
            summary.append(row)
    return pd.DataFrame(summary)


def save_internal_activities(records: Iterable[dict], user: str = "System") -> None:
    ensure_mvp_schema()
    with connect() as conn:
        for record in records:
            start = normalise_date_for_db(record.get("start_week")); end = normalise_date_for_db(record.get("end_week"))
            if not record.get("activity_name") or record.get("department") not in DISCIPLINES or not start or not end:
                continue
            values = (record["activity_name"], record["department"], start, end,
                      max(float(record.get("planned_hours_per_week") or 0), 0), int(bool(record.get("active", True))), record.get("notes"))
            if record.get("id"):
                old=conn.execute("SELECT * FROM internal_activities WHERE id=?",(int(record["id"]),)).fetchone()
                conn.execute("UPDATE internal_activities SET activity_name=?,department=?,start_week=?,end_week=?,planned_hours_per_week=?,active=?,notes=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                             (*values, int(record["id"])))
                if old and any(str(old[k] or "") != str(v or "") for k,v in zip(("activity_name","department","start_week","end_week","planned_hours_per_week","active","notes"),values)):
                    _audit(conn,user,"Internal activity updated","Internal activity",record["id"],department=record["department"],old=dict(old),new=dict(record),details=f"Updated {record['activity_name']}")
            else:
                cur=conn.execute("INSERT INTO internal_activities(activity_name,department,start_week,end_week,planned_hours_per_week,active,notes) VALUES (?,?,?,?,?,?,?)", values)
                _audit(conn,user,"Internal activity created","Internal activity",cur.lastrowid,department=record["department"],new=dict(record),details=f"Created {record['activity_name']}")
        increment_data_version(conn)


def get_internal_activities() -> pd.DataFrame:
    ensure_mvp_schema()
    return pd.DataFrame(rows("SELECT id,activity_name,department,start_week,end_week,planned_hours_per_week,active,notes FROM internal_activities ORDER BY active DESC,start_week,activity_name"))


def internal_activity_by_week(weeks: list[date]) -> pd.DataFrame:
    activities = get_internal_activities(); output=[]
    for week in weeks:
        for department in DISCIPLINES:
            hours = 0.0
            if not activities.empty:
                mask = ((activities.department == department) & (activities.active.astype(bool)) &
                        (activities.start_week <= week.isoformat()) & (activities.end_week >= week.isoformat()))
                hours = float(activities.loc[mask, "planned_hours_per_week"].sum())
            output.append({"week_start": week.isoformat(), "department": department, "internal_hours": round(hours, 2)})
    return pd.DataFrame(output)


def manager_allocations(weeks: list[date]) -> pd.DataFrame:
    if not weeks:
        return pd.DataFrame(columns=["project_code", "department", "week_start", "planned_hours"])
    return pd.DataFrame(rows("SELECT project_code,department,week_start,planned_hours FROM manager_weekly_plan "
                             "WHERE week_start BETWEEN ? AND ?",
                             (weeks[0].isoformat(), weeks[-1].isoformat())))


def project_health(remaining_hours: float, allocated_before_deadline: float,
                   tolerance: float = PROJECT_HEALTH_TOLERANCE_HOURS) -> str:
    """Classify delivery coverage, independently of departmental capacity."""
    remaining = max(float(remaining_hours or 0), 0)
    allocated = max(float(allocated_before_deadline or 0), 0)
    if remaining > tolerance and allocated <= tolerance:
        return "Unplanned"
    difference = allocated - remaining
    if difference < -tolerance:
        return "Under-resourced"
    if difference > tolerance:
        return "Over-resourced"
    return "Well-resourced"


def project_health_plans(as_of: date | None = None, department: str | None = None) -> pd.DataFrame:
    """Derive one explainable status per active project/discipline in bulk."""
    as_of = as_of or date.today()
    as_of = as_of - timedelta(days=as_of.weekday())
    projects = get_projects(False)
    allocations = pd.DataFrame(rows("SELECT project_code,department,week_start,planned_hours FROM manager_weekly_plan WHERE planned_hours>0 AND week_start>=?", (as_of.isoformat(),)))
    output=[]
    for p in projects.to_dict("records"):
        for disc in ([department] if department and department != "All" else DISCIPLINES):
            remaining=max(float(p.get(f"{disc.lower()}_hours") or 0)-float(p.get(f"actual_{disc.lower()}_hours") or 0),0)
            if remaining <= 0: continue
            sub=allocations[(allocations.project_code==p["project_code"]) & (allocations.department==disc)] if not allocations.empty else pd.DataFrame()
            deadline=str(p.get("end_date") or "")
            before=sub[sub.week_start <= deadline] if not sub.empty else sub
            allocated=float(before.planned_hours.sum()) if not before.empty else 0.0
            future_allocated=float(sub.planned_hours.sum()) if not sub.empty else 0.0
            first=str(sub.week_start.min()) if not sub.empty else None; last=str(sub.week_start.max()) if not sub.empty else None
            output.append({"Project Code":p["project_code"],"Project":p["project_name"],"Department":disc,
                           "Health":project_health(remaining,allocated),"Remaining Hours":round(remaining,2),
                           "Unplanned Hours":round(max(remaining-future_allocated,0),2),
                           "Future Allocated Hours":round(future_allocated,2),
                           "Allocated before deadline":round(allocated,2),"Shortfall / surplus":round(allocated-remaining,2),
                           "Required By":deadline,"Data Available":p.get(f"{disc.lower()}_start_date"),
                           "First planned week":first,"Last planned week":last})
    return pd.DataFrame(output)


def project_capacity_statuses(health: pd.DataFrame, balance: pd.DataFrame,
                              visible_start: date, visible_end: date) -> pd.DataFrame:
    """Attach shared departmental capacity conditions to project rows.

    Attribution means only that the project has a positive manager allocation in
    an affected department/week; it never claims that the project caused the
    combined shortage.  ``balance`` is the authoritative capacity_balance result
    already used by the chart.
    """
    result = health.copy()
    empty_defaults = {
        "Capacity Status": "Within capacity", "Over-capacity weeks": 0,
        "Peak departmental shortage": 0.0, "First affected week": None,
    }
    if result.empty:
        for name in empty_defaults: result[name] = pd.Series(dtype=object)
        return result
    for name, value in empty_defaults.items(): result[name] = value
    if balance.empty:
        return result
    boundary_start = monday_date(visible_start).isoformat()
    boundary_end = monday_date(visible_end).isoformat()
    allocations = pd.DataFrame(rows(
        "SELECT project_code,department,week_start,planned_hours FROM manager_weekly_plan "
        "WHERE planned_hours>0 AND week_start BETWEEN ? AND ?",
        (boundary_start, boundary_end),
    ))
    if allocations.empty:
        return result
    positions = balance.copy()
    positions["Capacity Status"] = positions["status"].map(CAPACITY_STATUS_LABELS).fillna("Within capacity")
    positions["shortage"] = (-positions["over_under_capacity"]).clip(lower=0)
    joined = allocations.merge(
        positions[["department", "week_start", "Capacity Status", "shortage"]],
        on=["department", "week_start"], how="left",
    )
    rank = {"Within capacity": 0, "Capacity risk": 1, "Over capacity": 2}
    for index, row in result.iterrows():
        affected = joined[(joined.project_code == row["Project Code"]) &
                          (joined.department == row["Department"])]
        if affected.empty:
            continue
        statuses = affected["Capacity Status"].fillna("Within capacity")
        result.at[index, "Capacity Status"] = max(statuses, key=lambda value: rank.get(value, 0))
        over = affected[statuses == "Over capacity"]
        if not over.empty:
            result.at[index, "Over-capacity weeks"] = int(over.week_start.nunique())
            result.at[index, "Peak departmental shortage"] = round(float(over.shortage.max()), 2)
            result.at[index, "First affected week"] = str(over.week_start.min())
    return result


def allocation_timeline(weeks: list[date], department: str | None = None) -> pd.DataFrame:
    """Timeline periods derived first from explicit manager allocation, with labelled baseline fallback."""
    projects = get_projects(False)
    allocations = pd.DataFrame(rows("SELECT project_code,department,week_start,planned_hours FROM manager_weekly_plan WHERE planned_hours>0"))
    out=[]
    if projects.empty:
        return pd.DataFrame()
    for p in projects.to_dict("records"):
        for disc in ([department] if department and department != "All" else DISCIPLINES):
            if float(p.get(f"{disc.lower()}_hours") or 0) <= 0: continue
            sub = allocations[(allocations.project_code == p["project_code"]) & (allocations.department == disc) & (allocations.planned_hours > 0)] if not allocations.empty else pd.DataFrame()
            explicit = not sub.empty
            if explicit:
                start = pd.to_datetime(sub.week_start).min().date(); end = pd.to_datetime(sub.week_start).max().date() + timedelta(days=6)
                future_sub=sub[sub.week_start>=weeks[0].isoformat()]
                allocated = float(future_sub.planned_hours.sum())
            else:
                start = _parse_date(p.get(f"{disc.lower()}_start_date") or p.get("start_date")); end = _parse_date(p.get("end_date")); allocated = 0.0
            visible_start, visible_end = weeks[0], weeks[-1] + timedelta(days=6)
            if not start or not end or end < visible_start or start > visible_end: continue
            remaining = max(float(p.get(f"{disc.lower()}_hours") or 0) - float(p.get(f"actual_{disc.lower()}_hours") or 0), 0)
            deadline_allocated = float(sub[(sub.week_start>=weeks[0].isoformat()) & (sub.week_start <= str(p.get("end_date")))].planned_hours.sum()) if not sub.empty else 0.0
            health = project_health(remaining, deadline_allocated)
            out.append({"Project": project_timeline_label(p), "project_code": p["project_code"], "Discipline": disc,
                        "Start": max(start, visible_start), "End": min(end, visible_end),
                        "Plan source": "Manager allocation" if explicit else "Forecast baseline",
                        "Remaining hours": remaining, "Allocated hours": allocated,
                        "Unplanned hours": unplanned_hours(remaining, p["project_code"], disc, weeks[0]), "Data available": p.get(f"{disc.lower()}_start_date"),
                        "Required by": p.get("end_date"), "Health status": health,
                        "Shortfall / surplus": round(deadline_allocated-remaining,2),
                        "Late": bool(explicit and end > _parse_date(p.get("end_date")))})
    result=pd.DataFrame(out)
    if not result.empty:
        priority={r.project_code:r.priority for r in projects.itertuples()}
        result["_priority"]=result.project_code.map(priority); result["_disc"]=result.Discipline.map({"RS":0,"GIS":1,"PLS":2})
        result=result.sort_values(["_priority","Required by","project_code","_disc"]).drop(columns=["_priority","_disc"])
    return result

def capacity_balance(weeks: list[date]) -> pd.DataFrame:
    grid = pd.DataFrame(
        [
            {"week_start": w.isoformat(), "department": d}
            for w in weeks
            for d in DISCIPLINES
        ]
    )
    if grid.empty:
        return pd.DataFrame(
            columns=[
                "week_start",
                "department",
                "available_capacity",
                "allocated_demand",
                "over_under_capacity",
                "status",
            ]
        )

    cap = weekly_department_capacity(weeks)
    if cap.empty or not {"week_start", "department", "available_capacity"}.issubset(cap.columns):
        cap = grid.assign(available_capacity=0.0)
    else:
        cap = grid.merge(cap, on=["week_start", "department"], how="left").fillna(
            {"available_capacity": 0.0}
        )

    dem = manager_allocations(weeks)
    if dem.empty or not {"week_start", "department", "planned_hours"}.issubset(dem.columns):
        demand = grid.assign(allocated_demand=0.0)
    else:
        demand = (
            dem.groupby(["week_start", "department"], as_index=False)["planned_hours"]
            .sum()
            .rename(columns={"planned_hours": "allocated_demand"})
        )
        demand = grid.merge(demand, on=["week_start", "department"], how="left").fillna(
            {"allocated_demand": 0.0}
        )

    merged = cap.merge(demand, on=["week_start", "department"], how="left").fillna(
        {"available_capacity": 0.0, "allocated_demand": 0.0}
    )
    internal = internal_activity_by_week(weeks)
    merged = merged.merge(internal, on=["week_start", "department"], how="left").fillna({"internal_hours": 0.0})
    merged["total_allocated"] = (merged["allocated_demand"] + merged["internal_hours"]).round(2)
    merged["over_under_capacity"] = (
        merged["available_capacity"] - merged["total_allocated"]
    ).round(2)
    merged["status"] = merged.apply(
        lambda r: capacity_status(
            (r["total_allocated"] / r["available_capacity"])
            if r["available_capacity"]
            else (float("inf") if r["total_allocated"] > 0 else None),
            r["available_capacity"],
        ),
        axis=1,
    )
    return merged
