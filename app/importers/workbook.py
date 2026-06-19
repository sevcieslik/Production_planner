from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from io import BytesIO
import json
import re
from zipfile import ZipFile
from xml.etree import ElementTree as ET

from app.data.db import connect, write_audit

try:  # pandas is only needed for Streamlit display helpers.
    import pandas as pd
except ModuleNotFoundError:  # pragma: no cover - supports audit/analyser in minimal envs.
    pd = None

DISCIPLINE_TOKENS = ('RS', 'GIS', 'PLS')
SOURCE_SHEETS = {'Projects', 'Roster Daily', 'Allocation Daily'}
VALIDATION_SHEETS = {'Teams Breakdown', 'Capacity'}
WORK_ITEM_TYPES = ('Project', 'QA', 'FLOW', 'Training', 'Admin', 'Leave', 'Unavailable', 'Other')

NS = {
    'a': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main',
    'r': 'http://schemas.openxmlformats.org/officeDocument/2006/relationships',
}

@dataclass
class SheetProfile:
    sheet_name: str
    rows: int
    columns: int
    likely_role: str
    detected_disciplines: str
    header_preview: str

@dataclass
class WorkbookStageResult:
    batch_id: int
    profiles: list[SheetProfile]
    summary: dict[str, int]
    validation_issues: list[dict]
    teams_reconciliation: list[dict]
    capacity_reconciliation: list[dict]


def _read_bytes(uploaded_file) -> bytes:
    if isinstance(uploaded_file, (str, bytes)):
        return open(uploaded_file, 'rb').read() if isinstance(uploaded_file, str) else uploaded_file
    return uploaded_file.getvalue() if hasattr(uploaded_file, 'getvalue') else uploaded_file.read()


def _col_to_num(col: str) -> int:
    n = 0
    for ch in col:
        n = n * 26 + ord(ch.upper()) - 64
    return n


def _num_to_col(n: int) -> str:
    out = ''
    while n:
        n, r = divmod(n - 1, 26)
        out = chr(65 + r) + out
    return out


def _cell_parts(coord: str) -> tuple[int, int]:
    m = re.match(r'([A-Z]+)(\d+)', coord)
    return int(m.group(2)), _col_to_num(m.group(1))


def _excel_date(value) -> str | None:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if not 20000 <= f <= 70000:
        return None
    return (datetime(1899, 12, 30) + timedelta(days=f)).date().isoformat()


def _as_float(value) -> float | None:
    if value in (None, ''):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _discipline(team_code: str | None) -> str | None:
    text = (team_code or '').upper()
    for token in DISCIPLINE_TOKENS:
        if token in text:
            return token
    return None


def _classify(sheet_name: str, headers: list[str]) -> str:
    if sheet_name in SOURCE_SHEETS:
        return {
            'Projects': 'Project source data',
            'Roster Daily': 'People / daily availability source',
            'Allocation Daily': 'Daily work item allocation source',
        }[sheet_name]
    if sheet_name in VALIDATION_SHEETS:
        return 'Calculated validation/reporting output'
    text = ' '.join([sheet_name, *headers]).lower()
    if 'osr' in text or 'actual' in text or 'progress' in text:
        return 'OSR actuals/progress source'
    if 'leave' in text or 'holiday' in text or 'absence' in text:
        return 'Availability reducer'
    return 'Reference / unknown'


