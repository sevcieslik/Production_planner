# PTM multi-user integration architecture

## Goal

Move the proven PTM View workflow into the Streamlit Production Planner while keeping fast editing, conflict-safe multi-user operation, High Level Google Sheets as the project source, and Google Sheets as an export/reporting target.

The Planner is also the operational front-end for read-only delivery dashboards and Delivery Operations tools. It should replace the need to move between multiple spreadsheets for routine planning and operational analysis.

## Product areas

### Planning

Editable operational workflow:

- Projects / Work Queue
- RS Planner
- GIS Planner
- PLS Planner
- Teams Breakdown
- People / Roster
- Time Off
- Temporary team assignments
- Plan Exceptions
- Sequence / handover analysis

### High Level dashboards

The Planner should expose the useful High Level views as **read-only dashboards**. These are presentation/decision-support views, not a second planning input surface.

Current High Level workbook views include:

- Executive
- Project View
- Project Health
- Planning Status
- Plan Changes
- Capacity
- Delivery

The hidden High Level import/reference tabs remain integration tables and are not shown as normal user navigation.

Where practical, dashboard data should come from the Planner/PostgreSQL model plus the same source data used by High Level rather than embedding the Google Sheet itself. This keeps the UI responsive and avoids Google API latency on normal page loads.

### Delivery Operations

The Planner should also absorb the Delivery Operations spreadsheet functions.

Current visible Delivery Operations workbook views include:

- Flow
- Product Activity analysis
- Backlog Model
- Processing rates
- Plan Exceptions
- Capacity / Allocation
- Operational KPIs

The current workbook also contains backend/reference datasets for processing-rate benchmarks, processor detail, outliers, backlog history/project model, tracker current/movements/history, flow KPIs and timesheet detail. Those become database-backed services rather than user-facing spreadsheet tabs.

Planned operational tools include:

- Rates Calculator
- Rates Tracker
- Processor Rates
- Product / Activity Rates
- Processing benchmark management
- Processing outlier review
- Backlog analysis
- Flow monitoring
- Capacity / Allocation analysis
- Operational KPIs

The Product Cost / Rates Calculator should retain the established inputs and constraints: Product, distance, ROW/Circuit, setup time, complexity and hourly rate, with one Base km/hr source rather than a DoT/EPIC split.

## Source-of-truth boundaries

- **High Level Google Sheet**: project identity, project code, Active flag, PM, Priority, Production/Delivery deadlines, Bid RS/GIS/PLS hours, PM notes.
- **Production Planner database**: PTM estimates, manager overrides, handover dates, roster, time off, temporary assignments, weekly allocations, rates/benchmark configuration, presence, audit/version metadata.
- **DoT / operational source data**: actual hours, product/activity/container detail and other source-controlled production records.
- **Google Sheets export**: reporting/output only. Exported PTM/Teams Breakdown views are not a second write source.

## Production database

Production should use PostgreSQL. SQLite remains supported for local development and isolated tests.

Environment:

- `DATABASE_URL` — PostgreSQL connection URL in production.
- `DATABASE_PATH` — optional local SQLite path when DATABASE_URL is absent.

On Render, use the PostgreSQL **internal** connection URL as `DATABASE_URL`. Never commit database credentials.

## Concurrency model

Avoid sheet-style full-table writes. All manager edits are row-level mutations.

Core rules:

1. Weekly allocation natural key:
   `person_id + discipline_id + project_id/work_item_id + week_start`.
2. Editable records carry:
   `version`, `updated_at`, `updated_by`.
3. Update uses optimistic locking:
   `UPDATE ... WHERE id=? AND version=?`.
4. If zero rows are updated, return a conflict to the UI instead of silently overwriting another user's change.
5. UI shows:
   `Saving…`, `Saved`, or `Conflict — reload/compare`.
6. Do not lock an entire project merely because another user opened it.
7. High-frequency edits should use small row/cell mutations; expensive dashboards and exports must not run synchronously with every edit.

