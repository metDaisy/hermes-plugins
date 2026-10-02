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


def test_model_identity_is_visible_in_event_details() -> None:
    source = _PLUGIN.read_text(encoding="utf-8")

    assert "사용 모델" in source
    assert "로컬 모델" in source
    assert "클라우드 모델" in source
    assert "모델 미확인" in source
    assert "if (!model?.name) return null" not in source


def test_filters_use_native_hermes_selects_in_project_first_order() -> None:
    source = _PLUGIN.read_text(encoding="utf-8")
    filters = source[source.index("function Filters"):source.index("function ModelBadge")]

    import_line = source.splitlines()[2]
    for component in ("Select", "SelectContent", "SelectItem", "SelectTrigger", "SelectValue"):
        assert component in import_line
    assert "jsx('select'" not in source
    assert filters.index("Project") < filters.index("Profile") < filters.index("활동 유형") < filters.index("결과")
    assert "모든 Project" in filters


def test_common_agent_actions_use_plain_language() -> None:
    source = _PLUGIN.read_text(encoding="utf-8")

    assert "명령을 실행했습니다" in source
    assert "Desktop UI와 상호작용했습니다" in source
    assert "웹에서 정보를 확인했습니다" in source
    assert "코드 구조를 조사했습니다" in source
    assert "작업 계획을 갱신했습니다" in source
    assert "const activityCode" in source
    assert source.count("activityCode(event)") >= 2


if __name__ == "__main__":
    test_selected_detail_is_inline_and_accessible()
    test_model_identity_is_visible_in_event_details()
    test_filters_use_native_hermes_selects_in_project_first_order()
    test_common_agent_actions_use_plain_language()
    print("agent-audit Desktop interaction contract tests: passed")
