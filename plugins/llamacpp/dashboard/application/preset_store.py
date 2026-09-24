"""SQLite-backed shared preset storage for the llama.cpp manager."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from threading import RLock
from typing import Any


class PresetStore:
    """Own the one machine-scoped source of truth for presets and assignments."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = RLock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._path, timeout=5)
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    @contextmanager
    def _connection(self):
        connection = self._connect()
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def _initialize(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS parameter_presets (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    options_json TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS model_presets (
                    model_id TEXT PRIMARY KEY,
                    preset_id TEXT NOT NULL REFERENCES parameter_presets(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )

    def migrate_legacy_state(self, state: dict[str, Any]) -> bool:
        """Import legacy JSON once, then make SQLite the sole preset authority."""
        if "parameter_presets" not in state and "model_presets" not in state:
            return False
        with self._lock, self._connection() as connection:
            connection.execute("DELETE FROM model_presets")
            connection.execute("DELETE FROM parameter_presets")
            raw_presets = state.get("parameter_presets")
            if isinstance(raw_presets, dict):
                for preset_id, value in raw_presets.items():
                    if not isinstance(value, dict):
                        continue
                    options = value.get("options")
                    if not isinstance(options, dict):
                        options = {}
                    connection.execute(
                        """
                        INSERT INTO parameter_presets(id, name, options_json, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            str(preset_id), str(value.get("name") or preset_id),
                            json.dumps(options, ensure_ascii=False, sort_keys=True),
                            float(value.get("created_at") or 0), float(value.get("updated_at") or value.get("created_at") or 0),
                        ),
                    )
            raw_assignments = state.get("model_presets")
            if isinstance(raw_assignments, dict):
                for model_id, preset_id in raw_assignments.items():
                    exists = connection.execute(
                        "SELECT 1 FROM parameter_presets WHERE id = ?", (str(preset_id),)
                    ).fetchone()
                    if exists:
                        connection.execute(
                            "INSERT OR REPLACE INTO model_presets(model_id, preset_id) VALUES (?, ?)",
                            (str(model_id), str(preset_id)),
                        )
            connection.execute(
                "INSERT OR REPLACE INTO metadata(key, value) VALUES ('legacy-json-migrated-v1', '1')"
            )
            return True

    def presets(self) -> dict[str, dict[str, Any]]:
        with self._lock, self._connection() as connection:
            rows = connection.execute(
                "SELECT id, name, options_json, created_at, updated_at FROM parameter_presets"
            ).fetchall()
        return {
            str(preset_id): {
                "name": str(name), "model_id": None,
                "options": dict(json.loads(options_json)),
                "created_at": created_at, "updated_at": updated_at,
            }
            for preset_id, name, options_json, created_at, updated_at in rows
        }

    def get(self, preset_id: str) -> dict[str, Any] | None:
        return self.presets().get(str(preset_id))

    def create(self, preset_id: str, name: str, options: dict[str, str], now: float) -> dict[str, Any]:
        with self._lock, self._connection() as connection:
            connection.execute(
                "INSERT INTO parameter_presets(id, name, options_json, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                (preset_id, name, json.dumps(options, ensure_ascii=False, sort_keys=True), now, now),
            )
        return self.get(preset_id) or {}

    def update(self, preset_id: str, name: str | None, options: dict[str, str] | None, now: float) -> dict[str, Any] | None:
        current = self.get(preset_id)
        if current is None:
            return None
        next_name = name if name is not None else str(current["name"])
        next_options = options if options is not None else dict(current["options"])
        with self._lock, self._connection() as connection:
            connection.execute(
                "UPDATE parameter_presets SET name = ?, options_json = ?, updated_at = ? WHERE id = ?",
                (next_name, json.dumps(next_options, ensure_ascii=False, sort_keys=True), now, preset_id),
            )
        return self.get(preset_id)

    def delete(self, preset_id: str) -> bool:
        with self._lock, self._connection() as connection:
            cursor = connection.execute("DELETE FROM parameter_presets WHERE id = ?", (preset_id,))
            return cursor.rowcount == 1

    def assign(self, model_id: str, preset_id: str) -> None:
        with self._lock, self._connection() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO model_presets(model_id, preset_id) VALUES (?, ?)",
                (model_id, preset_id),
            )

    def model_preset_map(self) -> dict[str, str]:
        with self._lock, self._connection() as connection:
            rows = connection.execute("SELECT model_id, preset_id FROM model_presets").fetchall()
        return {str(model_id): str(preset_id) for model_id, preset_id in rows}
