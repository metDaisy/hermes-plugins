"""Focused tests for requirement/behavior graph validation."""

from __future__ import annotations

import unittest

from tests.support import load_module


VALIDATION = load_module("authoring.graph.requirement_behavior_validation")
GRAPH_CONTRACT = load_module("authoring.graph.graph_contract")


class RequirementBehaviorValidationTests(unittest.TestCase):
    def test_valid_contract_builds_immutable_indexes(self) -> None:
        graph = {
            "change_requirements": [
                {
                    "id": "CHANGE-001",
                    "outcome": "Preserve the existing public behavior.",
                    "source": {
                        "locator": "docs/requirements.md#Public behavior",
                        "claim": "The public behavior must remain unchanged.",
                    },
                }
            ],
            "behaviors": [
                {
                    "id": "BEHAVIOR-001",
                    "outcome": "The existing public behavior remains observable.",
                    "state": "implemented",
                    "disposition": "inherited",
                    "evidence": ["src/example.py#public_behavior"],
                    "requirement_ids": ["CHANGE-001"],
                }
            ],
        }

        result = VALIDATION.validate_requirement_behaviors(graph)

        self.assertEqual((), result.errors)
        self.assertEqual(frozenset({"CHANGE-001"}), result.requirement_ids)
        self.assertEqual(frozenset({"BEHAVIOR-001"}), result.behavior_ids)
        self.assertIs(graph["behaviors"], result.behaviors)

    def test_requirement_errors_preserve_contract_codes_and_order(self) -> None:
        graph = {
            "change_requirements": [
                "not-an-object",
                {
                    "id": "CHANGE-001",
                    "outcome": "",
                    "source": "not-an-object",
                    "extra": True,
                },
                {
                    "id": "CHANGE-001",
                    "outcome": "Preserve the public behavior.",
                    "source": {
                        "locator": "",
                        "claim": "The behavior remains public.",
                        "extra": True,
                    },
                },
            ],
            "behaviors": [
                {
                    "id": "BEHAVIOR-001",
                    "outcome": "The public behavior remains observable.",
                    "state": "implemented",
                    "disposition": "inherited",
                    "evidence": ["src/example.py#public_behavior"],
                    "requirement_ids": ["CHANGE-001"],
                }
            ],
        }

        result = VALIDATION.validate_requirement_behaviors(graph)

        self.assertEqual(
            (
                "CHANGE_REQUIREMENT_NOT_OBJECT:0",
                "UNEXPECTED_CHANGE_REQUIREMENT_FIELD:1:extra",
                "MISSING_CHANGE_REQUIREMENT_OUTCOME:1",
                "CHANGE_REQUIREMENT_SOURCE_NOT_OBJECT:1",
                "DUPLICATE_CHANGE_REQUIREMENT_ID:CHANGE-001",
                "UNEXPECTED_CHANGE_REQUIREMENT_SOURCE_FIELD:2:extra",
                "MISSING_CHANGE_REQUIREMENT_LOCATOR:2",
            ),
            result.errors,
        )

    def test_graph_validator_preserves_requirement_error_order(self) -> None:
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
                "heading": "Public behavior",
                "revision": "a" * 40,
            },
            "change_requirements": [
                {
                    "id": "CHANGE-001",
                    "outcome": "",
                    "source": "not-an-object",
                    "extra": True,
                }
            ],
            "behaviors": [
                {
                    "id": "BEHAVIOR-001",
                    "outcome": "The public behavior remains observable.",
                    "state": "implemented",
                    "disposition": "inherited",
                    "evidence": ["src/example.py#public_behavior"],
                    "requirement_ids": ["CHANGE-001"],
                }
            ],
            "cards": [],
        }

        self.assertEqual(
            [
                "UNEXPECTED_CHANGE_REQUIREMENT_FIELD:0:extra",
                "MISSING_CHANGE_REQUIREMENT_OUTCOME:0",
                "CHANGE_REQUIREMENT_SOURCE_NOT_OBJECT:0",
                "MISSING_CARDS",
            ],
            GRAPH_CONTRACT.validate_graph(graph),
        )

    def test_behavior_errors_preserve_contract_codes_and_order(self) -> None:
        graph = {
            "change_requirements": [
                {
                    "id": "CHANGE-001",
                    "outcome": "Preserve the public behavior.",
                    "source": {
                        "locator": "docs/requirements.md#Public behavior",
                        "claim": "The public behavior remains unchanged.",
                    },
                }
            ],
            "behaviors": [
                "not-an-object",
                {
                    "id": "BEHAVIOR-001",
                    "outcome": "",
                    "state": "invalid",
                    "disposition": "invalid",
                    "evidence": [],
                    "requirement_ids": ["CHANGE-001", "CHANGE-001"],
                    "extra": True,
                },
                {
                    "id": "BEHAVIOR-001",
                    "outcome": "An unknown behavior is referenced.",
                    "state": "implemented",
                    "disposition": "planned",
                    "evidence": [],
                    "requirement_ids": ["UNKNOWN"],
                    "implementation_card_key": "impl-1",
                },
            ],
        }

        result = VALIDATION.validate_requirement_behaviors(graph)

        self.assertEqual(
            (
                "BEHAVIOR_NOT_OBJECT:0",
                "UNEXPECTED_BEHAVIOR_FIELD:1:extra",
                "INVALID_BEHAVIOR_STATE:BEHAVIOR-001",
                "INVALID_BEHAVIOR_DISPOSITION:BEHAVIOR-001",
                "MISSING_BEHAVIOR_OUTCOME:BEHAVIOR-001",
                "DUPLICATE_BEHAVIOR_REQUIREMENT_ID:BEHAVIOR-001",
                "DUPLICATE_BEHAVIOR_ID:BEHAVIOR-001",
                "UNKNOWN_CHANGE_REQUIREMENT:BEHAVIOR-001:UNKNOWN",
                "IMPLEMENTED_NOT_INHERITED:BEHAVIOR-001",
                "MISSING_INHERITED_EVIDENCE:BEHAVIOR-001",
            ),
            result.errors,
        )


if __name__ == "__main__":
    unittest.main()
