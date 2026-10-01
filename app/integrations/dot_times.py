from __future__ import annotations

import os
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Iterable

import gspread

from app.integrations.google_sheets import GoogleSheetsConfigurationError, google_client

DOT_MASTER_SHEET = "master"
VALID_DEPARTMENTS = {"RS", "GIS", "PLS"}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _number(value: Any) -> float:
    if value in (None, ""):
        return 0.0
    try:
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return 0.0


def _timestamp(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        text = _text(value)
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def aggregate_dot_actuals(
    rows: Iterable[dict[str, Any]],
    *,
    project_codes: set[str] | None = None,
) -> tuple[list[dict[str, Any]], datetime | None, int]:
    """
    Aggregate DoT master rows to project + department actual hours.

    The master source remains authoritative; this function does not attempt to
    infer missing departments or project codes.
    """
    totals: dict[tuple[str, str], float] = defaultdict(float)
    max_timestamp: datetime | None = None
    processed = 0

    for row in rows:
        department = _text(row.get("department")).upper()
        code = _text(row.get("project_code"))
        if department not in VALID_DEPARTMENTS or not code:
            continue
        if project_codes is not None and code not in project_codes:
            continue
        hours = _number(row.get("hours"))
        if hours < 0:
            # Preserve the source total if corrections are represented as negative time.
            pass
        totals[(code, department)] += hours
        processed += 1

        timestamp = _timestamp(row.get("timestamp")) or _timestamp(row.get("start_timestamp"))
        if timestamp is not None and (max_timestamp is None or timestamp > max_timestamp):
            max_timestamp = timestamp

    records = [
        {
            "project_code": code,
            "department": department,
            "actual_hours": round(hours, 2),
            "source_name": "DoT times/master",
        }
        for (code, department), hours in sorted(totals.items())
    ]
    return records, max_timestamp, processed


def read_dot_actuals(
    *,
    spreadsheet_id: str | None = None,
    project_codes: set[str] | None = None,
    client: gspread.Client | None = None,
) -> tuple[list[dict[str, Any]], datetime | None, int]:
    spreadsheet_id = spreadsheet_id or os.getenv("DOT_TIMES_SPREADSHEET_ID")
    if not spreadsheet_id:
        raise GoogleSheetsConfigurationError("DOT_TIMES_SPREADSHEET_ID is not configured.")
    client = client or google_client()
    worksheet = client.open_by_key(spreadsheet_id).worksheet(DOT_MASTER_SHEET)
    rows = worksheet.get_all_records(default_blank="")
    return aggregate_dot_actuals(rows, project_codes=project_codes)
