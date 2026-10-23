"""Regression tests for semantic closure in Impl batch authoring."""

from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


_ROOT = Path(__file__).resolve().parents[3]
from tests.support import load_module

DOMAIN = load_module("authoring.triage_domain")
BATCH = load_module("authoring.graph.implementation_batch")


class ImplementationBatchSemanticClosureTest(unittest.TestCase):
    def _valid_batch(self) -> dict:
        source = json.loads(
            (_ROOT / "tests" / "fixtures" / "valid-triage-authoring.json").read_text(encoding="utf-8")
        )
        body = DOMAIN.template(
            source["identity"]["issue_number"],
            source["identity"]["title"],
            source["identity"]["issue_url"],
            source["identity"]["planning_sha"],
            source["identity"]["current_state_sha"],
            source["identity"]["requirement_locators"],
            source["identity"]["current_state_locators"],
        )
        intake = DOMAIN.compose_intake(body, source["intake"])
        analysis = DOMAIN.compose_analysis(intake, source["analysis"])
        batch = BATCH.impl_batch_template(
            DOMAIN.freeze(analysis), generation=1, triage_id="t_triage"
        )
        implementation = batch["cards"][0]["body"]
        implementation["implementation_contract"] = {
            dimension: {
                "applicability": "applicable",
                "rules": ["해당 책임 경계를 명시적으로 유지한다."],
            }
            for dimension in BATCH.CONTRACT_DIMENSIONS
        }
        implementation["implementation_contract"]["preserved_behaviors"] = [
            "quantity=0 삭제 동작을 보존한다."
        ]
        implementation["context"]["entry_points"][0]["reason"] = (
            "[ACCEPT-001][modify] Order aggregate의 수량 변경 동작을 수정한다."
        )
        for required in batch["cards"][0].get("required_entry_points", []):
            if not any(
                entry.get("path") == required.get("path")
                and entry.get("symbol") == required.get("symbol")
                for entry in implementation["context"]["entry_points"]
            ):
                implementation["context"]["entry_points"].append(
                    {
                        "path": required["path"],
                        "symbol": required["symbol"],
                        "reason": "[ACCEPT-001][modify] frozen gap 근거 artifact를 명시한다.",
                    }
                )
        required_scenarios = batch["cards"][0].get(
            "required_verification_scenarios", {}
        ).get("VERIFY-001", [])
        implementation["verification"][0].update(
            {
                "test_level": "unit",
                "tests": [
                    "com.example.orders.domain.entity.OrderTest#quantityZeroDeletes"
                ],
                "scenarios": list(required_scenarios)
                + ["quantity=0이면 기존처럼 항목을 삭제한다."],
            }
        )
        return batch

    def test_template_requires_every_gap_evidence_artifact(self):
        batch = self._valid_batch()

        self.assertIn(
            {
                "path": (
                    "src/main/java/com/example/orders/application/"
                    "service/OrderService.java"
                ),
                "symbol": "OrderService#changeQuantity",
            },
            batch["cards"][0]["required_entry_points"],
        )

    def test_template_requires_exact_named_behavior_from_gap(self):
        batch = self._valid_batch()

        self.assertIn(
            "Order#changeQuantity",
            batch["cards"][0]["required_contract_symbols"],
        )

    def _batch_with_semantic_dependency(self) -> dict:
        batch = self._valid_batch()
        parent = batch["cards"][0]
        parent["priority"] = 2
        parent_body = parent["body"]
        parent_body["context"]["entry_points"] = [
            {
                "path": "src/main/java/example/OrderService.java",
                "symbol": "OrderService#changeQuantity",
                "reason": "[ACCEPT-001][modify] 서비스 위임 경로를 수정한다.",
            }
        ]

        child = copy.deepcopy(parent)
        child["ordinal"] = 2
        child["title"] = "G1-Issue42-Impl2"
        child["priority"] = 1
        child["body"]["slice"]["id"] = "SLICE-002"
        child["body"]["acceptance"] = [
            {
                "id": "ACCEPT-002",
                "criterion": "변경된 서비스 위임 경로를 통합 검증한다.",
                "requirement_ids": ["CHANGE-001"],
                "observable_ids": ["OBS-001"],
            }
        ]
        child["body"]["context"]["entry_points"] = [
            {
                "path": "src/main/java/example/OrderService.java",
                "symbol": "OrderService#changeQuantity",
                "reason": "[ACCEPT-002][verify] 변경된 서비스 위임 경로를 통합 검증한다.",
            }
        ]
        child["body"]["verification"] = [
            {
                "id": "VERIFY-002",
                "acceptance_ids": ["ACCEPT-002"],
                "test_level": "integration",
                "task": "test",
                "tests": ["example.OrderIntegrationTest#changeQuantityDelegatesToAggregate"],
                "scenarios": ["변경된 서비스 경로가 aggregate 동작을 호출한다."],
            }
        ]
        child["required_verification_ids"] = ["VERIFY-002"]
        child["depends_on_ordinals"] = []
        batch["cards"].append(child)
        return batch

    def test_batch_rejects_entry_point_without_acceptance_and_operation_markers(self):
        batch = self._valid_batch()
        for entry_point in batch["cards"][0]["body"]["context"]["entry_points"]:
            entry_point["reason"] = "Order aggregate의 수량 변경 동작을 수정한다."

        errors = BATCH.validate_impl_batch(batch)

        self.assertIn("IMPL_ENTRY_POINT_ACCEPTANCE_UNMAPPED:0:0", errors)
        self.assertIn("IMPL_ENTRY_POINT_OPERATION_MISSING:0:0", errors)
        self.assertIn("IMPL_ACCEPTANCE_ARTIFACTS_UNCOVERED:0:ACCEPT-001", errors)

    def test_batch_rejects_class_only_verification_selector(self):
        batch = self._valid_batch()
        batch["cards"][0]["body"]["verification"][0]["tests"] = [
            "com.example.orders.domain.entity.OrderTest"
        ]

        errors = BATCH.validate_impl_batch(batch)

        self.assertIn("IMPL_VERIFICATION_METHOD_SELECTOR_REQUIRED:0:VERIFY-001:0", errors)

    def test_batch_rejects_missing_frozen_gap_artifact(self):
        batch = self._valid_batch()
        required = batch["cards"][0]["required_entry_points"][0]
        batch["cards"][0]["body"]["context"]["entry_points"] = [
            entry
            for entry in batch["cards"][0]["body"]["context"]["entry_points"]
            if not (
                entry.get("path") == required["path"]
                and entry.get("symbol") == required["symbol"]
            )
        ]

        errors = BATCH.validate_impl_batch(batch)

        self.assertIn(
            "IMPL_REQUIRED_ENTRY_POINT_MISSING:0:"
            f"{required['path']}#{required['symbol']}",
            errors,
        )

    def test_batch_rejects_non_decisive_selector_for_lock_scenario(self):
        batch = self._valid_batch()
        verification = batch["cards"][0]["body"]["verification"][0]
        verification["scenarios"] = [
            "member owner query의 PESSIMISTIC_WRITE 잠금을 PostgreSQL에서 확인한다."
        ]

        errors = BATCH.validate_impl_batch(batch)

        self.assertIn(
            "IMPL_VERIFICATION_SELECTOR_NOT_DECISIVE:0:VERIFY-001:lock",
            errors,
        )

    def test_batch_rejects_declared_level_without_matching_selector(self):
        batch = self._valid_batch()
        verification = batch["cards"][0]["body"]["verification"][0]
        verification["test_level"] = "integration"

        errors = BATCH.validate_impl_batch(batch)

        self.assertIn(
            "IMPL_VERIFICATION_LEVEL_SELECTOR_REQUIRED:0:VERIFY-001:integration",
            errors,
        )

    def test_batch_requires_both_source_and_target_for_move_acceptance(self):
        batch = self._valid_batch()
        body = batch["cards"][0]["body"]
        body["acceptance"][0]["criterion"] = (
            "InventoryQueryPort를 application.port.out에서 domain.port.out으로 이동한다."
        )
        body["context"]["entry_points"] = [
            {
                "path": "src/main/java/example/application/port/out/InventoryQueryPort.java",
                "symbol": "InventoryQueryPort",
                "reason": "[ACCEPT-001][move-source] 기존 application port 선언을 이동한다.",
            }
        ]

        errors = BATCH.validate_impl_batch(batch)

        self.assertIn("IMPL_MOVE_TARGET_MISSING:0:ACCEPT-001:InventoryQueryPort", errors)

    def test_batch_requires_dependency_when_child_verifies_parent_changed_artifact(self):
        batch = self._batch_with_semantic_dependency()

        errors = BATCH.validate_impl_batch(batch)

        self.assertIn(
            "IMPL_SEMANTIC_DEPENDENCY_REQUIRED:1:2:OrderService#changeQuantity",
            errors,
        )

    def test_apply_completions_rejects_semantically_invalid_merged_batch(self):
        batch = self._batch_with_semantic_dependency()
        completions = {
            "cards": [
                {
                    "ordinal": card["ordinal"],
                    "implementation_contract": copy.deepcopy(
                        card["body"]["implementation_contract"]
                    ),
                    "entry_points": copy.deepcopy(
                        card["body"]["context"]["entry_points"]
                    ),
                    "verification": [
                        {
                            "id": verification["id"],
                            "test_level": verification["test_level"],
                            "tests": copy.deepcopy(verification["tests"]),
                            "scenarios": copy.deepcopy(verification["scenarios"]),
                        }
                        for verification in card["body"]["verification"]
                    ],
                    "depends_on_ordinals": [],
                }
                for card in batch["cards"]
            ],
            "dependency_audit": {"edges": []},
        }

        with self.assertRaisesRegex(
            ValueError,
            "IMPL_SEMANTIC_DEPENDENCY_REQUIRED:1:2:OrderService#changeQuantity",
        ):
            BATCH.apply_impl_completions(batch, completions)

    def test_apply_completions_preserves_frozen_required_scenarios(self):
        batch = self._valid_batch()
        required = copy.deepcopy(
            batch["cards"][0]["required_verification_scenarios"]["VERIFY-001"]
        )
        card = batch["cards"][0]
        completions = {
            "cards": [
                {
                    "ordinal": 1,
                    "implementation_contract": copy.deepcopy(
                        card["body"]["implementation_contract"]
                    ),
                    "entry_points": copy.deepcopy(
                        card["body"]["context"]["entry_points"]
                    ),
                    "verification": [
                        {
                            "id": "VERIFY-001",
                            "test_level": "unit",
                            "tests": [
                                "com.example.orders.domain.entity.OrderTest#quantityZeroDeletes"
                            ],
                            "scenarios": ["작성자가 추가한 구체적인 회귀 시나리오다."],
                        }
                    ],
                    "depends_on_ordinals": [],
                }
            ],
            "dependency_audit": {"edges": []},
        }

        merged = BATCH.apply_impl_completions(batch, completions)

        scenarios = merged["cards"][0]["body"]["verification"][0]["scenarios"]
        self.assertTrue(set(required).issubset(scenarios))
        self.assertIn("작성자가 추가한 구체적인 회귀 시나리오다.", scenarios)

    def test_bundled_helper_uses_core_impl_batch_semantics(self):
        batch = self._valid_batch()
        required = batch["cards"][0]["required_entry_points"][0]
        batch["cards"][0]["body"]["context"]["entry_points"] = [
            entry
            for entry in batch["cards"][0]["body"]["context"]["entry_points"]
            if not (
                entry.get("path") == required["path"]
                and entry.get("symbol") == required["symbol"]
            )
        ]
        expected = BATCH.validate_impl_batch(batch)
        self.assertTrue(expected)
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "batch.json"
            source.write_text(json.dumps(batch, ensure_ascii=False), encoding="utf-8")
            script = (
                _ROOT
                / "skills"
                / "kwc-build-task-graph"
                / "scripts"
                / "build_task_graph.py"
            )
            env = dict(os.environ)
            env["PYTHONPATH"] = str(_ROOT / "src")
            result = subprocess.run(
                [sys.executable, str(script), "validate-impl-batch", str(source)],
                cwd=str(_ROOT),
                env=env,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
        payload = json.loads(result.stdout)

        self.assertEqual(1, result.returncode)
        self.assertEqual(expected, payload["errors"])


if __name__ == "__main__":
    unittest.main()