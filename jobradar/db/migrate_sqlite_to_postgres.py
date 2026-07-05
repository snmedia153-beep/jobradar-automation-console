from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from jobradar.db.repository import JobRadarRepository

TABLES_IN_ORDER = [
    "job_postings",
    "campaigns",
    "search_profiles",
    "alert_rules",
    "alert_events",
    "crawler_runs",
    "emulator_sessions",
    "audit_logs",
    "operation_commands",
    "device_slots",
    "proxy_profiles",
    "worker_jobs",
    "ocr_results",
]


def _sqlite_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()]


def _sqlite_table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
    return row is not None


def _reset_postgres_sequence(repo: JobRadarRepository, table: str) -> None:
    if not repo.is_postgres:
        return
    with repo.connect() as conn:
        conn.execute(
            f"""
            SELECT setval(
                pg_get_serial_sequence('{table}', 'id'),
                COALESCE((SELECT MAX(id) FROM {table}), 1),
                COALESCE((SELECT MAX(id) FROM {table}), 0) > 0
            ) AS seq
            """
        )


def migrate_sqlite_to_postgres(sqlite_path: str | Path, postgres_url: str, dry_run: bool = False) -> dict[str, Any]:
    """Copy existing local SQLite data into the configured Postgres database.

    The migration preserves primary-key ids where possible so worker/job/session
    references remain readable. Existing Postgres rows with the same id are kept.
    """
    sqlite_db = Path(sqlite_path)
    if not sqlite_db.exists():
        raise FileNotFoundError(f"SQLite DB not found: {sqlite_db}")
    target = JobRadarRepository(postgres_url)
    if not target.is_postgres:
        raise ValueError("postgres_url must start with postgresql:// or postgres://")
    target.init_db()

    summary: dict[str, Any] = {"sqlite": str(sqlite_db), "postgres": postgres_url, "tables": {}}
    src = sqlite3.connect(sqlite_db)
    src.row_factory = sqlite3.Row
    try:
        for table in TABLES_IN_ORDER:
            if not _sqlite_table_exists(src, table):
                summary["tables"][table] = {"copied": 0, "skipped": "missing source table"}
                continue
            columns = _sqlite_columns(src, table)
            if not columns:
                summary["tables"][table] = {"copied": 0, "skipped": "no columns"}
                continue
            rows = src.execute(f"SELECT {', '.join(columns)} FROM {table} ORDER BY id ASC").fetchall()
            if dry_run:
                summary["tables"][table] = {"copied": 0, "source_rows": len(rows), "dry_run": True}
                continue
            placeholders = ", ".join("?" for _ in columns)
            col_sql = ", ".join(columns)
            insert_sql = f"INSERT INTO {table} ({col_sql}) VALUES ({placeholders}) ON CONFLICT (id) DO NOTHING"
            copied = 0
            with target.connect() as dst:
                for row in rows:
                    cur = dst.execute(insert_sql, tuple(row[col] for col in columns))
                    copied += max(0, int(cur.rowcount or 0))
            _reset_postgres_sequence(target, table)
            summary["tables"][table] = {"copied": copied, "source_rows": len(rows)}
    finally:
        src.close()
    return summary
