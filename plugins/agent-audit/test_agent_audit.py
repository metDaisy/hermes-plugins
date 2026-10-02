"""Regression tests for the project-local agent-audit hook."""

from __future__ import annotations

import importlib.util
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory


_PLUGIN = Path(__file__).with_name("__init__.py")
_SPEC = importlib.util.spec_from_file_location("agent_audit", _PLUGIN)
assert _SPEC and _SPEC.loader
_AUDIT = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_AUDIT)
_ORIGINAL_LOAD_RULE_MAPPING = _AUDIT._load_rule_mapping


def _configured_rule_mapping() -> dict:
    layer_prefixes = [
        f"src/main/java/com/example/{domain}/{layer}/"
        for domain in ("orders", "catalog", "billing")
        for layer in ("presentation", "application", "domain", "infra")
    ]
    return {
        "path_rules": [
            {"rule_id": "STYLE-JAVA-001", "prefixes": ["src/main/java/", "src/test/java/"]},
            {"rule_id": "TEST-JAVA-001", "prefixes": ["src/main/java/", "src/test/java/"]},
            {
                "rule_id": "ARCH-MOD-001",
                "prefixes": [
                    f"src/main/java/com/example/{module}/"
                    for module in ("orders", "users", "address", "catalog", "shared", "common")
                ],
            },
            {"rule_id": "ARCH-LAYER-001", "prefixes": layer_prefixes},
        ],
        "validator_rules": [
            {"contains": ["checkstyle"], "rule_ids": ["STYLE-JAVA-001"]},
            {
                "contains": ["modularitytest", "modularity"],
                "rule_ids": ["ARCH-MOD-001", "ARCH-LAYER-001"],
            },
            {"contains": ["test", "junit", "integrationtest"], "rule_ids": ["TEST-JAVA-001"]},
        ],
    }


def _reset() -> list[dict]:
    _AUDIT._VALIDATION_STATE.clear()
    _AUDIT._current_profile_name = lambda: None
    _AUDIT._load_rule_mapping = lambda: _AUDIT._normalize_rule_mapping(_configured_rule_mapping())
    events: list[dict] = []
    _AUDIT._write = lambda event, **fields: events.append(
        {"event": event, **{key: value for key, value in fields.items() if value is not None}}
    )
    return events


def test_rule_mapping_is_loaded_from_plugin_config() -> None:
    import hermes_cli.config as hermes_config

    original_loader = hermes_config.load_config_readonly
    hermes_config.load_config_readonly = lambda: {
        "plugins": {
            "entries": {
                "agent-audit": {
                    "rule_mapping": {
                        "path_rules": [{"rule_id": "STYLE-CUSTOM-001", "prefixes": ["app/"]}],
                        "validator_rules": [{"contains": ["custom-check"], "rule_ids": ["STYLE-CUSTOM-001"]}],
                    }
                }
            }
        }
    }
    try:
        mapping = _ORIGINAL_LOAD_RULE_MAPPING()
    finally:
        hermes_config.load_config_readonly = original_loader

    assert mapping == {
        "path_rules": [{"rule_ids": ["STYLE-CUSTOM-001"], "prefixes": ["app/"]}],
        "validator_rules": [{"contains": ["custom-check"], "rule_ids": ["STYLE-CUSTOM-001"]}],
    }


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
    assert summary == "BUILD FAILED"


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


def test_tool_call_records_profile_name_without_other_runtime_context() -> None:
    events = _reset()

    _AUDIT._on_post_tool_call(
        tool_name="read_file",
        args={"path": "docs/index.md"},
        status="success",
        session_id="session-profile",
        profile_name="project-manager",
    )

    tool_call = next(event for event in events if event["event"] == "tool_call")
    assert tool_call["profile_name"] == "project-manager"
    assert "args" not in tool_call


def test_terminal_tool_call_records_the_full_redacted_command() -> None:
    events = _reset()
    long_suffix = "x" * 1600

    _AUDIT._on_post_tool_call(
        tool_name="terminal",
        args={
            "command": (
                "curl -H 'Authorization: Bearer top-secret-token' "
                "C:/Users/leee/IdeaProjects/hermes-plugins/api && npm test && "
                + long_suffix
            )
        },
        status="success",
        session_id="session-terminal-command",
    )

    tool_call = next(event for event in events if event["event"] == "tool_call")
    command = tool_call["command"]
    assert command.startswith("curl -H 'Authorization:")
    assert "<project>/api && npm test && " in command
    assert "top-secret-token" not in command
    assert "C:/Users" not in command
    assert command.endswith(long_suffix)
    assert len(command) > 1200


