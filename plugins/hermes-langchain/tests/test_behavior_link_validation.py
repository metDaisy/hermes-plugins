"""Focused tests for Behavior-to-Implementation graph linkage."""

from __future__ import annotations

import unittest

from tests.support import load_module


VALIDATION = load_module("authoring.graph.behavior_link_validation")


class BehaviorLinkValidationTests(unittest.TestCase):
    def test_link_and_coverage_errors_preserve_contract_order(self) -> None:
        behaviors = [
            {
                "id": "BEHAVIOR-001",
                "state": "partial",
                "disposition": "planned",
                "implementation_card_key": "missing",
            },
            {
                "id": "BEHAVIOR-002",
                "state": "absent",
                "disposition": "planned",
                "implementation_card_key": "impl-1",
            },
            {
                "id": "BEHAVIOR-003",
                "state": "partial",
                "disposition": "inherited",
                "implementation_card_key": "not-impl",
            },
        ]
        records = {
            "impl-1": {
                "card_type": "implementation",
                "body": {"slice": {"behavior_ids": ["BEHAVIOR-002"]}},
            },
            "impl-extra": {
                "card_type": "implementation",
                "body": {"slice": {"behavior_ids": ["UNASSIGNED"]}},
            },
            "not-impl": {"card_type": "review", "body": {}},
        }

        result = VALIDATION.validate_behavior_implementation_links(
            behaviors,
            records,
            mode="new",
        )

        self.assertEqual(
            (
                "BEHAVIOR_IMPL_NOT_FOUND:BEHAVIOR-001:missing",
                "BEHAVIOR_IMPL_NOT_FOUND:BEHAVIOR-003:not-impl",
                "BEHAVIOR_IMPL_MISMATCH:BEHAVIOR-003:not-impl",
                "IMPLEMENTATION_BEHAVIOR_COVERAGE",
                "IMPLEMENTATION_BEHAVIOR_COVERAGE:impl-extra",
            ),
            result.errors,
        )
        self.assertEqual(frozenset({"impl-1", "impl-extra"}), result.implementation_keys)
        self.assertEqual(frozenset({"impl-1", "missing"}), result.planned_impl_keys)


if __name__ == "__main__":
    unittest.main()
