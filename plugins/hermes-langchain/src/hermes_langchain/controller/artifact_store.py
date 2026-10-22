"""Immutable SQLite Artifact Store for accepted workflow outputs."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .contracts import _freeze
from .executors import EXECUTOR_KINDS

_HEX_DIGEST_LENGTH = 64


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def canonical_json_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


class ArtifactStoreError(ValueError):
    """Stable fail-closed Artifact Store error."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ArtifactRecord:
    artifact_id: str
    run_id: str
    step_id: str
    attempt: int
    executor_kind: str
    input_sha256: str
    output_sha256: str
    timeout_seconds: int
    validation_result: str
    executor_identity: str
    payload: dict[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", _freeze(dict(self.payload)))


class SQLiteArtifactStore:
    """Append-only Artifact Store with exact-write idempotency."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS controller_artifacts (
                artifact_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                step_id TEXT NOT NULL,
                attempt INTEGER NOT NULL,
                executor_kind TEXT NOT NULL,
                input_sha256 TEXT NOT NULL,
                output_sha256 TEXT NOT NULL,
                timeout_seconds INTEGER NOT NULL,
                validation_result TEXT NOT NULL,
                executor_identity TEXT NOT NULL,
                payload_json TEXT NOT NULL
            )
            """
        )
        self._connection.commit()

    @classmethod
    def open(cls, path: Path) -> "SQLiteArtifactStore":
        path.parent.mkdir(parents=True, exist_ok=True)
        return cls(sqlite3.connect(str(path), check_same_thread=False))

    def put(self, record: ArtifactRecord, *, input_value: Any) -> ArtifactRecord:
        self._validate(record, input_value)
        existing = self._find(record.artifact_id)
        if existing is not None:
            if existing == record:
                return existing
            raise self._conflict(record.artifact_id)

        try:
            self._connection.execute(
                """
                INSERT INTO controller_artifacts (
                    artifact_id,
                    run_id,
                    step_id,
                    attempt,
                    executor_kind,
                    input_sha256,
                    output_sha256,
                    timeout_seconds,
                    validation_result,
                    executor_identity,
                    payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.artifact_id,
                    record.run_id,
                    record.step_id,
                    record.attempt,
                    record.executor_kind,
                    record.input_sha256,
                    record.output_sha256,
                    record.timeout_seconds,
                    record.validation_result,
                    record.executor_identity,
                    canonical_json(record.payload),
                ),
            )
            self._connection.commit()
        except sqlite3.IntegrityError:
            concurrent = self._find(record.artifact_id)
            if concurrent == record:
                return concurrent
            raise self._conflict(record.artifact_id) from None
        return record

    def get(self, artifact_id: str) -> ArtifactRecord:
        record = self._find(artifact_id)
        if record is None:
            raise ArtifactStoreError(
                "ARTIFACT_NOT_FOUND",
                f"artifact not found: {artifact_id}",
            )
        self._validate_read(record)
        return record

    def close(self) -> None:
        self._connection.close()

    def _find(self, artifact_id: str) -> ArtifactRecord | None:
        row = self._connection.execute(
            """
            SELECT artifact_id,
                   run_id,
                   step_id,
                   attempt,
                   executor_kind,
                   input_sha256,
                   output_sha256,
                   timeout_seconds,
                   validation_result,
                   executor_identity,
                   payload_json
              FROM controller_artifacts
             WHERE artifact_id = ?
            """,
            (artifact_id,),
        ).fetchone()
        if row is None:
            return None
        try:
            payload = json.loads(str(row["payload_json"]))
        except (TypeError, ValueError) as exc:
            raise ArtifactStoreError(
                "INVALID_OUTPUT_DIGEST",
                "stored artifact payload is not valid JSON",
            ) from exc
        if not isinstance(payload, dict):
            raise ArtifactStoreError(
                "INVALID_OUTPUT_DIGEST",
                "stored artifact payload must be a JSON object",
            )
        if (
            not self._is_sha256(str(row["output_sha256"]))
            or str(row["output_sha256"]) != canonical_json_sha256(payload)
        ):
            raise ArtifactStoreError(
                "INVALID_OUTPUT_DIGEST",
                "stored artifact JSON does not match its digest",
            )
        return ArtifactRecord(
            artifact_id=str(row["artifact_id"]),
            run_id=str(row["run_id"]),
            step_id=str(row["step_id"]),
            attempt=int(row["attempt"]),
            executor_kind=str(row["executor_kind"]),
            input_sha256=str(row["input_sha256"]),
            output_sha256=str(row["output_sha256"]),
            timeout_seconds=int(row["timeout_seconds"]),
            validation_result=str(row["validation_result"]),
            executor_identity=str(row["executor_identity"]),
            payload=payload,
        )

    @staticmethod
    def _validate(record: ArtifactRecord, input_value: Any) -> None:
        if type(record.attempt) is not int or record.attempt < 1:
            raise ArtifactStoreError(
                "INVALID_ATTEMPT",
                "artifact attempt must be a positive integer",
            )
        if type(record.timeout_seconds) is not int or record.timeout_seconds < 1:
            raise ArtifactStoreError(
                "INVALID_TIMEOUT",
                "artifact timeout budget must be a positive integer",
            )
        if record.executor_kind not in EXECUTOR_KINDS:
            raise ArtifactStoreError(
                "UNKNOWN_EXECUTOR_KIND",
                f"unknown artifact executor kind: {record.executor_kind}",
            )
        if record.validation_result != "accepted":
            raise ArtifactStoreError(
                "UNACCEPTED_ARTIFACT",
                "only accepted outputs may be persisted as artifacts",
            )
        try:
            expected_input_sha256 = canonical_json_sha256(input_value)
        except (TypeError, ValueError) as exc:
            raise ArtifactStoreError(
                "INVALID_INPUT_DIGEST",
                "artifact input is not canonical JSON",
            ) from exc
        if (
            not SQLiteArtifactStore._is_sha256(record.input_sha256)
            or record.input_sha256 != expected_input_sha256
        ):
            raise ArtifactStoreError(
                "INVALID_INPUT_DIGEST",
                "artifact input digest does not match its canonical input",
            )
        try:
            expected_output_sha256 = canonical_json_sha256(record.payload)
        except (TypeError, ValueError) as exc:
            raise ArtifactStoreError(
                "INVALID_OUTPUT_DIGEST",
                "artifact payload is not canonical JSON",
            ) from exc
        if record.output_sha256 != expected_output_sha256:
            raise ArtifactStoreError(
                "INVALID_OUTPUT_DIGEST",
                "artifact output digest does not match its canonical payload",
            )

    @staticmethod
    def _conflict(artifact_id: str) -> ArtifactStoreError:
        return ArtifactStoreError(
            "ARTIFACT_CONFLICT",
            f"artifact id already stores different content: {artifact_id}",
        )

    @staticmethod
    def _validate_read(record: ArtifactRecord) -> None:
        try:
            expected_output_sha256 = canonical_json_sha256(record.payload)
        except (TypeError, ValueError) as exc:
            raise ArtifactStoreError(
                "INVALID_OUTPUT_DIGEST",
                "stored artifact payload is not canonical JSON",
            ) from exc
        if (
            not SQLiteArtifactStore._is_sha256(record.output_sha256)
            or record.output_sha256 != expected_output_sha256
        ):
            raise ArtifactStoreError(
                "INVALID_OUTPUT_DIGEST",
                "stored artifact payload does not match its digest",
            )

    @staticmethod
    def _is_sha256(value: str) -> bool:
        return len(value) == _HEX_DIGEST_LENGTH and all(
            character in "0123456789abcdef" for character in value
        )
