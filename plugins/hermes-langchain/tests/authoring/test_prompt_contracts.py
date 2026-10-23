import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


class PromptContractTest(unittest.TestCase):

    def test_intake_prompt_exposes_exact_authoring_shape(self):
        prompt = (ROOT / "prompts" / "triage-intake.md").read_text(encoding="utf-8")

        for field in (
            "issue_number",
            "title",
            "issue_url",
            "planning_sha",
            "current_state_sha",
            "requirement_locators",
            "current_state_locators",
            "preserved_observables",
            "out_of_scope",
            "locator",
            "claim",
        ):
            self.assertIn(field, prompt)
        self.assertIn("docs/current-state.md", prompt)
        self.assertIn("repo-relative", prompt)
        self.assertIn("artifact_file", prompt)
        self.assertIn("generation-", prompt)
        self.assertIn("서로 독립적으로 구현·acceptance·verification", prompt)
        self.assertIn("owner나 verification seam이 다른 결과", prompt)
        self.assertIn("실패 원인과 회귀 검증이 다른 사용자 관찰 동작", prompt)
        self.assertIn("HTTP binding과 DTO boundary", prompt)
        self.assertIn("서로 다른 change requirement", prompt)
        self.assertIn("정상 조회 fact/projection과 error ownership", prompt)
        self.assertIn("서로 다른 preserved observable", prompt)
        self.assertIn("같은 fact들을 모두 명시하는 전용 정상 조회 preserved observable", prompt)
        self.assertIn("repository·persistence·transaction·locking 요구", prompt)
        self.assertIn("가장 가까운 repository test", prompt)
        self.assertIn("request DTO, response DTO, application service signature", prompt)

    def test_source_confirmation_requires_producer_implementation_evidence(self):
        prompt = (ROOT / "prompts" / "triage-source-confirmation.md").read_text(encoding="utf-8")

        self.assertIn("producer의 실제 production source", prompt)
        self.assertIn("Requirement 문서에 정의된 code/message는 구현된 public seam의 증거가 아니다", prompt)
        self.assertIn("absent|unknown", prompt)
        self.assertIn("accepted discovery artifact 전체의 `candidates[].path` 안에서만", prompt)
        self.assertIn("이미 발견한 candidate를 재사용", prompt)
        self.assertIn("non-empty 문자열 배열", prompt)
        self.assertIn("사용자 선택으로 승격하지 않는다", prompt)
        self.assertIn("Symbol이 존재하거나 호출된다는 사실만으로", prompt)
        self.assertIn("정확히 `locator`, `claim`만", prompt)
        self.assertIn("`Service.java#method`", prompt)

    def test_discovery_prompt_exposes_bounded_exact_authoring_shape(self):
        prompt = (ROOT / "prompts" / "triage-discovery.md").read_text(encoding="utf-8")

        self.assertIn("bounded discovery", prompt)
        self.assertIn("target별 8개 제한", prompt)
        self.assertIn("같은 target에 검색과 trace를 연속 실행", prompt)
        self.assertIn("nested breakdown이 아니라", prompt)
        self.assertIn("한 번의 batched `search_graph`", prompt)
        self.assertIn("`name_pattern` 정규식", prompt)
        self.assertIn('"tool_usage": {"codebase_memory": 3, "semble": 1}', prompt)
        self.assertIn("없으면 `null`", prompt)
        self.assertIn("Semble fallback", prompt)
        self.assertIn("`include_connected=true`", prompt)
        self.assertIn("Codebase Memory 보완 호출을 최대 한 번", prompt)
        self.assertIn("같은 path의 class/method candidate", prompt)
        self.assertIn("`resolution`에 `root_path`, `root_exists`를 복사하지 않는다", prompt)
        self.assertIn("pagination하지 않는다", prompt)
        self.assertIn("local `search_files` 호출을 최대 한 번", prompt)
        self.assertIn("governing repository guide", prompt)
        self.assertIn("`docs/architecture.md`", prompt)
        self.assertIn("`docs/domain-glossary.md`", prompt)
        self.assertIn("Shared document candidate", prompt)
        self.assertIn("Exact producer-owned public production source", prompt)
        self.assertIn("global completion query를 한 번", prompt)
        self.assertIn("`docs/domain-glossary.md`가 mandatory", prompt)
        self.assertIn("`fallbacks`는 실제 사용한 fallback마다", prompt)
        self.assertIn("raw tool 응답을 복사하지 않고 정확히", prompt)
        self.assertIn("identity.current_state_sha", prompt)
        self.assertIn("실제 `git rev-parse HEAD`", prompt)
        self.assertIn("`docs/backend-development-guide.md`", prompt)
        self.assertIn("`docs/backend-test-guide.md`", prompt)
        self.assertIn("repository production declaration/implementation", prompt)
        self.assertIn("가장 가까운 repository test", prompt)
        self.assertIn("controller가 import하는 request/response DTO", prompt)
        self.assertIn("application service method signature", prompt)

    def test_analysis_distinguishes_approved_bridge_from_undecided_seam(self):
        prompt = (ROOT / "prompts" / "triage-analysis.md").read_text(encoding="utf-8")

        self.assertIn("exact producer-owned production symbol", prompt)
        self.assertIn("Requirement 문서의 code/message 정의만으로", prompt)
        self.assertIn("approved-unimplemented", prompt)
        self.assertIn("contract_changes", prompt)
        self.assertIn("producer public bridge", prompt)
        self.assertIn("내부 구현·Repository", prompt)
        self.assertIn("public API bridge", prompt)
        self.assertIn("handoff.graph_allowed=false", prompt)
        self.assertIn("out-of-scope", prompt)
        self.assertIn("preserves_observable_ids", prompt)
        self.assertIn("upstream requirement rework", prompt)
        self.assertIn("published producer error를 consumer-owned error로 바꾸는 선택", prompt)
        self.assertIn("Source confirmation의 `open_questions`를 자동으로 open decision으로 복사하지 않는다", prompt)
        self.assertIn("Production source만으로 구현 선택을 user decision으로 만들지 않는다", prompt)
        self.assertIn("placement·ownership compliance 근거가 아니다", prompt)

    def test_analysis_prompt_exposes_exact_authoring_shape(self):
        prompt = (ROOT / "prompts" / "triage-analysis.md").read_text(encoding="utf-8")

        for field in (
            "intake_amendments",
            "requirement_locators",
            "intent_entries",
            "confirmed_gaps",
            "subject",
            "current_state",
            "required_state",
            "requirement_ids",
            "affected_observable_ids",
            "finding_ids",
            "observable_id",
            "current_coverage",
            "production_seam",
            "coverage_gap",
            "required_verification",
            "gap_ids",
            "acceptance",
            "verification_ids",
            "document",
            "disposition",
            "why_unresolved",
            "approved_option_id",
        ):
            self.assertIn(field, prompt)
        self.assertIn("변경이 없어도", prompt)
        self.assertIn("아래 필드만 사용한다", prompt)
        self.assertIn("change_requirements[].id", prompt)
        self.assertIn("preserved observable마다 정확히 한 행", prompt)
        self.assertIn("not-applicable`을 제외한", prompt)
        self.assertIn("`A 또는 B`, `A/B`, `사용하거나`", prompt)
        self.assertIn("`current_coverage=partial`인 모든 verification row", prompt)
        self.assertIn("`intake_amendments.intent_entries`", prompt)
        self.assertIn("baseline requirement/current-state document", prompt)
        self.assertIn("같은 document의 locator", prompt)
        self.assertIn("`intent.` prefix를 붙이지 않는다", prompt)
        self.assertIn("`changes`는 string이나 배열이 아니라", prompt)
        self.assertIn("`changes`에는 `source`만 허용", prompt)
        self.assertIn("outcome·behavior·quality gate 의미를 수정하지 않는다", prompt)
        self.assertIn("모든 source object에서 `kind`를 제거", prompt)
        self.assertIn("구조 검증은 별도 중복 row가 아니라", prompt)
        self.assertIn("`depends_on`은 항상 빈 배열", prompt)
        self.assertIn("같은 verification row에서 같은 evidence locator를 두 번 사용하지 않는다", prompt)
        self.assertIn("같은 규칙은 confirmed gap·contract change", prompt)
        self.assertIn("다른 confirmed locator의 claim을 옮겨 적지 않는다", prompt)
        self.assertIn("downstream Impl context로 복제되는 모든 claim", prompt)
        self.assertIn("`Order#changeQuantity`", prompt)
        self.assertIn("source-confirmed target package", prompt)
        self.assertIn("HTTP binding/input-format outcome", prompt)
        self.assertIn("application command/result boundary outcome", prompt)
        self.assertIn("마지막 finalization slice", prompt)
        self.assertIn("production change 없음; verification만 소유", prompt)
        self.assertIn("동일 selector의 path·short-name·괄호 variant", prompt)
        self.assertIn("METHOD_PLAN_DISPOSITION_REQUIRED", prompt)
        self.assertIn("PLANNED_METHOD_SIGNATURE_REQUIRED", prompt)
        self.assertIn("HTTP_DTO_OPERATION_SIGNATURE_PLAN_REQUIRED", prompt)
        self.assertIn("ACCEPTANCE_PRODUCTION_METHOD_REQUIRED", prompt)
        self.assertIn("`create|modify|move-source|move-target|delete|preserve|verify`", prompt)
        self.assertIn("`Owner#method(...)`", prompt)
        self.assertIn("HTTP request", prompt)
        self.assertIn("application result", prompt)
        self.assertIn("criterion 본문에 exact `Owner#method`", prompt)
        self.assertIn("모든 `verify:` scope 항목", prompt)
        self.assertIn("`verification_matrix[].required_verification`에도 exact하게", prompt)
        self.assertIn("서로 다른 boundary·layer 규칙", prompt)

    def test_source_confirmation_classifies_public_contract_state(self):
        prompt = (ROOT / "prompts" / "triage-source-confirmation.md").read_text(encoding="utf-8")

        self.assertIn("public_contract", prompt)
        self.assertIn("published|approved-unimplemented|undecided", prompt)
        self.assertIn("approval_evidence", prompt)

    def test_impl_runtime_requires_semantic_parents_for_final_integration_verification(self):
        runtime = (
            ROOT / "skills" / "kwc-impl-authoring-runtime" / "SKILL.md"
        ).read_text(encoding="utf-8")
        graph_contract = (
            ROOT
            / "skills"
            / "kwc-build-task-graph"
            / "references"
            / "implementation-card-contract.md"
        ).read_text(encoding="utf-8")

        for text in (runtime, graph_contract):
            self.assertIn("final integration/regression", text)
            self.assertIn("모든 owner card", text)
            self.assertIn("verify", text)
            self.assertIn("boundary.in_scope", text)
            self.assertIn("verification", text)
            self.assertIn("depends_on_ordinals", text)
            self.assertIn("dependency_audit.edges[]", text)
        self.assertIn("stale batch를 validate하지 않는다", runtime)
        self.assertIn("unrelated existing test", runtime)
        self.assertIn("Triage가 요구한 decisive", runtime)
        self.assertIn("planned method name", runtime)

    def test_triage_allows_explicit_cross_slice_finalization_verification(self):
        triage = (ROOT / "skills" / "kwc-create-triage" / "SKILL.md").read_text(
            encoding="utf-8"
        )

        self.assertIn("cross-slice integration/structure verification", triage)
        self.assertIn("production 변경을 다시 소유하지 않는다", triage)
        self.assertIn("semantic parent", triage)


if __name__ == "__main__":
    unittest.main()