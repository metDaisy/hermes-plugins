from __future__ import annotations

import unittest
from pathlib import Path


_ROOT = Path(__file__).resolve().parents[1]
from tests.support import load_module

CORE = load_module("kanban.kwc_core")


class AddOne(CORE.Activity[int, int]):
    def execute(self, request: int) -> int:
        return request + 1


class KwcCoreTests(unittest.TestCase):
    def test_activity_executor_owns_common_execution_lifecycle(self) -> None:
        result = CORE.ActivityExecutor().execute(AddOne(), 41)
        self.assertEqual(42, result.value)

    def test_container_fails_fast_for_duplicate_and_missing_bindings(self) -> None:
        container = CORE.ObjectContainer()
        container.bind(str, "value")
        self.assertEqual("value", container.require(str))
        with self.assertRaisesRegex(ValueError, "duplicate binding"):
            container.bind(str, "other")
        with self.assertRaisesRegex(ValueError, "missing binding"):
            container.require(int)


if __name__ == "__main__":
    unittest.main()
