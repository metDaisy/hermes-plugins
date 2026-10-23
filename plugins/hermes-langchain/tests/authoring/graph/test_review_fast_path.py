"""Review and Summary source-closure tests for Impl card v6."""

from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from tests.support import load_module


_ROOT = Path(__file__).resolve().parents[3]
REVIEW_FAST = load_module("authoring.graph.review_fast_path")
REVIEW_LINK = load_module("authoring.graph.review_link_validation")
IMPLEMENTATION = load_module("authoring.graph.legacy_implementation_card")
NATIVE_AUTHORING = load_module("authoring.graph.native_authoring")


class ReviewFastPathV6Test(unittest.TestCase):
    def setUp(self) -> None:
        self.digest = "a" * 64
        self.issue = 42
        self.workspace = _ROOT.as_posix()
        self.triage = {
            "schema": "triage-v7",
            "phase": "frozen",
            "frozen_digest": self.digest,
            "issue": {
                "number": self.issue,
                "title": "Order quantity change",
                "url": f"https://github.com/example/repository/issues/{self.issue}",
            },
            "baseline": {"planning_sha": "b" * 40},
            "intent": {
                "goal": "Order 수량 변경 책임을 정렬한다.",
                "change_requirements": [
                    {"id": "CHANGE-001", "outcome": "Order 동작을 aggregate에 위임한다."}
                ],
                "preserved_observables": [
                    {"id": "OBS-001", "behavior": "기존 Order 수량 변경 결과를 보존한다."}
                ],
                "out_of_scope": ["Inventory 내부 정책 변경은 제외한다."],
            },
        }
        self.impl = {
            "schema": "backend-implementation-card-v6",
            "trace": {
                "triage_digest": self.digest,
                "slice_id": "SLICE-001",
                "requirement_ids": ["CHANGE-001"],
                "observable_ids": ["OBS-001"],
                "gap_ids": ["GAP-001"],
            },
            "objective": {"outcome": "Order aggregate가 수량 변경 규칙을 소유한다."},
            "boundary": {
                "in_scope": ["Order#changeQuantity와 검증 test를 구현한다."],
                "out_of_scope": ["Inventory 정책은 변경하지 않는다."],
            },
            "obligations": [
                {
                    "id": "ACCEPT-001",
                    "kind": "behavior",
                    "given": ["수량을 변경할 주문이 존재한다."],
                    "operation": {
                        "symbol": "com.example.orders.domain.entity.Order#changeQuantity",
                        "invocation_count": 1,
                    },
                    "expected": ["수량이 0이면 주문 항목이 제거된다."],
                    "observables": [
                        {
                            "target": "Order items",
                            "source": "in-memory-state",
                            "comparison": "exact-map",
                            "identity": ["OrderItem.id"],
                            "values": ["quantity"],
                            "expected": "zero quantity item is removed",
                        }
                    ],
                }
            ],
            "constraints": [
                {
                    "id": "GAP-001-RULE-001",
                    "rule": "Application은 aggregate의 이름 있는 behavior를 호출한다.",
                    "source": {
                        "locator": "docs/backend-development-guide.md#Application 구현 규칙",
                        "claim": "Application은 aggregate behavior를 호출한다.",
                    },
                }
            ],
            "verification": [
                {
                    "id": "VERIFY-001",
                    "covers": ["ACCEPT-001", "GAP-001-RULE-001"],
                    "level": "unit",
                    "selectors": [
                        "com.example.orders.domain.entity.OrderTest#quantityZeroDeletes"
                    ],
                }
            ],
        }

    def task(self, task_id: str, title: str, body: dict, assignee: str) -> dict:
        return {
            "id": task_id,
            "title": title,
            "body": json.dumps(body, ensure_ascii=False),
            "assignee": assignee,
            "skills": [],
            "status": "todo",
            "priority": 1,
            "workspace_kind": "dir",
            "workspace_path": self.workspace,
        }

    def source_tasks(self) -> list[dict]:
        triage = self.task(
            "t_triage", f"G1-Issue{self.issue}-Triage", self.triage, "project-manager"
        )
        triage["status"] = "triage"
        return [
            triage,
            self.task("t_impl", f"G1-Issue{self.issue}-Impl1", self.impl, "coder"),
        ]

    def test_review_source_reads_v6_trace_and_contract_ids(self) -> None:
        bundle = REVIEW_FAST.prepare_review_fast_path(
            self.source_tasks(),
            triage_id="t_triage",
            implementation_ids=["t_impl"],
            issue=self.issue,
            generation=1,
            validate_triage=lambda body: [],
            validate_implementation=IMPLEMENTATION._validate_implementation_body,
            guide_locators={
                "docs/backend-development-guide.md#4. 계층별 설계 원칙",
                "docs/backend-test-guide.md#1. 공통 규칙",
                ".hermes/docs/plan.md#ACTIVE HANDOFF — 범용 graph 기반 KWC workflow runtime 설계",
            },
        )

        source = bundle["implementation_cards"][0]
        expected_contract_ids = {
            item["id"] for item in self.impl["obligations"] + self.impl["constraints"]
        }
        self.assertEqual(["OBS-001"], source["behavior_ids"])
        self.assertEqual(expected_contract_ids, set(source["acceptance_ids"]))
        self.assertEqual(
            {item["id"] for item in self.impl["verification"]},
            set(source["verification_ids"]),
        )

    def test_native_review_prepare_accepts_v6_implementation_body(self) -> None:
        original = NATIVE_AUTHORING._validate_frozen_triage
        NATIVE_AUTHORING._validate_frozen_triage = lambda body: []
        try:
            bundle = NATIVE_AUTHORING.prepare_review_fast_path(
                self.source_tasks(),
                triage_id="t_triage",
                implementation_ids=["t_impl"],
                issue=self.issue,
                generation=1,
            )
        finally:
            NATIVE_AUTHORING._validate_frozen_triage = original

        self.assertEqual(["impl-1"], bundle["review_body_template"]["implementation_card_keys"])

    def test_summary_accepts_v6_impl_parent_and_trace_digest(self) -> None:
        tasks = self.source_tasks()
        review_body = {
            "schema": "aggregate-review-card-v5",
            "triage_digest": self.digest,
        }
        tasks.append(
            self.task("t_review", f"G1-Issue{self.issue}-Review1", review_body, "reviewer")
        )

        bundle = REVIEW_FAST.prepare_summary_fast_path(
            tasks,
            triage_id="t_triage",
            issue=self.issue,
            generation=1,
            validate_triage=lambda body: [],
        )

        self.assertEqual(["t_impl", "t_review"], bundle["parent_ids"])
        self.assertEqual(self.digest, bundle["body"]["triage_digest"])

    def test_review_link_validation_collects_v6_source_closure(self) -> None:
        contract_ids = [
            item["id"] for item in self.impl["obligations"] + self.impl["constraints"]
        ]
        verification_ids = [item["id"] for item in self.impl["verification"]]
        errors: list[str] = []

        sources = REVIEW_LINK._collect_review_sources(
            "review-1",
            {"impl-1": {"card_type": "implementation", "body": self.impl}},
            {"impl-1"},
            errors,
        )

        self.assertEqual([], errors)
        self.assertEqual(set(contract_ids), sources.acceptance_ids)
        self.assertEqual(set(verification_ids), sources.verification_ids)
        self.assertEqual({"OBS-001"}, sources.implementation_behavior_ids)


if __name__ == "__main__":
    unittest.main()
