"""Contract tests for the minimal backend implementation card v6."""

from __future__ import annotations

import copy
import unittest
from pathlib import Path


_ROOT = Path(__file__).resolve().parents[1]
from tests.support import load_module

CONTRACT = load_module("authoring.graph.implementation_card_v6")


class ImplementationCardV6ContractTest(unittest.TestCase):
    @staticmethod
    def valid_card():
        return {
            "schema": "backend-implementation-card-v6",
            "trace": {
                "triage_digest": "a" * 64,
                "slice_id": "SLICE-005",
                "requirement_ids": ["CHANGE-002"],
                "observable_ids": ["OBS-009"],
                "gap_ids": ["GAP-003"],
            },
            "objective": {
                "outcome": "Cart 병합은 전체 후보를 검증한 뒤 전체 적용 또는 상태 불변을 보장한다."
            },
            "boundary": {
                "in_scope": ["Cart 병합 동작과 관련 테스트"],
                "out_of_scope": ["공개 Cart API signature 변경"],
            },
            "obligations": [
                {
                    "id": "OB-MERGE-CONFLICT",
                    "kind": "behavior",
                    "given": ["Offer 최대 수량과 Cart 전체 수량 충돌이 동시에 존재한다."],
                    "operation": {
                        "symbol": "io.github.metdaisy.amaazon.cart.application.service.CartService#mergeGuestCart",
                        "invocation_count": 1,
                    },
                    "expected": [
                        "두 충돌 Offer ID를 정확히 반환한다.",
                        "member와 guest CartItem 상태가 변하지 않는다.",
                    ],
                    "observables": [
                        {
                            "target": "merge result conflict Offer IDs",
                            "source": "return-value",
                            "comparison": "exact-set",
                            "identity": [],
                            "values": [],
                            "expected": "fixture conflict Offer IDs",
                        },
                        {
                            "target": "persisted member CartItem state",
                            "source": "persisted-reload",
                            "comparison": "unchanged",
                            "identity": ["CartItem.id"],
                            "values": ["quantity"],
                            "expected": "before state",
                        },
                    ],
                }
            ],
            "constraints": [
                {
                    "id": "CONSTRAINT-AGGREGATE",
                    "rule": "Cart collection invariant는 Cart aggregate가 소유한다.",
                    "source": {
                        "locator": "docs/backend-development-guide.md#Domain 구현 규칙",
                        "claim": "Aggregate owns collection invariants.",
                    },
                }
            ],
            "verification": [
                {
                    "id": "VERIFY-MERGE-CONFLICT",
                    "covers": ["OB-MERGE-CONFLICT", "CONSTRAINT-AGGREGATE"],
                    "level": "unit",
                    "selectors": [
                        "io.github.metdaisy.amaazon.cart.application.service.CartServiceTest#mergeReturnsExactConflicts"
                    ],
                }
            ],
        }

    def test_accepts_minimal_executable_contract(self):
        self.assertEqual([], CONTRACT.validate(self.valid_card()))

    def test_template_contains_only_minimal_v6_sections(self):
        card = CONTRACT.template()

        self.assertEqual(
            {
                "schema",
                "trace",
                "objective",
                "boundary",
                "obligations",
                "constraints",
                "verification",
            },
            set(card),
        )
        self.assertEqual("backend-implementation-card-v6", card["schema"])
        self.assertNotIn("acceptance", card)
        self.assertNotIn("implementation_contract", card)

    def test_rejects_legacy_or_extra_top_level_fields(self):
        card = self.valid_card()
        card["schema"] = "backend-implementation-card-v5"
        card["acceptance"] = []

        errors = CONTRACT.validate(card)

        self.assertIn("SCHEMA_VERSION", errors)
        self.assertIn("UNEXPECTED_CARD_FIELD:acceptance", errors)

    def test_requires_atomic_obligations_to_be_covered(self):
        card = self.valid_card()
        second = copy.deepcopy(card["obligations"][0])
        second["id"] = "OB-MERGE-EXPIRATION"
        card["obligations"].append(second)

        self.assertIn("UNCOVERED_OBLIGATION:OB-MERGE-EXPIRATION", CONTRACT.validate(card))

    def test_requires_constraints_to_be_covered(self):
        card = self.valid_card()
        second = copy.deepcopy(card["constraints"][0])
        second["id"] = "CONSTRAINT-PUBLIC-API"
        card["constraints"].append(second)

        self.assertIn("UNCOVERED_CONSTRAINT:CONSTRAINT-PUBLIC-API", CONTRACT.validate(card))

    def test_contract_ids_are_unique_across_sections(self):
        card = self.valid_card()
        card["constraints"][0]["id"] = "OB-MERGE-CONFLICT"
        card["verification"][0]["covers"] = ["OB-MERGE-CONFLICT"]

        self.assertIn("DUPLICATE_CONTRACT_ID:OB-MERGE-CONFLICT", CONTRACT.validate(card))

    def test_rejects_unknown_verification_coverage(self):
        card = self.valid_card()
        card["verification"][0]["covers"] = ["OB-UNKNOWN"]

        errors = CONTRACT.validate(card)

        self.assertIn("UNKNOWN_VERIFICATION_TARGET:VERIFY-MERGE-CONFLICT:OB-UNKNOWN", errors)
        self.assertIn("UNCOVERED_OBLIGATION:OB-MERGE-CONFLICT", errors)

    def test_requires_positive_production_invocation_count(self):
        card = self.valid_card()
        card["obligations"][0]["operation"]["invocation_count"] = 0

        self.assertIn(
            "INVALID_OBLIGATION_OPERATION_INVOCATION_COUNT:OB-MERGE-CONFLICT",
            CONTRACT.validate(card),
        )

    def test_structural_rules_are_constraints_not_operation_obligations(self):
        card = self.valid_card()
        card["obligations"][0]["kind"] = "structure"

        self.assertIn(
            "INVALID_OBLIGATION_KIND:OB-MERGE-CONFLICT",
            CONTRACT.validate(card),
        )

    def test_requires_canonical_production_operation_symbol(self):
        card = self.valid_card()
        card["obligations"][0]["operation"]["symbol"] = "perform the merge"

        self.assertIn(
            "INVALID_OBLIGATION_OPERATION_SYMBOL:OB-MERGE-CONFLICT",
            CONTRACT.validate(card),
        )

    def test_persisted_unchanged_state_requires_identity_and_values(self):
        card = self.valid_card()
        observable = card["obligations"][0]["observables"][1]
        observable["identity"] = []
        observable["values"] = []

        errors = CONTRACT.validate(card)

        self.assertIn("MISSING_OBSERVABLE_IDENTITY:OB-MERGE-CONFLICT:1", errors)
        self.assertIn("MISSING_OBSERVABLE_VALUES:OB-MERGE-CONFLICT:1", errors)

    def test_observable_expected_cannot_be_blank_prose(self):
        card = self.valid_card()
        card["obligations"][0]["observables"][0]["expected"] = "   "

        self.assertIn(
            "MISSING_OBSERVABLE_EXPECTED:OB-MERGE-CONFLICT:0",
            CONTRACT.validate(card),
        )

    def test_rejects_placeholder_observable_target(self):
        card = self.valid_card()
        card["obligations"][0]["observables"][0]["target"] = (
            "OB-MERGE-CONFLICT observable result"
        )

        self.assertIn(
            "PLACEHOLDER_OBSERVABLE_TARGET:OB-MERGE-CONFLICT:0",
            CONTRACT.validate(card),
        )

    def test_accepts_thrown_exception_as_a_direct_observable_source(self):
        card = self.valid_card()
        observable = card["obligations"][0]["observables"][0]
        observable.update(
            {
                "target": "thrown OfferNotFoundException contract",
                "source": "thrown-exception",
                "comparison": "exact",
                "identity": ["exception.class", "logDetails.offerId"],
                "values": ["code", "clientMessage", "systemMessage", "errorType"],
                "expected": "OfferNotFoundException, OFFER-001, 판매 조건을 찾을 수 없습니다., Offer 식별자 조회 실패 [offerId: fixture], NOT_FOUND",
            }
        )

        self.assertEqual([], CONTRACT.validate(card))

    def test_rejects_invalid_selector_and_duplicate_ids(self):
        card = self.valid_card()
        card["verification"][0]["selectors"] = ["run relevant tests"]
        duplicate = copy.deepcopy(card["obligations"][0])
        card["obligations"].append(duplicate)

        errors = CONTRACT.validate(card)

        self.assertIn("INVALID_VERIFICATION_SELECTOR:VERIFY-MERGE-CONFLICT", errors)
        self.assertIn("DUPLICATE_OBLIGATION_ID:OB-MERGE-CONFLICT", errors)

    def test_rejects_class_only_and_constructor_like_test_selectors(self):
        for selector in (
            "io.github.metdaisy.amaazon.cart.domain.entity.CartTest",
            "io.github.metdaisy.amaazon.cart.domain.entity.CartTest#CartTest",
        ):
            with self.subTest(selector=selector):
                card = self.valid_card()
                card["verification"][0]["selectors"] = [selector]

                self.assertIn(
                    "INVALID_VERIFICATION_SELECTOR:VERIFY-MERGE-CONFLICT",
                    CONTRACT.validate(card),
                )


if __name__ == "__main__":
    unittest.main()
