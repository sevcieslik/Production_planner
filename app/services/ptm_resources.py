from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Iterable

from sqlalchemy import and_, insert, select, update
from sqlalchemy.exc import IntegrityError

from app.data.planner_store import (
    DEPARTMENTS,
    get_engine,
    init_planner_store,
    planner_people,
    planner_team_assignments,
    planner_time_off,
)


class ResourceConflict(RuntimeError):
    def __init__(self, message: str, current: dict[str, Any] | None = None):
        super().__init__(message)
        self.current = current


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _department(value: str) -> str:
    department = str(value or "").upper().strip()
    if department not in DEPARTMENTS:
        raise ValueError(f"Unknown department: {value}")
    return department


def list_people(*, active_only: bool = False, engine=None) -> list[dict[str, Any]]:
    engine = engine or get_engine()
    init_planner_store(engine)
    stmt = select(planner_people)
    if active_only:
        stmt = stmt.where(planner_people.c.active.is_(True))
    stmt = stmt.order_by(planner_people.c.person_name)
    with engine.connect() as conn:
        return [dict(row) for row in conn.execute(stmt).mappings().all()]


def save_person(
    person_name: str,
    *,
    home_department: str,
    primary_role: str | None,
    secondary_role: str | None,
    standard_hours_day: float,
    active: bool,
    active_from: date | None,
    active_to: date | None,
    user: str,
    expected_version: int | None,
    engine=None,
) -> dict[str, Any]:
    """Create/update exactly one roster row. Absence from a UI filter never deletes it."""
    engine = engine or get_engine()
    init_planner_store(engine)
    name = str(person_name or "").strip()
    if not name:
        raise ValueError("Person name is required.")
    department = _department(home_department)
    hours = float(standard_hours_day)
    if hours <= 0 or hours > 24:
        raise ValueError("Standard hours/day must be greater than 0 and no more than 24.")
    if active_from and active_to and active_to < active_from:
        raise ValueError("Active To cannot be before Active From.")

    now = _utcnow()
    key = planner_people.c.person_name == name
    with engine.begin() as conn:
        existing = conn.execute(select(planner_people).where(key)).mappings().first()
        values = {
            "home_department": department,
            "primary_role": str(primary_role or "").strip() or None,
            "secondary_role": str(secondary_role or "").strip() or None,
            "standard_hours_day": hours,
            "active": bool(active),
            "active_from": active_from,
            "active_to": active_to,
            "updated_by": user,
            "updated_at": now,
        }

        if existing is None:
            if expected_version not in (None, 0):
                raise ResourceConflict("Roster row no longer matches the loaded version.")
            try:
                with conn.begin_nested():
                    conn.execute(
                        insert(planner_people).values(person_name=name, version=1, **values)
                    )
            except IntegrityError as exc:
                current = conn.execute(select(planner_people).where(key)).mappings().first()
                raise ResourceConflict("Another user created this person first.", dict(current) if current else None) from exc
        else:
            if expected_version is None or int(expected_version) != int(existing["version"]):
                raise ResourceConflict("This roster row changed after you loaded it.", dict(existing))
            result = conn.execute(
                update(planner_people)
                .where(and_(key, planner_people.c.version == int(expected_version)))
                .values(version=int(expected_version) + 1, **values)
            )
            if result.rowcount != 1:
                current = conn.execute(select(planner_people).where(key)).mappings().first()
                raise ResourceConflict("Another user saved this roster row first.", dict(current) if current else None)

        saved = conn.execute(select(planner_people).where(key)).mappings().one()
        return dict(saved)


def upsert_people(records: Iterable[dict[str, Any]], *, user: str = "Migration", engine=None) -> dict[str, int]:
    """Safe migration helper. Missing rows are never treated as deletions."""
    engine = engine or get_engine()
    init_planner_store(engine)
    counts = {"inserted": 0, "updated": 0, "unchanged": 0}
    for record in records:
        name = str(record.get("person_name") or record.get("name") or "").strip()
        if not name:
            continue
        current = next((row for row in list_people(engine=engine) if row["person_name"] == name), None)
        before = None if current is None else {
            key: current.get(key)
            for key in (
                "home_department", "primary_role", "secondary_role", "standard_hours_day",
                "active", "active_from", "active_to",
            )
        }
        target = {
            "home_department": _department(record.get("home_department")),
            "primary_role": str(record.get("primary_role") or "").strip() or None,
            "secondary_role": str(record.get("secondary_role") or "").strip() or None,
            "standard_hours_day": float(record.get("standard_hours_day") or 7.5),
            "active": bool(record.get("active", True)),
            "active_from": record.get("active_from"),
            "active_to": record.get("active_to"),
        }
        if current is not None and before == target:
            counts["unchanged"] += 1
            continue
        save_person(
            name,
            **target,
            user=user,
            expected_version=0 if current is None else int(current["version"]),
            engine=engine,
        )
        counts["inserted" if current is None else "updated"] += 1
    return counts


def list_assignments(*, engine=None) -> list[dict[str, Any]]:
    engine = engine or get_engine()
    init_planner_store(engine)
    stmt = select(planner_team_assignments).order_by(
        planner_team_assignments.c.from_date,
        planner_team_assignments.c.person_name,
        planner_team_assignments.c.department,
    )
    with engine.connect() as conn:
        return [dict(row) for row in conn.execute(stmt).mappings().all()]


