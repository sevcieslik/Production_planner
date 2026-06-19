from __future__ import annotations

import tempfile
import unittest
from pathlib import Path


import app.data.db as db
from app.importers.csv_xlsx import import_dataframe, normalize_dataframe, validate

PEOPLE_ALLOCATION_QUERY = '''SELECT pe.name, COALESCE(ac.work_date, '') work_date, COALESCE(ac.available_hours, pe.daily_hours, 0) available_hours, COALESCE(wi.name,pr.project_name,cat.name,'Unallocated') assigned_to, da.split_slot, COALESCE(da.allocated_hours,0) allocated_hours,
    CASE WHEN ac.work_date IS NULL THEN 'No availability record' WHEN COALESCE((SELECT SUM(allocated_hours) FROM daily_allocations x WHERE x.person_id=pe.id AND x.allocation_date=ac.work_date),0)>COALESCE(ac.available_hours,0) THEN 'Overallocated' WHEN COALESCE((SELECT SUM(allocated_hours) FROM daily_allocations x WHERE x.person_id=pe.id AND x.allocation_date=ac.work_date),0)=0 THEN 'Underallocated' ELSE 'OK' END flag
    FROM people pe LEFT JOIN availability_calendar ac ON ac.person_id=pe.id AND ac.work_date BETWEEN ? AND ? LEFT JOIN daily_allocations da ON da.person_id=pe.id AND da.allocation_date=ac.work_date LEFT JOIN projects pr ON pr.id=da.project_id LEFT JOIN work_items wi ON wi.id=da.work_item_id LEFT JOIN non_billable_categories cat ON cat.id=da.category_id WHERE pe.active=1 ORDER BY pe.name, ac.work_date, da.split_slot'''


class ImportRegressionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.original_db_path = db.DB_PATH
        db.DB_PATH = Path(self.tmp.name) / 'test.sqlite'
        db.initialize_database(seed=False)
        with db.connect() as conn:
            for code in ['RS', 'GIS', 'PLS']:
                conn.execute('INSERT INTO disciplines(code,name) VALUES (?,?)', (code, code))
            db.seed_capacity_rules(conn)

    def tearDown(self) -> None:
        db.DB_PATH = self.original_db_path
        self.tmp.cleanup()

    def test_people_import_creates_visible_people_records(self):
        result = import_dataframe([{'Name ': 'Imported Person', 'Discipline Code': 'RS', 'Daily Hours': 8, 'Weekly Hours': 40}], 'people', 'Tester')
        self.assertEqual(result.imported_rows, 1)
        names = [r['name'] for r in db.rows('SELECT name FROM people')]
        self.assertIn('Imported Person', names)

    def test_project_import_with_valid_rows_does_not_crash(self):
        result = import_dataframe([{'project_name': 'Valid Project', 'client': 'Client A', 'start_date': '2026-01-01', 'end_date': '2026-01-31', 'deadline': '2026-02-01', 'status': 'Active', 'notes': 'ok', 'source_reference_id': 'P1'}], 'projects', 'Tester')
        self.assertEqual(result.imported_rows, 1)
        self.assertEqual(result.skipped_rows, 0)
        self.assertEqual(db.rows('SELECT project_name FROM projects')[0]['project_name'], 'Valid Project')

    def test_project_import_skips_empty_project_name_rows(self):
        result = import_dataframe([{'project_name': '', 'client': 'Client A'}, {'project_name': 'Valid Project', 'client': 'Client A'}], 'projects', 'Tester')
        self.assertEqual(result.imported_rows, 1)
        self.assertEqual(result.skipped_rows, 1)
        self.assertEqual(result.validation_errors[0]['field'], 'project_name')
        self.assertEqual(len(db.rows('SELECT project_name FROM projects')), 1)

    def test_column_normalization_handles_trailing_spaces_and_bom(self):
        df = normalize_dataframe([{'\ufeffProject Name ': 'Normalized', 'Client ': 'Client A'}])
        self.assertIn('project_name', df[0])
        self.assertIn('client', df[0])
        self.assertFalse(validate(df, 'projects'))

    def test_imported_people_are_returned_by_people_allocation_query(self):
        import_dataframe([{'name': 'Visible Person', 'discipline_code': 'GIS', 'daily_hours': 7.5, 'weekly_hours': 37.5}], 'people', 'Tester')
        result = db.rows(PEOPLE_ALLOCATION_QUERY, ('2026-01-01', '2026-01-07'))
        self.assertIn('Visible Person', [r['name'] for r in result])
        visible = next(r for r in result if r['name'] == 'Visible Person')
        self.assertEqual(visible['flag'], 'No availability record')


if __name__ == '__main__':
    unittest.main()
