from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from typing import Any

from app.data.planner_store import (
    actual_hours_map,
    get_engine,
    list_projects,
    list_stage_inputs,
    list_weekly_allocations,
)

DEPARTMENTS = ("RS", "GIS", "PLS")
UPSTREAM = {"RS": None, "GIS": "RS", "PLS": "GIS"}


def monday(value: date) -> date:
    return value - timedelta(days=value.weekday())


def _bid_hours(project: dict[str, Any], department: str) -> float | None:
    value = project.get(f"bid_{department.lower()}_h")
    return None if value is None else float(value)


def target_remaining(
    project: dict[str, Any],
    department: str,
    stage: dict[str, Any] | None,
    actual: float,
) -> dict[str, Any]:
    """Mirror the PTM View remaining-hours precedence."""
    bid = _bid_hours(project, department)
    estimate = None if stage is None else stage.get("estimate_hours")
    override = None if stage is None else stage.get("remaining_override_hours")

    if override is not None:
        remaining = max(float(override), 0)
        source = "OVERRIDE"
    elif estimate is not None:
        remaining = max(float(estimate) - actual, 0)
        source = "PTM ESTIMATE"
    elif bid is not None:
        remaining = max(float(bid) - actual, 0)
        source = "BID"
    else:
        remaining = None
        source = "NONE"

    return {
        "bid_hours": bid,
        "estimate_hours": None if estimate is None else float(estimate),
        "actual_hours": float(actual or 0),
        "remaining_hours": remaining,
        "remaining_source": source,
        "remaining_override_hours": None if override is None else float(override),
    }


def forecast_finish(
    weekly_hours: dict[date, float], target_remaining_hours: float | None
) -> date | None:
    if target_remaining_hours is None or target_remaining_hours <= 0:
        return None
    cumulative = 0.0
    for week_start in sorted(weekly_hours):
        cumulative += max(float(weekly_hours[week_start] or 0), 0)
        if cumulative + 0.01 >= float(target_remaining_hours):
            return week_start + timedelta(days=4)
    return None


def action_label(
    remaining_hours: float | None,
    planned_hours: float,
    forecast: date | None,
    variance_days: int | None,
) -> str:
    if remaining_hours is None:
        return "ESTIMATE"

    remaining = max(float(remaining_hours), 0)
    planned = max(float(planned_hours), 0)

    if remaining <= 0.01:
        return f"OVER +{planned:.1f} h" if planned > 0.01 else "COMPLETE"

    gap = max(remaining - planned, 0)
    over = max(planned - remaining, 0)

    if planned <= 0.01:
        return f"PLAN {remaining:.1f} h"
    if gap > 0.01:
        return f"PLAN {gap:.1f} h"
    if forecast is not None and variance_days is not None and variance_days > 0:
        return f"LATE +{variance_days}d"
    if over > 0.01:
        return f"OVER +{over:.1f} h"
    return "OK"


def build_work_queue(
    *,
    today: date | None = None,
    engine=None,
) -> dict[str, list[dict[str, Any]]]:
    """Build the three PTM Projects / Work Queue sections from source records."""
    engine = engine or get_engine()
    current_week = monday(today or date.today())
    projects = list_projects(active_only=True, engine=engine)
    stages = {
        (row["project_code"], row["department"]): row
        for row in list_stage_inputs(engine=engine)
    }
    actuals = actual_hours_map(engine=engine)
    allocations = list_weekly_allocations(start_week=current_week, engine=engine)

    weekly: dict[tuple[str, str], dict[date, float]] = defaultdict(lambda: defaultdict(float))
    for row in allocations:
        key = (row["project_code"], row["department"])
        weekly[key][row["week_start"]] += float(row["hours"] or 0)

    result: dict[str, list[dict[str, Any]]] = {department: [] for department in DEPARTMENTS}

    for department in DEPARTMENTS:
        for project in projects:
            code = project["project_code"]
            stage = stages.get((code, department))
            actual = actuals.get((code, department), 0.0)
            target = target_remaining(project, department, stage, actual)
            future = weekly.get((code, department), {})
            planned = round(sum(future.values()), 2)
            forecast = forecast_finish(future, target["remaining_hours"])
            deadline = project.get("production_deadline") or project.get("delivery_deadline")
            variance_days = (forecast - deadline).days if forecast and deadline else None

            upstream_department = UPSTREAM[department]
            if upstream_department is None:
                upstream_ready: date | str = "N/A"
                upstream_source = "NONE"
            else:
                upstream_stage = stages.get((code, upstream_department))
                upstream_handover = (
                    upstream_stage.get("handover_start") if upstream_stage is not None else None
                )
                if upstream_handover:
                    upstream_ready = upstream_handover
                    upstream_source = "HANDOVER"
                else:
                    upstream_target = target_remaining(
                        project,
                        upstream_department,
                        upstream_stage,
                        actuals.get((code, upstream_department), 0.0),
                    )
                    if (
                        upstream_target["remaining_hours"] is not None
                        and upstream_target["remaining_hours"] <= 0.01
                    ):
                        upstream_ready = "Available now"
                        upstream_source = "COMPLETE"
                    else:
                        upstream_forecast = forecast_finish(
                            weekly.get((code, upstream_department), {}),
                            upstream_target["remaining_hours"],
                        )
                        if upstream_forecast:
                            upstream_ready = upstream_forecast
                            upstream_source = "FORECAST"
                        else:
                            upstream_ready = "Not set"
                            upstream_source = "NONE"

            result[department].append(
                {
                    "Project Code": code,
                    "Project": project["project_name"],
                    "Priority": project.get("priority"),
                    "PM Deadline": deadline,
                    "Upstream Ready": upstream_ready,
                    "Bid h": target["bid_hours"],
                    "Estimate h": target["estimate_hours"],
                    "Actual h": target["actual_hours"],
                    "Remaining h": target["remaining_hours"],
                    "Remaining Source": target["remaining_source"],
                    "Planned h": planned,
                    "Forecast": forecast,
                    "Variance days": variance_days,
                    "Action": action_label(
                        target["remaining_hours"], planned, forecast, variance_days
                    ),
                    "Manager Input Version": 0 if stage is None else int(stage["version"]),
                    "Upstream Source": upstream_source,
                }
            )

        result[department].sort(
            key=lambda row: (
                int(row["Priority"]) if str(row.get("Priority") or "").isdigit() else 999,
                row["PM Deadline"] or date.max,
                row["Project"],
            )
        )

    return result
