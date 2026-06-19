from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import re
from typing import Any

try:
    import pandas as pd
except ModuleNotFoundError:  # pragma: no cover - minimal test/runtime environments
    pd = None
from app.data.db import connect, write_audit

TABLE_COLUMNS = {
 'people': ['name','discipline_code','daily_hours','weekly_hours'],
 'projects': ['project_name','client','start_date','end_date','deadline','status','notes','source_reference_id'],
 'planned_hours': ['project_name','discipline_code','planned_hours'],
 'osr_progress': ['project_name','progress_date','percent_complete','actual_hours_to_date'],
 'public_holidays': ['holiday_date','name','hours_removed'],
 'annual_leave': ['person_name','start_date','end_date','hours_per_day','leave_type','notes'],
}

REQUIRED_COLUMNS = {
 'people': ['name','discipline_code'],
 'projects': ['project_name'],
 'planned_hours': ['project_name','discipline_code','planned_hours'],
 'osr_progress': ['project_name','progress_date','percent_complete','actual_hours_to_date'],
 'public_holidays': ['holiday_date','name'],
 'annual_leave': ['person_name','start_date','end_date'],
}

@dataclass
class ImportResult:
    affected_table: str
    imported_rows: int = 0
    skipped_rows: int = 0
    validation_errors: list[dict[str, Any]] = field(default_factory=list)

    @property
    def rows(self) -> int:
        return self.imported_rows

    def __int__(self) -> int:
        return self.imported_rows

    def __str__(self) -> str:
        return str(self.imported_rows)


def normalize_column_name(name: object) -> str:
    text = str(name).replace('\ufeff', '').replace('\u200b', '').strip().lower()
    text = re.sub(r'\s+', '_', text)
    text = re.sub(r'[^a-z0-9_]', '_', text)
    text = re.sub(r'_+', '_', text).strip('_')
    return text


def _is_missing(value: Any) -> bool:
    if value is None:
        return True
    if pd is not None:
        try:
            return bool(pd.isna(value))
        except (TypeError, ValueError):
            return False
    return False


def normalize_dataframe(df):
    if pd is not None and isinstance(df, pd.DataFrame):
        out = df.copy()
        out.columns = [normalize_column_name(c) for c in out.columns]
        return out.where(pd.notna(out), None)
    return [{normalize_column_name(k): (None if _is_missing(v) else v) for k, v in row.items()} for row in df]


def _columns(df) -> list[str]:
    if pd is not None and isinstance(df, pd.DataFrame):
        return list(df.columns)
    cols: list[str] = []
    for row in df:
        for key in row:
            if key not in cols:
                cols.append(key)
    return cols


def _records(df) -> list[dict[str, Any]]:
    if pd is not None and isinstance(df, pd.DataFrame):
        return df.to_dict('records')
    return list(df)


def _row_count(df) -> int:
    return len(df.index) if pd is not None and isinstance(df, pd.DataFrame) else len(df)


def read_upload(file):
    if pd is None:
        raise RuntimeError('CSV/XLSX upload requires pandas to be installed in the Streamlit environment.')
    name = file.name.lower()
    df = pd.read_excel(file) if name.endswith(('.xlsx','.xls')) else pd.read_csv(file)
    return normalize_dataframe(df)


def validate(df: pd.DataFrame, import_type: str) -> list[str]:
    normalized = normalize_dataframe(df)
    required = REQUIRED_COLUMNS[import_type]
    return [c for c in required if c not in _columns(normalized)]


def _clean(value: Any, default: str = '') -> str:
    if _is_missing(value):
        return default
    return str(value).replace('\ufeff', '').strip()


def _float(value: Any, default: float = 0.0) -> float:
    if _is_missing(value) or value == '':
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _date(value: Any, default: str | None = None) -> str:
    if _is_missing(value) or value == '':
        return default or datetime.utcnow().date().isoformat()
    if pd is not None:
        parsed = pd.to_datetime(value, errors='coerce')
        if pd.isna(parsed):
            return default or datetime.utcnow().date().isoformat()
        return parsed.date().isoformat()
    try:
        return datetime.fromisoformat(str(value)).date().isoformat()
    except ValueError:
        return default or datetime.utcnow().date().isoformat()


def _error(errors: list[dict[str, Any]], row_number: int, field: str, message: str, row: dict[str, Any]) -> None:
    errors.append({'row_number': row_number, 'field': field, 'message': message, 'row': row})


