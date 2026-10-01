from __future__ import annotations

from typing import Any

from app.data.planner_store import (
    get_engine,
    get_stage_input,
    get_weekly_allocation,
    list_non_project_allocations,
    list_projects,
    save_stage_input,
    save_weekly_allocations_batch,
    upsert_actual_hours,
    upsert_calendar_days,
    upsert_non_project_allocations,
)
from app.integrations.ptm_legacy import read_legacy_ptm_snapshot
from app.services.ptm_resources import (
    list_assignments,
    list_people,
    list_time_off,
    save_assignment,
    save_time_off,
    upsert_people,
)


def migrate_ptm_snapshot(
    *,
    snapshot: dict[str, list[dict[str, Any]]] | None = None,
    user: str = "PTM migration",
    engine=None,
) -> dict[str, Any]:
    """Import current PTM state without overwriting records already edited in Planner."""
    engine = engine or get_engine()
    snapshot = snapshot or read_legacy_ptm_snapshot()

    project_codes = {row["project_code"] for row in list_projects(active_only=False, engine=engine)}
    existing_people = {row["person_name"] for row in list_people(engine=engine)}

    valid_people = []
    skipped_people = []
    for person in snapshot["people"]:
        if person["home_department"] not in {"RS", "GIS", "PLS"}:
            skipped_people.append(person["person_name"])
            continue
        valid_people.append(person)

    calendar_result = upsert_calendar_days(
        snapshot.get("calendar", []), engine=engine
    )
    people_result = upsert_people(valid_people, user=user, engine=engine)
    existing_people.update(row["person_name"] for row in valid_people)

    existing_assignments = {
        (
            row["person_name"],
            row["from_date"],
            row.get("to_date"),
            row["department"],
            row["assignment_type"],
        )
        for row in list_assignments(engine=engine)
    }
    assignments_inserted = 0
    assignments_skipped = 0
    for row in snapshot["assignments"]:
        key = (
            row["person_name"], row["from_date"], row.get("to_date"),
            row["department"], str(row["assignment_type"]).upper(),
        )
        if row["person_name"] not in existing_people or key in existing_assignments:
            assignments_skipped += 1
            continue
        save_assignment(
            assignment_id=None,
            person_name=row["person_name"],
            from_date=row["from_date"],
            to_date=row.get("to_date"),
            department=row["department"],
            allocation_percent=row["allocation_percent"],
            assignment_type=row["assignment_type"],
            notes=row.get("notes"),
            user=user,
            expected_version=0,
            engine=engine,
        )
        existing_assignments.add(key)
        assignments_inserted += 1

    existing_time_off = {
        (
            row["person_name"], row["from_date"], row.get("to_date"),
            row["time_off_type"], float(row["available_hours_day"] or 0),
        )
        for row in list_time_off(engine=engine)
    }
    time_off_inserted = 0
    time_off_skipped = 0
    for row in snapshot["time_off"]:
        key = (
            row["person_name"], row["from_date"], row.get("to_date"),
            str(row["time_off_type"]).upper(), float(row["available_hours_day"] or 0),
        )
        if row["person_name"] not in existing_people or key in existing_time_off:
            time_off_skipped += 1
            continue
        save_time_off(
            time_off_id=None,
            person_name=row["person_name"],
            from_date=row["from_date"],
            to_date=row.get("to_date"),
            time_off_type=row["time_off_type"],
            available_hours_day=row["available_hours_day"],
            notes=row.get("notes"),
            user=user,
            expected_version=0,
            engine=engine,
        )
        existing_time_off.add(key)
        time_off_inserted += 1

    stage_created = 0
    stage_preserved = 0
    actual_records = []
    for row in snapshot["demand"]:
        if row["project_code"] not in project_codes:
            continue
        actual_records.append(
            {
                "project_code": row["project_code"],
                "department": row["department"],
                "actual_hours": row["actual_hours"],
                "source_name": "Legacy PTM / DoT",
            }
        )
        if (
            row.get("estimate_hours") is None
            and row.get("remaining_override_hours") is None
            and row.get("handover_start") is None
            and not row.get("notes")
        ):
            continue
        current = get_stage_input(
            row["project_code"], row["department"], engine=engine
        )
        if current is not None:
            stage_preserved += 1
            continue
        save_stage_input(
            row["project_code"],
            row["department"],
            estimate_hours=row.get("estimate_hours"),
            remaining_override_hours=row.get("remaining_override_hours"),
            handover_start=row.get("handover_start"),
            notes=row.get("notes"),
            user=user,
            expected_version=0,
            engine=engine,
        )
        stage_created += 1
    actual_result = upsert_actual_hours(actual_records, engine=engine)

    project_changes = []
    project_skipped = 0
    non_project_candidates = []
    for row in snapshot["allocations"]:
        if row["person_name"] not in existing_people:
            project_skipped += 1
            continue
        if row.get("project_code"):
            if row["project_code"] not in project_codes:
                project_skipped += 1
                continue
            current = get_weekly_allocation(
                row["project_code"],
                row["department"],
                row["person_name"],
                row["week_start"],
                engine=engine,
            )
            if current is not None:
                project_skipped += 1
                continue
            project_changes.append(
                {
                    "project_code": row["project_code"],
                    "department": row["department"],
                    "person_name": row["person_name"],
                    "week_start": row["week_start"],
                    "hours": row["hours"],
                    "expected_version": 0,
                }
            )
        else:
            non_project_candidates.append(row)

    if project_changes:
        save_weekly_allocations_batch(project_changes, user=user, engine=engine)

    existing_non_project = {
        (
            row["activity_name"], row["department"], row["person_name"], row["week_start"]
        )
        for row in list_non_project_allocations(engine=engine)
    }
    new_non_project = [
        {
            "activity_name": row["activity_name"],
            "department": row["department"],
            "person_name": row["person_name"],
            "week_start": row["week_start"],
            "hours": row["hours"],
        }
        for row in non_project_candidates
        if (
            row["activity_name"], row["department"], row["person_name"], row["week_start"]
        ) not in existing_non_project
    ]
    non_project_result = upsert_non_project_allocations(
        new_non_project, user=user, engine=engine
    )

    return {
        "calendar": calendar_result,
        "people": people_result,
        "skipped_people": skipped_people,
        "assignments_inserted": assignments_inserted,
        "assignments_skipped": assignments_skipped,
        "time_off_inserted": time_off_inserted,
        "time_off_skipped": time_off_skipped,
        "stage_inputs_created": stage_created,
        "stage_inputs_preserved": stage_preserved,
        "actuals": actual_result,
        "project_allocations_inserted": len(project_changes),
        "project_allocations_skipped": project_skipped,
        "non_project_allocations": non_project_result,
    }