def test_terminal_command_redaction_preserves_urls_and_hides_complete_absolute_paths() -> None:
    command = (
        'git clone https://example.com/org/repo.git && '
        'cat "/tmp/My Secrets/private.txt" && '
        'type "C:/Users/leee/My Projects/private.txt" && '
        'type "\\\\server\\share\\Private Folder\\secret.txt"'
    )

    sanitized = _AUDIT._safe_terminal_command("terminal", {"command": command})

    assert "https://example.com/org/repo.git" in sanitized
    assert sanitized.count("<absolute-path>") == 3
    for secret_part in ("My Secrets", "My Projects", "server", "Private Folder"):
        assert secret_part not in sanitized


def test_failed_terminal_call_records_only_compact_redacted_failure_evidence() -> None:
    events = _reset()

    _AUDIT._on_post_tool_call(
        tool_name="terminal",
        args={"command": "npm test"},
        result={
            "output": (
                "customer record: Alice Example\n"
                "token=top-secret-token\n"
                "3 tests failed: ProjectServiceTest\n"
                "unrelated source line after the diagnostic"
            ),
            "exit_code": 1,
            "error": None,
        },
        status="error",
        session_id="session-terminal-failure",
    )

    tool_call = next(event for event in events if event["event"] == "tool_call")
    assert tool_call["command"] == "npm test"
    assert tool_call["exit_code"] == 1
    assert tool_call["failure_type"] == "nonzero_exit"
    assert tool_call["failure_summary"].endswith("3 tests failed: ProjectServiceTest")
    assert "top-secret-token" not in tool_call["failure_summary"]
    assert "Alice Example" not in tool_call["failure_summary"]
    assert "unrelated source" not in tool_call["failure_summary"]
    assert "result" not in tool_call


def test_nonzero_terminal_exit_code_is_failure_even_when_status_says_success() -> None:
    events = _reset()

    _AUDIT._on_post_tool_call(
        tool_name="terminal",
        args={"command": "npm test"},
        result={"output": "", "exit_code": 2, "error": None},
        status="success",
        session_id="session-terminal-nonzero",
    )

    tool_call = next(event for event in events if event["event"] == "tool_call")
    assert tool_call["exit_code"] == 2
    assert tool_call["failure_type"] == "nonzero_exit"
    assert tool_call["failure_summary"] == "Command failed with exit code 2"


def test_retention_rolls_up_and_deletes_only_details_older_than_30_days() -> None:
    with TemporaryDirectory() as directory:
        database_path = Path(directory) / "audit.db"
        connection = sqlite3.connect(database_path)
        try:
            _AUDIT._initialize_database(connection)
            rows = [
                ("2026-08-31T23:59:59+00:00", "shop", "main", "old-session", "tool_call", "success"),
                ("2026-09-15T00:00:00+00:00", "shop", "main", "recent-session", "tool_call", "error"),
            ]
            connection.executemany(
                "INSERT INTO audit_events (timestamp, project_name, profile_name, session_id, "
                "event_type, status, payload_json) VALUES (?, ?, ?, ?, ?, ?, '{}')",
                rows,
            )

            _AUDIT._apply_retention(
                connection,
                datetime(2026, 10, 2, 0, 0, tzinfo=timezone.utc),
            )

            remaining = connection.execute(
                "SELECT timestamp FROM audit_events ORDER BY timestamp"
            ).fetchall()
            rollup = connection.execute(
                "SELECT date, project_name, profile_name, event_count, succeeded_count, "
                "failed_count, session_count FROM audit_daily_rollups"
            ).fetchone()
        finally:
            connection.close()

    assert remaining == [("2026-09-15T00:00:00+00:00",)]
    assert rollup == ("2026-08-31", "shop", "main", 1, 1, 0, 1)


def test_skill_view_tool_call_records_the_requested_skill_name() -> None:
    events = _reset()

    _AUDIT._on_post_tool_call(
        tool_name="skill_view",
        args={"name": "frontend-design"},
        status="success",
        session_id="session-skill-view",
    )

    tool_call = next(event for event in events if event["event"] == "tool_call")
    assert tool_call["skill"] == "frontend-design"


def test_writer_records_privacy_safe_project_name_from_current_directory() -> None:
    events = _reset()
    original = _AUDIT._current_project_name
    try:
        _AUDIT._current_project_name = lambda: "hermes-plugins"
        _AUDIT._on_post_tool_call(
            tool_name="read_file",
            args={"path": "docs/index.md"},
            status="success",
            session_id="session-project",
            profile_name="main",
        )
    finally:
        _AUDIT._current_project_name = original

    tool_call = next(event for event in events if event["event"] == "tool_call")
    assert tool_call["project_name"] == "hermes-plugins"


