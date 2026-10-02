from __future__ import annotations

from collections import defaultdict
from datetime import date
from typing import Any

import pandas as pd

from app.data.planner_store import (
    get_engine,
    list_non_project_allocations,
    list_projects,
    list_weekly_allocations,
    save_weekly_allocations_batch,
)
from app.services.ptm_capacity import weekly_person_capacity, week_starts
from app.services.ptm_work_queue import build_work_queue


def _helper_name(kind: str, week: date) -> str:
    return f"__{kind}__{week.isoformat()}"


def build_department_grid(
    department: str,
    project_code: str,
    *,
    start_week: date,
    horizon_weeks: int,
    engine=None,
) -> tuple[pd.DataFrame, dict[tuple[str, str], int], list[date]]:
    """Build one PTM-style editable person/week grid for a selected project."""
    engine = engine or get_engine()
    department = str(department).upper()
    weeks = week_starts(start_week, horizon_weeks)
    if not weeks:
        return pd.DataFrame(), {}, []

    projects = {
        row["project_code"]: row for row in list_projects(active_only=True, engine=engine)
    }
    if project_code not in projects:
        raise ValueError(f"Unknown active project: {project_code}")

    capacity = weekly_person_capacity(
        start_week,
        horizon_weeks,
        department=department,
        engine=engine,
    )
    allocations = list_weekly_allocations(
        department=department,
        start_week=weeks[0],
        end_week=weeks[-1],
        engine=engine,
    )
    non_project = list_non_project_allocations(
        department=department,
        start_week=weeks[0],
        end_week=weeks[-1],
        engine=engine,
    )

    available: dict[tuple[str, date], float] = defaultdict(float)
    roles: dict[str, str] = {}
    time_off: dict[tuple[str, date], bool] = {}
    people: set[str] = set()
    for row in capacity:
        person = row["person_name"]
        week = row["week_start"]
        people.add(person)
        roles[person] = row.get("primary_role") or ""
        available[(person, week)] += float(row.get("available_hours") or 0)
        time_off[(person, week)] = bool(row.get("time_off"))

    selected: dict[tuple[str, date], float] = defaultdict(float)
    all_planned: dict[tuple[str, date], float] = defaultdict(float)
    versions: dict[tuple[str, str], int] = {}
    for row in allocations:
        person = row["person_name"]
        week = row["week_start"]
        people.add(person)
        all_planned[(person, week)] += float(row.get("hours") or 0)
        if row["project_code"] == project_code:
            selected[(person, week)] += float(row.get("hours") or 0)
            versions[(person, week.isoformat())] = int(row.get("version") or 0)
    for row in non_project:
        person = row["person_name"]
        week = row["week_start"]
        people.add(person)
        all_planned[(person, week)] += float(row.get("hours") or 0)

    output = []
    for person in sorted(people):
        row: dict[str, Any] = {
            "Person": person,
            "Role": roles.get(person, ""),
            "H Available": round(sum(available[(person, week)] for week in weeks), 1),
            "H Left": round(
                sum(available[(person, week)] - all_planned[(person, week)] for week in weeks),
                1,
            ),
        }
        for week in weeks:
            selected_hours = round(selected[(person, week)], 1)
            row[week.isoformat()] = selected_hours
            row[_helper_name("available", week)] = round(available[(person, week)], 1)
            row[_helper_name("other", week)] = round(
                max(all_planned[(person, week)] - selected[(person, week)], 0), 1
            )
            row[_helper_name("timeoff", week)] = bool(time_off.get((person, week), False))
        output.append(row)

    return pd.DataFrame(output), versions, weeks


