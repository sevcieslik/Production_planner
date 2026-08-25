# Production Capacity Planner

A local-first Streamlit application that turns high-level project demand into an honest, manager-owned capacity plan. The production UI deliberately starts with no demonstration records and does not manufacture missing dates or project hours.

## Workflow

### 1. Project demand

PM/CDO users enter the planning facts on one page:

- project identity, client, PM, priority and late-delivery penalty exposure;
- SPUs, ROW length and circuit length;
- required completion date;
- forecast hours and data-available date for RS, GIS and PLS;
- material assumptions or evidence.

The page validates required facts and shows forecast hours less source-controlled actual hours as remaining demand. It creates a neutral, even baseline for manager review; this is not treated as a committed plan.

### 2. Capacity plan

Processing managers select RS, GIS or PLS and see:

- remaining demand, actual hours and explicit unplanned hours by project;
- real roster capacity less recorded holidays;
- a weekly editable smoothing grid;
- weekly over/under capacity;
- structured escalations for unresolved shortages, delays, estimate uncertainty, priority conflicts, skills gaps or dependencies.

A plan cannot allocate more than the project's remaining forecast. A planning review with unplanned hours cannot be completed unless an open escalation exists for the department.

### Administration

Roster maintenance is hidden in the sidebar Administration panel. It is separated from operational planning so PM and processing-manager workflows remain focused.

## Honest-data rules

- The app calls `initialize_database(seed=False)` and exposes no sample-data import buttons.
- Missing project dates are validation errors, not silently substituted dates.
- Capacity uses recorded weekly hours and holidays; the old unexplained 0.85 diminished-capacity factor is not applied.
- Actual hours are displayed as source-controlled values and are not editable in the project-demand form.
- Projects are archived by status rather than deleted from the primary workflow.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
streamlit run streamlit_app.py
```

The local database is `data/production_planner.sqlite`. SQLite is suitable for a single writer; use a server database before enabling concurrent editing.

## Key files

```text
streamlit_app.py              Two-page operational UI and hidden administration panel
app/services/mvp.py           Demand validation, capacity, manager plans and escalations
app/data/schema.sql           Existing normalized integration/import schema
app/data/db.py                SQLite connection and audit helpers
tests/test_mvp.py             Two-page workflow and capacity tests
```

## Current boundary

This release establishes the operating loop: **PM facts → baseline demand → manager smoothing → completed plan or escalation**. Automatic source-system actual-hour ingestion, dependency propagation, skills-aware cross-deployment and scenario optimization remain future integrations. Until actuals are integrated, the source-controlled actual fields remain zero rather than being populated with invented values.
