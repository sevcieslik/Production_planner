from __future__ import annotations

from collections import defaultdict
from datetime import datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from app.data.flow_store import (
    flow_history_for_snapshot,
    flow_movements_between,
    latest_backlog_rows,
    latest_flow_snapshot_at,
    list_flow_config,
)

_LONDON = ZoneInfo("Europe/London")


def _local(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=_LONDON)
    return dt.astimezone(_LONDON)


def _last_tuesday_1500(snapshot_at: datetime) -> datetime:
    local = _local(snapshot_at)
    days_since_tuesday = (local.weekday() - 1) % 7
    candidate_date = local.date() - timedelta(days=days_since_tuesday)
    candidate = datetime.combine(candidate_date, time(15, 0), tzinfo=_LONDON)
    if local <= candidate:
        candidate -= timedelta(days=7)
    return candidate


def _active_config(section: str) -> list[dict[str, Any]]:
    rows = [
        row
        for row in list_flow_config()
        if row["section"] == section and bool(row.get("active", True))
    ]
    return sorted(rows, key=lambda row: int(row.get("sort_order") or 0))


def build_flow_dashboard() -> dict[str, Any] | None:
    snapshot_at = latest_flow_snapshot_at()
    if snapshot_at is None:
        return None

    current_config = _active_config("CURRENT")
    movement_config = _active_config("MOVEMENT")

    current_rows = []
    for row in flow_history_for_snapshot(snapshot_at):
        output: dict[str, Any] = {
            "Project": row["project_name"],
            "Project Code": row["project_code"],
        }
        total = 0.0
        for cfg in current_config:
            value = float(row["metrics"].get(cfg["source_key"]) or 0)
            output[cfg["label"]] = round(value, 3)
            total += abs(value)
        if total > 0.0005:
            current_rows.append(output)

    cutoff = _last_tuesday_1500(snapshot_at)
    movements = flow_movements_between(cutoff, snapshot_at)
    grouped: dict[tuple[str, str], dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for row in movements:
        key = (row["project_code"], row["project_name"])
        for cfg in movement_config:
            grouped[key][cfg["source_key"]] += float(row["metrics"].get(cfg["source_key"]) or 0)

    movement_rows = []
    for (project_code, project_name), metrics in sorted(grouped.items(), key=lambda item: item[0][1]):
        output: dict[str, Any] = {"Project": project_name, "Project Code": project_code}
        total = 0.0
        for cfg in movement_config:
            value = float(metrics.get(cfg["source_key"]) or 0)
            output[cfg["label"]] = round(value, 3)
            total += abs(value)
        if total > 0.0005:
            movement_rows.append(output)

    backlog_at, backlog_rows_raw = latest_backlog_rows()
    backlog_rows = []
    for row in backlog_rows_raw:
        metrics = row["metrics"]
        backlog_rows.append(
            {
                "As Of": row["as_of"],
                "Team": row["team"],
                "Queue km": metrics.get("queue_km"),
                "Historical Capacity km/week": metrics.get("historical_capacity_km_week"),
                "Backlog Weeks": metrics.get("backlog_weeks"),
                "Provisional Backlog Weeks": metrics.get("provisional_backlog_weeks"),
                "Weeks Used": metrics.get("weeks_used"),
                "Lookback Weeks": metrics.get("lookback_weeks"),
                "Target Weeks": metrics.get("target_weeks"),
                "Status": metrics.get("status") or "",
            }
        )

    return {
        "snapshot_at": snapshot_at,
        "current_state": current_rows,
        "current_columns": [cfg["label"] for cfg in current_config],
        "movement_from": cutoff,
        "movement_to": snapshot_at,
        "movement": movement_rows,
        "movement_columns": [cfg["label"] for cfg in movement_config],
        "backlog_at": backlog_at,
        "backlog": backlog_rows,
    }
