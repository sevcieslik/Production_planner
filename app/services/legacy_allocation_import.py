"""Preview and atomically replace manager allocations from the legacy planner CSV."""
from __future__ import annotations

import io
import re
from datetime import date, datetime, timedelta
from typing import Any, BinaryIO

import pandas as pd

from app.data.db import connect
from app.services.mvp import _audit, ensure_mvp_schema, increment_data_version

DISCIPLINE_MAP = {"1_RS": "RS", "2_GIS": "GIS", "3_PLS": "PLS"}
ADMIN_NOTE = "legacy_planner_allocation_import:Administrative"
WARNING = ("This import will replace ALL existing project manager allocations. Existing weekly "
           "project allocations will be deleted before the imported plan is applied.")


def _text(value: Any) -> str:
    return "" if pd.isna(value) else str(value).strip()


def _normal_name(value: Any) -> str:
    return re.sub(r"\s+", " ", _text(value)).casefold()


def _week_header(value: Any) -> date | None:
    """Accept only legacy day/month/year headers, optionally with midnight/time."""
    if isinstance(value, (datetime, pd.Timestamp)):
        parsed = value.date()
    else:
        text = _text(value)
        match = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{4})(?:\s+\d{1,2}:\d{2}:\d{2})?", text)
        if not match:
            return None
        try:
            parsed = date(int(match[3]), int(match[2]), int(match[1]))
        except ValueError:
            return None
    return parsed - timedelta(days=parsed.weekday())


def _number(value: Any) -> float | None:
    if pd.isna(value) or _text(value) == "":
        return None
    try:
        return float(str(value).replace(",", ""))
    except ValueError:
        return None


def _read(source: BinaryIO | bytes) -> pd.DataFrame:
    raw = source if isinstance(source, bytes) else source.getvalue() if hasattr(source, "getvalue") else source.read()
    return pd.read_csv(io.BytesIO(raw), dtype=object)


def preview_legacy_allocation(source: BinaryIO | bytes, *, filename: str = "legacy.csv",
                              mappings: dict[str, str] | None = None,
                              include_inactive: bool = True) -> dict[str, Any]:
    """Parse and match without writing anything to the database."""
    frame = _read(source)
    columns = {_normal_name(c): c for c in frame.columns}
    required = {"active", "project", "discipline", "hrs assigned"}
    errors = [f"Missing required column: {name}" for name in sorted(required - columns.keys())]
    week_columns = [(column, _week_header(column)) for column in frame.columns]
    week_columns = [(column, week) for column, week in week_columns if week]
    if not week_columns:
        errors.append("No weekly date columns were found (expected DD/MM/YYYY headers).")

    with connect() as conn:
        projects = [dict(r) for r in conn.execute("SELECT project_code,project_name FROM mvp_projects").fetchall()]
        existing = conn.execute("SELECT COUNT(*) records,COALESCE(SUM(planned_hours),0) hours FROM manager_weekly_plan").fetchone()
    exact: dict[str, list[dict]] = {}
    normal: dict[str, list[dict]] = {}
    for project in projects:
        exact.setdefault(project["project_name"].strip().casefold(), []).append(project)
        normal.setdefault(_normal_name(project["project_name"]), []).append(project)

    details, allocations, activities = [], [], []
    mappings = mappings or {}
    for index, row in frame.iterrows():
        legacy_name = _text(row.get(columns.get("project")))
        legacy_discipline = _text(row.get(columns.get("discipline"))).upper()
        department = DISCIPLINE_MAP.get(legacy_discipline)
        if not legacy_name or not department:  # structurally excludes TOTAL, availability and footer rows
            continue
        cells = []
        for column, week in week_columns:
            amount = _number(row.get(column))
            if amount is not None and amount > 0:
                cells.append({"week_start": week.isoformat(), "hours": round(amount, 4)})
        if not cells:
            continue
        active_text = _text(row.get(columns.get("active"))).upper()
        is_active = active_text not in {"FALSE", "NO", "0"}
        assigned = _number(row.get(columns.get("hrs assigned"))) or 0.0
        total = round(sum(cell["hours"] for cell in cells), 4)
        base = {"row_number": int(index) + 2, "legacy_project": legacy_name,
                "legacy_discipline": legacy_discipline, "department": department,
                "active": is_active, "hrs_assigned": assigned, "weekly_total": total,
                "difference": round(total - assigned, 4), "first_week": cells[0]["week_start"],
                "last_week": cells[-1]["week_start"]}
        if legacy_name.casefold() == "administrative":
            activities.append({**base, "cells": cells})
            continue
        candidates = exact.get(legacy_name.strip().casefold(), [])
        if not candidates:
            candidates = normal.get(_normal_name(legacy_name), [])
        manual_code = mappings.get(legacy_name)
        if manual_code:
            candidates = [project for project in projects if project["project_code"] == manual_code]
        status = "Matched" if len(candidates) == 1 else "Ambiguous" if len(candidates) > 1 else "Unmatched"
        matched = candidates[0] if len(candidates) == 1 else {}
        detail = {**base, "project_code": matched.get("project_code"),
                  "planner_project": matched.get("project_name"), "match_status": status,
                  "candidate_codes": [candidate["project_code"] for candidate in candidates]}
        details.append(detail)
        if status == "Matched" and (include_inactive or is_active):
            allocations.extend({"project_code": matched["project_code"], "department": department,
                                **cell} for cell in cells)

    weeks = sorted({item["week_start"] for item in allocations})
    return {"filename": filename, "valid": not errors, "errors": errors, "warning": WARNING,
            "include_inactive": include_inactive, "rows": details, "internal_activities": activities,
            "allocations": allocations, "available_projects": projects,
            "existing_records": int(existing["records"]), "existing_hours": float(existing["hours"]),
            "legacy_rows": len(details) + len(activities),
            "matched": sum(r["match_status"] == "Matched" for r in details),
            "unmatched": sum(r["match_status"] == "Unmatched" for r in details),
            "ambiguous": sum(r["match_status"] == "Ambiguous" for r in details),
            "import_records": len(allocations), "import_hours": round(sum(a["hours"] for a in allocations), 2),
            "earliest_week": weeks[0] if weeks else None, "latest_week": weeks[-1] if weeks else None,
            "weeks": len(weeks), "internal_hours": round(sum(r["weekly_total"] for r in activities), 2)}


