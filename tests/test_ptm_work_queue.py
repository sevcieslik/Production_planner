from __future__ import annotations

from datetime import date

from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from app.data.planner_store import (
    init_planner_store,
    save_stage_input,
    save_weekly_allocation,
    upsert_actual_hours,
    upsert_high_level_projects,
)
from app.services.ptm_work_queue import action_label, build_work_queue, forecast_finish


def _engine():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    init_planner_store(engine)
    return engine


def test_work_queue_matches_ptm_remaining_precedence_and_forecast():
    engine = _engine()
    upsert_high_level_projects(
        [{
            "project_code": "NM1",
            "project_name": "Example",
            "active": True,
            "project_manager": "PM",
            "priority": "1",
            "production_deadline": date(2026, 10, 30),
            "delivery_deadline": None,
            "bid_rs_h": 100,
            "bid_gis_h": 200,
            "bid_pls_h": 50,
            "notes": None,
            "last_pm_update": None,
        }],
        engine=engine,
    )
    upsert_actual_hours(
        [{"project_code": "NM1", "department": "GIS", "actual_hours": 40}],
        engine=engine,
    )
    save_stage_input(
        "NM1",
        "GIS",
        estimate_hours=180,
        remaining_override_hours=None,
        handover_start=None,
        notes=None,
        user="Dom",
        expected_version=0,
        engine=engine,
    )
    save_weekly_allocation(
        "NM1", "GIS", "A", date(2026, 10, 5), 70,
        user="Dom", expected_version=0, engine=engine,
    )
    save_weekly_allocation(
        "NM1", "GIS", "A", date(2026, 10, 12), 70,
        user="Dom", expected_version=0, engine=engine,
    )

    queue = build_work_queue(today=date(2026, 10, 1), engine=engine)
    gis = queue["GIS"][0]
    assert gis["Remaining h"] == 140
    assert gis["Remaining Source"] == "PTM ESTIMATE"
    assert gis["Planned h"] == 140
    assert gis["Forecast"] == date(2026, 10, 16)
    assert gis["Action"] == "OK"


def test_upstream_ready_uses_manual_handover_before_forecast():
    engine = _engine()
    upsert_high_level_projects(
        [{
            "project_code": "NM2",
            "project_name": "Sequence",
            "active": True,
            "project_manager": "PM",
            "priority": "2",
            "production_deadline": date(2026, 12, 1),
            "delivery_deadline": None,
            "bid_rs_h": 100,
            "bid_gis_h": 100,
            "bid_pls_h": 100,
            "notes": None,
            "last_pm_update": None,
        }],
        engine=engine,
    )
    save_stage_input(
        "NM2",
        "RS",
        estimate_hours=None,
        remaining_override_hours=None,
        handover_start=date(2026, 10, 20),
        notes=None,
        user="Luke",
        expected_version=0,
        engine=engine,
    )
    queue = build_work_queue(today=date(2026, 10, 1), engine=engine)
    gis = queue["GIS"][0]
    assert gis["Upstream Ready"] == date(2026, 10, 20)
    assert gis["Upstream Source"] == "HANDOVER"


def test_forecast_and_action_helpers():
    assert forecast_finish(
        {date(2026, 10, 5): 20, date(2026, 10, 12): 30},
        50,
    ) == date(2026, 10, 16)
    assert action_label(None, 0, None, None) == "ESTIMATE"
    assert action_label(40, 0, None, None) == "PLAN 40.0 h"
    assert action_label(0, 8, None, None) == "OVER +8.0 h"