class WorkbookReader:
    def __init__(self, data: bytes):
        self.z = ZipFile(BytesIO(data))
        self.shared = self._shared_strings()
        wb = ET.fromstring(self.z.read('xl/workbook.xml'))
        rels = ET.fromstring(self.z.read('xl/_rels/workbook.xml.rels'))
        relmap = {r.attrib['Id']: r.attrib['Target'] for r in rels}
        self.sheets = []
        for sh in wb.find('a:sheets', NS):
            target = relmap[sh.attrib[f'{{{NS["r"]}}}id']].lstrip('/')
            self.sheets.append((sh.attrib['name'], 'xl/' + target if not target.startswith('xl/') else target))

    def _shared_strings(self) -> list[str]:
        if 'xl/sharedStrings.xml' not in self.z.namelist():
            return []
        root = ET.fromstring(self.z.read('xl/sharedStrings.xml'))
        return [''.join(t.text or '' for t in si.iter(f'{{{NS["a"]}}}t')) for si in root.findall('a:si', NS)]

    def sheet_matrix(self, name: str) -> dict[tuple[int, int], object]:
        path = dict(self.sheets)[name]
        root = ET.fromstring(self.z.read(path))
        out = {}
        for c in root.findall('.//a:sheetData/a:row/a:c', NS):
            coord = c.attrib['r']
            row, col = _cell_parts(coord)
            f = c.find('a:f', NS)
            v = c.find('a:v', NS)
            if f is not None:
                out[(row, col)] = '=' + (f.text or '')
            elif v is not None:
                val = v.text
                if c.attrib.get('t') == 's':
                    val = self.shared[int(val)]
                out[(row, col)] = val
        return out

    def dimension(self, name: str) -> tuple[str, int, int, int]:
        path = dict(self.sheets)[name]
        root = ET.fromstring(self.z.read(path))
        ref = root.find('a:dimension', NS).attrib.get('ref', 'A1:A1')
        end = ref.split(':')[-1]
        rows, cols = _cell_parts(end)
        formulas = len(root.findall('.//a:f', NS))
        return ref, rows, cols, formulas


def profile_workbook(uploaded_file) -> list[SheetProfile]:
    reader = WorkbookReader(_read_bytes(uploaded_file))
    profiles = []
    for sheet_name, _ in reader.sheets:
        ref, rows, cols, _ = reader.dimension(sheet_name)
        matrix = reader.sheet_matrix(sheet_name)
        preview = [str(matrix[k]).strip() for k in sorted(matrix) if k[0] <= 8 and str(matrix[k]).strip()][:20]
        discs = ', '.join(d for d in DISCIPLINE_TOKENS if any(d.lower() in h.lower() for h in preview + [sheet_name]))
        profiles.append(SheetProfile(sheet_name, rows, cols, _classify(sheet_name, preview), discs, ' | '.join(preview[:12])))
    return profiles


def profiles_to_frame(profiles: list[SheetProfile]):
    data = [p.__dict__ for p in profiles]
    if pd is None:
        return data
    return pd.DataFrame(data)


def _date_headers(matrix: dict[tuple[int, int], object], row: int, start_col: int, max_col: int) -> dict[int, str]:
    headers = {}
    current = None
    for col in range(start_col, max_col + 1):
        raw = matrix.get((row, col))
        parsed = _excel_date(raw)
        if parsed:
            current = date.fromisoformat(parsed)
        elif isinstance(raw, str) and raw.startswith('=') and current:
            current += timedelta(days=1)
            while current.weekday() >= 5:
                current += timedelta(days=1)
        if current:
            headers[col] = current.isoformat()
    return headers


def _work_item_type(label: str, project_names: set[str]) -> str:
    normalized = label.strip().lower()
    if label in project_names:
        return 'Project'
    if normalized in {'qa', 'quality', 'quality assurance'}:
        return 'QA'
    if normalized == 'flow':
        return 'FLOW'
    if 'train' in normalized:
        return 'Training'
    if normalized in {'admin', 'administrative', 'meeting', 'meetings'}:
        return 'Admin'
    if normalized in {'leave', 'annual leave', 'holiday', 'absence', 'pto'}:
        return 'Leave'
    if normalized in {'unavailable', 'not available'}:
        return 'Unavailable'
    return 'Other'


