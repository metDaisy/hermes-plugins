"""Detached KWC Profile worker with fenced lease and action heartbeats."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any


_HEARTBEAT_TTL_SECONDS = 30
_DEFAULT_TIMEOUT_SECONDS = 1800


def _load_sibling_module(unique_name: str, filename: str) -> Any:
    path = Path(__file__).resolve().with_name(filename)
    module = sys.modules.get(unique_name)
    if module is not None:
        if Path(module.__file__).resolve() != path:
            raise ImportError(f"{unique_name} is bound to another path")
        return module
    spec = importlib.util.spec_from_file_location(unique_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load KWC worker dependency from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[unique_name] = module
    spec.loader.exec_module(module)
    return module


def build_hermes_command(
    *,
    state_path: Path,
    action_id: str,
    action_claimant_id: str,
    action_claim_generation: int,
    lease_id: str,
    invocation_id: str,
    session_id: str,
    profile: str,
    skill: str,
    board: str,
    task_id: str,
    workspace_path: Path,
    run_id: int,
    parent_pid: int,
    hermes_executable: str,
) -> list[str]:
    del state_path, action_claimant_id, action_claim_generation, lease_id, parent_pid
    prompt = json.dumps(
        {
            "schema": "kwc-profile-execution-v1",
            "action_id": action_id,
            "board": board,
            "source_task_id": task_id,
            "run_id": int(run_id),
            "target_profile": profile,
            "skill": skill,
            "invocation_id": invocation_id,
            "instruction": (
                "Freshly read the canonical task, run, comments, events, Git state, and required "
                "Skill. Perform only this task-scoped contract. Before the terminal handoff, submit "
                "exactly one typed result with kwc_profile_response or kwc_pm_response as appropriate. "
                "After that typed response is accepted, use the native terminal Kanban mutation for "
                "the canonical handoff; terminal Kanban tools end the turn, so never reverse this order."
            ),
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return [
        str(hermes_executable),
        "-p",
        profile,
        "chat",
        "-Q",
        "--resume",
        session_id,
        "--in",
        str(Path(workspace_path).resolve()),
        "--format",
        "stream-json",
        "--source",
        "tool",
        "--skills",
        skill,
        "-q",
        prompt,
    ]


def _default_canonical_scope_reader(
    *, board: str, task_id: str, run_id: int
) -> dict[str, str]:
    from hermes_cli import kanban_db, kanban_db_connect

    conn = kanban_db_connect.connect(board=board)
    try:
        task = kanban_db.get_task(conn, task_id)
        current_run_id = getattr(task, "current_run_id", None) if task else None
        claim_lock = str(getattr(task, "claim_lock", "") or "") if task else ""
        if current_run_id != run_id or not claim_lock:
            raise ValueError("canonical task claim scope is unavailable")
        return {
            "claim_lock": claim_lock,
            "db_path": str(kanban_db.kanban_db_path(board=board)),
            "workspaces_root": str(kanban_db.workspaces_root(board=board)),
        }
    finally:
        conn.close()


def build_hermes_environment(
    *,
    state_path: Path,
    session_id: str,
    profile: str,
    board: str,
    task_id: str,
    run_id: int,
    workspace_path: Path,
    profile_home: Path | None = None,
    canonical_scope_reader: Callable[..., dict[str, str]] = _default_canonical_scope_reader,
) -> dict[str, str]:
    env = os.environ.copy()
    env.pop("HERMES_SESSION_KEY", None)
    env.pop("HERMES_DELEGATED_CHILD_CONTEXT", None)
    if profile_home is None:
        profile_home = _load_sibling_module(
            "_kwc_profile_worker_profile_launcher", "profile_launcher.py"
        ).resolve_profile_home(profile)
    scope = canonical_scope_reader(board=board, task_id=task_id, run_id=int(run_id))
    claim_lock = str(scope.get("claim_lock") or "").strip()
    db_path = str(scope.get("db_path") or "").strip()
    workspaces_root = str(scope.get("workspaces_root") or "").strip()
    if not claim_lock or not db_path or not workspaces_root:
        raise ValueError("canonical task claim scope is incomplete")
    workspace_path = Path(workspace_path).resolve()
    env["HERMES_SESSION_ID"] = str(session_id)
    env["HERMES_SESSION_PROFILE"] = str(profile)
    env["HERMES_SESSION_SOURCE"] = "kanban"
    env["HERMES_PROFILE"] = str(profile)
    env["HERMES_HOME"] = str(Path(profile_home))
    env["HERMES_KWC_STATE_PATH"] = str(Path(state_path))
    env["HERMES_KANBAN_BOARD"] = str(board)
    env["HERMES_KANBAN_TASK"] = str(task_id)
    env["HERMES_KANBAN_RUN_ID"] = str(int(run_id))
    env["HERMES_KANBAN_CLAIM_LOCK"] = claim_lock
    env["HERMES_KANBAN_DB"] = db_path
    env["HERMES_KANBAN_WORKSPACES_ROOT"] = workspaces_root
    env["HERMES_KANBAN_WORKSPACE"] = str(workspace_path)
    env["TERMINAL_CWD"] = str(workspace_path)
    return env


def _default_action_reader(*, state_path: Path, action_id: str) -> dict[str, Any] | None:
    actions = _load_sibling_module(
        "_kwc_profile_worker_action_service", "action_service.py"
    ).read_actions(state_path=state_path)
    return next((item for item in actions if item.get("action_id") == action_id), None)


def _default_canonical_heartbeat(
    *, board: str, task_id: str, action_id: str, ttl_seconds: int
) -> bool:
    from hermes_cli import kanban_db, kanban_db_connect

    conn = kanban_db_connect.connect(board=board)
    try:
        return bool(
            kanban_db.heartbeat_claim(
                conn,
                task_id,
                ttl_seconds=ttl_seconds,
                claimer=f"kwc:{action_id}",
            )
        )
    finally:
        conn.close()


def _canonical_run_task(snapshot: Any, *, task_id: str, run_id: int) -> dict[str, Any] | None:
    if not isinstance(snapshot, dict):
        return None
    tasks = snapshot.get("tasks")
    if not isinstance(tasks, list):
        return None
    task = next(
        (
            item
            for item in tasks
            if isinstance(item, dict) and item.get("task_id") == task_id
        ),
        None,
    )
    if task is None or task.get("latest_run_id") != int(run_id):
        return None
    return task


def _canonical_terminal_handoff(snapshot: Any, *, task_id: str, run_id: int) -> bool:
    task = _canonical_run_task(snapshot, task_id=task_id, run_id=run_id)
    if task is None:
        return False
    return (
        str(task.get("status") or ""),
        str(task.get("latest_run_status") or ""),
        str(task.get("latest_run_outcome") or ""),
    ) in {
        ("done", "done", "completed"),
        ("review", "review", "review_requested"),
        ("blocked", "blocked", "blocked"),
        ("blocked", "blocked", "dependency_wait"),
        ("blocked", "blocked", "failed"),
    }


def _canonical_handoff_complete(
    snapshot: Any, *, task_id: str, run_id: int, outcome: str
) -> bool:
    task = _canonical_run_task(snapshot, task_id=task_id, run_id=run_id)
    if task is None:
        return False
    status = str(task.get("status") or "")
    run_status = str(task.get("latest_run_status") or "")
    run_outcome = str(task.get("latest_run_outcome") or "")
    normalized_outcome = str(outcome or "").lower()
    if normalized_outcome == "completed":
        return (status, run_status, run_outcome) in {
            ("done", "done", "completed"),
            ("review", "review", "review_requested"),
        }
    if normalized_outcome in {"blocked", "failed"}:
        return (
            status == "blocked"
            and run_status == "blocked"
            and run_outcome in {"blocked", "dependency_wait", "failed"}
        )
    return False


def _terminate(
    process: Any,
    *,
    os_name: str = os.name,
    run_fn: Callable[..., Any] = subprocess.run,
) -> None:
    if process.poll() is not None:
        return
    if os_name == "nt":
        try:
            result = run_fn(
                ["taskkill", "/PID", str(int(process.pid)), "/T", "/F"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
                check=False,
                shell=False,
            )
            if result is None or int(getattr(result, "returncode", 0)) == 0:
                return
        except Exception:
            pass
    try:
        process.terminate()
        process.wait(timeout=5)
    except Exception:
        pass


def run_profile_worker(
    *,
    state_path: Path,
    action_id: str,
    action_claimant_id: str,
    action_claim_generation: int,
    lease_id: str,
    invocation_id: str,
    session_id: str,
    profile: str,
    skill: str,
    board: str,
    task_id: str,
    workspace_path: Path,
    run_id: int,
    parent_pid: int,
    hermes_executable: str,
    mode: str = "dispatch",
    action_reader: Callable[..., dict[str, Any] | None] | None = None,
    invocation_reader: Callable[..., dict[str, Any] | None] | None = None,
    snapshot_reader: Callable[[str], dict[str, Any]] | None = None,
    fingerprint_fn: Callable[[Any], str] | None = None,
    action_heartbeat: Callable[..., Any] | None = None,
    lease_heartbeat: Callable[..., Any] | None = None,
    canonical_heartbeat: Callable[..., bool] | None = None,
    action_complete: Callable[..., Any] | None = None,
    lease_release: Callable[..., Any] | None = None,
    pid_alive_fn: Callable[[int], bool] | None = None,
    popen_fn: Callable[..., Any] = subprocess.Popen,
    environment_builder: Callable[..., dict[str, str]] = build_hermes_environment,
    now_fn: Callable[[], int] = lambda: int(time.time()),
    sleep_fn: Callable[[float], None] = time.sleep,
    poll_interval_seconds: float = 2.0,
    timeout_seconds: int = _DEFAULT_TIMEOUT_SECONDS,
    max_process_attempts: int = 2,
) -> dict[str, Any]:
    action_module = _load_sibling_module(
        "_kwc_profile_worker_action_service", "action_service.py"
    )
    lease_module = _load_sibling_module(
        "_kwc_profile_worker_lease_service", "agent_lease_service.py"
    )
    invocation_module = _load_sibling_module(
        "_kwc_profile_worker_invocation_service", "invocation_service.py"
    )
    snapshot_module = _load_sibling_module(
        "_kwc_profile_worker_snapshot_service", "snapshot_service.py"
    )
    runtime_module = _load_sibling_module(
        "_kwc_profile_worker_runtime_service", "runtime_service.py"
    )
    action_reader = action_reader or _default_action_reader
    invocation_reader = invocation_reader or invocation_module.read_invocation
    snapshot_reader = snapshot_reader or snapshot_module.read_board_snapshot
    fingerprint_fn = fingerprint_fn or action_module.fingerprint_snapshot
    action_heartbeat = action_heartbeat or action_module.heartbeat_action_claim
    lease_heartbeat = lease_heartbeat or lease_module.heartbeat_local_agent_lease
    canonical_heartbeat = canonical_heartbeat or _default_canonical_heartbeat
    action_complete = action_complete or action_module.complete_action
    lease_release = lease_release or lease_module.release_local_agent_lease
    pid_alive_fn = pid_alive_fn or runtime_module._pid_alive
    holder_id = f"action:{action_id}"

    if not pid_alive_fn(int(parent_pid)):
        return {"ok": False, "result_code": "RUNTIME_PARENT_EXITED"}
    action = action_reader(state_path=state_path, action_id=action_id)
    if not action:
        return {"ok": False, "result_code": "ACTION_NOT_FOUND"}
    initial_fingerprint = str(action.get("snapshot_fingerprint") or "")
    if not initial_fingerprint:
        return {"ok": False, "result_code": "ACTION_FINGERPRINT_MISSING"}

    if mode not in {"dispatch", "wake"}:
        raise ValueError("mode must be dispatch or wake")
    command = None
    process = None
    process_attempts = 0
    if mode == "dispatch":
        if isinstance(max_process_attempts, bool) or int(max_process_attempts) <= 0:
            raise ValueError("max_process_attempts must be a positive integer")
        max_process_attempts = int(max_process_attempts)
        command = build_hermes_command(
            state_path=state_path,
            action_id=action_id,
            action_claimant_id=action_claimant_id,
            action_claim_generation=action_claim_generation,
            lease_id=lease_id,
            invocation_id=invocation_id,
            session_id=session_id,
            profile=profile,
            skill=skill,
            board=board,
            task_id=task_id,
            workspace_path=workspace_path,
            run_id=run_id,
            parent_pid=parent_pid,
            hermes_executable=hermes_executable,
        )
        process_environment = environment_builder(
            state_path=state_path,
            session_id=session_id,
            profile=profile,
            board=board,
            task_id=task_id,
            run_id=run_id,
            workspace_path=workspace_path,
        )

        def _launch() -> Any:
            return popen_fn(
                command,
                cwd=str(Path(workspace_path).resolve()),
                env=process_environment.copy(),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                shell=False,
            )

        process = _launch()
        process_attempts = 1
    started_at = int(now_fn())
    released = False
    try:
        while True:
            now = int(now_fn())
            if not pid_alive_fn(int(parent_pid)):
                if process is not None:
                    _terminate(process)
                return {"ok": False, "result_code": "RUNTIME_PARENT_EXITED"}
            if now - started_at >= int(timeout_seconds):
                if process is not None:
                    _terminate(process)
                return {"ok": False, "result_code": "PROFILE_TIMEOUT"}
            canonical_claim_alive = True
            try:
                action_heartbeat(
                    state_path=state_path,
                    action_id=action_id,
                    claimant_id=action_claimant_id,
                    claim_generation=action_claim_generation,
                    ttl_seconds=_HEARTBEAT_TTL_SECONDS,
                )
                lease_heartbeat(
                    state_path=state_path,
                    lease_id=lease_id,
                    holder_id=holder_id,
                    ttl_seconds=_HEARTBEAT_TTL_SECONDS,
                )
                if mode == "dispatch":
                    canonical_claim_alive = canonical_heartbeat(
                        board=board,
                        task_id=task_id,
                        action_id=action_id,
                        ttl_seconds=_HEARTBEAT_TTL_SECONDS,
                    )
            except (OSError, ValueError):
                if process is not None:
                    _terminate(process)
                return {"ok": False, "result_code": "EXECUTION_FENCE_LOST"}

            invocation = invocation_reader(
                state_path=state_path, invocation_id=invocation_id
            )
            response_accepted = bool(
                invocation and invocation.get("status") == "accepted"
            )
            snapshot = (
                snapshot_reader(board)
                if response_accepted or (mode == "dispatch" and not canonical_claim_alive)
                else None
            )
            if response_accepted:
                outcome = str(invocation.get("outcome") or "completed")
                handoff_complete = (
                    _canonical_handoff_complete(
                        snapshot,
                        task_id=task_id,
                        run_id=run_id,
                        outcome=outcome,
                    )
                    if mode == "dispatch"
                    else fingerprint_fn(snapshot) != initial_fingerprint
                )
                if handoff_complete:
                    result_code = f"PROFILE_RESPONSE:{outcome.upper()}"
                    action_complete(
                        state_path=state_path,
                        action_id=action_id,
                        claimant_id=action_claimant_id,
                        claim_generation=action_claim_generation,
                        result_code=result_code,
                    )
                    if process is not None:
                        _terminate(process)
                    lease_release(
                        state_path=state_path,
                        lease_id=lease_id,
                        holder_id=holder_id,
                    )
                    released = True
                    return {"ok": True, "result_code": result_code}
            if mode == "dispatch" and not canonical_claim_alive:
                if _canonical_terminal_handoff(
                    snapshot, task_id=task_id, run_id=run_id
                ):
                    if process is not None and process.poll() is None:
                        sleep_fn(poll_interval_seconds)
                        continue
                    if process_attempts < max_process_attempts:
                        process = _launch()
                        process_attempts += 1
                        sleep_fn(poll_interval_seconds)
                        continue
                    return {"ok": False, "result_code": "PROFILE_RESPONSE_MISSING"}
                if process is not None:
                    _terminate(process)
                return {"ok": False, "result_code": "CANONICAL_CLAIM_LOST"}
            if process is not None and process.poll() is not None:
                if process_attempts < max_process_attempts:
                    process = _launch()
                    process_attempts += 1
                    sleep_fn(poll_interval_seconds)
                    continue
                return {"ok": False, "result_code": "PROFILE_RESPONSE_MISSING"}
            sleep_fn(poll_interval_seconds)
    finally:
        if not released:
            try:
                lease_release(
                    state_path=state_path,
                    lease_id=lease_id,
                    holder_id=holder_id,
                )
            except (OSError, ValueError):
                pass


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="KWC detached Profile worker")
    parser.add_argument("--state-db", required=True, type=Path)
    parser.add_argument("--action-id", required=True)
    parser.add_argument("--action-claimant-id", required=True)
    parser.add_argument("--action-claim-generation", required=True, type=int)
    parser.add_argument("--lease-id", required=True)
    parser.add_argument("--invocation-id", required=True)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--skill", required=True)
    parser.add_argument("--board", required=True)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--run-id", required=True, type=int)
    parser.add_argument("--parent-pid", required=True, type=int)
    parser.add_argument("--hermes-executable", required=True)
    parser.add_argument("--mode", choices=("dispatch", "wake"), default="dispatch")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    result = run_profile_worker(
        state_path=args.state_db,
        action_id=args.action_id,
        action_claimant_id=args.action_claimant_id,
        action_claim_generation=args.action_claim_generation,
        lease_id=args.lease_id,
        invocation_id=args.invocation_id,
        session_id=args.session_id,
        profile=args.profile,
        skill=args.skill,
        board=args.board,
        task_id=args.task_id,
        workspace_path=args.workspace,
        run_id=args.run_id,
        parent_pid=args.parent_pid,
        hermes_executable=args.hermes_executable,
        mode=args.mode,
    )
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