def test_tool_call_derives_profile_name_when_hook_context_omits_it() -> None:
    events = _reset()
    original = _AUDIT._current_profile_name
    try:
        _AUDIT._current_profile_name = lambda: "coder"
        _AUDIT._on_post_tool_call(
            tool_name="read_file",
            args={"path": "docs/index.md"},
            status="success",
            session_id="session-derived-profile",
        )
        tool_call = next(event for event in events if event["event"] == "tool_call")
    finally:
        _AUDIT._current_profile_name = original

    assert tool_call["profile_name"] == "coder"


def test_write_persists_privacy_safe_profile_event_in_sqlite() -> None:
    with TemporaryDirectory() as directory:
        database_path = Path(directory) / "audit.db"
        spec = importlib.util.spec_from_file_location("agent_audit_sqlite", _PLUGIN)
        assert spec and spec.loader
        audit = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(audit)
        audit._db_path = lambda: database_path

        audit._write(
            "tool_call",
            profile_name="project-manager",
            session_id="session-audit-db",
            status="success",
            tool="read_file",
            unsafe_prompt="must not be stored",
        )

        connection = sqlite3.connect(database_path)
        try:
            row = connection.execute(
                "SELECT profile_name, event_type, status, payload_json FROM audit_events"
            ).fetchone()
        finally:
            connection.close()

    assert row[:3] == ("project-manager", "tool_call", "success")
    assert "unsafe_prompt" not in row[3]


def test_model_context_distinguishes_cloud_and_local_models() -> None:
    assert _AUDIT._model_kind("openai-codex", "https://api.openai.com/v1") == "cloud"
    assert _AUDIT._model_kind("llamacpp", "http://127.0.0.1:8080/v1") == "local"
    assert _AUDIT._model_kind("custom", "http://localhost:9000/v1") == "local"


def test_tool_event_inherits_latest_privacy_safe_model_context() -> None:
    with TemporaryDirectory() as directory:
        database_path = Path(directory) / "audit.db"
        spec = importlib.util.spec_from_file_location("agent_audit_model_context", _PLUGIN)
        assert spec and spec.loader
        audit = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(audit)
        audit._db_path = lambda: database_path

        audit._on_pre_api_request(
            model="Qwen3-Coder-30B-A3B-Instruct",
            provider="llamacpp",
            base_url="http://127.0.0.1:8080/v1",
            request={"body": {"reasoning": {"effort": "high"}}},
            session_id="session-local-model",
            turn_id="turn-local-model",
            profile_name="main",
        )
        audit._on_post_tool_call(
            tool_name="read_file",
            args={"path": "docs/index.md"},
            status="success",
            session_id="session-local-model",
            turn_id="turn-local-model",
            profile_name="main",
        )

        connection = sqlite3.connect(database_path)
        try:
            payload = connection.execute(
                "SELECT payload_json FROM audit_events WHERE event_type = 'tool_call'"
            ).fetchone()[0]
        finally:
            connection.close()

    assert '"model":"Qwen3-Coder-30B-A3B-Instruct"' in payload
    assert '"model_provider":"llamacpp"' in payload
    assert '"model_kind":"local"' in payload
    assert '"reasoning_effort":"high"' in payload


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
    _reset()
    assert _AUDIT._rule_ids_for_paths(
        ["src/main/java/com/example/orders/package-info.java"]
    ) == [
        "ARCH-MOD-001",
        "STYLE-JAVA-001",
        "TEST-JAVA-001",
    ]


def test_architecture_scope_matches_current_modularity_tests() -> None:
    _reset()
    assert "ARCH-MOD-001" in _AUDIT._rule_ids_for_paths(
        ["src/main/java/com/example/users/domain/entity/User.java"]
    )
    assert "ARCH-LAYER-001" not in _AUDIT._rule_ids_for_paths(
        ["src/main/java/com/example/users/domain/entity/User.java"]
    )
    assert "ARCH-MOD-001" in _AUDIT._rule_ids_for_paths(
        ["src/main/java/com/example/common/security/config/SecurityConfig.java"]
    )
    assert "ARCH-LAYER-001" not in _AUDIT._rule_ids_for_paths(
        ["src/main/java/com/example/common/security/config/SecurityConfig.java"]
    )
    assert "ARCH-LAYER-001" in _AUDIT._rule_ids_for_paths(
        ["src/main/java/com/example/orders/presentation/OrderController.java"]
    )


