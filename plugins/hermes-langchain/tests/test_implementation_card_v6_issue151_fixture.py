"""Semantic contract checks for the Issue-151 Impl5 v6 authoring fixture."""

from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path


_ROOT = Path(__file__).resolve().parents[1]
from tests.support import load_module

CONTRACT = load_module("authoring.graph.implementation_card_v6")


class Issue151Impl5V6FixtureTest(unittest.TestCase):
    def setUp(self):
        self.card = json.loads(
            (_ROOT / "fixtures" / "issue151-impl5-v6.json").read_text(encoding="utf-8")
        )
        self.obligations = {item["id"]: item for item in self.card["obligations"]}
        self.verification = {item["id"]: item for item in self.card["verification"]}

    @staticmethod
    def _by_id(items, identifier):
        return next(
            item
            for item in items
            if item.get("id") == identifier or item.get("target") == identifier
        )

    def _apply_regression(self, regression):
        card = copy.deepcopy(self.card)
        current = card
        for segment in regression["path"][:-1]:
            if isinstance(current, list):
                current = self._by_id(current, segment)
            else:
                current = current[segment]
        current[regression["path"][-1]] = regression["value"]
        return card

    def _semantic_findings(self, card):
        obligations = {item["id"]: item for item in card["obligations"]}
        constraints = {item["id"]: item for item in card["constraints"]}
        findings = []
        aggregate = obligations["OB-AGGREGATE-BATCH"]
        service = obligations["OB-SERVICE-COMBINED-CONFLICT"]
        concurrency = obligations["OB-CONCURRENT-MERGE"]
        success = obligations["OB-SUCCESS-MERGE-EXPIRATION"]
        expired = obligations["OB-EXPIRED-MERGE"]
        cleanup = obligations["OB-PAYMENT-CLEANUP"]
        aggregate_targets = {item["target"]: item for item in aggregate["observables"]}
        service_targets = {item["target"]: item for item in service["observables"]}
        concurrency_targets = {item["target"]: item for item in concurrency["observables"]}
        success_targets = {item["target"]: item for item in success["observables"]}
        expired_targets = {item["target"]: item for item in expired["observables"]}

        if aggregate["operation"]["invocation_count"] != 1:
            findings.append("AGGREGATE_NOT_SINGLE_CALL")
        if aggregate_targets["combined conflict Offer IDs"]["comparison"] != "exact-set":
            findings.append("COMBINED_CONFLICT_NOT_EXACT_SET")
        if service["operation"]["symbol"] != "io.github.metdaisy.amaazon.cart.application.service.CartService#mergeGuestCart":
            findings.append("SERVICE_OPERATION_NOT_PRODUCTION_ENTRY")
        if not any("missing, unavailable" in item for item in service["given"]):
            findings.append("UNRELATED_GUARDS_NOT_NEUTRALIZED")
        member_state = service_targets["member CartItem state"]
        if member_state["identity"] != ["CartItem.id"] or member_state["values"] != ["quantity"]:
            findings.append("REJECTED_STATE_IDENTITY_NOT_EXACT")
        if "pre-release timeout" not in concurrency_targets["concurrent call ordering"]["expected"]:
            findings.append("CONCURRENCY_ORDER_NOT_DETERMINISTIC")
        if success_targets["guest expiration after successful merge"]["expected"] != "operationTime + Cart.GUEST_LIFETIME":
            findings.append("RENEWAL_NOT_OPERATION_TIME_BASED")
        if expired_targets["member/Offer/save interactions"]["expected"] != "0 member reads, 0 Offer reads, 0 saves":
            findings.append("EXPIRED_SIDE_EFFECTS_NOT_ZERO")
        if cleanup["operation"]["invocation_count"] != 2:
            findings.append("CLEANUP_NOT_REPEATED")
        if "CartMergeApi와 CartPaymentCleanupApi만 사용" not in constraints["CONSTRAINT-MODULE-PUBLIC-API"]["rule"]:
            findings.append("MODULE_PUBLIC_API_NOT_PRESERVED")
        return findings

    def test_regression_mutations_recreate_each_past_impl5_proof_defect(self):
        regressions = json.loads(
            (_ROOT / "fixtures" / "issue151-impl5-v6-regressions.json").read_text(encoding="utf-8")
        )

        self.assertEqual([], self._semantic_findings(self.card))
        for regression in regressions:
            with self.subTest(regression=regression["id"]):
                defective = self._apply_regression(regression)
                self.assertEqual([], CONTRACT.validate(defective))
                self.assertIn(regression["finding"], self._semantic_findings(defective))

    def test_fixture_is_a_complete_valid_v6_contract(self):
        self.assertEqual([], CONTRACT.validate(self.card))
        self.assertEqual("SLICE-005", self.card["trace"]["slice_id"])
        self.assertEqual(["GAP-003"], self.card["trace"]["gap_ids"])
        self.assertEqual(7, len(self.obligations))
        self.assertEqual(7, len(self.verification))

    def test_combined_conflict_is_one_causal_production_invocation(self):
        obligation = self.obligations["OB-SERVICE-COMBINED-CONFLICT"]

        self.assertEqual(
            {
                "symbol": "io.github.metdaisy.amaazon.cart.application.service.CartService#mergeGuestCart",
                "invocation_count": 1,
            },
            obligation["operation"],
        )
        self.assertTrue(any("동시에" in item for item in obligation["given"]))
        targets = {item["target"]: item for item in obligation["observables"]}
        self.assertEqual("exact-set", targets["merge conflict Offer IDs"]["comparison"])
        self.assertEqual(["CartItem.id"], targets["member CartItem state"]["identity"])
        self.assertEqual(["quantity"], targets["member CartItem state"]["values"])
        self.assertEqual("1", targets["Cart.mergeItems calls"]["expected"])
        self.assertEqual("0", targets["CartRepository save calls"]["expected"])
        self.assertEqual("0", targets["CartRepository delete calls"]["expected"])

    def test_aggregate_batch_proves_exact_success_conflict_and_rejection_state(self):
        obligation = self.obligations["OB-AGGREGATE-BATCH"]

        self.assertEqual(
            {
                "symbol": "io.github.metdaisy.amaazon.cart.domain.entity.Cart#mergeItems",
                "invocation_count": 1,
            },
            obligation["operation"],
        )
        self.assertTrue(any("서로 독립적으로 도달 가능" in item for item in obligation["given"]))
        targets = {item["target"]: item for item in obligation["observables"]}
        self.assertEqual("exact-map", targets["successful Cart items"]["comparison"])
        self.assertEqual(["CartItem.id"], targets["successful Cart items"]["identity"])
        self.assertEqual(["quantity"], targets["successful Cart items"]["values"])
        self.assertEqual("exact-set", targets["combined conflict Offer IDs"]["comparison"])
        self.assertEqual("unchanged", targets["Cart items after rejected batch"]["comparison"])

    def test_service_conflict_neutralizes_unrelated_guards(self):
        obligation = self.obligations["OB-SERVICE-COMBINED-CONFLICT"]

        self.assertTrue(any("missing, unavailable" in item for item in obligation["given"]))

    def test_concurrency_obligation_names_start_signal_and_persisted_result(self):
        obligation = self.obligations["OB-CONCURRENT-MERGE"]

        self.assertTrue(any("competingStarted" in item for item in obligation["given"]))
        targets = {item["target"]: item for item in obligation["observables"]}
        self.assertEqual("ordered", targets["concurrent call ordering"]["comparison"])
        self.assertEqual("persisted-reload", targets["persisted member CartItem state"]["source"])
        self.assertEqual(["CartItem.id"], targets["persisted member CartItem state"]["identity"])
        self.assertEqual("absent", targets["persisted guest Cart"]["comparison"])

    def test_expiration_proof_is_split_into_failed_success_and_expired_operations(self):
        failed = self.obligations["OB-FAILED-MERGE-EXPIRATION"]
        success = self.obligations["OB-SUCCESS-MERGE-EXPIRATION"]
        expired = self.obligations["OB-EXPIRED-MERGE"]

        self.assertEqual(1, failed["operation"]["invocation_count"])
        self.assertEqual(1, success["operation"]["invocation_count"])
        self.assertEqual(1, expired["operation"]["invocation_count"])
        success_targets = {item["target"]: item for item in success["observables"]}
        self.assertEqual(
            "operationTime + Cart.GUEST_LIFETIME",
            success_targets["guest expiration after successful merge"]["expected"],
        )
        expired_targets = {item["target"]: item for item in expired["observables"]}
        self.assertEqual("MERGED", expired_targets["expired merge status"]["expected"])
        self.assertEqual(["CartItem.id"], expired_targets["expired guest original CartItem state"]["identity"])
        self.assertEqual(["quantity"], expired_targets["expired guest original CartItem state"]["values"])
        self.assertEqual("1", expired_targets["expired guest deletion"]["expected"])
        self.assertEqual("0 member reads, 0 Offer reads, 0 saves", expired_targets["member/Offer/save interactions"]["expected"])

    def test_cleanup_and_module_constraints_have_independent_proof(self):
        cleanup = self.obligations["OB-PAYMENT-CLEANUP"]
        cleanup_targets = {item["target"]: item for item in cleanup["observables"]}
        constraints = {item["id"] for item in self.card["constraints"]}

        self.assertEqual(2, cleanup["operation"]["invocation_count"])
        self.assertEqual("1", cleanup_targets["first cleanup deleted count"]["expected"])
        self.assertEqual("0", cleanup_targets["second cleanup deleted count"]["expected"])
        self.assertEqual("present", cleanup_targets["persisted retained item"]["comparison"])
        self.assertEqual(
            {
                "CONSTRAINT-AGGREGATE-OWNER",
                "CONSTRAINT-LOCKED-TRANSACTION",
                "CONSTRAINT-LIFECYCLE-PRESERVATION",
                "CONSTRAINT-MODULE-PUBLIC-API",
            },
            constraints,
        )
        self.assertIn("CONSTRAINT-LIFECYCLE-PRESERVATION", self.verification["VERIFY-012"]["covers"])
        self.assertEqual(
            ["CONSTRAINT-MODULE-PUBLIC-API"],
            self.verification["VERIFY-014"]["covers"],
        )

    def test_fixture_uses_exact_approved_selectors_and_no_workflow_prose(self):
        selectors = {
            selector
            for verification in self.card["verification"]
            for selector in verification["selectors"]
        }
        self.assertEqual(
            {
                "io.github.metdaisy.amaazon.cart.domain.entity.CartTest#mergeItemsValidatesWholeBatchBeforeMutation",
                "io.github.metdaisy.amaazon.cart.application.service.CartServiceTest#mergeReturnsExactConflictsWithoutDeletingGuestCart",
                "io.github.metdaisy.amaazon.cart.presentation.controller.CartIntegrationTest#mergeIsAtomicUnderConflict",
                "io.github.metdaisy.amaazon.cart.infra.repository.CartJpaRepositoryTest#mergeLoadsOwnersWithWriteLock",
                "io.github.metdaisy.amaazon.cart.application.service.CartServiceTest#guestExpirationChangesOnlyAfterSuccessfulMutation",
                "io.github.metdaisy.amaazon.cart.infra.repository.CartJpaRepositoryTest#selectedPaymentCleanupIsIdempotent",
                "io.github.metdaisy.amaazon.ModularityTest#verifyModularStructure",
            },
            selectors,
        )
        boundary_text = "\n".join(self.card["boundary"]["in_scope"])
        self.assertEqual(3, len(self.card["boundary"]["in_scope"]))
        self.assertEqual(4, len(self.card["boundary"]["out_of_scope"]))
        self.assertNotIn("source.locator=", boundary_text)
        for prefix in CONTRACT.WORKFLOW_SCOPE_PREFIXES:
            self.assertNotIn(f"{prefix}:", boundary_text)


if __name__ == "__main__":
    unittest.main()
