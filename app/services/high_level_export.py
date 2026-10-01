from __future__ import annotations

import os
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Any

from app.data.planner_store import (
    get_engine,
    list_non_project_allocations,
    list_projects,
    list_stage_inputs,
    list_weekly_allocations,
)
from app.integrations.google_sheets import (
    GoogleSheetsConfigurationError,
    google_client,
    replace_sheet_rows,
)
from app.services.ptm_capacity import monday, weekly_person_capacity, week_starts
from app.services.ptm_work_queue import build_work_queue

PLANNING_IMPORT_SHEET = "_planning_import"
CAPACITY_IMPORT_SHEET = "_capacity_import"
ALLOCATION_IMPORT_SHEET = "_allocation_weekly_import"


def _planning_status(remaining: float | None, planned: float) -> str:
    if remaining is None:
        return "NOT ESTIMATED"
    remaining = max(float(remaining), 0)
    planned = max(float(planned), 0)
    if remaining <= 0.01:
        return "COMPLETE"
    if planned <= 0.01:
        return "ESTIMATED / NOT PLANNED"
    delta = round(planned - remaining, 1)
    if delta > 0.1:
        return f"OVERPLANNED +{delta:g} h"
    if delta < -0.1:
        return "PARTIALLY PLANNED"
    return "PLANNED"


def planning_import_rows(*, today: date | None = None, engine=None) -> list[dict[str, Any]]:
    engine = engine or get_engine()
    today = today or date.today()
    queue = build_work_queue(today=today, engine=engine)
    stages = {
        (row["project_code"], row["department"]): row
        for row in list_stage_inputs(engine=engine)
    }
    allocations = list_weekly_allocations(
        start_week=monday(today), engine=engine
    )
    latest_allocation_update: dict[tuple[str, str], datetime] = {}
    for row in allocations:
        key = (row["project_code"], row["department"])
        updated = row.get("updated_at")
        if updated is None:
            continue
        previous = latest_allocation_update.get(key)
        if previous is None or updated > previous:
            latest_allocation_update[key] = updated

    output: list[dict[str, Any]] = []
    for department in ("RS", "GIS", "PLS"):
        for row in queue[department]:
            code = row["Project Code"]
            stage = stages.get((code, department))
            stage_updated = stage.get("updated_at") if stage else None
            allocation_updated = latest_allocation_update.get((code, department))
            candidates = [value for value in (stage_updated, allocation_updated) if value is not None]
            last_update = max(candidates) if candidates else None
            manager = None
            if stage and stage.get("updated_by"):
                manager = stage["updated_by"]
            elif allocation_updated:
                managers = [
                    alloc.get("updated_by")
                    for alloc in allocations
                    if alloc["project_code"] == code
                    and alloc["department"] == department
                    and alloc.get("updated_at") == allocation_updated
                ]
                manager = next((value for value in managers if value), None)

            output.append(
                {
                    "Project Code": code,
                    "Project": row["Project"],
                    "Department": department,
                    "Bid h": row["Bid h"],
                    "PTM Estimate h": row["Estimate h"],
                    "Actual h": row["Actual h"],
                    "Remaining h": row["Remaining h"],
                    "Planned Future h": row["Planned h"],
                    "Forecast Finish": row["Forecast"],
                    "PM Deadline": row["PM Deadline"],
                    "Planning Status": _planning_status(row["Remaining h"], row["Planned h"]),
                    "Variance Days": row["Variance days"],
                    "Last Plan Update": last_update,
                    "Manager": manager,
                    "Previous Forecast": row["Forecast"],
                    "Forecast Changed": False,
                    "Handover Start": None if stage is None else stage.get("handover_start"),
                }
            )
    return output


