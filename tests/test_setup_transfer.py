from __future__ import annotations

from io import BytesIO
import json
from pathlib import Path
import tempfile
import unittest
from zipfile import ZipFile

import pandas as pd

import app.data.db as db
from app.services.mvp import PROJECT_FIELDS, ensure_mvp_schema, parse_audit_timestamps, save_internal_activities, save_projects, save_resources
from app.services.setup_transfer import (
    FORMAT, SETUP_FILES, VERSION, apply_planner_setup, apply_project_import,
    export_planner_setup, export_projects_csv, preview_planner_setup,
    preview_project_import,
)


def project(code: str, name: str = "Project", hours: float = 10) -> dict:
    return {"project_code": code, "project_name": name, "client": "Client", "project_manager": "Manager",
            "priority": "P2", "penalty_exposure": "None", "row_km": 0, "cct_km": 0, "spus": 0,
            "rs_hours": hours, "gis_hours": 0, "pls_hours": 0, "actual_rs_hours": 0,
            "actual_gis_hours": 0, "actual_pls_hours": 0, "start_date": "2026-08-24",
            "end_date": "2026-10-07", "loading_type": "even", "rs_start_date": "2026-08-24",
            "gis_start_date": "2026-08-24", "pls_start_date": "2026-08-24", "status": "active", "assumptions": ""}


class SetupTransferTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.old = db.DB_PATH
        db.DB_PATH = Path(self.tmp.name) / "planner.sqlite"
        db.initialize_database(seed=False); ensure_mvp_schema()

    def tearDown(self):
        db.DB_PATH = self.old; self.tmp.cleanup()

    def test_mixed_and_malformed_audit_timestamps_are_tolerated(self):
        values = pd.Series(["2026-08-25T13:50:12", "2026-08-25 13:56:38",
                            "2026-08-25T13:56:38.123456", "not a timestamp"])
        parsed = parse_audit_timestamps(values)
        self.assertEqual(parsed.notna().sum(), 3); self.assertTrue(pd.isna(parsed.iloc[3]))

    def test_new_audit_timestamp_uses_sqlite_format(self):
        with db.connect() as conn: db.write_audit(conn,"Admin","Test",None,"change",None,None)
        timestamp = db.rows("SELECT timestamp FROM audit_log ORDER BY id DESC LIMIT 1")[0]["timestamp"]
        self.assertRegex(timestamp, r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")

    def test_project_csv_export_preview_upsert_and_non_deletion(self):
        save_projects([project("KEEP","Keep"),project("CHANGE","Before",10)])
        exported = pd.read_csv(BytesIO(export_projects_csv()))
        self.assertEqual(exported.columns.tolist(), PROJECT_FIELDS)
        changed=project("CHANGE","After",20); new=project("NEW","New")
        upload=pd.DataFrame([project("KEEP","Keep"),changed,new]).to_csv(index=False).encode()
        preview=preview_project_import(BytesIO(upload))
        self.assertEqual((preview["new"],preview["updated"],preview["unchanged"],preview["invalid"]),(1,1,1,0))
        self.assertTrue(any(d["field"]=="rs_hours" for d in preview["changes"][0]["differences"]))
        apply_project_import(preview,"Admin"); apply_project_import(preview_project_import(BytesIO(upload)),"Admin")
        self.assertEqual(len(db.rows("SELECT * FROM mvp_projects")),3)
        self.assertTrue(db.rows("SELECT 1 FROM mvp_projects WHERE project_code='KEEP'"))
        event=db.rows("SELECT * FROM audit_log WHERE action='Project CSV imported' ORDER BY id DESC LIMIT 1")[0]
        self.assertEqual(event["user_name"],"Admin"); self.assertNotIn("password",(event["new_value"] or "").lower())

    def _populated_zip(self) -> bytes:
        save_resources([{"person_name":"Person","department":"RS","weekly_hours":37.5,"active_status":"active"}])
        rid=db.rows("SELECT id FROM mvp_resources WHERE person_name='Person'")[0]["id"]
        with db.connect() as conn: conn.execute("INSERT INTO resource_employee_ids(employee_id,resource_id,employee_name) VALUES ('E123',?,'Person')",(rid,))
        save_projects([project("P1")]); save_internal_activities([{"activity_name":"Training","department":"RS","start_week":"2026-08-24","end_week":"2026-08-31","planned_hours_per_week":5,"active":True}])
        with db.connect() as conn: conn.execute("INSERT INTO manager_weekly_plan(project_code,department,week_start,planned_hours,updated_by) VALUES ('P1','RS','2026-08-24',8,'Old user')")
        return export_planner_setup()

    def test_setup_zip_contents_manifest_records_and_exclusions(self):
        payload=self._populated_zip()
        with ZipFile(BytesIO(payload)) as archive:
            self.assertEqual(set(archive.namelist()),SETUP_FILES)
            manifest=json.loads(archive.read("manifest.json")); self.assertEqual((manifest["format"],manifest["version"]),(FORMAT,VERSION))
            combined=b"".join(archive.read(n) for n in archive.namelist()).lower()
            self.assertIn(b"p1",combined); self.assertIn(b"person",combined); self.assertIn(b"e123",combined)
            for forbidden in (b"password",b"audit_log",b"planner_users_json",b"holiday_imports",b"old user"):
                self.assertNotIn(forbidden,combined)

    def test_setup_import_is_idempotent_validates_fk_and_audits_admin(self):
        payload=self._populated_zip()
        db.DB_PATH=Path(self.tmp.name)/"target.sqlite"; db.initialize_database(seed=False); ensure_mvp_schema()
        preview=preview_planner_setup(BytesIO(payload)); self.assertTrue(preview["valid"],preview.get("errors"))
        apply_planner_setup(preview,"Render Admin"); apply_planner_setup(preview_planner_setup(BytesIO(payload)),"Render Admin")
        self.assertEqual(len(db.rows("SELECT * FROM mvp_resources")),1); self.assertEqual(len(db.rows("SELECT * FROM mvp_projects")),1)
        self.assertEqual(len(db.rows("SELECT * FROM manager_weekly_plan")),1); self.assertEqual(len(db.rows("SELECT * FROM internal_activities")),1)
        allocation=db.rows("SELECT * FROM manager_weekly_plan")[0]; self.assertEqual(allocation["updated_by"],"Render Admin")
        event=db.rows("SELECT * FROM audit_log WHERE action='Planner setup imported' ORDER BY id DESC LIMIT 1")[0]
        self.assertEqual(event["user_name"],"Render Admin"); self.assertNotIn("password",(event["new_value"] or "").lower())
        with ZipFile(BytesIO(payload)) as source:
            files={n:source.read(n) for n in source.namelist()}
        allocations=pd.read_csv(BytesIO(files["weekly_allocations.csv"])); allocations.loc[0,"project_code"]="MISSING"; files["weekly_allocations.csv"]=allocations.to_csv(index=False).encode()
        invalid=BytesIO()
        with ZipFile(invalid,"w") as target:
            for name,data in files.items(): target.writestr(name,data)
        bad=preview_planner_setup(BytesIO(invalid.getvalue())); self.assertFalse(bad["valid"]); self.assertTrue(any("unknown project_code" in e for e in bad["errors"]))

    def test_setup_apply_failure_rolls_back(self):
        payload=self._populated_zip(); db.DB_PATH=Path(self.tmp.name)/"rollback.sqlite"; db.initialize_database(seed=False); ensure_mvp_schema()
        preview=preview_planner_setup(BytesIO(payload))
        with self.assertRaises(RuntimeError): apply_planner_setup(preview,"Admin",fail_after="resources")
        self.assertFalse(db.rows("SELECT * FROM mvp_resources")); self.assertFalse(db.rows("SELECT * FROM mvp_projects"))


if __name__ == "__main__": unittest.main()
