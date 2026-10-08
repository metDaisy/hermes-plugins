"""Contract tests for the Runtime action executor."""

from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace


_ROOT = Path(__file__).parent
_SPEC = importlib.util.spec_from_file_location(
    "kanban_workflow_controller_action_executor", _ROOT / "action_executor.py"
)
assert _SPEC and _SPEC.loader
EXECUTOR = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = EXECUTOR
_SPEC.loader.exec_module(EXECUTOR)


class ActionExecutorTests(unittest.TestCase):
    def test_default_sibling_loader_can_load_runtime_services(self) -> None:
        module = EXECUTOR._load_sibling_module(
            "_kwc_action_executor_loader_probe", "action_service.py"
        )
        self.assertTrue(callable(module.claim_next_action))

    def action(self, kind: str) -> dict:
        return {
            "schema": "kwc-workflow-action-v1",
            "action_id": f"act_{kind.replace('-', '_')}",
            "snapshot_fingerprint": "fp-before",
            "board": "issue151",
            "action_kind": kind,
            "task_id": "t_impl",
            "profile": "project-manager" if kind == "wake-profile" else "coder",
            "skill": "run-workflow" if kind == "wake-profile" else "run-impl-card",
            "reason": "worker_needs_input" if kind == "wake-profile" else "ready_implementation",
            "status": "claimed",
            "claim_generation": 2,
        }

    def test_promote_action_executes_native_adapter_and_completes_claim(self) -> None:
        action = self.action("promote-task")
        completed = []
        action_service = SimpleNamespace(
            claim_next_action=lambda **_kwargs: {"claimed": True, **action},
            complete_action=lambda **kwargs: completed.append(kwargs),
        )
        result = EXECUTOR.execute_next_action(
            state_path=Path("state.db"),
            runtime_generation="runtime-1",
            parent_pid=99,
            plugin_root=_ROOT,
            hermes_executable="C:/Hermes/hermes.EXE",
            action_service=action_service,
            mutation_service=SimpleNamespace(
                execute_promote_action=lambda **_kwargs: {"result_code": "PROMOTED"}
            ),
            snapshot_service=SimpleNamespace(read_board_snapshot=lambda _board: {}),
        )

        self.assertEqual(result["result_code"], "PROMOTED")
        self.assertEqual(completed[0]["result_code"], "PROMOTED")

    def test_dispatch_claims_core_creates_session_invocation_and_launches_worker(self) -> None:
        action = self.action("dispatch-task")
        events = []
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            result = EXECUTOR.execute_next_action(
                state_path=workspace / "state.db",
                runtime_generation="runtime-1",
                parent_pid=99,
                plugin_root=_ROOT,
                hermes_executable="C:/Hermes/hermes.EXE",
                action_service=SimpleNamespace(
                    claim_next_action=lambda **_kwargs: {"claimed": True, **action}
                ),
                lease_service=SimpleNamespace(
                    acquire_local_agent_lease=lambda **_kwargs: {"acquired": True},
                    release_local_agent_lease=lambda **_kwargs: events.append("released"),
                ),
                dispatch_service=SimpleNamespace(
                    execute_dispatch_action=lambda **_kwargs: {
                        "run_id": 17,
                        "workspace_path": str(workspace),
                    }
                ),
                invocation_service=SimpleNamespace(
                    create_invocation=lambda **kwargs: events.append(("invocation", kwargs))
                ),
                profile_launcher=SimpleNamespace(
                    create_task_session=lambda **_kwargs: {"session_id": "session-1"},
                    launch_profile_worker=lambda **kwargs: events.append(("launch", kwargs))
                    or {"pid": 4321},
                ),
                snapshot_service=SimpleNamespace(read_board_snapshot=lambda _board: {}),
            )

        self.assertEqual(result["status"], "launched")
        invocation = next(value for kind, value in events if kind == "invocation")
        launch = next(value for kind, value in events if kind == "launch")
        self.assertEqual(invocation["session_key"], "session-1")
        self.assertEqual(invocation["allowed_outcomes"], ["completed", "blocked", "failed"])
        self.assertEqual(launch["mode"], "dispatch")
        self.assertNotIn("released", events)

    def test_wake_action_binds_existing_session_enqueues_and_launches_guard(self) -> None:
        action = self.action("wake-profile")
        events = []
        result = EXECUTOR.execute_next_action(
            state_path=Path("state.db"),
            runtime_generation="runtime-1",
            parent_pid=99,
            plugin_root=_ROOT,
            hermes_executable="C:/Hermes/hermes.EXE",
            action_service=SimpleNamespace(
                claim_next_action=lambda **_kwargs: {"claimed": True, **action}
            ),
            lease_service=SimpleNamespace(
                acquire_local_agent_lease=lambda **_kwargs: {"acquired": True},
                release_local_agent_lease=lambda **_kwargs: events.append("released"),
            ),
            invocation_service=SimpleNamespace(
                create_invocation=lambda **kwargs: events.append(("invocation", kwargs))
            ),
            wake_service=SimpleNamespace(
                enqueue_profile_wake=lambda **kwargs: events.append(("wake", kwargs))
            ),
            profile_launcher=SimpleNamespace(
                launch_profile_worker=lambda **kwargs: events.append(("launch", kwargs))
                or {"pid": 4322}
            ),
            snapshot_service=SimpleNamespace(read_board_snapshot=lambda _board: {}),
            session_resolver=lambda _state, _profile: "pm-session",
        )

        invocation = next(value for kind, value in events if kind == "invocation")
        wake = next(value for kind, value in events if kind == "wake")
        launch = next(value for kind, value in events if kind == "launch")
        self.assertEqual(result["status"], "launched")
        self.assertEqual(invocation["session_key"], "pm-session")
        self.assertEqual(wake["invocation_id"], invocation["invocation_id"])
        self.assertEqual(launch["mode"], "wake")
        self.assertEqual(launch["run_id"], 0)


if __name__ == "__main__":
    unittest.main()
