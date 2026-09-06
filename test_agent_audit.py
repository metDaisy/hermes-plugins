"""Regression tests for the project-local agent-audit hook."""

from __future__ import annotations

import importlib.util
from pathlib import Path


_PLUGIN = Path(__file__).with_name("__init__.py")
_SPEC = importlib.util.spec_from_file_location("agent_audit", _PLUGIN)
assert _SPEC and _SPEC.loader
_AUDIT = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_AUDIT)


def _reset() -> list[dict]:
    _AUDIT._VALIDATION_STATE.clear()
    events: list[dict] = []
    _AUDIT._write = lambda event, **fields: events.append({"event": event, **fields})
    return events


def _tool(session: str, args: dict, result: str, status: str = "success") -> None:
    _AUDIT._on_post_tool_call(
        tool_name="mcp__gradle_mcp__gradle_mcp__gradle",
        args=args,
        result=result,
        status=status,
        session_id=session,
        turn_id="turn-1",
    )


def _change(session: str, path: str) -> None:
    _AUDIT._on_post_tool_call(
        tool_name="patch",
        args={"path": path},
        status="success",
        session_id=session,
        turn_id="turn-2",
    )


def _patch_change(session: str, path: str) -> None:
    _AUDIT._on_post_tool_call(
        tool_name="patch",
        args={
            "mode": "patch",
            "patch": f"*** Begin Patch\n*** Update File: {path}\n*** End Patch",
        },
        status="success",
        session_id=session,
        turn_id="turn-3",
    )


def test_rule_fan_out_and_freshness() -> None:
    events = _reset()
    session = "session-rules"
    _change(session, "src/main/java/io/github/example/OrderService.java")
    assert events[-1]["event"] == "validation_trigger"
    assert events[-1]["trigger"] == "JAVA-CHANGE-001"
    assert events[-1]["required_rules"] == ["STYLE-JAVA-001", "TEST-JAVA-001"]

    gate = _AUDIT._on_pre_verify(
        coding=True, attempt=0, changed_paths=["src/main/java/io/github/example/OrderService.java"], session_id=session
    )
    assert gate and "STYLE-JAVA-001, TEST-JAVA-001" in gate["message"]
    assert events[-2]["rule_statuses"] == {"STYLE-JAVA-001": "stale", "TEST-JAVA-001": "stale"}
    assert events[-1]["reason"] == "missing_validation"

    _tool(session, {"commandLine": ":checkstyleMain"}, "BUILD SUCCESSFUL")
    assert events[-1]["event"] == "validation_result"
    assert events[-1]["rules"] == ["STYLE-JAVA-001"]
    gate = _AUDIT._on_pre_verify(coding=True, attempt=0, changed_paths=[], session_id=session)
    assert gate and "TEST-JAVA-001" in gate["message"]

    _tool(session, {"commandLine": ":test"}, "BUILD SUCCESSFUL")
    assert _AUDIT._on_pre_verify(coding=True, attempt=0, changed_paths=[], session_id=session) is None

    _change(session, "src/main/java/io/github/example/OrderService.java")
    gate = _AUDIT._on_pre_verify(coding=True, attempt=0, changed_paths=[], session_id=session)
    assert gate and "STYLE-JAVA-001, TEST-JAVA-001" in gate["message"]


def test_failed_rule_summary_is_redacted() -> None:
    _reset()
    session = "session-failure"
    _change(session, "src/main/java/io/github/example/OrderService.java")
    _tool(session, {"commandLine": ":checkstyleMain"}, "token: SECRET BUILD FAILED", status="error")

    gate = _AUDIT._on_pre_verify(coding=True, attempt=0, changed_paths=[], session_id=session)
    assert gate and "STYLE-JAVA-001" in gate["message"]
    assert "SECRET" not in gate["message"]
    assert "[REDACTED]" in gate["message"]


def test_failure_summary_redacts_structured_and_prefixed_credentials() -> None:
    summary = _AUDIT._failure_summary(
        '{"api_key":"sk-test-1234567890", "authorization":"Bearer abc.def.ghi", '
        '"PASSWORD":"plain-password", "message":"BUILD FAILED"}'
    )

    assert "sk-test-1234567890" not in summary
    assert "abc.def.ghi" not in summary
    assert "plain-password" not in summary
    assert summary.count("[REDACTED]") >= 3


def test_java_gate_applies_without_coding_posture() -> None:
    events = _reset()
    session = "session-cli-posture"
    _change(session, "src/main/java/io/github/example/OrderService.java")

    gate = _AUDIT._on_pre_verify(
        coding=False,
        attempt=0,
        changed_paths=["src/main/java/io/github/example/OrderService.java"],
        session_id=session,
    )

    assert gate and "STYLE-JAVA-001" in gate["message"]
    assert events[-2]["event"] == "verification_gate"
    assert events[-1]["event"] == "workflow_deviation"


