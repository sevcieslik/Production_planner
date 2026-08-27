from __future__ import annotations

import tempfile
import unittest
from io import BytesIO
from pathlib import Path

import app.data.db as db
from app.services.legacy_allocation_import import (
    DISCIPLINE_MAP, apply_legacy_allocation, legacy_preview_row_key, legacy_upload_key,
    preview_legacy_allocation,
)
from app.services.mvp import get_data_version, save_projects


def csv_file(rows: list[str], second_date: str = "31/08/2026 00:00:00") -> bytes:
    header = f"Active,Project,Discipline,Planned HRS,HRS Assigned,HRS remaining,Status,24/08/2026,{second_date}"
    return (header + "\n" + "\n".join(rows)).encode()


class LegacyAllocationImportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = db.DB_PATH
        db.DB_PATH = Path(self.tmp.name) / "test.sqlite"
        db.initialize_database(seed=False)
        save_projects([
            {"project_code": "P1", "project_name": "HONI 2026", "client": "Client", "start_date": "2026-01-01", "end_date": "2026-12-31", "rs_hours": 100, "actual_rs_hours": 12, "loading_type": "even", "status": "active"},
            {"project_code": "P2", "project_name": "SoCo   GPC 2026", "client": "Client", "start_date": "2026-01-01", "end_date": "2026-12-31", "loading_type": "even", "status": "active"},
        ])
        with db.connect() as conn:
            conn.execute("INSERT INTO manager_weekly_plan VALUES ('P1','GIS','2025-01-06',99,'Old',CURRENT_TIMESTAMP)")
            conn.execute("INSERT INTO manager_weekly_plan VALUES ('P2','PLS','2025-01-13',21,'Old',CURRENT_TIMESTAMP)")

    def tearDown(self):
        db.DB_PATH = self.old
        self.tmp.cleanup()

    def test_replacement_dates_zero_inactive_footer_admin_and_preservation(self):
        source = csv_file([
            "TRUE,HONI 2026,1_RS,999,10,0,,10,0",
            "FALSE,  soco gpc 2026  ,2_GIS,999,5,0,,,5",
            "TRUE,Unknown,3_PLS,5,5,0,,5,",
            "TRUE,Administrative,3_PLS,3,3,0,,3,",
            "TRUE,Summary,TOTAL,999,999,0,,999,999",
            ",Total Availability,,999,999,0,,999,999",
            ",Luke Beaumont,,,,,,,",
            ",product / km/h reference data,,,,,,,",
        ])
        before_project = db.rows("SELECT rs_hours,actual_rs_hours FROM mvp_projects WHERE project_code='P1'")[0]
        version = get_data_version()
        preview = preview_legacy_allocation(BytesIO(source), filename="legacy.csv")
        self.assertEqual((preview["matched"], preview["unmatched"], preview["ambiguous"]), (2, 1, 0))
        self.assertEqual(preview["legacy_rows"], 4)
        self.assertEqual(preview["import_records"], 2)  # blank and zero cells never become records
        self.assertEqual((preview["earliest_week"], preview["latest_week"]), ("2026-08-24", "2026-08-31"))
        self.assertEqual(preview["internal_hours"], 3)
        self.assertEqual(len(db.rows("SELECT * FROM manager_weekly_plan")), 2)  # preview is read-only

        result = apply_legacy_allocation(preview, user="Admin", confirmed=True)
        plans = db.rows("SELECT project_code,department,week_start,planned_hours FROM manager_weekly_plan ORDER BY project_code")
        self.assertEqual(result["removed_records"], 2)
        self.assertEqual(result["imported_records"], 2)
        self.assertEqual([(p["project_code"], p["department"], p["week_start"], p["planned_hours"]) for p in plans],
                         [("P1", "RS", "2026-08-24", 10), ("P2", "GIS", "2026-08-31", 5)])
        self.assertEqual(db.rows("SELECT rs_hours,actual_rs_hours FROM mvp_projects WHERE project_code='P1'")[0], before_project)
        activity = db.rows("SELECT * FROM internal_activities WHERE activity_name='Administrative'")
        self.assertEqual((len(activity), activity[0]["planned_hours_per_week"]), (1, 3))
        audit = db.rows("SELECT action,new_value FROM audit_log WHERE action='legacy_planner_allocation_import'")
        self.assertEqual(len(audit), 1)
        self.assertGreater(get_data_version(), version)

    def test_rollback_after_clear_retains_existing_plan(self):
        preview = preview_legacy_allocation(csv_file(["TRUE,HONI 2026,1_RS,5,5,0,,5,"]))
        with self.assertRaisesRegex(ValueError, "failed after clear"):
            apply_legacy_allocation(preview, user="Admin", confirmed=True, fail_after_clear=True)
        self.assertEqual(sum(r["planned_hours"] for r in db.rows("SELECT planned_hours FROM manager_weekly_plan")), 120)

    def test_ambiguous_match_blocks_apply_and_manual_mapping_resolves(self):
        save_projects([{"project_code": "P3", "project_name": "HONI 2026", "client": "Other", "start_date": "2026-01-01", "end_date": "2026-12-31", "loading_type": "even", "status": "active"}])
        source = csv_file(["TRUE,HONI 2026,3_PLS,5,5,0,,5,"])
        ambiguous = preview_legacy_allocation(source)
        self.assertEqual(ambiguous["ambiguous"], 1)
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            apply_legacy_allocation(ambiguous, user="Admin", confirmed=True)
        resolved = preview_legacy_allocation(source, mappings={"HONI 2026": "P3"})
        self.assertEqual((resolved["matched"], resolved["ambiguous"]), (1, 0))

    def test_discipline_mapping_contract(self):
        self.assertEqual(DISCIPLINE_MAP, {"1_RS": "RS", "2_GIS": "GIS", "3_PLS": "PLS"})

    def test_widget_identities_are_stable_and_unique_for_duplicate_projects(self):
        content = csv_file([
            "TRUE,Repeated,1_RS,5,5,0,,5,",
            "TRUE,Repeated,2_GIS,5,5,0,,5,",
            "TRUE,Repeated,1_RS,5,5,0,,5,",
        ])
        first_upload_key = legacy_upload_key("legacy.csv", content)
        self.assertEqual(first_upload_key, legacy_upload_key("legacy.csv", content))
        self.assertNotEqual(first_upload_key, legacy_upload_key("legacy.csv", content + b"\n"))

        preview = preview_legacy_allocation(content)
        row_keys = [row["mapping_id"] for row in preview["rows"]]
        self.assertEqual(len(row_keys), len(set(row_keys)))
        self.assertEqual(row_keys[0], legacy_preview_row_key(2, "Repeated", "RS"))

    def test_manual_mappings_are_independent_per_source_row(self):
        content = csv_file([
            "TRUE,Repeated,1_RS,5,5,0,,5,",
            "TRUE,Repeated,2_GIS,5,5,0,,5,",
        ])
        rs_key = legacy_preview_row_key(2, "Repeated", "RS")
        resolved = preview_legacy_allocation(content, mappings={rs_key: "P1"})
        self.assertEqual(resolved["rows"][0]["project_code"], "P1")
        self.assertEqual(resolved["rows"][0]["match_status"], "Matched")
        self.assertEqual(resolved["rows"][1]["match_status"], "Unmatched")


if __name__ == "__main__":
    unittest.main()
