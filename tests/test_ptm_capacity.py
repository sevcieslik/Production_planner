from __future__ import annotations

from datetime import date

from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from app.data.planner_store import init_planner_store
from app.services.ptm_capacity import weekly_person_capacity, weekly_team_capacity
from app.services.ptm_resources import save_assignment, save_person, save_time_off


def _engine():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    init_planner_store(engine)
    return engine


def test_time_off_reduces_weekly_capacity_and_keeps_person_visible():
    engine = _engine()
    save_person(
        "Test, User",
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
    save_time_off(
        time_off_id=None,
        person_name="Test, User",
        from_date=date(2026, 10, 5),
        to_date=date(2026, 10, 9),
        time_off_type="HOLIDAY",
        available_hours_day=0,
        notes=None,
        user="Admin",
        expected_version=0,
        engine=engine,
    )

    rows = weekly_person_capacity(date(2026, 10, 5), 1, department="GIS", engine=engine)
    assert len(rows) == 1
    assert rows[0]["available_hours"] == 0
    assert rows[0]["time_off"] is True


def test_temporary_assignment_moves_capacity_without_changing_home_department():
    engine = _engine()
    save_person(
        "Processor, One",
        home_department="RS",
        primary_role="Remote Sensing Processor",
        secondary_role="GIS Technician",
        standard_hours_day=8,
        active=True,
        active_from=None,
        active_to=None,
        user="Admin",
        expected_version=0,
        engine=engine,
    )
    save_assignment(
        assignment_id=None,
        person_name="Processor, One",
        from_date=date(2026, 10, 5),
        to_date=date(2026, 10, 9),
        department="GIS",
        allocation_percent=0.5,
        assignment_type="TEMP_SUPPORT",
        notes=None,
        user="Admin",
        expected_version=0,
        engine=engine,
    )

    rows = weekly_person_capacity(date(2026, 10, 5), 1, engine=engine)
    by_department = {row["department"]: row["available_hours"] for row in rows}
    assert by_department["RS"] == 20
    assert by_department["GIS"] == 20


def test_partial_time_off_is_split_after_availability_reduction():
    engine = _engine()
    save_person(
        "Processor, Two",
        home_department="RS",
        primary_role="Remote Sensing Processor",
        secondary_role="GIS Technician",
        standard_hours_day=8,
        active=True,
        active_from=None,
        active_to=None,
        user="Admin",
        expected_version=0,
        engine=engine,
    )
    save_assignment(
        assignment_id=None,
        person_name="Processor, Two",
        from_date=date(2026, 10, 5),
        to_date=date(2026, 10, 9),
        department="GIS",
        allocation_percent=1,
        assignment_type="SECONDMENT",
        notes=None,
        user="Admin",
        expected_version=0,
        engine=engine,
    )
    save_time_off(
        time_off_id=None,
        person_name="Processor, Two",
        from_date=date(2026, 10, 7),
        to_date=date(2026, 10, 7),
        time_off_type="APPOINTMENT",
        available_hours_day=4,
        notes=None,
        user="Admin",
        expected_version=0,
        engine=engine,
    )

    rows = weekly_person_capacity(date(2026, 10, 5), 1, engine=engine)
    by_department = {row["department"]: row["available_hours"] for row in rows}
    assert by_department.get("RS", 0) == 0
    assert by_department["GIS"] == 36


def test_team_capacity_aggregates_people():
    engine = _engine()
    for name in ("A", "B"):
        save_person(
            name,
            home_department="PLS",
            primary_role="PLS Processor",
            secondary_role=None,
            standard_hours_day=7.5,
            active=True,
            active_from=None,
            active_to=None,
            user="Admin",
            expected_version=0,
            engine=engine,
        )
    rows = weekly_team_capacity(date(2026, 10, 5), 1, engine=engine)
    pls = next(row for row in rows if row["department"] == "PLS")
    assert pls["available_hours"] == 75
