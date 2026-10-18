"""Focused tests for Review cross-card linkage validation."""

from __future__ import annotations

import unittest

from tests.support import load_module


VALIDATION = load_module("authoring.graph.review_link_validation")


class ReviewLinkValidationTests(unittest.TestCase):
    def test_latest_review_and_lineage_errors_are_deterministic(self) -> None:
        records = {
            "review-1": {
                "title": "G1-Issue151-Review1",
                "card_type": "review",
                "body": {
                    "triage_digest": "wrong",
                    "baseline_sha": "wrong",
                    "behavior_ids": [],
                    "implementation_card_keys": [],
                    "acceptance": [],
                    "review_checks": [],
                },
            }
        }

        result = VALIDATION.validate_review_links(
            records,
            ("review-1",),
            behavior_ids=set(),
            implementation_keys=set(),
            triage_digest="a" * 64,
            planning_baseline_sha="b" * 40,
        )

        self.assertEqual("review-1", result.latest_review)
        self.assertEqual(
            (
                "REVIEW_TRIAGE_DIGEST_MISMATCH:review-1",
                "REVIEW_BASELINE_MISMATCH:review-1",
            ),
            result.errors,
        )


if __name__ == "__main__":
    unittest.main()
