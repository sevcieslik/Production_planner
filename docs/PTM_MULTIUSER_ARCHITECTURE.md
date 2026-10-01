# PTM multi-user integration architecture

## Goal

Move the proven PTM View workflow into the Streamlit Production Planner while keeping fast editing, conflict-safe multi-user operation, High Level Google Sheets as the project source, and Google Sheets as an export/reporting target.

## Source-of-truth boundaries

- **High Level Google Sheet**: project identity, project code, Active flag, PM, Priority, Production/Delivery deadlines, Bid RS/GIS/PLS hours, PM notes.
- **Production Planner database**: PTM estimates, manager overrides, handover dates, roster, time off, temporary assignments, weekly allocations, presence, audit/version metadata.
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

## Google Sheets export

Configuration:

- `GSHEETS_EXPORT_SPREADSHEET_ID`

The export spreadsheet must be shared with the service-account email as Editor.

Initial export targets:

- Projects / Work Queue
- Teams Breakdown
- optional planning snapshot tables

Exports are replace/snapshot operations and are never read back as planning input.

## Delivery order

1. PostgreSQL compatibility and migrations.
2. Row-level allocation writes + version columns + optimistic locking.
3. Presence heartbeat and active-user indicator.
4. High Level Projects connector/upsert.
5. PTM-style Projects / Work Queue.
6. RS/GIS/PLS planner with fast save.
7. Teams Breakdown.
8. Google Sheets export.
9. People / Time Off / temporary assignments.
10. DoT actual-hours connector.

## Deployment requirements

For the first PostgreSQL deployment on Render:

1. Create one Render PostgreSQL database.
2. Add its internal URL to the web service as `DATABASE_URL`.
3. Do not send the password or URL in chat or commit it to GitHub.
4. Keep existing `PLANNER_USERS_JSON`.
5. Later, create a Google Cloud service account, enable Google Sheets API, and add its JSON as a Render secret.