def allocation_grid_summary(
    department: str,
    project_code: str,
    *,
    start_week: date,
    horizon_weeks: int,
    engine=None,
) -> pd.DataFrame:
    """Return PTM-style weekly summary rows using the same allocation/capacity records."""
    engine = engine or get_engine()
    weeks = week_starts(start_week, horizon_weeks)
    capacity_rows = weekly_person_capacity(
        start_week,
        horizon_weeks,
        department=department,
        engine=engine,
    )
    allocations = list_weekly_allocations(
        department=department,
        start_week=weeks[0],
        end_week=weeks[-1],
        engine=engine,
    )
    non_project = list_non_project_allocations(
        department=department,
        start_week=weeks[0],
        end_week=weeks[-1],
        engine=engine,
    )
    team_capacity: dict[date, float] = defaultdict(float)
    selected_project: dict[date, float] = defaultdict(float)
    all_planned: dict[date, float] = defaultdict(float)
    for row in capacity_rows:
        team_capacity[row["week_start"]] += float(row.get("available_hours") or 0)
    for row in allocations:
        week = row["week_start"]
        all_planned[week] += float(row.get("hours") or 0)
        if row["project_code"] == project_code:
            selected_project[week] += float(row.get("hours") or 0)
    for row in non_project:
        all_planned[row["week_start"]] += float(row.get("hours") or 0)

    summary = []
    for label, values in (
        ("PROJECT PLANNED", selected_project),
        ("TOTAL PLANNED", all_planned),
        ("TEAM CAPACITY", team_capacity),
    ):
        row = {"Summary": label}
        for week in weeks:
            row[week.isoformat()] = round(values[week], 1)
        summary.append(row)

    free = {"Summary": "FREE / OVER"}
    for week in weeks:
        free[week.isoformat()] = round(team_capacity[week] - all_planned[week], 1)
    summary.append(free)
    return pd.DataFrame(summary)


def build_team_project_matrix(
    department: str,
    *,
    start_week: date,
    horizon_weeks: int,
    engine=None,
) -> tuple[pd.DataFrame, pd.DataFrame, list[date]]:
    """Show every project/activity consuming a department's hours across the horizon."""
    engine = engine or get_engine()
    department = str(department).upper()
    weeks = week_starts(start_week, horizon_weeks)
    if not weeks:
        return pd.DataFrame(), pd.DataFrame(), []

    projects = {
        row["project_code"]: row["project_name"]
        for row in list_projects(active_only=False, engine=engine)
    }
    queue = {
        row["Project Code"]: row for row in build_work_queue(engine=engine)[department]
    }
    allocations = list_weekly_allocations(
        department=department,
        start_week=weeks[0],
        end_week=weeks[-1],
        engine=engine,
    )
    non_project = list_non_project_allocations(
        department=department,
        start_week=weeks[0],
        end_week=weeks[-1],
        engine=engine,
    )

    project_week: dict[tuple[str, date], float] = defaultdict(float)
    for row in allocations:
        project_week[(row["project_code"], row["week_start"])] += float(row.get("hours") or 0)

    rows: list[dict[str, Any]] = []
    project_codes = set(queue) | {row["project_code"] for row in allocations}
    for code in sorted(project_codes, key=lambda value: (projects.get(value, value), value)):
        total = round(sum(project_week[(code, week)] for week in weeks), 1)
        work = queue.get(code)
        remaining = None if work is None else work.get("Remaining h")
        if total <= 0 and (remaining is None or float(remaining) <= 0):
            continue
        row: dict[str, Any] = {
            "Project Code": code,
            "Project / Activity": projects.get(code, code),
            "Type": "PROJECT",
            "Remaining h": remaining,
            "Planned h": total,
            "Gap vs Remaining": None if remaining is None else round(total - float(remaining), 1),
        }
        for week in weeks:
            row[week.isoformat()] = round(project_week[(code, week)], 1)
        rows.append(row)

    activity_week: dict[tuple[str, date], float] = defaultdict(float)
    for item in non_project:
        activity_week[(item["activity_name"], item["week_start"])] += float(item.get("hours") or 0)
    for activity in sorted({row["activity_name"] for row in non_project}):
        total = round(sum(activity_week[(activity, week)] for week in weeks), 1)
        if total <= 0:
            continue
        row = {
            "Project Code": "",
            "Project / Activity": f"ACTIVITY: {activity}",
            "Type": "ACTIVITY",
            "Remaining h": None,
            "Planned h": total,
            "Gap vs Remaining": None,
        }
        for week in weeks:
            row[week.isoformat()] = round(activity_week[(activity, week)], 1)
        rows.append(row)

    capacity_rows = weekly_person_capacity(
        start_week,
        horizon_weeks,
        department=department,
        engine=engine,
    )
    team_capacity: dict[date, float] = defaultdict(float)
    all_planned: dict[date, float] = defaultdict(float)
    for item in capacity_rows:
        team_capacity[item["week_start"]] += float(item.get("available_hours") or 0)
    for item in allocations:
        all_planned[item["week_start"]] += float(item.get("hours") or 0)
    for item in non_project:
        all_planned[item["week_start"]] += float(item.get("hours") or 0)

    summary_rows = []
    for label, values in (
        ("TOTAL PLANNED", all_planned),
        ("TEAM CAPACITY", team_capacity),
    ):
        summary_row: dict[str, Any] = {"Summary": label}
        for week in weeks:
            summary_row[week.isoformat()] = round(values[week], 1)
        summary_rows.append(summary_row)
    free_row: dict[str, Any] = {"Summary": "FREE / OVER"}
    for week in weeks:
        free_row[week.isoformat()] = round(team_capacity[week] - all_planned[week], 1)
    summary_rows.append(free_row)

    return pd.DataFrame(rows), pd.DataFrame(summary_rows), weeks


