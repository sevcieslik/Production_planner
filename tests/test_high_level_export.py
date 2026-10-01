from __future__ import annotations

from datetime import date

from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from app.data.planner_store import (
    init_planner_store,
    save_stage_input,
    save_weekly_allocation,
    upsert_high_level_projects,
    upsert_non_project_allocations,
)
from app.services.high_level_export import (
    allocation_import_rows,
    capacity_import_rows,
    planning_import_rows,
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


def test_planning_export_matches_high_level_import_shape():
    engine = _engine()
    _seed(engine)
    save_stage_input(
        "NM1",
        "GIS",
        estimate_hours=180,
        remaining_override_hours=None,
        handover_start=date(2026, 10, 12),
        notes=None,
        user="Dom",
        expected_version=0,
        engine=engine,
    )
    save_weekly_allocation(
        "NM1",
        "GIS",
        "User, Test",
        date(2026, 10, 5),
        40,
        user="Dom",
        expected_version=0,
        engine=engine,
    )

    rows = planning_import_rows(today=date(2026, 10, 1), engine=engine)
    gis = next(row for row in rows if row["Department"] == "GIS")
    assert list(gis) == [
        "Project Code", "Project", "Department", "Bid h", "PTM Estimate h",
        "Actual h", "Remaining h", "Planned Future h", "Forecast Finish",
        "PM Deadline", "Planning Status", "Variance Days", "Last Plan Update",
        "Manager", "Previous Forecast", "Forecast Changed", "Handover Start",
    ]
    assert gis["Planned Future h"] == 40
    assert gis["Planning Status"] == "PARTIALLY PLANNED"
    assert gis["Handover Start"] == date(2026, 10, 12)


def test_capacity_and_allocation_exports_include_non_project_work():
    engine = _engine()
    _seed(engine)
    save_weekly_allocation(
        "NM1",
        "GIS",
        "User, Test",
        date(2026, 10, 5),
        24,
        user="Dom",
        expected_version=0,
        engine=engine,
    )
    upsert_non_project_allocations(
        [{
            "activity_name": "Training",
            "department": "GIS",
            "person_name": "User, Test",
            "week_start": date(2026, 10, 5),
            "hours": 8,
        }],
        user="Dom",
        engine=engine,
    )

    capacity = capacity_import_rows(
        start_week=date(2026, 10, 5), horizon_weeks=1, engine=engine
    )
    gis = next(row for row in capacity if row["Department"] == "GIS")
    assert gis["Available h"] == 40
    assert gis["Project Planned h"] == 24
    assert gis["Non-project h"] == 8
    assert gis["Total Planned h"] == 32
    assert gis["Unallocated h"] == 8

    allocations = allocation_import_rows(
        start_week=date(2026, 10, 5), engine=engine
    )
    assert any(row["Work Type"] == "PROJECT" and row["Planned h"] == 24 for row in allocations)
    assert any(
        row["Work Type"] == "NON_PROJECT"
        and row["Project"] == "Training"
        and row["Planned h"] == 8
        for row in allocations
    )
