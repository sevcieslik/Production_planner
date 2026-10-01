from __future__ import annotations

import os
from typing import Any

import gspread

from app.integrations.google_sheets import (
    GoogleSheetsConfigurationError,
    date_or_none,
    google_client,
)

ROSTER_SHEET = "_roster"
ASSIGNMENTS_SHEET = "_team_assignments"
TIME_OFF_SHEET = "Time Off"
DEMAND_SHEET = "_project_demand"
ALLOCATIONS_SHEET = "_resource_allocations"
CALENDAR_SHEET = "_calendar"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return _text(value).lower() in {"true", "1", "yes", "y", "active"}


def _number(value: Any, default: float | None = None) -> float | None:
    if value in (None, ""):
        return default
    try:
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return default


def _records(spreadsheet: gspread.Spreadsheet, sheet_name: str) -> list[dict[str, Any]]:
    return spreadsheet.worksheet(sheet_name).get_all_records(default_blank="")


def read_legacy_ptm_snapshot(
    *,
    spreadsheet_id: str | None = None,
    client: gspread.Client | None = None,
) -> dict[str, list[dict[str, Any]]]:
    spreadsheet_id = spreadsheet_id or os.getenv("PTM_LEGACY_SPREADSHEET_ID")
    if not spreadsheet_id:
        raise GoogleSheetsConfigurationError("PTM_LEGACY_SPREADSHEET_ID is not configured.")
    client = client or google_client()
    spreadsheet = client.open_by_key(spreadsheet_id)

    roster_rows = _records(spreadsheet, ROSTER_SHEET)
    assignment_rows = _records(spreadsheet, ASSIGNMENTS_SHEET)
    time_off_rows = _records(spreadsheet, TIME_OFF_SHEET)
    demand_rows = _records(spreadsheet, DEMAND_SHEET)
    allocation_rows = _records(spreadsheet, ALLOCATIONS_SHEET)
    calendar_rows = _records(spreadsheet, CALENDAR_SHEET)

    people = []
    for row in roster_rows:
        name = _text(row.get("Name"))
        if not name:
            continue
        people.append(
            {
                "person_name": name,
                "home_department": _text(row.get("Home Department")).upper(),
                "primary_role": _text(row.get("Primary Role")) or None,
                "secondary_role": _text(row.get("Secondary Role")) or None,
                "standard_hours_day": _number(row.get("Standard h/day"), 7.5),
                "active": _bool(row.get("Include in Capacity")),
                "active_from": date_or_none(row.get("Active From")),
                "active_to": date_or_none(row.get("Active To")),
            }
        )

    assignments = []
    for row in assignment_rows:
        name = _text(row.get("Name"))
        department = _text(row.get("Department")).upper()
        from_date = date_or_none(row.get("From"))
        pct = _number(row.get("Allocation %"))
        if not name or not department or from_date is None or pct is None:
            continue
        assignments.append(
            {
                "person_name": name,
                "from_date": from_date,
                "to_date": date_or_none(row.get("To")),
                "department": department,
                "allocation_percent": pct,
                "assignment_type": _text(row.get("Assignment Type")) or "TEMP_SUPPORT",
                "notes": _text(row.get("Notes")) or None,
            }
        )

    time_off = []
    for row in time_off_rows:
        name = _text(row.get("Name"))
        from_date = date_or_none(row.get("From"))
        if not name or from_date is None:
            continue
        time_off.append(
            {
                "person_name": name,
                "from_date": from_date,
                "to_date": date_or_none(row.get("To")) or from_date,
                "time_off_type": _text(row.get("Type")) or "HOLIDAY",
                "available_hours_day": _number(row.get("Available h/day"), 0) or 0,
                "notes": _text(row.get("Notes")) or None,
            }
        )

    demand = []
    for row in demand_rows:
        code = _text(row.get("Project Code"))
        department = _text(row.get("Department")).upper()
        if not code or department not in {"RS", "GIS", "PLS"}:
            continue
        demand.append(
            {
                "project_code": code,
                "department": department,
                "estimate_hours": _number(row.get("PTM Estimate h")),
                "remaining_override_hours": _number(row.get("Remaining Override h")),
                "actual_hours": _number(row.get("Actual h"), 0) or 0,
                "handover_start": date_or_none(row.get("Handover Start")),
                "notes": _text(row.get("Notes")) or None,
            }
        )

    allocations = []
    for row in allocation_rows:
        person = _text(row.get("Name"))
        week = date_or_none(row.get("Week Commencing"))
        department = _text(row.get("Department")).upper()
        hours = _number(row.get("Planned h"), 0) or 0
        if not person or week is None or department not in {"RS", "GIS", "PLS"} or hours <= 0:
            continue
        code = _text(row.get("Project Code"))
        name = _text(row.get("Project"))
        allocations.append(
            {
                "person_name": person,
                "week_start": week,
                "department": department,
                "project_code": code or None,
                "activity_name": None if code else (name or "OTHER"),
                "hours": hours,
            }
        )

    calendar = []
    for row in calendar_rows:
        work_date = date_or_none(row.get("Date"))
        if work_date is None:
            continue
        calendar.append(
            {
                "work_date": work_date,
                "effective_working_day": _bool(row.get("Effective Working Day")),
                "bank_holiday": _bool(row.get("Bank Holiday")),
                "holiday_name": _text(row.get("Holiday Name")) or None,
                "source": _text(row.get("Source")) or "PTM calendar",
            }
        )

    return {
        "people": people,
        "assignments": assignments,
        "time_off": time_off,
        "demand": demand,
        "allocations": allocations,
        "calendar": calendar,
    }
