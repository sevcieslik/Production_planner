from __future__ import annotations

from typing import Any

from app.data.planner_store import (
    get_engine,
    list_projects,
    record_sync_status,
    upsert_actual_hours,
)
from app.integrations.dot_times import read_dot_actuals

DOT_SYNC_SOURCE = "DoT times/master"


def sync_dot_actuals(*, spreadsheet_id: str | None = None, engine=None) -> dict[str, Any]:
    """Refresh source-controlled project actual hours without touching manager planning fields."""
    engine = engine or get_engine()
    project_codes = {
        row["project_code"] for row in list_projects(active_only=False, engine=engine)
    }
    records, source_max_timestamp, processed = read_dot_actuals(
        spreadsheet_id=spreadsheet_id,
        project_codes=project_codes,
    )
    result = upsert_actual_hours(records, engine=engine)
    status = record_sync_status(
        DOT_SYNC_SOURCE,
        rows_processed=processed,
        source_max_timestamp=source_max_timestamp,
        details=f"{len(records)} project/department totals",
        engine=engine,
    )
    return {
        **result,
        "source_rows_processed": processed,
        "aggregates": len(records),
        "source_max_timestamp": source_max_timestamp,
        "last_synced_at": status["last_synced_at"],
    }
