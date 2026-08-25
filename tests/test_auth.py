import json
import os
from pathlib import Path

import pytest

from app.auth import AuthenticationConfigurationError, authenticate, hash_password, load_users, navigation_for_role
import app.data.db as db
from app.services.mvp import apply_quick_allocation, ensure_mvp_schema, save_projects


def configured_users():
    return {
        "admin@example.com": {
            "name": "Planner Admin", "password_hash": hash_password("correct"),
            "role": "admin",
        },
        "manager@example.com": {
            "name": "Planning Manager", "password_hash": hash_password("manager-pass"),
            "role": "manager", "active": True,
        },
        "inactive@example.com": {
            "name": "Inactive", "password_hash": hash_password("correct"),
            "role": "manager", "active": False,
        },
    }


def test_authentication_success_failures_role_and_inactive():
    users = load_users(json.dumps(configured_users()))
    user = authenticate("ADMIN@EXAMPLE.COM", "correct", users)
    assert user and user.role == "admin" and user.name == "Planner Admin"
    assert authenticate("admin@example.com", "wrong", users) is None
    assert authenticate("unknown@example.com", "correct", users) is None
    assert authenticate("inactive@example.com", "correct", users) is None


@pytest.mark.parametrize("raw", [None, "not-json", "{}", '{"a": {"name": "A"}}'])
def test_malformed_or_missing_configuration_fails_securely(monkeypatch, raw):
    monkeypatch.delenv("PLANNER_USERS_JSON", raising=False)
    with pytest.raises(AuthenticationConfigurationError):
        load_users(raw)


def test_role_navigation_hides_administration_from_managers():
    assert "Administration" in navigation_for_role("admin")
    assert "Administration" not in navigation_for_role("manager")


def test_database_path_default_and_environment_override(monkeypatch, tmp_path):
    old = db.DB_PATH
    try:
        monkeypatch.delenv("DATABASE_PATH", raising=False)
        db.DB_PATH = tmp_path / "local.sqlite"
        with db.connect() as conn:
            assert Path(conn.execute("PRAGMA database_list").fetchone()[2]) == db.DB_PATH
            assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
            assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
            assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        override = tmp_path / "render" / "planner.db"
        monkeypatch.setenv("DATABASE_PATH", str(override))
        with db.connect() as conn:
            assert Path(conn.execute("PRAGMA database_list").fetchone()[2]) == override
    finally:
        db.DB_PATH = old


def test_authenticated_identity_reaches_service_write_audit(monkeypatch, tmp_path):
    old = db.DB_PATH
    monkeypatch.delenv("DATABASE_PATH", raising=False)
    try:
        db.DB_PATH = tmp_path / "planner.sqlite"
        db.initialize_database(seed=False)
        ensure_mvp_schema()
        identity = "Planning Manager <manager@example.com>"
        save_projects([{"project_code": "AUTH", "project_name": "Identity", "client": "Example",
                        "project_manager": "PM", "priority": "P1", "rs_hours": 10,
                        "start_date": "2026-08-03", "end_date": "2026-08-31",
                        "rs_start_date": "2026-08-03", "status": "active"}], identity)
        apply_quick_allocation("AUTH", "RS", [__import__("datetime").date(2026, 8, 3)], [5], identity, "replace")
        assert {row["user_name"] for row in db.rows("SELECT user_name FROM audit_log")} == {identity}
    finally:
        db.DB_PATH = old
