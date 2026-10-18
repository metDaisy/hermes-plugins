"""Contract tests for the detached KWC Profile worker."""

from __future__ import annotations

import os
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


_ROOT = Path(__file__).resolve().parents[1]
from tests.support import load_module

WORKER = load_module("runtime.profile_worker")


class FakeProcess:
    def __init__(self, return_code=None, pid=1234) -> None:
        self.return_code = return_code
        self.pid = pid
        self.terminated = False

    def poll(self):
        return self.return_code

    def terminate(self):
        self.terminated = True
        self.return_code = -15

    def wait(self, timeout=None):
        return self.return_code


class ProfileWorkerTests(unittest.TestCase):
    def args(self, workspace: Path) -> dict:
        return {
            "state_path": workspace / "state.db",
            "action_id": "act_dispatch_1",
            "action_claimant_id": "runtime-1",
            "action_claim_generation": 3,
            "lease_id": "lease-1",
            "invocation_id": "inv-1",
            "session_id": "20261008_120000_abcd1234",
            "profile": "coder",
            "skill": "run-impl-card",
            "board": "issue151",
            "task_id": "t_impl",
            "workspace_path": workspace,
            "run_id": 17,
            "parent_pid": 99,
            "hermes_executable": "C:/Hermes/hermes.EXE",
        }

    def test_command_uses_resumable_profile_session_and_compact_wakeup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            command = WORKER.build_hermes_command(**self.args(Path(tmp)))

        self.assertEqual(command[0], "C:/Hermes/hermes.EXE")
        self.assertEqual(command[3], "chat")
        self.assertIn("coder", command)
        self.assertIn("20261008_120000_abcd1234", command)
        self.assertIn("run-impl-card", command)
        prompt = command[command.index("-q") + 1]
        self.assertNotIn("-z", command)
        self.assertIn('"invocation_id":"inv-1"', prompt)
        self.assertLess(
            prompt.index("submit exactly one typed result"),
            prompt.index("use the native terminal Kanban mutation"),
        )
        self.assertNotIn("task_body", prompt)
        self.assertNotIn("reasoning", prompt)

    def test_fresh_agent_command_uses_host_profile_and_explicit_contract_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            manifest_path = workspace / "manifest.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "schema": "kwc-fresh-agent-manifest-v1",
                        "execution_id": "act_checkpoint",
                        "contract": {
                            "schema": "kwc-fresh-agent-execution-v1",
                            "situation": "implementation-checkpoint",
                            "actor": "kwc-fresh-agent",
                            "behavior_source": "kanban-workflow-controller",
                            "host_profile": "main",
                            "kanban_profile": "project-manager",
                            "ignore_rules": True,
                            "load_soul": False,
                            "load_memory": False,
                            "skills": ["hermes-langchain:kwc-run-workflow"],
                            "toolsets": ["file", "terminal", "hermes-langchain"],
                            "mcp_servers": [
                                "codebase-memory-mcp",
                                "github-mcp",
                                "semble-mcp",
                            ],
                            "allowed_tools": ["kwc_pm_response"],
                            "output_schema": "kwc-profile-response-v1",
                        },
                        "workspace_path": str(workspace),
                        "prompt": "perform the checkpoint",
                        "prompt_sha256": "0" * 64,
                        "context": {"invocation_id": "inv-1"},
                    }
                ),
                encoding="utf-8",
            )
            command = WORKER.build_hermes_command(
                **self.args(workspace), fresh_manifest=manifest_path
            )

        self.assertEqual(command[:3], ["C:/Hermes/hermes.EXE", "-p", "main"])
        self.assertIn("--ignore-rules", command)
        self.assertEqual(
            "hermes-langchain:kwc-run-workflow",
            command[command.index("--skills") + 1],
        )
        self.assertEqual(
            "file,terminal,hermes-langchain,codebase-memory-mcp,github-mcp,semble-mcp",
            command[command.index("--toolsets") + 1],
        )
        prompt = command[command.index("-q") + 1]
        self.assertIn(str(manifest_path.resolve()), prompt)
        self.assertIn("kwc-profile-response-v1", prompt)
        self.assertNotIn('"target_profile":"project-manager"', prompt)

    def test_fresh_agent_environment_separates_host_profile_from_execution_role(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            manifest_path = workspace / "manifest.json"
            manifest_path.write_text("{}", encoding="utf-8")
            env = WORKER.build_hermes_environment(
                state_path=workspace / "state.db",
                session_id="20261008_120000_abcd1234",
                profile="project-manager",
                board="issue151",
                task_id="t_impl",
                run_id=17,
                workspace_path=workspace,
                profile_home=workspace / "profiles" / "main",
                host_profile="main",
                fresh_manifest=manifest_path,
                canonical_scope_reader=lambda **_kwargs: {
                    "claim_lock": "claim-lock-17",
                    "db_path": "C:/Hermes/kanban/issue151.db",
                    "workspaces_root": "C:/Hermes/kanban/workspaces/issue151",
                },
            )

        self.assertEqual("main", env["HERMES_PROFILE"])
        self.assertEqual("main", env["HERMES_SESSION_PROFILE"])
        self.assertEqual("project-manager", env["HERMES_KWC_EXECUTION_ROLE"])
        self.assertEqual("main", env["HERMES_KWC_FRESH_HOST_PROFILE"])
        self.assertEqual(str(WORKER.fresh_agent_contract.PLUGIN_ROOT), env["HERMES_KWC_PLUGIN_ROOT"])
        self.assertEqual(str(manifest_path.resolve()), env["HERMES_KWC_FRESH_MANIFEST"])

    def test_worker_environment_rebinds_profile_session_without_parent_gateway_key(self) -> None:
        with patch.dict(os.environ, {"HERMES_SESSION_KEY": "parent-session"}, clear=False):
            env = WORKER.build_hermes_environment(
                state_path=Path("C:/kwc/state.db"),
                session_id="20261008_120000_abcd1234",
                profile="coder",
                board="issue151",
                task_id="t_impl",
                run_id=17,
                workspace_path=Path("C:/workspace/issue151"),
                profile_home=Path("C:/Hermes/profiles/coder"),
                canonical_scope_reader=lambda **_kwargs: {
                    "claim_lock": "claim-lock-17",
                    "db_path": "C:/Hermes/kanban/issue151.db",
                    "workspaces_root": "C:/Hermes/kanban/workspaces/issue151",
                },
            )

        self.assertNotIn("HERMES_SESSION_KEY", env)
        self.assertEqual(env["HERMES_SESSION_ID"], "20261008_120000_abcd1234")
        self.assertEqual(env["HERMES_SESSION_PROFILE"], "coder")
        self.assertEqual(env["HERMES_PROFILE"], "coder")
        self.assertEqual(env["HERMES_LANGCHAIN_STATE_PATH"], str(Path("C:/kwc/state.db")))
        self.assertEqual(env["HERMES_HOME"], str(Path("C:/Hermes/profiles/coder")))
        self.assertEqual(env["HERMES_KANBAN_BOARD"], "issue151")
        self.assertEqual(env["HERMES_KANBAN_TASK"], "t_impl")
        self.assertEqual(env["HERMES_KANBAN_RUN_ID"], "17")
        self.assertEqual(env["HERMES_KANBAN_CLAIM_LOCK"], "claim-lock-17")
        self.assertEqual(env["HERMES_KANBAN_DB"], "C:/Hermes/kanban/issue151.db")
        self.assertEqual(
            env["HERMES_KANBAN_WORKSPACES_ROOT"],
            "C:/Hermes/kanban/workspaces/issue151",
        )
        self.assertEqual(env["HERMES_KANBAN_WORKSPACE"], str(Path("C:/workspace/issue151")))
        self.assertEqual(env["TERMINAL_CWD"], str(Path("C:/workspace/issue151")))
        self.assertEqual(env["HERMES_SESSION_SOURCE"], "kanban")

    def test_accepted_response_and_canonical_task_handoff_complete_the_fenced_action(self) -> None:
        events = []
        process = FakeProcess()
        with tempfile.TemporaryDirectory() as tmp:
            result = WORKER.run_profile_worker(
                **self.args(Path(tmp)),
                action_reader=lambda **_kwargs: {"snapshot_fingerprint": "before"},
                invocation_reader=lambda **_kwargs: {
                    "status": "accepted",
                    "outcome": "completed",
                },
                snapshot_reader=lambda _board: {
                    "tasks": [
                        {
                            "task_id": "t_impl",
                            "status": "done",
                            "latest_run_id": 17,
                            "latest_run_status": "done",
                            "latest_run_outcome": "completed",
                        }
                    ]
                },
                fingerprint_fn=lambda _snapshot: "after",
                action_heartbeat=lambda **_kwargs: events.append("action-heartbeat"),
                lease_heartbeat=lambda **_kwargs: events.append("lease-heartbeat"),
                canonical_heartbeat=lambda **_kwargs: True,
                action_complete=lambda **kwargs: events.append(
                    ("completed", kwargs["result_code"])
                ),
                lease_release=lambda **_kwargs: events.append("released"),
                pid_alive_fn=lambda _pid: True,
                environment_builder=lambda **_kwargs: {},
                popen_fn=lambda *_args, **kwargs: events.append(kwargs) or process,
                now_fn=lambda: 1000,
                sleep_fn=lambda _seconds: None,
            )

        self.assertEqual(result["result_code"], "PROFILE_RESPONSE:COMPLETED")
        self.assertIn("action-heartbeat", events)
        self.assertIn("lease-heartbeat", events)
        self.assertIn(("completed", "PROFILE_RESPONSE:COMPLETED"), events)
        self.assertIn("released", events)
        self.assertTrue(process.terminated)
        popen_kwargs = next(item for item in events if isinstance(item, dict))
        self.assertFalse(popen_kwargs["shell"])
        self.assertEqual(popen_kwargs["stdout"], subprocess.DEVNULL)

    def test_terminal_handoff_is_observed_before_the_released_claim_is_rejected(self) -> None:
        completions = []
        process = FakeProcess()
        with tempfile.TemporaryDirectory() as tmp:
            result = WORKER.run_profile_worker(
                **self.args(Path(tmp)),
                action_reader=lambda **_kwargs: {"snapshot_fingerprint": "before"},
                invocation_reader=lambda **_kwargs: {
                    "status": "accepted",
                    "outcome": "completed",
                },
                snapshot_reader=lambda _board: {
                    "tasks": [
                        {
                            "task_id": "t_impl",
                            "status": "done",
                            "latest_run_id": 17,
                            "latest_run_status": "done",
                            "latest_run_outcome": "completed",
                        }
                    ]
                },
                fingerprint_fn=lambda _snapshot: "after",
                action_heartbeat=lambda **_kwargs: None,
                lease_heartbeat=lambda **_kwargs: None,
                canonical_heartbeat=lambda **_kwargs: False,
                action_complete=lambda **kwargs: completions.append(kwargs),
                lease_release=lambda **_kwargs: None,
                pid_alive_fn=lambda _pid: True,
                environment_builder=lambda **_kwargs: {},
                popen_fn=lambda *_args, **_kwargs: process,
                now_fn=lambda: 1000,
                sleep_fn=lambda _seconds: None,
            )

        self.assertEqual(result["result_code"], "PROFILE_RESPONSE:COMPLETED")
        self.assertEqual(len(completions), 1)

    def test_accepted_response_does_not_complete_while_task_is_still_running(self) -> None:
        completions = []
        process = FakeProcess(return_code=0)
        with tempfile.TemporaryDirectory() as tmp:
            result = WORKER.run_profile_worker(
                **self.args(Path(tmp)),
                action_reader=lambda **_kwargs: {"snapshot_fingerprint": "before"},
                invocation_reader=lambda **_kwargs: {
                    "status": "accepted",
                    "outcome": "completed",
                },
                snapshot_reader=lambda _board: {
                    "tasks": [
                        {
                            "task_id": "t_impl",
                            "status": "running",
                            "latest_run_id": 17,
                            "latest_run_status": "running",
                        }
                    ]
                },
                fingerprint_fn=lambda _snapshot: "after",
                action_heartbeat=lambda **_kwargs: None,
                lease_heartbeat=lambda **_kwargs: None,
                canonical_heartbeat=lambda **_kwargs: True,
                action_complete=lambda **kwargs: completions.append(kwargs),
                lease_release=lambda **_kwargs: None,
                pid_alive_fn=lambda _pid: True,
                environment_builder=lambda **_kwargs: {},
                popen_fn=lambda *_args, **_kwargs: process,
                now_fn=lambda: 1000,
                sleep_fn=lambda _seconds: None,
                max_process_attempts=1,
            )

        self.assertEqual(result["result_code"], "PROFILE_RESPONSE_MISSING")
        self.assertEqual(completions, [])

    def test_windows_termination_kills_the_entire_profile_process_tree(self) -> None:
        process = FakeProcess(pid=4321)
        calls = []

        WORKER._terminate(
            process,
            os_name="nt",
            run_fn=lambda command, **kwargs: calls.append((command, kwargs)),
        )

        self.assertEqual(calls[0][0], ["taskkill", "/PID", "4321", "/T", "/F"])
        self.assertEqual(calls[0][1]["stdin"], subprocess.DEVNULL)
        self.assertEqual(calls[0][1]["stdout"], subprocess.DEVNULL)
        self.assertEqual(calls[0][1]["stderr"], subprocess.DEVNULL)
        self.assertFalse(process.terminated)

    def test_worker_retries_a_failed_profile_process_before_giving_up(self) -> None:
        processes = [FakeProcess(return_code=2), FakeProcess()]
        launches = []
        responses = iter(
            [
                {"status": "pending"},
                {"status": "accepted", "outcome": "completed"},
            ]
        )
        with tempfile.TemporaryDirectory() as tmp:
            result = WORKER.run_profile_worker(
                **self.args(Path(tmp)),
                action_reader=lambda **_kwargs: {"snapshot_fingerprint": "before"},
                invocation_reader=lambda **_kwargs: next(responses),
                snapshot_reader=lambda _board: {
                    "tasks": [
                        {
                            "task_id": "t_impl",
                            "status": "done",
                            "latest_run_id": 17,
                            "latest_run_status": "done",
                            "latest_run_outcome": "completed",
                        }
                    ]
                },
                fingerprint_fn=lambda _snapshot: "after",
                action_heartbeat=lambda **_kwargs: None,
                lease_heartbeat=lambda **_kwargs: None,
                canonical_heartbeat=lambda **_kwargs: True,
                action_complete=lambda **_kwargs: None,
                lease_release=lambda **_kwargs: None,
                pid_alive_fn=lambda _pid: True,
                environment_builder=lambda **_kwargs: {},
                popen_fn=lambda *_args, **_kwargs: launches.append(True) or processes.pop(0),
                now_fn=lambda: 1000,
                sleep_fn=lambda _seconds: None,
                max_process_attempts=2,
            )

        self.assertEqual(result["result_code"], "PROFILE_RESPONSE:COMPLETED")
        self.assertEqual(len(launches), 2)

    def test_retry_reuses_the_initial_canonical_scope_environment(self) -> None:
        processes = [FakeProcess(return_code=2), FakeProcess()]
        responses = iter(
            [
                {"status": "pending"},
                {"status": "accepted", "outcome": "completed"},
            ]
        )
        environment_calls = []

        def build_environment(**_kwargs):
            environment_calls.append(True)
            if len(environment_calls) > 1:
                raise ValueError("canonical claim was already released")
            return {"KWC_SCOPE": "captured"}

        with tempfile.TemporaryDirectory() as tmp:
            result = WORKER.run_profile_worker(
                **self.args(Path(tmp)),
                action_reader=lambda **_kwargs: {"snapshot_fingerprint": "before"},
                invocation_reader=lambda **_kwargs: next(responses),
                snapshot_reader=lambda _board: {
                    "tasks": [
                        {
                            "task_id": "t_impl",
                            "status": "done",
                            "latest_run_id": 17,
                            "latest_run_status": "done",
                            "latest_run_outcome": "completed",
                        }
                    ]
                },
                fingerprint_fn=lambda _snapshot: "after",
                action_heartbeat=lambda **_kwargs: None,
                lease_heartbeat=lambda **_kwargs: None,
                canonical_heartbeat=lambda **_kwargs: True,
                action_complete=lambda **_kwargs: None,
                lease_release=lambda **_kwargs: None,
                pid_alive_fn=lambda _pid: True,
                environment_builder=build_environment,
                popen_fn=lambda *_args, **_kwargs: processes.pop(0),
                now_fn=lambda: 1000,
                sleep_fn=lambda _seconds: None,
                max_process_attempts=2,
            )

        self.assertEqual(result["result_code"], "PROFILE_RESPONSE:COMPLETED")
        self.assertEqual(len(environment_calls), 1)

    def test_terminal_handoff_without_response_retries_the_same_session(self) -> None:
        processes = [FakeProcess(return_code=0), FakeProcess()]
        responses = iter(
            [
                {"status": "pending"},
                {"status": "accepted", "outcome": "blocked"},
            ]
        )
        launches = []
        completions = []
        with tempfile.TemporaryDirectory() as tmp:
            result = WORKER.run_profile_worker(
                **self.args(Path(tmp)),
                action_reader=lambda **_kwargs: {"snapshot_fingerprint": "before"},
                invocation_reader=lambda **_kwargs: next(responses),
                snapshot_reader=lambda _board: {
                    "tasks": [
                        {
                            "task_id": "t_impl",
                            "status": "blocked",
                            "latest_run_id": 17,
                            "latest_run_status": "blocked",
                            "latest_run_outcome": "blocked",
                        }
                    ]
                },
                fingerprint_fn=lambda _snapshot: "after",
                action_heartbeat=lambda **_kwargs: None,
                lease_heartbeat=lambda **_kwargs: None,
                canonical_heartbeat=lambda **_kwargs: False,
                action_complete=lambda **kwargs: completions.append(kwargs),
                lease_release=lambda **_kwargs: None,
                pid_alive_fn=lambda _pid: True,
                environment_builder=lambda **_kwargs: {},
                popen_fn=lambda *_args, **_kwargs: launches.append(True) or processes.pop(0),
                now_fn=lambda: 1000,
                sleep_fn=lambda _seconds: None,
                max_process_attempts=2,
            )

        self.assertEqual(result["result_code"], "PROFILE_RESPONSE:BLOCKED")
        self.assertEqual(len(launches), 2)
        self.assertEqual(len(completions), 1)

    def test_wake_guard_waits_without_launching_a_second_hermes_process(self) -> None:
        events = []
        with tempfile.TemporaryDirectory() as tmp:
            args = self.args(Path(tmp))
            args["mode"] = "wake"
            args["run_id"] = 0
            result = WORKER.run_profile_worker(
                **args,
                action_reader=lambda **_kwargs: {"snapshot_fingerprint": "before"},
                invocation_reader=lambda **_kwargs: {
                    "status": "accepted",
                    "outcome": "completed",
                },
                snapshot_reader=lambda _board: {"changed": True},
                fingerprint_fn=lambda _snapshot: "after",
                action_heartbeat=lambda **_kwargs: None,
                lease_heartbeat=lambda **_kwargs: None,
                canonical_heartbeat=lambda **_kwargs: self.fail("wake has no task claim"),
                action_complete=lambda **_kwargs: events.append("completed"),
                lease_release=lambda **_kwargs: events.append("released"),
                pid_alive_fn=lambda _pid: True,
                popen_fn=lambda *_args, **_kwargs: self.fail("wake must use gateway injection"),
                now_fn=lambda: 1000,
                sleep_fn=lambda _seconds: None,
            )

        self.assertEqual(result["result_code"], "PROFILE_RESPONSE:COMPLETED")
        self.assertEqual(events, ["completed", "released"])

    def test_worker_does_not_launch_when_desktop_runtime_parent_is_dead(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = WORKER.run_profile_worker(
                **self.args(Path(tmp)),
                action_reader=lambda **_kwargs: {"snapshot_fingerprint": "before"},
                invocation_reader=lambda **_kwargs: None,
                snapshot_reader=lambda _board: {},
                fingerprint_fn=lambda _snapshot: "before",
                action_heartbeat=lambda **_kwargs: None,
                lease_heartbeat=lambda **_kwargs: None,
                canonical_heartbeat=lambda **_kwargs: True,
                action_complete=lambda **_kwargs: self.fail("must not complete"),
                lease_release=lambda **_kwargs: None,
                pid_alive_fn=lambda _pid: False,
                popen_fn=lambda *_args, **_kwargs: self.fail("must not launch"),
                now_fn=lambda: 1000,
                sleep_fn=lambda _seconds: None,
            )

        self.assertEqual(result["result_code"], "RUNTIME_PARENT_EXITED")


if __name__ == "__main__":
    unittest.main()