def capacity_import_rows(
    *,
    start_week: date | None = None,
    horizon_weeks: int = 52,
    engine=None,
) -> list[dict[str, Any]]:
    engine = engine or get_engine()
    start = monday(start_week or date.today())
    weeks = week_starts(start, horizon_weeks)
    if not weeks:
        return []
    end = weeks[-1]

    capacity = weekly_person_capacity(start, horizon_weeks, engine=engine)
    project_allocations = list_weekly_allocations(
        start_week=start, end_week=end, engine=engine
    )
    non_project = list_non_project_allocations(
        start_week=start, end_week=end, engine=engine
    )

    available: dict[tuple[str, date], float] = defaultdict(float)
    project_planned: dict[tuple[str, date], float] = defaultdict(float)
    non_project_planned: dict[tuple[str, date], float] = defaultdict(float)

    for row in capacity:
        available[(row["department"], row["week_start"])] += float(
            row.get("available_hours") or 0
        )
    for row in project_allocations:
        project_planned[(row["department"], row["week_start"])] += float(
            row.get("hours") or 0
        )
    for row in non_project:
        non_project_planned[(row["department"], row["week_start"])] += float(
            row.get("hours") or 0
        )

    output = []
    for week in weeks:
        for department in ("RS", "GIS", "PLS"):
            cap = round(available[(department, week)], 2)
            project_h = round(project_planned[(department, week)], 2)
            non_project_h = round(non_project_planned[(department, week)], 2)
            total = round(project_h + non_project_h, 2)
            output.append(
                {
                    "Week Commencing": week,
                    "Department": department,
                    "Available h": cap,
                    "Project Planned h": project_h,
                    "Non-project h": non_project_h,
                    "Total Planned h": total,
                    "Unallocated h": round(max(cap - total, 0), 2),
                    "Overallocated h": round(max(total - cap, 0), 2),
                }
            )
    return output


def allocation_import_rows(
    *,
    start_week: date | None = None,
    engine=None,
) -> list[dict[str, Any]]:
    engine = engine or get_engine()
    start = monday(start_week or date.today())
    projects = {
        row["project_code"]: row["project_name"]
        for row in list_projects(active_only=False, engine=engine)
    }
    project_allocations = list_weekly_allocations(
        start_week=start, engine=engine
    )
    non_project = list_non_project_allocations(
        start_week=start, engine=engine
    )

    project_totals: dict[tuple[date, str, str], float] = defaultdict(float)
    for row in project_allocations:
        project_totals[
            (row["week_start"], row["department"], row["project_code"])
        ] += float(row.get("hours") or 0)

    non_project_totals: dict[tuple[date, str, str], float] = defaultdict(float)
    for row in non_project:
        non_project_totals[
            (row["week_start"], row["department"], row["activity_name"])
        ] += float(row.get("hours") or 0)

    output: list[dict[str, Any]] = []
    for (week, department, code), hours in sorted(project_totals.items()):
        output.append(
            {
                "Week Commencing": week,
                "Department": department,
                "Project Code": code,
                "Project": projects.get(code, code),
                "Work Type": "PROJECT",
                "Planned h": round(hours, 2),
            }
        )
    for (week, department, activity), hours in sorted(non_project_totals.items()):
        output.append(
            {
                "Week Commencing": week,
                "Department": department,
                "Project Code": "",
                "Project": activity,
                "Work Type": "NON_PROJECT",
                "Planned h": round(hours, 2),
            }
        )
    output.sort(
        key=lambda row: (
            row["Week Commencing"],
            {"RS": 1, "GIS": 2, "PLS": 3}.get(row["Department"], 9),
            row["Project"],
        )
    )
    return output


def export_to_high_level(
    *,
    spreadsheet_id: str | None = None,
    start_week: date | None = None,
    capacity_horizon_weeks: int = 52,
    engine=None,
) -> dict[str, int]:
    """
    Publish Planner-owned operational tables into High Level hidden import tabs.

    This function deliberately never writes to High Level's Projects tab.
    """
    spreadsheet_id = spreadsheet_id or os.getenv("HIGH_LEVEL_SPREADSHEET_ID")
    if not spreadsheet_id:
        raise GoogleSheetsConfigurationError("HIGH_LEVEL_SPREADSHEET_ID is not configured.")

    engine = engine or get_engine()
    client = google_client()
    planning = planning_import_rows(today=date.today(), engine=engine)
    capacity = capacity_import_rows(
        start_week=start_week,
        horizon_weeks=capacity_horizon_weeks,
        engine=engine,
    )
    allocations = allocation_import_rows(start_week=start_week, engine=engine)

    return {
        PLANNING_IMPORT_SHEET: replace_sheet_rows(
            planning,
            spreadsheet_id=spreadsheet_id,
            sheet_name=PLANNING_IMPORT_SHEET,
            client=client,
        ),
        CAPACITY_IMPORT_SHEET: replace_sheet_rows(
            capacity,
            spreadsheet_id=spreadsheet_id,
            sheet_name=CAPACITY_IMPORT_SHEET,
            client=client,
        ),
        ALLOCATION_IMPORT_SHEET: replace_sheet_rows(
            allocations,
            spreadsheet_id=spreadsheet_id,
            sheet_name=ALLOCATION_IMPORT_SHEET,
            client=client,
        ),
    }
