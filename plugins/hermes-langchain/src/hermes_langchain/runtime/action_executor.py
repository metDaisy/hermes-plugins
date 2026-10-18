"""Runtime action executor for canonical mutations and fenced Profile launches."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any, Callable

from ..dependencies import load as _load_dependency


_ACTION_TTL_SECONDS = 30
_AGENT_TTL_SECONDS = 30
_ALLOWED_OUTCOMES = ["completed", "blocked", "failed"]


def _load_sibling_module(unique_name: str, filename: str) -> Any:
    del unique_name
    return _load_dependency(filename)


def _default_session_resolver(state_path: Path, profile: str) -> str:
    conn = sqlite3.connect(state_path, timeout=5.0)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT session_key FROM profile_session_binding WHERE profile=?", (profile,)
        ).fetchone()
        if row is not None and str(row["session_key"] or "").strip():
            return str(row["session_key"]).strip()
        row = conn.execute(
            "SELECT coordinator_session_key AS session_key FROM coordinator_binding WHERE singleton=1"
        ).fetchone()
        return str(row["session_key"] or "").strip() if row is not None else ""
    finally:
        conn.close()


def _module(value: Any, unique_name: str, filename: str) -> Any:
    return value if value is not None else _load_sibling_module(unique_name, filename)


def execute_next_action(
    *,
    state_path: Path,
    runtime_generation: str,
    parent_pid: int,
    plugin_root: Path,
    hermes_executable: str,
    fresh_agent_host_profile: str | None = None,
    kanban_profile: str = "project-manager",
    action_service: Any = None,
    lease_service: Any = None,
    mutation_service: Any = None,
    dispatch_service: Any = None,
    invocation_service: Any = None,
    wake_service: Any = None,
    profile_launcher: Any = None,
    snapshot_service: Any = None,
    fresh_agent_contract: Any = None,
    session_resolver: Callable[[Path, str], str] = _default_session_resolver,
) -> dict[str, Any]:
    """Claim and execute at most one durable action without blocking Runtime."""
    action_service = _module(
        action_service, "_kwc_executor_action_service", "action_service.py"
    )
    snapshot_service = _module(
        snapshot_service, "_kwc_executor_snapshot_service", "snapshot_service.py"
    )
    if fresh_agent_contract is None:
        from . import fresh_agent_contract as fresh_agent_contract_module

        fresh_agent_contract = fresh_agent_contract_module
    fresh_agent_host_profile = str(
        fresh_agent_host_profile
        or os.getenv("HERMES_KWC_FRESH_HOST_PROFILE")
        or os.getenv("HERMES_PROFILE")
        or "main"
    ).strip()
    if not fresh_agent_host_profile:
        raise ValueError("fresh_agent_host_profile is required")
    claimant_id = f"runtime:{runtime_generation}"
    action = action_service.claim_next_action(
        state_path=state_path,
        claimant_id=claimant_id,
        ttl_seconds=_ACTION_TTL_SECONDS,
    )
    if not bool(action.get("claimed")):
        return {
            "ok": True,
            "status": "idle",
            "reason_code": str(action.get("reason_code") or "ACTION_QUEUE_EMPTY"),
        }

    action_id = str(action["action_id"])
    action_kind = str(action["action_kind"])
    claim_generation = int(action["claim_generation"])
    if action_kind == "promote-task":
        mutation_service = _module(
            mutation_service, "_kwc_executor_mutation_service", "mutation_service.py"
        )
        mutation = mutation_service.execute_promote_action(
            action=action,
            snapshot_reader=snapshot_service.read_board_snapshot,
        )
        result_code = str(mutation["result_code"])
        action_service.complete_action(
            state_path=state_path,
            action_id=action_id,
            claimant_id=claimant_id,
            claim_generation=claim_generation,
            result_code=result_code,
        )
        return {"ok": True, "status": "completed", "result_code": result_code}

    if action_kind not in {"dispatch-task", "wake-profile"}:
        raise ValueError("unsupported actionable kind")

    lease_service = _module(
        lease_service, "_kwc_executor_lease_service", "agent_lease_service.py"
    )
    invocation_service = _module(
        invocation_service,
        "_kwc_executor_invocation_service",
        "invocation_service.py",
    )
    profile_launcher = _module(
        profile_launcher, "_kwc_executor_profile_launcher", "profile_launcher.py"
    )
    lease_id = f"lease:{action_id}:{claim_generation}"
    holder_id = f"action:{action_id}"
    lease = lease_service.acquire_local_agent_lease(
        state_path=state_path,
        lease_id=lease_id,
        holder_id=holder_id,
        ttl_seconds=_AGENT_TTL_SECONDS,
    )
    if not bool(lease.get("acquired")):
        return {
            "ok": True,
            "status": "waiting",
            "reason_code": str(lease.get("reason_code") or "LEASE_HELD"),
        }

    launched = False
    try:
        board = str(action["board"])
        task_id = str(action["task_id"])
        profile = str(action["profile"])
        skill = str(action["skill"])
        use_fresh_agent = profile == str(kanban_profile)
        workflow_instance_id = f"workflow:{board}"
        wake_service_module = None
        if action_kind == "dispatch-task":
            invocation_id = f"inv:{action_id}:{claim_generation}"
            dispatch_service = _module(
                dispatch_service,
                "_kwc_executor_dispatch_service",
                "dispatch_service.py",
            )
            dispatch = dispatch_service.execute_dispatch_action(
                action=action,
                snapshot_reader=snapshot_service.read_board_snapshot,
            )
            workspace_path = Path(str(dispatch.get("workspace_path") or ""))
            if not workspace_path.is_dir():
                raise ValueError("canonical task workspace is unavailable")
            session = profile_launcher.create_task_session(
                profile=fresh_agent_host_profile if use_fresh_agent else profile,
                task_id=task_id,
                workspace_path=workspace_path,
            )
            session_key = str(session["session_id"])
            run_id = int(dispatch["run_id"])
            mode = "dispatch"
        elif not use_fresh_agent:
            wake_service_module = _module(
                wake_service, "_kwc_executor_wake_service", "wake_service.py"
            )
            existing_wakes = [
                item
                for item in wake_service_module.read_profile_wakes(
                    state_path=state_path, board=board
                )
                if str(item.get("action_id") or "") == action_id
            ]
            if len(existing_wakes) > 1:
                raise ValueError("action_id is bound to multiple wake contracts")
            invocation_id = (
                str(existing_wakes[0]["invocation_id"])
                if existing_wakes
                else f"inv:{action_id}:{claim_generation}"
            )
            session_key = str(session_resolver(state_path, profile) or "").strip()
            if not session_key:
                action_service.defer_action_claim(
                    state_path=state_path,
                    action_id=action_id,
                    claimant_id=claimant_id,
                    claim_generation=claim_generation,
                )
                return {
                    "ok": True,
                    "status": "waiting",
                    "reason_code": "TARGET_SESSION_UNBOUND",
                }
            workspace_path = Path(plugin_root).resolve()
            run_id = 0
            mode = "wake"
        else:
            invocation_id = f"inv:{action_id}:{claim_generation}"
            snapshot = snapshot_service.read_board_snapshot(board)
            task = next(
                (
                    item
                    for item in snapshot.get("tasks", [])
                    if isinstance(item, dict) and item.get("task_id") == task_id
                ),
                None,
            )
            workspace_path = Path(str((task or {}).get("workspace_path") or "")).resolve()
            if not workspace_path.is_dir():
                raise ValueError("canonical task workspace is unavailable")
            session = profile_launcher.create_task_session(
                profile=fresh_agent_host_profile,
                task_id=task_id,
                workspace_path=workspace_path,
            )
            session_key = str(session["session_id"])
            run_id = 0
            mode = "wake"

        fresh_manifest = None
        situation = None
        if use_fresh_agent:
            situation = fresh_agent_contract.situation_for_reason(action.get("reason"))
            contract = fresh_agent_contract.build_contract(
                situation=situation,
                host_profile=fresh_agent_host_profile,
                kanban_profile=profile,
            )
            manifest = fresh_agent_contract.write_manifest(
                workspace=workspace_path,
                execution_id=action_id,
                contract=contract,
                prompt=(
                    "Read the canonical KWC task, run, comments, events, repository state, and the "
                    "injected KWC-owned Skill. Execute only the situation-specific contract. Submit "
                    "exactly one typed kwc_pm_response before the canonical Kanban handoff mutation."
                ),
                context={
                    "action_id": action_id,
                    "action_kind": action_kind,
                    "reason": action.get("reason"),
                    "board": board,
                    "task_id": task_id,
                    "run_id": run_id,
                    "profile": profile,
                    "skill": skill,
                    "invocation_id": invocation_id,
                    "snapshot_fingerprint": action.get("snapshot_fingerprint"),
                },
            )
            fresh_manifest = Path(manifest["path"])

        invocation_service.create_invocation(
            state_path=state_path,
            invocation_id=invocation_id,
            workflow_instance_id=workflow_instance_id,
            board=board,
            profile=profile,
            session_key=session_key,
            expected_response="kwc-profile-response-v1",
            allowed_outcomes=_ALLOWED_OUTCOMES,
            snapshot_fingerprint=str(action["snapshot_fingerprint"]),
        )
        if action_kind == "wake-profile" and not use_fresh_agent:
            wake_service_module.enqueue_profile_wake(
                state_path=state_path,
                action_id=action_id,
                board=board,
                task_id=task_id,
                profile=profile,
                skill=skill,
                invocation_id=invocation_id,
            )

        worker = profile_launcher.launch_profile_worker(
            plugin_root=plugin_root,
            state_path=state_path,
            action_id=action_id,
            action_claimant_id=claimant_id,
            action_claim_generation=claim_generation,
            lease_id=lease_id,
            invocation_id=invocation_id,
            session_id=session_key,
            profile=profile,
            skill=skill,
            board=board,
            task_id=task_id,
            workspace_path=workspace_path,
            run_id=run_id,
            parent_pid=parent_pid,
            mode=mode,
            hermes_executable=hermes_executable,
            host_profile=fresh_agent_host_profile if use_fresh_agent else None,
            situation=situation,
            fresh_manifest=fresh_manifest,
        )
        launched = True
        return {
            "ok": True,
            "status": "launched",
            "action_id": action_id,
            "invocation_id": invocation_id,
            "profile": profile,
            "execution_mode": "fresh-agent" if use_fresh_agent else "profile",
            "manifest_path": str(fresh_manifest) if fresh_manifest else None,
            "pid": int(worker["pid"]),
        }
    finally:
        if not launched:
            try:
                lease_service.release_local_agent_lease(
                    state_path=state_path,
                    lease_id=lease_id,
                    holder_id=holder_id,
                )
            except (OSError, ValueError):
                pass
