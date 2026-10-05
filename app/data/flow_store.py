from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Iterable

from sqlalchemy import Boolean, Column, DateTime, Float, Integer, PrimaryKeyConstraint, String, Table, Text, and_, delete, insert, select, update
from sqlalchemy.engine import Engine

from app.data.planner_store import get_engine, metadata

flow_dashboard_snapshots = Table(
    "flow_dashboard_snapshots",
    metadata,
    Column("snapshot_at", DateTime(timezone=True), primary_key=True),
    Column("payload_json", Text, nullable=False),
    Column("source_label", String(128), nullable=False, default="legacy_import"),
    Column("imported_at", DateTime(timezone=True), nullable=False),
)

flow_history_rows = Table(
    "flow_history_rows",
    metadata,
    Column("snapshot_at", DateTime(timezone=True), nullable=False),
    Column("project_code", String(64), nullable=False),
    Column("project_name", String(255), nullable=False),
    Column("metrics_json", Text, nullable=False),
    Column("imported_at", DateTime(timezone=True), nullable=False),
    PrimaryKeyConstraint("snapshot_at", "project_code"),
)

flow_movement_rows = Table(
    "flow_movement_rows",
    metadata,
    Column("from_snapshot", DateTime(timezone=True), nullable=False),
    Column("to_snapshot", DateTime(timezone=True), nullable=False),
    Column("project_code", String(64), nullable=False),
    Column("project_name", String(255), nullable=False),
    Column("portfolio_status", String(64)),
    Column("metrics_json", Text, nullable=False),
    Column("imported_at", DateTime(timezone=True), nullable=False),
    PrimaryKeyConstraint("from_snapshot", "to_snapshot", "project_code"),
)

flow_backlog_rows = Table(
    "flow_backlog_rows",
    metadata,
    Column("as_of", DateTime(timezone=True), nullable=False),
    Column("team", String(64), nullable=False),
    Column("metrics_json", Text, nullable=False),
    Column("imported_at", DateTime(timezone=True), nullable=False),
    PrimaryKeyConstraint("as_of", "team"),
)

