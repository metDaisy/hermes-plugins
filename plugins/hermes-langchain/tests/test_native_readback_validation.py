"""Tests for extracted native Kanban read-back validation."""

from __future__ import annotations

import unittest

from tests.support import load_module


NATIVE = load_module("authoring.graph.native_readback_validation")
CONTRACT = load_module("authoring.graph.graph_contract")


class NativeReadbackValidationTests(unittest.TestCase):
    def test_public_facade_delegates_to_extracted_validator(self) -> None:
        graph = {
            "cards": [
                {
                    "key": "triage",
                    "title": "Triage",
                    "card_type": "triage",
                    "assignee": "project-manager",
                    "status": "done",
                    "priority": 0,
                    "skills": ["triage"],
                    "body": None,
                    "parents": [],
                }
            ]
        }
        readback = [
            {
                "task": {
                    "id": "task-1",
                    "title": "Triage",
                    "assignee": "project-manager",
                    "status": "done",
                    "priority": 0,
                    "skills": ["triage"],
                    "body": "persisted triage contract",
                },
                "parents": [],
            }
        ]
        self.assertEqual([], NATIVE.validate_native_readback(graph, readback))
        self.assertEqual([], CONTRACT.validate_native_readback(graph, readback))

    def test_body_parse_errors_preserve_order(self) -> None:
        graph = {
            "cards": [
                {
                    "key": "review-1",
                    "title": "Review 1",
                    "card_type": "review",
                    "assignee": "reviewer",
                    "status": "ready",
                    "priority": 0,
                    "skills": ["code-review"],
                    "body": {"schema": "aggregate-review-card-v5"},
                    "parents": [],
                }
            ]
        }
        readback = [
            {
                "task": {
                    "id": "task-1",
                    "title": "Review 1",
                    "assignee": "reviewer",
                    "status": "ready",
                    "priority": 0,
                    "skills": ["code-review"],
                    "body": "not-json",
                },
                "parents": [],
            }
        ]
        self.assertEqual(
            ["NATIVE_BODY_NOT_JSON:review-1", "NATIVE_BODY_MISMATCH:review-1"],
            NATIVE.validate_native_readback(graph, readback),
        )


if __name__ == "__main__":
    unittest.main()
