"""Human-readable, non-destructive migration of operational planner setup."""
from __future__ import annotations

from datetime import datetime, timezone
from io import BytesIO
import json
import sqlite3
from typing import Any
from zipfile import ZIP_DEFLATED, BadZipFile, ZipFile

import pandas as pd

from app.data.db import connect, write_audit
from app.services.mvp import (
    DISCIPLINES, PROJECT_DATE_COLUMNS, PROJECT_FIELDS, RESOURCE_DATE_COLUMNS,
    ensure_mvp_schema, increment_data_version, normalise_date_for_db,
)

FORMAT = "production_planner_setup"
VERSION = 1
SETUP_FILES = {"manifest.json", "projects.csv", "resources.csv", "weekly_allocations.csv", "internal_activities.csv"}
ALLOCATION_FIELDS = ["project_code", "department", "week_start", "planned_hours"]
ACTIVITY_FIELDS = ["activity_name", "department", "start_week", "end_week", "planned_hours_per_week", "active", "notes"]
SETUP_RESOURCE_FIELDS = ["person_name", "department", "weekly_hours", "active_status", "status_reason", "status_start_date", "status_end_date"]


def _csv(frame: pd.DataFrame, columns: list[str]) -> bytes:
    return frame.reindex(columns=columns).to_csv(index=False).encode("utf-8")


def export_projects_csv() -> bytes:
    ensure_mvp_schema()
    with connect() as conn:
        frame = pd.read_sql_query(f"SELECT {','.join(PROJECT_FIELDS)} FROM mvp_projects ORDER BY project_code", conn)
    return _csv(frame, PROJECT_FIELDS)


def export_planner_setup() -> bytes:
    """Build the versioned ZIP in memory. Holidays, audit and auth are excluded."""
    ensure_mvp_schema()
    with connect() as conn:
        projects = pd.read_sql_query(f"SELECT {','.join(PROJECT_FIELDS)} FROM mvp_projects ORDER BY project_code", conn)
        resources = pd.read_sql_query(
            f"SELECT {','.join('r.' + c for c in SETUP_RESOURCE_FIELDS)},"
            "(SELECT employee_id FROM resource_employee_ids e WHERE e.resource_id=r.id ORDER BY employee_id LIMIT 1) employee_id "
            "FROM mvp_resources r ORDER BY r.department,r.person_name", conn)
        allocations = pd.read_sql_query(f"SELECT {','.join(ALLOCATION_FIELDS)} FROM manager_weekly_plan ORDER BY project_code,department,week_start", conn)
        activities = pd.read_sql_query(f"SELECT {','.join(ACTIVITY_FIELDS)} FROM internal_activities ORDER BY activity_name,department,start_week", conn)
    manifest = {"format": FORMAT, "version": VERSION,
                "exported_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")}
    output = BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps(manifest, indent=2))
        archive.writestr("projects.csv", _csv(projects, PROJECT_FIELDS))
        archive.writestr("resources.csv", _csv(resources, [*SETUP_RESOURCE_FIELDS, "employee_id"]))
        archive.writestr("weekly_allocations.csv", _csv(allocations, ALLOCATION_FIELDS))
        archive.writestr("internal_activities.csv", _csv(activities, ACTIVITY_FIELDS))
    return output.getvalue()


def _read_csv(value: Any) -> pd.DataFrame:
    if hasattr(value, "seek"):
        value.seek(0)
    return pd.read_csv(value, dtype=object, keep_default_na=False)


def _comparable(value: Any) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    text = str(value).strip()
    try:
        return str(float(text)) if text else ""
    except ValueError:
        return text


