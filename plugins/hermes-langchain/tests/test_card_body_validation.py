"""Focused tests for Review, Summary, and Decision card body validation."""

from __future__ import annotations

import unittest

from tests.support import load_module


VALIDATION = load_module("authoring.graph.card_body_validation")


class CardBodyValidationTests(unittest.TestCase):
    def test_decision_body_errors_preserve_contract_order(self) -> None:
        card = {
            "key": "decision-1",
            "card_type": "decision",
            "body": {
                "schema": "wrong",
                "source_review_key": "",
                "finding_ids": [],
                "question": "",
                "decision_owner": "project-manager",
                "behavior_ids": ["UNKNOWN"],
            },
        }

        self.assertEqual(
            (
                "CARD_BODY_SCHEMA:decision-1",
                "UNEXPECTED_DECISION_BODY_FIELD:decision-1:behavior_ids",
                "MISSING_DECISION_REVIEW:decision-1",
                "MISSING_DECISION_FINDINGS:decision-1",
                "MISSING_DECISION_QUESTION:decision-1",
                "INVALID_DECISION_OWNER:decision-1",
                "UNKNOWN_BEHAVIOR:decision-1:UNKNOWN",
            ),
            VALIDATION.validate_card_body(card, {"BEHAVIOR-001"}),
        )

    def test_triage_and_implementation_keep_external_body_contracts(self) -> None:
        triage = {"key": "triage", "card_type": "triage", "body": {}}
        implementation = {
            "key": "impl-1",
            "card_type": "implementation",
            "body": {"schema": "wrong"},
        }

        self.assertEqual(
            ("TRIAGE_BODY_MUST_BE_EXTERNAL:triage",),
            VALIDATION.validate_card_body(triage, set()),
        )
        self.assertEqual(
            ("IMPLEMENTATION_SCHEMA:impl-1",),
            VALIDATION.validate_card_body(implementation, set()),
        )


if __name__ == "__main__":
    unittest.main()
