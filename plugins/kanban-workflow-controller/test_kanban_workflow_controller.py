"""Contract tests for Kanban Workflow Controller."""

from __future__ import annotations

import importlib.util
import json
import re
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch


_ROOT = Path(__file__).parent
_SPEC = importlib.util.spec_from_file_location(
    "kanban_workflow_controller_plugin", _ROOT / "__init__.py"
)
assert _SPEC and _SPEC.loader
PLUGIN = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(PLUGIN)


class FakeContext:
    def __init__(self, settings=None) -> None:
        self.hooks: dict[str, object] = {}
        self.settings = settings or {}
        self.injected: list[tuple[str, str]] = []

    def register_hook(self, name: str, callback) -> None:
        self.hooks[name] = callback

    def get_config(self, key: str, default=None):
        return self.settings.get(key, default)

    def inject_message(self, content: str, *, session_key: str) -> bool:
        self.injected.append((session_key, content))
        return True


class KanbanWorkflowControllerContractTests(unittest.TestCase):
    def _seed_state(self, path: Path, *, session_key: str = "gateway-session", cursor: int = 41) -> None:
        with patch.object(PLUGIN, "_state_path", return_value=path):
            PLUGIN._bind_state("issue151", session_key, cursor)

    def _read_state(self, path: Path):
        with patch.object(PLUGIN, "_state_path", return_value=path):
            return PLUGIN._read_state()

    def test_manifest_starts_new_product_at_version_0_1_0(self) -> None:
        manifest = (_ROOT / "plugin.yaml").read_text(encoding="utf-8")
        changelog = (_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        self.assertRegex(manifest, r"(?m)^name:\s*kanban-workflow-controller\s*$")
        self.assertRegex(manifest, r"(?m)^version:\s*0\.1\.0\s*$")
        latest_release = re.search(r"(?m)^## \[([^]]+)\]", changelog)
        self.assertIsNotNone(latest_release)
        self.assertEqual(latest_release.group(1), "0.1.0")

    def test_registers_only_passive_observer_hooks(self) -> None:
        ctx = FakeContext()
        PLUGIN.register(ctx)
        self.assertEqual(set(ctx.hooks), {"pre_llm_call", "on_kanban_dispatch_tick"})

    def test_run_workflow_binds_durable_gateway_session_key(self) -> None:
        ctx = FakeContext({"board": "issue151"})
        PLUGIN.register(ctx)
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.db"
            with (
                patch.object(PLUGIN, "_state_path", return_value=state_path),
                patch.object(PLUGIN, "_latest_event_id", return_value=41),
                patch.object(PLUGIN, "_current_session_key", return_value="durable-gateway-key"),
                patch.object(PLUGIN, "_current_session_profile", return_value="project-manager"),
            ):
                ctx.hooks["pre_llm_call"](
                    session_id="ephemeral-agent-id",
                    user_message='[IMPORTANT: The user has invoked the "run-workflow" skill.]',
                )
                state = PLUGIN._read_state()
        self.assertEqual(state["coordinator_session_key"], "durable-gateway-key")
        self.assertEqual(state["last_event_id"], 41)

    def test_missing_durable_session_key_does_not_bind(self) -> None:
        ctx = FakeContext({"board": "issue151"})
        PLUGIN.register(ctx)
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.db"
            with (
                patch.object(PLUGIN, "_state_path", return_value=state_path),
                patch.object(PLUGIN, "_current_session_key", return_value=""),
            ):
                ctx.hooks["pre_llm_call"](
                    session_id="ephemeral-agent-id", user_message="/run-workflow Issue 151"
                )
            self.assertFalse(state_path.exists())

    def test_non_workflow_child_session_cannot_replace_binding(self) -> None:
        ctx = FakeContext({"board": "issue151"})
        PLUGIN.register(ctx)
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.db"
            self._seed_state(state_path)
            with patch.object(PLUGIN, "_state_path", return_value=state_path):
                ctx.hooks["pre_llm_call"](
                    session_id="child-agent-id",
                    parent_session_id="parent-agent-id",
                    user_message="delegated child work",
                )
            state = self._read_state(state_path)
        self.assertEqual(state["coordinator_session_key"], "gateway-session")
        self.assertEqual(state["last_event_id"], 41)

    def test_child_workflow_marker_cannot_replace_binding(self) -> None:
        ctx = FakeContext({"board": "issue151"})
        PLUGIN.register(ctx)
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.db"
            self._seed_state(state_path)
            with (
                patch.object(PLUGIN, "_state_path", return_value=state_path),
                patch.object(PLUGIN, "_latest_event_id", return_value=99),
                patch.object(PLUGIN, "_current_session_key", return_value="child-key"),
                patch.object(PLUGIN, "_current_session_profile", return_value="project-manager"),
            ):
                ctx.hooks["pre_llm_call"](
                    session_id="child-agent-id",
                    parent_session_id="parent-agent-id",
                    user_message='[IMPORTANT: The user has invoked the "run-workflow" skill.]',
                )
            state = self._read_state(state_path)
        self.assertEqual(state["coordinator_session_key"], "gateway-session")
        self.assertEqual(state["last_event_id"], 41)

    def test_run_workflow_detection_rejects_prefixes_and_quoted_marker(self) -> None:
        self.assertTrue(PLUGIN._is_run_workflow_invocation("/run-workflow Issue 151"))
        self.assertFalse(PLUGIN._is_run_workflow_invocation("/run-workflow-anything"))
        self.assertFalse(
            PLUGIN._is_run_workflow_invocation(
                'Please quote: The user has invoked the "run-workflow" skill'
            )
        )
        self.assertFalse(
            PLUGIN._is_run_workflow_invocation(
                '[IMPORTANT: The user has invoked the "run-workflow" skill-not-real.]'
            )
        )

    def test_non_coordinator_profile_cannot_bind(self) -> None:
        ctx = FakeContext({"board": "issue151", "coordinator_profile": "project-manager"})
        PLUGIN.register(ctx)
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.db"
            with (
                patch.object(PLUGIN, "_state_path", return_value=state_path),
                patch.object(PLUGIN, "_latest_event_id", return_value=41),
                patch.object(PLUGIN, "_current_session_key", return_value="main-key"),
                patch.object(PLUGIN, "_current_session_profile", return_value="main"),
            ):
                ctx.hooks["pre_llm_call"](user_message="/run-workflow Issue 151")
            self.assertFalse(state_path.exists())

    def test_same_session_refresh_preserves_pending_cursor(self) -> None:
        ctx = FakeContext({"board": "issue151"})
        PLUGIN.register(ctx)
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.db"
            self._seed_state(state_path, cursor=41)
            with (
                patch.object(PLUGIN, "_state_path", return_value=state_path),
                patch.object(PLUGIN, "_latest_event_id", return_value=99),
                patch.object(PLUGIN, "_current_session_key", return_value="gateway-session"),
                patch.object(PLUGIN, "_current_session_profile", return_value="project-manager"),
            ):
                ctx.hooks["pre_llm_call"](
                    parent_session_id="compressed-parent",
                    user_message='[IMPORTANT: The user has invoked the "run-workflow" skill.]',
                )
            state = self._read_state(state_path)
        self.assertEqual(state["last_event_id"], 41)

    def test_rebinding_cannot_move_cursor_behind_committed_progress(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.db"
            self._seed_state(state_path, session_key="old", cursor=50)
            with patch.object(PLUGIN, "_state_path", return_value=state_path):
                PLUGIN._bind_state("issue151", "new", 41)
            state = self._read_state(state_path)
        self.assertEqual(state["coordinator_session_key"], "new")
        self.assertEqual(state["last_event_id"], 50)

    def test_dispatch_tick_routes_only_worker_and_user_decision_events(self) -> None:
        ctx = FakeContext(
            {
                "board": "issue151",
                "coordinator_profile": "project-manager",
                "decision_comment_author": "dashboard",
            }
        )
        PLUGIN.register(ctx)
        decision_body = {"schema": "policy-decision-card-v1", "decision_owner": "user"}
        events = [
            {"id": 42, "task_id": "t_impl", "kind": "commented", "payload": {"author": "coder"},
             "status": "blocked", "assignee": "coder", "event_profile": None, "task_body": {}},
            {"id": 43, "task_id": "t_impl", "kind": "blocked", "payload": {},
             "status": "done", "assignee": "project-manager", "event_profile": "coder", "task_body": {}},
            {"id": 44, "task_id": "t_impl", "kind": "completed", "payload": {},
             "status": "done", "assignee": "project-manager", "event_profile": "coder", "task_body": {}},
            {"id": 45, "task_id": "t_pm", "kind": "completed", "payload": {},
             "status": "done", "assignee": "coder", "event_profile": "project-manager", "task_body": {}},
            {"id": 46, "task_id": "t_generic", "kind": "commented", "payload": {"author": "dashboard"},
             "status": "blocked", "assignee": "project-manager", "event_profile": None,
             "task_body": {"schema": "other"}},
            {"id": 47, "task_id": "t_decision", "kind": "commented", "payload": {"author": "coder"},
             "status": "blocked", "assignee": "project-manager", "event_profile": None,
             "task_body": decision_body},
            {"id": 48, "task_id": "t_decision", "kind": "commented", "payload": {"author": "dashboard"},
             "status": "blocked", "assignee": "project-manager", "event_profile": None,
             "task_body": decision_body},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.db"
            self._seed_state(state_path)
            with (
                patch.object(PLUGIN, "_state_path", return_value=state_path),
                patch.object(PLUGIN, "_read_events", return_value=events),
            ):
                ctx.hooks["on_kanban_dispatch_tick"](board="issue151", dry_run=False)
            state = self._read_state(state_path)
        routed = [json.loads(content) for _, content in ctx.injected]
        self.assertEqual([item["event_kind"] for item in routed], ["blocked", "completed", "commented"])
        self.assertEqual([item["source_task_id"] for item in routed], ["t_impl", "t_impl", "t_decision"])
        self.assertTrue(all(session == "gateway-session" for session, _ in ctx.injected))
        self.assertTrue(all("comment" not in item for item in routed))
        self.assertEqual(state["last_event_id"], 48)

    def test_failed_session_injection_keeps_event_for_retry(self) -> None:
        class RejectingContext(FakeContext):
            def inject_message(self, content: str, *, session_key: str) -> bool:
                self.injected.append((session_key, content))
                return False

        ctx = RejectingContext({"board": "issue151"})
        PLUGIN.register(ctx)
        events = [{"id": 9, "task_id": "t_impl", "kind": "blocked", "payload": {},
                   "status": "blocked", "assignee": "coder", "event_profile": "coder",
                   "task_body": {}}]
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.db"
            self._seed_state(state_path, cursor=8)
            with (
                patch.object(PLUGIN, "_state_path", return_value=state_path),
                patch.object(PLUGIN, "_read_events", return_value=events),
            ):
                ctx.hooks["on_kanban_dispatch_tick"](board="issue151", dry_run=False)
            state = self._read_state(state_path)
        self.assertEqual(state["last_event_id"], 8)

    def test_stale_dispatcher_cannot_inject_into_replaced_binding(self) -> None:
        ctx = FakeContext({"board": "issue151"})
        PLUGIN.register(ctx)
        event = {"id": 9, "task_id": "t_impl", "kind": "blocked", "payload": {},
                 "status": "blocked", "assignee": "coder", "event_profile": "coder",
                 "task_body": {}}
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.db"
            self._seed_state(state_path, session_key="old", cursor=8)
            stale = self._read_state(state_path)
            self._seed_state(state_path, session_key="new", cursor=20)
            with patch.object(PLUGIN, "_state_path", return_value=state_path):
                processed = PLUGIN._process_event(stale, event, True, "wake")
            current = self._read_state(state_path)
        self.assertFalse(processed)
        self.assertEqual(ctx.injected, [])
        self.assertEqual(current["coordinator_session_key"], "new")
        self.assertEqual(current["last_event_id"], 20)

    def test_concurrent_dispatch_ticks_inject_event_once(self) -> None:
        ctx = FakeContext({"board": "issue151"})
        PLUGIN.register(ctx)
        event = {"id": 9, "task_id": "t_impl", "kind": "completed", "payload": {},
                 "status": "done", "assignee": "coder", "event_profile": "coder",
                 "task_body": {}}
        barrier = threading.Barrier(2)

        def read_events(_board, _after_id):
            barrier.wait(timeout=5)
            return [event]

        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.db"
            self._seed_state(state_path, cursor=8)
            with (
                patch.object(PLUGIN, "_state_path", return_value=state_path),
                patch.object(PLUGIN, "_read_events", side_effect=read_events),
            ):
                threads = [
                    threading.Thread(
                        target=ctx.hooks["on_kanban_dispatch_tick"],
                        kwargs={"board": "issue151", "dry_run": False},
                    )
                    for _ in range(2)
                ]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(timeout=10)
            state = self._read_state(state_path)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(len(ctx.injected), 1)
        self.assertEqual(state["last_event_id"], 9)

    def test_dispatch_tick_ignores_other_board_and_dry_run(self) -> None:
        ctx = FakeContext({"board": "issue151"})
        PLUGIN.register(ctx)
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.db"
            self._seed_state(state_path, cursor=1)
            with (
                patch.object(PLUGIN, "_state_path", return_value=state_path),
                patch.object(PLUGIN, "_read_events") as read_events,
            ):
                ctx.hooks["on_kanban_dispatch_tick"](board="another", dry_run=False)
                ctx.hooks["on_kanban_dispatch_tick"](board="issue151", dry_run=True)
        read_events.assert_not_called()
        self.assertEqual(ctx.injected, [])


if __name__ == "__main__":
    unittest.main()