def _project_preview(frame: pd.DataFrame, conn: sqlite3.Connection) -> dict[str, Any]:
    errors: list[str] = []
    missing = [c for c in PROJECT_FIELDS if c not in frame.columns]
    if missing:
        return {"records": [], "new": 0, "updated": 0, "unchanged": 0,
                "invalid": len(frame) or 1, "errors": ["Projects are missing required columns: " + ", ".join(missing)], "changes": []}
    existing = {r["project_code"]: dict(r) for r in conn.execute(f"SELECT {','.join(PROJECT_FIELDS)} FROM mvp_projects")}
    records, changes = [], []
    counts = {"new": 0, "updated": 0, "unchanged": 0, "invalid": 0}
    seen: set[str] = set()
    for number, raw in enumerate(frame.to_dict("records"), 2):
        record = {key: (None if raw.get(key) == "" else raw.get(key)) for key in PROJECT_FIELDS}
        code, name = str(record.get("project_code") or "").strip(), str(record.get("project_name") or "").strip()
        row_errors = []
        if not code: row_errors.append("project_code is required")
        if not name: row_errors.append("project_name is required")
        if code in seen: row_errors.append("project_code is duplicated in the file")
        for column in PROJECT_DATE_COLUMNS:
            if record.get(column) and not normalise_date_for_db(record[column]): row_errors.append(f"{column} is not a valid date")
        for column in ("row_km", "cct_km", "spus", "rs_hours", "gis_hours", "pls_hours", "actual_rs_hours", "actual_gis_hours", "actual_pls_hours"):
            try:
                if float(record.get(column) or 0) < 0: row_errors.append(f"{column} must be non-negative")
            except (TypeError, ValueError): row_errors.append(f"{column} must be numeric")
        if row_errors:
            counts["invalid"] += 1; errors.append(f"projects.csv row {number}: " + "; ".join(row_errors)); continue
        seen.add(code); record["project_code"] = code; record["project_name"] = name; records.append(record)
        old = existing.get(code)
        if old is None: counts["new"] += 1
        else:
            diff = [{"field": key, "current": old.get(key), "import": record.get(key)} for key in PROJECT_FIELDS
                    if _comparable(old.get(key)) != _comparable(record.get(key))]
            counts["updated" if diff else "unchanged"] += 1
            if diff: changes.append({"project_code": code, "project_name": name, "differences": diff})
    return {"records": records, **counts, "errors": errors, "changes": changes}


def preview_project_import(value: Any) -> dict[str, Any]:
    ensure_mvp_schema()
    try: frame = _read_csv(value)
    except Exception as exc: return {"records": [], "new": 0, "updated": 0, "unchanged": 0, "invalid": 1, "errors": [f"Could not read project CSV: {exc}"], "changes": []}
    with connect() as conn: return _project_preview(frame, conn)


def _validate_department(value: Any, label: str, errors: list[str]) -> str:
    department = str(value or "").strip().upper()
    if department not in DISCIPLINES: errors.append(f"{label}: department must be RS, GIS or PLS")
    return department


