from datetime import date
from pathlib import Path
import tempfile
import unittest
import json

import pandas as pd
import app.data.db as db
from app.services.mvp import (
    allocation_timeline, apply_quick_allocation, clear_future_allocation, create_escalation, get_issues,
    future_project_allocation, manager_plan,
    project_health, save_internal_activities, save_projects, save_resources,
    update_issue, capacity_balance,
)


class OperationalPlanningTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.old=db.DB_PATH
        db.DB_PATH=Path(self.tmp.name)/"planner.sqlite"; db.initialize_database(seed=False)
        save_projects([{"project_code":"P1","project_name":"Delivery","client":"C","project_manager":"M","priority":"P1","rs_hours":500,"gis_hours":50,"start_date":"2026-09-01","end_date":"2026-09-30","rs_start_date":"2026-09-03","gis_start_date":"2026-09-10","status":"active"}],"Planner")
        self.weeks=[date(2026,9,7),date(2026,9,14),date(2026,9,21),date(2026,9,28)]
    def tearDown(self): db.DB_PATH=self.old; self.tmp.cleanup()

    def test_project_health_definitions_and_tolerance(self):
        self.assertEqual(project_health(500,0),"Unplanned")
        self.assertEqual(project_health(500,300),"Under-resourced")
        self.assertEqual(project_health(500,500),"Well-resourced")
        self.assertEqual(project_health(500,501),"Over-resourced")
        self.assertEqual(project_health(500,500.01),"Well-resourced")

    def test_gantt_explicit_bounds_ignore_zero_edges_and_fallback_groups(self):
        with db.connect() as conn:
            for week,hours in zip(self.weeks,[0,20,30,0]):
                conn.execute("INSERT INTO manager_weekly_plan VALUES (?,?,?,?,?,CURRENT_TIMESTAMP)",("P1","RS",week.isoformat(),hours,"Planner"))
        gantt=allocation_timeline(self.weeks,"All")
        rs=gantt[gantt.Discipline=="RS"].iloc[0]; gis=gantt[gantt.Discipline=="GIS"].iloc[0]
        self.assertEqual((rs.Start,rs.End),(date(2026,9,14),date(2026,9,27)))
        self.assertEqual(rs["Plan source"],"Manager allocation")
        self.assertEqual(gis["Plan source"],"Forecast baseline")
        self.assertEqual(gis["Required by"],"2026-09-30")
        self.assertEqual(gantt.Discipline.tolist(),["RS","GIS"])

    def test_after_deadline_allocation_does_not_cover_health(self):
        apply_quick_allocation("P1","RS",[date(2026,10,5)],[500],"Planner","replace")
        gantt=allocation_timeline([date(2026,9,7),date(2026,10,5)],"RS")
        self.assertEqual(gantt.iloc[0]["Health status"],"Unplanned")

    def test_balance_includes_internal_activity(self):
        save_resources([{"person_name":"A","department":"RS","weekly_hours":40,"active_status":"active"}])
        apply_quick_allocation("P1","RS",[self.weeks[0]],[10],"Planner","replace")
        save_internal_activities([{"activity_name":"Training","department":"RS","start_week":self.weeks[0],"end_week":self.weeks[0],"planned_hours_per_week":5,"active":True}],"Planner")
        row=capacity_balance([self.weeks[0]]).query("department == 'RS'").iloc[0]
        self.assertEqual(row.over_under_capacity,row.available_capacity-10-5)

    def test_issue_lifecycle_and_audit_change_suppression(self):
        issue=create_escalation("P1","RS","Capacity shortage",20,"Choose coverage","Owner",date(2026,9,20),"Planner")
        update_issue(issue,"Planner",resolution="Moved capacity",status="Closed")
        self.assertEqual(get_issues("Closed").iloc[0].resolution,"Moved capacity")
        update_issue(issue,"Planner",status="Open")
        self.assertEqual(get_issues("Open").iloc[0].status,"Open")
        apply_quick_allocation("P1","RS",[self.weeks[0]],[10],"Planner","replace")
        before=len(db.rows("SELECT id FROM audit_log"))
        apply_quick_allocation("P1","RS",[self.weeks[0]],[10],"Planner","replace")
        self.assertEqual(len(db.rows("SELECT id FROM audit_log")),before)
        event=db.rows("SELECT * FROM audit_log WHERE action='Quick Allocation'")[0]
        self.assertEqual((json.loads(event["previous_value"]),json.loads(event["new_value"])),(0,10.0))

    def test_replace_and_clear_future_preserve_history_and_ignore_visible_end(self):
        apply_quick_allocation("P1","RS",[date(2026,8,24),date(2026,9,7),date(2026,12,7)],
                               [5,20,30],"Planner","replace")
        apply_quick_allocation("P1","RS",[date(2026,9,14)],[40],"Planner","replace_future",False,date(2026,9,1))
        saved=db.rows("SELECT week_start,planned_hours FROM manager_weekly_plan WHERE project_code='P1' AND department='RS' AND planned_hours>0 ORDER BY week_start")
        self.assertEqual([(r["week_start"],r["planned_hours"]) for r in saved],[('2026-08-24',5.0),('2026-09-14',40.0)])
        self.assertEqual(future_project_allocation("P1","RS",date(2026,9,1)),40)
        clear_future_allocation("P1","RS",date(2026,9,1),"Planner")
        self.assertEqual(db.rows("SELECT planned_hours FROM manager_weekly_plan WHERE week_start='2026-08-24'")[0]["planned_hours"],5)
        self.assertEqual(future_project_allocation("P1","RS",date(2026,9,1)),0)
        self.assertTrue(db.rows("SELECT id FROM audit_log WHERE action='Future allocation cleared'"))

    def test_unplanned_uses_all_future_allocation_not_visible_end_or_history(self):
        apply_quick_allocation("P1","RS",[date(2026,8,24),date(2026,12,7)],[300,200],"Planner","replace",True)
        plan=manager_plan([date(2026,9,7)],"RS")
        self.assertEqual(plan.iloc[0]["Unplanned Hours"],300)
        gantt=allocation_timeline([date(2026,9,7),date(2026,12,7)],"RS")
        self.assertEqual(gantt.iloc[0]["Unplanned hours"],300)


if __name__ == '__main__': unittest.main()
