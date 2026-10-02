from __future__ import annotations

from datetime import date

from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from app.data.planner_store import init_planner_store, upsert_high_level_projects
from app.services.ptm_planner_grid import (
    allocation_grid_summary,
    build_department_grid,
    build_processor_project_matrix,
    build_team_project_matrix,
    changed_grid_cells,
    changed_processor_matrix_cells,
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


def test_grid_exposes_week_helper_state_for_cell_formatting():
    engine = _engine()
    _seed(engine)
    frame, _, weeks = build_department_grid(
        "GIS", "NM1", start_week=date(2026, 10, 5), horizon_weeks=1, engine=engine
    )
    week = weeks[0].isoformat()
    row = frame.iloc[0]
    assert row[f"__available__{week}"] == 40
    assert row[f"__other__{week}"] == 0
    assert bool(row[f"__timeoff__{week}"]) is False


def test_team_by_project_and_processor_drilldown_share_allocation_records():
    engine = _engine()
    _seed(engine)
    original, versions, weeks = build_department_grid(
        "GIS", "NM1", start_week=date(2026, 10, 5), horizon_weeks=2, engine=engine
    )
    edited = original.copy()
    edited.loc[0, weeks[0].isoformat()] = 24
    edited.loc[0, weeks[1].isoformat()] = 16
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

    team, team_summary, team_weeks = build_team_project_matrix(
        "GIS", start_week=date(2026, 10, 5), horizon_weeks=2, engine=engine
    )
    project = next(row for row in team.to_dict("records") if row["Project Code"] == "NM1")
    assert project["Planned h"] == 40
    assert project[team_weeks[0].isoformat()] == 24
    assert project[team_weeks[1].isoformat()] == 16

    summary_rows = {row["Summary"]: row for row in team_summary.to_dict("records")}
    assert summary_rows["TOTAL PLANNED"][team_weeks[0].isoformat()] == 24
    assert summary_rows["TEAM CAPACITY"][team_weeks[0].isoformat()] == 40

    detail, detail_summary, detail_weeks, people, versions = build_processor_project_matrix(
        "GIS",
        "User, Test",
        start_week=date(2026, 10, 5),
        horizon_weeks=2,
        engine=engine,
    )
    assert "User, Test" in people
    detail_project = next(row for row in detail.to_dict("records") if row["Project Code"] == "NM1")
    assert detail_project["Total h"] == 40
    detail_rows = {row["Summary"]: row for row in detail_summary.to_dict("records")}
    assert detail_rows["PLANNED"][detail_weeks[0].isoformat()] == 24
    assert detail_rows["FREE / OVER"][detail_weeks[0].isoformat()] == 16


def test_processor_drilldown_diff_saves_project_cells_only():
    engine = _engine()
    _seed(engine)
    original, versions, weeks = build_department_grid(
        "GIS", "NM1", start_week=date(2026, 10, 5), horizon_weeks=1, engine=engine
    )
    edited = original.copy()
    edited.loc[0, weeks[0].isoformat()] = 8
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

    detail, _, detail_weeks, _, detail_versions = build_processor_project_matrix(
        "GIS",
        "User, Test",
        start_week=date(2026, 10, 5),
        horizon_weeks=1,
        engine=engine,
    )
    changed = detail.copy()
    changed.loc[changed["Project Code"] == "NM1", detail_weeks[0].isoformat()] = 12

    changes = changed_processor_matrix_cells(
        detail,
        changed,
        department="GIS",
        person_name="User, Test",
        weeks=detail_weeks,
        versions=detail_versions,
    )
    assert len(changes) == 1
    assert changes[0]["project_code"] == "NM1"
    assert changes[0]["hours"] == 12
    assert changes[0]["expected_version"] == 1
