from datetime import date
from io import StringIO
from pathlib import Path
import tempfile
import unittest

import app.data.db as db
from app.services.mvp import (
    apply_holiday_snapshot, apply_quick_allocation, capacity_balance,
    clear_future_allocation, import_approved_holidays, monthly_allocation_matrix,
    move_allocation, preview_holiday_snapshot, quick_allocation_values,
    resolve_resource_for_employee, save_internal_activities, save_projects,
    save_resources,
)


class PlannerExtensionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.old = db.DB_PATH
        db.DB_PATH = Path(self.tmp.name) / "test.sqlite"; db.initialize_database(seed=False)
        save_resources([{"person_name":"Luke Beaumont","department":"RS","weekly_hours":37.5,"active_status":"active"}])
        save_projects([{"project_code":"HONI","project_name":"HONI 2026","client":"C","project_manager":"M","priority":"P1","rs_hours":600,"actual_rs_hours":10,"gis_hours":0,"pls_hours":0,"start_date":"2026-09-07","end_date":"2026-12-31","rs_start_date":"2026-09-07","status":"active"}])
        self.weeks = [date(2026,9,7),date(2026,9,14),date(2026,9,21),date(2026,9,28)]

    def tearDown(self):
        db.DB_PATH=self.old; self.tmp.cleanup()

    def test_people_add_replace_overallocation_move_and_clear(self):
        values=quick_allocation_values("People",self.weeks,people=3,hours_per_person=37.5)
        self.assertEqual(values,[112.5]*4)
        apply_quick_allocation("HONI","RS",self.weeks,values,"Manager")
        apply_quick_allocation("HONI","RS",self.weeks,[1]*4,"Manager","add")
        self.assertEqual(sum(r["planned_hours"] for r in db.rows("SELECT planned_hours FROM manager_weekly_plan")),454)
        apply_quick_allocation("HONI","RS",self.weeks,[10]*4,"Manager","replace")
        self.assertEqual(sum(r["planned_hours"] for r in db.rows("SELECT planned_hours FROM manager_weekly_plan")),40)
        with self.assertRaises(ValueError):
            apply_quick_allocation("HONI","RS",self.weeks,[200]*4,"Manager","replace")
        moved=move_allocation("HONI","RS",1,date(2026,9,7),date(2026,10,26),"Manager")
        self.assertEqual(moved["moved_hours"],40)
        clear_future_allocation("HONI","RS",date(2026,9,28),"Manager")
        active=db.rows("SELECT week_start,planned_hours FROM manager_weekly_plan WHERE planned_hours>0")
        self.assertEqual([r["week_start"] for r in active],["2026-09-14","2026-09-21"])

    def test_monthly_aggregation_and_internal_capacity_equation(self):
        apply_quick_allocation("HONI","RS",self.weeks,[10]*4,"M","replace")
        matrix=monthly_allocation_matrix(date(2026,9,1),date(2026,10,31),"RS")
        self.assertEqual(float(matrix["Sep 2026"].iloc[0]),40)
        save_internal_activities([{"activity_name":"Training","department":"RS","start_week":"2026-09-07","end_week":"2026-09-07","planned_hours_per_week":5,"active":True}])
        bal=capacity_balance([date(2026,9,7)]).query("department == 'RS'").iloc[0]
        self.assertEqual(float(bal.available_capacity),37.5)
        self.assertEqual(float(bal.total_allocated),15)
        self.assertEqual(float(bal.over_under_capacity),22.5)

    def test_holiday_snapshot_half_day_change_cancel_match_and_idempotency(self):
        first=StringIO("Employee,Date From,Date To,Days of Absence\n\"Beaumont, Luke (28786)\",07/09/2026,07/09/2026,0.5\nMissing Person (99),08/09/2026,08/09/2026,1\n"); first.name="h.csv"
        preview=preview_holiday_snapshot(first)
        self.assertEqual(len(preview["new"]),1); self.assertEqual(preview["new"][0]["hours"],3.75)
        self.assertEqual(preview["unmatched"],["Missing Person (99)"])
        apply_holiday_snapshot(preview,"h.csv","HR")
        same=StringIO("Employee,Date From,Date To,Days of Absence\n\"Beaumont, Luke (28786)\",07/09/2026,07/09/2026,0.5\n"); same.name="h.csv"
        preview2=preview_holiday_snapshot(same)
        self.assertEqual((len(preview2["new"]),len(preview2["changed"]),len(preview2["removed"])),(0,0,0))
        changed=StringIO("Employee,Date From,Date To,Days of Absence\n\"Beaumont, Luke (28786)\",07/09/2026,07/09/2026,1\n"); changed.name="h.csv"
        p3=preview_holiday_snapshot(changed); self.assertEqual(len(p3["changed"]),1)
        apply_holiday_snapshot(p3,"h.csv","HR")
        empty=StringIO("Employee,Date From,Date To,Days of Absence\n"); empty.name="h.csv"
        p4=preview_holiday_snapshot(empty); self.assertEqual(len(p4["removed"]),1)
        apply_holiday_snapshot(p4,"h.csv","HR")
        self.assertEqual(db.rows("SELECT status FROM holidays")[0]["status"],"cancelled")

    def test_holiday_employee_resolution_order(self):
        save_resources([
            {"person_name":"Allott, Mathew (32920)","department":"RS","weekly_hours":37.5,"active_status":"active"},
            {"person_name":"Mapped, Employee (777)","department":"RS","weekly_hours":37.5,"active_status":"active"},
            {"person_name":"Dunhill, Rachel","department":"RS","weekly_hours":37.5,"active_status":"active"},
        ])
        resources = db.rows("SELECT * FROM mvp_resources")
        by_name = {row["person_name"]: row for row in resources}

        exact = resolve_resource_for_employee("Allott, Mathew (32920)", resources, {})
        self.assertEqual(exact["id"], by_name["Allott, Mathew (32920)"]["id"])
        different_format = resolve_resource_for_employee("Completely Different (32920)", resources, {})
        self.assertEqual(different_format["id"], exact["id"])

        saved = resolve_resource_for_employee(
            "Allott, Mathew (32920)", resources,
            {"32920": by_name["Mapped, Employee (777)"]},
        )
        self.assertEqual(saved["id"], by_name["Mapped, Employee (777)"]["id"])

        fallback = resolve_resource_for_employee("Dunhill, Rachel", resources, {})
        self.assertEqual(fallback["id"], by_name["Dunhill, Rachel"]["id"])
        self.assertIsNone(resolve_resource_for_employee("Nobody, Missing", resources, {}))

    def test_embedded_id_preview_apply_mapping_consistency(self):
        save_resources([{"person_name":"Allott, Mathew (32920)","department":"RS","weekly_hours":37.5,"active_status":"active"}])
        upload = StringIO('Employee,Date From,Date To,Days of Absence\n"Different, Formatting (32920)",07/09/2026,07/09/2026,1\n')
        upload.name = "holidays.csv"
        preview = preview_holiday_snapshot(upload)
        resource_id = db.rows("SELECT id FROM mvp_resources WHERE person_name='Allott, Mathew (32920)'")[0]["id"]
        self.assertEqual(preview["unmatched"], [])
        self.assertEqual(preview["records"][0]["resource_id"], resource_id)

        apply_holiday_snapshot(preview, upload.name, "HR")
        holiday = db.rows("SELECT resource_id FROM holidays WHERE employee_id='32920'")[0]
        mapping = db.rows("SELECT resource_id FROM resource_employee_ids WHERE employee_id='32920'")[0]
        self.assertEqual(holiday["resource_id"], resource_id)
        self.assertEqual(mapping["resource_id"], resource_id)


if __name__ == "__main__": unittest.main()
