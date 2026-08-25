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

### Resource management and administration

Managers and administrators can maintain the roster, approved absence, and temporary
capacity adjustments in Resource Management. Temporary assignments move available
capacity between departments for their date range without changing the employee's
home roster record; unavailable adjustments reduce operational capacity. Technical
setup transfer, imports, internal-activity administration, and the audit viewer remain
in the administrator-only Administration area.

Planning includes an advisory Sequence view for material RS → GIS and GIS → PLS
gaps, possible overlaps, downstream starvation, and pull-forward opportunities based
on explicit manager allocations. It does not move allocations automatically.

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

The local database is `data/production_planner.sqlite` unless `DATABASE_PATH` overrides it.

## Render pilot deployment

This pilot supports multiple browser users through **one** Streamlit service and one SQLite database. Keep the Render service at exactly one instance; do not enable horizontal scaling or multiple worker processes for this SQLite deployment.

### Environment variables

```text
DATABASE_PATH=/var/data/planner.db
PLANNER_USERS_JSON=<JSON>
```

`PLANNER_USERS_JSON` is required. Missing or malformed configuration fails closed. The following is a fake example only (replace both placeholders with generated hashes):

```json
{
  "admin@example.com": {
    "name": "Planner Admin",
    "password_hash": "<generated hash>",
    "role": "admin"
  },
  "manager@example.com": {
    "name": "Planning Manager",
    "password_hash": "<generated hash>",
    "role": "manager",
    "active": true
  }
}
```

Roles are `admin` and `manager`. Both can use Projects, Planning, and operational
Resource Management; only admins receive the technical Administration tab. Set
optional `active` to `false` to deny login.

### Persistent Disk

Create a Render Persistent Disk mounted at exactly:

```text
/var/data
```

### Build Command

```text
pip install -r requirements.txt
```

### Start Command

```text
streamlit run streamlit_app.py --server.address 0.0.0.0 --server.port $PORT --server.headless true
```

### Creating users

1. Run `python scripts/generate_password_hash.py` locally and enter/confirm the password at the secure prompts.
2. Copy the single salted PBKDF2-SHA256 hash printed by the script.
3. Add or update the user's entry in the `PLANNER_USERS_JSON` Render secret. Do not commit the JSON or plaintext password.
4. Redeploy or restart the Render service so new sessions load the updated environment.

Successful login/logout and all existing writes use the authenticated display name and email in the audit log. This deliberately simple pilot has environment-managed users rather than self-service password reset, MFA, SSO, centralized session revocation, or brute-force rate limiting; expose it only to a controlled internal audience and use Render HTTPS.

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