## Presence

Add an `active_sessions` table:

- session_id
- user_email
- display_name
- role
- current_view
- department
- project_id nullable
- editing_scope nullable
- last_seen_at
- created_at

Each active Streamlit session sends a lightweight heartbeat roughly every 10–15 seconds.

A user is shown as active while `last_seen_at` is recent (target: <= 30–45 s).

Example UI:

`Active now: ● Carlos — PLS / HONI   ● Dom — GIS   ● Sev — Projects`

When another user is editing the same project/department, show a non-blocking warning. Actual collisions are handled by optimistic locking.

## High Level Google Sheets connector

Configuration:

- `HIGH_LEVEL_SPREADSHEET_ID`
- `GOOGLE_SERVICE_ACCOUNT_JSON` (secret JSON string) or an equivalent secret-file mechanism

The High Level spreadsheet must be shared with the service-account email as Viewer.

Sync maps the `Projects` tab by Project Code and performs upsert only on source-controlled fields. It must never overwrite manager-owned PTM fields.

Current expected source columns include:

- Project Code
- Project
- Active
- Project Manager
- Priority
- Production Deadline
- Delivery Deadline
- Bid RS h
- Bid GIS h
- Bid PLS h
- Notes
- Last PM Update

Dashboard views sourced from High Level are read-only in Planner.

## Delivery Operations connector / migration

Current Delivery Operations Google Sheet is used as a reference implementation while functionality is migrated.

Initial migration should map the current workbook's visible operational views and backend datasets into PostgreSQL-backed services.

Important source domains include:

- DoT timesheet detail
- tracker current / movements / history
- flow KPIs
- processing activity scope
- processor detail
- processing benchmarks
- processing outliers
- processing rates data
- backlog rolling history
- backlog project model

Once a module is migrated and validated, the Planner database becomes the operational source for that module rather than writing business logic back into Google Sheet formulas.

## Google Sheets export

Configuration:

- `GSHEETS_EXPORT_SPREADSHEET_ID`

The export spreadsheet must be shared with the service-account email as Editor.

Initial export targets:

- Projects / Work Queue
- Teams Breakdown
- planning snapshot tables
- selected dashboard/reporting tables where spreadsheet distribution is still useful

Exports are replace/snapshot operations and are never read back as planning input.

## UX and performance principles

- Main planning edits should feel immediate.
- Save only changed records.
- Do not rebuild dashboards on normal allocation edits.
- Keep dashboard queries read-only and cacheable.
- Prefer background/explicit sync for Google Sheets reads and exports.
- Preserve a clear visual distinction between editable planning modules and read-only dashboards.
- Keep navigation shallow: the user should be able to move between planning, dashboards and rates/operations without opening separate workbooks.

## Delivery order

1. PostgreSQL compatibility and migrations.
2. Row-level allocation writes + version columns + optimistic locking.
3. Presence heartbeat and active-user indicator.
4. High Level Projects connector/upsert.
5. PTM-style Projects / Work Queue.
6. RS/GIS/PLS planner with fast save.
7. Teams Breakdown.
8. High Level read-only dashboard shell.
9. Google Sheets export.
10. People / Time Off / temporary assignments.
11. DoT actual-hours connector.
12. Delivery Operations data ingestion.
13. Flow / Backlog / Capacity / KPI dashboards.
14. Processing Rates / Processor Rates.
15. Rates Calculator and Rates Tracker.
16. Remaining Delivery Operations modules and spreadsheet parity validation.

## Deployment requirements

For the first PostgreSQL deployment on Render:

1. Create one Render PostgreSQL database.
2. Add its internal URL to the web service as `DATABASE_URL`.
3. Do not send the password or URL in chat or commit it to GitHub.
4. Keep existing `PLANNER_USERS_JSON`.
5. Later, create a Google Cloud service account, enable Google Sheets API, and add its JSON as a Render secret.
