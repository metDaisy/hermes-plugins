"""Source-level interaction contract for the uncompiled Desktop plugin."""
from pathlib import Path


_PLUGIN = Path(__file__).parent / "desktop" / "plugin.js"


def test_selected_detail_is_inline_and_accessible() -> None:
    source = _PLUGIN.read_text(encoding="utf-8")
    timeline = source[source.index("function Timeline"):source.index("function Pagination")]

    assert "상세 내용이 위에 표시됩니다" not in source
    assert "aria-expanded" in source
    assert "aria-controls" in source
    assert "selectedId" in timeline
    assert "jsx(Details" in timeline


def test_model_and_reasoning_effort_are_not_repeated_in_details() -> None:
    source = _PLUGIN.read_text(encoding="utf-8")
    details = source[source.index("function Details"):source.index("function Timeline")]

    assert "reasoning_effort" in source
    assert "function ModelLine" not in source
    assert "jsx(ModelLine" not in details
    assert "사용 모델" not in details
    assert "추론 강도" not in details


def test_project_selector_precedes_native_secondary_filters() -> None:
    source = _PLUGIN.read_text(encoding="utf-8")
    filters = source[source.index("function Filters"):source.index("function ModelBadge")]

    import_line = source.splitlines()[2]
    for component in ("Popover", "PopoverContent", "PopoverTrigger", "Checkbox"):
        assert component in import_line
    assert "DropdownMenuCheckboxItem" not in import_line
    assert "jsx('select'" not in source
    assert "function ProjectSelector" in source
    assert source.index("function ProjectSelector") < source.index("function Filters")
    assert filters.index("Session") < filters.index("Profile") < filters.index("활동 유형") < filters.index("결과")
    assert "모든 Project" not in filters
    assert "모든 Session" in filters
    assert "session_id: filters.session" in source
    assert "useAuditSessions" in source
    assert "toggleFilterValue" in filters
    assert "onCheckedChange" in filters
    assert "role: 'checkbox'" in filters
    assert "open, setOpen" in filters


def test_row_omits_redundant_project_and_keeps_colored_profile() -> None:
    source = _PLUGIN.read_text(encoding="utf-8")
    row = source[source.index("function EventRow"):source.index("function DetailValue")]

    assert "function ProjectBadge" not in source
    assert "function ProfileBadge" in source
    assert "profileTone" in source
    assert "jsx(ProjectBadge" not in row
    assert "'aria-label': `${heading}" not in row


def test_model_badge_uses_compact_parenthesized_effort() -> None:
    source = _PLUGIN.read_text(encoding="utf-8")
    badge = source[source.index("function ModelBadge"):source.index("function EventRow")]

    assert "`${name}(${effort})`" in badge
    assert "추론" not in badge


def test_terminal_command_is_collapsed_behind_an_accessible_more_control() -> None:
    source = _PLUGIN.read_text(encoding="utf-8")
    details = source[source.index("function Details"):source.index("function Timeline")]

    assert "function CommandDetails" in source
    assert "jsxs('details'" in source
    assert "실행 명령 더보기" in source
    assert "jsx(CommandDetails, { command })" in details
    assert "jsx('pre'" not in details


def test_session_title_is_clickable_and_internal_ids_are_hidden() -> None:
    source = _PLUGIN.read_text(encoding="utf-8")
    details = source[source.index("function Details"):source.index("function Timeline")]

    assert "host.openSession" in source
    assert "session?.title" in source
    assert "function TechnicalLine" in source
    assert "jsx(TechnicalLine" in details
    assert "['Task'" not in details
    assert "['Turn'" not in details
    assert "['Metadata'" not in details


def test_technical_information_is_one_useful_line() -> None:
    source = _PLUGIN.read_text(encoding="utf-8")
    technical = source[source.index("function TechnicalLine"):source.index("function SessionLink")]
    details = source[source.index("function Details"):source.index("function Timeline")]

    assert "기술 정보 보기" not in details
    assert "jsx('details'" not in details
    assert "도구" in technical
    assert "event.activity?.tool || event.tool" in technical
    assert "Skill" in technical
    assert "event.activity?.skill || event.skill" in technical
    assert "검증 도구" in technical
    assert "코드 조사 도구" in technical
    assert "['시간'" not in details
    assert "['Profile'" not in details
    assert "['Event type'" not in details
    assert "['Reason'" not in details


def test_common_agent_actions_use_plain_language() -> None:
    source = _PLUGIN.read_text(encoding="utf-8")

    assert "명령 실행" in source
    assert "Desktop 조작" in source
    assert "웹 정보 확인" in source
    assert "코드 구조 조사" in source
    assert "작업 계획 갱신" in source
    assert "const activityCode" in source
    assert source.count("activityCode(event)") >= 2
    assert "파일 내용 확인" in source
    assert "읽음" in source
    assert "파일 내용을 확인했습니다" not in source
    assert "Profile이" not in source
    assert "Skill 지침 확인 ·" in source
    assert "실행 명령" in source


def test_detail_uses_plain_result_and_explains_targets_and_rules() -> None:
    source = _PLUGIN.read_text(encoding="utf-8")
    details = source[source.index("function Details"):source.index("function Timeline")]

    assert "`결과 · ${statusLabel(state)}`" not in details
    assert "children: statusLabel(state)" in details
    assert "function TargetDetails" in source
    assert "다국어(i18n)" in source
    assert "검색 위치" in source
    assert "확인한 파일" in source
    assert "수정한 파일" in source
    assert "검증 규칙" in source
    assert "Rule" not in details
    assert "hasDetailValue" in source


def test_project_summary_only_contains_requested_decision_metrics() -> None:
    source = _PLUGIN.read_text(encoding="utf-8")

    assert "function ProjectSummary" in source
    assert "useAuditInsights" in source
    assert "api(`/insights" in source
    assert "주요 활동" not in source
    assert "사용 도구" not in source
    assert "파일 작업" not in source
    assert "최근 활동" not in source


def test_project_first_page_requires_one_project_before_showing_metrics_and_timeline() -> None:
    source = _PLUGIN.read_text(encoding="utf-8")
    page = source[source.index("function Page"):source.index("export default")]

    assert "function ProjectSelector" in source
    assert "프로젝트 선택" in source
    assert "프로젝트를 선택하면 해당 프로젝트의 기록과 세션을 표시합니다." in source
    assert page.index("jsx(ProjectSelector") < page.index("jsx(ProjectSummary") < page.index("jsx(Filters") < page.index("jsx(Timeline")
    assert "enabled: filters.project.length === 1" in source
    assert "기록" in source
    assert "세션" in source
    assert "성공률" in source
    assert "실패" in source


if __name__ == "__main__":
    test_selected_detail_is_inline_and_accessible()
    test_model_and_reasoning_effort_are_not_repeated_in_details()
    test_project_selector_precedes_native_secondary_filters()
    test_row_omits_redundant_project_and_keeps_colored_profile()
    test_model_badge_uses_compact_parenthesized_effort()
    test_terminal_command_is_collapsed_behind_an_accessible_more_control()
    test_session_title_is_clickable_and_internal_ids_are_hidden()
    test_technical_information_is_one_useful_line()
    test_common_agent_actions_use_plain_language()
    test_detail_uses_plain_result_and_explains_targets_and_rules()
    test_project_summary_only_contains_requested_decision_metrics()
    test_project_first_page_requires_one_project_before_showing_metrics_and_timeline()
    print("agent-audit Desktop interaction contract tests: passed")
