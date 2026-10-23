"""Immutable controller Artifact Store contract tests."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from tests.support import load_module

CONTROLLER = load_module("controller")
INPUT_VALUE = {"request": "Build it"}


def accepted_record(
    *,
    artifact_id: str = "run-store:extract:1",
    payload: dict[str, object] | None = None,
) -> object:
    output_value = payload or {"requirement": "Use an immutable store."}
    return CONTROLLER.ArtifactRecord(
        artifact_id=artifact_id,
        run_id="run-store",
        step_id="extract",
        attempt=1,
        executor_kind="agent",
        input_sha256=CONTROLLER.canonical_json_sha256(INPUT_VALUE),
        output_sha256=CONTROLLER.canonical_json_sha256(output_value),
        timeout_seconds=300,
        validation_result="accepted",
        payload=output_value,
        executor_identity="agent-extract",
    )


class ControllerArtifactStoreTests(unittest.TestCase):
    def test_rejects_payload_tampering_during_read(self) -> None:
        record = accepted_record()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "artifacts.sqlite3"
            store = CONTROLLER.SQLiteArtifactStore.open(path)
            try:
                store.put(record, input_value=INPUT_VALUE)
            finally:
                store.close()

            connection = sqlite3.connect(path)
            try:
                connection.execute(
                    "UPDATE controller_artifacts SET payload_json = ? WHERE artifact_id = ?",
                    ('{"requirement":"tampered"}', record.artifact_id),
                )
                connection.commit()
            finally:
                connection.close()

            reopened = CONTROLLER.SQLiteArtifactStore.open(path)
            try:
                with self.assertRaises(CONTROLLER.ArtifactStoreError) as raised:
                    reopened.get(record.artifact_id)
            finally:
                reopened.close()

            connection = sqlite3.connect(path)
            try:
                connection.execute(
                    "UPDATE controller_artifacts SET payload_json = ? WHERE artifact_id = ?",
                    ("{", record.artifact_id),
                )
                connection.commit()
            finally:
                connection.close()

            malformed_store = CONTROLLER.SQLiteArtifactStore.open(path)
            try:
                with self.assertRaises(CONTROLLER.ArtifactStoreError) as malformed:
                    malformed_store.get(record.artifact_id)
            finally:
                malformed_store.close()

            connection = sqlite3.connect(path)
            try:
                connection.execute(
                    "UPDATE controller_artifacts SET payload_json = ? WHERE artifact_id = ?",
                    (
                        '[["requirement","Use an immutable store."]] ',
                        record.artifact_id,
                    ),
                )
                connection.commit()
            finally:
                connection.close()

            wrong_shape_store = CONTROLLER.SQLiteArtifactStore.open(path)
            try:
                with self.assertRaises(CONTROLLER.ArtifactStoreError) as wrong_shape:
                    wrong_shape_store.get(record.artifact_id)
            finally:
                wrong_shape_store.close()

        self.assertEqual(raised.exception.code, "INVALID_OUTPUT_DIGEST")
        self.assertEqual(malformed.exception.code, "INVALID_OUTPUT_DIGEST")
        self.assertEqual(wrong_shape.exception.code, "INVALID_OUTPUT_DIGEST")

    def test_persists_and_reopens_immutable_accepted_artifact(self) -> None:
        mutable_payload = {
            "requirement": "Use an immutable store.",
            "evidence": ["contract", "digest"],
        }
        record = accepted_record(payload=mutable_payload)
        mutable_payload["evidence"].append("must not leak")

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "artifacts.sqlite3"
            store = CONTROLLER.SQLiteArtifactStore.open(path)
            try:
                stored = store.put(record, input_value=INPUT_VALUE)
            finally:
                store.close()

            reopened = CONTROLLER.SQLiteArtifactStore.open(path)
            try:
                loaded = reopened.get(record.artifact_id)
            finally:
                reopened.close()

        self.assertEqual(stored, record)
        self.assertEqual(loaded, record)
        self.assertEqual(loaded.executor_identity, "agent-extract")
        self.assertEqual(loaded.payload["evidence"], ("contract", "digest"))
        with self.assertRaises(TypeError):
            loaded.payload["requirement"] = "mutated"

    def test_rejects_non_immutable_or_unaccepted_artifact_writes(self) -> None:
        base = accepted_record()
        with tempfile.TemporaryDirectory() as tmp:
            store = CONTROLLER.SQLiteArtifactStore.open(Path(tmp) / "artifacts.sqlite3")
            try:
                self.assertEqual(store.put(base, input_value=INPUT_VALUE), base)
                self.assertEqual(store.put(base, input_value=INPUT_VALUE), base)

                conflicting_payload = {"requirement": "Conflicting output."}
                conflict = replace(
                    base,
                    payload=conflicting_payload,
                    output_sha256=CONTROLLER.canonical_json_sha256(conflicting_payload),
                )
                self._assert_store_error(store, conflict, "ARTIFACT_CONFLICT")

                invalid_records = (
                    (
                        replace(
                            base,
                            artifact_id="bad-input",
                            input_sha256="0" * 64,
                        ),
                        "INVALID_INPUT_DIGEST",
                    ),
                    (replace(base, artifact_id="bad-output", output_sha256="0" * 64), "INVALID_OUTPUT_DIGEST"),
                    (replace(base, artifact_id="bad-attempt", attempt=0), "INVALID_ATTEMPT"),
                    (replace(base, artifact_id="float-attempt", attempt=1.5), "INVALID_ATTEMPT"),
                    (replace(base, artifact_id="bool-attempt", attempt=True), "INVALID_ATTEMPT"),
                    (replace(base, artifact_id="string-attempt", attempt="1"), "INVALID_ATTEMPT"),
                    (replace(base, artifact_id="bad-timeout", timeout_seconds=0), "INVALID_TIMEOUT"),
                    (replace(base, artifact_id="float-timeout", timeout_seconds=2.5), "INVALID_TIMEOUT"),
                    (replace(base, artifact_id="bool-timeout", timeout_seconds=True), "INVALID_TIMEOUT"),
                    (replace(base, artifact_id="string-timeout", timeout_seconds="2"), "INVALID_TIMEOUT"),
                    (
                        replace(
                            base,
                            artifact_id="rejected",
                            validation_result="rejected",
                        ),
                        "UNACCEPTED_ARTIFACT",
                    ),
                )
                for record, code in invalid_records:
                    with self.subTest(code=code):
                        self._assert_store_error(store, record, code)

                self.assertEqual(store.get(base.artifact_id), base)
            finally:
                store.close()

    def _assert_store_error(self, store: object, record: object, code: str) -> None:
        with self.assertRaises(CONTROLLER.ArtifactStoreError) as raised:
            getattr(store, "put")(record, input_value=INPUT_VALUE)
        self.assertEqual(raised.exception.code, code)


if __name__ == "__main__":
    unittest.main()