def stage_workbook(uploaded_file, filename: str, user: str) -> WorkbookStageResult:
    data = _read_bytes(uploaded_file)
    reader = WorkbookReader(data)
    profiles = profile_workbook(data)
    now = datetime.utcnow().isoformat(timespec='seconds')
    summary = {'projects': 0, 'roster_days': 0, 'allocations': 0}
    with connect() as conn:
        cur = conn.execute('INSERT INTO workbook_import_batches(filename,uploaded_by,uploaded_at,status) VALUES (?,?,?,?)', (filename, user, now, 'staged'))
        batch_id = cur.lastrowid
        project_matrix = reader.sheet_matrix('Projects') if 'Projects' in dict(reader.sheets) else {}
        project_names = set()
        for row in range(3, 224):
            name = project_matrix.get((row, 2))
            if not name:
                continue
            project_names.add(str(name))
            conn.execute('''INSERT INTO workbook_staging_projects(batch_id,row_number,project_code,project_name,progress_percent,lead,rs_hours,gis_hours,pls_hours,production_total,start_date,end_date,pm_end_date,archived)
                            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)''', (
                batch_id, row, project_matrix.get((row, 1)), str(name), _as_float(project_matrix.get((row, 3))), project_matrix.get((row, 4)),
                _as_float(project_matrix.get((row, 11))), _as_float(project_matrix.get((row, 12))), _as_float(project_matrix.get((row, 13))),
                _as_float(project_matrix.get((row, 14))), _excel_date(project_matrix.get((row, 15))), _excel_date(project_matrix.get((row, 16))),
                _excel_date(project_matrix.get((row, 24))), int(_as_float(project_matrix.get((row, 25))) or 0)))
            summary['projects'] += 1
        roster = reader.sheet_matrix('Roster Daily') if 'Roster Daily' in dict(reader.sheets) else {}
        _, _, roster_cols, _ = reader.dimension('Roster Daily') if roster else ('', 0, 0, 0)
        roster_dates = _date_headers(roster, 1, 5, roster_cols)
        roster_hours = {}
        for row in range(2, 500):
            person = roster.get((row, 1))
            if not person:
                continue
            team = roster.get((row, 2)); disc = _discipline(str(team))
            for col, work_date in roster_dates.items():
                hours = _as_float(roster.get((row, col)))
                if hours is None:
                    continue
                roster_hours[(str(person), work_date)] = hours
                conn.execute('''INSERT INTO workbook_staging_roster(batch_id,row_number,person_name,team_code,discipline_code,primary_role,secondary_role,work_date,available_hours)
                                VALUES (?,?,?,?,?,?,?,?,?)''', (batch_id, row, str(person), team, disc, roster.get((row, 3)), roster.get((row, 4)), work_date, hours))
                summary['roster_days'] += 1
        allocations = reader.sheet_matrix('Allocation Daily') if 'Allocation Daily' in dict(reader.sheets) else {}
        _, _, alloc_cols, _ = reader.dimension('Allocation Daily') if allocations else ('', 0, 0, 0)
        alloc_dates = _date_headers(allocations, 1, 3, alloc_cols)
        for row in range(2, 500):
            person = allocations.get((row, 1))
            if not person:
                continue
            team = allocations.get((row, 2)); disc = _discipline(str(team))
            for col, allocation_date in alloc_dates.items():
                label = allocations.get((row, col))
                if label in (None, '') or str(label).startswith('='):
                    continue
                conn.execute('''INSERT INTO workbook_staging_allocations(batch_id,row_number,person_name,team_code,discipline_code,allocation_date,allocation_label,available_hours)
                                VALUES (?,?,?,?,?,?,?,?)''', (batch_id, row, str(person), team, disc, allocation_date, str(label), roster_hours.get((str(person), allocation_date))))
                summary['allocations'] += 1
        _validate_batch(conn, batch_id, project_names)
        teams = reconcile_teams_breakdown(conn, batch_id, reader)
        capacity = reconcile_capacity(conn, batch_id, reader)
        conn.execute('UPDATE workbook_import_batches SET summary_json=? WHERE id=?', (json.dumps(summary), batch_id))
        write_audit(conn, user, 'WorkbookImport', batch_id, 'stage', None, summary, 'Workbook staged and validated')
        issues = [dict(r) for r in conn.execute('SELECT * FROM workbook_validation_issues WHERE batch_id=? ORDER BY severity DESC, issue_type', (batch_id,)).fetchall()]
    return WorkbookStageResult(batch_id, profiles, summary, issues, teams, capacity)


