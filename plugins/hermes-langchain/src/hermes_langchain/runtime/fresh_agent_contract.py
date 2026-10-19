"""Profile-independent fresh-agent execution contracts owned by KWC."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from ..paths import PLUGIN_ROOT

_SCHEMA = "kwc-fresh-agent-execution-v1"
_STABLE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
_SENSITIVE_KEY = re.compile(
    r"(?i)(?:password|passwd|token|secret|api[_-]?key|authorization|connection[_-]?string)"
)
_INLINE_SECRET = re.compile(
    r"(?i)\b(password|passwd|token|secret|api[_-]?key|authorization|connection[_-]?string)"
    r"\s*[:=]\s*[^;\r\n]+"
)
_AUTHORING_TOOLSETS = ("file", "terminal", "hermes-langchain")
_COORDINATOR_TOOLSETS = ("file", "terminal", "hermes-langchain")
_VERIFICATION_MCP_SERVERS = (
    "codebase-memory-mcp",
    "github-mcp",
    "semble-mcp",
    "gradle-mcp",
)
_ORCHESTRATOR_SKILL = "hermes-langchain:kwc-orchestrator-contract"

# A situation is KWC behavior, not a Hermes Profile.  host_profile only supplies model,
# credentials, plugin registration, and provider settings; --ignore-rules prevents its
# SOUL, project rules, auto-loaded skills, and memory from becoming behavior inputs.
_SITUATIONS: dict[str, dict[str, Any]] = {
    "triage-authoring": {
        "skill": "hermes-langchain:kwc-triage-authoring-runtime",
        "toolsets": _AUTHORING_TOOLSETS,
        "mcp_servers": ("codebase-memory-mcp", "github-mcp", "semble-mcp"),
        "allowed_tools": ("kwc_advance_triage", "kwc_create_triage"),
        "output_schema": "kwc-assignment-v1",
    },
    "impl-authoring": {
        "skill": "hermes-langchain:kwc-impl-authoring-runtime",
        "toolsets": _AUTHORING_TOOLSETS,
        "mcp_servers": ("codebase-memory-mcp", "semble-mcp"),
        "allowed_tools": ("kwc_create_impl_graph", "kwc_recover_frozen_triage"),
        "output_schema": "kwc-assignment-v1",
    },
    "review-authoring": {
        "skill": "hermes-langchain:kwc-review-authoring-runtime",
        "toolsets": _AUTHORING_TOOLSETS,
        "mcp_servers": (),
        "allowed_tools": ("kwc_create_review",),
        "output_schema": "kwc-assignment-v1",
    },
    "summary-authoring": {
        "skill": "hermes-langchain:kwc-regenerate-summary",
        "toolsets": ("file", "terminal", "hermes-langchain"),
        "mcp_servers": (),
        "allowed_tools": ("kwc_create_summary",),
        "output_schema": "kwc-assignment-v1",
    },
    "implementation-checkpoint": {
        "skill": "hermes-langchain:kwc-run-workflow",
        "toolsets": _COORDINATOR_TOOLSETS,
        "mcp_servers": _VERIFICATION_MCP_SERVERS,
        "allowed_tools": ("kwc_pm_response", "kwc_comment", "kwc_reply"),
        "output_schema": "kwc-profile-response-v1",
    },
    "worker-needs-input": {
        "skill": "hermes-langchain:kwc-run-workflow",
        "toolsets": _COORDINATOR_TOOLSETS,
        "mcp_servers": ("codebase-memory-mcp", "github-mcp", "semble-mcp"),
        "allowed_tools": ("kwc_pm_response", "kwc_comment", "kwc_reply"),
        "output_schema": "kwc-profile-response-v1",
    },
    "route-review-findings": {
        "skill": "hermes-langchain:kwc-run-workflow",
        "toolsets": _COORDINATOR_TOOLSETS,
        "mcp_servers": ("codebase-memory-mcp", "github-mcp", "semble-mcp"),
        "allowed_tools": ("kwc_pm_response", "kwc_comment", "kwc_reply"),
        "output_schema": "kwc-profile-response-v1",
    },
    "summary-finalization": {
        "skill": "hermes-langchain:kwc-run-workflow",
        "toolsets": _COORDINATOR_TOOLSETS,
        "mcp_servers": _VERIFICATION_MCP_SERVERS,
        "allowed_tools": ("kwc_pm_response", "kwc_comment", "kwc_reply"),
        "output_schema": "kwc-profile-response-v1",
    },
    "service-planning": {
        "skill": "hermes-langchain:kwc-service-planning",
        "toolsets": (*_COORDINATOR_TOOLSETS, "web", "browser"),
        "mcp_servers": ("github-mcp", "semble-mcp"),
        "allowed_tools": ("kwc_pm_response", "kwc_comment", "kwc_reply"),
        "output_schema": "kwc-profile-response-v1",
    },
    "sync-docs": {
        "skill": "hermes-langchain:kwc-sync-docs",
        "toolsets": _COORDINATOR_TOOLSETS,
        "mcp_servers": ("codebase-memory-mcp", "github-mcp", "semble-mcp"),
        "allowed_tools": ("kwc_pm_response", "kwc_comment", "kwc_reply"),
        "output_schema": "kwc-profile-response-v1",
    },
    "update-current-state": {
        "skill": "hermes-langchain:kwc-update-current-state",
        "toolsets": _COORDINATOR_TOOLSETS,
        "mcp_servers": ("codebase-memory-mcp", "semble-mcp"),
        "allowed_tools": ("kwc_pm_response", "kwc_comment", "kwc_reply"),
        "output_schema": "kwc-profile-response-v1",
    },
    "regenerate-summary": {
        "skill": "hermes-langchain:kwc-regenerate-summary",
        "toolsets": ("file", "terminal", "hermes-langchain"),
        "mcp_servers": (),
        "allowed_tools": ("kwc_pm_response", "kwc_comment", "kwc_reply"),
        "output_schema": "kwc-profile-response-v1",
    },
}

_REASON_TO_SITUATION = {
    "implementation_checkpoint": "implementation-checkpoint",
    "active_implementation_checkpoint": "implementation-checkpoint",
    "worker_needs_input": "worker-needs-input",
    "route_review_findings": "route-review-findings",
    "summary_finalization": "summary-finalization",
}


def _required_stable(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not text or not _STABLE.fullmatch(text):
        raise ValueError(f"{field} must be a compact stable identifier")
    return text


def _redact(value: Any, *, key: str | None = None) -> Any:
    if key is not None and _SENSITIVE_KEY.fullmatch(str(key)):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {str(item_key): _redact(item_value, key=str(item_key)) for item_key, item_value in value.items()}
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, tuple):
        return [_redact(item) for item in value]
    if isinstance(value, str):
        return _INLINE_SECRET.sub(lambda match: f"{match.group(1)}=[REDACTED]", value)
    return value


def situation_for_authoring(phase: Any) -> str:
    phase = _required_stable(phase, "phase")
    mapping = {
        "triage": "triage-authoring",
        "impl": "impl-authoring",
        "review": "review-authoring",
        "summary": "summary-authoring",
    }
    try:
        return mapping[phase]
    except KeyError as exc:
        raise ValueError("phase has no fresh-agent contract") from exc


def situation_for_reason(reason: Any) -> str:
    reason = _required_stable(reason, "reason")
    try:
        return _REASON_TO_SITUATION[reason]
    except KeyError as exc:
        raise ValueError("reason has no fresh-agent contract") from exc


def build_contract(*, situation: Any, host_profile: Any, kanban_profile: Any) -> dict[str, Any]:
    situation = _required_stable(situation, "situation")
    host_profile = _required_stable(host_profile, "host_profile")
    kanban_profile = _required_stable(kanban_profile, "kanban_profile")
    try:
        template = _SITUATIONS[situation]
    except KeyError as exc:
        raise ValueError("unknown fresh-agent situation") from exc
    return {
        "schema": _SCHEMA,
        "situation": situation,
        "actor": "kwc-fresh-agent",
        "behavior_source": "kanban-workflow-controller",
        "host_profile": host_profile,
        "kanban_profile": kanban_profile,
        "ignore_rules": True,
        "load_soul": False,
        "load_memory": False,
        "skills": [_ORCHESTRATOR_SKILL, template["skill"]],
        "toolsets": list(template["toolsets"]),
        "mcp_servers": list(template["mcp_servers"]),
        "allowed_tools": list(template["allowed_tools"]),
        "output_schema": template["output_schema"],
    }


def runtime_toolsets(contract: dict[str, Any]) -> list[str]:
    """Return the exact native and MCP toolset allowlist for one fresh run."""
    if contract.get("schema") != _SCHEMA:
        raise ValueError("fresh-agent contract schema is invalid")
    combined: list[str] = []
    for field in ("toolsets", "mcp_servers"):
        values = contract.get(field)
        if not isinstance(values, list):
            raise ValueError(f"fresh-agent contract {field} must be a list")
        for value in values:
            name = _required_stable(value, field)
            if name not in combined:
                combined.append(name)
    if not combined:
        raise ValueError("fresh-agent runtime toolsets are required")
    return combined


def manifest_path(*, workspace: Path, execution_id: Any) -> Path:
    execution_id = _required_stable(execution_id, "execution_id")
    filename = re.sub(r"[^A-Za-z0-9._-]", "_", execution_id)
    return Path(workspace).resolve() / ".temp" / "kwc-executions" / f"{filename}.json"


def write_manifest(
    *,
    workspace: Path,
    execution_id: Any,
    contract: dict[str, Any],
    prompt: str,
    context: dict[str, Any],
) -> dict[str, Any]:
    workspace = Path(workspace).resolve()
    if not workspace.is_dir():
        raise ValueError("workspace must be an existing directory")
    execution_id = _required_stable(execution_id, "execution_id")
    if contract.get("schema") != _SCHEMA:
        raise ValueError("fresh-agent contract schema is invalid")
    prompt = str(_redact(str(prompt or "").strip()))
    if not prompt:
        raise ValueError("prompt is required")
    if not isinstance(context, dict):
        raise ValueError("context must be an object")
    payload = {
        "schema": "kwc-fresh-agent-manifest-v1",
        "execution_id": execution_id,
        "contract": contract,
        "workspace_path": str(workspace),
        "prompt": prompt,
        "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        "context": _redact(context),
    }
    path = manifest_path(workspace=workspace, execution_id=execution_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "payload": payload}


def write_comparison_artifact(
    *,
    output_path: Path,
    prompt: str,
    expected: str,
    fresh_result: dict[str, Any],
    pm_result: dict[str, Any],
) -> dict[str, Any]:
    prompt = str(_redact(str(prompt or "").strip()))
    expected = str(expected or "").strip()
    if not prompt or not expected:
        raise ValueError("comparison prompt and expected result are required")
    prompt_sha256 = hashlib.sha256(prompt.encode("utf-8")).hexdigest()

    def normalize(label: str, value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise ValueError(f"{label} result must be an object")
        if value.get("prompt_sha256") != prompt_sha256:
            raise ValueError(f"{label} result prompt does not match comparison prompt")
        normalized = _redact(value)
        for field in ("profile", "session_id", "model", "text"):
            if not str(normalized.get(field) or "").strip():
                raise ValueError(f"{label} result requires {field}")
        duration_ms = normalized.get("duration_ms")
        if not isinstance(duration_ms, int) or duration_ms < 0:
            raise ValueError(f"{label} result requires non-negative duration_ms")
        if not isinstance(normalized.get("tokens"), dict):
            raise ValueError(f"{label} result requires tokens")
        return normalized

    fresh = normalize("kwc_fresh", fresh_result)
    pm = normalize("pm_profile", pm_result)
    fresh_exact = str(fresh["text"]).strip() == expected
    pm_exact = str(pm["text"]).strip() == expected
    if fresh_exact and not pm_exact:
        winner = "kwc_fresh"
    elif pm_exact and not fresh_exact:
        winner = "pm_profile"
    else:
        winner = "tie"
    payload = {
        "schema": "kwc-pm-comparison-v1",
        "prompt": prompt,
        "prompt_sha256": prompt_sha256,
        "expected": expected,
        "runs": {"kwc_fresh": fresh, "pm_profile": pm},
        "verdicts": {
            "same_prompt": True,
            "same_model": fresh["model"] == pm["model"],
            "kwc_fresh_exact": fresh_exact,
            "pm_profile_exact": pm_exact,
            "quality_winner": winner,
        },
    }
    output_path = Path(output_path).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return {
        "path": str(output_path),
        "sha256": hashlib.sha256(output_path.read_bytes()).hexdigest(),
        "payload": payload,
    }


def plugin_skill_paths() -> dict[str, Path]:
    return {
        name: PLUGIN_ROOT / "skills" / name / "SKILL.md"
        for name in (
            "kwc-orchestrator-contract",
            "kwc-create-triage",
            "kwc-review-authoring-runtime",
            "kwc-build-task-graph",
            "kwc-run-workflow",
            "kwc-service-planning",
            "kwc-sync-docs",
            "kwc-update-current-state",
            "kwc-regenerate-summary",
        )
    }
