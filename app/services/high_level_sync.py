from __future__ import annotations

from typing import Any, Iterable

from app.data.planner_store import get_engine, init_planner_store, upsert_high_level_projects
from app.integrations.google_sheets import normalise_high_level_projects, read_high_level_projects


def sync_high_level_projects(
    *,
    rows: Iterable[dict[str, Any]] | None = None,
    spreadsheet_id: str | None = None,
    engine=None,
) -> dict[str, int]:
    """Synchronise source-controlled High Level project fields into Planner."""
    engine = engine or get_engine()
    init_planner_store(engine)

    if rows is None:
        projects = read_high_level_projects(spreadsheet_id=spreadsheet_id)
    else:
        raw = list(rows)
        # Accept either raw High Level headers or already-normalised planner records.
        if raw and "project_code" in raw[0]:
            projects = raw
        else:
            projects = normalise_high_level_projects(raw)

    return upsert_high_level_projects(projects, engine=engine)