def build_processor_project_matrix(
    department: str,
    person_name: str,
    *,
    start_week: date,
    horizon_weeks: int,
    engine=None,
) -> tuple[pd.DataFrame, pd.DataFrame, list[date], list[str], dict[tuple[str, str], int]]:
    """Show one processor's projects/activities and weekly availability."""
    engine = engine or get_engine()
    department = str(department).upper()
    weeks = week_starts(start_week, horizon_weeks)
    if not weeks:
        return pd.DataFrame(), pd.DataFrame(), [], [], {}

    capacity_rows = weekly_person_capacity(
        start_week,
        horizon_weeks,
        department=department,
        engine=engine,
    )
    people = sorted({
        row["person_name"]
        for row in capacity_rows
        if float(row.get("available_hours") or 0) > 0 or row["person_name"] == person_name
    })
    if not person_name and people:
        person_name = people[0]

    projects = {
        row["project_code"]: row["project_name"]
        for row in list_projects(active_only=False, engine=engine)
    }
    allocations = [
        row
        for row in list_weekly_allocations(
            department=department,
            start_week=weeks[0],
            end_week=weeks[-1],
            engine=engine,
        )
        if row["person_name"] == person_name
    ]
    versions = {
        (row["project_code"], row["week_start"].isoformat()): int(row.get("version") or 0)
        for row in allocations
    }
    non_project = [
        row
        for row in list_non_project_allocations(
            department=department,
            start_week=weeks[0],
            end_week=weeks[-1],
            engine=engine,
        )
        if row["person_name"] == person_name
    ]

    project_week: dict[tuple[str, date], float] = defaultdict(float)
    for row in allocations:
        project_week[(row["project_code"], row["week_start"])] += float(row.get("hours") or 0)

    rows: list[dict[str, Any]] = []
    for code in sorted({row["project_code"] for row in allocations}, key=lambda value: projects.get(value, value)):
        total = round(sum(project_week[(code, week)] for week in weeks), 1)
        row: dict[str, Any] = {
            "Project Code": code,
            "Project / Activity": projects.get(code, code),
            "Type": "PROJECT",
            "Total h": total,
        }
        for week in weeks:
            row[week.isoformat()] = round(project_week[(code, week)], 1)
        rows.append(row)

    activity_week: dict[tuple[str, date], float] = defaultdict(float)
    for item in non_project:
        activity_week[(item["activity_name"], item["week_start"])] += float(item.get("hours") or 0)
    for activity in sorted({row["activity_name"] for row in non_project}):
        total = round(sum(activity_week[(activity, week)] for week in weeks), 1)
        row = {
            "Project Code": "",
            "Project / Activity": f"ACTIVITY: {activity}",
            "Type": "ACTIVITY",
            "Total h": total,
        }
        for week in weeks:
            row[week.isoformat()] = round(activity_week[(activity, week)], 1)
        rows.append(row)

    available: dict[date, float] = defaultdict(float)
    for row in capacity_rows:
        if row["person_name"] == person_name:
            available[row["week_start"]] += float(row.get("available_hours") or 0)

    planned: dict[date, float] = defaultdict(float)
    for row in allocations:
        planned[row["week_start"]] += float(row.get("hours") or 0)
    for row in non_project:
        planned[row["week_start"]] += float(row.get("hours") or 0)

    summary_rows = []
    for label, values in (
        ("AVAILABLE", available),
        ("PLANNED", planned),
    ):
        item: dict[str, Any] = {"Summary": label}
        for week in weeks:
            item[week.isoformat()] = round(values[week], 1)
        summary_rows.append(item)
    free = {"Summary": "FREE / OVER"}
    for week in weeks:
        free[week.isoformat()] = round(available[week] - planned[week], 1)
    summary_rows.append(free)

    return pd.DataFrame(rows), pd.DataFrame(summary_rows), weeks, people, versions


