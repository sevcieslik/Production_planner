from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path

import pandas as pd

import app.data.db as db
from app.services.mvp import (
    PROJECT_DATE_COLUMNS,
    RESOURCE_DATE_COLUMNS,
    capacity_balance,
    import_approved_holidays,
    import_default_projects,
    import_sample_roster,
    get_data_version,
    load_projects_csv,
    normalise_date_for_db,
    prepare_date_columns_for_editor,
    save_projects,
    save_resources,
    recalculate_holiday_totals,
    summary_rows_from_capacity_balance,
    weekly_department_capacity,
    weekly_project_demand,
)
from app.services.planning import spread_hours, week_starts


class MvpWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = db.DB_PATH
        db.DB_PATH = Path(self.tmp.name) / "test.sqlite"
        db.initialize_database(seed=False)

    def tearDown(self):
        db.DB_PATH = self.old
        self.tmp.cleanup()


    def test_capacity_balance_returns_rows_with_no_project_demand(self):
        save_resources([{"person_name": "A", "department": "RS", "weekly_hours": 40, "active_status": "active"}])
        weeks = [date(2026, 1, 5), date(2026, 1, 12)]
        bal = capacity_balance(weeks)
        self.assertEqual(len(bal), len(weeks) * 3)
        self.assertTrue((bal["allocated_demand"] == 0).all())
        rs = bal[(bal.department == "RS") & (bal.week_start == "2026-01-05")].iloc[0]
        self.assertEqual(float(rs.over_under_capacity), float(rs.available_capacity))

    def test_capacity_balance_returns_all_departments_for_each_week(self):
        weeks = [date(2026, 1, 5), date(2026, 1, 12)]
        bal = capacity_balance(weeks)
        for week in weeks:
            departments = set(bal[bal.week_start == week.isoformat()].department)
            self.assertEqual(departments, {"RS", "GIS", "PLS"})

    def test_project_save_increments_data_version(self):
        before = get_data_version()
        save_projects([{"project_code": "V1", "project_name": "Version", "start_date": "2026-01-05", "end_date": "2026-01-11", "loading_type": "even", "status": "active"}])
        self.assertGreater(get_data_version(), before)

    def test_resource_save_increments_data_version(self):
        before = get_data_version()
        save_resources([{"person_name": "Version Person", "department": "GIS", "weekly_hours": 37.5, "active_status": "active"}])
        self.assertGreater(get_data_version(), before)

    def test_holiday_import_increments_data_version(self):
        save_resources([{"person_name": "A", "department": "RS", "weekly_hours": 40, "active_status": "active"}])
        before = get_data_version()
        path = Path(self.tmp.name) / "holidays.csv"
        path.write_text("Employee,Date From,Date To,Days of Absence\nA,05/01/2026,05/01/2026,1\n")
        import_approved_holidays(path)
        self.assertGreater(get_data_version(), before)

    def test_summary_rows_generated_from_empty_demand_capacity_balance(self):
        weeks = [date(2026, 1, 5)]
        bal = capacity_balance(weeks)
        summary = summary_rows_from_capacity_balance(bal, [w.isoformat() for w in weeks])
        self.assertEqual(
            summary["Summary"].tolist(),
            [
                "RS available capacity",
                "RS allocated demand",
                "RS over/under capacity",
                "GIS available capacity",
                "GIS allocated demand",
                "GIS over/under capacity",
                "PLS available capacity",
                "PLS allocated demand",
                "PLS over/under capacity",
            ],
        )

    def test_prepare_project_dates_for_editor_are_datetime_compatible(self):
        frame = pd.DataFrame(
            [
                {
                    "start_date": "2026-05-01",
                    "end_date": "01-May-26",
                    "rs_start_date": "",
                    "gis_start_date": None,
                    "pls_start_date": "1 May 2026",
                }
            ]
        )
        prepared = prepare_date_columns_for_editor(frame, PROJECT_DATE_COLUMNS)
        self.assertTrue(pd.api.types.is_datetime64_any_dtype(prepared["start_date"]))
        self.assertTrue(pd.api.types.is_datetime64_any_dtype(prepared["end_date"]))
        self.assertTrue(pd.isna(prepared.loc[0, "rs_start_date"]))
        self.assertTrue(pd.isna(prepared.loc[0, "gis_start_date"]))

    def test_resource_status_dates_for_editor_are_datetime_compatible(self):
        frame = pd.DataFrame(
            [{"status_start_date": "2026-01-05", "status_end_date": ""}]
        )
        prepared = prepare_date_columns_for_editor(frame, RESOURCE_DATE_COLUMNS)
        self.assertTrue(
            pd.api.types.is_datetime64_any_dtype(prepared["status_start_date"])
        )
        self.assertTrue(pd.isna(prepared.loc[0, "status_end_date"]))

    def test_normalise_date_for_db_handles_supported_inputs_and_blanks(self):
        self.assertEqual(normalise_date_for_db("01-May-26"), "2026-05-01")
        self.assertEqual(normalise_date_for_db("1 May 2026"), "2026-05-01")
        self.assertEqual(normalise_date_for_db("2026-05-01"), "2026-05-01")
        self.assertIsNone(normalise_date_for_db(""))

    def test_saved_dates_use_iso_and_reload_as_editor_compatible(self):
        save_projects(
            [
                {
                    "project_code": "D1",
                    "project_name": "Dates",
                    "rs_hours": 1,
                    "gis_hours": 0,
                    "pls_hours": 0,
                    "start_date": pd.Timestamp("2026-05-01"),
                    "end_date": "1 May 2026",
                    "loading_type": "even",
                    "rs_start_date": "",
                    "gis_start_date": None,
                    "pls_start_date": "01-May-26",
                    "status": "active",
                }
            ]
        )
        saved = db.rows(
            """
            SELECT
                start_date,
                end_date,
                rs_start_date,
                gis_start_date,
                pls_start_date
            FROM mvp_projects
            WHERE project_code="D1"
            """
        )[0]
        self.assertEqual(saved["start_date"], "2026-05-01")
        self.assertEqual(saved["end_date"], "2026-05-01")
        self.assertEqual(saved["pls_start_date"], "2026-05-01")

        reloaded = prepare_date_columns_for_editor(
            pd.DataFrame([saved]), PROJECT_DATE_COLUMNS
        )
        self.assertTrue(pd.api.types.is_datetime64_any_dtype(reloaded["start_date"]))

    def test_project_import_from_sample_csv(self):
        df = load_projects_csv("sample-data/projects.csv")
        self.assertIn("project_code", df.columns)
        self.assertIn("rs_start_date", df.columns)
        self.assertGreater(len(df), 0)
        self.assertEqual(import_default_projects(), len(df))
        self.assertGreater(len(db.rows("SELECT * FROM mvp_projects")), 0)

    def test_editable_project_save_update(self):
        save_projects(
            [
                {
                    "project_code": "P1",
                    "project_name": "Project One",
                    "rs_hours": 10,
                    "gis_hours": 0,
                    "pls_hours": 0,
                    "start_date": "2026-01-05",
                    "end_date": "2026-01-18",
                    "loading_type": "even",
                    "rs_start_date": "2026-01-05",
                    "gis_start_date": "2026-01-05",
                    "pls_start_date": "2026-01-05",
                    "status": "active",
                }
            ]
        )
        save_projects(
            [
                {
                    "project_code": "P1",
                    "project_name": "Project One",
                    "rs_hours": 30,
                    "gis_hours": 0,
                    "pls_hours": 0,
                    "start_date": "2026-01-05",
                    "end_date": "2026-01-18",
                    "loading_type": "even",
                    "rs_start_date": "2026-01-05",
                    "gis_start_date": "2026-01-05",
                    "pls_start_date": "2026-01-05",
                    "status": "active",
                }
            ]
        )
        self.assertEqual(
            db.rows('SELECT rs_hours FROM mvp_projects WHERE project_code="P1"')[0][
                "rs_hours"
            ],
            30,
        )

    def test_resource_capacity_calculation(self):
        save_resources(
            [
                {
                    "person_name": "A",
                    "department": "RS",
                    "weekly_hours": 40,
                    "holiday_booked_hours": 0,
                    "holiday_remaining_hours": 0,
                    "active_status": "active",
                }
            ]
        )
        cap = weekly_department_capacity([date(2026, 1, 5)])
        self.assertEqual(float(cap[(cap.department == "RS")].available_capacity.iloc[0]), 34.0)

    def test_suspended_resource_removed_from_capacity(self):
        save_resources(
            [
                {
                    "person_name": "A",
                    "department": "RS",
                    "weekly_hours": 40,
                    "holiday_booked_hours": 0,
                    "holiday_remaining_hours": 0,
                    "active_status": "suspended",
                    "status_start_date": "2026-01-01",
                    "status_end_date": "2026-01-31",
                }
            ]
        )
        cap = weekly_department_capacity([date(2026, 1, 5)])
        self.assertEqual(float(cap[(cap.department == "RS")].available_capacity.iloc[0]), 0.0)

    def test_department_change_by_date_range(self):
        save_resources(
            [
                {
                    "person_name": "A",
                    "department": "RS",
                    "weekly_hours": 40,
                    "holiday_booked_hours": 0,
                    "holiday_remaining_hours": 0,
                    "active_status": "active",
                }
            ]
        )
        rid = db.rows('SELECT id FROM mvp_resources WHERE person_name="A"')[0]["id"]
        db.execute(
            """
            INSERT INTO resource_department_assignments(
                resource_id,
                department,
                start_date,
                end_date
            )
            VALUES (?,?,?,?)
            """,
            (rid, "GIS", "2026-01-05", "2026-01-11"),
        )
        cap = weekly_department_capacity([date(2026, 1, 5)])
        self.assertEqual(float(cap[(cap.department == "GIS")].available_capacity.iloc[0]), 34.0)
        self.assertEqual(float(cap[(cap.department == "RS")].available_capacity.iloc[0]), 0.0)

    def test_holiday_booked_total_is_not_subtracted_each_week(self):
        save_resources(
            [
                {
                    "person_name": "A",
                    "department": "RS",
                    "weekly_hours": 40,
                    "holiday_booked_hours": 8,
                    "holiday_remaining_hours": 0,
                    "active_status": "active",
                }
            ]
        )
        cap = weekly_department_capacity([date(2026, 1, 5)])
        self.assertEqual(float(cap[(cap.department == "RS")].available_capacity.iloc[0]), 34.0)

    def test_holiday_records_reduce_weekly_capacity(self):
        save_resources([{"person_name": "A", "department": "RS", "weekly_hours": 40, "active_status": "active"}])
        rid = db.rows('SELECT id FROM mvp_resources WHERE person_name="A"')[0]["id"]
        db.execute('INSERT INTO holidays(resource_id,person_name,holiday_date,hours,source) VALUES (?,?,?,?,?)', (rid, "A", "2026-01-06", 8, "test"))
        cap = weekly_department_capacity([date(2026, 1, 5)])
        self.assertEqual(float(cap[(cap.department == "RS")].available_capacity.iloc[0]), 27.2)

    def test_roster_csv_import_creates_resources_and_daily_hours_converts(self):
        path = Path(self.tmp.name) / "roster.csv"
        path.write_text("Name,Team,Daily Hours\nDaily Person,GIS,7.5\n")
        result = import_sample_roster(path)
        self.assertEqual(result.imported_people_count, 1)
        saved = db.rows('SELECT department,weekly_hours,active_status FROM mvp_resources WHERE person_name="Daily Person"')[0]
        self.assertEqual(saved["department"], "GIS")
        self.assertEqual(saved["weekly_hours"], 37.5)
        self.assertEqual(saved["active_status"], "active")

    def test_approved_holidays_import_expands_ranges_and_reports_unmatched(self):
        save_resources([{"person_name": "A", "department": "RS", "weekly_hours": 40, "active_status": "active"}])
        path = Path(self.tmp.name) / "holidays.csv"
        path.write_text("Employee,Date From,Date To,Days of Absence\nA,05/01/2026,07/01/2026,3\nMissing,05/01/2026,05/01/2026,1\n")
        result = import_approved_holidays(path)
        self.assertEqual(result.imported_holiday_records_count, 3)
        self.assertIn("Missing", result.unmatched_holiday_names)
        self.assertEqual(len(db.rows('SELECT * FROM holidays WHERE person_name="A"')), 3)
        cap = weekly_department_capacity([date(2026, 1, 5)])
        self.assertEqual(float(cap[(cap.department == "RS")].available_capacity.iloc[0]), 13.6)

    def test_planning_weeks_generate_through_end_of_2026(self):
        weeks = week_starts(date(2026, 1, 1), date(2026, 12, 31))
        self.assertEqual(weeks[-1], date(2026, 12, 28))

    def test_even_front_and_back_loaded_spreads(self):
        weeks = week_starts(date(2026, 1, 5), date(2026, 1, 25))
        self.assertEqual(spread_hours(90, weeks, "even"), [30, 30, 30])
        self.assertEqual(spread_hours(60, weeks, "front_loaded"), [30, 20, 10])
        self.assertEqual(spread_hours(60, weeks, "back_loaded"), [10, 20, 30])

    def test_allocations_summary_by_department_and_over_under_detection(self):
        save_resources(
            [
                {
                    "person_name": "A",
                    "department": "RS",
                    "weekly_hours": 40,
                    "holiday_booked_hours": 0,
                    "holiday_remaining_hours": 0,
                    "active_status": "active",
                }
            ]
        )
        save_projects(
            [
                {
                    "project_code": "P1",
                    "project_name": "Project One",
                    "rs_hours": 68,
                    "gis_hours": 0,
                    "pls_hours": 0,
                    "start_date": "2026-01-05",
                    "end_date": "2026-01-11",
                    "loading_type": "even",
                    "rs_start_date": "2026-01-05",
                    "gis_start_date": "2026-01-05",
                    "pls_start_date": "2026-01-05",
                    "status": "active",
                }
            ]
        )
        demand = weekly_project_demand()
        self.assertEqual(float(demand[(demand.department == "RS")].demand_hours.sum()), 68.0)

        bal = capacity_balance([date(2026, 1, 5)])
        rs = bal[bal.department == "RS"].iloc[0]

        self.assertEqual(float(rs.available_capacity), 34.0)
        self.assertEqual(float(rs.allocated_demand), 68.0)
        self.assertEqual(float(rs.over_under_capacity), -34.0)
        self.assertEqual(rs.status, "red")


if __name__ == "__main__":
    unittest.main()