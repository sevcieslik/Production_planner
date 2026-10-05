from __future__ import annotations

from collections import defaultdict
from datetime import date
from typing import Any, Iterable

import pandas as pd

from app.data.planner_store import (
    get_engine,
    list_non_project_allocations,
    list_projects,
    list_weekly_allocations,
)
from app.services.ptm_capacity import monday, weekly_person_capacity, week_starts
from app.services.ptm_work_queue import build_work_queue

VALID_SCOPES = {"All", "Projects Only", "Activities Only", "Selected Projects"}
VALID_VIEWS = {"By Department", "By Project"}


def build_teams_breakdown(
    *,
    start_week: date,
    horizon_weeks: int,
    view_mode: str = "By Department",
    department: str = "All",
    scope: str = "All",
    selected_projects: Iterable[str] = (),
    today: date | None = None,
    engine=None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Build the read-only Teams Breakdown from the same planner source records."""
    engine = engine or get_engine()
    view_mode = view_mode if view_mode in VALID_VIEWS else "By Department"
    scope = scope if scope in VALID_SCOPES else "All"
    department = str(department or "All").upper()
    if department not in {"ALL", "RS", "GIS", "PLS"}:
        department = "ALL"

    current_week = monday(today or date.today())
    start_week = max(monday(start_week), current_week)
    weeks = week_starts(start_week, max(1, min(int(horizon_weeks), 26)))
    end_week = weeks[-1]

    projects = list_projects(active_only=True, engine=engine)
    queue = build_work_queue(today=today or date.today(), engine=engine)
    project_allocations_all = list_weekly_allocations(
        start_week=current_week, engine=engine
    )
    non_project_all = list_non_project_allocations(
        start_week=current_week, engine=engine
    )

    project_future: dict[tuple[str, str], float] = defaultdict(float)
    project_week: dict[tuple[str, str, date], float] = defaultdict(float)
    for row in project_allocations_all:
        key = (row["department"], row["project_code"])
        project_future[key] += float(row.get("hours") or 0)
        if weeks[0] <= row["week_start"] <= end_week:
            project_week[(row["department"], row["project_code"], row["week_start"])] += float(
                row.get("hours") or 0
            )

    activity_future: dict[tuple[str, str], float] = defaultdict(float)
    activity_week: dict[tuple[str, str, date], float] = defaultdict(float)
    for row in non_project_all:
        key = (row["department"], row["activity_name"])
        activity_future[key] += float(row.get("hours") or 0)
        if weeks[0] <= row["week_start"] <= end_week:
            activity_week[(row["department"], row["activity_name"], row["week_start"])] += float(
                row.get("hours") or 0
            )

    queue_by = {
        (dept, row["Project Code"]): row
        for dept, rows in queue.items()
        for row in rows
    }
    selected = {str(value) for value in selected_projects}
    rows: list[dict[str, Any]] = []

    departments = ("RS", "GIS", "PLS")
    for project in projects:
        code = project["project_code"]
        name = project["project_name"]
        for dept in departments:
            if department != "ALL" and dept != department:
                continue
            q = queue_by.get((dept, code))
            assigned = round(project_future[(dept, code)], 2)
            remaining = None if q is None else q.get("Remaining h")
            actual = 0.0 if q is None else float(q.get("Actual h") or 0)
            if remaining is None and assigned <= 0:
                continue
            if scope == "Activities Only":
                continue
            if scope == "Selected Projects" and code not in selected and name not in selected:
                continue

            gap = None if remaining is None else round(assigned - float(remaining), 2)
            row: dict[str, Any] = {
                "Group": dept if view_mode == "By Department" else name,
                "Project / Activity": name,
                "Dept": dept,
                "Remaining h": remaining,
                "Assigned h": assigned,
                "Gap vs Remaining": gap,
                "Actual h": actual,
                "Type": "PROJECT",
                "Project Code": code,
            }
            for week in weeks:
                row[week.isoformat()] = round(project_week[(dept, code, week)], 1)
            rows.append(row)

    activity_names = sorted({row["activity_name"] for row in non_project_all})
    for activity in activity_names:
        for dept in departments:
            if department != "ALL" and dept != department:
                continue
            assigned = round(activity_future[(dept, activity)], 2)
            if assigned <= 0:
                continue
            if scope in {"Projects Only", "Selected Projects"}:
                continue
            row = {
                "Group": dept if view_mode == "By Department" else f"ACTIVITY: {activity}",
                "Project / Activity": f"ACTIVITY: {activity}",
                "Dept": dept,
                "Remaining h": None,
                "Assigned h": assigned,
                "Gap vs Remaining": None,
                "Actual h": None,
                "Type": "ACTIVITY",
                "Project Code": "",
            }
            for week in weeks:
                row[week.isoformat()] = round(activity_week[(dept, activity, week)], 1)
            rows.append(row)

    if view_mode == "By Department":
        order = {"RS": 1, "GIS": 2, "PLS": 3}
        rows.sort(
            key=lambda row: (
                order.get(row["Dept"], 9),
                1 if row["Type"] == "ACTIVITY" else 0,
                row["Project / Activity"],
            )
        )
    else:
        rows.sort(
            key=lambda row: (
                row["Project / Activity"],
                {"RS": 1, "GIS": 2, "PLS": 3}.get(row["Dept"], 9),
            )
        )

    frame = pd.DataFrame(rows)

    capacity_rows = weekly_person_capacity(
        weeks[0], len(weeks), engine=engine
    )
    capacity: dict[tuple[str, date], float] = defaultdict(float)
    for row in capacity_rows:
        capacity[(row["department"], row["week_start"])] += float(row["available_hours"] or 0)

    project_week_load: dict[tuple[str, date], float] = defaultdict(float)
    for row in project_allocations_all:
        if weeks[0] <= row["week_start"] <= end_week:
            project_week_load[(row["department"], row["week_start"])] += float(row["hours"] or 0)
    activity_week_load: dict[tuple[str, date], float] = defaultdict(float)
    for row in non_project_all:
        if weeks[0] <= row["week_start"] <= end_week:
            activity_week_load[(row["department"], row["week_start"])] += float(row["hours"] or 0)

    summary_rows = []
    for dept in departments:
        if department != "ALL" and dept != department:
            continue
        available = {"Summary": f"{dept} Available", "Dept": dept}
        planned = {"Summary": f"{dept} Planned", "Dept": dept}
        net = {"Summary": f"{dept} Net Capacity", "Dept": dept}
        for week in weeks:
            cap = round(capacity[(dept, week)], 1)
            load = round(
                project_week_load[(dept, week)] + activity_week_load[(dept, week)], 1
            )
            available[week.isoformat()] = cap
            planned[week.isoformat()] = load
            net[week.isoformat()] = round(cap - load, 1)
        summary_rows.extend([available, planned, net])

    return frame, pd.DataFrame(summary_rows)
