# Production Capacity Planner

A local-first Streamlit MVP for production capacity allocation and monitoring. It replaces fragile spreadsheet editing with form-based workflows, a normalized SQLite data model, import/export screens, simple forecasting, alerts and an audit trail.

## What this MVP includes

- Dashboard for available, allocated, unallocated and overallocated hours.
- Team capacity view by RS, GIS and PLS.
- Project view with discipline budgets, OSR progress, actual hours, EAC forecast and deadline risk.
- People allocation calendar table with under/over allocation flags.
- Safe allocation editor with bulk date-range allocation, overwrite confirmation and equal 50/50 split support.
- CSV/XLSX import abstraction for projects, OSR progress and public holidays, with schemas declared for future people/planned-hours/leave imports.
- CSV export for PM reporting.
- Normalized SQLite schema covering the requested entities.
- Seed data for a usable demo on first run.
- Audit log for imports and allocation changes.

## Architecture

```text
streamlit_app.py              Streamlit UI and safe forms
app/data/schema.sql           Normalized relational schema
app/data/db.py                SQLite connection, seed data and audit helper
app/importers/csv_xlsx.py     CSV/XLSX import adapter abstraction
app/services/forecasting.py   EAC and deadline risk calculations
app/services/alerts.py        MVP alert engine
```

The application is local-first. By default it creates `data/production_planner.sqlite`. For a small team, this file can be stored in a synced Google Drive folder, but only one editor should write at a time until a server database or locking workflow is added.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
streamlit run streamlit_app.py
```

Open the local URL printed by Streamlit, usually <http://localhost:8501>.

## Data model

The initial schema is normalized around these tables:

- `people`, `teams`, `disciplines`, `user_roles`
- `projects`, `project_discipline_budgets`, `project_schedules`
- `availability_calendar`, `holiday_calendar`, `leave_records`
- `non_billable_categories`, `weekly_demand`, `daily_allocations`
- `actual_hours`, `osr_progress`, `forecasts`, `alerts`, `audit_log`

## Import formats

Use the Import / export screen for CSV or XLSX files. MVP import support is deliberately controlled rather than raw-table editing.

### Projects

Required columns:

```text
project_name, client, start_date, end_date, deadline, status, notes, source_reference_id
```

### OSR progress

Required columns:

```text
project_name, progress_date, percent_complete, actual_hours_to_date
```

### Public holidays

Required columns:

```text
holiday_date, name, hours_removed
```

Additional schemas for people, planned hours and annual leave are declared in the adapter so they can be implemented without changing the UI pattern.

## Forecasting

For each project, the MVP calculates:

- planned hours from project discipline budgets
- actual hours to date from latest OSR progress
- estimated hours at completion: `actual_hours_to_date / (percent_complete / 100)`
- variance: `EAC - planned_hours`
- deadline risk by comparing remaining forecast hours with future allocated capacity before the deadline

## Alerts

The dashboard generates MVP alerts for:

- planned project demand with no allocation
- available person time with no allocation
- person overallocated
- insufficient future allocation before a deadline
- actual hours exceeding planned hours
- forecast EAC exceeding planned hours

## Roles and permissions

Authentication is intentionally lightweight for MVP. Use the sidebar local user selector:

- CDO and Manager users can import and edit allocations.
- PM Viewer can view and export only.

The schema includes `user_roles` so proper authentication and role-based access control can be added later.

## Roadmap for Google Drive / Google Sheets integration

1. Add a `GoogleSheetsImporter` implementing the same adapter interface as `csv_xlsx.py`.
2. Use a Google Cloud service account and store credentials outside the repository.
3. Add source configuration tables for sheet ID, tab name, range, import type and refresh cadence.
4. Add import staging tables with validation and reconciliation before committing to production tables.
5. Add stale-source alerts when OSR/progress imports are older than an agreed threshold.
6. Add row-level conflict detection for manual edits made after the last import.
7. Move from shared SQLite on Google Drive to Postgres or a single hosted app if concurrent editing becomes necessary.

## Notes on the current workbook

The workbook was not available inside this Codex workspace, so the MVP uses the workflow and naming patterns described in the request rather than recreating workbook tabs directly.

## Planning workflow improvements

### Planning mode

Managers can choose a project, discipline, date range and total planned demand hours, then spread those hours by week using:

- even spread
- front-loaded spread
- back-loaded spread
- manual weekly spread

Saved demand is written to `weekly_demand` and audited.

### Allocation mode

Managers can filter available people by RS/GIS/PLS, view open project demand, and assign a person to a project across a date range. Allocation supports full-day assignment and 50/50 split sizing. Existing allocations are skipped unless overwrite is explicitly confirmed.

### Gap analysis

Gap analysis compares project demand to assigned capacity by week, highlights unfilled demand, lists people with available time but no work, and shows overallocated people.

### Manager dashboard

The Manager dashboard provides one page per discipline for RS, GIS and PLS. It summarises this week, next week and the next four weeks with available, allocated, unallocated and overallocated hours, plus projects at forecast/deadline risk.

### Workbook structure support

The requested `/sample-data/2PROD Capacity plan 2026.xlsx` workbook was not present in this Codex workspace. To support that structure when available, the Import / export screen includes a workbook analyser that profiles each sheet, detects likely workflow roles such as roster, demand, allocation, OSR, leave or holidays, and provides a mapping summary for controlled imports rather than recreating fragile spreadsheet formulas.
