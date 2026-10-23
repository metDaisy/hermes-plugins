import copy
import json
import math
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import sys

SKILL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_DIR / "scripts"))

import triage
from triage import (
    JsonInputError,
    _loads_json,
    compose_intake,
    decision_request_ids,
    freeze,
    main,
    read_card,
    replace_card_body,
    template,
    validate,
    validate_card,
    validate_current_state_inspection,
    validate_decision_alignment,
    validate_repository_locators,
    write_json,
)


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
                "required_state": "CartService는 Cart#changeQuantity를 이름 있는 behavior로 호출한다.",
                "evidence": [
                    self.evidence(
                        "src/main/java/example/CartService.java#changeQuantity",
                        "CartService가 CartItem 수량을 직접 변경한다.",
                    )
                ],
                "rules": [
                    self.source(
                        "docs/backend-development-guide.md#Application-구현-규칙",
                        "Application은 aggregate의 이름 있는 behavior를 호출한다.",
                    )
                ],
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
                    "modify src/main/java/example/CartService.java#changeQuantity(UUID,UUID,int)에서 직접 상태 변경을 제거한다.",
                    "modify src/main/java/example/Cart.java#changeQuantity(UUID,int) 이름 있는 behavior를 사용한다.",
                ],
                "out_of_scope": ["공개 API 변경"],
                "acceptance": [
                    {
                        "id": "ACCEPT-001",
                        "criterion": "CartService가 Cart#changeQuantity를 호출하고 CartItem.changeQuantity를 직접 호출하지 않는다.",
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
                "required_verification": [
                    "example.CartTest#quantityZeroDeletes에서 quantity=0 삭제를 회귀 검증한다.",
                    "com.example.orders.ModularityTest#verifyModularity로 구조를 검증한다.",
                ],
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

        composed = triage.compose_analysis(intake, self.analysis_authoring())

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

        composed = triage.compose_analysis(intake, authoring)

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
            triage.compose_analysis(self.populate_intake(), authoring)

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

    def test_agent_documents_define_triage_as_an_implementation_brief(self):
        skill_dir = Path(__file__).resolve().parents[1]
        skill = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
        contract = (skill_dir / "references" / "triage-contract.md").read_text(encoding="utf-8")
        orchestrator = (
            skill_dir.parent / "kwc-orchestrator-contract" / "SKILL.md"
        ).read_text(encoding="utf-8")
        coverage = (
            skill_dir.parent
            / "kwc-orchestrator-contract"
            / "references"
            / "pm-responsibility-coverage.json"
        ).read_text(encoding="utf-8")

        self.assertIn("검증된 구현 입력 문서", skill)
        self.assertIn("intake → analysis → frozen", skill)
        self.assertIn("같은 Triage card", skill)
        self.assertIn("Analysis artifact", skill)
        self.assertNotIn("Analysis body를 read-back", skill)
        self.assertIn("현재 working directory를 planning workspace로 고정", skill)
        self.assertIn("첫 terminal 호출은 `pwd`", skill)
        self.assertIn("다른 `workdir`를 지정한 terminal 호출은 금지", skill)
        self.assertIn("literal `PLANNING_ROOT`", skill)
        self.assertIn("cd \"$PLANNING_ROOT\" && pwd", skill)
        self.assertIn("`run-workflow`를 추가로 load하지 않는다", skill)
        self.assertIn("archived task는 조회하지 않는다", skill)
        self.assertIn("archived task body를 읽거나 Triage 근거로 사용하지 않는다", skill)
        self.assertIn("triage-v7:attempt-$ATTEMPT", skill)
        self.assertIn("Kanban CLI syntax 검색과 `--help` 호출을 하지 않는다", skill)
        self.assertNotIn("모를 때만 해당 `--help`", skill)
        self.assertIn("Issue를 읽기 전 workspace filename search를 실행하지 않는다", skill)
        self.assertIn("`get_parent`와 `get_sub_issues`를 호출하지 않는다", skill)
        self.assertIn("helper source를 읽거나 검색하지 않는다", skill)
        self.assertIn("replace-card-body", skill)
        self.assertIn("Producer 공개 error seam이 없더라도 즉시 Decision으로 보내지 않는다", skill)
        self.assertIn("Issue의 numbered scope와 각 bullet", skill)
        self.assertIn("HTTP binding과 DTO boundary", skill)
        self.assertIn("`@RequestBody` type을 application service에", skill)
        self.assertIn("조사 시작점" + '"' + " 목록은 허용 범위의 끝이 아니므로", skill)
        self.assertIn("absence가 정상 조회 결과인지 command 실패인지", skill)
        self.assertIn("동일 저장소의 기존 공개 contract", skill)
        self.assertIn("조회 use case와 command use case를 하나의 missing semantics로 합치지 않는다", contract)
        self.assertIn("codebase-memory-mcp", skill)
        self.assertIn("semble-search", skill)
        self.assertIn("MCP 결과는 locator 후보", skill)
        self.assertIn("서로 독립적인 MCP 조회는 같은 tool turn", skill)
        self.assertIn("정상 analysis의 Codebase Memory 호출은", skill)
        self.assertIn("project not found or not indexed", skill)
        self.assertIn("실패가 확정된 `search_graph`를 형식적으로 호출하는 것은 정상 checkpoint가 아니다", skill)
        self.assertIn("unavailable/stale index 경로에서 정확히 `index_status` 1회", skill)
        self.assertIn("unavailable/stale index면 `search_graph = 0`", skill)
        self.assertIn("source-closure checklist", skill)
        self.assertIn("production source는 최대 두 번의 batched read turn", skill)
        self.assertIn("test source는\n한 번의 batched read turn", skill)
        self.assertIn("unvalidated candidate", skill)
        self.assertIn("Initial search turn이 끝나면 production/test exact path 목록을 고정", skill)
        self.assertIn("각 search/read tool turn 직전에", skill)
        self.assertIn("사후에 예산 초과를 발견해 `TRIAGE_BLOCKED`로 바꾸는 것은 금지", skill)
        self.assertIn("`.java` 확장자를 생략하지 않는다", skill)
        self.assertIn("Directory locator는 조사 후보 묶음일 뿐", skill)
        self.assertIn("Semble 호출은 최대", skill)
        self.assertIn("mcp__codebase_memory__index_status", skill)
        self.assertIn("mcp__codebase_memory__search_graph", skill)
        self.assertIn("mcp__semble_mcp__search", skill)
        self.assertIn("첫 direct source read 전에", skill)
        self.assertIn("`src/main/**`, `src/test/**`, migration, `package-info.java`", skill)
        self.assertIn("문서 읽기와 production/test source 읽기를 같은 단계로 취급하지 않는다", skill)
        self.assertIn("MCP checkpoint 이전에는 production/test source path가 prompt나 Issue에 직접 적혀 있어도 읽지 않는다", skill)
        self.assertIn("MCP checkpoint를 통과하기 전 source read tool call을 구성하지 않는다", skill)
        self.assertIn("`search_graph` 3회째 호출", skill)
        self.assertIn("compose-analysis", skill)
        self.assertIn("analysis-authoring.json", skill)
        self.assertIn("canonical `analysis.json`을 만든다", skill)
        self.assertIn("중복 authoring output만 제거", skill)
        self.assertIn("terminal·Python rewrite로\n`analysis-authoring.json`이나 `analysis.json`을 수정하지 않는다", skill)
        self.assertIn("첫 terminal command contract", skill)
        self.assertIn("native intake create와 immediate read-back을 완료한 뒤에만 MCP checkpoint", skill)
        self.assertIn("compose-intake", skill)
        self.assertIn("intake-authoring.json", skill)
        self.assertIn("canonical field 위치를 helper가 결정", skill)
        self.assertIn("`out_of_scope`: source object가 아니라 non-empty string list", skill)
        self.assertIn("downstream card 생성·activation 같은 실행 제한", skill)
        self.assertIn("반복 key만 단독 hunk로 사용하지 않는다", skill)
        self.assertIn("JSON array의 마지막 항목을 삭제", skill)
        self.assertIn("intake task ID가 없으면 MCP나 source confirmation을 시작하지 않는다", skill)
        self.assertIn("`validate --repo .`가 모든 local locator의 실제 path 존재를 검사", skill)
        self.assertIn("모든 structural·locator·semantic error를 한 번에 분석", skill)
        self.assertIn("positive predicate", skill)
        self.assertIn("brittle negative-text expression", skill)
        self.assertIn("preserved observable마다 **정확히 한 row**", skill)
        self.assertIn("하나의 row는 함께 회귀하고 같은 verification subject", skill)
        self.assertIn("서로 다른 test family", skill)
        self.assertIn("generic verification-only gap이나 slice를 만들지 않는다", skill)
        self.assertIn("Issue가 이미 구현된 baseline behavior의 독립 검증을 명시적으로 요구한 경우", skill)
        self.assertIn("production_seam", skill)
        self.assertIn("Provider·DTO·test만", skill)
        self.assertIn("동일 `observable_id`를 가진 두 번째", skill)
        self.assertIn("`DUPLICATE_VERIFICATION_OBSERVABLE:<OBS-ID>`", skill)
        self.assertIn("중복 object 전체를", skill)
        self.assertIn("exact absolute `resolved_path`", skill)
        self.assertIn("`success=false`를 반환하면 correction budget이", skill)
        self.assertIn('TRIAGE_PY="$HERMES_KWC_PLUGIN_ROOT/skills/kwc-create-triage/scripts/triage.py"', skill)
        self.assertIn('CURRENT_STATE_PY="$HERMES_KWC_PLUGIN_ROOT/skills/kwc-update-current-state/scripts/current_state.py"', skill)
        self.assertIn('while test -e "$RUN_BASE/attempt-$ATTEMPT"', skill)
        self.assertIn("Triage authoring: `kwc-create-triage`", orchestrator)
        self.assertIn("analysis-authoring.json`을 한 번의 `write_file`", skill)
        self.assertIn("intake에서 이미 확정한 identity, baseline, `intent`, `investigation_targets`", skill)
        self.assertIn("top-level key는 `intake_amendments`, `confirmed_gaps`, `target_dispositions`", skill)
        self.assertIn("`section + id + changes`", skill)
        self.assertIn("전체 `intent`나\ntarget list를 다시 쓰지 않는다", skill)
        self.assertIn("incremental patch를 금지", skill)
        self.assertIn("codebase-memory-mcp", coverage)
        self.assertIn("list --status triage --json` 한 번", skill)
        self.assertIn("`tool_search`는 하지 않는다", skill)
        self.assertIn("모든 reference를 선로드하지 않는다", skill)
        self.assertIn("두 native write와 두 authoritative read-back", skill)
        self.assertIn("Repository root의 Issue 번호 검색", skill)
        self.assertIn("Triage Card는", contract)
        self.assertIn("구현 설계 계약", contract)
        self.assertIn("UNSOURCED_TARGET_BEHAVIOR", contract)
        self.assertIn("preserved observable의 현재 동작 자체를 gap으로 분류하지 않는다", contract)
        self.assertIn("path variable binding 규칙을 cookie parsing에 그대로 적용하지 않는다", contract)
        self.assertIn("caller-or-owner-symbol", contract)
        self.assertIn("다른 module의\ncaller에게 맡긴 behavior", contract)

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

    def test_write_json_is_confined_to_temp_artifact_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = Path(".temp") / "triage" / "board" / "issue-151" / "attempt-1" / "body.json"
            with patch("triage.Path.cwd", return_value=root):
                write_json(output, {"ok": True})
            self.assertEqual({"ok": True}, json.loads((root / output).read_text(encoding="utf-8")))
            with patch("triage.Path.cwd", return_value=root):
                with self.assertRaisesRegex(ValueError, "OUTPUT_PATH_OUTSIDE_TEMP"):
                    write_json(Path("body.json"), {"ok": True})
                with self.assertRaisesRegex(ValueError, "OUTPUT_PATH_OUTSIDE_TEMP"):
                    write_json(Path(".temp") / ".." / "escaped.json", {"ok": True})

    @patch("triage.subprocess.run")
    def test_read_card_uses_official_read_only_cli_surface(self, run):
        run.return_value.stdout = json.dumps({"task": {"id": "t_123abc"}})
        self.assertEqual({"id": "t_123abc"}, read_card("hermes", "board", "t_123abc"))
        run.assert_called_once_with(
            ["hermes", "kanban", "--board", "board", "show", "t_123abc", "--json"],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )

    @patch("triage.subprocess.run")
    def test_replace_card_body_uses_official_mutation_cli_without_readback(self, run):
        run.return_value.stdout = "updated\n"
        with tempfile.TemporaryDirectory() as tmp:
            body_path = Path(tmp) / "frozen.json"
            body_path.write_text(json.dumps(self.populate_analysis()), encoding="utf-8")

            replace_card_body("hermes", "board", "t_123abc", body_path)

            run.assert_called_once_with(
                [
                    "hermes", "kanban", "--board", "board", "edit", "t_123abc",
                    "--body-file", str(body_path),
                ],
                check=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )

    @patch("triage.replace_card_body")
    @patch("triage.validate", return_value=[])
    @patch("triage._read_json")
    def test_replace_card_body_command_accepts_body_file_flag(self, read_json, validate_body, replace):
        read_json.return_value = self.populate_analysis()
        body_path = Path("frozen.json")
        with patch("sys.argv", [
            "triage.py", "replace-card-body",
            "--task-id", "t_123abc",
            "--board", "board",
            "--body-file", str(body_path),
        ]):
            self.assertEqual(0, main())

        read_json.assert_called_once_with(body_path)
        replace.assert_called_once_with("hermes", "board", "t_123abc", body_path)


if __name__ == "__main__":
    unittest.main()
