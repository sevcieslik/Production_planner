from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from typing import Any, Iterable

from app.data.planner_store import DEPARTMENTS, get_engine, list_calendar_days
from app.services.ptm_resources import list_assignments, list_people, list_time_off


def monday(value: date) -> date:
    return value - timedelta(days=value.weekday())


def week_starts(start: date, weeks: int) -> list[date]:
    first = monday(start)
    return [first + timedelta(weeks=i) for i in range(max(0, int(weeks)))]


def _active_on_day(person: dict[str, Any], day: date) -> bool:
    if not bool(person.get("active")):
        return False
    active_from = person.get("active_from")
    active_to = person.get("active_to")
    return (active_from is None or day >= active_from) and (active_to is None or day <= active_to)


def _in_range(day: date, start: date, end: date | None) -> bool:
    return day >= start and (end is None or day <= end)


def department_mix_for_day(
    person: dict[str, Any],
    assignments: Iterable[dict[str, Any]],
    day: date,
) -> dict[str, float]:
    """Return the PTM-style daily department split, including home-team remainder."""
    home = str(person.get("home_department") or "").upper()
    active = [
        row
        for row in assignments
        if row.get("person_name") == person.get("person_name")
        and row.get("from_date") is not None
        and _in_range(day, row["from_date"], row.get("to_date"))
        and float(row.get("allocation_percent") or 0) > 0
    ]
    if not active:
        return {home: 1.0} if home in DEPARTMENTS else {}

    total = sum(float(row.get("allocation_percent") or 0) for row in active)
    scale = 1.0 / total if total > 1.0 else 1.0
    mix: dict[str, float] = defaultdict(float)
    for row in active:
        department = str(row.get("department") or "").upper()
        if department not in DEPARTMENTS:
            continue
        mix[department] += float(row.get("allocation_percent") or 0) * scale

    used = sum(mix.values())
    if home in DEPARTMENTS and used < 0.999999:
        mix[home] += 1.0 - used

    return {department: fraction for department, fraction in mix.items() if fraction > 0.000001}


def available_hours_for_day(
    person: dict[str, Any],
    time_off_rows: Iterable[dict[str, Any]],
    day: date,
    *,
    working_day: bool | None = None,
) -> tuple[float, bool]:
    is_working_day = day.weekday() < 5 if working_day is None else bool(working_day)
    if not is_working_day or not _active_on_day(person, day):
        return 0.0, False

    standard = max(float(person.get("standard_hours_day") or 0), 0)
    overlaps = [
        row
        for row in time_off_rows
        if row.get("person_name") == person.get("person_name")
        and row.get("from_date") is not None
        and _in_range(day, row["from_date"], row.get("to_date"))
    ]
    if not overlaps:
        return standard, False

    caps = [max(float(row.get("available_hours_day") or 0), 0) for row in overlaps]
    return min([standard, *caps]), True


def weekly_person_capacity(
    start_week: date,
    horizon_weeks: int,
    *,
    department: str | None = None,
    engine=None,
) -> list[dict[str, Any]]:
    """Calculate per-person weekly capacity using daily roster/absence/assignment rules."""
    engine = engine or get_engine()
    if department is not None:
        department = str(department).upper()
        if department not in DEPARTMENTS:
            raise ValueError(f"Unknown department: {department}")

    people = list_people(active_only=False, engine=engine)
    assignments = list_assignments(engine=engine)
    time_off = list_time_off(engine=engine)
    weeks = week_starts(start_week, horizon_weeks)
    if weeks:
        calendar_rows = list_calendar_days(
            start_date=weeks[0],
            end_date=weeks[-1] + timedelta(days=6),
            engine=engine,
        )
    else:
        calendar_rows = []
    working_days = {
        row["work_date"]: bool(row["effective_working_day"]) for row in calendar_rows
    }
    output: list[dict[str, Any]] = []

    for person in people:
        for week in weeks:
            totals = {code: 0.0 for code in DEPARTMENTS}
            has_time_off = False
            for offset in range(7):
                day = week + timedelta(days=offset)
                available, off = available_hours_for_day(
                    person,
                    time_off,
                    day,
                    working_day=working_days.get(day),
                )
                has_time_off = has_time_off or off
                if available <= 0:
                    continue
                mix = department_mix_for_day(person, assignments, day)
                for code, fraction in mix.items():
                    totals[code] += available * fraction

            departments = [department] if department else DEPARTMENTS
            for code in departments:
                hours = round(totals[code], 2)
                # Keep a home/assigned person visible even when Time Off reduces
                # the current week to zero. The UI can distinguish zero capacity.
                relevant = (
                    code == person.get("home_department")
                    or hours > 0
                    or any(
                        row.get("person_name") == person.get("person_name")
                        and row.get("department") == code
                        and row.get("from_date") is not None
                        and _in_range(week, row["from_date"], row.get("to_date"))
                        for row in assignments
                    )
                )
                if not relevant:
                    continue
                output.append(
                    {
                        "person_name": person["person_name"],
                        "home_department": person["home_department"],
                        "primary_role": person.get("primary_role"),
                        "department": code,
                        "week_start": week,
                        "available_hours": hours,
                        "time_off": has_time_off,
                    }
                )

    return output


def weekly_team_capacity(
    start_week: date,
    horizon_weeks: int,
    *,
    engine=None,
) -> list[dict[str, Any]]:
    rows = weekly_person_capacity(start_week, horizon_weeks, engine=engine)
    totals: dict[tuple[str, date], float] = defaultdict(float)
    for row in rows:
        totals[(row["department"], row["week_start"])] += float(row["available_hours"] or 0)
    return [
        {
            "department": department,
            "week_start": week,
            "available_hours": round(hours, 2),
        }
        for (department, week), hours in sorted(totals.items(), key=lambda item: (item[0][1], item[0][0]))
    ]
