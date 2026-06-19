from __future__ import annotations

import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

import app.data.db as db
from app.services.planning import (
    build_loading_preview,
    effective_capacity_by_week,
    fit_hours_to_capacity,
    manual_loading_difference,
    overall_capacity_summary,
    spread_hours,
    week_starts,
)


class PlanningWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.original_db_path = db.DB_PATH
        db.DB_PATH = Path(self.tmp.name) / 'test.sqlite'
        db.initialize_database(seed=False)
        with db.connect() as conn:
            for code in ['RS', 'GIS', 'PLS']:
                conn.execute('INSERT INTO disciplines(code,name) VALUES (?,?)', (code, code))
            db.seed_capacity_rules(conn)
            self.rs_id = conn.execute('SELECT id FROM disciplines WHERE code="RS"').fetchone()['id']
            conn.execute('INSERT INTO people(name,discipline_id,daily_hours,weekly_hours,active) VALUES (?,?,?,?,1)', ('Planner', self.rs_id, 8, 40))
            self.person_id = conn.execute('SELECT id FROM people WHERE name="Planner"').fetchone()['id']
            self.project_id = conn.execute("INSERT INTO projects(project_name,client,start_date,end_date,deadline,status) VALUES ('P','C','2026-01-05','2026-01-25','2026-01-25','Active')").lastrowid
            for i in range(14):
                d = date(2026, 1, 5) + timedelta(days=i)
                if d.weekday() < 5:
                    conn.execute('INSERT INTO availability_calendar(person_id,work_date,available_hours) VALUES (?,?,?)', (self.person_id, d.isoformat(), 8))

    def tearDown(self) -> None:
        db.DB_PATH = self.original_db_path
        self.tmp.cleanup()

    def test_even_spread(self):
        weeks = week_starts(date(2026, 1, 5), date(2026, 1, 25))
        self.assertEqual(spread_hours(90, weeks, 'Even spread'), [30, 30, 30])

    def test_front_loaded_spread(self):
        weeks = week_starts(date(2026, 1, 5), date(2026, 1, 25))
        self.assertEqual(spread_hours(60, weeks, 'Front-loaded'), [30, 20, 10])

    def test_back_loaded_spread(self):
        weeks = week_starts(date(2026, 1, 5), date(2026, 1, 25))
        self.assertEqual(spread_hours(60, weeks, 'Back-loaded'), [10, 20, 30])

    def test_manual_loading_validation_difference(self):
        self.assertEqual(manual_loading_difference(100, [25, 25, 40]), 10)

    def test_effective_capacity_uses_diminished_capacity_factor(self):
        caps = effective_capacity_by_week(self.rs_id, [date(2026, 1, 5)])
        self.assertEqual(caps['2026-01-05'], 34.0)

    def test_shortage_detection_in_preview(self):
        preview = build_loading_preview(self.project_id, self.rs_id, [date(2026, 1, 5)], [40])
        self.assertEqual(preview[0]['status'], 'red')
        self.assertEqual(preview[0]['surplus_or_shortage'], -6.0)

    def test_fit_to_capacity_behaviour(self):
        weeks = [date(2026, 1, 5), date(2026, 1, 12)]
        allocation, remaining, completion = fit_hours_to_capacity(70, weeks, self.rs_id, self.project_id)
        self.assertEqual(allocation, [34.0, 34.0])
        self.assertEqual(remaining, 2.0)
        self.assertIsNone(completion)

    def test_overall_capacity_summary_calculations(self):
        with db.connect() as conn:
            conn.execute('INSERT INTO weekly_demand(project_id,discipline_id,week_start,demand_hours) VALUES (?,?,?,?)', (self.project_id, self.rs_id, '2026-01-05', 40))
        summary = overall_capacity_summary('2026-01-05', '2026-01-11')
        self.assertEqual(summary['RS']['capacity'], 34.0)
        self.assertEqual(summary['RS']['allocated'], 40.0)
        self.assertEqual(summary['RS']['shortage'], 6.0)


if __name__ == '__main__':
    unittest.main()
