"""Environment-backed authentication and small role policy helpers."""
from __future__ import annotations

import json
import os
import base64
import hashlib
import hmac
from dataclasses import dataclass

class AuthenticationConfigurationError(ValueError):
    """Raised when authentication cannot be configured securely."""


@dataclass(frozen=True)
class AuthenticatedUser:
    email: str
    name: str
    role: str

    @property
    def audit_identity(self) -> str:
        return f"{self.name} <{self.email}>"


def load_users(raw: str | None = None) -> dict[str, dict]:
    """Parse and validate PLANNER_USERS_JSON without ever logging its contents."""
    value = os.getenv("PLANNER_USERS_JSON") if raw is None else raw
    if not value:
        raise AuthenticationConfigurationError("PLANNER_USERS_JSON is not configured.")
    try:
        users = json.loads(value)
    except (json.JSONDecodeError, TypeError) as exc:
        raise AuthenticationConfigurationError("PLANNER_USERS_JSON is not valid JSON.") from exc
    if not isinstance(users, dict) or not users:
        raise AuthenticationConfigurationError("PLANNER_USERS_JSON must contain at least one user.")
    for email, config in users.items():
        if not isinstance(email, str) or not isinstance(config, dict):
            raise AuthenticationConfigurationError("PLANNER_USERS_JSON has an invalid user entry.")
        if not all(isinstance(config.get(key), str) and config[key].strip() for key in ("name", "password_hash", "role")):
            raise AuthenticationConfigurationError("Every user requires name, password_hash, and role.")
        if config["role"].lower() not in {"admin", "manager"}:
            raise AuthenticationConfigurationError("Every user role must be admin or manager.")
        try:
            verify_password("configuration-check", config["password_hash"])
        except (ValueError, TypeError) as exc:
            raise AuthenticationConfigurationError("A configured password_hash is invalid.") from exc
    return {email.strip().lower(): config for email, config in users.items()}


def authenticate(email: str, password: str, users: dict[str, dict]) -> AuthenticatedUser | None:
    config = users.get(email.strip().lower())
    if not config or config.get("active", True) is not True:
        return None
    try:
        valid = verify_password(password, config["password_hash"])
    except (ValueError, TypeError):
        return None
    if not valid:
        return None
    return AuthenticatedUser(email=email.strip().lower(), name=config["name"].strip(), role=config["role"].lower())


def hash_password(password: str, *, iterations: int = 600_000) -> str:
    """Return a salted PBKDF2-SHA256 hash in a self-describing format."""
    if not password:
        raise ValueError("Password must not be empty.")
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return f"pbkdf2_sha256${iterations}${base64.b64encode(salt).decode()}${base64.b64encode(digest).decode()}"


def verify_password(password: str, encoded: str) -> bool:
    algorithm, iteration_text, salt_text, digest_text = encoded.split("$", 3)
    if algorithm != "pbkdf2_sha256":
        raise ValueError("Unsupported password hash")
    iterations = int(iteration_text)
    if iterations < 100_000:
        raise ValueError("Password hash iteration count is too low")
    salt = base64.b64decode(salt_text, validate=True)
    expected = base64.b64decode(digest_text, validate=True)
    actual = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return hmac.compare_digest(actual, expected)


def navigation_for_role(role: str) -> list[str]:
    tabs = ["Projects", "Planning", "Principles", "Resource Management"]
    if role == "admin":
        tabs.append("Administration")
    return tabs
