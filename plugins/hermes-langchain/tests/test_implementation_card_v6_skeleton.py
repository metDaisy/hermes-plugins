"""Frozen triage-v7 to implementation-card-v6 authoring skeleton tests."""

from __future__ import annotations

import copy
import hashlib
import json
import unittest
from pathlib import Path

from tests.support import load_module


_ROOT = Path(__file__).resolve().parents[1]
CONTRACT = load_module("authoring.graph.implementation_card_v6")
TRIAGE = load_module("authoring.triage_domain")


class ImplementationCardV6SkeletonTest(unittest.TestCase):
    def frozen_issue151_triage(self, *, include_process_scope: bool = False):
        authoring = json.loads(
            (_ROOT / "fixtures" / "valid-triage-authoring.json").read_text(encoding="utf-8")
        )
        if include_process_scope:
            authoring["analysis"]["implementation_slices"][0]["scope"].extend(
                [
                    "RED: 실패 테스트를 먼저 작성한다.",
                    "GREEN: 최소 구현으로 통과시킨다.",
                    "검증 CHECK/EXPECT: 전체 검증을 실행한다.",
                    "완료 증거: handoff에 결과를 기록한다.",
                ]
            )
        identity = authoring["identity"]
        body = TRIAGE.template(
            identity["issue_number"],
            identity["title"],
            identity["issue_url"],
            identity["planning_sha"],
            identity["current_state_sha"],
            identity["requirement_locators"],
            identity["current_state_locators"],
        )
        intake = TRIAGE.compose_intake(body, authoring["intake"])
        analysis = TRIAGE.compose_analysis(intake, authoring["analysis"])
        return TRIAGE.freeze(analysis)

    def frozen_offer_integration_regression_triage(self):
        triage = self.frozen_issue151_triage()
        gap = triage["confirmed_gaps"][0]
        gap["evidence"] = [
            {
                "locator": "src/main/java/io/github/metdaisy/amaazon/offer/application/api/OfferQueryApi.java#OfferQueryApi.requireCurrentOffer",
                "claim": "Offer producer required lookup and Offer-owned not-found contract already exist and are published.",
            },
            {
                "locator": "src/main/java/io/github/metdaisy/amaazon/cart/infra/adapter/OfferQueryAdapter.java#OfferQueryAdapter.findById",
                "claim": "The remaining gap is the Cart adapter's tolerant Optional consumer path.",
            },
        ]
        matrix = triage["verification_matrix"][0]
        matrix["evidence"] = [
            {
                "locator": "src/test/java/io/github/metdaisy/amaazon/offer/application/api/OfferQueryApiIntegrationTest.java#OfferQueryApiIntegrationTest.requiredLookupThrowsOfferOwnedNotFoundContract",
                "claim": "Concrete producer contract regression method.",
            },
            {
                "locator": "src/test/java/io/github/metdaisy/amaazon/ModularityTest.java#ModularityTest",
                "claim": "Class-only selector must not be treated as executable proof.",
            },
        ]
        canonical = copy.deepcopy(triage)
        canonical["frozen_digest"] = None
        payload = json.dumps(
            canonical,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        triage["frozen_digest"] = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        return triage

    def test_derives_known_issue151_facts_without_changing_triage(self):
        triage = self.frozen_issue151_triage()
        original = copy.deepcopy(triage)

        cards = CONTRACT.skeletons_from_frozen_triage(triage)

        self.assertEqual(original, triage)
        self.assertEqual(1, len(cards))
        card = cards[0]
        slice_body = triage["implementation_slices"][0]
        self.assertEqual("backend-implementation-card-v6", card["schema"])
        self.assertEqual(
            {
                "triage_digest": triage["frozen_digest"],
                "slice_id": "SLICE-001",
                "requirement_ids": ["CHANGE-001"],
                "observable_ids": ["OBS-001"],
                "gap_ids": ["GAP-001"],
            },
            card["trace"],
        )
        self.assertEqual({"outcome": slice_body["outcome"]}, card["objective"])
        self.assertEqual(
            [*slice_body["scope"]],
            card["boundary"]["in_scope"],
        )
        self.assertEqual(
            [*triage["intent"]["out_of_scope"], *slice_body["out_of_scope"]],
            card["boundary"]["out_of_scope"],
        )
        self.assertEqual("GAP-001-FACT-001", card["constraints"][0]["id"])
        self.assertEqual(
            triage["confirmed_gaps"][0]["evidence"][0]["claim"],
            card["constraints"][0]["rule"],
        )
        self.assertEqual(
            triage["confirmed_gaps"][0]["evidence"][0],
            card["constraints"][0]["source"],
        )
        self.assertEqual("GAP-001-RULE-001", card["constraints"][1]["id"])
        self.assertEqual(
            triage["confirmed_gaps"][0]["required_state"],
            card["constraints"][1]["rule"],
        )
        self.assertEqual(
            triage["confirmed_gaps"][0]["rules"][0],
            card["constraints"][1]["source"],
        )

    def test_marks_non_derivable_proof_details_as_unresolved(self):
        card = CONTRACT.skeletons_from_frozen_triage(self.frozen_issue151_triage())[0]

        obligation = card["obligations"][0]
        self.assertEqual("ACCEPT-001", obligation["id"])
        self.assertEqual(
            "io.github.metdaisy.amaazon.cart.domain.entity.Cart#changeQuantity",
            obligation["operation"]["symbol"],
        )
        self.assertEqual(0, obligation["operation"]["invocation_count"])
        self.assertEqual([], obligation["given"])
        self.assertEqual([], obligation["observables"])
        self.assertEqual(
            ["CartService가 CartItem.changeQuantity를 직접 호출하지 않는다."],
            obligation["expected"],
        )

        verification = card["verification"][0]
        self.assertEqual("VERIFY-001", verification["id"])
        self.assertEqual(
            ["ACCEPT-001", "GAP-001-FACT-001", "GAP-001-RULE-001"],
            verification["covers"],
        )
        self.assertEqual("", verification["level"])
        self.assertEqual(
            ["io.github.metdaisy.amaazon.cart.domain.entity.CartTest#quantityZeroDeletes"],
            verification["selectors"],
        )

        errors = CONTRACT.validate(card)
        self.assertIn("INVALID_OBLIGATION_OPERATION_INVOCATION_COUNT:ACCEPT-001", errors)
        self.assertIn("MISSING_OBLIGATION_OBSERVABLES:ACCEPT-001", errors)
        self.assertIn("INVALID_VERIFICATION_LEVEL:VERIFY-001", errors)
        self.assertNotIn("MISSING_TRACE_REQUIREMENT_IDS", errors)
        self.assertNotIn("MISSING_TRACE_OBSERVABLE_IDS", errors)
        self.assertNotIn("MISSING_TRACE_GAP_IDS", errors)

    def test_preserves_existing_offer_contract_fact_and_only_concrete_test_methods(self):
        card = CONTRACT.skeletons_from_frozen_triage(
            self.frozen_offer_integration_regression_triage()
        )[0]

        constraints = {item["id"]: item for item in card["constraints"]}
        self.assertEqual(
            "Offer producer required lookup and Offer-owned not-found contract already exist and are published.",
            constraints["GAP-001-FACT-001"]["rule"],
        )
        self.assertEqual(
            "The remaining gap is the Cart adapter's tolerant Optional consumer path.",
            constraints["GAP-001-FACT-002"]["rule"],
        )
        self.assertEqual(
            [
                "io.github.metdaisy.amaazon.offer.application.api.OfferQueryApiIntegrationTest#requiredLookupThrowsOfferOwnedNotFoundContract"
            ],
            card["verification"][0]["selectors"],
        )

    def test_structural_acceptance_is_a_constraint_without_fake_runtime_operation(self):
        triage = self.frozen_offer_integration_regression_triage()
        triage["verification_matrix"][0]["production_seam"] = {
            "locator": "src/main/java/io/github/metdaisy/amaazon/cart/application/service/CartService.java#CartService.requirePurchasableOffer",
            "claim": "The local conversion method is the seam to remove.",
        }
        triage["verification_matrix"][0]["evidence"].insert(
            0,
            {
                "locator": "src/main/java/io/github/metdaisy/amaazon/cart/application/port/out/OfferNotFoundException.java#OfferNotFoundException",
                "claim": "The duplicate Cart-owned exception definition must be absent.",
            },
        )
        canonical = copy.deepcopy(triage)
        canonical["frozen_digest"] = None
        payload = json.dumps(
            canonical,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        triage["frozen_digest"] = hashlib.sha256(payload.encode("utf-8")).hexdigest()

        card = CONTRACT.skeletons_from_frozen_triage(triage)[0]
        card["verification"][0]["level"] = "modulith"

        self.assertEqual([], card["obligations"])
        structural = next(item for item in card["constraints"] if item["id"] == "ACCEPT-001")
        self.assertEqual(
            triage["implementation_slices"][0]["acceptance"][0]["criterion"],
            structural["rule"],
        )
        self.assertIn("ACCEPT-001", card["verification"][0]["covers"])
        self.assertEqual([], CONTRACT.validate(card))

    def test_boundary_excludes_workflow_owned_scope_instructions(self):
        card = CONTRACT.skeletons_from_frozen_triage(
            self.frozen_issue151_triage(include_process_scope=True)
        )[0]

        self.assertEqual(
            [
                "CartService의 changeQuantity 경로를 수정한다.",
                "Cart aggregate의 이름 있는 behavior를 사용한다.",
            ],
            card["boundary"]["in_scope"],
        )

    def test_rejects_non_frozen_or_wrong_schema_triage(self):
        triage = self.frozen_issue151_triage()
        triage["phase"] = "analysis"
        with self.assertRaisesRegex(ValueError, "FROZEN_TRIAGE_REQUIRED"):
            CONTRACT.skeletons_from_frozen_triage(triage)

        triage = self.frozen_issue151_triage()
        triage["schema"] = "triage-v8"
        with self.assertRaisesRegex(ValueError, "FROZEN_TRIAGE_REQUIRED"):
            CONTRACT.skeletons_from_frozen_triage(triage)

    def test_rejects_frozen_triage_changed_after_digest(self):
        triage = self.frozen_issue151_triage()
        triage["implementation_slices"][0]["outcome"] = "동결 후 변경"

        with self.assertRaisesRegex(ValueError, "FROZEN_TRIAGE_DIGEST_MISMATCH"):
            CONTRACT.skeletons_from_frozen_triage(triage)

    def test_java_class_locator_is_not_treated_as_concrete_test_method(self):
        self.assertEqual(
            "",
            CONTRACT._java_locator_symbol(
                "src/test/java/io/github/metdaisy/amaazon/cart/domain/entity/CartTest.java#CartTest",
                "test",
            )
        )


if __name__ == "__main__":
    unittest.main()
