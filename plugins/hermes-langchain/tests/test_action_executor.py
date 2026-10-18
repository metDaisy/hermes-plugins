"""Contract tests for the Runtime action executor."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace


_ROOT = Path(__file__).resolve().parents[1]
from tests.support import load_module

EXECUTOR = load_module("runtime.action_executor")


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

    def test_missing_wake_session_defers_action_without_starving_queue(self) -> None:
        action = {
            **self.action("wake-profile"),
            "profile": "coder",
            "skill": "run-impl-card",
        }
        events = []
        result = EXECUTOR.execute_next_action(
            state_path=Path("state.db"),
            runtime_generation="runtime-1",
            parent_pid=99,
            plugin_root=_ROOT,
            hermes_executable="C:/Hermes/hermes.EXE",
            action_service=SimpleNamespace(
                claim_next_action=lambda **_kwargs: {"claimed": True, **action},
                defer_action_claim=lambda **kwargs: events.append(("deferred", kwargs)),
            ),
            lease_service=SimpleNamespace(
                acquire_local_agent_lease=lambda **_kwargs: {"acquired": True},
                release_local_agent_lease=lambda **_kwargs: events.append(("released", {})),
            ),
            invocation_service=SimpleNamespace(),
            profile_launcher=SimpleNamespace(),
            snapshot_service=SimpleNamespace(read_board_snapshot=lambda _board: {}),
            session_resolver=lambda _state, _profile: "",
        )

        self.assertEqual(result["status"], "waiting")
        self.assertEqual(result["reason_code"], "TARGET_SESSION_UNBOUND")
        deferred = next(value for kind, value in events if kind == "deferred")
        self.assertEqual(deferred["action_id"], action["action_id"])
        self.assertIn(("released", {}), events)

    def test_wake_action_binds_existing_session_enqueues_and_launches_guard(self) -> None:
        action = {
            **self.action("wake-profile"),
            "profile": "coder",
            "skill": "run-impl-card",
        }
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
                read_profile_wakes=lambda **_kwargs: [],
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

    def test_pm_wake_launches_a_new_fresh_agent_instead_of_resuming_pm_profile(self) -> None:
        action = self.action("wake-profile")
        events = []
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            result = EXECUTOR.execute_next_action(
                state_path=workspace / "state.db",
                runtime_generation="runtime-1",
                parent_pid=99,
                plugin_root=_ROOT,
                hermes_executable="C:/Hermes/hermes.EXE",
                fresh_agent_host_profile="main",
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
                profile_launcher=SimpleNamespace(
                    create_task_session=lambda **kwargs: events.append(("session", kwargs))
                    or {"session_id": "fresh-session-1"},
                    launch_profile_worker=lambda **kwargs: events.append(("launch", kwargs))
                    or {"pid": 4323},
                ),
                fresh_agent_contract=SimpleNamespace(
                    situation_for_reason=lambda _reason: "worker-needs-input",
                    build_contract=lambda **_kwargs: {
                        "schema": "kwc-fresh-agent-execution-v1",
                        "situation": "worker-needs-input",
                        "host_profile": "main",
                        "kanban_profile": "project-manager",
                    },
                    write_manifest=lambda **_kwargs: {
                        "path": str(workspace / ".temp/kwc-executions/act_wake_profile.json")
                    },
                ),
                snapshot_service=SimpleNamespace(
                    read_board_snapshot=lambda _board: {
                        "tasks": [
                            {"task_id": "t_impl", "workspace_path": str(workspace)}
                        ]
                    }
                ),
                session_resolver=lambda *_args: self.fail("must not resume PM session"),
            )

        self.assertEqual("launched", result["status"])
        session = next(value for kind, value in events if kind == "session")
        invocation = next(value for kind, value in events if kind == "invocation")
        launch = next(value for kind, value in events if kind == "launch")
        self.assertEqual("main", session["profile"])
        self.assertEqual("project-manager", invocation["profile"])
        self.assertEqual("fresh-session-1", invocation["session_key"])
        self.assertEqual("main", launch["host_profile"])
        self.assertEqual("project-manager", launch["profile"])
        self.assertEqual("worker-needs-input", launch["situation"])
        self.assertTrue(str(launch["fresh_manifest"]).endswith("act_wake_profile.json"))


if __name__ == "__main__":
    unittest.main()
