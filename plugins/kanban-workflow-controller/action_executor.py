"""Runtime action executor for canonical mutations and fenced Profile launches."""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path
from typing import Any, Callable


_ACTION_TTL_SECONDS = 30
_AGENT_TTL_SECONDS = 30
_ALLOWED_OUTCOMES = ["completed", "blocked", "failed"]


def _load_sibling_module(unique_name: str, filename: str) -> Any:
    path = Path(__file__).resolve().with_name(filename)
    module = sys.modules.get(unique_name)
    if module is not None:
        if Path(module.__file__).resolve() != path:
            raise ImportError(f"{unique_name} is bound to another path")
        return module
    spec = importlib.util.spec_from_file_location(unique_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load KWC executor dependency from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[unique_name] = module
    spec.loader.exec_module(module)
    return module


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
    action_service: Any = None,
    lease_service: Any = None,
    mutation_service: Any = None,
    dispatch_service: Any = None,
    invocation_service: Any = None,
    wake_service: Any = None,
    profile_launcher: Any = None,
    snapshot_service: Any = None,
    session_resolver: Callable[[Path, str], str] = _default_session_resolver,
) -> dict[str, Any]:
    """Claim and execute at most one durable action without blocking Runtime."""
    action_service = _module(
        action_service, "_kwc_executor_action_service", "action_service.py"
    )
    snapshot_service = _module(
        snapshot_service, "_kwc_executor_snapshot_service", "snapshot_service.py"
    )
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
        invocation_id = f"inv:{action_id}:{claim_generation}"
        workflow_instance_id = f"workflow:{board}"
        if action_kind == "dispatch-task":
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
                profile=profile,
                task_id=task_id,
                workspace_path=workspace_path,
            )
            session_key = str(session["session_id"])
            run_id = int(dispatch["run_id"])
            mode = "dispatch"
        else:
            session_key = str(session_resolver(state_path, profile) or "").strip()
            if not session_key:
                raise ValueError("target Profile session is not bound")
            workspace_path = Path(plugin_root).resolve()
            run_id = 0
            mode = "wake"

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
        if action_kind == "wake-profile":
            wake_service = _module(
                wake_service, "_kwc_executor_wake_service", "wake_service.py"
            )
            wake_service.enqueue_profile_wake(
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
        )
        launched = True
        return {
            "ok": True,
            "status": "launched",
            "action_id": action_id,
            "invocation_id": invocation_id,
            "profile": profile,
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
