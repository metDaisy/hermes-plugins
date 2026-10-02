"""Shared audit database path resolution for runtime and dashboard surfaces."""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path


DETAIL_RETENTION_DAYS = 30


def _git_root(candidates: list[Path]) -> Path | None:
    for candidate in candidates:
        if (candidate / ".git").exists():
            return candidate
    return None


def storage_root(module_file: str | Path) -> Path:
    """Resolve the repository/profile root used by the audit database."""
    module_path = Path(module_file).resolve()
    module_root = _git_root(list(module_path.parents))
    if module_root is not None:
        return module_root

    hermes_home = os.environ.get("HERMES_HOME")
    if hermes_home:
        return Path(hermes_home).resolve().parent

    cwd_root = _git_root([Path.cwd().resolve(), *Path.cwd().resolve().parents])
    if cwd_root is not None:
        return cwd_root

    return module_path.parents[3]


def database_path(module_file: str | Path) -> Path:
    """Return the shared privacy-safe SQLite audit database path."""
    return storage_root(module_file) / ".hermes" / "audit.db"


def apply_retention(
    connection: sqlite3.Connection,
    now: datetime | None = None,
    days: int = DETAIL_RETENTION_DAYS,
) -> None:
    """Expire details on complete UTC-day boundaries and retain daily counts."""
    now = now or datetime.now(timezone.utc)
    cleanup_date = now.date().isoformat()
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS audit_daily_rollups (
            date TEXT NOT NULL,
            project_name TEXT NOT NULL,
            profile_name TEXT NOT NULL,
            event_count INTEGER NOT NULL,
            succeeded_count INTEGER NOT NULL,
            failed_count INTEGER NOT NULL,
            session_count INTEGER NOT NULL,
            PRIMARY KEY (date, project_name, profile_name)
        );
        CREATE TABLE IF NOT EXISTS audit_retention_state (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS audit_daily_rollups_project_date_idx
            ON audit_daily_rollups(project_name, date DESC);
        """
    )
    previous = connection.execute(
        "SELECT value FROM audit_retention_state WHERE key = 'last_cleanup_date'"
    ).fetchone()
    if previous and previous[0] == cleanup_date:
        return

    cutoff_date = (now - timedelta(days=days)).date().isoformat()
    cutoff = f"{cutoff_date}T00:00:00+00:00"
    connection.execute(
        """
        INSERT INTO audit_daily_rollups (
            date, project_name, profile_name, event_count,
            succeeded_count, failed_count, session_count
        )
        SELECT
            substr(timestamp, 1, 10),
            COALESCE(project_name, ''),
            COALESCE(profile_name, ''),
            COUNT(*),
            SUM(CASE WHEN lower(COALESCE(status, '')) IN
                ('success', 'succeeded', 'passed', 'completed', 'ok', 'allow') THEN 1 ELSE 0 END),
            SUM(CASE WHEN lower(COALESCE(status, '')) IN
                ('failed', 'failure', 'error', 'blocked') THEN 1 ELSE 0 END),
            COUNT(DISTINCT CASE WHEN COALESCE(session_id, '') <> '' THEN session_id END)
        FROM audit_events
        WHERE timestamp < ?
        GROUP BY substr(timestamp, 1, 10), COALESCE(project_name, ''), COALESCE(profile_name, '')
        ON CONFLICT(date, project_name, profile_name) DO UPDATE SET
            event_count = event_count + excluded.event_count,
            succeeded_count = succeeded_count + excluded.succeeded_count,
            failed_count = failed_count + excluded.failed_count,
            session_count = session_count + excluded.session_count
        """,
        (cutoff,),
    )
    connection.execute("DELETE FROM audit_events WHERE timestamp < ?", (cutoff,))
    has_model_contexts = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'audit_model_contexts'"
    ).fetchone()
    if has_model_contexts:
        connection.execute("DELETE FROM audit_model_contexts WHERE timestamp < ?", (cutoff,))
    connection.execute(
        "INSERT INTO audit_retention_state(key, value) VALUES ('last_cleanup_date', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (cleanup_date,),
    )
    connection.commit()
