"""Project-local, privacy-conscious Hermes audit hooks.

The plugin records compact lifecycle metadata in ``.hermes/events.jsonl``.
It observes discovery and validation without persisting prompts, commands,
tool arguments, or results.
"""

from __future__ import annotations

import json
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_LOCK = threading.Lock()
_STATE_LOCK = threading.Lock()
_VALIDATION_STATE: dict[str, dict[str, Any]] = {}
_MUTATING_TOOLS = {"patch", "write_file", "edit_file", "delete_file"}
_EXPECTED_DISCOVERY_PROVIDERS = {"semble", "codebase-memory"}
_DISCOVERY_OPERATIONS = {
    "semble": {"search", "find_related"},
    "codebase-memory": {"search_graph", "trace_path"},
}
_MAX_FAILURE_SUMMARY = 600
_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_JAVA_SOURCE_PREFIXES = ("src/main/java/", "src/test/java/")
_MAIN_JAVA_PREFIX = "src/main/java/io/github/metdaisy/amaazon/"
_APPLICATION_MODULES = {"auth", "user", "address", "catalog", "seller", "common", "global"}
_LAYER_DOMAINS = {"auth", "user", "catalog"}
_LAYER_SEGMENTS = ("/presentation/", "/application/", "/domain/", "/infra/")


def _project_dir() -> Path:
    """Return the project root that contains this project-local plugin."""
    return _PROJECT_ROOT


def _log_path() -> Path:
    return _project_dir() / ".hermes" / "events.jsonl"


def _safe_path(value: Any) -> str | None:
    """Return a project-relative path without exposing an absolute path."""
    if not isinstance(value, str) or not value:
        return None
    path = Path(value)
    try:
        return path.resolve().relative_to(_project_dir().resolve()).as_posix()
    except (OSError, ValueError):
        return path.name or None


def _safe_paths(values: Any) -> list[str]:
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, (list, tuple)):
        return []
    return sorted({path for path in (_safe_path(value) for value in values) if path})


def _paths_from_args(args: Any) -> list[str]:
    if not isinstance(args, dict):
        return []
    paths = _safe_paths(args.get("path"))
    if paths:
        return paths
    patch_text = args.get("patch")
    if not isinstance(patch_text, str):
        return []
    patch_paths = re.findall(
        r"^\*\*\* (?:Update|Add|Delete) File: ([^\r\n]+)$", patch_text, re.MULTILINE
    )
    return _safe_paths(patch_paths)


def _opaque(value: Any) -> str | None:
    """Keep opaque identifiers useful for correlation but bounded."""
    if value is None:
        return None
    text = str(value)
    return text[:96] if text else None


def _write(event: str, **fields: Any) -> None:
    """Append one compact event; audit failure must never break the Agent."""
    payload = {
        "schema_version": 1,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event": event,
        **{key: value for key, value in fields.items() if value is not None},
    }
    try:
        path = _log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with _LOCK, path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
            stream.write("\n")
    except (OSError, TypeError, ValueError):
        # Observability is fail-open. A broken local log must not break coding.
        return


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(value)