def changed_processor_matrix_cells(
    original: pd.DataFrame,
    edited: pd.DataFrame,
    *,
    department: str,
    person_name: str,
    weeks: list[date],
    versions: dict[tuple[str, str], int],
) -> list[dict[str, Any]]:
    """Diff editable project rows in a processor drill-down matrix."""
    if original.empty and edited.empty:
        return []

    original_by_code = {
        str(row.get("Project Code") or ""): row
        for row in original.to_dict("records")
        if str(row.get("Project Code") or "").strip()
    }
    changes: list[dict[str, Any]] = []
    for row in edited.to_dict("records"):
        if str(row.get("Type") or "").upper() != "PROJECT":
            continue
        project_code = str(row.get("Project Code") or "").strip()
        if not project_code:
            continue
        old_row = original_by_code.get(project_code, {})
        for week in weeks:
            key = week.isoformat()
            old = round(float(old_row.get(key) or 0), 2)
            new = round(max(float(row.get(key) or 0), 0), 2)
            if abs(old - new) <= 0.005:
                continue
            changes.append(
                {
                    "project_code": project_code,
                    "department": department,
                    "person_name": person_name,
                    "week_start": week,
                    "hours": new,
                    "expected_version": versions.get((project_code, key), 0),
                }
            )
    return changes


def changed_grid_cells(
    original: pd.DataFrame,
    edited: pd.DataFrame,
    *,
    project_code: str,
    department: str,
    weeks: list[date],
    versions: dict[tuple[str, str], int],
) -> list[dict[str, Any]]:
    """Diff the edited grid and return only changed allocation cells."""
    if original.empty and edited.empty:
        return []

    original_by_person = {
        str(row["Person"]): row for row in original.to_dict("records")
    }
    changes: list[dict[str, Any]] = []
    for row in edited.to_dict("records"):
        person = str(row.get("Person") or "").strip()
        if not person:
            continue
        old_row = original_by_person.get(person, {})
        for week in weeks:
            key = week.isoformat()
            old = round(float(old_row.get(key) or 0), 2)
            new = round(max(float(row.get(key) or 0), 0), 2)
            if abs(old - new) <= 0.005:
                continue
            changes.append(
                {
                    "project_code": project_code,
                    "department": department,
                    "person_name": person,
                    "week_start": week,
                    "hours": new,
                    "expected_version": versions.get((person, key), 0),
                }
            )
    return changes


def save_grid_changes(
    changes: list[dict[str, Any]],
    *,
    user: str,
    engine=None,
) -> list[dict[str, Any]]:
    if not changes:
        return []
    return save_weekly_allocations_batch(changes, user=user, engine=engine)


def project_workload_card(project_code: str, department: str, *, engine=None) -> dict[str, Any]:
    queue = build_work_queue(engine=engine)
    match = next(
        (row for row in queue[department] if row["Project Code"] == project_code),
        None,
    )
    if match is None:
        raise ValueError(f"Project {project_code} is not in the {department} work queue.")
    return match
