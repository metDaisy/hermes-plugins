"""Focused tests for Summary cross-card linkage validation."""

from __future__ import annotations

import unittest

from tests.support import load_module


VALIDATION = load_module("authoring.graph.summary_link_validation")


class SummaryLinkValidationTests(unittest.TestCase):
    def test_summary_lineage_and_projection_errors_preserve_order(self) -> None:
        graph = {
            "change_requirements": [{"id": "CHANGE-001", "outcome": "Expected"}],
            "behaviors": [{"id": "BEHAVIOR-001", "outcome": "Preserved"}],
        }
        records = {
            "summary": {
                "body": {
                    "triage_digest": "wrong",
                    "issue": {"number": 999, "url": "https://example.com/issues/999"},
                    "target_outcomes": [],
                    "preserved_behaviors": [],
                }
            }
        }

        errors = VALIDATION.validate_summary_links(
            graph,
            records,
            ("summary",),
            triage_digest="a" * 64,
            issue_number=151,
            issue_url="https://example.com/issues/151",
        )

        self.assertEqual(
            (
                "SUMMARY_TRIAGE_DIGEST_MISMATCH",
                "SUMMARY_ISSUE_MISMATCH",
                "SUMMARY_TARGET_OUTCOMES_MISMATCH",
                "SUMMARY_PRESERVED_BEHAVIORS_MISMATCH",
            ),
            errors,
        )


if __name__ == "__main__":
    unittest.main()
