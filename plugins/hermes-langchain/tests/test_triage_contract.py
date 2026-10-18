import copy
import json
import math
import tempfile
import unittest
from pathlib import Path


_ROOT = Path(__file__).resolve().parents[1]
from tests.support import load_module

DOMAIN = load_module("authoring.triage_domain")
for _name in DOMAIN.__all__:
    globals()[_name] = getattr(DOMAIN, _name)


class TriageV7ContractTest(unittest.TestCase):
    def setUp(self):
        self.body = template(
            151,
            "[P3] Cart 계층 구조 및 구현 리팩터링",
            "https://github.com/metDaisy/e-commerce-clone-coding/issues/151",
            "a" * 40,
            "b" * 40,
            ["docs/requirement/p3/p3-cart.md#Cart"],
            ["docs/current-state.md#P3"],
        )

    @staticmethod
    def source(locator="docs/requirement/p3/p3-cart.md#Cart", claim="Cart behavior is fixed."):
        return {"locator": locator, "claim": claim}

    @staticmethod
    def evidence(locator="src/main/java/example/CartService.java#changeQuantity", claim="Service mutates an item directly."):
        return {"locator": locator, "claim": claim}

    @staticmethod
    def make_test_evidence(locator="src/test/java/example/CartTest.java#quantityZeroDeletes", claim="The test asserts deletion."):
        return {"locator": locator, "claim": claim}

    def populate_intake(self):
        body = copy.deepcopy(self.body)
        body["intent"] = {
            "goal": "공개 Cart 동작을 보존하면서 계층 책임 위반을 정리한다.",
            "change_requirements": [
                {
                    "id": "CHANGE-001",
                    "outcome": "Application service가 aggregate 내부 상태를 직접 변경하지 않는다.",
                    "source": self.source("https://github.com/metDaisy/e-commerce-clone-coding/issues/151#완료-기준", "Application service does not mutate aggregate internals."),
                }
            ],
            "preserved_observables": [
                {
                    "id": "OBS-001",
                    "behavior": "quantity=0은 Cart Item 삭제로 처리한다.",
                    "source": self.source("docs/requirement/p3/p3-cart.md#관계와-제약", "quantity=0 deletes the item."),
                }
            ],
            "quality_gates": [
                {
                    "id": "GATE-001",
                    "outcome": "관련 Cart 테스트와 Modulith 검증을 실행한다.",
                    "source": self.source("https://github.com/metDaisy/e-commerce-clone-coding/issues/151#완료-기준", "Run focused and structural verification."),
                }
            ],
            "finalization_requirements": [
                {
                    "id": "FINAL-001",
                    "outcome": "검증된 구현 SHA로 current-state lifecycle을 마감한다.",
                    "source": self.source("https://github.com/metDaisy/e-commerce-clone-coding/issues/151#완료-기준", "Update current state after implementation verification."),
                }
            ],
            "out_of_scope": ["공개 Cart API와 error 의미 변경"],
        }
        body["investigation_targets"] = [
            {
                "id": "TARGET-001",
                "locator": "src/main/java/example/CartService.java#changeQuantity",
                "question": "aggregate 상태를 직접 변경하는가?",
                "source": self.source("https://github.com/metDaisy/e-commerce-clone-coding/issues/151#조사-대상", "Inspect CartService."),
            }
        ]
        return body

    def test_compose_intake_places_authored_fields_in_canonical_schema(self):
        populated = self.populate_intake()
        authoring = {
            "intent": populated["intent"],
            "investigation_targets": populated["investigation_targets"],
        }

        composed = compose_intake(self.body, authoring)

        self.assertEqual(populated, composed)
        self.assertEqual([], validate(composed, "triage"))

    def test_kwc_object_api_composes_and_freezes(self):
        module = build_triage_module()
        identity = TriageIdentity(
            issue_number=151,
            title="[P3] Cart 계층 구조 및 구현 리팩터링",
            issue_url="https://github.com/metDaisy/e-commerce-clone-coding/issues/151",
            planning_sha="a" * 40,
            current_state_sha="b" * 40,
            requirement_locators=("docs/requirement/p3/p3-cart.md#Cart",),
            current_state_locators=("docs/current-state.md#P3",),
        )
        populated = self.populate_intake()
        authoring = {
            "intent": populated["intent"],
            "investigation_targets": populated["investigation_targets"],
        }

        intake = module.compose_intake.execute(identity, authoring)
        analysis = module.compose_analysis.execute(intake, self.analysis_authoring())
        frozen = module.freezer.execute(analysis, self.inspection())

        self.assertEqual(compose_intake(self.body, authoring), intake)
        self.assertEqual(compose_analysis(intake, self.analysis_authoring()), analysis)
        self.assertEqual(freeze(analysis, self.inspection()), frozen)
        self.assertEqual([], module.validator.validate(frozen, "triage"))

    def test_compose_intake_rejects_misplaced_out_of_scope(self):
        populated = self.populate_intake()
        authoring = {
            "intent": {
                key: value
                for key, value in populated["intent"].items()
                if key != "out_of_scope"
            },
            "investigation_targets": populated["investigation_targets"],
            "out_of_scope": populated["intent"]["out_of_scope"],
        }

        with self.assertRaisesRegex(
            ValueError,
            "UNEXPECTED_INTAKE_AUTHORING_FIELD:out_of_scope",
        ):
            compose_intake(self.body, authoring)

    def test_compose_intake_reports_out_of_scope_object_shape(self):
        populated = self.populate_intake()
        populated["intent"]["out_of_scope"] = [
            {
                "id": "OOS-001",
                "outcome": "공개 API 변경",
                "source": self.source(),
            }
        ]
        authoring = {
            "intent": populated["intent"],
            "investigation_targets": populated["investigation_targets"],
        }

        with self.assertRaisesRegex(
            ValueError,
            "INTAKE_OUT_OF_SCOPE_MUST_BE_NONEMPTY_STRING_LIST",
        ):
            compose_intake(self.body, authoring)

    def populate_analysis(self):
        body = self.populate_intake()
        body["phase"] = "analysis"
        body["confirmed_gaps"] = [
            {
                "id": "GAP-001",
                "subject": "CartService aggregate 우회",
                "current_state": "CartService가 CartItem을 직접 변경한다.",
                "required_state": "CartService는 Cart aggregate의 이름 있는 behavior를 호출한다.",
                "evidence": [self.evidence()],
                "rules": [self.source("docs/backend-development-guide.md#Application-구현-규칙", "Application calls aggregate behavior.")],
                "requirement_ids": ["CHANGE-001"],
                "affected_observable_ids": ["OBS-001"],
            }
        ]
        body["target_dispositions"] = [
            {
                "target_id": "TARGET-001",
                "disposition": "gap",
                "finding_ids": ["GAP-001"],
                "rationale": "직접 상태 변경이 확인됐다.",
            }
        ]
        body["implementation_slices"] = [
            {
                "id": "SLICE-001",
                "title": "Cart aggregate behavior 위임",
                "outcome": "CartService의 직접 상태 변경을 aggregate 호출로 치환한다.",
                "gap_ids": ["GAP-001"],
                "scope": [
                    "CartService의 changeQuantity 경로를 수정한다.",
                    "Cart aggregate의 이름 있는 behavior를 사용한다.",
                ],
                "out_of_scope": ["공개 API 변경"],
                "acceptance": [
                    {
                        "id": "ACCEPT-001",
                        "criterion": "CartService가 CartItem.changeQuantity를 직접 호출하지 않는다.",
                        "requirement_ids": ["CHANGE-001"],
                        "observable_ids": ["OBS-001"],
                        "verification_ids": ["VERIFY-001"],
                    }
                ],
                "contract_change_ids": [],
                "depends_on": [],
            }
        ]
        body["verification_matrix"] = [
            {
                "id": "VERIFY-001",
                "observable_id": "OBS-001",
                "current_coverage": "covered",
                "evidence": [self.make_test_evidence()],
                "production_seam": self.evidence(
                    "src/main/java/example/Cart.java#changeQuantity",
                    "Cart aggregate executes the quantity transition.",
                ),
                "coverage_gap": None,
                "required_verification": ["quantity=0 삭제를 회귀 검증한다."],
            }
        ]
        body["contract_changes"] = []
        body["document_impacts"] = [
            {
                "document": "docs/requirement/p3/p3-cart.md",
                "disposition": "no-change",
                "evidence": [self.source("docs/requirement/p3/p3-cart.md#Cart", "The behavior is already defined.")],
                "action": None,
            }
        ]
        body["open_decisions"] = []
        body["handoff"] = {"graph_allowed": False, "blocker": "freeze-required"}
        return body

    def analysis_authoring(self):
        body = self.populate_analysis()
        authoring = {
            field: copy.deepcopy(body[field])
            for field in (
                "confirmed_gaps",
                "target_dispositions",
                "implementation_slices",
                "verification_matrix",
                "contract_changes",
                "document_impacts",
                "open_decisions",
            )
        }
        authoring["intake_amendments"] = {
            "requirement_locators": [],
            "current_state_locators": [],
            "intent_entries": [],
            "investigation_targets": [],
        }
        return authoring

    def test_compose_analysis_reuses_intake_and_places_only_analysis_fields(self):
        intake = self.populate_intake()

        composed = compose_analysis(intake, self.analysis_authoring())

        self.assertEqual(self.populate_analysis(), composed)
        self.assertEqual([], validate(composed, "triage"))

    def test_compose_analysis_applies_compact_intake_amendments_by_stable_id(self):
        intake = self.populate_intake()
        authoring = self.analysis_authoring()
        authoring["intake_amendments"] = {
            "requirement_locators": [
                "docs/requirement/p3/p3-cart.md#Cart",
                "docs/requirement/p9/p9-offer.md#Offer",
            ],
            "current_state_locators": [
                "docs/current-state.md#P3",
                "docs/current-state.md#P9",
            ],
            "intent_entries": [
                {
                    "section": "preserved_observables",
                    "id": "OBS-001",
                    "changes": {
                        "source": self.source(
                            "docs/requirement/p3/p3-cart.md#Cart",
                            "quantity=0 deletion remains public behavior.",
                        )
                    },
                }
            ],
            "investigation_targets": [
                {
                    "id": "TARGET-001",
                    "changes": {"question": "aggregate behavior와 caller boundary를 함께 확인했는가?"},
                }
            ],
        }

        composed = compose_analysis(intake, authoring)

        self.assertEqual(
            authoring["intake_amendments"]["requirement_locators"],
            composed["baseline"]["requirement_locators"],
        )
        self.assertEqual(
            "quantity=0 deletion remains public behavior.",
            composed["intent"]["preserved_observables"][0]["source"]["claim"],
        )
        self.assertEqual(
            "aggregate behavior와 caller boundary를 함께 확인했는가?",
            composed["investigation_targets"][0]["question"],
        )

    def test_compose_analysis_rejects_inherited_or_handoff_fields(self):
        authoring = self.analysis_authoring()
        authoring["intent"] = self.populate_intake()["intent"]
        authoring["handoff"] = {"graph_allowed": True, "blocker": None}

        with self.assertRaisesRegex(
            ValueError,
            "UNEXPECTED_ANALYSIS_AUTHORING_FIELD:handoff.*UNEXPECTED_ANALYSIS_AUTHORING_FIELD:intent",
        ):
            compose_analysis(self.populate_intake(), authoring)

    def inspection(self):
        return {
            "schema": "current-state-inspection-v1",
            "freshness": "fresh",
            "inspection_sha": "a" * 40,
            "snapshot_sha": "b" * 40,
            "worktree_clean": True,
            "snapshot_is_ancestor": True,
        }

    def test_template_creates_visible_v7_intake(self):
        self.assertEqual("triage-v7", self.body["schema"])
        self.assertEqual("intake", self.body["phase"])
        self.assertEqual({
            "schema", "phase", "frozen_digest", "issue", "baseline", "intent",
            "investigation_targets", "confirmed_gaps", "target_dispositions",
            "implementation_slices", "verification_matrix", "contract_changes",
            "document_impacts", "open_decisions", "handoff",
        }, set(self.body))

    def test_intake_is_valid_before_source_analysis(self):
        self.assertEqual([], validate(self.populate_intake(), "triage"))

    def test_analysis_requires_every_change_requirement_to_map_to_a_gap(self):
        body = self.populate_analysis()
        body["confirmed_gaps"][0]["requirement_ids"] = []
        self.assertIn("UNMAPPED_CHANGE_REQUIREMENT:CHANGE-001", validate(body, "triage"))

    def test_repository_locator_validation_rejects_nonexistent_local_evidence_path(self):
        body = self.populate_analysis()
        body["confirmed_gaps"][0]["evidence"][0]["locator"] = (
            "src/main/java/io/github/metdaisy/amaazon/amaazon/cart/CartService.java#changeQuantity"
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            valid_path = root / "docs" / "requirement" / "p3" / "p3-cart.md"
            valid_path.parent.mkdir(parents=True)
            valid_path.write_text("# Cart", encoding="utf-8")

            errors = validate_repository_locators(body, root)

        self.assertIn(
            "MISSING_REPOSITORY_LOCATOR:src/main/java/io/github/metdaisy/amaazon/amaazon/cart/CartService.java",
            errors,
        )

    def test_analysis_requires_every_named_target_disposition(self):
        body = self.populate_analysis()
        body["target_dispositions"] = []
        self.assertIn("UNRESOLVED_INVESTIGATION_TARGET:TARGET-001", validate(body, "triage"))

    def test_slice_requires_confirmed_gap_and_verifiable_acceptance(self):
        body = self.populate_analysis()
        body["implementation_slices"][0]["gap_ids"] = []
        body["implementation_slices"][0]["acceptance"][0]["verification_ids"] = []
        errors = validate(body, "triage")
        self.assertIn("UNSLICED_CONFIRMED_GAP:GAP-001", errors)
        self.assertIn("MISSING_IMPLEMENTATION_SLICE:0_ACCEPTANCE:0_VERIFICATION_IDS", errors)

    def test_slice_rejects_descriptions_that_cannot_materialize_a_korean_impl_card(self):
        body = self.populate_analysis()
        body["implementation_slices"][0]["title"] = "Offer public error bridge"

        self.assertIn(
            "DESCRIPTION_NOT_KOREAN:IMPLEMENTATION_SLICE:0:title",
            validate(body, "triage"),
        )

    def test_gap_requires_repository_backed_constraint_for_impl_context(self):
        body = self.populate_analysis()
        body["confirmed_gaps"][0]["rules"] = [
            self.source(
                "https://github.com/metDaisy/e-commerce-clone-coding/issues/151#완료-기준",
                "Application calls aggregate behavior.",
            )
        ]

        self.assertIn(
            "MISSING_REPOSITORY_CONSTRAINT:CONFIRMED_GAP:GAP-001",
            validate(body, "triage"),
        )

    def test_contract_change_must_be_mapped_to_an_implementation_slice(self):
        body = self.populate_analysis()
        body["contract_changes"] = [
            {
                "id": "CONTRACT-001",
                "consumer": "cart",
                "producer": "offer",
                "change": "OfferQueryApi에 producer-owned public bridge를 추가한다.",
                "evidence": [self.evidence()],
                "verification_ids": ["VERIFY-001"],
            }
        ]

        self.assertIn("UNMAPPED_CONTRACT_CHANGE:CONTRACT-001", validate(body, "triage"))

        body["implementation_slices"][0]["contract_change_ids"] = ["CONTRACT-001"]
        self.assertEqual([], validate(body, "triage"))

    def test_slice_rejects_process_only_outcome(self):
        body = self.populate_analysis()
        body["implementation_slices"][0]["outcome"] = "Cart 구조를 검토한다."
        self.assertIn("NON_OBSERVABLE_IMPLEMENTATION_SLICE_OUTCOME:SLICE-001", validate(body, "triage"))

    def test_frozen_body_rejects_ephemeral_coordinator_scope(self):
        body = self.populate_analysis()
        body["intent"]["out_of_scope"].append("이번 Triage에서 downstream card를 만들지 않는다.")

        self.assertIn("EPHEMERAL_COORDINATOR_CONTEXT", validate(body, "triage"))
        with self.assertRaisesRegex(ValueError, "EPHEMERAL_COORDINATOR_CONTEXT"):
            freeze(body, self.inspection())

        body = self.populate_analysis()
        body["intent"]["finalization_requirements"][0]["outcome"] = (
            "이 작업에서는 Impl·Review·Summary downstream card를 생성하지 않는다."
        )
        self.assertIn("EPHEMERAL_COORDINATOR_CONTEXT", validate(body, "triage"))

        body = self.populate_analysis()
        body["implementation_slices"][0]["out_of_scope"].append(
            "downstream card lifecycle mutation"
        )
        self.assertIn("EPHEMERAL_COORDINATOR_CONTEXT", validate(body, "triage"))

    def test_ephemeral_context_marker_does_not_match_longer_product_word(self):
        body = self.populate_analysis()
        body["intent"]["preserved_observables"][0]["source"]["claim"] = (
            "The user explicitly requires this caller-specific tolerant contract."
        )

        self.assertNotIn("EPHEMERAL_COORDINATOR_CONTEXT", validate(body, "triage"))

    def test_analysis_rejects_unresolved_implementation_alternative_without_decision(self):
        body = self.populate_analysis()
        body["intent"]["change_requirements"][0]["outcome"] = (
            "producer 공개 error를 사용하거나 consumer 결과로 변환한다."
        )

        errors = validate(body, "triage")
        self.assertIn("UNRESOLVED_IMPLEMENTATION_ALTERNATIVE", errors)
        self.assertIn(
            "UNRESOLVED_IMPLEMENTATION_ALTERNATIVE:"
            "intent.change_requirements[CHANGE-001].outcome:사용하거나",
            errors,
        )
        with self.assertRaisesRegex(ValueError, "UNRESOLVED_IMPLEMENTATION_ALTERNATIVE"):
            freeze(body, self.inspection())

    def test_analysis_allows_conjunctions_in_a_resolved_contract(self):
        body = self.populate_analysis()
        body["intent"]["change_requirements"][0]["outcome"] = (
            "field 또는 collection을 직접 변경하지 않고 aggregate behavior를 사용한다."
        )

        self.assertNotIn("UNRESOLVED_IMPLEMENTATION_ALTERNATIVE", validate(body, "triage"))

    def test_covered_verification_requires_direct_test_evidence(self):
        body = self.populate_analysis()
        body["verification_matrix"][0]["evidence"] = []
        self.assertIn("COVERED_VERIFICATION_WITHOUT_TEST_EVIDENCE:VERIFY-001", validate(body, "triage"))

    def test_covered_verification_rejects_production_source_as_test_evidence(self):
        body = self.populate_analysis()
        body["verification_matrix"][0]["evidence"] = [self.evidence()]
        self.assertIn("COVERED_VERIFICATION_WITHOUT_TEST_EVIDENCE:VERIFY-001", validate(body, "triage"))

    def test_verification_requires_the_current_production_seam(self):
        body = self.populate_analysis()
        del body["verification_matrix"][0]["production_seam"]
        self.assertIn("MISSING_VERIFICATION:0_FIELD:production_seam", validate(body, "triage"))

    def test_verification_rejects_requirement_as_a_production_seam(self):
        body = self.populate_analysis()
        body["verification_matrix"][0]["production_seam"] = self.source()

        self.assertIn(
            "INVALID_VERIFICATION:0_PRODUCTION_SEAM_LOCATOR",
            validate(body, "triage"),
        )

    def test_partial_verification_requires_named_gap(self):
        body = self.populate_analysis()
        verification = body["verification_matrix"][0]
        verification["current_coverage"] = "partial"
        verification["coverage_gap"] = None
        self.assertIn("PARTIAL_VERIFICATION_WITHOUT_GAP:VERIFY-001", validate(body, "triage"))

    def test_verification_matrix_rejects_a_second_row_for_the_same_observable(self):
        body = self.populate_analysis()
        duplicate = copy.deepcopy(body["verification_matrix"][0])
        duplicate["id"] = "VERIFY-002"
        body["verification_matrix"].append(duplicate)

        self.assertIn(
            "DUPLICATE_VERIFICATION_OBSERVABLE:OBS-001",
            validate(body, "triage"),
        )

    def test_cross_domain_contract_is_optional(self):
        body = self.populate_analysis()
        body["contract_changes"] = []
        self.assertEqual([], validate(body, "triage"))

    def test_decision_option_cannot_reopen_preserved_observable(self):
        body = self.populate_analysis()
        body["open_decisions"] = [
            {
                "id": "DECISION-001",
                "question": "어떤 seam을 사용할 것인가?",
                "why_unresolved": "두 구현 seam이 남는다.",
                "evidence": [self.source()],
                "affected_observable_ids": ["OBS-001"],
                "options": [
                    {"id": "A", "choice": "Seam A", "impact": "No behavior change", "preserves_observable_ids": ["OBS-001"]},
                    {"id": "B", "choice": "Seam B", "impact": "Changes quantity=0", "preserves_observable_ids": []},
                ],
                "owner": "user",
                "status": "pending",
                "approved_option_id": None,
            }
        ]
        body["handoff"] = {"graph_allowed": False, "blocker": "decision-required"}
        self.assertIn("FIXED_OBSERVABLE_REOPENED:DECISION-001:B:OBS-001", validate(body, "triage"))

    def test_decision_requires_user_owner_and_distinct_choices(self):
        body = self.populate_analysis()
        body["open_decisions"] = [
            {
                "id": "DECISION-001",
                "question": "어떤 공개 계약을 승인할 것인가?",
                "why_unresolved": "두 결과가 requirement와 양립한다.",
                "evidence": [self.source()],
                "affected_observable_ids": ["OBS-001"],
                "options": [
                    {"id": "A", "choice": "Same choice", "impact": "Preserved", "preserves_observable_ids": ["OBS-001"]},
                    {"id": "B", "choice": " same choice ", "impact": "Preserved", "preserves_observable_ids": ["OBS-001"]},
                ],
                "owner": "project-manager",
                "status": "pending",
                "approved_option_id": None,
            }
        ]
        body["handoff"] = {"graph_allowed": False, "blocker": "decision-required"}
        errors = validate(body, "triage")
        self.assertIn("DECISION_OWNER_MUST_BE_USER:DECISION-001", errors)
        self.assertIn("DUPLICATE_DECISION_CHOICE:DECISION-001:B", errors)

    def test_decision_requires_requirement_or_issue_authority_evidence(self):
        body = self.populate_analysis()
        body["open_decisions"] = [
            {
                "id": "DECISION-001",
                "question": "어떤 내부 method shape을 사용할 것인가?",
                "why_unresolved": "두 구현이 가능하다.",
                "evidence": [self.evidence()],
                "affected_observable_ids": ["OBS-001"],
                "options": [
                    {"id": "A", "choice": "Method A", "impact": "Preserved", "preserves_observable_ids": ["OBS-001"]},
                    {"id": "B", "choice": "Method B", "impact": "Preserved", "preserves_observable_ids": ["OBS-001"]},
                ],
                "owner": "user",
                "status": "pending",
                "approved_option_id": None,
            }
        ]
        body["handoff"] = {"graph_allowed": False, "blocker": "decision-required"}

        self.assertIn(
            "DECISION_AUTHORITY_EVIDENCE_REQUIRED:DECISION-001",
            validate(body, "triage"),
        )

    def test_non_gap_target_disposition_cannot_reference_gap(self):
        body = self.populate_analysis()
        body["target_dispositions"][0]["disposition"] = "compliant"
        self.assertIn("NON_GAP_DISPOSITION_WITH_FINDINGS:TARGET-001", validate(body, "triage"))

    def test_freeze_opens_handoff_only_for_resolved_analysis(self):
        frozen = freeze(self.populate_analysis(), self.inspection())
        self.assertEqual("frozen", frozen["phase"])
        self.assertTrue(frozen["handoff"]["graph_allowed"])
        self.assertIsNone(frozen["handoff"]["blocker"])
        self.assertEqual([], validate(frozen, "triage"))
        self.assertEqual([], validate(frozen, "done"))

    def test_frozen_digest_detects_body_change(self):
        frozen = freeze(self.populate_analysis(), self.inspection())
        frozen["intent"]["goal"] = "changed after freeze"
        self.assertIn("FROZEN_BODY_DIGEST_MISMATCH", validate(frozen, "triage"))

    def test_freeze_rejects_open_decision(self):
        body = self.populate_analysis()
        body["open_decisions"] = [
            {
                "id": "DECISION-001",
                "question": "Choose a seam",
                "why_unresolved": "Two compatible seams remain.",
                "evidence": [self.source()],
                "affected_observable_ids": ["OBS-001"],
                "options": [
                    {"id": "A", "choice": "A", "impact": "Preserved", "preserves_observable_ids": ["OBS-001"]},
                    {"id": "B", "choice": "B", "impact": "Preserved", "preserves_observable_ids": ["OBS-001"]},
                ],
                "owner": "user",
                "status": "pending",
                "approved_option_id": None,
            }
        ]
        body["handoff"] = {"graph_allowed": False, "blocker": "decision-required"}
        with self.assertRaisesRegex(ValueError, "OPEN_DECISIONS_PREVENT_FREEZE"):
            freeze(body, self.inspection())

    def test_current_state_inspection_must_match_baseline(self):
        inspection = self.inspection()
        inspection["inspection_sha"] = "c" * 40
        self.assertIn(
            "CURRENT_STATE_INSPECTION_SHA_MISMATCH",
            validate_current_state_inspection(self.populate_analysis(), inspection),
        )

    def test_current_state_inspection_allows_only_hermes_dirty_paths_for_committed_baseline(self):
        inspection = self.inspection()
        inspection.update(
            {
                "freshness": "insufficient",
                "worktree_clean": False,
                "dirty_paths": [
                    "hermes/docs/plan.md",
                    "hermes/profiles/project-manager/distribution.yaml",
                ],
                "covered_changed_paths": [],
                "errors": ["WORKTREE_DIRTY"],
            }
        )
        self.assertEqual(
            [],
            validate_current_state_inspection(self.populate_analysis(), inspection),
        )

    def test_current_state_inspection_rejects_product_or_mixed_dirty_paths(self):
        inspection = self.inspection()
        inspection.update(
            {
                "freshness": "insufficient",
                "worktree_clean": False,
                "dirty_paths": ["hermes/docs/plan.md", "src/main/java/example/Cart.java"],
                "covered_changed_paths": ["src/main/java/example/Cart.java"],
                "errors": ["WORKTREE_DIRTY"],
            }
        )
        errors = validate_current_state_inspection(self.populate_analysis(), inspection)
        self.assertIn("CURRENT_STATE_INSPECTION_NOT_FRESH", errors)
        self.assertIn("CURRENT_STATE_INSPECTION_DIRTY", errors)

    def test_validate_card_uses_native_identity_and_v7_body(self):
        body = self.populate_analysis()
        task = {
            "id": "t_123abc",
            "status": "triage",
            "assignee": "project-manager",
            "body": json.dumps(body, ensure_ascii=False),
        }
        self.assertEqual([], validate_card(
            task,
            "t_123abc",
            "triage",
            "project-manager",
            151,
            "[P3] Cart 계층 구조 및 구현 리팩터링",
            "https://github.com/metDaisy/e-commerce-clone-coding/issues/151",
        ))

    def test_decision_alignment_preserves_cardinality(self):
        body = self.populate_analysis()
        body["open_decisions"] = [
            {
                "id": "DECISION-001",
                "question": "Q",
                "why_unresolved": "Why",
                "evidence": [self.source()],
                "affected_observable_ids": [],
                "options": [],
                "owner": "user",
                "status": "pending",
                "approved_option_id": None,
            }
        ]
        decision_body = {"decision_requests": [{"id": "DECISION-001"}, {"id": "DECISION-001"}]}
        self.assertIn("DECISION_REQUEST_COVERAGE_MISMATCH", validate_decision_alignment(body, decision_body))

    def test_decision_request_ids_supports_singular_and_aggregate_cards(self):
        self.assertEqual({"DECISION-001"}, decision_request_ids({"decision_request_id": "DECISION-001"}))
        self.assertEqual(
            {"DECISION-001", "DECISION-002"},
            decision_request_ids({"decision_requests": [{"id": "DECISION-001"}, {"id": "DECISION-002"}]}),
        )

    def test_malformed_nested_values_return_errors_without_traceback(self):
        malformed_values = [None, "x", 1, {"unexpected": True}]
        for field in (
            "investigation_targets",
            "confirmed_gaps",
            "target_dispositions",
            "implementation_slices",
            "verification_matrix",
            "contract_changes",
            "document_impacts",
            "open_decisions",
        ):
            for value in malformed_values:
                with self.subTest(field=field, value=value):
                    body = self.populate_analysis()
                    body[field] = value
                    self.assertTrue(validate(body, "triage"))

    def test_strict_json_rejects_duplicate_keys_and_non_finite_numbers(self):
        with self.assertRaises(JsonInputError):
            _loads_json('{"schema":"triage-v7","schema":"other"}')
        for value in ("NaN", "Infinity", "-Infinity"):
            with self.subTest(value=value):
                with self.assertRaises(JsonInputError):
                    _loads_json('{"value":' + value + "}")
        body = self.populate_analysis()
        body["issue"]["number"] = math.nan
        self.assertIn("NON_FINITE_NUMBER", validate(body, "triage"))



if __name__ == "__main__":
    unittest.main()
