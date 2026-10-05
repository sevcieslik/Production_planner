from __future__ import annotations

import os
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable

from sqlalchemy import (
    Boolean,
    Column,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    Index,
    MetaData,
    PrimaryKeyConstraint,
    String,
    Table,
    Text,
    UniqueConstraint,
    and_,
    create_engine,
    delete,
    insert,
    select,
    update,
)
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

DEPARTMENTS = ("RS", "GIS", "PLS")
DEFAULT_PLANNER_DB_PATH = Path("data/planner_v2.sqlite")

metadata = MetaData()

planner_projects = Table(
    "planner_projects",
    metadata,
    Column("project_code", String(64), primary_key=True),
    Column("project_name", String(255), nullable=False),
    Column("active", Boolean, nullable=False, default=True),
    Column("project_manager", String(255)),
    Column("priority", String(64)),
    Column("production_deadline", Date),
    Column("delivery_deadline", Date),
    Column("bid_rs_h", Float),
    Column("bid_gis_h", Float),
    Column("bid_pls_h", Float),
    Column("notes", Text),
    Column("last_pm_update", Date),
    Column("source_updated_at", DateTime(timezone=True), nullable=False),
)

planner_actuals = Table(
    "planner_actuals",
    metadata,
    Column("project_code", String(64), ForeignKey("planner_projects.project_code", ondelete="CASCADE"), nullable=False),
    Column("department", String(8), nullable=False),
    Column("actual_hours", Float, nullable=False, default=0),
    Column("source_name", String(128), nullable=False, default="DoT"),
    Column("source_updated_at", DateTime(timezone=True), nullable=False),
    PrimaryKeyConstraint("project_code", "department"),
)

