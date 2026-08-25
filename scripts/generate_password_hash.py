"""Interactively generate a PBKDF2-SHA256 hash for PLANNER_USERS_JSON."""
from getpass import getpass
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.auth import hash_password


def main() -> None:
    password = getpass("Password: ")
    confirmation = getpass("Confirm password: ")
    if not password:
        raise SystemExit("Password must not be empty.")
    if password != confirmation:
        raise SystemExit("Passwords do not match.")
    print(hash_password(password))


if __name__ == "__main__":
    main()