def _redact_sensitive_text(text: str) -> str:
    key_pattern = (
        r"(?:api[_-]?key|authorization|bearer|credential|password|passwd|secret|token)"
    )
    text = re.sub(
        rf"(?i)([\"']?{key_pattern}[\"']?\s*[:=]\s*)(?:\"[^\"]*\"|'[^']*'|[^,\s}}\]]+)",
        r"\1[REDACTED]",
        text,
    )
    text = re.sub(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+", "Bearer [REDACTED]", text)
    text = re.sub(
        r"(?i)\b(?:sk|ghp|github_pat|xox[baprs]-|AIza|AKIA)[A-Za-z0-9_./+=-]{8,}",
        "[REDACTED]",
        text,
    )
    return text


def _failure_summary(result: Any) -> str:
    """Extract a short, path-sanitized diagnostic without persisting raw output."""
    text = _text(result).replace(str(_project_dir()), "<project>")
    text = _redact_sensitive_text(text)
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return " ".join(lines)[-_MAX_FAILURE_SUMMARY:]


def _validation_kind(tool_name: str, args: Any) -> str:
    text = f"{tool_name} {_text(args)}".lower()
    if "checkstyle" in text:
        return "codestyle"
    if "modularitytest" in text or "modularity" in text:
        return "modulith"
    if "gradle-mcp" in text or "gradle_mcp" in text or "gradle" in text:
        return "gradle"
    return "validation"


def _is_direct_gradle_invocation(tool_name: str, args: Any) -> bool:
    """Identify prohibited shell Gradle use without retaining the command."""
    if tool_name != "terminal":
        return False
    text = _text(args).lower()
    return "gradle-mcp" not in text and bool(re.search(r"\b(?:gradlew(?:\.bat)?|gradle)\b", text))


def _looks_failed(status: Any, result: Any) -> bool:
    if str(status).lower() in {"error", "failed", "failure", "blocked", "cancelled"}:
        return True
    text = _text(result)
    return bool(re.search(r"(?im)^\s*BUILD\s+FAILED\b|^\s*FAILURE:\s", text))


def _rule_ids_for_paths(paths: list[str]) -> list[str]:
    """Return stable Rule IDs affected by project-relative changed paths."""
    rule_ids: set[str] = set()
    for path in paths:
        normalized = f"/{path.lstrip('/')}"
        is_main_java = path.startswith("src/main/java/")
        if path.startswith(_JAVA_SOURCE_PREFIXES):
            rule_ids.update({"STYLE-JAVA-001", "TEST-JAVA-001"})
        module = None
        if path.startswith(_MAIN_JAVA_PREFIX):
            module = path[len(_MAIN_JAVA_PREFIX) :].split("/", 1)[0]
        if is_main_java and module in _APPLICATION_MODULES:
            rule_ids.add("ARCH-MOD-001")
        if module in _LAYER_DOMAINS and any(segment in normalized for segment in _LAYER_SEGMENTS):
            rule_ids.add("ARCH-LAYER-001")
    return sorted(rule_ids)


def _rule_ids_for_validation(tool_name: str, args: Any) -> list[str]:
    """Map an observed validator invocation to applicable Rule IDs."""
    text = f"{tool_name} {_text(args)}".lower()
    rule_ids: set[str] = set()
    if "checkstyle" in text:
        rule_ids.add("STYLE-JAVA-001")
    if "modularitytest" in text or "modularity" in text:
        rule_ids.update({"ARCH-MOD-001", "ARCH-LAYER-001"})
    elif re.search(r"\b(test|junit|integrationtest)\b", text):
        rule_ids.add("TEST-JAVA-001")
    return sorted(rule_ids)


def _validation_state(session_id: str) -> dict[str, Any]:
    """Return a mutable, per-session state container."""
    state = _VALIDATION_STATE.setdefault(session_id, {"generation": 0, "rules": {}})
    state.setdefault("generation", 0)
    state.setdefault("rules", {})
    state.setdefault("discovery_providers", set())
    return state


def _discovery_call(tool_name: str, args: Any = None) -> tuple[str, str] | None:
    """Classify semantic discovery without retaining arguments."""
    normalized = tool_name.lower().replace("-", "_")
    operation = normalized.rsplit("__", 1)[-1]
    if "semble" in normalized and operation in _DISCOVERY_OPERATIONS["semble"]:
        return "semble", operation
    if "codebase_memory" in normalized and operation in _DISCOVERY_OPERATIONS["codebase-memory"]:
        return "codebase-memory", operation
    if normalized == "terminal":
        command = _text(args).lower().replace("-", "_")
        semble = re.search(r"\b(search|find_related)\b", command)
        if "semble" in command and semble:
            return "semble", semble.group(1)
        codebase = re.search(r"\b(search_graph|trace_path)\b", command)
        if codebase:
            return "codebase-memory", codebase.group(1)
    return None


def _is_success_status(status: Any) -> bool:
    return str(status).lower() in {"success", "succeeded", "passed", "completed", "ok"}


def _is_kanban_create(tool_name: str, args: Any) -> bool:
    normalized = tool_name.lower().replace("-", "_")
    if "kanban" in normalized and "create" in normalized:
        return True
    if normalized != "terminal":
        return False
    return bool(re.search(r"\bkanban\b.*\bcreate\b", _text(args), re.IGNORECASE))


def _record_mutation(session_id: str, changed_paths: list[str]) -> list[str]:
    affected_rules = _rule_ids_for_paths(changed_paths)
    with _STATE_LOCK:
        state = _validation_state(session_id)
        state["generation"] += 1
        state["last_changed_paths"] = changed_paths
        state["unresolved_mutation"] = not bool(changed_paths)
        generation = state["generation"]
        for rule_id in affected_rules:
            state["rules"][rule_id] = {
                "status": "stale",
                "generation": generation,
                "changed_paths": changed_paths,
                "validator": None,
                "summary": None,
            }
        generation = state["generation"]
    _write(
        "validation_trigger",
        trigger="JAVA-CHANGE-001" if any(path.endswith(".java") for path in changed_paths) else None,
        required_rules=affected_rules,
        changed_paths=changed_paths,
        generation=generation,
        session_id=session_id,
    )
    return affected_rules


def _on_skill_lifecycle(**kwargs: Any) -> None:
    _write(
        "skill_lifecycle",
        action=_opaque(kwargs.get("action")),
        skill=_opaque(kwargs.get("skill_name")),
        provenance=_opaque(kwargs.get("provenance")),
        use_count=kwargs.get("use_count"),
        reused=kwargs.get("reused"),
        session_id=_opaque(kwargs.get("session_id")),
        task_id=_opaque(kwargs.get("task_id")),
    )


def _on_post_tool_call(**kwargs: Any) -> None:
    tool_name = str(kwargs.get("tool_name") or "")
    session_id = _opaque(kwargs.get("session_id")) or "unknown"
    args = kwargs.get("args")
    status = kwargs.get("status")
    paths = _paths_from_args(args)

    _write(
        "tool_call",
        tool=_opaque(tool_name),
        status=_opaque(status),
        duration_ms=kwargs.get("duration_ms"),
        session_id=session_id,
        task_id=_opaque(kwargs.get("task_id")),
        turn_id=_opaque(kwargs.get("turn_id")),
        paths=paths,
    )

    discovery = _discovery_call(tool_name, args)
    if discovery:
        provider, operation = discovery
        _write(
            "discovery_observation",
            provider=provider,
            operation=operation,
            status=_opaque(status),
            session_id=session_id,
            task_id=_opaque(kwargs.get("task_id")),
            turn_id=_opaque(kwargs.get("turn_id")),
        )
        if _is_success_status(status) and not _looks_failed(status, kwargs.get("result")):
            with _STATE_LOCK:
                _validation_state(session_id)["discovery_providers"].add(provider)

    if _is_kanban_create(tool_name, args) and _is_success_status(status):
        with _STATE_LOCK:
            observed = sorted(_validation_state(session_id)["discovery_providers"])
        missing = _EXPECTED_DISCOVERY_PROVIDERS - set(observed)
        if missing:
            _write(
                "workflow_deviation",
                category="planning_discovery",
                reason="kanban_create_without_expected_discovery",
                expected_providers=sorted(_EXPECTED_DISCOVERY_PROVIDERS),
                observed_providers=observed,
                session_id=session_id,
                task_id=_opaque(kwargs.get("task_id")),
                turn_id=_opaque(kwargs.get("turn_id")),
            )

    if _is_direct_gradle_invocation(tool_name, args):
        _write(
            "workflow_deviation",
            category="tool_policy",
            reason="direct_gradle_invocation",
            rule_id="GRADLE-MCP-001",
            session_id=session_id,
            turn_id=_opaque(kwargs.get("turn_id")),
        )

    if tool_name in _MUTATING_TOOLS:
        _record_mutation(session_id, paths)
        return

    if "gradle-mcp" not in tool_name and "gradle_mcp" not in tool_name:
        return

    failed = _looks_failed(status, kwargs.get("result"))
    kind = _validation_kind(tool_name, args)
    rule_ids = _rule_ids_for_validation(tool_name, args)
    validation_status = "failed" if failed else "passed"
    changed_paths: set[str] = set()
    generation = None
    matched_rule_ids: list[str] = []
    with _STATE_LOCK:
        state = _validation_state(session_id)
        generation = state["generation"]
        for rule_id in rule_ids:
            rule_state = state["rules"].get(rule_id)
            if not rule_state or rule_state.get("generation") != generation:
                continue
            rule_state.update(
                {
                    "status": validation_status,
                    "validator": kind,
                    "summary": _failure_summary(kwargs.get("result")) if failed else None,
                    "validated_generation": generation,
                }
            )
            changed_paths.update(rule_state.get("changed_paths", []))
            matched_rule_ids.append(rule_id)
    _write(
        "validation_result",
        validator=kind,
        tool=_opaque(tool_name),
        status=validation_status,
        rules=matched_rule_ids,
        changed_paths=sorted(changed_paths),
        generation=generation,
        session_id=session_id,
        turn_id=_opaque(kwargs.get("turn_id")),
    )


def _on_pre_verify(**kwargs: Any) -> dict[str, str] | None:
    if kwargs.get("attempt", 0):
        return None

    changed_paths = _safe_paths(kwargs.get("changed_paths"))
    session_id = _opaque(kwargs.get("session_id")) or "unknown"
    with _STATE_LOCK:
        state = _VALIDATION_STATE.get(session_id, {})
        required_rules = _rule_ids_for_paths(changed_paths)
        if not changed_paths:
            required_rules = _rule_ids_for_paths(state.get("last_changed_paths", []))
        unresolved_mutation = bool(state.get("unresolved_mutation"))
        if not kwargs.get("coding") and not required_rules and not unresolved_mutation:
            return None
        generation = state.get("generation")
        rule_states = state.get("rules", {})
        rule_statuses: dict[str, str] = {}
        failed_rules: list[tuple[str, dict[str, Any]]] = []
        missing_rules: list[str] = []
        if unresolved_mutation and not required_rules:
            _write(
                "verification_gate",
                session_id=session_id,
                attempt=kwargs.get("attempt"),
                changed_paths=changed_paths,
                validation_status="not_run",
                rule_statuses={},
                missing_rules=[],
                failed_rules=[],
                action="continue",
                unresolved_mutation=True,
                generation=generation,
            )
            _write(
                "workflow_deviation",
                category="verification",
                reason="unresolved_mutation",
                rules=[],
                generation=generation,
                session_id=session_id,
            )
            return {
                "action": "continue",
                "message": (
                    "변경된 파일 경로를 확인하지 못해 검증을 완료할 수 없습니다. "
                    "변경 경로를 명시한 도구로 다시 수정한 뒤 필요한 validator를 실행하세요."
                ),
            }
        for rule_id in required_rules:
            rule_state = rule_states.get(rule_id, {})
            if rule_state.get("generation") != generation:
                status = "not_run"
            else:
                status = str(rule_state.get("status") or "not_run")
            rule_statuses[rule_id] = status
            if status == "failed":
                failed_rules.append((rule_id, rule_state))
            elif status != "passed":
                missing_rules.append(rule_id)
        legacy_failed = state.get("status") == "failed" and not required_rules
        overall_status = (
            "failed"
            if failed_rules or legacy_failed
            else "passed"
            if required_rules and not missing_rules
            else "not_run"
            if required_rules
            else state.get("status")
        )
    _write(
        "verification_gate",
        session_id=session_id,
        attempt=kwargs.get("attempt"),
        changed_paths=changed_paths,
        validation_status=overall_status,
        rule_statuses=rule_statuses,
        missing_rules=missing_rules,
        failed_rules=[rule_id for rule_id, _ in failed_rules],
        action="continue" if failed_rules or missing_rules or legacy_failed else "allow",
        generation=generation,
    )
    if failed_rules or legacy_failed or missing_rules:
        _write(
            "workflow_deviation",
            category="verification",
            reason="failed_validation" if failed_rules or legacy_failed else "missing_validation",
            rules=[rule_id for rule_id, _ in failed_rules] if failed_rules else missing_rules,
            generation=generation,
            session_id=session_id,
        )

    if failed_rules or legacy_failed:
        if failed_rules:
            rule_id, failed_state = failed_rules[0]
            validator = failed_state.get("validator") or "validation"
            summary = failed_state.get("summary") or "상세 결과를 다시 확인하세요."
        else:
            rule_id = "validation"
            validator = state.get("kind", "validation")
            summary = state.get("summary", "상세 결과를 다시 확인하세요.")
        return {
            "action": "continue",
            "message": (
                "최근 결정론적 검증이 실패했습니다. "
                f"({rule_id}, {validator}) 실패 요약: {summary} "
                "원인을 수정한 뒤 동일한 검증을 다시 실행하고, 결과를 확인한 후 완료하세요."
            ),
        }

    if missing_rules:
        return {
            "action": "continue",
            "message": (
                "변경된 파일에 필요한 결정론적 검증이 아직 완료되지 않았습니다. "
                f"Rule ID: {', '.join(missing_rules)}. "
                "각 Rule에 해당하는 validator를 실행하고, 실패하면 원인을 수정한 뒤 다시 검증하세요."
            ),
        }
    return None


def _on_session_end(**kwargs: Any) -> None:
    session_id = _opaque(kwargs.get("session_id"))
    _write(
        "session_end",
        session_id=session_id,
        completed=kwargs.get("completed"),
        failed=kwargs.get("failed"),
        interrupted=kwargs.get("interrupted"),
        turn_exit_reason=_opaque(kwargs.get("turn_exit_reason")),
    )
    if session_id:
        with _STATE_LOCK:
            _VALIDATION_STATE.pop(session_id, None)


def register(ctx: Any) -> None:
    ctx.register_hook("on_skill_lifecycle", _on_skill_lifecycle)
    ctx.register_hook("post_tool_call", _on_post_tool_call)
    ctx.register_hook("pre_verify", _on_pre_verify)
    ctx.register_hook("on_session_end", _on_session_end)
