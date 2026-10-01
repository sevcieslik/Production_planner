from __future__ import annotations

from app.integrations.dot_times import aggregate_dot_actuals


def test_dot_actuals_aggregate_by_project_and_department_and_filter_scope():
    rows = [
        {
            "timestamp": "2026-09-25 10:00:00+00:00",
            "department": "GIS",
            "project_code": "NM1",
            "hours": 4,
        },
        {
            "timestamp": "2026-09-26 10:00:00+00:00",
            "department": "GIS",
            "project_code": "NM1",
            "hours": 2.5,
        },
        {
            "timestamp": "2026-09-27 10:00:00+00:00",
            "department": "RS",
            "project_code": "NM1",
            "hours": 3,
        },
        {
            "timestamp": "2026-09-28 10:00:00+00:00",
            "department": "GIS",
            "project_code": "OTHER",
            "hours": 99,
        },
        {
            "timestamp": "2026-09-29 10:00:00+00:00",
            "department": "G&A",
            "project_code": "NM1",
            "hours": 7,
        },
    ]

    records, latest, processed = aggregate_dot_actuals(
        rows, project_codes={"NM1"}
    )
    assert records == [
        {
            "project_code": "NM1",
            "department": "GIS",
            "actual_hours": 6.5,
            "source_name": "DoT times/master",
        },
        {
            "project_code": "NM1",
            "department": "RS",
            "actual_hours": 3.0,
            "source_name": "DoT times/master",
        },
    ]
    assert processed == 3
    assert latest.isoformat() == "2026-09-27T10:00:00+00:00"
