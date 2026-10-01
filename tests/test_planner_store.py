from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from app.data.planner_store import (
    AllocationConflict,
    StageInputConflict,
    heartbeat_session,
    init_planner_store,
    list_active_sessions,
    list_projects,
    save_stage_input,
    save_weekly_allocation,
    upsert_high_level_projects,
)


@pytest.fixture()
def engine():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    init_planner_store(engine)
    yield engine
    engine.dispose()


def _project(**overrides):
    record = {
        "project_code": "NMCP26054",
        "project_name": "ATCO",
        "active": True,
        "project_manager": "Darren W.",
        "priority": "4",
        "production_deadline": date(2026, 12, 11),
        "delivery_deadline": None,
        "bid_rs_h": 240.0,
        "bid_gis_h": 700.0,
        "bid_pls_h": None,
        "notes": "Awaiting capture",
        "last_pm_update": None,
    }
    record.update(overrides)
    return record


def test_high_level_upsert_only_changes_source_fields(engine):
    first = upsert_high_level_projects([_project()], engine=engine)
    assert first == {"inserted": 1, "updated": 0, "unchanged": 0}

    unchanged = upsert_high_level_projects([_project()], engine=engine)
    assert unchanged == {"inserted": 0, "updated": 0, "unchanged": 1}

    changed = upsert_high_level_projects(
        [_project(priority="1", bid_gis_h=725.0)], engine=engine
    )
    assert changed == {"inserted": 0, "updated": 1, "unchanged": 0}
    project = list_projects(engine=engine)[0]
    assert project["priority"] == "1"
    assert project["bid_gis_h"] == 725.0


def test_weekly_allocation_optimistic_locking(engine):
    upsert_high_level_projects([_project()], engine=engine)

    created = save_weekly_allocation(
        "NMCP26054",
        "GIS",
        "Processor, Test",
        date(2026, 10, 5),
        32,
        user="Dom",
        expected_version=0,
        engine=engine,
    )
    assert created["version"] == 1
    assert created["hours"] == 32

    updated = save_weekly_allocation(
        "NMCP26054",
        "GIS",
        "Processor, Test",
        date(2026, 10, 5),
        40,
        user="Carlos",
        expected_version=1,
        engine=engine,
    )
    assert updated["version"] == 2
    assert updated["hours"] == 40
    assert updated["updated_by"] == "Carlos"

    with pytest.raises(AllocationConflict) as conflict:
        save_weekly_allocation(
            "NMCP26054",
            "GIS",
            "Processor, Test",
            date(2026, 10, 5),
            20,
            user="Dom",
            expected_version=1,
            engine=engine,
        )
    assert conflict.value.current["version"] == 2
    assert conflict.value.current["hours"] == 40


def test_stage_input_optimistic_locking(engine):
    upsert_high_level_projects([_project()], engine=engine)
    created = save_stage_input(
        "NMCP26054",
        "GIS",
        estimate_hours=780,
        remaining_override_hours=None,
        handover_start=date(2026, 10, 12),
        notes="Initial estimate",
        user="Dom",
        expected_version=0,
        engine=engine,
    )
    assert created["version"] == 1

    with pytest.raises(StageInputConflict):
        save_stage_input(
            "NMCP26054",
            "GIS",
            estimate_hours=800,
            remaining_override_hours=None,
            handover_start=date(2026, 10, 12),
            notes="Stale browser tab",
            user="Carlos",
            expected_version=0,
            engine=engine,
        )


def test_presence_is_non_blocking_and_lists_active_users(engine):
    heartbeat_session(
        "session-a",
        user_email="carlos@example.com",
        display_name="Carlos",
        role="manager",
        current_view="Planner PLS",
        department="PLS",
        project_code="NMCP26054",
        editing_scope="weekly allocations",
        engine=engine,
    )
    heartbeat_session(
        "session-b",
        user_email="dom@example.com",
        display_name="Dom",
        role="manager",
        current_view="Planner GIS",
        department="GIS",
        engine=engine,
    )

    active = list_active_sessions(exclude_session_id="session-a", engine=engine)
    assert [row["display_name"] for row in active] == ["Dom"]
    assert active[0]["department"] == "GIS"
