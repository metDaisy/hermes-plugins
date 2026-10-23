"""Focused tests for Implementation card lineage and coverage validation."""

from __future__ import annotations

import unittest

from tests.support import load_module


VALIDATION = load_module("authoring.graph.implementation_card_validation")


class ImplementationCardValidationTests(unittest.TestCase):
    def test_mismatch_errors_preserve_external_and_contract_order(self) -> None:
        context = VALIDATION.ImplementationCardContext(
            issue_number=151,
            issue_url="https://example.com/issues/151",
            triage_digest="a" * 64,
            behavior_ids=frozenset({"BEHAVIOR-001"}),
            requirement_ids=frozenset({"CHANGE-001"}),
            behaviors=(
                {
                    "id": "BEHAVIOR-001",
                    "requirement_ids": ["CHANGE-001"],
                },
            ),
        )
        card = {
            "key": "impl-1",
            "card_type": "implementation",
            "body": {
                "issue": {
                    "number": 999,
                    "url": "https://example.com/issues/999",
                },
                "triage_digest": "b" * 64,
                "slice": {"behavior_ids": ["UNKNOWN"]},
                "acceptance": [
                    {"requirement_ids": ["UNKNOWN-REQ"]},
                ],
            },
        }

        errors = VALIDATION.validate_implementation_card(
            card,
            context,
            validate_external=lambda body: ["EXTERNAL_FAILURE"],
        )

        self.assertEqual(
            (
                "IMPLEMENTATION:impl-1:EXTERNAL_FAILURE",
                "IMPLEMENTATION_ISSUE_NUMBER_MISMATCH:impl-1",
                "IMPLEMENTATION_ISSUE_URL_MISMATCH:impl-1",
                "IMPLEMENTATION_TRIAGE_DIGEST_MISMATCH:impl-1",
                "IMPLEMENTATION_UNKNOWN_BEHAVIOR:impl-1",
                "IMPLEMENTATION_UNKNOWN_REQUIREMENT:impl-1:UNKNOWN-REQ",
                "IMPLEMENTATION_REQUIREMENT_COVERAGE:impl-1",
            ),
            errors,
        )

    def test_v6_trace_supplies_lineage_and_coverage(self) -> None:
        context = VALIDATION.ImplementationCardContext(
            issue_number=151,
            issue_url="https://example.com/issues/151",
            triage_digest="a" * 64,
            behavior_ids=frozenset({"OBS-001"}),
            requirement_ids=frozenset({"CHANGE-001"}),
            behaviors=(
                {
                    "id": "OBS-001",
                    "requirement_ids": ["CHANGE-001"],
                },
            ),
        )
        card = {
            "key": "impl-1",
            "card_type": "implementation",
            "body": {
                "schema": "backend-implementation-card-v6",
                "trace": {
                    "triage_digest": "a" * 64,
                    "slice_id": "S-001",
                    "requirement_ids": ["CHANGE-001"],
                    "observable_ids": ["OBS-001"],
                    "gap_ids": ["G-001"],
                },
            },
        }

        self.assertEqual(
            (),
            VALIDATION.validate_implementation_card(
                card,
                context,
                validate_external=lambda body: [],
            ),
        )


if __name__ == "__main__":
    unittest.main()
