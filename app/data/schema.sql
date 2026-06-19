PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS disciplines (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  code TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS teams (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE,
  discipline_id INTEGER REFERENCES disciplines(id)
);

CREATE TABLE IF NOT EXISTS people (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE,
  team_id INTEGER REFERENCES teams(id),
  discipline_id INTEGER NOT NULL REFERENCES disciplines(id),
  daily_hours REAL NOT NULL DEFAULT 7.5,
  weekly_hours REAL NOT NULL DEFAULT 37.5,
  active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS user_roles (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_name TEXT NOT NULL UNIQUE,
  role TEXT NOT NULL CHECK(role IN ('CDO','Manager','PM')),
  discipline_id INTEGER REFERENCES disciplines(id)
);

CREATE TABLE IF NOT EXISTS non_billable_categories (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE,
  category_type TEXT NOT NULL DEFAULT 'admin',
  consumes_capacity INTEGER NOT NULL DEFAULT 1,
  billable_project_work INTEGER NOT NULL DEFAULT 0,
  removes_availability INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS projects (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project_name TEXT NOT NULL,
  client TEXT NOT NULL,
  start_date TEXT NOT NULL,
  end_date TEXT NOT NULL,
  deadline TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'Active',
  notes TEXT,
  source_reference_id TEXT,
  imported_at TEXT,
  UNIQUE(project_name, client)
);

CREATE TABLE IF NOT EXISTS project_discipline_budgets (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  discipline_id INTEGER NOT NULL REFERENCES disciplines(id),
  planned_hours REAL NOT NULL DEFAULT 0,
  UNIQUE(project_id, discipline_id)
);

CREATE TABLE IF NOT EXISTS project_schedules (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  milestone TEXT NOT NULL,
  scheduled_date TEXT NOT NULL,
  notes TEXT
);

CREATE TABLE IF NOT EXISTS availability_calendar (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  person_id INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE,
  work_date TEXT NOT NULL,
  available_hours REAL NOT NULL,
  source TEXT NOT NULL DEFAULT 'default',
  UNIQUE(person_id, work_date)
);

CREATE TABLE IF NOT EXISTS holiday_calendar (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  holiday_date TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL,
  hours_removed REAL
);

CREATE TABLE IF NOT EXISTS leave_records (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  person_id INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE,
  start_date TEXT NOT NULL,
  end_date TEXT NOT NULL,
  hours_per_day REAL,
  leave_type TEXT NOT NULL DEFAULT 'Annual leave',
  notes TEXT
);

CREATE TABLE IF NOT EXISTS weekly_demand (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  discipline_id INTEGER NOT NULL REFERENCES disciplines(id),
  week_start TEXT NOT NULL,
  demand_hours REAL NOT NULL DEFAULT 0,
  source TEXT NOT NULL DEFAULT 'manual',
  UNIQUE(project_id, discipline_id, week_start)
);

CREATE TABLE IF NOT EXISTS daily_allocations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  person_id INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE,
  allocation_date TEXT NOT NULL,
  project_id INTEGER REFERENCES projects(id) ON DELETE SET NULL,
  category_id INTEGER REFERENCES non_billable_categories(id),
  split_slot INTEGER NOT NULL DEFAULT 1 CHECK(split_slot IN (1,2)),
  allocated_hours REAL NOT NULL,
  notes TEXT,
  source TEXT NOT NULL DEFAULT 'manual',
  UNIQUE(person_id, allocation_date, split_slot)
);

CREATE TABLE IF NOT EXISTS actual_hours (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  discipline_id INTEGER REFERENCES disciplines(id),
  work_date TEXT NOT NULL,
  hours REAL NOT NULL,
  source TEXT NOT NULL DEFAULT 'OSR',
  imported_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS osr_progress (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  progress_date TEXT NOT NULL,
  percent_complete REAL NOT NULL CHECK(percent_complete >= 0 AND percent_complete <= 100),
  actual_hours_to_date REAL NOT NULL DEFAULT 0,
  source TEXT NOT NULL DEFAULT 'OSR',
  imported_at TEXT NOT NULL,
  UNIQUE(project_id, progress_date)
);

CREATE TABLE IF NOT EXISTS forecasts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  forecast_date TEXT NOT NULL,
  planned_hours REAL NOT NULL,
  actual_hours_to_date REAL NOT NULL,
  percent_complete REAL NOT NULL,
  eac_hours REAL,
  variance_hours REAL,
  deadline_risk TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS alerts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  alert_date TEXT NOT NULL,
  severity TEXT NOT NULL CHECK(severity IN ('Info','Warning','Critical')),
  alert_type TEXT NOT NULL,
  object_type TEXT NOT NULL,
  object_id INTEGER,
  message TEXT NOT NULL,
  resolved INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS audit_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  timestamp TEXT NOT NULL,
  user_name TEXT NOT NULL,
  object_type TEXT NOT NULL,
  object_id INTEGER,
  action TEXT NOT NULL,
  previous_value TEXT,
  new_value TEXT,
  reason TEXT
);
