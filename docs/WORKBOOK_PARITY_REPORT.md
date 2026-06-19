# Workbook parity report

Audit date: 2026-06-19.

## Scope

Workbook parity now treats `Projects`, `Roster Daily`, and `Allocation Daily` as source sheets. `Teams Breakdown` and `Capacity` are not imported as data sources; they are validation/reconciliation targets.

## Imported sheets

| Workbook sheet | Import role | App destination |
|---|---|---|
| `Projects` | Source | `workbook_staging_projects`, then `projects`, `project_discipline_budgets`, and project-backed `work_items` |
| `Roster Daily` | Source | `workbook_staging_roster`, then `people`, `teams`, `disciplines`, and `availability_calendar` |
| `Allocation Daily` | Source | `workbook_staging_allocations`, then `daily_allocations` linked to `work_items` |
| `Teams Breakdown` | Validation target | Compared against generated weekly project/discipline outputs |
| `Capacity` | Validation target | Compared against generated project-level capacity outputs |

## Mapping rules

- Diminished Capacity is a configurable business rule stored in `capacity_settings`. The seeded global default is `0.85`.
- Effective planning capacity is `Available Hours × Diminished Capacity Factor`.
- Capacity settings are scoped for future overrides by `global`, `discipline`, `team`, and `person`.
- Allocation labels are normalized as Work Items. Project names become `Project` Work Items; known non-project labels map to `QA`, `FLOW`, `Training`, `Admin`, `Leave`, `Unavailable`, or `Other`.
- Workbook date columns are normalized into row-based `work_date`, `allocation_date`, and `week_start` values.
- `Teams Breakdown` is generated from normalized allocations, people disciplines, project budgets, availability, and Diminished Capacity.
- `Capacity` is generated from project budgets, normalized allocations, progress values, and Diminished Capacity.

## Validation workflow

Workbook Upload → Staging Tables → Validation → Preview Changes → Commit.

Validation detects:

- unknown/missing people names;
- unknown or unmapped allocation labels;
- invalid or unmapped dates;
- duplicate allocations for the same person/date;
- overallocated people;
- missing discipline mapping from workbook team labels.

## Trial import summary for `sample-data/2PROD Capacity plan 2026.xlsx`

A read-only staging run against the workbook produced:

| Metric | Count |
|---|---:|
| Staged projects | 165 |
| Staged roster day records | 26,748 |
| Staged allocation records | 14,651 |
| Validation issues | 42 |

A trial commit from staging produced:

| Metric | Count |
|---|---:|
| Projects committed | 165 |
| People committed | 47 |
| Availability records committed | 26,735 |
| Allocation records committed | 14,633 |

## Reconciliation status

The app now has services that generate internal `Teams Breakdown` and `Capacity` style outputs and compare them with cached workbook target values where available. Reconciliation rows are classified as `match`, `mismatch`, `missing_generated`, or `missing_in_workbook`, with variance percentages for comparable hour totals. Because workbook calculated sheets include external and broken-reference formula dependencies, any large variance should be reviewed as either an import/mapping issue or an unstable workbook formula source.

Target thresholds remain:

- allocated-hour calculations: less than 2% difference;
- project-level totals: less than 1% difference.

## Variance analysis

Known expected variance drivers:

1. The workbook has embedded external `IMPORTRANGE`/OSR formulas and some broken `#REF!` references.
2. `Teams Breakdown` applies Diminished Capacity (`0.85`) to weekly assignment calculations; the app now uses a configurable setting rather than a hardcoded spreadsheet formula.
3. Workbook allocation cells sometimes contain non-project labels that must be mapped to Work Item types.
4. Some labels are imported as `Other` until a manager supplies an explicit mapping.
5. Duplicate or conflicting person/date assignments are validation issues and may be skipped or require resolution before commit.

## Next parity step

Persist reconciliation rows by workbook batch so historical parity runs can be compared over time, then add manager-owned mappings for labels currently classified as `Other`.
