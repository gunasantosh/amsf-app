import os
import sqlite3
from datetime import datetime
from pathlib import Path

from sqlalchemy.engine import make_url

import settings  # noqa: F401

REQUIRED_TABLES = {
    "audit_events",
    "contributions",
    "loan_repayments",
    "loan_votes",
    "loans",
    "members",
    "notification_logs",
    "password_reset_tokens",
    "reminder_dispatch_logs",
}
REQUIRED_COLUMNS = {
    "contributions": {"reviewed_at", "reviewed_by_id"},
    "loan_repayments": {"reviewed_at", "reviewed_by_id"},
}


def database_path() -> Path:
    url = make_url(os.getenv("AMSF_DATABASE_URL", "sqlite:///./amsf.db"))
    if url.get_backend_name() != "sqlite" or not url.database or url.database == ":memory:":
        raise RuntimeError("migrate_db.py requires a file-backed SQLite AMSF_DATABASE_URL.")
    return Path(url.database).expanduser().resolve()


def assert_integrity(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        result = connection.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        connection.close()
    if result != "ok":
        raise RuntimeError(f"SQLite integrity check failed for {path}: {result}")


def create_backup(source: Path) -> Path:
    backup_dir = Path(os.getenv("AMSF_BACKUP_DIR", str(source.parent / "backups"))).expanduser().resolve()
    backup_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    destination = backup_dir / f"{source.stem}-{timestamp}.db"

    source_connection = sqlite3.connect(source)
    destination_connection = sqlite3.connect(destination)
    try:
        source_connection.backup(destination_connection)
    finally:
        destination_connection.close()
        source_connection.close()
    assert_integrity(destination)
    return destination


def assert_expected_schema(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        missing_tables = REQUIRED_TABLES - tables
        if missing_tables:
            raise RuntimeError(f"Migration did not create expected tables: {sorted(missing_tables)}")
        for table, expected_columns in REQUIRED_COLUMNS.items():
            actual_columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
            missing_columns = expected_columns - actual_columns
            if missing_columns:
                raise RuntimeError(f"Migration did not add {table} columns: {sorted(missing_columns)}")
    finally:
        connection.close()


def main() -> None:
    path = database_path()
    if not path.exists():
        raise RuntimeError(f"Production database was not found: {path}")

    print(f"Checking SQLite integrity: {path}")
    assert_integrity(path)
    backup_path = create_backup(path)
    print(f"Backup created: {backup_path}")

    from database import init_db

    print("Applying AMSF schema migration...")
    init_db()
    assert_integrity(path)
    assert_expected_schema(path)
    print("Migration completed successfully.")


if __name__ == "__main__":
    main()
