"""Tests for the Desktop-owned KWC runtime process lifecycle."""

from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


_ROOT = Path(__file__).parent
_SPEC = importlib.util.spec_from_file_location(
    "kanban_workflow_controller_runtime_service", _ROOT / "runtime_service.py"
)
assert _SPEC and _SPEC.loader
RUNTIME = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(RUNTIME)


class _FakeProcess:
    def __init__(self, pid: int) -> None:
        self.pid = pid


class RuntimeServiceTests(unittest.TestCase):
    def test_windows_pid_probe_never_uses_os_kill(self) -> None:
        with (
            patch.object(RUNTIME.os, "name", "nt"),
            patch.object(RUNTIME, "_windows_pid_alive", return_value=True) as windows_probe,
            patch.object(RUNTIME.os, "kill") as kill,
        ):
            self.assertTrue(RUNTIME._pid_alive(4321))

        windows_probe.assert_called_once_with(4321)
        kill.assert_not_called()

    def test_ensure_runtime_starts_once_and_reuses_live_process(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.db"
            calls: list[tuple[list[str], dict[str, object]]] = []

            def popen(command, **kwargs):
                calls.append((list(command), dict(kwargs)))
                return _FakeProcess(4321)

            first = RUNTIME.ensure_runtime(
                state_path=state_path,
                plugin_root=_ROOT,
                parent_pid=1234,
                python_executable="python-test",
                now_fn=lambda: 1000,
                popen_fn=popen,
                pid_alive_fn=lambda pid: pid in {4321, 1234},
            )
            second = RUNTIME.ensure_runtime(
                state_path=state_path,
                plugin_root=_ROOT,
                parent_pid=1234,
                python_executable="python-test",
                now_fn=lambda: 1001,
                popen_fn=popen,
                pid_alive_fn=lambda pid: pid in {4321, 1234},
            )

        self.assertTrue(first["running"])
        self.assertTrue(first["started"])
        self.assertEqual(first["pid"], 4321)
        self.assertEqual(second["generation"], first["generation"])
        self.assertFalse(second["started"])
        self.assertEqual(len(calls), 1)
        command, kwargs = calls[0]
        self.assertEqual(command[0], "python-test")
        self.assertIn("--worker", command)
        self.assertIn("--state-db", command)
        self.assertIn("--parent-pid", command)
        self.assertFalse(kwargs["shell"])
        if RUNTIME.os.name == "nt":
            self.assertEqual(kwargs["creationflags"], RUNTIME.subprocess.CREATE_NO_WINDOW)

    def test_live_orphan_is_replaced_when_its_desktop_parent_is_gone(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.db"
            pids = iter((1111, 2222))
            calls: list[int] = []

            def popen(_command, **_kwargs):
                pid = next(pids)
                calls.append(pid)
                return _FakeProcess(pid)

            first = RUNTIME.ensure_runtime(
                state_path=state_path,
                plugin_root=_ROOT,
                parent_pid=9000,
                now_fn=lambda: 1000,
                popen_fn=popen,
                pid_alive_fn=lambda pid: pid in {1111, 9000},
            )
            second = RUNTIME.ensure_runtime(
                state_path=state_path,
                plugin_root=_ROOT,
                parent_pid=9001,
                now_fn=lambda: 1001,
                popen_fn=popen,
                pid_alive_fn=lambda pid: pid in {1111, 2222, 9001},
            )

        self.assertEqual(calls, [1111, 2222])
        self.assertNotEqual(first["generation"], second["generation"])
        self.assertEqual(second["parent_pid"], 9001)

    def test_stale_runtime_is_replaced_under_singleton_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.db"
            pids = iter((1111, 2222))
            calls: list[int] = []

            def popen(_command, **_kwargs):
                pid = next(pids)
                calls.append(pid)
                return _FakeProcess(pid)

            first = RUNTIME.ensure_runtime(
                state_path=state_path,
                plugin_root=_ROOT,
                parent_pid=9000,
                now_fn=lambda: 1000,
                popen_fn=popen,
                pid_alive_fn=lambda pid: pid == 1111,
            )
            second = RUNTIME.ensure_runtime(
                state_path=state_path,
                plugin_root=_ROOT,
                parent_pid=9001,
                now_fn=lambda: 1100,
                popen_fn=popen,
                pid_alive_fn=lambda _pid: False,
            )

        self.assertEqual(calls, [1111, 2222])
        self.assertNotEqual(first["generation"], second["generation"])
        self.assertEqual(second["pid"], 2222)
        self.assertTrue(second["started"])

    def test_stop_request_is_persisted_for_the_owned_generation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.db"
            started = RUNTIME.ensure_runtime(
                state_path=state_path,
                plugin_root=_ROOT,
                parent_pid=1234,
                now_fn=lambda: 1000,
                popen_fn=lambda *_args, **_kwargs: _FakeProcess(4321),
                pid_alive_fn=lambda pid: pid in {4321, 1234},
            )

            stopped = RUNTIME.request_runtime_stop(
                state_path=state_path,
                generation=started["generation"],
                now_fn=lambda: 1005,
                pid_alive_fn=lambda pid: pid in {4321, 1234},
            )

        self.assertTrue(stopped["stop_requested"])
        self.assertEqual(stopped["generation"], started["generation"])

    def test_worker_exits_when_parent_dies_and_marks_generation_stopped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.db"
            started = RUNTIME.ensure_runtime(
                state_path=state_path,
                plugin_root=_ROOT,
                parent_pid=1234,
                now_fn=lambda: 1000,
                popen_fn=lambda *_args, **_kwargs: _FakeProcess(4321),
                pid_alive_fn=lambda pid: pid in {4321, 1234},
            )
            parent_checks = iter((True, False))
            times = iter((1001, 1002, 1003, 1004))

            result = RUNTIME.run_worker(
                state_path=state_path,
                parent_pid=1234,
                generation=started["generation"],
                interval_seconds=0,
                now_fn=lambda: next(times),
                sleep_fn=lambda _seconds: None,
                pid_alive_fn=lambda _pid: next(parent_checks),
            )
            status = RUNTIME.read_runtime_status(
                state_path=state_path,
                now_fn=lambda: 1005,
                pid_alive_fn=lambda _pid: False,
            )

        self.assertEqual(result["reason"], "parent_exited")
        self.assertEqual(status["status"], "stopped")
        self.assertFalse(status["running"])


if __name__ == "__main__":
    unittest.main()
