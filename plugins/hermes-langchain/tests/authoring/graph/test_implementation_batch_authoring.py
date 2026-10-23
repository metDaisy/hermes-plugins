"""Current-writer v6 integration tests for Impl batch authoring."""

from __future__ import annotations

import copy
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.support import load_module


_ROOT = Path(__file__).resolve().parents[3]
DOMAIN = load_module("authoring.triage_domain")
BATCH = load_module("authoring.graph.implementation_batch")
NATIVE = load_module("authoring.graph.native_authoring")


class ImplementationBatchV6AuthoringTest(unittest.TestCase):
    def frozen_triage(self) -> dict:
        source = json.loads(
            (_ROOT / "tests" / "fixtures" / "valid-triage-authoring.json").read_text(encoding="utf-8")
        )
        identity = source["identity"]
        body = DOMAIN.template(
            identity["issue_number"],
            identity["title"],
            identity["issue_url"],
            identity["planning_sha"],
            identity["current_state_sha"],
            identity["requirement_locators"],
            identity["current_state_locators"],
        )
        intake = DOMAIN.compose_intake(body, source["intake"])
        return DOMAIN.freeze(DOMAIN.compose_analysis(intake, source["analysis"]))

    def valid_completions(self, batch: dict) -> dict:
        card = batch["cards"][0]
        obligations = copy.deepcopy(card["body"]["obligations"])
        obligations[0]["given"] = ["OrderService가 기존 Order와 OrderItem을 조회했다."]
        obligations[0]["operation"]["invocation_count"] = 1
        obligations[0]["observables"] = [
            {
                "target": "수량 변경 뒤 OrderItem 상태",
                "source": "in-memory-state",
                "comparison": "present",
                "identity": ["OrderItem.id"],
                "values": ["quantity"],
                "expected": "Order aggregate가 수량 변경과 quantity=0 삭제를 소유한다.",
            }
        ]
        verification = copy.deepcopy(card["body"]["verification"])
        verification[0]["level"] = "unit"
        return {
            "cards": [
                {
                    "ordinal": 1,
                    "obligations": obligations,
                    "verification": verification,
                    "depends_on_ordinals": [],
                }
            ],
            "dependency_audit": {"edges": []},
        }

    def test_current_writer_batch_uses_v6_skeletons(self) -> None:
        batch = BATCH.impl_batch_template_v6(
            self.frozen_triage(), generation=1, triage_id="t_triage"
        )

        wrapper = batch["cards"][0]
        self.assertEqual("backend-implementation-card-v6", wrapper["body"]["schema"])
        self.assertEqual(["VERIFY-001"], wrapper["required_verification_ids"])
        self.assertEqual(["t_triage"], wrapper["parent_ids"])
        self.assertNotIn("required_entry_points", wrapper)

    def test_contract_change_constraint_is_covered_by_declared_verification(self) -> None:
        triage = self.frozen_triage()
        triage["contract_changes"] = [
            {
                "id": "CONTRACT-001",
                "consumer": "orders",
                "producer": "catalog",
                "change": "Catalog required lookup 오류 계약을 Order command가 사용한다.",
                "evidence": [
                    {
                        "locator": "src/main/java/com/example/orders/application/service/OrderService.java#changeQuantity",
                        "claim": "OrderService가 Catalog 공개 조회 계약을 호출한다.",
                    }
                ],
                "verification_ids": ["VERIFY-001"],
            }
        ]
        triage["implementation_slices"][0]["contract_change_ids"] = ["CONTRACT-001"]
        triage["frozen_digest"] = DOMAIN._body_digest(triage)

        batch = BATCH.impl_batch_template_v6(
            triage, generation=1, triage_id="t_triage"
        )

        verification = batch["cards"][0]["body"]["verification"][0]
        self.assertIn("CONTRACT-001", verification["covers"])

    def test_apply_completions_merges_and_validates_v6_contract(self) -> None:
        batch = BATCH.impl_batch_template_v6(
            self.frozen_triage(), generation=1, triage_id="t_triage"
        )

        merged = BATCH.apply_impl_completions(batch, self.valid_completions(batch))

        self.assertEqual("backend-implementation-card-v6", merged["cards"][0]["body"]["schema"])
        self.assertEqual(1, merged["cards"][0]["body"]["obligations"][0]["operation"]["invocation_count"])
        self.assertEqual([], BATCH.validate_impl_batch(merged))

    def test_apply_completions_preserves_obligation_operation_symbol(self) -> None:
        batch = BATCH.impl_batch_template_v6(
            self.frozen_triage(), generation=1, triage_id="t_triage"
        )
        completions = self.valid_completions(batch)
        completions["cards"][0]["obligations"][0]["operation"]["symbol"] = (
            "com.example.orders.application.service.OrderService#unrelatedOperation"
        )

        with self.assertRaisesRegex(
            ValueError, "IMPL_COMPLETION_OPERATION_SYMBOL_MISMATCH:1:ACCEPT-001"
        ):
            BATCH.apply_impl_completions(batch, completions)

    def test_apply_completions_preserves_acceptance_criterion(self) -> None:
        batch = BATCH.impl_batch_template_v6(
            self.frozen_triage(), generation=1, triage_id="t_triage"
        )
        completions = self.valid_completions(batch)
        completions["cards"][0]["obligations"][0]["expected"] = [
            "Order 수량 변경이 성공한다."
        ]

        with self.assertRaisesRegex(
            ValueError, "IMPL_COMPLETION_EXPECTED_ANCHOR_MISSING:1:ACCEPT-001"
        ):
            BATCH.apply_impl_completions(batch, completions)

    def test_apply_completions_preserves_verification_coverage_mapping(self) -> None:
        batch = BATCH.impl_batch_template_v6(
            self.frozen_triage(), generation=1, triage_id="t_triage"
        )
        completions = self.valid_completions(batch)
        completions["cards"][0]["verification"][0]["covers"] = [
            batch["cards"][0]["body"]["constraints"][0]["id"]
        ]

        with self.assertRaisesRegex(
            ValueError, "IMPL_COMPLETION_VERIFICATION_COVERS_MISMATCH:1:VERIFY-001"
        ):
            BATCH.apply_impl_completions(batch, completions)

    def test_v6_batch_requires_dependency_when_final_card_verifies_parent_artifact(self) -> None:
        batch = BATCH.impl_batch_template_v6(
            self.frozen_triage(), generation=1, triage_id="t_triage"
        )
        batch = BATCH.apply_impl_completions(batch, self.valid_completions(batch))
        parent = batch["cards"][0]
        parent["priority"] = 2
        parent["body"]["boundary"]["in_scope"] = [
            "modify: src/main/java/com/example/orders/domain/entity/Order.java#changeQuantity(UUID,int) 수량 변경 규칙을 수정한다."
        ]

        child = copy.deepcopy(parent)
        child["ordinal"] = 2
        child["title"] = "G1-Issue42-Impl2"
        child["priority"] = 1
        child["body"]["trace"]["slice_id"] = "SLICE-002"
        child["body"]["objective"]["outcome"] = "변경된 Order 수량 규칙을 통합 검증한다."
        child["body"]["boundary"]["in_scope"] = [
            "production change 없음; verification만 소유",
            "verify: com.example.orders.OrderIntegrationTest#changeQuantity - 수량 변경 규칙을 검증한다.",
        ]
        child["body"]["obligations"] = []
        child["body"]["constraints"] = [
            {
                "id": "ACCEPT-002",
                "rule": "변경된 Order 수량 규칙을 통합 검증한다.",
                "source": {
                    "locator": "src/main/java/com/example/orders/domain/entity/Order.java#changeQuantity",
                    "claim": "Order가 수량 변경 규칙을 소유한다.",
                },
            }
        ]
        child["body"]["verification"] = [
            {
                "id": "VERIFY-002",
                "covers": ["ACCEPT-002"],
                "level": "integration",
                "selectors": [
                    "com.example.orders.OrderIntegrationTest#changeQuantity"
                ],
            }
        ]
        child["required_verification_ids"] = ["VERIFY-002"]
        child["required_contract_symbols"] = []
        child["depends_on_ordinals"] = []
        batch["cards"].append(child)

        error = "IMPL_SEMANTIC_DEPENDENCY_REQUIRED:1:2:Order#changeQuantity"
        self.assertIn(error, BATCH.validate_impl_batch(batch))

        child["depends_on_ordinals"] = [1]
        batch["dependency_audit"] = {
            "edges": [
                {
                    "from_ordinal": 1,
                    "to_ordinal": 2,
                    "required_artifact": "CHANGE-001",
                    "consumer_entry_point": "CHANGE-001",
                    "why_baseline_cannot_satisfy": (
                        "같은 requirement ID가 양쪽 body에 있으므로 충분하다고 위조한다."
                    ),
                }
            ]
        }

        counterfeit_errors = BATCH.validate_impl_batch(batch)
        self.assertIn(
            "IMPL_DEPENDENCY_ARTIFACT_UNPROVEN:1:2", counterfeit_errors
        )
        self.assertIn(
            "IMPL_DEPENDENCY_CONSUMER_UNPROVEN:1:2", counterfeit_errors
        )

        batch["dependency_audit"] = {
            "edges": [
                {
                    "from_ordinal": 1,
                    "to_ordinal": 2,
                    "required_artifact": (
                        "src/main/java/com/example/orders/domain/entity/"
                        "Order.java#changeQuantity"
                    ),
                    "consumer_entry_point": (
                        "com.example.orders.OrderIntegrationTest#changeQuantity"
                    ),
                    "why_baseline_cannot_satisfy": (
                        "통합 검증은 parent card가 변경한 수량 규칙을 필요로 한다."
                    ),
                }
            ]
        }

        batch["dependency_audit"]["edges"][0]["consumer_entry_point"] = (
            "Order#changeQuantity"
        )
        self.assertIn(
            "IMPL_DEPENDENCY_CONSUMER_UNPROVEN:1:2",
            BATCH.validate_impl_batch(batch),
        )
        batch["dependency_audit"]["edges"][0]["consumer_entry_point"] = (
            "com.example.orders.OrderIntegrationTest#changeQuantity"
        )

        valid_errors = BATCH.validate_impl_batch(batch)
        self.assertNotIn(error, valid_errors)
        self.assertNotIn("IMPL_DEPENDENCY_ARTIFACT_UNPROVEN:1:2", valid_errors)
        self.assertNotIn("IMPL_DEPENDENCY_CONSUMER_UNPROVEN:1:2", valid_errors)

        edge = batch["dependency_audit"]["edges"][0]
        edge["required_artifact"] = (
            "com.example.orders.domain.entity.Order#changeQuantity"
        )
        child["body"]["obligations"] = copy.deepcopy(
            parent["body"]["obligations"]
        )
        self.assertNotIn(
            "IMPL_DEPENDENCY_ARTIFACT_UNPROVEN:1:2",
            BATCH.validate_impl_batch(batch),
        )

        counterfeit = (
            "com.example.orders.domain.entity.PurchaseOrder#changeQuantity"
        )
        parent["body"]["obligations"][0]["operation"]["symbol"] = counterfeit
        child["body"]["obligations"][0]["operation"]["symbol"] = counterfeit
        edge["required_artifact"] = counterfeit
        self.assertIn(
            "IMPL_DEPENDENCY_ARTIFACT_UNPROVEN:1:2",
            BATCH.validate_impl_batch(batch),
        )

        parent["body"]["obligations"][0]["operation"]["symbol"] = (
            "com.example.orders.domain.entity.Order#changeQuantity"
        )
        child["body"]["obligations"] = []

        selector = "com.example.orders.OrderIntegrationTest#changeQuantity"
        parent["body"]["verification"][0]["selectors"].append(selector)
        edge["required_artifact"] = selector
        self.assertNotIn(
            "IMPL_DEPENDENCY_ARTIFACT_UNPROVEN:1:2",
            BATCH.validate_impl_batch(batch),
        )

    def test_prepare_fast_path_uses_current_v6_writer(self) -> None:
        triage = self.frozen_triage()
        source = {
            "task": {
                "id": "t_triage",
                "title": "G1-Issue42-Triage",
                "status": "triage",
                "assignee": "project-manager",
                "skills": [],
                "workspace_kind": "dir",
                "workspace_path": _ROOT.as_posix(),
                "body": json.dumps(triage, ensure_ascii=False),
            }
        }

        prepared = BATCH.prepare_impl_fast_path(
            source,
            [],
            triage_id="t_triage",
            issue=42,
            generation=1,
            workdir=_ROOT,
        )

        self.assertEqual(
            "backend-implementation-card-v6",
            prepared["batch"]["cards"][0]["body"]["schema"],
        )

    def test_native_materialization_uses_v6_trace_slice_id(self) -> None:
        triage = self.frozen_triage()
        batch = BATCH.impl_batch_template_v6(
            triage, generation=1, triage_id="t_triage"
        )
        batch = BATCH.apply_impl_completions(batch, self.valid_completions(batch))
        wrapper = batch["cards"][0]

        temp_root = _ROOT / ".temp"
        temp_root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=temp_root) as directory:
            body_dir = Path(directory)
            for filename, body in BATCH.render_impl_batch(batch).items():
                (body_dir / filename).write_text(
                    json.dumps(body, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )

            triage_task = {
                "id": "t_triage",
                "status": "triage",
                "workspace_kind": "dir",
                "workspace_path": _ROOT.as_posix(),
                "body": json.dumps(triage, ensure_ascii=False),
            }
            impl_task = {
                "id": "t_impl",
                "title": wrapper["title"],
                "body": (body_dir / "impl-1.json").read_text(encoding="utf-8"),
                "assignee": "coder",
                "skills": [],
                "status": "todo",
                "priority": wrapper["priority"],
                "workspace_kind": "dir",
                "workspace_path": _ROOT.as_posix(),
            }

            def fake_run(command, **_kwargs):
                if "create" in command:
                    payload = {"id": "t_impl", "title": wrapper["title"]}
                elif "list" in command:
                    payload = []
                elif command[-2:] == ["t_triage", "--json"]:
                    payload = {"task": triage_task, "parents": []}
                elif command[-2:] == ["t_impl", "--json"]:
                    payload = {"task": impl_task, "parents": [{"id": "t_triage"}]}
                else:
                    self.fail(f"unexpected native command: {command}")
                return subprocess.CompletedProcess(
                    command,
                    0,
                    stdout=json.dumps(payload, ensure_ascii=False),
                    stderr="",
                )

            with patch.object(NATIVE.subprocess, "run", side_effect=fake_run):
                report = NATIVE.create_impl_batch_native(
                    batch,
                    profile="project-manager",
                    board="issue-42",
                    triage_id="t_triage",
                    body_dir=body_dir,
                    workspace_path=_ROOT,
                )

        self.assertEqual(["t_impl"], [item["id"] for item in report["created"]])

    def test_exact_production_seam_remains_behavior_with_class_evidence(self) -> None:
        triage = self.frozen_triage()
        seam_path = triage["verification_matrix"][0]["production_seam"]["locator"].split(
            "#", 1
        )[0]
        class_name = Path(seam_path).stem
        triage["verification_matrix"][0]["evidence"].append(
            {
                "locator": f"{seam_path}#{class_name}",
                "claim": "The operation owner class is confirmed.",
            }
        )
        triage = DOMAIN.freeze(
            {
                **triage,
                "phase": "analysis",
                "frozen_digest": None,
                "handoff": {"graph_allowed": False, "blocker": "freeze-required"},
            }
        )

        batch = BATCH.impl_batch_template_v6(
            triage, generation=1, triage_id="t_triage"
        )

        expected = triage["implementation_slices"][0]["acceptance"][0]["id"]
        self.assertEqual(
            [expected],
            [item["id"] for item in batch["cards"][0]["body"]["obligations"]],
        )

    def test_verification_only_slice_keeps_acceptance_as_constraint(self) -> None:
        triage = self.frozen_triage()
        triage["implementation_slices"][0]["scope"] = [
            "production change 없음; verification만 소유",
            "verify: src/main/java/com/example/orders/application/service/OrderService.java#changeQuantity 기존 동작을 검증한다.",
            "verify: src/main/java/com/example/orders/domain/entity/Order.java#changeQuantity(UUID,int) aggregate 동작을 검증한다.",
            "verify: com.example.ModularityTest#verifyModularity로 구조를 검증한다.",
        ]
        triage["verification_matrix"][0]["required_verification"].append(
            "com.example.ModularityTest#verifyModularity"
        )
        triage = DOMAIN.freeze(
            {
                **triage,
                "phase": "analysis",
                "frozen_digest": None,
                "handoff": {"graph_allowed": False, "blocker": "freeze-required"},
            }
        )

        batch = BATCH.impl_batch_template_v6(
            triage, generation=1, triage_id="t_triage"
        )
        body = batch["cards"][0]["body"]
        expected = triage["implementation_slices"][0]["acceptance"][0]["id"]

        self.assertEqual([], body["obligations"])
        self.assertIn(expected, [item["id"] for item in body["constraints"]])


if __name__ == "__main__":
    unittest.main()