def _validate_batch(conn, batch_id: int, project_names: set[str]) -> None:
    def issue(sev, typ, sheet, row, field, msg):
        conn.execute('INSERT INTO workbook_validation_issues(batch_id,severity,issue_type,sheet_name,row_number,field_name,message) VALUES (?,?,?,?,?,?,?)', (batch_id, sev, typ, sheet, row, field, msg))
    for r in conn.execute('SELECT DISTINCT person_name,row_number FROM workbook_staging_roster WHERE batch_id=?', (batch_id,)):
        if not r['person_name']:
            issue('Error', 'unknown_people', 'Roster Daily', r['row_number'], 'Name', 'Missing person name')
    for r in conn.execute('SELECT DISTINCT discipline_code,team_code,row_number FROM workbook_staging_roster WHERE batch_id=?', (batch_id,)):
        if not r['discipline_code']:
            issue('Error', 'missing_discipline_mapping', 'Roster Daily', r['row_number'], 'Team', f'Cannot map team {r["team_code"]} to RS/GIS/PLS')
    for r in conn.execute('SELECT DISTINCT allocation_label,row_number FROM workbook_staging_allocations WHERE batch_id=?', (batch_id,)):
        label = r['allocation_label']
        typ = _work_item_type(label, project_names)
        if typ == 'Other' and label not in project_names:
            issue('Warning', 'unknown_allocation_labels', 'Allocation Daily', r['row_number'], 'allocation_label', f'Label {label!r} will import as Other unless mapped')
    dups = conn.execute('''SELECT person_name, allocation_date, COUNT(*) c FROM workbook_staging_allocations WHERE batch_id=? GROUP BY person_name,allocation_date HAVING c>1''', (batch_id,)).fetchall()
    for r in dups:
        issue('Error', 'duplicate_allocations', 'Allocation Daily', None, 'allocation_date', f'{r["person_name"]} has {r["c"]} allocations on {r["allocation_date"]}')
    over = conn.execute('''SELECT person_name, allocation_date, SUM(COALESCE(available_hours,0)) allocated, MAX(COALESCE(available_hours,0)) available
                           FROM workbook_staging_allocations WHERE batch_id=? GROUP BY person_name,allocation_date HAVING allocated>available AND available>0''', (batch_id,)).fetchall()
    for r in over:
        issue('Warning', 'overallocated_people', 'Allocation Daily', None, 'available_hours', f'{r["person_name"]} has {r["allocated"]}h allocated vs {r["available"]}h available on {r["allocation_date"]}')


