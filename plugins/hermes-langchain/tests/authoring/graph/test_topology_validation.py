"""Focused tests for build-task graph topology validation."""

from __future__ import annotations

import unittest

from tests.support import load_module


TOPOLOGY = load_module("authoring.graph.topology_validation")


class TopologyValidationTests(unittest.TestCase):
    def test_valid_topology_returns_card_role_indexes(self) -> None:
        records = {
            "triage": {
                "card_type": "triage",
                "status": "triage",
                "priority": 0,
                "parents": [],
            },
            "impl": {
                "card_type": "implementation",
                "status": "todo",
                "priority": 2,
                "parents": ["triage"],
            },
            "review": {
                "card_type": "review",
                "status": "todo",
                "priority": 1,
                "parents": ["impl"],
            },
            "summary": {
                "card_type": "summary",
                "status": "todo",
                "priority": 0,
                "parents": ["impl", "review"],
            },
        }
        graph = {
            "execution_order": ["impl", "review"],
            "ready_candidate": "impl",
        }

        result = TOPOLOGY.validate_card_topology(graph, records, mode="new")

        self.assertEqual((), result.errors)
        self.assertEqual(("triage",), result.triages)
        self.assertEqual(("summary",), result.summaries)
        self.assertEqual(("review",), result.reviews)

    def test_parent_cycle_and_role_errors_preserve_contract_order(self) -> None:
        records = {
            "triage": {
                "card_type": "triage",
                "status": "triage",
                "priority": 1,
                "parents": ["missing", "triage"],
            }
        }

        result = TOPOLOGY.validate_card_topology({}, records, mode="new")

        self.assertEqual(
            (
                "SUMMARY_COUNT",
                "REVIEW_COUNT",
                "UNKNOWN_PARENT:triage:missing",
                "SELF_PARENT:triage",
                "DEPENDENCY_CYCLE:triage",
                "INVALID_EXECUTION_ORDER",
                "NON_EXECUTION_PRIORITY_MUST_BE_ZERO:triage",
                "TRIAGE_HAS_PARENT",
            ),
            result.errors,
        )

    def test_membership_and_parent_type_errors_preserve_contract_order(self) -> None:
        records = {
            "triage": {
                "card_type": "triage",
                "status": "triage",
                "priority": 0,
                "parents": [],
            },
            "impl": {
                "card_type": "implementation",
                "status": "todo",
                "priority": 2,
                "parents": ["summary"],
            },
            "review": {
                "card_type": "review",
                "status": "todo",
                "priority": 1,
                "parents": [],
            },
            "summary": {
                "card_type": "summary",
                "status": "todo",
                "priority": 0,
                "parents": [],
            },
            "decision": {
                "card_type": "decision",
                "status": "blocked",
                "priority": 0,
                "parents": ["impl"],
            },
        }
        graph = {
            "execution_order": ["impl", "review"],
            "ready_candidate": "impl",
        }

        result = TOPOLOGY.validate_card_topology(graph, records, mode="new")

        self.assertEqual(
            (
                "SUMMARY_MEMBERSHIP_MISMATCH",
                "PLANNING_DECISION_MUST_NOT_BE_GRAPH_CARD",
                "REVIEW_WITHOUT_PARENT:review",
                "INVALID_IMPL_PARENTS:impl",
                "INVALID_REVIEW_PARENTS:review",
                "DECISION_WITHOUT_REVIEW_PARENT:decision",
                "IMPLEMENTATION_WITHOUT_REVIEW_CHILD:impl",
            ),
            result.errors,
        )


if __name__ == "__main__":
    unittest.main()
