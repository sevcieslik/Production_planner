from datetime import date
from pathlib import Path
import tempfile
import unittest

import app.data.db as db
from app.services.mvp import (
    apply_quick_allocation,
    save_capacity_adjustment,
    save_projects,
    save_resources,
    resource_availability_matrix,
    sequence_analysis,
    weekly_department_capacity,
)


class ResourceAdjustmentAndSequenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = db.DB_PATH
        db.DB_PATH = Path(self.tmp.name) / "test.sqlite"
        db.initialize_database(seed=False)
        save_resources([{"person_name": "Alex", "department": "RS", "weekly_hours": 37.5, "active_status": "active"}])
        self.resource_id = db.rows("SELECT id FROM mvp_resources WHERE person_name='Alex'")[0]["id"]

    def tearDown(self):
        db.DB_PATH = self.old
        self.tmp.cleanup()

    def test_partial_assignment_moves_capacity_without_changing_home_department(self):
        save_capacity_adjustment({"resource_id": self.resource_id, "adjustment_type": "Temporary assignment",
            "destination_department": "GIS", "start_date": "2026-09-07", "end_date": "2026-09-11",
            "capacity_percent": 50, "reason": "Delivery support", "active": True}, "Manager")
        cap = weekly_department_capacity([date(2026, 9, 7)]).set_index("department").available_capacity
        self.assertEqual(cap["RS"], 18.75)
        self.assertEqual(cap["GIS"], 18.75)
        self.assertEqual(db.rows("SELECT department FROM mvp_resources WHERE id=?", (self.resource_id,))[0]["department"], "RS")
        self.assertEqual(db.rows("SELECT object_type FROM audit_log ORDER BY id DESC LIMIT 1")[0]["object_type"], "Resource capacity adjustment")
        detail = resource_availability_matrix([date(2026, 9, 7)]).iloc[0]
        self.assertEqual(detail["Availability"], "18.75 GIS / 18.75 RS")
        self.assertEqual(sum(detail["Department Contributions"].values()), 37.5)
        self.assertEqual(sum(cap), detail["Available Hours"])

    def test_holiday_and_unavailability_are_capped_and_overlap_validation_rejects_over_100_percent(self):
        db.execute("INSERT INTO holidays(resource_id,person_name,holiday_date,hours,source) VALUES (?,?,?,?,?)",
                   (self.resource_id, "Alex", "2026-09-07", 7.5, "test"))
        save_capacity_adjustment({"resource_id": self.resource_id, "adjustment_type": "Training",
            "start_date": "2026-09-07", "end_date": "2026-09-11", "capacity_percent": 50,
            "reason": "Course", "active": True}, "Manager")
        cap = weekly_department_capacity([date(2026, 9, 7)]).set_index("department").available_capacity
        self.assertEqual(cap["RS"], 15.0)
        with self.assertRaisesRegex(ValueError, "exceed 100%"):
            save_capacity_adjustment({"resource_id": self.resource_id, "adjustment_type": "Unavailable",
                "start_date": "2026-09-07", "end_date": "2026-09-11", "capacity_percent": 60,
                "active": True}, "Manager")

    def test_sequence_analysis_reports_gap_starvation_and_pull_forward_from_explicit_plan(self):
        save_resources([{"person_name":"Gina","department":"GIS","weekly_hours":37.5,"active_status":"active"}])
        save_projects([{"project_code":"P1","project_name":"Project","client":"C","project_manager":"M",
            "rs_hours":20,"gis_hours":20,"pls_hours":0,"start_date":"2026-09-07","end_date":"2026-10-30",
            "rs_start_date":"2026-09-07","gis_start_date":"2026-09-14","status":"active"}])
        weeks=[date(2026,9,7),date(2026,9,14),date(2026,9,21),date(2026,9,28),date(2026,10,5)]
        apply_quick_allocation("P1","RS",[weeks[0]],[20],"M","replace")
        apply_quick_allocation("P1","GIS",[weeks[4]],[20],"M","replace")
        findings=sequence_analysis(weeks)
        self.assertEqual(set(findings.Category), {"Gap","Downstream starvation","Pull-forward opportunity"})
        self.assertEqual(int(findings.iloc[0]["Gap days"]), 21)


if __name__ == "__main__":
    unittest.main()