def commit_workbook_batch(batch_id: int, user: str) -> dict[str, int]:
    counts = {'projects': 0, 'people': 0, 'availability': 0, 'allocations': 0}
    now = datetime.utcnow().isoformat(timespec='seconds')
    with connect() as conn:
        conn.execute('DELETE FROM workbook_validation_issues WHERE batch_id=? AND severity="Info"', (batch_id,))
        for code in DISCIPLINE_TOKENS:
            conn.execute('INSERT OR IGNORE INTO disciplines(code,name) VALUES (?,?)', (code, code))
        disc = {r['code']: r['id'] for r in conn.execute('SELECT * FROM disciplines')}
        for r in conn.execute('SELECT DISTINCT team_code,discipline_code FROM workbook_staging_roster WHERE batch_id=? AND team_code IS NOT NULL', (batch_id,)):
            if r['discipline_code']:
                conn.execute('INSERT OR IGNORE INTO teams(name,discipline_id) VALUES (?,?)', (r['team_code'], disc[r['discipline_code']]))
        teams = {r['name']: r['id'] for r in conn.execute('SELECT * FROM teams')}
        project_type = conn.execute('SELECT id FROM work_item_types WHERE name="Project"').fetchone()['id']
        for r in conn.execute('SELECT * FROM workbook_staging_projects WHERE batch_id=?', (batch_id,)):
            status = 'Archived' if r['archived'] else 'Active'
            conn.execute('''INSERT INTO projects(project_name,client,start_date,end_date,deadline,status,notes,source_reference_id,imported_at)
                            VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(project_name,client) DO UPDATE SET start_date=excluded.start_date,end_date=excluded.end_date,deadline=excluded.deadline,status=excluded.status,source_reference_id=excluded.source_reference_id,imported_at=excluded.imported_at''',
                         (r['project_name'], 'Workbook', r['start_date'] or date.today().isoformat(), r['end_date'] or r['pm_end_date'] or date.today().isoformat(), r['pm_end_date'] or r['end_date'] or date.today().isoformat(), status, r['lead'], r['project_code'], now))
            p = conn.execute('SELECT id FROM projects WHERE project_name=? AND client=?', (r['project_name'], 'Workbook')).fetchone()
            conn.execute('INSERT OR IGNORE INTO work_items(name,work_item_type_id,project_id,active) VALUES (?,?,?,1)', (r['project_name'], project_type, p['id']))
            for code, hrs in [('RS', r['rs_hours']), ('GIS', r['gis_hours']), ('PLS', r['pls_hours'])]:
                if hrs is not None:
                    conn.execute('INSERT OR REPLACE INTO project_discipline_budgets(project_id,discipline_id,planned_hours) VALUES (?,?,?)', (p['id'], disc[code], hrs))
            counts['projects'] += 1
        for r in conn.execute('SELECT DISTINCT person_name,team_code,discipline_code FROM workbook_staging_roster WHERE batch_id=?', (batch_id,)):
            if r['person_name'] and r['discipline_code']:
                conn.execute('INSERT OR IGNORE INTO people(name,team_id,discipline_id,daily_hours,weekly_hours,active) VALUES (?,?,?,?,?,1)', (r['person_name'], teams.get(r['team_code']), disc[r['discipline_code']], 8, 40))
                counts['people'] += 1
        people = {r['name']: r['id'] for r in conn.execute('SELECT id,name FROM people')}
        for r in conn.execute('SELECT * FROM workbook_staging_roster WHERE batch_id=?', (batch_id,)):
            pid = people.get(r['person_name'])
            if pid and r['work_date']:
                conn.execute('INSERT OR REPLACE INTO availability_calendar(person_id,work_date,available_hours,source) VALUES (?,?,?,?)', (pid, r['work_date'], r['available_hours'], f'workbook:{batch_id}'))
                counts['availability'] += 1
        project_names = {r['project_name'] for r in conn.execute('SELECT project_name FROM projects')}
        for r in conn.execute('SELECT * FROM workbook_staging_allocations WHERE batch_id=?', (batch_id,)):
            pid = people.get(r['person_name']); label = r['allocation_label']
            if not pid or not label:
                continue
            typ = _work_item_type(label, project_names)
            type_id = conn.execute('SELECT id FROM work_item_types WHERE name=?', (typ,)).fetchone()['id']
            pr = conn.execute('SELECT id FROM projects WHERE project_name=?', (label,)).fetchone()
            wi = conn.execute('SELECT id FROM work_items WHERE name=?', (label,)).fetchone()
            if not wi:
                conn.execute('INSERT INTO work_items(name,work_item_type_id,project_id,active) VALUES (?,?,?,1)', (label, type_id, pr['id'] if pr else None))
                wi = conn.execute('SELECT id FROM work_items WHERE name=?', (label,)).fetchone()
            conn.execute('''INSERT OR REPLACE INTO daily_allocations(person_id,allocation_date,project_id,work_item_id,split_slot,allocated_hours,notes,source)
                            VALUES (?,?,?,?,?,?,?,?)''', (pid, r['allocation_date'], pr['id'] if pr else None, wi['id'], 1, r['available_hours'] or 0, 'Workbook import', f'workbook:{batch_id}'))
            counts['allocations'] += 1
        conn.execute('UPDATE workbook_import_batches SET status="committed" WHERE id=?', (batch_id,))
        write_audit(conn, user, 'WorkbookImport', batch_id, 'commit', None, counts, 'Workbook committed from staging')
    return counts


