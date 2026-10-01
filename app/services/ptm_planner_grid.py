from __future__ import annotations

from collections import defaultdict
from datetime import date
from typing import Any

import pandas as pd

from app.data.planner_store import (
    get_engine,
    list_projects,
    list_weekly_allocations,
    save_weekly_allocations_batch,
)
from app.services.ptm_capacity import weekly_person_capacity, week_starts
from app.services.ptm_work_queue import build_work_queue


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
            "Time Off": any(time_off.get((person, week), False) for week in weeks),
        }
        for week in weeks:
            row[week.isoformat()] = round(selected[(person, week)], 1)
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


def changed_grid_cells(
    original: pd.DataFrame,
    edited: pd.DataFrame,
    *,
    project_code: str,
    department: str,
    weeks: list[date],
    versions: dict[tuple[str, str], int],
) -> list[dict[str, Any]]:
    """Diff Streamlit's edited grid and return only changed allocation cells."""
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
