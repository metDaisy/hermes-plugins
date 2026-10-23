"""Focused tests for type-specific card-body validators."""

from __future__ import annotations

import unittest

from tests.support import load_module


REVIEW = load_module("authoring.graph.review_body_validation")
SUMMARY = load_module("authoring.graph.summary_body_validation")
DECISION = load_module("authoring.graph.decision_body_validation")


class TypedCardBodyValidationTests(unittest.TestCase):
    def test_empty_review_body_error_order(self) -> None:
        self.assertEqual(
            (
                "INVALID_REVIEW_TRIAGE_DIGEST:review-1",
                "INVALID_REVIEW_BASELINE:review-1",
                "MISSING_REVIEW_BEHAVIORS:review-1",
                "MISSING_REVIEW_IMPLS:review-1",
                "MISSING_REVIEW_ACCEPTANCE:review-1",
                "MISSING_REVIEW_CHECKS:review-1",
                "REVIEW_DEVELOPMENT_GUIDE_COVERAGE:review-1",
                "REVIEW_TEST_GUIDE_COVERAGE:review-1",
                "REVIEW_ACTIVE_PLAN_COVERAGE:review-1",
                "INVALID_REVIEW_SCOPE_EXCLUSIONS:review-1",
            ),
            REVIEW.validate_review_body("review-1", {}),
        )

    def test_empty_summary_body_error_order(self) -> None:
        self.assertEqual(
            (
                "INVALID_SUMMARY_ISSUE:summary",
                "INVALID_SUMMARY_TRIAGE_DIGEST:summary",
                "INVALID_SUMMARY_OBJECTIVE:summary",
                "INVALID_SUMMARY_TARGET_OUTCOMES:summary",
                "INVALID_SUMMARY_PRESERVED_BEHAVIORS:summary",
                "INVALID_SUMMARY_COMPLETION_CONTRACT:summary",
                "INVALID_SUMMARY_RELEASE_BOUNDARY:summary",
            ),
            SUMMARY.validate_summary_body("summary", {}),
        )

    def test_empty_decision_body_error_order(self) -> None:
        self.assertEqual(
            (
                "MISSING_DECISION_REVIEW:decision-1",
                "MISSING_DECISION_FINDINGS:decision-1",
                "MISSING_DECISION_QUESTION:decision-1",
                "INVALID_DECISION_OWNER:decision-1",
            ),
            DECISION.validate_decision_body("decision-1", {}),
        )


if __name__ == "__main__":
    unittest.main()