def effective_capacity(conn, person_id: int, available_hours: float) -> float:
    person = conn.execute('SELECT discipline_id, team_id FROM people WHERE id=?', (person_id,)).fetchone()
    checks = [('person', person_id), ('team', person['team_id'] if person else None), ('discipline', person['discipline_id'] if person else None), ('global', 0)]
    for scope, sid in checks:
        row = conn.execute('SELECT diminished_capacity_factor FROM capacity_settings WHERE scope_type=? AND (scope_id IS ? OR scope_id=?)', (scope, sid, sid)).fetchone()
        if row:
            return round((available_hours or 0) * row['diminished_capacity_factor'], 4)
    return round((available_hours or 0) * 0.85, 4)


def generated_teams_breakdown(conn, start: str | None = None, end: str | None = None) -> list[dict]:
    where = ''
    params = []
    if start and end:
        where = 'WHERE da.allocation_date BETWEEN ? AND ?'
        params = [start, end]
    data = []
    for r in conn.execute(f'''SELECT COALESCE(wi.name,p.project_name) work_item, d.code discipline, date(da.allocation_date, '-' || ((strftime('%w',da.allocation_date)+6)%7) || ' days') week_start,
                              SUM(da.allocated_hours) raw_hours, SUM(CASE WHEN wit.name='Project' THEN da.allocated_hours ELSE 0 END) project_hours
                              FROM daily_allocations da JOIN people pe ON pe.id=da.person_id JOIN disciplines d ON d.id=pe.discipline_id
                              LEFT JOIN work_items wi ON wi.id=da.work_item_id LEFT JOIN work_item_types wit ON wit.id=wi.work_item_type_id LEFT JOIN projects p ON p.id=da.project_id
                              {where} GROUP BY work_item,d.code,week_start''', params).fetchall():
        planned = conn.execute('''SELECT b.planned_hours FROM project_discipline_budgets b JOIN projects p ON p.id=b.project_id JOIN disciplines d ON d.id=b.discipline_id WHERE p.project_name=? AND d.code=?''', (r['work_item'], r['discipline'])).fetchone()
        data.append({'project': r['work_item'], 'discipline': r['discipline'], 'week_start': r['week_start'], 'weekly_assigned_hours': round((r['raw_hours'] or 0) * current_global_factor(conn), 2), 'planned_hours': planned['planned_hours'] if planned else 0, 'remaining_hours': round((planned['planned_hours'] if planned else 0) - ((r['raw_hours'] or 0) * current_global_factor(conn)), 2)})
    return data


def current_global_factor(conn) -> float:
    row = conn.execute('SELECT diminished_capacity_factor FROM capacity_settings WHERE scope_type="global" ORDER BY id LIMIT 1').fetchone()
    return float(row['diminished_capacity_factor']) if row else 0.85


def generated_capacity(conn) -> list[dict]:
    out = []
    for p in conn.execute('SELECT id,project_name FROM projects').fetchall():
        planned = conn.execute('SELECT COALESCE(SUM(planned_hours),0) v FROM project_discipline_budgets WHERE project_id=?', (p['id'],)).fetchone()['v'] or 0
        assigned = conn.execute('SELECT COALESCE(SUM(allocated_hours),0) v FROM daily_allocations WHERE project_id=?', (p['id'],)).fetchone()['v'] or 0
        assigned *= current_global_factor(conn)
        prog = conn.execute('SELECT percent_complete FROM osr_progress WHERE project_id=? ORDER BY progress_date DESC LIMIT 1', (p['id'],)).fetchone()
        remaining = max(planned - assigned, 0)
        out.append({'project': p['project_name'], 'planned_hours': round(planned, 2), 'assigned_hours': round(assigned, 2), 'remaining_hours': round(remaining, 2), 'effort_spent_percent': round((assigned / planned * 100) if planned else 0, 2), 'progress_percent': prog['percent_complete'] if prog else None, 'variance': round(assigned - planned, 2)})
    return out


