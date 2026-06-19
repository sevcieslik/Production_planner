from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path

import app.data.db as db
from app.services.mvp import (
    capacity_balance,
    import_default_projects,
    load_projects_csv,
    save_projects,
    save_resources,
    weekly_department_capacity,
    weekly_project_demand,
)
from app.services.planning import spread_hours, week_starts


class MvpWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = db.DB_PATH
        db.DB_PATH = Path(self.tmp.name) / 'test.sqlite'
        db.initialize_database(seed=False)

    def tearDown(self):
        db.DB_PATH = self.old
        self.tmp.cleanup()

    def test_project_import_from_sample_csv(self):
        df = load_projects_csv('sample-data/projects.csv')
        self.assertIn('project_code', df.columns)
        self.assertIn('rs_start_date', df.columns)
        self.assertGreater(len(df), 0)
        self.assertEqual(import_default_projects(), len(df))
        self.assertGreater(len(db.rows('SELECT * FROM mvp_projects')), 0)

    def test_editable_project_save_update(self):
        save_projects([{'project_code': 'P1', 'project_name': 'Project One', 'rs_hours': 10, 'gis_hours': 0, 'pls_hours': 0, 'start_date': '2026-01-05', 'end_date': '2026-01-18', 'loading_type': 'even', 'rs_start_date': '2026-01-05', 'gis_start_date': '2026-01-05', 'pls_start_date': '2026-01-05', 'status': 'active'}])
        save_projects([{'project_code': 'P1', 'project_name': 'Project One', 'rs_hours': 30, 'gis_hours': 0, 'pls_hours': 0, 'start_date': '2026-01-05', 'end_date': '2026-01-18', 'loading_type': 'even', 'rs_start_date': '2026-01-05', 'gis_start_date': '2026-01-05', 'pls_start_date': '2026-01-05', 'status': 'active'}])
        self.assertEqual(db.rows('SELECT rs_hours FROM mvp_projects WHERE project_code="P1"')[0]['rs_hours'], 30)

    def test_resource_capacity_calculation(self):
        save_resources([{'person_name': 'A', 'department': 'RS', 'weekly_hours': 40, 'holiday_booked_hours': 0, 'holiday_remaining_hours': 0, 'active_status': 'active'}])
        cap = weekly_department_capacity([date(2026, 1, 5)])
        self.assertEqual(float(cap[(cap.department == 'RS')].available_capacity.iloc[0]), 34.0)

    def test_suspended_resource_removed_from_capacity(self):
        save_resources([{'person_name': 'A', 'department': 'RS', 'weekly_hours': 40, 'holiday_booked_hours': 0, 'holiday_remaining_hours': 0, 'active_status': 'suspended', 'status_start_date': '2026-01-01', 'status_end_date': '2026-01-31'}])
        cap = weekly_department_capacity([date(2026, 1, 5)])
        self.assertEqual(float(cap[(cap.department == 'RS')].available_capacity.iloc[0]), 0.0)

    def test_department_change_by_date_range(self):
        save_resources([{'person_name': 'A', 'department': 'RS', 'weekly_hours': 40, 'holiday_booked_hours': 0, 'holiday_remaining_hours': 0, 'active_status': 'active'}])
        rid = db.rows('SELECT id FROM mvp_resources WHERE person_name="A"')[0]['id']
        db.execute('INSERT INTO resource_department_assignments(resource_id,department,start_date,end_date) VALUES (?,?,?,?)', (rid, 'GIS', '2026-01-05', '2026-01-11'))
        cap = weekly_department_capacity([date(2026, 1, 5)])
        self.assertEqual(float(cap[(cap.department == 'GIS')].available_capacity.iloc[0]), 34.0)
        self.assertEqual(float(cap[(cap.department == 'RS')].available_capacity.iloc[0]), 0.0)

    def test_holiday_reduction(self):
        save_resources([{'person_name': 'A', 'department': 'RS', 'weekly_hours': 40, 'holiday_booked_hours': 8, 'holiday_remaining_hours': 0, 'active_status': 'active'}])
        cap = weekly_department_capacity([date(2026, 1, 5)])
        self.assertEqual(float(cap[(cap.department == 'RS')].available_capacity.iloc[0]), 27.2)

    def test_even_front_and_back_loaded_spreads(self):
        weeks = week_starts(date(2026, 1, 5), date(2026, 1, 25))
        self.assertEqual(spread_hours(90, weeks, 'even'), [30, 30, 30])
        self.assertEqual(spread_hours(60, weeks, 'front_loaded'), [30, 20, 10])
        self.assertEqual(spread_hours(60, weeks, 'back_loaded'), [10, 20, 30])

    def test_allocations_summary_by_department_and_over_under_detection(self):
        save_resources([{'person_name': 'A', 'department': 'RS', 'weekly_hours': 40, 'holiday_booked_hours': 0, 'holiday_remaining_hours': 0, 'active_status': 'active'}])
        save_projects([{'project_code': 'P1', 'project_name': 'Project One', 'rs_hours': 68, 'gis_hours': 0, 'pls_hours': 0, 'start_date': '2026-01-05', 'end_date': '2026-01-11', 'loading_type': 'even', 'rs_start_date': '2026-01-05', 'gis_start_date': '2026-01-05', 'pls_start_date': '2026-01-05', 'status': 'active'}])
        demand = weekly_project_demand()
        self.assertEqual(float(demand[(demand.department == 'RS')].demand_hours.sum()), 68.0)
        bal = capacity_balance([date(2026, 1, 5)])
        rs = bal[bal.department == 'RS'].iloc[0]
        self.assertEqual(float(rs.available_capacity), 34.0)
        self.assertEqual(float(rs.allocated_demand), 68.0)
        self.assertEqual(float(rs.over_under_capacity), -34.0)
        self.assertEqual(rs.status, 'red')


if __name__ == '__main__':
    unittest.main()
