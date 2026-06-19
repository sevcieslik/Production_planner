from __future__ import annotations
from datetime import datetime
import pandas as pd
from app.data.db import connect, write_audit

TABLE_COLUMNS = {
 'people': ['name','discipline_code','daily_hours','weekly_hours'],
 'projects': ['project_name','client','start_date','end_date','deadline','status','notes','source_reference_id'],
 'planned_hours': ['project_name','discipline_code','planned_hours'],
 'osr_progress': ['project_name','progress_date','percent_complete','actual_hours_to_date'],
 'public_holidays': ['holiday_date','name','hours_removed'],
 'annual_leave': ['person_name','start_date','end_date','hours_per_day','leave_type','notes'],
}

def read_upload(file) -> pd.DataFrame:
    name = file.name.lower()
    return pd.read_excel(file) if name.endswith(('.xlsx','.xls')) else pd.read_csv(file)

def validate(df: pd.DataFrame, import_type: str) -> list[str]:
    required = TABLE_COLUMNS[import_type]
    return [c for c in required if c not in df.columns]

def import_dataframe(df: pd.DataFrame, import_type: str, user: str) -> int:
    now = datetime.utcnow().isoformat(timespec='seconds')
    count=0
    with connect() as conn:
      if import_type == 'projects':
        for _, r in df.iterrows():
          conn.execute('''INSERT OR REPLACE INTO projects(project_name,client,start_date,end_date,deadline,status,notes,source_reference_id,imported_at) VALUES (?,?,?,?,?,?,?,?,?)''', (r.project_name,r.client,str(r.start_date),str(r.end_date),str(r.deadline),getattr(r,'status','Active'),getattr(r,'notes',''),getattr(r,'source_reference_id',''),now)); count+=1
      elif import_type == 'osr_progress':
        for _, r in df.iterrows():
          p=conn.execute('SELECT id FROM projects WHERE project_name=?',(r.project_name,)).fetchone()
          if p:
            conn.execute('INSERT OR REPLACE INTO osr_progress(project_id,progress_date,percent_complete,actual_hours_to_date,imported_at) VALUES (?,?,?,?,?)',(p['id'],str(r.progress_date),float(r.percent_complete),float(r.actual_hours_to_date),now)); count+=1
      elif import_type == 'people':
        for _, r in df.iterrows():
          d=conn.execute('SELECT id FROM disciplines WHERE code=?',(r.discipline_code,)).fetchone()
          if d:
            conn.execute('INSERT OR REPLACE INTO people(name,discipline_id,daily_hours,weekly_hours,active) VALUES (?,?,?,?,1)',(r['name'],d['id'],float(r.daily_hours),float(r.weekly_hours))); count+=1
      elif import_type == 'planned_hours':
        for _, r in df.iterrows():
          p=conn.execute('SELECT id FROM projects WHERE project_name=?',(r.project_name,)).fetchone(); d=conn.execute('SELECT id FROM disciplines WHERE code=?',(r.discipline_code,)).fetchone()
          if p and d:
            conn.execute('INSERT OR REPLACE INTO project_discipline_budgets(project_id,discipline_id,planned_hours) VALUES (?,?,?)',(p['id'],d['id'],float(r.planned_hours))); count+=1
      elif import_type == 'annual_leave':
        for _, r in df.iterrows():
          p=conn.execute('SELECT id FROM people WHERE name=?',(r.person_name,)).fetchone()
          if p:
            conn.execute('INSERT INTO leave_records(person_id,start_date,end_date,hours_per_day,leave_type,notes) VALUES (?,?,?,?,?,?)',(p['id'],str(r.start_date),str(r.end_date),float(r.hours_per_day) if not pd.isna(r.hours_per_day) else None,getattr(r,'leave_type','Annual leave'),getattr(r,'notes',''))); count+=1
      elif import_type == 'public_holidays':
        for _, r in df.iterrows(): conn.execute('INSERT OR REPLACE INTO holiday_calendar(holiday_date,name,hours_removed) VALUES (?,?,?)',(str(r.holiday_date),r['name'],float(r.hours_removed) if not pd.isna(r.hours_removed) else None)); count+=1
      write_audit(conn,user,'Import',None,import_type,None,{'rows':count},'CSV/XLSX import')
    return count