def import_dataframe(df: pd.DataFrame, import_type: str, user: str) -> ImportResult:
    df = normalize_dataframe(df)
    result = ImportResult(affected_table=import_type)
    missing = validate(df, import_type)
    if missing:
        for col in missing:
            result.validation_errors.append({'row_number': None, 'field': col, 'message': f'Missing required column: {col}', 'row': {}})
        result.skipped_rows = _row_count(df)
        return result

    now = datetime.utcnow().isoformat(timespec='seconds')
    with connect() as conn:
      if import_type == 'projects':
        project_type = conn.execute('SELECT id FROM work_item_types WHERE name="Project"').fetchone()
        for row_number, r in enumerate(_records(df), start=2):
          project_name = _clean(r.get('project_name'))
          if not project_name:
            _error(result.validation_errors, row_number, 'project_name', 'Project name is required; row skipped.', r); result.skipped_rows += 1; continue
          client = _clean(r.get('client'))
          start_date = _date(r.get('start_date'))
          end_date = _date(r.get('end_date'), start_date)
          deadline = _date(r.get('deadline'), end_date)
          status = _clean(r.get('status'), 'Active') or 'Active'
          notes = _clean(r.get('notes'))
          source_reference_id = _clean(r.get('source_reference_id'))
          conn.execute('''INSERT INTO projects(project_name,client,start_date,end_date,deadline,status,notes,source_reference_id,imported_at)
                          VALUES (?,?,?,?,?,?,?,?,?)
                          ON CONFLICT(project_name,client) DO UPDATE SET start_date=excluded.start_date,end_date=excluded.end_date,deadline=excluded.deadline,status=excluded.status,notes=excluded.notes,source_reference_id=excluded.source_reference_id,imported_at=excluded.imported_at''',
                       (project_name, client, start_date, end_date, deadline, status, notes, source_reference_id, now))
          if project_type:
            p = conn.execute('SELECT id FROM projects WHERE project_name=? AND client=?', (project_name, client)).fetchone()
            conn.execute('INSERT OR IGNORE INTO work_items(name,work_item_type_id,project_id,active) VALUES (?,?,?,1)', (project_name, project_type['id'], p['id']))
          result.imported_rows += 1
      elif import_type == 'osr_progress':
        for row_number, r in enumerate(_records(df), start=2):
          project_name = _clean(r.get('project_name'))
          p=conn.execute('SELECT id FROM projects WHERE project_name=?',(project_name,)).fetchone()
          if p:
            conn.execute('INSERT OR REPLACE INTO osr_progress(project_id,progress_date,percent_complete,actual_hours_to_date,imported_at) VALUES (?,?,?,?,?)',(p['id'],_date(r.get('progress_date')), _float(r.get('percent_complete')), _float(r.get('actual_hours_to_date')),now)); result.imported_rows+=1
          else:
            _error(result.validation_errors, row_number, 'project_name', f'Unknown project {project_name!r}; row skipped.', r); result.skipped_rows += 1
      elif import_type == 'people':
        for row_number, r in enumerate(_records(df), start=2):
          name = _clean(r.get('name'))
          discipline_code = _clean(r.get('discipline_code')).upper()
          if not name:
            _error(result.validation_errors, row_number, 'name', 'Name is required; row skipped.', r); result.skipped_rows += 1; continue
          d=conn.execute('SELECT id FROM disciplines WHERE code=?',(discipline_code,)).fetchone()
          if not d:
            _error(result.validation_errors, row_number, 'discipline_code', f'Unknown discipline {discipline_code!r}; row skipped.', r); result.skipped_rows += 1; continue
          conn.execute('''INSERT INTO people(name,discipline_id,daily_hours,weekly_hours,active)
                          VALUES (?,?,?,?,1)
                          ON CONFLICT(name) DO UPDATE SET discipline_id=excluded.discipline_id,daily_hours=excluded.daily_hours,weekly_hours=excluded.weekly_hours,active=1''',
                       (name,d['id'],_float(r.get('daily_hours'), 7.5),_float(r.get('weekly_hours'), 37.5)))
          result.imported_rows+=1
      elif import_type == 'planned_hours':
        for row_number, r in enumerate(_records(df), start=2):
          project_name = _clean(r.get('project_name')); discipline_code = _clean(r.get('discipline_code')).upper()
          p=conn.execute('SELECT id FROM projects WHERE project_name=?',(project_name,)).fetchone(); d=conn.execute('SELECT id FROM disciplines WHERE code=?',(discipline_code,)).fetchone()
          if p and d:
            conn.execute('INSERT OR REPLACE INTO project_discipline_budgets(project_id,discipline_id,planned_hours) VALUES (?,?,?)',(p['id'],d['id'],_float(r.get('planned_hours')))); result.imported_rows+=1
          else:
            _error(result.validation_errors, row_number, 'project_name/discipline_code', 'Unknown project or discipline; row skipped.', r); result.skipped_rows += 1
      elif import_type == 'annual_leave':
        for row_number, r in enumerate(_records(df), start=2):
          person_name = _clean(r.get('person_name'))
          p=conn.execute('SELECT id FROM people WHERE name=?',(person_name,)).fetchone()
          if p:
            conn.execute('INSERT INTO leave_records(person_id,start_date,end_date,hours_per_day,leave_type,notes) VALUES (?,?,?,?,?,?)',(p['id'],_date(r.get('start_date')),_date(r.get('end_date')),_float(r.get('hours_per_day')) if r.get('hours_per_day') is not None else None,_clean(r.get('leave_type'),'Annual leave') or 'Annual leave',_clean(r.get('notes')))); result.imported_rows+=1
          else:
            _error(result.validation_errors, row_number, 'person_name', f'Unknown person {person_name!r}; row skipped.', r); result.skipped_rows += 1
      elif import_type == 'public_holidays':
        for row_number, r in enumerate(_records(df), start=2):
          name = _clean(r.get('name'))
          if not name:
            _error(result.validation_errors, row_number, 'name', 'Holiday name is required; row skipped.', r); result.skipped_rows += 1; continue
          conn.execute('INSERT OR REPLACE INTO holiday_calendar(holiday_date,name,hours_removed) VALUES (?,?,?)',(_date(r.get('holiday_date')),name,_float(r.get('hours_removed')) if r.get('hours_removed') is not None else None)); result.imported_rows+=1
      write_audit(conn,user,'Import',None,import_type,None,{'imported_rows':result.imported_rows,'skipped_rows':result.skipped_rows,'validation_errors':len(result.validation_errors)},'CSV/XLSX import')
    return result
