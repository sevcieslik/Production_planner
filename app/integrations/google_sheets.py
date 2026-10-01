from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta
from typing import Any, Iterable

import gspread
from google.oauth2.service_account import Credentials

SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets"
DRIVE_SCOPE = "https://www.googleapis.com/auth/drive.readonly"

HIGH_LEVEL_SHEET_NAME = "Projects"


class GoogleSheetsConfigurationError(RuntimeError):
    """Raised when required Google Sheets credentials/configuration are missing."""


def _service_account_info(raw: str | None = None) -> dict[str, Any]:
    value = raw if raw is not None else os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not value:
        raise GoogleSheetsConfigurationError("GOOGLE_SERVICE_ACCOUNT_JSON is not configured.")
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise GoogleSheetsConfigurationError("GOOGLE_SERVICE_ACCOUNT_JSON is not valid JSON.") from exc
    if not isinstance(parsed, dict) or not parsed.get("client_email") or not parsed.get("private_key"):
        raise GoogleSheetsConfigurationError("Google service-account JSON is incomplete.")
    return parsed


def google_client(raw_credentials: str | None = None) -> gspread.Client:
    info = _service_account_info(raw_credentials)
    credentials = Credentials.from_service_account_info(
        info,
        scopes=[SHEETS_SCOPE, DRIVE_SCOPE],
    )
    return gspread.authorize(credentials)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return _text(value).lower() in {"true", "1", "yes", "y", "active"}


def _float_or_none(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    cleaned = _text(value).replace(",", "")
    if not cleaned:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def date_or_none(value: Any) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, (int, float)):
        # Google Sheets / Excel serial date system.
        return date(1899, 12, 30) + timedelta(days=int(value))
    text = _text(value)
    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d/%m/%y", "%m/%d/%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    return None


def normalise_high_level_projects(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Map the High Level Projects tab to Planner source-controlled fields."""
    output: list[dict[str, Any]] = []
    for row in rows:
        code = _text(row.get("Project Code"))
        name = _text(row.get("Project"))
        if not code or not name:
            continue
        output.append(
            {
                "project_code": code,
                "project_name": name,
                "active": _bool(row.get("Active")),
                "project_manager": _text(row.get("Project Manager")) or None,
                "priority": _text(row.get("Priority")) or None,
                "production_deadline": date_or_none(row.get("Production Deadline")),
                "delivery_deadline": date_or_none(row.get("Delivery Deadline")),
                "bid_rs_h": _float_or_none(row.get("Bid RS h")),
                "bid_gis_h": _float_or_none(row.get("Bid GIS h")),
                "bid_pls_h": _float_or_none(row.get("Bid PLS h")),
                "notes": _text(row.get("Notes")) or None,
                "last_pm_update": date_or_none(row.get("Last PM Update")),
            }
        )
    return output


def read_high_level_projects(
    *,
    spreadsheet_id: str | None = None,
    client: gspread.Client | None = None,
) -> list[dict[str, Any]]:
    spreadsheet_id = spreadsheet_id or os.getenv("HIGH_LEVEL_SPREADSHEET_ID")
    if not spreadsheet_id:
        raise GoogleSheetsConfigurationError("HIGH_LEVEL_SPREADSHEET_ID is not configured.")
    client = client or google_client()
    worksheet = client.open_by_key(spreadsheet_id).worksheet(HIGH_LEVEL_SHEET_NAME)
    rows = worksheet.get_all_records(default_blank="")
    return normalise_high_level_projects(rows)


def replace_sheet_rows(
    records: Iterable[dict[str, Any]],
    *,
    spreadsheet_id: str | None = None,
    sheet_name: str,
    client: gspread.Client | None = None,
) -> int:
    """Replace one export worksheet with a header + records snapshot."""
    spreadsheet_id = spreadsheet_id or os.getenv("GSHEETS_EXPORT_SPREADSHEET_ID")
    if not spreadsheet_id:
        raise GoogleSheetsConfigurationError("GSHEETS_EXPORT_SPREADSHEET_ID is not configured.")
    client = client or google_client()
    spreadsheet = client.open_by_key(spreadsheet_id)
    try:
        worksheet = spreadsheet.worksheet(sheet_name)
    except gspread.WorksheetNotFound:
        worksheet = spreadsheet.add_worksheet(title=sheet_name, rows=1000, cols=40)

    rows = list(records)
    headers: list[str] = []
    for record in rows:
        for key in record:
            if key not in headers:
                headers.append(key)

    values = [headers]
    for record in rows:
        values.append([record.get(key, "") for key in headers])

    worksheet.clear()
    if values and headers:
        worksheet.update(values, "A1", value_input_option="USER_ENTERED")
    return len(rows)