planner_stage_inputs = Table(
    "planner_stage_inputs",
    metadata,
    Column("project_code", String(64), ForeignKey("planner_projects.project_code", ondelete="CASCADE"), nullable=False),
    Column("department", String(8), nullable=False),
    Column("estimate_hours", Float),
    Column("remaining_override_hours", Float),
    Column("handover_start", Date),
    Column("notes", Text),
    Column("version", Integer, nullable=False, default=1),
    Column("updated_by", String(255), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    PrimaryKeyConstraint("project_code", "department"),
)

planner_weekly_allocations = Table(
    "planner_weekly_allocations",
    metadata,
    Column("project_code", String(64), ForeignKey("planner_projects.project_code", ondelete="CASCADE"), nullable=False),
    Column("department", String(8), nullable=False),
    Column("person_name", String(255), nullable=False),
    Column("week_start", Date, nullable=False),
    Column("hours", Float, nullable=False, default=0),
    Column("version", Integer, nullable=False, default=1),
    Column("updated_by", String(255), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    PrimaryKeyConstraint("project_code", "department", "person_name", "week_start"),
)

planner_non_project_allocations = Table(
    "planner_non_project_allocations",
    metadata,
    Column("activity_name", String(255), nullable=False),
    Column("department", String(8), nullable=False),
    Column("person_name", String(255), nullable=False),
    Column("week_start", Date, nullable=False),
    Column("hours", Float, nullable=False, default=0),
    Column("version", Integer, nullable=False, default=1),
    Column("updated_by", String(255), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    PrimaryKeyConstraint("activity_name", "department", "person_name", "week_start"),
)

planner_calendar = Table(
    "planner_calendar",
    metadata,
    Column("work_date", Date, primary_key=True),
    Column("effective_working_day", Boolean, nullable=False),
    Column("bank_holiday", Boolean, nullable=False, default=False),
    Column("holiday_name", String(255)),
    Column("source", String(128)),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

planner_people = Table(
    "planner_people",
    metadata,
    Column("person_name", String(255), primary_key=True),
    Column("home_department", String(8), nullable=False),
    Column("primary_role", String(255)),
    Column("secondary_role", String(255)),
    Column("standard_hours_day", Float, nullable=False, default=7.5),
    Column("active", Boolean, nullable=False, default=True),
    Column("active_from", Date),
    Column("active_to", Date),
    Column("version", Integer, nullable=False, default=1),
    Column("updated_by", String(255), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

planner_team_assignments = Table(
    "planner_team_assignments",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("person_name", String(255), ForeignKey("planner_people.person_name", ondelete="CASCADE"), nullable=False),
    Column("from_date", Date, nullable=False),
    Column("to_date", Date),
    Column("department", String(8), nullable=False),
    Column("allocation_percent", Float, nullable=False),
    Column("assignment_type", String(64), nullable=False, default="TEMP_SUPPORT"),
    Column("notes", Text),
    Column("version", Integer, nullable=False, default=1),
    Column("updated_by", String(255), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint(
        "person_name", "from_date", "to_date", "department", "assignment_type",
        name="uq_planner_team_assignment",
    ),
)

planner_time_off = Table(
    "planner_time_off",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("person_name", String(255), ForeignKey("planner_people.person_name", ondelete="CASCADE"), nullable=False),
    Column("from_date", Date, nullable=False),
    Column("to_date", Date),
    Column("time_off_type", String(64), nullable=False, default="HOLIDAY"),
    Column("available_hours_day", Float, nullable=False, default=0),
    Column("notes", Text),
    Column("version", Integer, nullable=False, default=1),
    Column("updated_by", String(255), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

integration_sync_status = Table(
    "integration_sync_status",
    metadata,
    Column("source_name", String(128), primary_key=True),
    Column("last_synced_at", DateTime(timezone=True), nullable=False),
    Column("source_max_timestamp", DateTime(timezone=True)),
    Column("rows_processed", Integer, nullable=False, default=0),
    Column("details", Text),
)

active_sessions = Table(
    "active_sessions",
    metadata,
    Column("session_id", String(128), primary_key=True),
    Column("user_email", String(320), nullable=False),
    Column("display_name", String(255), nullable=False),
    Column("role", String(64), nullable=False),
    Column("current_view", String(128)),
    Column("department", String(8)),
    Column("project_code", String(64)),
    Column("editing_scope", String(255)),
    Column("last_seen_at", DateTime(timezone=True), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)

Index("idx_planner_allocations_week", planner_weekly_allocations.c.department, planner_weekly_allocations.c.week_start)
Index("idx_planner_allocations_project", planner_weekly_allocations.c.project_code, planner_weekly_allocations.c.department)
Index("idx_active_sessions_last_seen", active_sessions.c.last_seen_at)
Index("idx_planner_people_department", planner_people.c.home_department, planner_people.c.active)
Index("idx_planner_non_project_week", planner_non_project_allocations.c.department, planner_non_project_allocations.c.week_start)
Index("idx_planner_assignments_dates", planner_team_assignments.c.person_name, planner_team_assignments.c.from_date, planner_team_assignments.c.to_date)
Index("idx_planner_time_off_dates", planner_time_off.c.person_name, planner_time_off.c.from_date, planner_time_off.c.to_date)


class AllocationConflict(RuntimeError):
    """Raised when another user changed an allocation after it was loaded."""

    def __init__(self, message: str, current: dict[str, Any] | None = None):
        super().__init__(message)
        self.current = current


class StageInputConflict(RuntimeError):
    """Raised when a manager-input row was changed by another user."""

    def __init__(self, message: str, current: dict[str, Any] | None = None):
        super().__init__(message)
        self.current = current


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _normalise_database_url(value: str) -> str:
    # Render commonly exposes postgresql://. Explicitly select psycopg v3 so the
    # deployment does not depend on psycopg2 being present.
    if value.startswith("postgres://"):
        value = "postgresql://" + value[len("postgres://") :]
    if value.startswith("postgresql://"):
        value = "postgresql+psycopg://" + value[len("postgresql://") :]
    return value


def planner_database_url() -> str:
    configured = os.getenv("PLANNER_DATABASE_URL") or os.getenv("DATABASE_URL")
    if configured:
        return _normalise_database_url(configured.strip())

    path = Path(os.getenv("PLANNER_DATABASE_PATH", str(DEFAULT_PLANNER_DB_PATH)))
    path.parent.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{path}"


@lru_cache(maxsize=8)
def _engine_for_url(url: str) -> Engine:
    kwargs: dict[str, Any] = {"pool_pre_ping": True}
    if url.startswith("sqlite:"):
        kwargs["connect_args"] = {"check_same_thread": False}
    return create_engine(url, future=True, **kwargs)


def get_engine(url: str | None = None) -> Engine:
    return _engine_for_url(_normalise_database_url(url) if url else planner_database_url())


def reset_engine_cache() -> None:
    """Test/deployment helper used after changing DATABASE_URL."""
    _engine_for_url.cache_clear()


def init_planner_store(engine: Engine | None = None) -> None:
    metadata.create_all(engine or get_engine())


def _validate_department(department: str) -> str:
    value = str(department or "").upper().strip()
    if value not in DEPARTMENTS:
        raise ValueError(f"Unknown department: {department}")
    return value


def _mapping(row) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def upsert_high_level_projects(
    projects: Iterable[dict[str, Any]], engine: Engine | None = None
) -> dict[str, int]:
    """Upsert source-controlled project fields without touching manager-owned data."""
    engine = engine or get_engine()
    init_planner_store(engine)
    counts = {"inserted": 0, "updated": 0, "unchanged": 0}
    now = _utcnow()

    source_fields = [
        "project_name",
        "active",
        "project_manager",
        "priority",
        "production_deadline",
        "delivery_deadline",
        "bid_rs_h",
        "bid_gis_h",
        "bid_pls_h",
        "notes",
        "last_pm_update",
    ]

    with engine.begin() as conn:
        for raw in projects:
            code = str(raw.get("project_code") or "").strip()
            name = str(raw.get("project_name") or "").strip()
            if not code or not name:
                continue

            values = {
                "project_code": code,
                "project_name": name,
                "active": bool(raw.get("active", True)),
                "project_manager": raw.get("project_manager") or None,
                "priority": raw.get("priority") or None,
                "production_deadline": raw.get("production_deadline"),
                "delivery_deadline": raw.get("delivery_deadline"),
                "bid_rs_h": raw.get("bid_rs_h"),
                "bid_gis_h": raw.get("bid_gis_h"),
                "bid_pls_h": raw.get("bid_pls_h"),
                "notes": raw.get("notes") or None,
                "last_pm_update": raw.get("last_pm_update"),
                "source_updated_at": now,
            }

            existing = conn.execute(
                select(planner_projects).where(planner_projects.c.project_code == code)
            ).mappings().first()

            if existing is None:
                conn.execute(insert(planner_projects).values(**values))
                counts["inserted"] += 1
                continue

            changed = any(existing.get(field) != values.get(field) for field in source_fields)
            if not changed:
                counts["unchanged"] += 1
                continue

            conn.execute(
                update(planner_projects)
                .where(planner_projects.c.project_code == code)
                .values(**{field: values[field] for field in source_fields}, source_updated_at=now)
            )
            counts["updated"] += 1

    return counts


def upsert_actual_hours(
    records: Iterable[dict[str, Any]], engine: Engine | None = None
) -> dict[str, int]:
    """Upsert source-controlled actual hours by project and department."""
    engine = engine or get_engine()
    init_planner_store(engine)
    counts = {"inserted": 0, "updated": 0, "unchanged": 0}
    now = _utcnow()

    with engine.begin() as conn:
        for raw in records:
            code = str(raw.get("project_code") or "").strip()
            if not code:
                continue
            department = _validate_department(raw.get("department"))
            actual = round(max(float(raw.get("actual_hours") or 0), 0), 2)
            source_name = str(raw.get("source_name") or "DoT").strip() or "DoT"
            key = and_(
                planner_actuals.c.project_code == code,
                planner_actuals.c.department == department,
            )
            existing = conn.execute(select(planner_actuals).where(key)).mappings().first()
            if existing is None:
                conn.execute(
                    insert(planner_actuals).values(
                        project_code=code,
                        department=department,
                        actual_hours=actual,
                        source_name=source_name,
                        source_updated_at=now,
                    )
                )
                counts["inserted"] += 1
            elif (
                float(existing["actual_hours"] or 0) != actual
                or str(existing["source_name"] or "") != source_name
            ):
                conn.execute(
                    update(planner_actuals)
                    .where(key)
                    .values(
                        actual_hours=actual,
                        source_name=source_name,
                        source_updated_at=now,
                    )
                )
                counts["updated"] += 1
            else:
                counts["unchanged"] += 1
    return counts


def actual_hours_map(*, engine: Engine | None = None) -> dict[tuple[str, str], float]:
    engine = engine or get_engine()
    init_planner_store(engine)
    with engine.connect() as conn:
        rows = conn.execute(select(planner_actuals)).mappings().all()
    return {
        (str(row["project_code"]), str(row["department"])): float(row["actual_hours"] or 0)
        for row in rows
    }


def list_projects(*, active_only: bool = True, engine: Engine | None = None) -> list[dict[str, Any]]:
    engine = engine or get_engine()
    init_planner_store(engine)
    stmt = select(planner_projects)
    if active_only:
        stmt = stmt.where(planner_projects.c.active.is_(True))
    stmt = stmt.order_by(planner_projects.c.priority, planner_projects.c.project_code)
    with engine.connect() as conn:
        return [dict(row) for row in conn.execute(stmt).mappings().all()]


def list_stage_inputs(*, engine: Engine | None = None) -> list[dict[str, Any]]:
    engine = engine or get_engine()
    init_planner_store(engine)
    with engine.connect() as conn:
        return [dict(row) for row in conn.execute(select(planner_stage_inputs)).mappings().all()]


def get_stage_input(
    project_code: str, department: str, *, engine: Engine | None = None
) -> dict[str, Any] | None:
    engine = engine or get_engine()
    department = _validate_department(department)
    init_planner_store(engine)
    stmt = select(planner_stage_inputs).where(
        and_(
            planner_stage_inputs.c.project_code == project_code,
            planner_stage_inputs.c.department == department,
        )
    )
    with engine.connect() as conn:
        return _mapping(conn.execute(stmt).mappings().first())


def save_stage_input(
    project_code: str,
    department: str,
    *,
    estimate_hours: float | None,
    remaining_override_hours: float | None,
    handover_start: date | None,
    notes: str | None,
    user: str,
    expected_version: int | None,
    engine: Engine | None = None,
) -> dict[str, Any]:
    engine = engine or get_engine()
    department = _validate_department(department)
    init_planner_store(engine)
    now = _utcnow()

    with engine.begin() as conn:
        existing = conn.execute(
            select(planner_stage_inputs).where(
                and_(
                    planner_stage_inputs.c.project_code == project_code,
                    planner_stage_inputs.c.department == department,
                )
            )
        ).mappings().first()

        values = {
            "estimate_hours": estimate_hours,
            "remaining_override_hours": remaining_override_hours,
            "handover_start": handover_start,
            "notes": notes or None,
            "updated_by": user,
            "updated_at": now,
        }

        if existing is None:
            if expected_version not in (None, 0):
                raise StageInputConflict("Manager input no longer matches the loaded version.")
            try:
                with conn.begin_nested():
                    conn.execute(
                        insert(planner_stage_inputs).values(
                            project_code=project_code,
                            department=department,
                            version=1,
                            **values,
                        )
                    )
            except IntegrityError as exc:
                current = conn.execute(
                    select(planner_stage_inputs).where(
                        and_(
                            planner_stage_inputs.c.project_code == project_code,
                            planner_stage_inputs.c.department == department,
                        )
                    )
                ).mappings().first()
                raise StageInputConflict(
                    "Another user created this manager-input row first.", _mapping(current)
                ) from exc
        else:
            if expected_version is None or int(expected_version) != int(existing["version"]):
                raise StageInputConflict(
                    "This manager-input row changed after you loaded it.", dict(existing)
                )
            result = conn.execute(
                update(planner_stage_inputs)
                .where(
                    and_(
                        planner_stage_inputs.c.project_code == project_code,
                        planner_stage_inputs.c.department == department,
                        planner_stage_inputs.c.version == int(expected_version),
                    )
                )
                .values(version=int(expected_version) + 1, **values)
            )
            if result.rowcount != 1:
                current = conn.execute(
                    select(planner_stage_inputs).where(
                        and_(
                            planner_stage_inputs.c.project_code == project_code,
                            planner_stage_inputs.c.department == department,
                        )
                    )
                ).mappings().first()
                raise StageInputConflict(
                    "Another user saved this manager-input row first.", _mapping(current)
                )

        saved = conn.execute(
            select(planner_stage_inputs).where(
                and_(
                    planner_stage_inputs.c.project_code == project_code,
                    planner_stage_inputs.c.department == department,
                )
            )
        ).mappings().one()
        return dict(saved)


def get_weekly_allocation(
    project_code: str,
    department: str,
    person_name: str,
    week_start: date,
    *,
    engine: Engine | None = None,
) -> dict[str, Any] | None:
    engine = engine or get_engine()
    department = _validate_department(department)
    init_planner_store(engine)
    stmt = select(planner_weekly_allocations).where(
        and_(
            planner_weekly_allocations.c.project_code == project_code,
            planner_weekly_allocations.c.department == department,
            planner_weekly_allocations.c.person_name == person_name,
            planner_weekly_allocations.c.week_start == week_start,
        )
    )
    with engine.connect() as conn:
        return _mapping(conn.execute(stmt).mappings().first())


def save_weekly_allocation(
    project_code: str,
    department: str,
    person_name: str,
    week_start: date,
    hours: float,
    *,
    user: str,
    expected_version: int | None,
    engine: Engine | None = None,
) -> dict[str, Any]:
    """Save exactly one allocation cell with optimistic locking."""
    engine = engine or get_engine()
    department = _validate_department(department)
    init_planner_store(engine)
    hours = round(float(hours or 0), 2)
    if hours < 0:
        raise ValueError("Allocation hours cannot be negative.")
    now = _utcnow()

    key = and_(
        planner_weekly_allocations.c.project_code == project_code,
        planner_weekly_allocations.c.department == department,
        planner_weekly_allocations.c.person_name == person_name,
        planner_weekly_allocations.c.week_start == week_start,
    )

    with engine.begin() as conn:
        existing = conn.execute(select(planner_weekly_allocations).where(key)).mappings().first()

        if existing is None:
            if expected_version not in (None, 0):
                raise AllocationConflict("Allocation no longer matches the loaded version.")
            try:
                with conn.begin_nested():
                    conn.execute(
                        insert(planner_weekly_allocations).values(
                            project_code=project_code,
                            department=department,
                            person_name=person_name,
                            week_start=week_start,
                            hours=hours,
                            version=1,
                            updated_by=user,
                            updated_at=now,
                        )
                    )
            except IntegrityError as exc:
                current = conn.execute(
                    select(planner_weekly_allocations).where(key)
                ).mappings().first()
                raise AllocationConflict(
                    "Another user created this allocation first.", _mapping(current)
                ) from exc
        else:
            if expected_version is None or int(expected_version) != int(existing["version"]):
                raise AllocationConflict(
                    "This allocation changed after you loaded it.", dict(existing)
                )
            result = conn.execute(
                update(planner_weekly_allocations)
                .where(and_(key, planner_weekly_allocations.c.version == int(expected_version)))
                .values(
                    hours=hours,
                    version=int(expected_version) + 1,
                    updated_by=user,
                    updated_at=now,
                )
            )
            if result.rowcount != 1:
                current = conn.execute(
                    select(planner_weekly_allocations).where(key)
                ).mappings().first()
                raise AllocationConflict(
                    "Another user saved this allocation first.", _mapping(current)
                )

        saved = conn.execute(select(planner_weekly_allocations).where(key)).mappings().one()
        return dict(saved)


def save_weekly_allocations_batch(
    changes: Iterable[dict[str, Any]],
    *,
    user: str,
    engine: Engine | None = None,
) -> list[dict[str, Any]]:
    """Atomically save changed allocation cells using per-cell optimistic versions."""
    engine = engine or get_engine()
    init_planner_store(engine)
    now = _utcnow()
    saved_rows: list[dict[str, Any]] = []

    with engine.begin() as conn:
        for change in changes:
            project_code = str(change.get("project_code") or "").strip()
            person_name = str(change.get("person_name") or "").strip()
            department = _validate_department(change.get("department"))
            week_start = change.get("week_start")
            hours = round(float(change.get("hours") or 0), 2)
            expected_version = change.get("expected_version")

            if not project_code or not person_name or not isinstance(week_start, date):
                raise ValueError("Allocation change is missing project, person or week.")
            if hours < 0:
                raise ValueError("Allocation hours cannot be negative.")

            key = and_(
                planner_weekly_allocations.c.project_code == project_code,
                planner_weekly_allocations.c.department == department,
                planner_weekly_allocations.c.person_name == person_name,
                planner_weekly_allocations.c.week_start == week_start,
            )
            existing = conn.execute(
                select(planner_weekly_allocations).where(key)
            ).mappings().first()

            if existing is None:
                if expected_version not in (None, 0):
                    raise AllocationConflict(
                        f"{person_name} / {week_start.isoformat()} no longer matches the loaded version."
                    )
                try:
                    with conn.begin_nested():
                        conn.execute(
                            insert(planner_weekly_allocations).values(
                                project_code=project_code,
                                department=department,
                                person_name=person_name,
                                week_start=week_start,
                                hours=hours,
                                version=1,
                                updated_by=user,
                                updated_at=now,
                            )
                        )
                except IntegrityError as exc:
                    current = conn.execute(
                        select(planner_weekly_allocations).where(key)
                    ).mappings().first()
                    raise AllocationConflict(
                        f"{person_name} / {week_start.isoformat()} was created by another user.",
                        _mapping(current),
                    ) from exc
            else:
                if expected_version is None or int(expected_version) != int(existing["version"]):
                    raise AllocationConflict(
                        f"{person_name} / {week_start.isoformat()} changed after you loaded it.",
                        dict(existing),
                    )
                result = conn.execute(
                    update(planner_weekly_allocations)
                    .where(
                        and_(
                            key,
                            planner_weekly_allocations.c.version == int(expected_version),
                        )
                    )
                    .values(
                        hours=hours,
                        version=int(expected_version) + 1,
                        updated_by=user,
                        updated_at=now,
                    )
                )
                if result.rowcount != 1:
                    current = conn.execute(
                        select(planner_weekly_allocations).where(key)
                    ).mappings().first()
                    raise AllocationConflict(
                        f"{person_name} / {week_start.isoformat()} was saved by another user first.",
                        _mapping(current),
                    )

            saved = conn.execute(
                select(planner_weekly_allocations).where(key)
            ).mappings().one()
            saved_rows.append(dict(saved))

    return saved_rows


def upsert_calendar_days(
    records: Iterable[dict[str, Any]],
    *,
    engine: Engine | None = None,
) -> dict[str, int]:
    engine = engine or get_engine()
    init_planner_store(engine)
    counts = {"inserted": 0, "updated": 0, "unchanged": 0}
    now = _utcnow()
    with engine.begin() as conn:
        for record in records:
            work_date = record.get("work_date")
            if not isinstance(work_date, date):
                continue
            key = planner_calendar.c.work_date == work_date
            values = {
                "effective_working_day": bool(record.get("effective_working_day")),
                "bank_holiday": bool(record.get("bank_holiday")),
                "holiday_name": str(record.get("holiday_name") or "").strip() or None,
                "source": str(record.get("source") or "").strip() or None,
                "updated_at": now,
            }
            existing = conn.execute(select(planner_calendar).where(key)).mappings().first()
            if existing is None:
                conn.execute(insert(planner_calendar).values(work_date=work_date, **values))
                counts["inserted"] += 1
                continue
            changed = any(
                existing.get(field) != values[field]
                for field in (
                    "effective_working_day", "bank_holiday", "holiday_name", "source"
                )
            )
            if not changed:
                counts["unchanged"] += 1
                continue
            conn.execute(update(planner_calendar).where(key).values(**values))
            counts["updated"] += 1
    return counts


def list_calendar_days(
    *,
    start_date: date | None = None,
    end_date: date | None = None,
    engine: Engine | None = None,
) -> list[dict[str, Any]]:
    engine = engine or get_engine()
    init_planner_store(engine)
    stmt = select(planner_calendar)
    conditions = []
    if start_date:
        conditions.append(planner_calendar.c.work_date >= start_date)
    if end_date:
        conditions.append(planner_calendar.c.work_date <= end_date)
    if conditions:
        stmt = stmt.where(and_(*conditions))
    stmt = stmt.order_by(planner_calendar.c.work_date)
    with engine.connect() as conn:
        return [dict(row) for row in conn.execute(stmt).mappings().all()]


def upsert_non_project_allocations(
    records: Iterable[dict[str, Any]],
    *,
    user: str = "Migration",
    engine: Engine | None = None,
) -> dict[str, int]:
    """Idempotent import/upsert for non-project workload such as FLOW or Training."""
    engine = engine or get_engine()
    init_planner_store(engine)
    counts = {"inserted": 0, "updated": 0, "unchanged": 0}
    now = _utcnow()
    with engine.begin() as conn:
        for record in records:
            activity = str(record.get("activity_name") or "").strip()
            person = str(record.get("person_name") or "").strip()
            week = record.get("week_start")
            if not activity or not person or not isinstance(week, date):
                continue
            department = _validate_department(record.get("department"))
            hours = round(max(float(record.get("hours") or 0), 0), 2)
            key = and_(
                planner_non_project_allocations.c.activity_name == activity,
                planner_non_project_allocations.c.department == department,
                planner_non_project_allocations.c.person_name == person,
                planner_non_project_allocations.c.week_start == week,
            )
            existing = conn.execute(
                select(planner_non_project_allocations).where(key)
            ).mappings().first()
            if existing is None:
                conn.execute(
                    insert(planner_non_project_allocations).values(
                        activity_name=activity,
                        department=department,
                        person_name=person,
                        week_start=week,
                        hours=hours,
                        version=1,
                        updated_by=user,
                        updated_at=now,
                    )
                )
                counts["inserted"] += 1
            elif abs(float(existing["hours"] or 0) - hours) <= 0.005:
                counts["unchanged"] += 1
            else:
                conn.execute(
                    update(planner_non_project_allocations)
                    .where(key)
                    .values(
                        hours=hours,
                        version=int(existing["version"]) + 1,
                        updated_by=user,
                        updated_at=now,
                    )
                )
                counts["updated"] += 1
    return counts


def list_non_project_allocations(
    *,
    department: str | None = None,
    start_week: date | None = None,
    end_week: date | None = None,
    engine: Engine | None = None,
) -> list[dict[str, Any]]:
    engine = engine or get_engine()
    init_planner_store(engine)
    stmt = select(planner_non_project_allocations)
    conditions = []
    if department:
        conditions.append(planner_non_project_allocations.c.department == _validate_department(department))
    if start_week:
        conditions.append(planner_non_project_allocations.c.week_start >= start_week)
    if end_week:
        conditions.append(planner_non_project_allocations.c.week_start <= end_week)
    if conditions:
        stmt = stmt.where(and_(*conditions))
    stmt = stmt.order_by(
        planner_non_project_allocations.c.week_start,
        planner_non_project_allocations.c.department,
        planner_non_project_allocations.c.person_name,
        planner_non_project_allocations.c.activity_name,
    )
    with engine.connect() as conn:
        return [dict(row) for row in conn.execute(stmt).mappings().all()]


def list_weekly_allocations(
    *,
    department: str | None = None,
    project_code: str | None = None,
    start_week: date | None = None,
    end_week: date | None = None,
    engine: Engine | None = None,
) -> list[dict[str, Any]]:
    engine = engine or get_engine()
    init_planner_store(engine)
    stmt = select(planner_weekly_allocations)
    conditions = []
    if department:
        conditions.append(planner_weekly_allocations.c.department == _validate_department(department))
    if project_code:
        conditions.append(planner_weekly_allocations.c.project_code == project_code)
    if start_week:
        conditions.append(planner_weekly_allocations.c.week_start >= start_week)
    if end_week:
        conditions.append(planner_weekly_allocations.c.week_start <= end_week)
    if conditions:
        stmt = stmt.where(and_(*conditions))
    stmt = stmt.order_by(
        planner_weekly_allocations.c.week_start,
        planner_weekly_allocations.c.department,
        planner_weekly_allocations.c.person_name,
        planner_weekly_allocations.c.project_code,
    )
    with engine.connect() as conn:
        return [dict(row) for row in conn.execute(stmt).mappings().all()]


def record_sync_status(
    source_name: str,
    *,
    rows_processed: int,
    source_max_timestamp: datetime | None = None,
    details: str | None = None,
    engine: Engine | None = None,
) -> dict[str, Any]:
    engine = engine or get_engine()
    init_planner_store(engine)
    name = str(source_name or "").strip()
    if not name:
        raise ValueError("source_name is required.")
    now = _utcnow()
    with engine.begin() as conn:
        existing = conn.execute(
            select(integration_sync_status).where(
                integration_sync_status.c.source_name == name
            )
        ).mappings().first()
        values = {
            "last_synced_at": now,
            "source_max_timestamp": source_max_timestamp,
            "rows_processed": max(int(rows_processed), 0),
            "details": details,
        }
        if existing is None:
            conn.execute(
                insert(integration_sync_status).values(source_name=name, **values)
            )
        else:
            conn.execute(
                update(integration_sync_status)
                .where(integration_sync_status.c.source_name == name)
                .values(**values)
            )
        return dict(
            conn.execute(
                select(integration_sync_status).where(
                    integration_sync_status.c.source_name == name
                )
            ).mappings().one()
        )


def get_sync_status(
    source_name: str, *, engine: Engine | None = None
) -> dict[str, Any] | None:
    engine = engine or get_engine()
    init_planner_store(engine)
    with engine.connect() as conn:
        row = conn.execute(
            select(integration_sync_status).where(
                integration_sync_status.c.source_name == source_name
            )
        ).mappings().first()
    return _mapping(row)


def heartbeat_session(
    session_id: str,
    *,
    user_email: str,
    display_name: str,
    role: str,
    current_view: str | None = None,
    department: str | None = None,
    project_code: str | None = None,
    editing_scope: str | None = None,
    engine: Engine | None = None,
) -> None:
    """Upsert one presence row. Presence never locks planning records."""
    engine = engine or get_engine()
    init_planner_store(engine)
    now = _utcnow()
    if department:
        department = _validate_department(department)

    with engine.begin() as conn:
        existing = conn.execute(
            select(active_sessions.c.session_id).where(active_sessions.c.session_id == session_id)
        ).first()
        values = {
            "user_email": user_email,
            "display_name": display_name,
            "role": role,
            "current_view": current_view,
            "department": department,
            "project_code": project_code,
            "editing_scope": editing_scope,
            "last_seen_at": now,
        }
        if existing:
            conn.execute(
                update(active_sessions)
                .where(active_sessions.c.session_id == session_id)
                .values(**values)
            )
        else:
            conn.execute(
                insert(active_sessions).values(session_id=session_id, created_at=now, **values)
            )


def list_active_sessions(
    *,
    max_age_seconds: int = 45,
    exclude_session_id: str | None = None,
    engine: Engine | None = None,
) -> list[dict[str, Any]]:
    engine = engine or get_engine()
    init_planner_store(engine)
    cutoff = _utcnow() - timedelta(seconds=max(1, int(max_age_seconds)))
    stmt = select(active_sessions).where(active_sessions.c.last_seen_at >= cutoff)
    if exclude_session_id:
        stmt = stmt.where(active_sessions.c.session_id != exclude_session_id)
    stmt = stmt.order_by(active_sessions.c.display_name, active_sessions.c.last_seen_at.desc())
    with engine.connect() as conn:
        return [dict(row) for row in conn.execute(stmt).mappings().all()]


def remove_session(session_id: str, *, engine: Engine | None = None) -> None:
    engine = engine or get_engine()
    init_planner_store(engine)
    with engine.begin() as conn:
        conn.execute(delete(active_sessions).where(active_sessions.c.session_id == session_id))


def purge_stale_sessions(
    *, older_than_seconds: int = 300, engine: Engine | None = None
) -> int:
    engine = engine or get_engine()
    init_planner_store(engine)
    cutoff = _utcnow() - timedelta(seconds=max(1, int(older_than_seconds)))
    with engine.begin() as conn:
        result = conn.execute(delete(active_sessions).where(active_sessions.c.last_seen_at < cutoff))
        return int(result.rowcount or 0)
