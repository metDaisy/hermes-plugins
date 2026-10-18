import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


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

    def test_discovery_prompt_exposes_bounded_exact_authoring_shape(self):
        prompt = (ROOT / "prompts" / "triage-discovery.md").read_text(encoding="utf-8")

        self.assertIn("bounded discovery", prompt)
        self.assertIn("최대 5개", prompt)
        self.assertIn("같은 target에 검색과 trace를 연속 실행", prompt)
        self.assertIn("nested breakdown이 아니라", prompt)
        self.assertIn('"tool_usage": {"codebase_memory": 11, "semble": 1}', prompt)
        self.assertIn("없으면 `null`", prompt)
        self.assertIn("호출 **하나만**", prompt)
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
        self.assertIn("`OFFER-001/404`를 Cart-owned error로 바꾸는 선택", prompt)
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
        self.assertIn("모든 source object에서 `kind`를 제거", prompt)
        self.assertIn("구조 검증은 별도 중복 row가 아니라", prompt)

    def test_source_confirmation_classifies_public_contract_state(self):
        prompt = (ROOT / "prompts" / "triage-source-confirmation.md").read_text(encoding="utf-8")

        self.assertIn("public_contract", prompt)
        self.assertIn("published|approved-unimplemented|undecided", prompt)
        self.assertIn("approval_evidence", prompt)


if __name__ == "__main__":
    unittest.main()