def preview_planner_setup(value: Any) -> dict[str, Any]:
    ensure_mvp_schema()
    try:
        if hasattr(value, "seek"): value.seek(0)
        with ZipFile(value) as archive:
            names = set(archive.namelist())
            missing = SETUP_FILES - names
            if missing: raise ValueError("Setup ZIP is missing: " + ", ".join(sorted(missing)))
            manifest = json.loads(archive.read("manifest.json"))
            if manifest.get("format") != FORMAT: raise ValueError("Unsupported planner setup format.")
            if manifest.get("version") != VERSION: raise ValueError(f"Unsupported planner setup version: {manifest.get('version')}")
            frames = {name: pd.read_csv(BytesIO(archive.read(name)), dtype=object, keep_default_na=False)
                      for name in SETUP_FILES if name.endswith(".csv")}
    except (BadZipFile, ValueError, KeyError, json.JSONDecodeError, pd.errors.ParserError) as exc:
        return {"valid": False, "errors": [str(exc)]}
    errors: list[str] = []
    required = {"resources.csv": [*SETUP_RESOURCE_FIELDS, "employee_id"], "weekly_allocations.csv": ALLOCATION_FIELDS,
                "internal_activities.csv": ACTIVITY_FIELDS}
    for filename, columns in required.items():
        absent = [c for c in columns if c not in frames[filename].columns]
        if absent: errors.append(f"{filename} is missing required columns: {', '.join(absent)}")
    with connect() as conn:
        projects = _project_preview(frames["projects.csv"], conn)
        existing_codes = {r[0] for r in conn.execute("SELECT project_code FROM mvp_projects")}
        existing_resources = {r["person_name"]: dict(r) for r in conn.execute("SELECT * FROM mvp_resources")}
        existing_activities = {(r["activity_name"],r["department"],r["start_week"],r["end_week"]): dict(r) for r in conn.execute("SELECT * FROM internal_activities")}
    errors.extend(projects["errors"]); imported_codes = {r["project_code"] for r in projects["records"]}
    resource_counts = {"new": 0, "updated": 0, "unchanged": 0}; resource_records = []
    if not any(e.startswith("resources.csv is missing") for e in errors):
        for number, raw in enumerate(frames["resources.csv"].to_dict("records"), 2):
            name = str(raw.get("person_name") or "").strip(); row_errors=[]
            dept = _validate_department(raw.get("department"), f"resources.csv row {number}", row_errors)
            if not name: row_errors.append(f"resources.csv row {number}: person_name is required")
            try:
                hours=float(raw.get("weekly_hours") or 0)
                if hours < 0: raise ValueError
            except ValueError: row_errors.append(f"resources.csv row {number}: weekly_hours must be numeric and non-negative")
            for column in RESOURCE_DATE_COLUMNS[:2]:
                if raw.get(column) and not normalise_date_for_db(raw[column]): row_errors.append(f"resources.csv row {number}: {column} is not a valid date")
            errors.extend(row_errors)
            if row_errors: continue
            raw["person_name"],raw["department"] = name,dept; resource_records.append(raw); old=existing_resources.get(name)
            if old is None: resource_counts["new"] += 1
            elif any(_comparable(old.get(k)) != _comparable(raw.get(k)) for k in SETUP_RESOURCE_FIELDS): resource_counts["updated"] += 1
            else: resource_counts["unchanged"] += 1
    allocation_records=[]
    if not any(e.startswith("weekly_allocations.csv is missing") for e in errors):
        for number, raw in enumerate(frames["weekly_allocations.csv"].to_dict("records"),2):
            row_errors=[]; code=str(raw.get("project_code") or "").strip(); dept=_validate_department(raw.get("department"),f"weekly_allocations.csv row {number}",row_errors)
            if not code: row_errors.append(f"weekly_allocations.csv row {number}: project_code is required")
            if code not in existing_codes | imported_codes: row_errors.append(f"weekly_allocations.csv row {number}: unknown project_code {code}")
            week=normalise_date_for_db(raw.get("week_start"));
            if not week: row_errors.append(f"weekly_allocations.csv row {number}: week_start is not a valid date")
            try:
                hours=float(raw.get("planned_hours"));
                if hours < 0: raise ValueError
            except (TypeError,ValueError): row_errors.append(f"weekly_allocations.csv row {number}: planned_hours must be numeric and non-negative")
            errors.extend(row_errors)
            if not row_errors: allocation_records.append({"project_code":code,"department":dept,"week_start":week,"planned_hours":hours})
    activity_records=[]; activity_counts={"new":0,"updated":0,"unchanged":0}
    if not any(e.startswith("internal_activities.csv is missing") for e in errors):
        for number, raw in enumerate(frames["internal_activities.csv"].to_dict("records"),2):
            row_errors=[]; name=str(raw.get("activity_name") or "").strip(); dept=_validate_department(raw.get("department"),f"internal_activities.csv row {number}",row_errors)
            start,end=normalise_date_for_db(raw.get("start_week")),normalise_date_for_db(raw.get("end_week"))
            if not name: row_errors.append(f"internal_activities.csv row {number}: activity_name is required")
            if not start or not end or start>end: row_errors.append(f"internal_activities.csv row {number}: valid start_week/end_week are required")
            try:
                hours=float(raw.get("planned_hours_per_week") or 0)
                if hours<0: raise ValueError
            except ValueError: row_errors.append(f"internal_activities.csv row {number}: planned_hours_per_week must be numeric and non-negative")
            errors.extend(row_errors)
            if row_errors: continue
            record={**raw,"activity_name":name,"department":dept,"start_week":start,"end_week":end,"planned_hours_per_week":hours,"active":str(raw.get("active","1")).lower() in ("1","true","yes")}; activity_records.append(record)
            old=existing_activities.get((name,dept,start,end))
            if old is None: activity_counts["new"]+=1
            elif any(_comparable(old.get(k)) != _comparable(record.get(k)) for k in ("planned_hours_per_week","active","notes")): activity_counts["updated"]+=1
            else: activity_counts["unchanged"]+=1
    return {"valid":not errors,"errors":errors,"manifest":manifest,"projects":projects,"resources":{**resource_counts,"records":resource_records},
            "allocations":{"rows":len(allocation_records),"projects_matched":len({r['project_code'] for r in allocation_records}),"records":allocation_records},
            "activities":{**activity_counts,"records":activity_records}}


