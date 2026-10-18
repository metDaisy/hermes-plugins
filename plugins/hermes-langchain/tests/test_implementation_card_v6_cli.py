"""CLI contract tests for the backend implementation card v6 validator."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path


_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = _ROOT / "src" / "hermes_langchain" / "authoring" / "graph" / "build_task_graph.py"
_FIXTURE = _ROOT / "fixtures" / "issue151-impl5-v6.json"


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

    def test_accepts_complete_issue151_v6_fixture(self):
        result = self.run_validator(_FIXTURE)

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual({"valid": True, "errors": []}, json.loads(result.stdout))

    def test_rejects_invalid_v6_operation_count_with_stable_error(self):
        card = json.loads(_FIXTURE.read_text(encoding="utf-8"))
        card["obligations"][0]["operation"]["invocation_count"] = 0
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "invalid-v6.json"
            path.write_text(json.dumps(card, ensure_ascii=False), encoding="utf-8")

            result = self.run_validator(path)

        self.assertEqual(1, result.returncode, result.stderr)
        payload = json.loads(result.stdout)
        self.assertFalse(payload["valid"])
        self.assertIn(
            "INVALID_OBLIGATION_OPERATION_INVOCATION_COUNT:OB-AGGREGATE-BATCH",
            payload["errors"],
        )

    def test_v6_card_is_accepted_by_native_impl_batch_boundary(self):
        card = json.loads(_FIXTURE.read_text(encoding="utf-8"))
        batch = {
            "issue": 151,
            "generation": 11,
            "cards": [
                {
                    "ordinal": 1,
                    "title": "G11-Issue151-Impl1",
                    "priority": 1,
                    "body": card,
                    "contract_changes": [],
                    "required_contract_symbols": [
                        "io.github.metdaisy.amaazon.cart.domain.entity.Cart#mergeItems"
                    ],
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
