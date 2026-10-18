"""Contract tests for KWC task-scoped Profile worker launching."""

from __future__ import annotations

import sqlite3
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch


_ROOT = Path(__file__).resolve().parents[1]
from tests.support import load_module

LAUNCHER = load_module("runtime.profile_launcher")


class FakeSessionDB:
    def __init__(self) -> None:
        self.created = []
        self.titles = []
        self.closed = False

    def create_session(self, session_id, **kwargs) -> None:
        self.created.append((session_id, kwargs))

    def set_session_title(self, session_id, title) -> None:
        self.titles.append((session_id, title))

    def close(self) -> None:
        self.closed = True


class FakeProcess:
    pid = 4321


class ProfileLauncherTests(unittest.TestCase):
    def test_create_task_session_is_profile_scoped_and_contains_no_task_body(self) -> None:
        db = FakeSessionDB()
        with tempfile.TemporaryDirectory() as tmp:
            profile_home = Path(tmp) / "profiles" / "coder"
            profile_home.mkdir(parents=True)
            session = LAUNCHER.create_task_session(
                profile="coder",
                task_id="t_impl",
                workspace_path=Path(tmp),
                profile_home=profile_home,
                session_id_factory=lambda: "20261008_120000_abcd1234",
                session_db_factory=lambda _path: db,
            )

        self.assertEqual(session["session_id"], "20261008_120000_abcd1234")
        self.assertEqual(session["profile"], "coder")
        self.assertEqual(db.created[0][1]["profile_name"], "coder")
        self.assertEqual(db.created[0][1]["source"], "cli")
        self.assertEqual(db.created[0][1]["cwd"], str(Path(tmp).resolve()))
        self.assertEqual(
            db.titles,
            [("20261008_120000_abcd1234", "KWC task t_impl abcd1234")],
        )
        self.assertNotIn("task_body", db.created[0][1])
        self.assertTrue(db.closed)

    def test_missing_profile_home_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "profile_home"):
                LAUNCHER.create_task_session(
                    profile="coder",
                    task_id="t_impl",
                    workspace_path=Path(tmp),
                    profile_home=Path(tmp) / "profiles" / "missing",
                    session_id_factory=lambda: "20261008_120000_abcd1234",
                    session_db_factory=lambda _path: self.fail("must not create a profile"),
                )

    def test_reserve_session_row_uses_an_existing_canonical_session_store(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            profile_home = Path(tmp) / "profile"
            profile_home.mkdir()
            conn = sqlite3.connect(profile_home / "state.db")
            conn.execute(
                """CREATE TABLE sessions (
                    id TEXT PRIMARY KEY,
                    source TEXT NOT NULL,
                    created_source TEXT,
                    started_at REAL NOT NULL,
                    cwd TEXT,
                    profile_name TEXT,
                    title TEXT,
                    title_source TEXT
                )"""
            )
            conn.commit()
            conn.close()

            LAUNCHER.reserve_session_row(
                profile_home=profile_home,
                session_id="20261008_120000_abcd1234",
                profile="coder",
                workspace_path=Path(tmp) / "workspace",
                title="KWC task t_impl",
            )

            conn = sqlite3.connect(profile_home / "state.db")
            row = conn.execute(
                "SELECT source, created_source, cwd, profile_name, title, title_source "
                "FROM sessions WHERE id = ?",
                ("20261008_120000_abcd1234",),
            ).fetchone()
            conn.close()
            self.assertEqual(
                row,
                (
                    "cli",
                    "cli",
                    str(Path(tmp) / "workspace"),
                    "coder",
                    "KWC task t_impl",
                    "user",
                ),
            )

    def test_default_session_id_factory_uses_the_core_session_id_minter(self) -> None:
        db = FakeSessionDB()
        ids_module = types.ModuleType("hermes_state_ids")
        ids_module.new_session_id = lambda: "20261008_120000_core01"
        state_module = types.ModuleType("hermes_state")
        state_module.SessionDB = lambda _path: db
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            sys.modules,
            {"hermes_state_ids": ids_module, "hermes_state": state_module},
        ):
            profile_home = Path(tmp) / "profiles" / "coder"
            profile_home.mkdir(parents=True)
            result = LAUNCHER.create_task_session(
                profile="coder",
                task_id="t_impl",
                workspace_path=Path(tmp),
                profile_home=profile_home,
            )

        self.assertEqual(result["session_id"], "20261008_120000_core01")

    def test_launch_uses_explicit_uv_project_worker_and_devnull_without_shell(self) -> None:
        calls = []

        def fake_popen(command, **kwargs):
            calls.append((command, kwargs))
            return FakeProcess()

        with tempfile.TemporaryDirectory() as tmp:
            result = LAUNCHER.launch_profile_worker(
                plugin_root=_ROOT,
                state_path=Path(tmp) / "state.db",
                action_id="act_dispatch_1",
                action_claimant_id="runtime-1",
                action_claim_generation=3,
                lease_id="lease-1",
                invocation_id="inv-1",
                session_id="20261008_120000_abcd1234",
                profile="coder",
                skill="run-impl-card",
                board="issue151",
                task_id="t_impl",
                workspace_path=Path(tmp),
                run_id=17,
                parent_pid=99,
                uv_executable="C:/Tools/uv.exe",
                hermes_project_root=Path("C:/Hermes/hermes-agent"),
                hermes_executable="C:/Hermes/hermes.EXE",
                popen_fn=fake_popen,
            )

        command, kwargs = calls[0]
        self.assertEqual(command[0], "C:/Tools/uv.exe")
        self.assertEqual(
            command[1:7],
            [
                "run",
                "--frozen",
                "--no-sync",
                "--project",
                str(Path("C:/Hermes/hermes-agent")),
                "python",
            ],
        )
        self.assertEqual(
            command[7:9],
            ["-m", "hermes_langchain.runtime.profile_worker"],
        )
        self.assertIn(str(_ROOT / "src"), kwargs["env"]["PYTHONPATH"])
        self.assertIn("C:/Hermes/hermes.EXE", command)
        self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
        self.assertEqual(kwargs["stdout"], subprocess.DEVNULL)
        self.assertEqual(kwargs["stderr"], subprocess.DEVNULL)
        self.assertFalse(kwargs["shell"])
        self.assertEqual(result["pid"], 4321)
        self.assertNotIn("prompt", result)
        self.assertNotIn("command", result)

    def test_launch_wake_guard_uses_the_same_fenced_worker_without_a_run(self) -> None:
        calls = []
        with tempfile.TemporaryDirectory() as tmp:
            LAUNCHER.launch_profile_worker(
                plugin_root=_ROOT,
                state_path=Path(tmp) / "state.db",
                action_id="act_wake_1",
                action_claimant_id="runtime-1",
                action_claim_generation=2,
                lease_id="lease-1",
                invocation_id="inv-1",
                session_id="gateway-session",
                profile="project-manager",
                skill="run-workflow",
                board="issue151",
                task_id="t_review",
                workspace_path=Path(tmp),
                run_id=0,
                parent_pid=99,
                mode="wake",
                uv_executable="C:/Tools/uv.exe",
                hermes_project_root=Path("C:/Hermes/hermes-agent"),
                hermes_executable="C:/Hermes/hermes.EXE",
                popen_fn=lambda command, **_kwargs: calls.append(command) or FakeProcess(),
            )

        self.assertEqual(calls[0][calls[0].index("--mode") + 1], "wake")
        self.assertEqual(calls[0][calls[0].index("--run-id") + 1], "0")

    def test_profile_home_is_resolved_from_active_profile_root(self) -> None:
        active = Path("C:/Users/test/AppData/Local/hermes/profiles/main")
        resolved = LAUNCHER.resolve_profile_home("reviewer", active_home=active)
        self.assertEqual(
            resolved,
            Path("C:/Users/test/AppData/Local/hermes/profiles/reviewer"),
        )


if __name__ == "__main__":
    unittest.main()