def apply_legacy_allocation(preview: dict[str, Any], *, user: str, confirmed: bool,
                            fail_after_clear: bool = False) -> dict[str, Any]:
    """Clear and insert in one transaction; any exception restores the old plan."""
    if not confirmed:
        raise ValueError("Explicit replacement confirmation is required.")
    if not preview.get("valid") or preview.get("ambiguous"):
        raise ValueError("The preview contains blocking errors or ambiguous project matches.")
    ensure_mvp_schema()
    with connect() as conn:
        before = conn.execute("SELECT COUNT(*) records,COALESCE(SUM(planned_hours),0) hours FROM manager_weekly_plan").fetchone()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM manager_weekly_plan")
            if fail_after_clear:  # deterministic rollback seam used by regression tests
                raise ValueError("Allocation import failed after clear.")
            conn.executemany("""INSERT INTO manager_weekly_plan(project_code,department,week_start,planned_hours,updated_by)
                                VALUES (?,?,?,?,?)""",
                             [(a["project_code"], a["department"], a["week_start"], a["hours"], user)
                              for a in preview["allocations"]])
            conn.execute("DELETE FROM internal_activities WHERE notes=?", (ADMIN_NOTE,))
            activity_values = []
            for row in preview["internal_activities"]:
                if preview.get("include_inactive", True) or row["active"]:
                    activity_values.extend(("Administrative", row["department"], cell["week_start"],
                                            cell["week_start"], cell["hours"], 1, ADMIN_NOTE)
                                           for cell in row["cells"])
            conn.executemany("""INSERT INTO internal_activities(activity_name,department,start_week,end_week,
                                planned_hours_per_week,active,notes) VALUES (?,?,?,?,?,?,?)""", activity_values)
            summary = {"source_filename": preview["filename"], "removed_records": int(before["records"]),
                       "removed_hours": float(before["hours"]), "imported_records": len(preview["allocations"]),
                       "imported_hours": preview["import_hours"], "earliest_week": preview["earliest_week"],
                       "latest_week": preview["latest_week"], "unmatched_excluded_rows": preview["unmatched"],
                       "internal_activity_records": len(activity_values)}
            _audit(conn, user, "legacy_planner_allocation_import", "Manager allocation import",
                   new=summary, details=(f"Replaced {summary['removed_records']} allocations ({summary['removed_hours']:g} h) "
                                         f"with {summary['imported_records']} allocations ({summary['imported_hours']:g} h) "
                                         f"from {preview['filename']}"))
            increment_data_version(conn)
            conn.commit()
            return summary
        except Exception:
            conn.rollback()
            raise