def test_modularity_result_only_updates_architecture_rules() -> None:
    events = _reset()
    session = "session-architecture"
    _change(session, "src/main/java/com/example/orders/presentation/OrderController.java")
    _tool(
        session,
        {"commandLine": ":test --tests com.example.ModularityTest"},
        "BUILD SUCCESSFUL",
    )
    assert events[-1]["event"] == "validation_result"
    assert events[-1]["rules"] == ["ARCH-LAYER-001", "ARCH-MOD-001"]


def test_discovery_tools_record_only_safe_operation_metadata() -> None:
    events = _reset()
    session = "session-discovery"

    _AUDIT._on_post_tool_call(
        tool_name="mcp__semble_mcp__semble__search",
        args={"query": "sensitive implementation question", "repo": "C:/private/repo"},
        result={"content": "raw source result"},
        status="success",
        session_id=session,
        turn_id="turn-semble",
    )
    _AUDIT._on_post_tool_call(
        tool_name="mcp__codebase_memory__codebase_memory__search_graph",
        args={"project": "private-project", "query": "caller relationship"},
        result={"nodes": ["SensitiveSymbol"]},
        status="success",
        session_id=session,
        turn_id="turn-codebase",
    )

    discovery = [event for event in events if event["event"] == "discovery_observation"]
    assert discovery == [
        {
            "event": "discovery_observation",
            "provider": "semble",
            "operation": "search",
            "status": "success",
            "session_id": session,
            "turn_id": "turn-semble",
        },
        {
            "event": "discovery_observation",
            "provider": "codebase-memory",
            "operation": "search_graph",
            "status": "success",
            "session_id": session,
            "turn_id": "turn-codebase",
        },
    ]
    assert "sensitive implementation question" not in str(discovery)
    assert "SensitiveSymbol" not in str(discovery)


def test_kanban_create_without_discovery_records_non_blocking_deviation() -> None:
    events = _reset()

    _AUDIT._on_post_tool_call(
        tool_name="terminal",
        args={"command": "hermes --profile project-manager kanban --board issue-20 create --title task"},
        result="created t_aaaaaaaa",
        status="success",
        session_id="session-no-discovery",
        turn_id="turn-create",
    )

    assert events[-1] == {
        "event": "workflow_deviation",
        "category": "planning_discovery",
        "reason": "kanban_create_without_expected_discovery",
        "expected_providers": ["codebase-memory", "semble"],
        "observed_providers": [],
        "session_id": "session-no-discovery",
        "turn_id": "turn-create",
    }


def test_kanban_create_after_both_discovery_providers_has_no_deviation() -> None:
    events = _reset()
    session = "session-complete-discovery"
    for tool_name in (
        "mcp__semble_mcp__semble__search",
        "mcp__codebase_memory__codebase_memory__trace_path",
    ):
        _AUDIT._on_post_tool_call(
            tool_name=tool_name,
            args={},
            result={},
            status="success",
            session_id=session,
            turn_id="turn-discovery",
        )

    before_create = len([event for event in events if event["event"] == "workflow_deviation"])
    _AUDIT._on_post_tool_call(
        tool_name="terminal",
        args={"command": "hermes --profile project-manager kanban --board issue-20 create --title task"},
        result="created t_aaaaaaaa",
        status="success",
        session_id=session,
        turn_id="turn-create",
    )

    after_create = len([event for event in events if event["event"] == "workflow_deviation"])
    assert after_create == before_create


def test_terminal_discovery_fallback_records_provider_without_command() -> None:
    events = _reset()
    session = "session-terminal-discovery"
    for command in (
        'semble search "private query" C:/private/repo',
        'codebase-memory search_graph --project private-project --query "private relationship"',
    ):
        _AUDIT._on_post_tool_call(
            tool_name="terminal",
            args={"command": command},
            result="private raw result",
            status="success",
            session_id=session,
            turn_id="turn-terminal",
        )

    discovery = [event for event in events if event["event"] == "discovery_observation"]
    assert [(event["provider"], event["operation"]) for event in discovery] == [
        ("semble", "search"),
        ("codebase-memory", "search_graph"),
    ]
    assert "private query" not in str(discovery)
    assert "private relationship" not in str(discovery)


if __name__ == "__main__":
    for name, test in sorted(globals().items()):
        if name.startswith("test_"):
            test()
    print("agent-audit state tests: passed")
