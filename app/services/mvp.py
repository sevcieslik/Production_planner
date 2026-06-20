from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from html.parser import HTMLParser
import re
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

from app.data.db import connect, rows
from app.services.planning import capacity_status, spread_hours, week_starts

DISCIPLINES = ["RS", "GIS", "PLS"]
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
    "row_km",
    "cct_km",
    "spus",
    "rs_hours",
    "gis_hours",
    "pls_hours",
    "start_date",
    "end_date",
    "loading_type",
    "rs_start_date",
    "gis_start_date",
    "pls_start_date",
    "status",
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


def ensure_mvp_schema() -> None:
    with connect() as conn:
        conn.executescript(
            """
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
              notes TEXT,
              UNIQUE(person_name, holiday_date, source)
            );

            CREATE TABLE IF NOT EXISTS settings (
              key TEXT PRIMARY KEY,
              value TEXT NOT NULL
            );

            -- harmless when created on a fresh database; ignored below for existing DBs

            INSERT OR IGNORE INTO settings(key,value)
            VALUES ('diminished_capacity_factor','0.85');

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

    return out[PROJECT_FIELDS]


def save_projects(records: Iterable[dict]) -> None:
    ensure_mvp_schema()
    with connect() as conn:
        for r in records:
            code = str(r.get("project_code") or "").strip()
            name = str(r.get("project_name") or "").strip()

            if not code or not name:
                continue

            vals = {k: r.get(k) for k in PROJECT_FIELDS}
            vals["loading_type"] = normalize_loading_type(vals.get("loading_type"))
            vals["status"] = str(vals.get("status") or "active").lower()
            archived = 1 if vals["status"] == "archived" else 0

            conn.execute(
                """
                INSERT INTO mvp_projects(
                    project_code,
                    project_name,
                    row_km,
                    cct_km,
                    spus,
                    rs_hours,
                    gis_hours,
                    pls_hours,
                    start_date,
                    end_date,
                    loading_type,
                    rs_start_date,
                    gis_start_date,
                    pls_start_date,
                    status,
                    archived,
                    updated_at
                )
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'))
                ON CONFLICT(project_code) DO UPDATE SET
                    project_name=excluded.project_name,
                    row_km=excluded.row_km,
                    cct_km=excluded.cct_km,
                    spus=excluded.spus,
                    rs_hours=excluded.rs_hours,
                    gis_hours=excluded.gis_hours,
                    pls_hours=excluded.pls_hours,
                    start_date=excluded.start_date,
                    end_date=excluded.end_date,
                    loading_type=excluded.loading_type,
                    rs_start_date=excluded.rs_start_date,
                    gis_start_date=excluded.gis_start_date,
                    pls_start_date=excluded.pls_start_date,
                    status=excluded.status,
                    archived=excluded.archived,
                    updated_at=datetime('now')
                """,
                (
                    code,
                    name,
                    float(vals.get("row_km") or 0),
                    float(vals.get("cct_km") or 0),
                    float(vals.get("spus") or 0),
                    float(vals.get("rs_hours") or 0),
                    float(vals.get("gis_hours") or 0),
                    float(vals.get("pls_hours") or 0),
                    normalise_date_for_db(vals.get("start_date"), date.today()),
                    normalise_date_for_db(vals.get("end_date"), date.today() + timedelta(days=28)),
                    vals["loading_type"],
                    normalise_date_for_db(vals.get("rs_start_date"), date.today()),
                    normalise_date_for_db(vals.get("gis_start_date"), date.today()),
                    normalise_date_for_db(vals.get("pls_start_date"), date.today()),
                    vals["status"],
                    archived,
                ),
            )
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



@dataclass
class MvpImportResult:
    imported_people_count: int = 0
    updated_people_count: int = 0
    imported_holiday_records_count: int = 0
    unmatched_holiday_names: list[str] = field(default_factory=list)
    skipped_rows: int = 0
    validation_issues: list[str] = field(default_factory=list)


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


def _read_sample_table(path: str | Path) -> pd.DataFrame:
    path = Path(path)
    if path.suffix.lower() == ".csv":
        return pd.read_csv(path)
    if path.read_bytes().lstrip().lower().startswith(b"<table"):
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
        parser = Parser(); parser.feed(path.read_text(errors="ignore"))
        header = parser.rows[0]
        data = [r for r in parser.rows[1:] if any(c.strip() for c in r)]
        width = len(header)
        return pd.DataFrame([r[:width] + [""] * max(width - len(r), 0) for r in data], columns=header)
    return pd.read_excel(path)


def load_roster_csv(path: str | Path = "sample-data/roster.csv") -> pd.DataFrame:
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


def import_sample_roster(path: str | Path = "sample-data/roster.csv") -> MvpImportResult:
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
    save_resources(records)
    return result


def _parse_date(value: Any) -> date | None:
    parsed = pd.to_datetime(value, errors="coerce", dayfirst=True)
    return None if pd.isna(parsed) else parsed.date()


def _working_days(start: date, end: date) -> list[date]:
    out=[]; cur=start
    while cur <= end:
        if cur.weekday() < 5: out.append(cur)
        cur += timedelta(days=1)
    return out


def import_approved_holidays(path: str | Path = "sample-data/Employee Holiday - Approved - From 01_01_2026 to 31_12_2026 .xls") -> MvpImportResult:
    ensure_mvp_schema(); result = MvpImportResult(); df = _read_sample_table(path); cmap = _column_map(df.columns)
    with connect() as conn:
        people = {r["person_name"].strip().lower(): r for r in conn.execute("SELECT * FROM mvp_resources").fetchall()}
        seen=set()
        for i, row in df.iterrows():
            name = str(row.get(cmap.get("person_name", ""), "")).strip()
            if not name: continue
            person = people.get(name.lower())
            if not person:
                result.unmatched_holiday_names.append(name); result.skipped_rows += 1; continue
            start = _parse_date(row.get(cmap.get("start_date", "")))
            end = _parse_date(row.get(cmap.get("end_date", ""))) or start
            if not start:
                result.skipped_rows += 1; result.validation_issues.append(f"{name}: missing holiday date"); continue
            days = _working_days(start, end)
            total_hours = _number(row.get(cmap.get("hours", ""), None), None)
            if total_hours is None:
                duration_days = _number(row.get(cmap.get("days", ""), None), None)
                per_day = float(person["weekly_hours"] or 0) / 5
                total_hours = per_day * (duration_days if duration_days is not None and len(days) <= 1 else len(days))
            hours_per_day = total_hours / max(len(days), 1)
            for d in days:
                if d.year != 2026: result.validation_issues.append(f"{name}: holiday outside 2026 on {d.isoformat()}")
                if hours_per_day < 0: result.validation_issues.append(f"{name}: negative holiday hours on {d.isoformat()}"); continue
                key=(name.lower(), d.isoformat())
                if key in seen: result.validation_issues.append(f"{name}: duplicate holiday on {d.isoformat()}")
                seen.add(key)
                cur = conn.execute("INSERT OR IGNORE INTO holidays(resource_id,person_name,holiday_date,hours,source,notes) VALUES (?,?,?,?,?,?)", (person["id"], name, d.isoformat(), round(hours_per_day,2), "sample-approved", str(row.get(cmap.get("notes", ""), "")).strip())).rowcount
                if cur: result.imported_holiday_records_count += 1
    recalculate_holiday_totals()
    increment_data_version()
    result.unmatched_holiday_names = sorted(set(result.unmatched_holiday_names))
    return result


def recalculate_holiday_totals() -> int:
    ensure_mvp_schema()
    with connect() as conn:
        conn.execute("UPDATE mvp_resources SET holiday_booked_hours=COALESCE((SELECT SUM(hours) FROM holidays h WHERE lower(h.person_name)=lower(mvp_resources.person_name)),0), updated_at=datetime('now')")
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
    return pd.DataFrame(rows(f"SELECT h.id,h.person_name,r.department,h.holiday_date,h.hours,h.source,h.notes FROM holidays h LEFT JOIN mvp_resources r ON lower(r.person_name)=lower(h.person_name) {where} ORDER BY h.holiday_date,h.person_name", tuple(params)))


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

def save_resources(records: Iterable[dict]) -> None:
    ensure_mvp_schema()
    with connect() as conn:
        for r in records:
            name = str(r.get("person_name") or "").strip()
            dept = str(r.get("department") or "RS").strip().upper()

            if not name or dept not in DISCIPLINES:
                continue

            status = str(r.get("active_status") or "active").lower()

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
        increment_data_version(conn)


def get_resources() -> pd.DataFrame:
    ensure_mvp_schema()
    return pd.DataFrame(
        rows(
            f"""
            SELECT {",".join(RESOURCE_FIELDS)}
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


def weekly_department_capacity(weeks: list[date]) -> pd.DataFrame:
    ensure_mvp_schema()

    factor = setting_float("diminished_capacity_factor", 0.85)
    resources = rows("SELECT * FROM mvp_resources")
    holiday_rows = rows("SELECT person_name, holiday_date, hours FROM holidays")
    out = []

    for w in weeks:
        totals = {d: 0.0 for d in DISCIPLINES}

        for r in resources:
            if not resource_active_for_week(r, w):
                continue

            dept = department_for_resource(r["id"], r["department"], w)

            holiday_hours = 0.0
            for h in holiday_rows:
                holiday_date = pd.to_datetime(h["holiday_date"], errors="coerce")
                if pd.isna(holiday_date):
                    continue

                if w <= holiday_date.date() <= w + timedelta(days=6):
                    if h["person_name"] == r["person_name"]:
                        holiday_hours += float(h["hours"] or 0)

            totals[dept] += max(
                float(r["weekly_hours"] or 0)
                - holiday_hours,
                0,
            )

        for d, h in totals.items():
            out.append(
                {
                    "week_start": w.isoformat(),
                    "department": d,
                    "available_capacity": round(h * factor, 2),
                }
            )

    return pd.DataFrame(out)


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

    dem = weekly_project_demand()
    if dem.empty or not {"week_start", "department", "demand_hours"}.issubset(dem.columns):
        demand = grid.assign(allocated_demand=0.0)
    else:
        demand = (
            dem.groupby(["week_start", "department"], as_index=False)["demand_hours"]
            .sum()
            .rename(columns={"demand_hours": "allocated_demand"})
        )
        demand = grid.merge(demand, on=["week_start", "department"], how="left").fillna(
            {"allocated_demand": 0.0}
        )

    merged = cap.merge(demand, on=["week_start", "department"], how="left").fillna(
        {"available_capacity": 0.0, "allocated_demand": 0.0}
    )
    merged["over_under_capacity"] = (
        merged["available_capacity"] - merged["allocated_demand"]
    ).round(2)
    merged["status"] = merged.apply(
        lambda r: capacity_status(
            (r["allocated_demand"] / r["available_capacity"])
            if r["available_capacity"]
            else None,
            r["available_capacity"],
        ),
        axis=1,
    )
    return merged