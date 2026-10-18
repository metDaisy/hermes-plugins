"""Focused tests for graph activation, native status, and archive validation."""

from __future__ import annotations

import unittest

from tests.support import load_module


VALIDATION = load_module("authoring.graph.graph_state_validation")


class GraphStateValidationTests(unittest.TestCase):
    def test_draft_activation_and_archive_errors_preserve_order(self) -> None:
        graph = {
            "mode": "new",
            "ready_candidate": "impl-1",
            "archived_card_keys": None,
        }
        records = {
            "triage": {
                "card_type": "triage",
                "status": "todo",
                "parents": [],
            },
            "impl-1": {
                "card_type": "implementation",
                "status": "ready",
                "parents": [],
            },
            "summary": {
                "card_type": "summary",
                "status": "todo",
                "parents": ["impl-1"],
            },
        }

        errors = VALIDATION.validate_graph_state(
            graph,
            records,
            triages=("triage",),
            summaries=("summary",),
            planned_impl_keys={"impl-1"},
            mode="new",
            phase="draft",
        )

        self.assertEqual(
            (
                "READY_CANDIDATE_PARENT_SET",
                "DRAFT_HAS_READY_CARD",
                "DRAFT_TRIAGE_STATUS",
                "DRAFT_ACTIVE_STATUS:impl-1",
                "INVALID_ARCHIVED_CARD_KEYS",
            ),
            errors,
        )


if __name__ == "__main__":
    unittest.main()
