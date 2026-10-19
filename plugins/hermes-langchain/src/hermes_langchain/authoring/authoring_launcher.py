"""Launch one fresh Project Manager turn for a KWC authoring assignment."""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

from ..paths import PLUGIN_ROOT
from ..runtime import fresh_agent_contract

_PARENT_SESSION_ENV_KEYS = (
    "HERMES_SESSION_PLATFORM",
    "HERMES_SESSION_SOURCE",
    "HERMES_SESSION_CHAT_ID",
    "HERMES_SESSION_CHAT_TYPE",
    "HERMES_SESSION_CHAT_NAME",
    "HERMES_SESSION_THREAD_ID",
    "HERMES_SESSION_USER_ID",
    "HERMES_SESSION_USER_ID_ALT",
    "HERMES_SESSION_USER_NAME",
    "HERMES_SESSION_SCOPE_ID",
    "HERMES_SESSION_KEY",
    "HERMES_SESSION_ID",
    "HERMES_UI_SESSION_ID",
    "HERMES_SESSION_MESSAGE_ID",
    "HERMES_SESSION_PROFILE",
    "HERMES_BROWSER_CONTROL_PRINCIPAL",
    "HERMES_BROWSER_CONTROL_TRANSPORT_FAMILY",
    "HERMES_CRON_SESSION",
    "HERMES_SESSION_PARENT_CHAT_ID",
)