flow_bucket_config = Table(
    "flow_bucket_config",
    metadata,
    Column("section", String(16), nullable=False),
    Column("source_key", String(128), nullable=False),
    Column("label", String(128), nullable=False),
    Column("department", String(16)),
    Column("sort_order", Integer, nullable=False),
    Column("active", Boolean, nullable=False, default=True),
    Column("product_keys", Text),
    Column("statuses", Text),
    Column("status_groups", Text),
    Column("version", Integer, nullable=False, default=1),
    Column("updated_by", String(255), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    PrimaryKeyConstraint("section", "source_key"),
)

DEFAULT_FLOW_CONFIG = [
    # Current-state layout mirrors the semantic layout proven in Delivery Operations.
    {"section": "CURRENT", "source_key": "tpc_outstanding_km", "label": "TPC Outstanding", "department": "RS", "sort_order": 10},
    {"section": "CURRENT", "source_key": "tpc_wip_km", "label": "TPC WIP", "department": "RS", "sort_order": 20},
    {"section": "CURRENT", "source_key": "gis_subcon_wip_km", "label": "With Subcon", "department": "GIS", "sort_order": 30},
    {"section": "CURRENT", "source_key": "gis_ready_km", "label": "GIS Ready", "department": "GIS", "sort_order": 40},
    {"section": "CURRENT", "source_key": "gis_production_wip_km", "label": "GIS Production", "department": "GIS", "sort_order": 50},
    {"section": "CURRENT", "source_key": "gis_ready_for_qc_km", "label": "Ready for QC", "department": "GIS", "sort_order": 60},
    {"section": "CURRENT", "source_key": "gis_qc_wip_km", "label": "QC WIP", "department": "GIS", "sort_order": 70},
    {"section": "CURRENT", "source_key": "pls_ready_km", "label": "PLS Ready", "department": "PLS", "sort_order": 80},
    {"section": "CURRENT", "source_key": "pls_wip_km", "label": "PLS WIP", "department": "PLS", "sort_order": 90},
    # Movement layout mirrors the Delivery Operations terminology.
    {"section": "MOVEMENT", "source_key": "scope_added_km", "label": "Scope Added", "department": None, "sort_order": 10},
    {"section": "MOVEMENT", "source_key": "tpc_started_km", "label": "TPC Started", "department": "RS", "sort_order": 20},
    {"section": "MOVEMENT", "source_key": "tpc_released_km", "label": "TPC Released", "department": "RS", "sort_order": 30},
    {"section": "MOVEMENT", "source_key": "gis_started_km", "label": "GIS Started", "department": "GIS", "sort_order": 40},
    {"section": "MOVEMENT", "source_key": "sent_to_subcon_km", "label": "Sent to Subcon", "department": "GIS", "sort_order": 50},
    {"section": "MOVEMENT", "source_key": "gis_processed_km", "label": "GIS Processed", "department": "GIS", "sort_order": 60},
    {"section": "MOVEMENT", "source_key": "qc_started_km", "label": "QC Started", "department": "GIS", "sort_order": 70},
    {"section": "MOVEMENT", "source_key": "gis_completed_km", "label": "GIS Completed", "department": "GIS", "sort_order": 80},
    {"section": "MOVEMENT", "source_key": "pls_started_km", "label": "PLS Started", "department": "PLS", "sort_order": 90},
    {"section": "MOVEMENT", "source_key": "pls_completed_km", "label": "PLS Completed", "department": "PLS", "sort_order": 100},
]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _json(value: Any) -> str:
    return json.dumps(value, default=str, separators=(",", ":"))


def _loads(value: str | None) -> Any:
    return json.loads(value or "{}")


def init_flow_store(engine: Engine | None = None) -> None:
    metadata.create_all(engine or get_engine())


def ensure_default_flow_config(*, user: str = "system", engine: Engine | None = None) -> None:
    engine = engine or get_engine()
    init_flow_store(engine)
    now = _utcnow()
    with engine.begin() as conn:
        for item in DEFAULT_FLOW_CONFIG:
            key = and_(
                flow_bucket_config.c.section == item["section"],
                flow_bucket_config.c.source_key == item["source_key"],
            )
            if conn.execute(select(flow_bucket_config.c.source_key).where(key)).first():
                continue
            conn.execute(
                insert(flow_bucket_config).values(
                    **item,
                    active=True,
                    product_keys=None,
                    statuses=None,
                    status_groups=None,
                    version=1,
                    updated_by=user,
                    updated_at=now,
                )
            )


def list_flow_config(*, engine: Engine | None = None) -> list[dict[str, Any]]:
    engine = engine or get_engine()
    init_flow_store(engine)
    ensure_default_flow_config(engine=engine)
    stmt = select(flow_bucket_config).order_by(
        flow_bucket_config.c.section, flow_bucket_config.c.sort_order
    )
    with engine.connect() as conn:
        return [dict(row) for row in conn.execute(stmt).mappings().all()]


def save_flow_config(
    rows: Iterable[dict[str, Any]],
    *,
    user: str,
    engine: Engine | None = None,
) -> int:
    engine = engine or get_engine()
    init_flow_store(engine)
    now = _utcnow()
    changed = 0
    with engine.begin() as conn:
        for raw in rows:
            section = str(raw.get("section") or "").upper().strip()
            source_key = str(raw.get("source_key") or "").strip()
            if section not in {"CURRENT", "MOVEMENT"} or not source_key:
                continue
            key = and_(
                flow_bucket_config.c.section == section,
                flow_bucket_config.c.source_key == source_key,
            )
            existing = conn.execute(select(flow_bucket_config).where(key)).mappings().first()
            values = {
                "label": str(raw.get("label") or source_key).strip(),
                "department": (str(raw.get("department") or "").upper().strip() or None),
                "sort_order": int(raw.get("sort_order") or 0),
                "active": bool(raw.get("active", True)),
                "product_keys": str(raw.get("product_keys") or "").strip() or None,
                "statuses": str(raw.get("statuses") or "").strip() or None,
                "status_groups": str(raw.get("status_groups") or "").strip() or None,
                "updated_by": user,
                "updated_at": now,
            }
            if existing is None:
                conn.execute(
                    insert(flow_bucket_config).values(
                        section=section,
                        source_key=source_key,
                        version=1,
                        **values,
                    )
                )
            else:
                conn.execute(
                    update(flow_bucket_config)
                    .where(key)
                    .values(version=int(existing["version"] or 0) + 1, **values)
                )
            changed += 1
    return changed


def import_legacy_flow(
    parsed: dict[str, Any],
    *,
    user: str,
    engine: Engine | None = None,
) -> dict[str, int]:
    """Idempotently import historical Flow records from a legacy workbook."""
    engine = engine or get_engine()
    init_flow_store(engine)
    ensure_default_flow_config(user=user, engine=engine)
    now = _utcnow()
    counts = {"dashboard": 0, "history": 0, "movements": 0, "backlog": 0}

    with engine.begin() as conn:
        dashboard = parsed.get("dashboard")
        snapshot_at = parsed.get("dashboard_snapshot_at")
        if dashboard and snapshot_at:
            key = flow_dashboard_snapshots.c.snapshot_at == snapshot_at
            values = {
                "payload_json": _json(dashboard),
                "source_label": "legacy_delivery_operations_import",
                "imported_at": now,
            }
            existing = conn.execute(select(flow_dashboard_snapshots.c.snapshot_at).where(key)).first()
            if existing:
                conn.execute(update(flow_dashboard_snapshots).where(key).values(**values))
            else:
                conn.execute(insert(flow_dashboard_snapshots).values(snapshot_at=snapshot_at, **values))
            counts["dashboard"] += 1

        for row in parsed.get("history", []):
            key = and_(
                flow_history_rows.c.snapshot_at == row["snapshot_at"],
                flow_history_rows.c.project_code == row["project_code"],
            )
            values = {
                "project_name": row["project_name"],
                "metrics_json": _json(row["metrics"]),
                "imported_at": now,
            }
            if conn.execute(select(flow_history_rows.c.project_code).where(key)).first():
                conn.execute(update(flow_history_rows).where(key).values(**values))
            else:
                conn.execute(
                    insert(flow_history_rows).values(
                        snapshot_at=row["snapshot_at"],
                        project_code=row["project_code"],
                        **values,
                    )
                )
            counts["history"] += 1

        for row in parsed.get("movements", []):
            key = and_(
                flow_movement_rows.c.from_snapshot == row["from_snapshot"],
                flow_movement_rows.c.to_snapshot == row["to_snapshot"],
                flow_movement_rows.c.project_code == row["project_code"],
            )
            values = {
                "project_name": row["project_name"],
                "portfolio_status": row.get("portfolio_status"),
                "metrics_json": _json(row["metrics"]),
                "imported_at": now,
            }
            if conn.execute(select(flow_movement_rows.c.project_code).where(key)).first():
                conn.execute(update(flow_movement_rows).where(key).values(**values))
            else:
                conn.execute(
                    insert(flow_movement_rows).values(
                        from_snapshot=row["from_snapshot"],
                        to_snapshot=row["to_snapshot"],
                        project_code=row["project_code"],
                        **values,
                    )
                )
            counts["movements"] += 1

        for row in parsed.get("backlog", []):
            key = and_(
                flow_backlog_rows.c.as_of == row["as_of"],
                flow_backlog_rows.c.team == row["team"],
            )
            values = {"metrics_json": _json(row["metrics"]), "imported_at": now}
            if conn.execute(select(flow_backlog_rows.c.team).where(key)).first():
                conn.execute(update(flow_backlog_rows).where(key).values(**values))
            else:
                conn.execute(
                    insert(flow_backlog_rows).values(
                        as_of=row["as_of"], team=row["team"], **values
                    )
                )
            counts["backlog"] += 1

    return counts


def latest_flow_snapshot_at(*, engine: Engine | None = None) -> datetime | None:
    engine = engine or get_engine()
    init_flow_store(engine)
    with engine.connect() as conn:
        rows = conn.execute(select(flow_history_rows.c.snapshot_at)).all()
    return max((row[0] for row in rows), default=None)


def flow_history_for_snapshot(
    snapshot_at: datetime,
    *,
    engine: Engine | None = None,
) -> list[dict[str, Any]]:
    engine = engine or get_engine()
    init_flow_store(engine)
    stmt = select(flow_history_rows).where(flow_history_rows.c.snapshot_at == snapshot_at)
    with engine.connect() as conn:
        rows = conn.execute(stmt).mappings().all()
    return [
        {
            "snapshot_at": row["snapshot_at"],
            "project_code": row["project_code"],
            "project_name": row["project_name"],
            "metrics": _loads(row["metrics_json"]),
        }
        for row in rows
    ]


def flow_movements_between(
    from_at: datetime,
    to_at: datetime,
    *,
    engine: Engine | None = None,
) -> list[dict[str, Any]]:
    engine = engine or get_engine()
    init_flow_store(engine)
    stmt = select(flow_movement_rows).where(
        and_(
            flow_movement_rows.c.to_snapshot > from_at,
            flow_movement_rows.c.to_snapshot <= to_at,
        )
    )
    with engine.connect() as conn:
        rows = conn.execute(stmt).mappings().all()
    return [
        {
            "from_snapshot": row["from_snapshot"],
            "to_snapshot": row["to_snapshot"],
            "project_code": row["project_code"],
            "project_name": row["project_name"],
            "portfolio_status": row["portfolio_status"],
            "metrics": _loads(row["metrics_json"]),
        }
        for row in rows
    ]


def latest_backlog_rows(*, engine: Engine | None = None) -> tuple[datetime | None, list[dict[str, Any]]]:
    engine = engine or get_engine()
    init_flow_store(engine)
    with engine.connect() as conn:
        timestamps = [row[0] for row in conn.execute(select(flow_backlog_rows.c.as_of)).all()]
        latest = max(timestamps, default=None)
        if latest is None:
            return None, []
        rows = conn.execute(
            select(flow_backlog_rows).where(flow_backlog_rows.c.as_of == latest)
        ).mappings().all()
    return latest, [
        {"as_of": row["as_of"], "team": row["team"], "metrics": _loads(row["metrics_json"])}
        for row in rows
    ]


def flow_store_counts(*, engine: Engine | None = None) -> dict[str, int]:
    engine = engine or get_engine()
    init_flow_store(engine)
    with engine.connect() as conn:
        return {
            "history": len(conn.execute(select(flow_history_rows.c.project_code)).all()),
            "movements": len(conn.execute(select(flow_movement_rows.c.project_code)).all()),
            "backlog": len(conn.execute(select(flow_backlog_rows.c.team)).all()),
        }