def reconcile_teams_breakdown(conn, batch_id: int, reader: WorkbookReader | None = None) -> list[dict]:
    generated = generated_teams_breakdown(conn)
    generated_map = {(r['project'], r['discipline'], r['week_start']): r for r in generated}
    if reader is None or 'Teams Breakdown' not in dict(reader.sheets):
        return [{'status': 'generated_only', **r, 'variance_percent': None} for r in generated[:500]]
    matrix = reader.sheet_matrix('Teams Breakdown')
    _, rows, cols, _ = reader.dimension('Teams Breakdown')
    week_headers = _date_headers(matrix, 1, 7, cols)
    out = []
    seen = set()
    for row in range(2, rows + 1):
        project = matrix.get((row, 2))
        discipline = matrix.get((row, 3))
        if not project or not discipline:
            continue
        for col, week_start in week_headers.items():
            workbook_hours = _as_float(matrix.get((row, col)))
            if workbook_hours is None:
                continue
            key = (str(project), str(discipline).replace('1_', '').replace('2_', '').replace('3_', ''), week_start)
            gen = generated_map.get(key)
            seen.add(key)
            generated_hours = gen['weekly_assigned_hours'] if gen else 0
            variance = generated_hours - workbook_hours
            variance_pct = (variance / workbook_hours * 100) if workbook_hours else (0 if generated_hours == 0 else 100)
            out.append({'status': 'match' if abs(variance_pct) < 2 else 'mismatch', 'project': project, 'discipline': key[1], 'week_start': week_start, 'workbook_hours': round(workbook_hours, 2), 'generated_hours': round(generated_hours, 2), 'variance_percent': round(variance_pct, 2)})
            if len(out) >= 500:
                return out
    for key, gen in generated_map.items():
        if key not in seen and len(out) < 500:
            out.append({'status': 'missing_in_workbook', **gen, 'variance_percent': None})
    return out


def reconcile_capacity(conn, batch_id: int, reader: WorkbookReader | None = None) -> list[dict]:
    generated = generated_capacity(conn)
    generated_map = {r['project']: r for r in generated}
    if reader is None or 'Capacity' not in dict(reader.sheets):
        return [{'status': 'generated_only', **r, 'variance_percent': None} for r in generated[:500]]
    matrix = reader.sheet_matrix('Capacity')
    _, rows, _, _ = reader.dimension('Capacity')
    out = []
    seen = set()
    for row in range(2, rows + 1):
        project = matrix.get((row, 2))
        if not project:
            continue
        workbook_assigned = _as_float(matrix.get((row, 4))) or 0
        workbook_planned = _as_float(matrix.get((row, 5))) or 0
        workbook_remaining = _as_float(matrix.get((row, 6))) or 0
        gen = generated_map.get(str(project))
        seen.add(str(project))
        if not gen:
            out.append({'status': 'missing_generated', 'project': project, 'workbook_assigned': workbook_assigned, 'workbook_planned': workbook_planned, 'workbook_remaining': workbook_remaining, 'variance_percent': None})
        else:
            variance = gen['assigned_hours'] - workbook_assigned
            variance_pct = (variance / workbook_assigned * 100) if workbook_assigned else (0 if gen['assigned_hours'] == 0 else 100)
            out.append({'status': 'match' if abs(variance_pct) < 1 else 'mismatch', 'project': project, 'workbook_assigned': round(workbook_assigned, 2), 'generated_assigned': gen['assigned_hours'], 'workbook_planned': round(workbook_planned, 2), 'generated_planned': gen['planned_hours'], 'workbook_remaining': round(workbook_remaining, 2), 'generated_remaining': gen['remaining_hours'], 'variance_percent': round(variance_pct, 2)})
        if len(out) >= 500:
            return out
    for project, gen in generated_map.items():
        if project not in seen and len(out) < 500:
            out.append({'status': 'missing_in_workbook', **gen, 'variance_percent': None})
    return out
