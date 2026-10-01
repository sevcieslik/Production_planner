from __future__ import annotations

from datetime import date

from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from app.data.planner_store import init_planner_store, upsert_high_level_projects
from app.services.ptm_planner_grid import (
    allocation_grid_summary,
    build_department_grid,
    changed_grid_cells,
    save_grid_changes,
)
from app.services.ptm_resources import save_person


def _engine():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    init_planner_store(engine)
    return engine


def _seed(engine):
    upsert_high_level_projects(
        [{
            "project_code": "NM1",
            "project_name": "Example",
            "active": True,
            "project_manager": "PM",
            "priority": "1",
            "production_deadline": date(2026, 12, 1),
            "delivery_deadline": None,
            "bid_rs_h": 100,
            "bid_gis_h": 200,
            "bid_pls_h": 50,
            "notes": None,
            "last_pm_update": None,
        }],
        engine=engine,
    )
    save_person(
        "User, Test",
        home_department="GIS",
        primary_role="Geospatial Processor",
        secondary_role=None,
        standard_hours_day=8,
        active=True,
        active_from=None,
        active_to=None,
        user="Admin",
        expected_version=0,
        engine=engine,
    )


def test_grid_diffs_only_changed_cells_and_saves_versions():
    engine = _engine()
    _seed(engine)
    original, versions, weeks = build_department_grid(
        "GIS", "NM1", start_week=date(2026, 10, 5), horizon_weeks=2, engine=engine
    )
    edited = original.copy()
    edited.loc[0, weeks[0].isoformat()] = 24

    changes = changed_grid_cells(
        original,
        edited,
        project_code="NM1",
        department="GIS",
        weeks=weeks,
        versions=versions,
    )
    assert len(changes) == 1
    assert changes[0]["hours"] == 24
    assert changes[0]["expected_version"] == 0

    saved = save_grid_changes(changes, user="Dom", engine=engine)
    assert saved[0]["version"] == 1

    refreshed, refreshed_versions, _ = build_department_grid(
        "GIS", "NM1", start_week=date(2026, 10, 5), horizon_weeks=2, engine=engine
    )
    assert refreshed.loc[0, weeks[0].isoformat()] == 24
    assert refreshed_versions[("User, Test", weeks[0].isoformat())] == 1


def test_grid_summary_uses_same_capacity_and_allocation_records():
    engine = _engine()
    _seed(engine)
    original, versions, weeks = build_department_grid(
        "GIS", "NM1", start_week=date(2026, 10, 5), horizon_weeks=1, engine=engine
    )
    edited = original.copy()
    edited.loc[0, weeks[0].isoformat()] = 32
    save_grid_changes(
        changed_grid_cells(
            original,
            edited,
            project_code="NM1",
            department="GIS",
            weeks=weeks,
            versions=versions,
        ),
        user="Dom",
        engine=engine,
    )

    summary = allocation_grid_summary(
        "GIS", "NM1", start_week=date(2026, 10, 5), horizon_weeks=1, engine=engine
    )
    rows = {row["Summary"]: row for row in summary.to_dict("records")}
    assert rows["PROJECT PLANNED"][weeks[0].isoformat()] == 32
    assert rows["TOTAL PLANNED"][weeks[0].isoformat()] == 32
    assert rows["TEAM CAPACITY"][weeks[0].isoformat()] == 40
    assert rows["FREE / OVER"][weeks[0].isoformat()] == 8
