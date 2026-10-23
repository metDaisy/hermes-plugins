"""CLI contract tests for the backend implementation card v6 validator."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.authoring.graph.test_implementation_card import ImplementationCardV6ContractTest


_ROOT = Path(__file__).resolve().parents[3]
_SCRIPT = _ROOT / "src" / "hermes_langchain" / "authoring" / "graph" / "build_task_graph.py"


class ImplementationCardV6CliTest(unittest.TestCase):
    def run_validator(self, card: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["python", str(_SCRIPT), "validate-implementation-v6", str(card)],
            cwd=str(_ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )

    def test_accepts_complete_v6_card(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "valid-v6.json"
            path.write_text(
                json.dumps(ImplementationCardV6ContractTest.valid_card(), ensure_ascii=False),
                encoding="utf-8",
            )
            result = self.run_validator(path)

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual({"valid": True, "errors": []}, json.loads(result.stdout))

    def test_rejects_invalid_v6_operation_count_with_stable_error(self):
        card = ImplementationCardV6ContractTest.valid_card()
        card["obligations"][0]["operation"]["invocation_count"] = 0
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "invalid-v6.json"
            path.write_text(json.dumps(card, ensure_ascii=False), encoding="utf-8")

            result = self.run_validator(path)

        self.assertEqual(1, result.returncode, result.stderr)
        payload = json.loads(result.stdout)
        self.assertFalse(payload["valid"])
        self.assertIn(
            "INVALID_OBLIGATION_OPERATION_INVOCATION_COUNT:OB-MERGE-CONFLICT",
            payload["errors"],
        )

    def test_v6_card_is_accepted_by_native_impl_batch_boundary(self):
        card = ImplementationCardV6ContractTest.valid_card()
        batch = {
            "issue": 42,
            "generation": 1,
            "cards": [
                {
                    "ordinal": 1,
                    "title": "G1-Issue42-Impl1",
                    "priority": 1,
                    "body": card,
                    "contract_changes": [],
                    "required_contract_symbols": [card["obligations"][0]["operation"]["symbol"]],
                    "parent_ids": ["t_triage"],
                    "depends_on_ordinals": [],
                }
            ],
            "dependency_audit": {"edges": []},
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "batch-v6.json"
            path.write_text(json.dumps(batch, ensure_ascii=False), encoding="utf-8")
            result = subprocess.run(
                ["python", str(_SCRIPT), "validate-impl-batch", str(path)],
                cwd=str(_ROOT),
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual({"valid": True, "errors": []}, json.loads(result.stdout))


if __name__ == "__main__":
    unittest.main()
