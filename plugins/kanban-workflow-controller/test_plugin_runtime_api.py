"""Contract tests for KWC runtime endpoints."""

from __future__ import annotations

import importlib.util
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch


_ROOT = Path(__file__).parent
_API_SPEC = importlib.util.spec_from_file_location(
    "kanban_workflow_controller_runtime_api", _ROOT / "dashboard" / "plugin_api.py"
)
assert _API_SPEC and _API_SPEC.loader
PLUGIN_API = importlib.util.module_from_spec(_API_SPEC)
_fastapi = types.ModuleType("fastapi")


class _FakeRouter:
    def get(self, _path):
        return lambda function: function

    def post(self, _path):
        return lambda function: function


class _FakeBackgroundTasks:
    def add_task(self, _function, *_args, **_kwargs) -> None:
        return None


class _FakeHTTPException(Exception):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


_fastapi.APIRouter = _FakeRouter
_fastapi.BackgroundTasks = _FakeBackgroundTasks
_fastapi.HTTPException = _FakeHTTPException
with patch.dict(sys.modules, {"fastapi": _fastapi}):
    _API_SPEC.loader.exec_module(PLUGIN_API)


class RuntimeApiTests(unittest.TestCase):
    def test_runtime_lease_starts_a_child_owned_by_desktop_serve(self) -> None:
        expected = {
            "running": True,
            "started": True,
            "status": "starting",
            "generation": "generation-1",
            "pid": 4321,
        }
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.db"
            with (
                patch.object(PLUGIN_API, "_state_path", return_value=state_path),
                patch.object(PLUGIN_API.os, "getpid", return_value=1234),
                patch.object(PLUGIN_API, "_ensure_runtime", return_value=expected) as ensure,
            ):
                response = PLUGIN_API.runtime_lease()

        self.assertEqual(response, expected)
        self.assertEqual(ensure.call_args.kwargs["state_path"], state_path)
        self.assertEqual(ensure.call_args.kwargs["plugin_root"], _ROOT)
        self.assertEqual(ensure.call_args.kwargs["parent_pid"], 1234)

    def test_status_reports_runtime_and_dispatcher_health_separately(self) -> None:
        runtime = {
            "running": True,
            "started": False,
            "status": "running",
            "generation": "generation-1",
            "pid": 4321,
            "parent_pid": 1234,
            "heartbeat_at": 1009,
            "stop_requested": False,
            "exit_reason": None,
        }
        reconciliation = {
            "attempted_at": 1009,
            "succeeded_at": 1009,
            "healthy": True,
            "error_code": None,
        }
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.db"
            with (
                patch.object(PLUGIN_API, "_state_path", return_value=state_path),
                patch.object(PLUGIN_API, "_read_runtime_status", return_value=runtime),
                patch.object(
                    PLUGIN_API, "_read_reconcile_health", return_value=reconciliation
                ),
                patch.object(PLUGIN_API, "_read_heartbeats", return_value=[]),
                patch.object(PLUGIN_API, "_now", return_value=1010),
            ):
                response = PLUGIN_API.status()

        self.assertTrue(response["running"])
        self.assertEqual(response["runtime"], runtime)
        self.assertEqual(response["reconciliation"], reconciliation)
        self.assertFalse(response["dispatcher_running"])

    def test_runtime_decisions_returns_privacy_safe_engine_readback(self) -> None:
        expected = [
            {
                "board": "issue-151",
                "evaluated_at": 1000,
                "decision_schema": "kwc-workflow-decision-v1",
                "valid": True,
                "phase": "ready",
                "action_kind": "dispatch-task",
                "task_id": "t_impl",
                "profile": "coder",
                "skill": "run-impl-card",
                "reason": "ready_implementation",
                "errors": [],
            }
        ]
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.db"
            with (
                patch.object(PLUGIN_API, "_state_path", return_value=state_path),
                patch.object(
                    PLUGIN_API, "_read_workflow_decisions", return_value=expected
                ) as read_decisions,
            ):
                response = PLUGIN_API.runtime_decisions()

        self.assertEqual({"items": expected, "total": 1}, response)
        read_decisions.assert_called_once_with(state_path=state_path)

    def test_runtime_stop_targets_the_current_generation(self) -> None:
        current = {"generation": "generation-1"}
        expected = {"running": False, "status": "stopping", "generation": "generation-1"}
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.db"
            with (
                patch.object(PLUGIN_API, "_state_path", return_value=state_path),
                patch.object(PLUGIN_API, "_read_runtime_status", return_value=current),
                patch.object(PLUGIN_API, "_request_runtime_stop", return_value=expected) as stop,
            ):
                response = PLUGIN_API.runtime_stop()

        self.assertEqual(response, expected)
        stop.assert_called_once_with(state_path=state_path, generation="generation-1")

    def test_runtime_stop_is_a_noop_when_no_generation_exists(self) -> None:
        current = {"running": False, "status": "stopped", "generation": None}
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.db"
            with (
                patch.object(PLUGIN_API, "_state_path", return_value=state_path),
                patch.object(PLUGIN_API, "_read_runtime_status", return_value=current),
                patch.object(PLUGIN_API, "_request_runtime_stop") as stop,
            ):
                response = PLUGIN_API.runtime_stop()

        self.assertEqual(response, current)
        stop.assert_not_called()


if __name__ == "__main__":
    unittest.main()
