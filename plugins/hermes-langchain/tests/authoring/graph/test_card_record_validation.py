"""Focused tests for stateful build-task card indexing."""

from __future__ import annotations

import unittest

from tests.support import load_module


CARD_RECORDS = load_module("authoring.graph.card_record_validation")


class CardRecordIndexTests(unittest.TestCase):
    def test_valid_card_is_indexed_with_no_metadata_errors(self) -> None:
        index = CARD_RECORDS.CardRecordIndex(
            generation=1,
            issue_number=151,
            workspace="C:/workspace/project",
        )
        card = {
            "key": "triage",
            "title": "G1-Issue151-Triage",
            "card_type": "triage",
            "assignee": "project-manager",
            "skills": [],
            "workspace": "C:/workspace/project",
            "status": "triage",
            "priority": 0,
            "parents": [],
            "body": None,
        }

        result = index.add(0, card)

        self.assertEqual((), result.errors)
        self.assertEqual("triage", result.key)
        self.assertIs(card, result.card)
        self.assertEqual({"triage": card}, index.records)

    def test_non_object_and_missing_key_are_not_indexed(self) -> None:
        index = CARD_RECORDS.CardRecordIndex(
            generation=1,
            issue_number=151,
            workspace="C:/workspace/project",
        )

        non_object = index.add(0, "not-an-object")
        missing_key = index.add(1, {"key": "  "})

        self.assertEqual(("CARD_NOT_OBJECT:0",), non_object.errors)
        self.assertEqual(("MISSING_CARD_KEY:1",), missing_key.errors)
        self.assertEqual({}, index.records)

    def test_duplicate_card_metadata_errors_preserve_contract_order(self) -> None:
        index = CARD_RECORDS.CardRecordIndex(
            generation=1,
            issue_number=151,
            workspace="C:/workspace/project",
        )
        first = {
            "key": "triage",
            "title": "G1-Issue151-Triage",
            "card_type": "triage",
            "assignee": "project-manager",
            "skills": [],
            "workspace": "C:/workspace/project",
            "status": "triage",
            "priority": 0,
            "parents": [],
            "body": None,
        }
        second = {
            "key": "triage",
            "title": "G1-Issue151-Triage",
            "card_type": "unknown",
            "assignee": "coder",
            "skills": ["unexpected"],
            "workspace": "C:/workspace/other",
            "status": "invalid",
            "priority": True,
            "parents": None,
            "body": None,
            "extra": True,
        }
        index.add(0, first)

        result = index.add(1, second)

        self.assertEqual(
            (
                "UNEXPECTED_CARD_FIELD:1:extra",
                "DUPLICATE_CARD_KEY:triage",
                "DUPLICATE_CARD_TITLE:G1-Issue151-Triage",
                "TITLE_CARD_TYPE_MISMATCH:triage",
                "INVALID_CARD_TYPE:triage",
                "ASSIGNEE_MISMATCH:triage",
                "TASK_SKILLS_MUST_BE_EMPTY:triage",
                "WORKSPACE_MISMATCH:triage",
                "INVALID_CARD_STATUS:triage",
                "INVALID_CARD_PRIORITY:triage",
                "INVALID_PARENTS:triage",
            ),
            result.errors,
        )
        self.assertIs(second, index.records["triage"])


if __name__ == "__main__":
    unittest.main()