def save_assignment(
    *,
    assignment_id: int | None,
    person_name: str,
    from_date: date,
    to_date: date | None,
    department: str,
    allocation_percent: float,
    assignment_type: str,
    notes: str | None,
    user: str,
    expected_version: int | None,
    engine=None,
) -> dict[str, Any]:
    engine = engine or get_engine()
    init_planner_store(engine)
    if not person_name:
        raise ValueError("Person is required.")
    if not from_date:
        raise ValueError("Assignment From date is required.")
    if to_date and to_date < from_date:
        raise ValueError("Assignment To cannot be before From.")
    department = _department(department)
    pct = float(allocation_percent)
    if pct <= 0 or pct > 1:
        raise ValueError("Allocation % must be greater than 0 and no more than 100%.")
    assignment_type = str(assignment_type or "TEMP_SUPPORT").strip().upper()
    now = _utcnow()

    with engine.begin() as conn:
        existing = None
        if assignment_id is not None:
            existing = conn.execute(
                select(planner_team_assignments).where(planner_team_assignments.c.id == assignment_id)
            ).mappings().first()
        values = {
            "person_name": person_name,
            "from_date": from_date,
            "to_date": to_date,
            "department": department,
            "allocation_percent": pct,
            "assignment_type": assignment_type,
            "notes": str(notes or "").strip() or None,
            "updated_by": user,
            "updated_at": now,
        }
        if existing is None:
            if assignment_id is not None or expected_version not in (None, 0):
                raise ResourceConflict("Assignment no longer exists or changed.")
            try:
                with conn.begin_nested():
                    result = conn.execute(
                        insert(planner_team_assignments).values(version=1, **values)
                    )
                    new_id = result.inserted_primary_key[0]
            except IntegrityError as exc:
                raise ResourceConflict("A matching assignment already exists.") from exc
        else:
            if expected_version is None or int(expected_version) != int(existing["version"]):
                raise ResourceConflict("This assignment changed after you loaded it.", dict(existing))
            result = conn.execute(
                update(planner_team_assignments)
                .where(
                    and_(
                        planner_team_assignments.c.id == assignment_id,
                        planner_team_assignments.c.version == int(expected_version),
                    )
                )
                .values(version=int(expected_version) + 1, **values)
            )
            if result.rowcount != 1:
                current = conn.execute(
                    select(planner_team_assignments).where(planner_team_assignments.c.id == assignment_id)
                ).mappings().first()
                raise ResourceConflict("Another user saved this assignment first.", dict(current) if current else None)
            new_id = assignment_id

        saved = conn.execute(
            select(planner_team_assignments).where(planner_team_assignments.c.id == new_id)
        ).mappings().one()
        return dict(saved)


def list_time_off(*, engine=None) -> list[dict[str, Any]]:
    engine = engine or get_engine()
    init_planner_store(engine)
    stmt = select(planner_time_off).order_by(
        planner_time_off.c.from_date,
        planner_time_off.c.person_name,
    )
    with engine.connect() as conn:
        return [dict(row) for row in conn.execute(stmt).mappings().all()]


def save_time_off(
    *,
    time_off_id: int | None,
    person_name: str,
    from_date: date,
    to_date: date | None,
    time_off_type: str,
    available_hours_day: float,
    notes: str | None,
    user: str,
    expected_version: int | None,
    engine=None,
) -> dict[str, Any]:
    engine = engine or get_engine()
    init_planner_store(engine)
    if not person_name:
        raise ValueError("Person is required.")
    if not from_date:
        raise ValueError("Time Off From date is required.")
    if to_date and to_date < from_date:
        raise ValueError("Time Off To cannot be before From.")
    available = float(available_hours_day)
    if available < 0 or available > 24:
        raise ValueError("Available h/day must be between 0 and 24.")
    now = _utcnow()

    with engine.begin() as conn:
        existing = None
        if time_off_id is not None:
            existing = conn.execute(
                select(planner_time_off).where(planner_time_off.c.id == time_off_id)
            ).mappings().first()
        values = {
            "person_name": person_name,
            "from_date": from_date,
            "to_date": to_date,
            "time_off_type": str(time_off_type or "HOLIDAY").strip().upper(),
            "available_hours_day": available,
            "notes": str(notes or "").strip() or None,
            "updated_by": user,
            "updated_at": now,
        }
        if existing is None:
            if time_off_id is not None or expected_version not in (None, 0):
                raise ResourceConflict("Time Off row no longer exists or changed.")
            result = conn.execute(insert(planner_time_off).values(version=1, **values))
            new_id = result.inserted_primary_key[0]
        else:
            if expected_version is None or int(expected_version) != int(existing["version"]):
                raise ResourceConflict("This Time Off row changed after you loaded it.", dict(existing))
            result = conn.execute(
                update(planner_time_off)
                .where(
                    and_(
                        planner_time_off.c.id == time_off_id,
                        planner_time_off.c.version == int(expected_version),
                    )
                )
                .values(version=int(expected_version) + 1, **values)
            )
            if result.rowcount != 1:
                current = conn.execute(
                    select(planner_time_off).where(planner_time_off.c.id == time_off_id)
                ).mappings().first()
                raise ResourceConflict("Another user saved this Time Off row first.", dict(current) if current else None)
            new_id = time_off_id

        saved = conn.execute(
            select(planner_time_off).where(planner_time_off.c.id == new_id)
        ).mappings().one()
        return dict(saved)
