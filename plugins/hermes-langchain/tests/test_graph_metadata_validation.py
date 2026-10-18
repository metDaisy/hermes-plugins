"""Focused tests for graph wrapper metadata and rework lineage validation."""

from __future__ import annotations

import unittest

from tests.support import load_module


VALIDATION = load_module("authoring.graph.graph_metadata_validation")


class GraphMetadataValidationTests(unittest.TestCase):
    def test_valid_new_graph_metadata_returns_parsed_context(self) -> None:
        graph = {
            "schema": "build-task-graph-v6",
            "mode": "new",
            "issue": {"number": 151, "url": "https://example.com/issues/151"},
            "generation": 1,
            "workspace": "C:/workspace/project",
            "planning_baseline_sha": "a" * 40,
            "triage_digest": "b" * 64,
            "requirement_basis": {
                "path": "docs/requirements.md",
                "heading": "Requirement",
                "revision": "a" * 40,
            },
            "revised_requirement": None,
            "lineage": None,
            "source_review": None,
            "change_requirements": [],
            "behaviors": [],
            "cards": [],
            "execution_order": [],
            "ready_candidate": None,
            "archived_card_keys": [],
        }

        result = VALIDATION.validate_graph_metadata(graph)

        self.assertEqual((), result.errors)
        self.assertEqual("new", result.mode)
        self.assertEqual(151, result.issue_number)
        self.assertEqual("https://example.com/issues/151", result.issue_url)
        self.assertEqual(1, result.generation)
        self.assertEqual("C:/workspace/project", result.workspace)
        self.assertEqual("a" * 40, result.planning_baseline_sha)
        self.assertEqual("b" * 64, result.triage_digest)


if __name__ == "__main__":
    unittest.main()