def launch_assignment(
    *,
    assignment: dict[str, Any],
    workspace: Path,
    state_path: Path,
    profile: str,
    host_profile: str | None = None,
    hermes_executable: str,
    popen_fn=subprocess.Popen,
) -> dict[str, Any]:
    workspace = Path(workspace).resolve()
    state_path = Path(state_path).resolve()
    if not workspace.is_dir():
        raise ValueError("KWC_WORKSPACE_REQUIRED")
    if assignment.get("schema") != "kwc-assignment-v1":
        raise ValueError("KWC_ASSIGNMENT_SCHEMA")
    profile = str(profile or "").strip()
    if not profile:
        raise ValueError("KWC_COORDINATOR_PROFILE_REQUIRED")
    host_profile = str(
        host_profile
        or os.getenv("HERMES_KWC_FRESH_HOST_PROFILE")
        or os.getenv("HERMES_PROFILE")
        or "main"
    ).strip()
    if not host_profile:
        raise ValueError("KWC_FRESH_HOST_PROFILE_REQUIRED")
    executable = str(hermes_executable or "").strip()
    if not executable:
        raise ValueError("KWC_HERMES_EXECUTABLE_REQUIRED")
    allowed_tools = assignment.get("allowed_tools")
    next_action = str(assignment.get("next_action") or "").strip()
    if (
        not isinstance(allowed_tools, list)
        or not next_action
        or next_action not in allowed_tools
    ):
        raise ValueError("KWC_LANGGRAPH_ROUTE_MISMATCH")
    board = str(assignment.get("board") or "").strip()
    generation = assignment.get("generation")
    if not board or not isinstance(generation, int) or generation < 1:
        raise ValueError("KWC_ASSIGNMENT_IDENTITY")
    board_slug = re.sub(r"[^A-Za-z0-9._-]+", "-", board).strip("-")
    phase = str(assignment.get("phase") or "").strip()
    session_name = f"kwc-{phase}-{board_slug}-g{generation}"
    phase_guidance = ""
    if phase == "review":
        phase_guidance = (
            "Review authoring requires the installed Skill's prepare-review-fast-path before writing. "
            "Run it from $HERMES_KWC_PLUGIN_ROOT/skills/kwc-build-task-graph/scripts/"
            "build_task_graph.py using assignment.sources and write its body output to the exact "
            "assignment.artifact_file. Read review_body_template and guide_locators from the prepared "
            "bundle, preserve every immutable/template field, and populate only acceptance, review_checks, "
            "and scope_exclusions. The only review check focus values are: "
            "behavior | implementation-convention | cross-card-seam | test-quality | preservation. "
            "Use exact guide_locators from the bundle plus one non-empty .hermes/docs/plan.md#... active "
            "handoff locator. Never add issue/card_type/generation/review_card_key or any other top-level "
            "field. Finally pass the exact assignment.artifact_file to kwc_create_review.\n\n"
        )
    prompt = (
        "KWC fresh-agent authoring assignment follows. Treat it as the complete current-stage contract. "
        "Use normal read/search/write tools for evidence and artifact authoring, but execute the exact "
        "assignment.next_action value and use only the current assignment's allowed_tools for KWC state mutation. "
        "At intake, read the named Issue directly, then only its governing requirement locators, "
        "docs/current-state.md, and Git HEAD. Write each stage artifact directly to the assignment's artifact_file "
        "and pass that exact repo-relative value to the KWC tool; do not choose another path or use Profile cache "
        "as an intermediate. "
        "Do not scan historical boards, .temp artifacts, or .hermes design notes. In later stages, inspect only "
        "the accepted stage projection and its exact target locators before expanding a search. "
        "KWC owns board identity, validation, native mutation, exact read-back, and transition. "
        "Do not activate or dispatch cards. "
        + phase_guidance
        + (
            "After each successful KWC tool call, read next_assignment from the result and immediately continue "
            "when its phase is triage. Do not wait for another user message between Triage stages. When "
            "next_assignment.phase가 triage가 아니면 the frozen Triage has been materialized: stop without authoring "
            "Impl, Review, or Summary.\n\n"
            if phase == "triage"
            else "Author exactly this one non-Triage phase with the assigned situation Skill, execute next_action "
            "once, verify the returned next_assignment, and then stop.\n\n"
        )
        + json.dumps(assignment, ensure_ascii=False, sort_keys=True)
    )
    situation = fresh_agent_contract.situation_for_authoring(assignment.get("phase"))
    contract = fresh_agent_contract.build_contract(
        situation=situation,
        host_profile=host_profile,
        kanban_profile=profile,
    )
    manifest = fresh_agent_contract.write_manifest(
        workspace=workspace,
        execution_id=assignment.get("assignment_id"),
        contract=contract,
        prompt=prompt,
        context={
            "assignment_id": assignment.get("assignment_id"),
            "board": board,
            "issue_number": assignment.get("issue_number"),
            "generation": generation,
            "phase": assignment.get("phase"),
            "triage_stage": assignment.get("triage_stage"),
            "state_path": str(state_path),
        },
    )
    command = [
        executable,
        "--profile",
        host_profile,
        "chat",
        "--oneshot",
        "--quiet",
        "--ignore-rules",
        "--toolsets",
        ",".join(fresh_agent_contract.runtime_toolsets(contract)),
        "--skills",
        ",".join(contract["skills"]),
        "--source",
        "tool",
        "--pass-session-id",
        "--max-turns",
        "300",
        "--no-restore-cwd",
        "--in",
        str(workspace),
        "-q",
        prompt,
    ]
    env = os.environ.copy()
    for key in _PARENT_SESSION_ENV_KEYS:
        env.pop(key, None)
    env["HERMES_LANGCHAIN_STATE_PATH"] = str(state_path)
    env["HERMES_PROFILE_NAME"] = host_profile
    env["HERMES_PROFILE"] = host_profile
    env["HERMES_SESSION_PROFILE"] = host_profile
    env["HERMES_KWC_EXECUTION_ROLE"] = profile
    env["HERMES_KWC_KANBAN_PROFILE"] = profile
    env["HERMES_KWC_FRESH_HOST_PROFILE"] = host_profile
    env["HERMES_KWC_FRESH_MANIFEST"] = str(Path(manifest["path"]).resolve())
    env["HERMES_KWC_PLUGIN_ROOT"] = str(PLUGIN_ROOT)
    kwargs: dict[str, Any] = {
        "cwd": str(workspace),
        "env": env,
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
        "shell": False,
    }
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    process = popen_fn(command, **kwargs)
    return {
        "ok": True,
        "pid": int(process.pid),
        "profile": host_profile,
        "execution_role": profile,
        "session_name": session_name,
        "manifest_path": manifest["path"],
        "manifest_sha256": manifest["sha256"],
        "assignment_id": assignment.get("assignment_id"),
        "board": board,
        "phase": assignment.get("phase"),
        "triage_stage": assignment.get("triage_stage"),
    }