def test_missing_validation_records_a_workflow_deviation() -> None:
    events = _reset()
    session = "session-missing-validation"
    _change(session, "src/main/java/io/github/example/OrderService.java")

    _AUDIT._on_pre_verify(
        coding=True,
        attempt=0,
        changed_paths=["src/main/java/io/github/example/OrderService.java"],
        session_id=session,
    )

    assert events[-1] == {
        "event": "workflow_deviation",
        "category": "verification",
        "reason": "missing_validation",
        "rules": ["STYLE-JAVA-001", "TEST-JAVA-001"],
        "generation": 1,
        "session_id": session,
    }


def test_direct_gradle_invocation_records_a_policy_deviation() -> None:
    events = _reset()

    _AUDIT._on_post_tool_call(
        tool_name="terminal",
        args={"command": "./gradlew test"},
        status="success",
        session_id="session-direct-gradle",
        turn_id="turn-direct-gradle",
    )

    assert events[-1] == {
        "event": "workflow_deviation",
        "category": "tool_policy",
        "reason": "direct_gradle_invocation",
        "rule_id": "GRADLE-MCP-001",
        "session_id": "session-direct-gradle",
        "turn_id": "turn-direct-gradle",
    }


def test_patch_add_and_delete_headers_preserve_paths() -> None:
    path = "src/main/java/io/github/example/OrderService.java"
    for header in ("Add", "Delete"):
        patch_text = f"*** Begin Patch\n*** {header} File: {path}\n*** End Patch"
        assert _AUDIT._paths_from_args({"patch": patch_text}) == [path]


def test_unresolved_mutation_cannot_pass_pre_verify() -> None:
    events = _reset()
    session = "session-unresolved-mutation"
    _AUDIT._on_post_tool_call(
        tool_name="patch",
        args={"mode": "patch", "patch": "*** Begin Patch\n*** End Patch"},
        status="success",
        session_id=session,
        turn_id="turn-unknown",
    )

    gate = _AUDIT._on_pre_verify(coding=False, attempt=0, changed_paths=[], session_id=session)

    assert gate and gate["action"] == "continue"
    assert events[-2]["unresolved_mutation"] is True
    assert events[-1]["reason"] == "unresolved_mutation"


def test_patch_header_preserves_validation_correlation() -> None:
    events = _reset()
    session = "session-patch-correlation"
    path = "src/main/java/io/github/example/OrderService.java"
    _change(session, path)
    _tool(session, {"commandLine": ":checkstyleMain"}, "BUILD FAILED", status="error")
    failed_gate = _AUDIT._on_pre_verify(
        coding=False, attempt=0, changed_paths=[path], session_id=session
    )
    assert failed_gate and events[-2]["action"] == "continue"
    assert events[-1]["reason"] == "failed_validation"

    _patch_change(session, path)
    _tool(session, {"commandLine": ":checkstyleMain"}, "BUILD SUCCESSFUL")
    assert events[-1]["rules"] == ["STYLE-JAVA-001"]
    _tool(session, {"commandLine": ":test"}, "BUILD SUCCESSFUL")
    assert _AUDIT._on_pre_verify(
        coding=False, attempt=0, changed_paths=[path], session_id=session
    ) is None
    assert events[-1]["action"] == "allow"


def test_structural_paths_map_to_architecture_rules() -> None:
    assert _AUDIT._rule_ids_for_paths(
        ["src/main/java/io/github/metdaisy/amaazon/auth/package-info.java"]
    ) == [
        "ARCH-MOD-001",
        "STYLE-JAVA-001",
        "TEST-JAVA-001",
    ]


def test_architecture_scope_matches_current_modularity_tests() -> None:
    assert "ARCH-MOD-001" in _AUDIT._rule_ids_for_paths(
        ["src/main/java/io/github/metdaisy/amaazon/seller/domain/entity/Seller.java"]
    )
    assert "ARCH-LAYER-001" not in _AUDIT._rule_ids_for_paths(
        ["src/main/java/io/github/metdaisy/amaazon/seller/domain/entity/Seller.java"]
    )
    assert "ARCH-MOD-001" in _AUDIT._rule_ids_for_paths(
        ["src/main/java/io/github/metdaisy/amaazon/global/security/config/SecurityConfig.java"]
    )
    assert "ARCH-LAYER-001" not in _AUDIT._rule_ids_for_paths(
        ["src/main/java/io/github/metdaisy/amaazon/global/security/config/SecurityConfig.java"]
    )
    assert "ARCH-LAYER-001" in _AUDIT._rule_ids_for_paths(
        ["src/main/java/io/github/metdaisy/amaazon/auth/presentation/AuthController.java"]
    )


def test_modularity_result_only_updates_architecture_rules() -> None:
    events = _reset()
    session = "session-architecture"
    _change(session, "src/main/java/io/github/metdaisy/amaazon/auth/presentation/AuthController.java")
    _tool(
        session,
        {"commandLine": ":test --tests io.github.metdaisy.amaazon.ModularityTest"},
        "BUILD SUCCESSFUL",
    )
    assert events[-1]["event"] == "validation_result"
    assert events[-1]["rules"] == ["ARCH-LAYER-001", "ARCH-MOD-001"]


if __name__ == "__main__":
    for name, test in sorted(globals().items()):
        if name.startswith("test_"):
            test()
    print("agent-audit state tests: passed")
