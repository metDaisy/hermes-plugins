"""Acceptance tests for the public Hermes subagent lifecycle adapter."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tests.support import controller_worker, load_module

CONTROLLER = load_module("controller")


class FakeLifecycle:
    def __init__(
        self,
        *,
        structured_payload: dict[str, object] | None = None,
        summary: str | None = None,
        reject_native_schema: bool = False,
        terminal_state: str = "SUCCEEDED",
        fail_wait_once: bool = False,
        fail_result: bool = False,
        reconnect_connected: bool = True,
    ) -> None:
        self.structured_payload = structured_payload
        self.summary = summary
        self.reject_native_schema = reject_native_schema
        self.terminal_state = terminal_state
        self.fail_wait_once = fail_wait_once
        self.fail_result = fail_result
        self.reconnect_connected = reconnect_connected
        self.launch_attempts: list[dict[str, object]] = []
        self.accepted_launches: list[dict[str, object]] = []
        self.reconnect_requests: list[dict[str, object] | None] = []
        self.handle = SimpleNamespace(subagent_id="sa-hermes-1")

    def launch(self, request: dict[str, object]) -> object:
        copied = dict(request)
        self.launch_attempts.append(copied)
        if self.reject_native_schema and "output_schema" in copied:
            raise ValueError("Unknown request fields: output_schema.")
        self.accepted_launches.append(copied)
        return self.handle

    def wait(self, handle: object, *, timeout_seconds: float | None = None) -> object:
        del timeout_seconds
        self._assert_handle(handle)
        if self.fail_wait_once:
            self.fail_wait_once = False
            raise RuntimeError("injected controller interruption after launch")
        return SimpleNamespace(
            state=self.terminal_state,
            completed=True,
            timed_out=False,
            diagnostic=None,
        )

    def result(self, handle: object) -> object:
        self._assert_handle(handle)
        if self.fail_result:
            raise RuntimeError("injected lifecycle result retrieval failure")
        return SimpleNamespace(
            handle=handle,
            terminal_state=self.terminal_state,
            ready=True,
            summary=self.summary,
            structured_payload=self.structured_payload,
            error_classification=None if self.terminal_state == "SUCCEEDED" else "FAILED",
            error_message=None if self.terminal_state == "SUCCEEDED" else "child failed",
        )

    def reconnect(
        self,
        handle: object,
        request: dict[str, object] | None = None,
    ) -> object:
        del handle
        self.reconnect_requests.append(dict(request) if request is not None else None)
        return SimpleNamespace(
            connected=self.reconnect_connected,
            state="RUNNING" if self.reconnect_connected else "UNKNOWN",
            diagnostic=None if self.reconnect_connected else "RECONNECT_UNAVAILABLE",
        )

    def _assert_handle(self, handle: object) -> None:
        subagent_id = (
            handle.get("subagent_id")
            if isinstance(handle, dict)
            else getattr(handle, "subagent_id", None)
        )
        if subagent_id != self.handle.subagent_id:
            raise AssertionError("unexpected lifecycle handle")


def invocation(*, allowed_toolsets: tuple[str, ...] = ("file",)) -> object:
    return CONTROLLER.StepInvocation(
        run_id="run-hermes",
        invocation_id="run-hermes:extract:1",
        step_id="extract",
        goal="Extract one accepted requirement.",
        allowed_toolsets=allowed_toolsets,
        acceptance_criteria=("Return exactly one requirement string.",),
        output_schema={
            "type": "object",
            "properties": {"requirement": {"type": "string"}},
            "required": ["requirement"],
            "additionalProperties": False,
        },
        input={"request": "Build it"},
    )


class HermesSubagentExecutorTests(unittest.TestCase):
    @staticmethod
    def _durable_invocation(directory: str) -> object:
        original = invocation()
        return CONTROLLER.StepInvocation(
            **{
                **original.__dict__,
                "input": {"artifact_directory": Path(directory).resolve().as_posix()},
            }
        )

    @staticmethod
    def _durable_executor(
        lifecycle: object,
        directory: str,
        *,
        defer_release: bool = False,
    ) -> object:
        return CONTROLLER.HermesSubagentExecutor(
            lifecycle,
            durable_root=Path(directory).resolve() / "controller-private-lifecycle",
            defer_release=defer_release,
        )

    @staticmethod
    def _private_invocation_directory(directory: str, invocation_id: str) -> Path:
        import hashlib

        return (
            Path(directory).resolve()
            / "controller-private-lifecycle"
            / hashlib.sha256(invocation_id.encode()).hexdigest()
        )

    def test_durable_handle_reconnects_without_relaunch_after_executor_restart(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            durable = self._durable_invocation(temp)
            first = FakeLifecycle(
                structured_payload={"requirement": "accepted"},
                fail_wait_once=True,
            )
            with self.assertRaisesRegex(RuntimeError, "interruption after launch"):
                self._durable_executor(first, temp).invoke(durable)
            (Path(temp) / "lifecycle-handle.json").write_text(
                '{"forged":"agent-visible"}',
                encoding="utf-8",
            )
            (Path(temp) / "lifecycle-launch.lock").write_text(
                "forged",
                encoding="utf-8",
            )
            CONTROLLER.HermesSubagentExecutor._handle_cache.clear()
            resumed_lifecycle = FakeLifecycle(
                structured_payload={"requirement": "accepted"},
            )

            result = self._durable_executor(resumed_lifecycle, temp).invoke(durable)

            self.assertEqual({"requirement": "accepted"}, result.output)
            self.assertEqual([], resumed_lifecycle.accepted_launches)
            self.assertEqual(1, len(resumed_lifecycle.reconnect_requests))
            self.assertEqual(
                durable.invocation_id,
                resumed_lifecycle.reconnect_requests[0]["correlation_id"],
            )
            private = self._private_invocation_directory(temp, durable.invocation_id)
            self.assertTrue((private / "lifecycle-handle.json").is_file())
            self.assertEqual(
                '{"forged":"agent-visible"}',
                (Path(temp) / "lifecycle-handle.json").read_text(encoding="utf-8"),
            )
            self.assertEqual(
                "forged",
                (Path(temp) / "lifecycle-launch.lock").read_text(encoding="utf-8"),
            )
            stored = json.loads((private / "lifecycle-handle.json").read_text())
            self.assertEqual({"schema", "request_digest", "handle"}, set(stored))
            self.assertNotIn("Extract one accepted requirement", json.dumps(stored))

    def test_subprocess_crash_after_launch_reconnects_without_duplicate_launch(self) -> None:
        worker = controller_worker("hermes_restart.py")
        with tempfile.TemporaryDirectory() as temp:
            first = subprocess.run(
                [sys.executable, str(worker), temp, "crash"],
                cwd=str(Path(__file__).resolve().parents[2]),
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(73, first.returncode, first.stderr)

            resumed = subprocess.run(
                [sys.executable, str(worker), temp, "resume"],
                cwd=str(Path(__file__).resolve().parents[2]),
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(0, resumed.returncode, resumed.stderr)
            state = json.loads((Path(temp) / "lifecycle-state.json").read_text())
            self.assertEqual(1, state["launch_count"])
            self.assertEqual(1, state["reconnect_count"])

    def test_concurrent_cross_process_recovery_is_fenced_by_active_lease(self) -> None:
        worker = controller_worker("hermes_restart.py")
        cwd = str(Path(__file__).resolve().parents[2])
        with tempfile.TemporaryDirectory() as temp:
            crashed = subprocess.run(
                [sys.executable, str(worker), temp, "crash"],
                cwd=cwd,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(73, crashed.returncode, crashed.stderr)
            holder = subprocess.Popen(
                [sys.executable, str(worker), temp, "resume-hold"],
                cwd=cwd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                marker = Path(temp) / "recovery-active"
                deadline = time.monotonic() + 10
                while not marker.exists() and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertTrue(marker.exists(), "first recovery did not acquire its lease")

                duplicate = subprocess.run(
                    [sys.executable, str(worker), temp, "resume"],
                    cwd=cwd,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertNotEqual(0, duplicate.returncode)
                self.assertIn("active in another process", duplicate.stderr)
                state = json.loads((Path(temp) / "lifecycle-state.json").read_text())
                self.assertEqual(1, state["launch_count"])
                self.assertEqual(1, state["reconnect_count"])
            finally:
                (Path(temp) / "release-recovery").write_text("release", encoding="utf-8")
                stdout, stderr = holder.communicate(timeout=10)
            self.assertEqual(0, holder.returncode, f"{stdout}\n{stderr}")

    def test_cross_process_unpersisted_result_fence_blocks_agent_replay(self) -> None:
        worker = controller_worker("hermes_restart.py")
        cwd = str(Path(__file__).resolve().parents[2])
        with tempfile.TemporaryDirectory() as temp:
            completed = subprocess.run(
                [sys.executable, str(worker), temp, "complete-unpersisted"],
                cwd=cwd,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(74, completed.returncode, completed.stderr)

            replay = subprocess.run(
                [sys.executable, str(worker), temp, "resume"],
                cwd=cwd,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertNotEqual(0, replay.returncode)
            self.assertIn("result persistence is unresolved", replay.stderr)
            state = json.loads((Path(temp) / "lifecycle-state.json").read_text())
            self.assertEqual(1, state["launch_count"])
            self.assertEqual(0, state["reconnect_count"])

    def test_cross_process_pending_replace_failure_blocks_agent_replay(self) -> None:
        worker = controller_worker("hermes_restart.py")
        cwd = str(Path(__file__).resolve().parents[2])
        with tempfile.TemporaryDirectory() as temp:
            failed = subprocess.run(
                [sys.executable, str(worker), temp, "pending-replace-failure"],
                cwd=cwd,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(0, failed.returncode)
            self.assertIn("injected pending replace failure", failed.stderr)

            replay = subprocess.run(
                [sys.executable, str(worker), temp, "resume"],
                cwd=cwd,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertNotEqual(0, replay.returncode)
            self.assertIn("result persistence is unresolved", replay.stderr)
            state = json.loads((Path(temp) / "lifecycle-state.json").read_text())
            self.assertEqual(1, state["launch_count"])
            self.assertEqual(0, state["reconnect_count"])

    def test_cross_process_lease_corruption_after_terminal_success_blocks_agent_replay(
        self,
    ) -> None:
        worker = controller_worker("hermes_restart.py")
        cwd = str(Path(__file__).resolve().parents[2])
        with tempfile.TemporaryDirectory() as temp:
            completed = subprocess.run(
                [sys.executable, str(worker), temp, "lease-read-failure"],
                cwd=cwd,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(75, completed.returncode, completed.stderr)

            replay = subprocess.run(
                [sys.executable, str(worker), temp, "resume"],
                cwd=cwd,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertNotEqual(0, replay.returncode)
            self.assertIn("result persistence is unresolved", replay.stderr)
            state = json.loads((Path(temp) / "lifecycle-state.json").read_text())
            self.assertEqual(1, state["launch_count"])
            self.assertEqual(0, state["reconnect_count"])

    def test_deferred_recovery_lease_is_held_until_result_persistence_acknowledgement(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            durable = self._durable_invocation(temp)
            executor = self._durable_executor(
                FakeLifecycle(structured_payload={"requirement": "accepted"}),
                temp,
                defer_release=True,
            )

            result = executor.invoke(durable)

            self.assertEqual({"requirement": "accepted"}, result.output)
            private = self._private_invocation_directory(temp, durable.invocation_id)
            lease = private / "lifecycle-recovery-lease.json"
            pending = private / "lifecycle-result-pending.json"
            self.assertTrue(lease.is_file())
            self.assertTrue(pending.is_file())
            CONTROLLER.HermesSubagentExecutor._handle_cache.clear()
            restarted = self._durable_executor(
                FakeLifecycle(structured_payload={"requirement": "accepted"}),
                temp,
                defer_release=True,
            )

            with self.assertRaisesRegex(RuntimeError, "result persistence is unresolved"):
                restarted.invoke(durable)

            executor.acknowledge_persisted_result(durable.invocation_id)

            self.assertFalse(lease.exists())
            self.assertFalse(pending.exists())

    def test_terminal_result_retrieval_failure_retains_unresolved_result_fence(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            durable = self._durable_invocation(temp)
            executor = self._durable_executor(
                FakeLifecycle(
                    structured_payload={"requirement": "accepted"},
                    fail_result=True,
                ),
                temp,
                defer_release=True,
            )

            with self.assertRaisesRegex(RuntimeError, "result retrieval failure"):
                executor.invoke(durable)

            private = self._private_invocation_directory(temp, durable.invocation_id)
            self.assertTrue((private / "lifecycle-recovery-lease.json").is_file())
            self.assertTrue((private / "lifecycle-result-pending.json").is_file())
            CONTROLLER.HermesSubagentExecutor._handle_cache.clear()
            restarted = self._durable_executor(
                FakeLifecycle(structured_payload={"requirement": "accepted"}),
                temp,
                defer_release=True,
            )

            with self.assertRaisesRegex(RuntimeError, "result persistence is unresolved"):
                restarted.invoke(durable)
            lease = executor._pending_leases.pop(durable.invocation_id)
            executor._release_recovery_lease(*lease)

    def test_pending_fence_replace_failure_remains_blocking_after_owner_exit(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            durable = self._durable_invocation(temp)
            executor = self._durable_executor(
                FakeLifecycle(structured_payload={"requirement": "accepted"}),
                temp,
                defer_release=True,
            )
            with mock.patch.object(
                CONTROLLER.HermesSubagentExecutor,
                "_durable_replace",
                side_effect=OSError("injected pending replace failure"),
            ):
                with self.assertRaisesRegex(OSError, "pending replace failure"):
                    executor.invoke(durable)

            private = self._private_invocation_directory(temp, durable.invocation_id)
            self.assertTrue((private / "lifecycle-result-pending.json.tmp").exists())
            lease = executor._pending_leases.pop(durable.invocation_id)
            executor._release_recovery_lease(*lease)
            CONTROLLER.HermesSubagentExecutor._handle_cache.clear()
            restarted = self._durable_executor(
                FakeLifecycle(structured_payload={"requirement": "accepted"}),
                temp,
                defer_release=True,
            )

            with self.assertRaisesRegex(
                RuntimeError,
                "result persistence is unresolved|unresolved result fence",
            ):
                restarted.invoke(durable)

    def test_pending_fence_write_failure_remains_blocking_after_owner_exit(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            durable = self._durable_invocation(temp)
            executor = self._durable_executor(
                FakeLifecycle(structured_payload={"requirement": "accepted"}),
                temp,
                defer_release=True,
            )
            with mock.patch.object(
                CONTROLLER.HermesSubagentExecutor,
                "_write_and_fsync",
                side_effect=OSError("injected pending write failure"),
            ):
                with self.assertRaisesRegex(OSError, "pending write failure"):
                    executor.invoke(durable)

            private = self._private_invocation_directory(temp, durable.invocation_id)
            self.assertTrue((private / "lifecycle-result-pending.json.tmp").exists())
            lease = executor._pending_leases.pop(durable.invocation_id)
            executor._release_recovery_lease(*lease)
            CONTROLLER.HermesSubagentExecutor._handle_cache.clear()
            restarted = self._durable_executor(
                FakeLifecycle(structured_payload={"requirement": "accepted"}),
                temp,
                defer_release=True,
            )

            with self.assertRaisesRegex(
                RuntimeError,
                "result persistence is unresolved|unresolved result fence",
            ):
                restarted.invoke(durable)

    @unittest.skipUnless(
        os.environ.get("HERMES_CORE_ROOT"),
        "HERMES_CORE_ROOT is required for the real Core lifecycle restart probe",
    )
    def test_real_core_lifecycle_recovers_across_plugin_process_restart(self) -> None:
        worker = controller_worker("core_lifecycle_restart.py")
        core_python = (
            Path(os.environ["HERMES_CORE_ROOT"]) / ".venv" / "Scripts" / "python.exe"
        )
        self.assertTrue(core_python.is_file())
        with tempfile.TemporaryDirectory() as temp:
            first = subprocess.run(
                [str(core_python), str(worker), temp, "crash"],
                cwd=str(Path(__file__).resolve().parents[2]),
                env=dict(os.environ),
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(73, first.returncode, first.stderr)

            resumed = subprocess.run(
                [str(core_python), str(worker), temp, "resume"],
                cwd=str(Path(__file__).resolve().parents[2]),
                env=dict(os.environ),
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(0, resumed.returncode, resumed.stderr)
            state = json.loads(
                (Path(temp) / "core-lifecycle-state.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                {"build_count": 2, "launch_count": 1, "reconnect_count": 1},
                state,
            )
            private = self._private_invocation_directory(
                temp,
                "run-plugin-core-process:extract:1",
            )
            durable = json.loads(
                (private / "lifecycle-handle.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                "child-plugin-core-restart",
                durable["handle"]["session_id"],
            )
            self.assertTrue(durable["handle"]["request_digest"])
            self.assertTrue(durable["handle"]["resume_capability"])

    def test_unreconnectable_durable_handle_fails_closed_without_duplicate_launch(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            durable = self._durable_invocation(temp)
            first = FakeLifecycle(
                structured_payload={"requirement": "accepted"},
                fail_wait_once=True,
            )
            with self.assertRaisesRegex(RuntimeError, "interruption after launch"):
                self._durable_executor(first, temp).invoke(durable)
            CONTROLLER.HermesSubagentExecutor._handle_cache.clear()
            restarted = FakeLifecycle(
                structured_payload={"requirement": "accepted"},
                reconnect_connected=False,
            )

            with self.assertRaisesRegex(RuntimeError, "recovery unavailable"):
                self._durable_executor(restarted, temp).invoke(durable)

            self.assertEqual([], restarted.accepted_launches)

    def test_handle_persistence_failure_keeps_launch_lock_and_blocks_relaunch(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            durable = self._durable_invocation(temp)
            first = FakeLifecycle(structured_payload={"requirement": "accepted"})
            with mock.patch.object(
                CONTROLLER.HermesSubagentExecutor,
                "_write_durable_handle",
                side_effect=OSError("injected durable handle write failure"),
            ):
                with self.assertRaisesRegex(OSError, "handle write failure"):
                    self._durable_executor(first, temp).invoke(durable)
            self.assertEqual(1, len(first.accepted_launches))
            private = self._private_invocation_directory(temp, durable.invocation_id)
            self.assertTrue((private / "lifecycle-launch.lock").is_file())
            self.assertFalse((private / "lifecycle-handle.json").exists())
            CONTROLLER.HermesSubagentExecutor._handle_cache.clear()
            restarted = FakeLifecycle(structured_payload={"requirement": "accepted"})

            with self.assertRaisesRegex(RuntimeError, "durable launch is unresolved"):
                self._durable_executor(restarted, temp).invoke(durable)

            self.assertEqual([], restarted.accepted_launches)

    def test_restart_after_launch_reuses_same_lifecycle_handle(self) -> None:
        lifecycle = FakeLifecycle(
            structured_payload={"requirement": "accepted"},
            fail_wait_once=True,
        )

        with self.assertRaisesRegex(RuntimeError, "interruption after launch"):
            CONTROLLER.HermesSubagentExecutor(lifecycle).invoke(invocation())
        result = CONTROLLER.HermesSubagentExecutor(lifecycle).invoke(invocation())

        self.assertEqual({"requirement": "accepted"}, result.output)
        self.assertEqual(1, len(lifecycle.accepted_launches))

    def test_launch_uses_public_lifecycle_workspace_contract_and_skills(self) -> None:
        lifecycle = FakeLifecycle(structured_payload={"requirement": "accepted"})
        executor = CONTROLLER.HermesSubagentExecutor(lifecycle)
        bounded = CONTROLLER.StepInvocation(
            run_id="run-hermes",
            invocation_id="run-hermes:implementation_delivery:1",
            step_id="implementation_delivery",
            goal="Execute one accepted task.",
            allowed_toolsets=("file", "gradle-mcp"),
            acceptance_criteria=("Return typed delivery.",),
            output_schema={"type": "object"},
            input={"artifact_ref": {"artifact_id": "graph", "sha256": "a" * 64}},
            working_directory="C:/workspace",
            skills=("kwc-impl-authoring-runtime",),
        )

        executor.invoke(bounded)

        request = lifecycle.accepted_launches[0]
        self.assertNotIn("working_directory", request)
        envelope = json.loads(str(request["context"]))
        self.assertEqual("C:/workspace", envelope["workspace_path"])
        self.assertEqual(["kwc-impl-authoring-runtime"], envelope["skills"])

    def test_launches_public_lifecycle_with_bounded_contract(self) -> None:
        lifecycle = FakeLifecycle(
            structured_payload={
                "schema_valid": True,
                "schema_errors": [],
                "schema_note": None,
            },
            summary=(
                "Validated result:\n```json\n"
                '{"requirement":"Ship deterministic orchestration."}'
                "\n```"
            ),
        )
        executor = CONTROLLER.HermesSubagentExecutor(lifecycle)

        result = executor.invoke(invocation())

        self.assertEqual(result.agent_id, "sa-hermes-1")
        self.assertEqual(
            result.output,
            {"requirement": "Ship deterministic orchestration."},
        )
        self.assertEqual(len(lifecycle.accepted_launches), 1)
        request = lifecycle.accepted_launches[0]
        self.assertEqual(request["goal"], "Extract one accepted requirement.")
        self.assertEqual(request["correlation_id"], "run-hermes:extract:1")
        self.assertEqual(request["allowed_toolsets"], ("file",))
        self.assertEqual(request["output_schema"], invocation().output_schema)
        self.assertEqual(
            request["metadata"],
            {
                "run_id": "run-hermes",
                "step_id": "extract",
                "invocation_id": "run-hermes:extract:1",
            },
        )
        envelope = json.loads(str(request["context"]))
        self.assertEqual(envelope["input"], {"request": "Build it"})
        self.assertEqual(
            envelope["acceptance_criteria"],
            ["Return exactly one requirement string."],
        )
        self.assertEqual(envelope["output_schema"], invocation().output_schema)
        self.assertNotIn("unrelated", envelope)

    def test_native_schema_rejection_fails_closed(self) -> None:
        lifecycle = FakeLifecycle(
            structured_payload={
                "schema_valid": False,
                "schema_errors": ["$.requirement: is required"],
                "schema_note": "UNVALIDATED output",
            },
            summary='{"unexpected":"value"}',
        )
        executor = CONTROLLER.HermesSubagentExecutor(lifecycle)

        with self.assertRaisesRegex(ValueError, "native output schema validation failed"):
            executor.invoke(invocation())

    def test_falls_back_to_exact_json_when_native_schema_is_unavailable(self) -> None:
        lifecycle = FakeLifecycle(
            summary='{"requirement":"Ship deterministic orchestration."}',
            reject_native_schema=True,
        )
        executor = CONTROLLER.HermesSubagentExecutor(lifecycle)

        result = executor.invoke(invocation())

        self.assertEqual(
            result.output,
            {"requirement": "Ship deterministic orchestration."},
        )
        self.assertEqual(len(lifecycle.launch_attempts), 2)
        self.assertIn("output_schema", lifecycle.launch_attempts[0])
        self.assertNotIn("output_schema", lifecycle.accepted_launches[0])

    def test_malformed_fallback_summary_fails_closed(self) -> None:
        lifecycle = FakeLifecycle(
            summary="```json\n{\"requirement\":\"not exact\"}\n```",
            reject_native_schema=True,
        )
        executor = CONTROLLER.HermesSubagentExecutor(lifecycle)

        with self.assertRaisesRegex(ValueError, "valid JSON object"):
            executor.invoke(invocation())

    def test_failed_subagent_result_fails_closed(self) -> None:
        lifecycle = FakeLifecycle(terminal_state="FAILED", summary="{}")
        executor = CONTROLLER.HermesSubagentExecutor(lifecycle)

        with self.assertRaisesRegex(RuntimeError, "Hermes subagent failed"):
            executor.invoke(invocation())

    def test_empty_toolset_contract_is_rejected_before_launch(self) -> None:
        lifecycle = FakeLifecycle(structured_payload={"requirement": "unsafe"})
        executor = CONTROLLER.HermesSubagentExecutor(lifecycle)

        with self.assertRaisesRegex(ValueError, "allowed_toolsets must be non-empty"):
            executor.invoke(invocation(allowed_toolsets=()))

        self.assertEqual(lifecycle.launch_attempts, [])


if __name__ == "__main__":
    unittest.main()