def _upsert_project(conn: sqlite3.Connection, r: dict[str, Any]) -> None:
    values=[]
    numeric={"row_km","cct_km","spus","rs_hours","gis_hours","pls_hours","actual_rs_hours","actual_gis_hours","actual_pls_hours"}
    for key in PROJECT_FIELDS:
        value=r.get(key)
        if key in numeric: value=float(value or 0)
        elif key in PROJECT_DATE_COLUMNS: value=normalise_date_for_db(value)
        values.append(value)
    columns=",".join(PROJECT_FIELDS); updates=",".join(f"{c}=excluded.{c}" for c in PROJECT_FIELDS if c!="project_code")
    conn.execute(f"INSERT INTO mvp_projects({columns},archived) VALUES ({','.join('?' for _ in PROJECT_FIELDS)},?) ON CONFLICT(project_code) DO UPDATE SET {updates},archived=excluded.archived,updated_at=CURRENT_TIMESTAMP", (*values,int(str(r.get('status')).lower()=='archived')))


def apply_project_import(preview: dict[str, Any], user: str) -> None:
    if preview.get("errors") or preview.get("invalid"): raise ValueError("Project import has validation errors; nothing was written.")
    with connect() as conn:
        for record in preview["records"]: _upsert_project(conn,record)
        write_audit(conn,user,"Planner migration",None,"Project CSV imported",None,{k:preview[k] for k in ("new","updated","unchanged")},"Non-destructive project_code upsert")
        increment_data_version(conn)


def apply_planner_setup(preview: dict[str, Any], user: str, *, fail_after: str | None = None) -> None:
    """Apply a validated preview in one transaction; exceptions roll back the lot."""
    if not preview.get("valid"): raise ValueError("Planner setup has validation errors; nothing was written.")
    with connect() as conn:
        for r in preview["resources"]["records"]:
            conn.execute("""INSERT INTO mvp_resources(person_name,department,weekly_hours,holiday_booked_hours,holiday_remaining_hours,active_status,status_reason,status_start_date,status_end_date)
                VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(person_name) DO UPDATE SET department=excluded.department,weekly_hours=excluded.weekly_hours,active_status=excluded.active_status,status_reason=excluded.status_reason,status_start_date=excluded.status_start_date,status_end_date=excluded.status_end_date,updated_at=CURRENT_TIMESTAMP""",
                (r["person_name"],r["department"],float(r.get("weekly_hours") or 0),0,0,r.get("active_status") or "active",r.get("status_reason") or None,normalise_date_for_db(r.get("status_start_date")),normalise_date_for_db(r.get("status_end_date"))))
            if r.get("employee_id"):
                rid=conn.execute("SELECT id FROM mvp_resources WHERE person_name=?",(r["person_name"],)).fetchone()[0]
                conn.execute("INSERT INTO resource_employee_ids(employee_id,resource_id,employee_name) VALUES (?,?,?) ON CONFLICT(employee_id) DO UPDATE SET resource_id=excluded.resource_id,employee_name=excluded.employee_name,updated_at=CURRENT_TIMESTAMP",(str(r["employee_id"]),rid,r["person_name"]))
        if fail_after=="resources": raise RuntimeError("Injected setup import failure")
        for r in preview["projects"]["records"]: _upsert_project(conn,r)
        for r in preview["activities"]["records"]:
            old=conn.execute("SELECT id FROM internal_activities WHERE activity_name=? AND department=? AND start_week=? AND end_week=?",(r["activity_name"],r["department"],r["start_week"],r["end_week"])).fetchone()
            values=(float(r["planned_hours_per_week"]),int(r["active"]),r.get("notes") or None)
            if old: conn.execute("UPDATE internal_activities SET planned_hours_per_week=?,active=?,notes=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",(*values,old[0]))
            else: conn.execute("INSERT INTO internal_activities(activity_name,department,start_week,end_week,planned_hours_per_week,active,notes) VALUES (?,?,?,?,?,?,?)",(r["activity_name"],r["department"],r["start_week"],r["end_week"],*values))
        for r in preview["allocations"]["records"]:
            conn.execute("INSERT INTO manager_weekly_plan(project_code,department,week_start,planned_hours,updated_by) VALUES (?,?,?,?,?) ON CONFLICT(project_code,department,week_start) DO UPDATE SET planned_hours=excluded.planned_hours,updated_by=excluded.updated_by,updated_at=CURRENT_TIMESTAMP",(r["project_code"],r["department"],r["week_start"],r["planned_hours"],user))
        summary={"Projects":{k:preview["projects"][k] for k in ("new","updated","unchanged")},"Resources":{k:preview["resources"][k] for k in ("new","updated","unchanged")},"Weekly allocations":preview["allocations"]["rows"],"Internal activities":{k:preview["activities"][k] for k in ("new","updated","unchanged")}}
        write_audit(conn,user,"Planner migration",None,"Planner setup imported",None,summary,"Atomic non-destructive setup upsert")
        increment_data_version(conn)
