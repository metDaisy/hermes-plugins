"""Tests for KWC adapters that materialize PM-authored cards."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path


_ROOT = Path(__file__).resolve().parents[2]
from tests.support import load_module

EXECUTOR = load_module("authoring.authoring_executor")


class AuthoringExecutorTests(unittest.TestCase):
    def test_helper_failure_surfaces_structured_errors(self) -> None:
        command = ["python", "helper.py"]

        def fail(_command, **_kwargs):
            raise subprocess.CalledProcessError(
                2,
                command,
                output=json.dumps(
                    {
                        "valid": False,
                        "errors": ["IMPL_SLICE_CLOSURE_MISMATCH:missing=S-1"],
                    }
                ),
                stderr="ignored private stderr",
            )

        with self.assertRaisesRegex(
            ValueError, "IMPL_SLICE_CLOSURE_MISMATCH:missing=S-1"
        ):
            EXECUTOR._run(command, cwd=_ROOT, run_fn=fail)

    def test_kwc_owned_graph_helper_is_self_contained(self) -> None:
        graph_script = _ROOT / "src" / "hermes_langchain" / "authoring" / "graph" / "build_task_graph.py"

        result = subprocess.run(
            ["python", str(graph_script), "--help"],
            cwd=str(_ROOT),
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("validate-impl-batch", result.stdout)
        self.assertNotIn("skills/build-task-graph", graph_script.read_text(encoding="utf-8"))

    def test_validate_review_graph_rejects_fast_path_bundle_explicitly(self) -> None:
        graph_script = _ROOT / "src" / "hermes_langchain" / "authoring" / "graph" / "build_task_graph.py"
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "review-source.json"
            source.write_text(
                json.dumps({"schema": "review-fast-path-v1"}),
                encoding="utf-8",
            )

            result = subprocess.run(
                ["python", str(graph_script), "validate-review-graph", str(source)],
                cwd=str(_ROOT),
                capture_output=True,
                text=True,
                check=False,
            )

        self.assertEqual(1, result.returncode)
        payload = json.loads(result.stdout)
        self.assertEqual(["REVIEW_GRAPH_WRAPPER_REQUIRED"], payload["errors"])

    def assignment(self, workspace: Path, phase: str) -> dict:
        return {
            "schema": "kwc-assignment-v1",
            "assignment_id": f"kwa_{phase}",
            "board": "issue-151-authoring",
            "issue_number": 151,
            "generation": 1,
            "workspace_path": str(workspace),
            "phase": phase,
            "role": "orchestrator",
            "sources": {
                "triage_id": "t_123abc" if phase != "triage" else None,
                "impl_ids": ["t_111aaa", "t_222bbb"] if phase in {"review", "summary"} else [],
                "review_id": "t_333ccc" if phase == "summary" else None,
                "summary_id": None,
            },
        }

    def test_executes_each_phase_through_kwc_owned_helpers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            profile_home = workspace / "pm-home"
            graph_script = _ROOT / "src" / "hermes_langchain" / "authoring" / "graph" / "build_task_graph.py"
            authoring = workspace / ".temp/triage-authoring.json"
            inspection = workspace / ".temp/inspection.json"
            batch = workspace / ".temp/batch.json"
            review_body = workspace / ".temp/review.json"
            for path in (authoring, inspection, batch, review_body):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("{}\n", encoding="utf-8")
            review_body.write_text('{"rows":[1,2]}\n', encoding="utf-8")
            commands = []

            def run(command, **_kwargs):
                commands.append(command)
                output_flag = command.index("--output") if "--output" in command else None
                if output_flag is not None:
                    output = Path(command[output_flag + 1])
                    if not output.is_absolute():
                        output = Path(_kwargs["cwd"]) / output
                    output.parent.mkdir(parents=True, exist_ok=True)
                    if "create-triage-native" in command:
                        payload = {"valid": True, "verified": True, "task_id": "t_123abc"}
                    elif "create-impl-batch-native" in command:
                        payload = {"created": [{"id": "t_111aaa"}, {"id": "t_222bbb"}]}
                    elif "create-review-native" in command:
                        payload = {"valid": True, "created": {"id": "t_333ccc"}}
                    elif "regenerate-summary-native" in command:
                        payload = {"valid": True, "task_id": "t_444ddd"}
                    else:
                        payload = {"valid": True}
                    output.write_text(json.dumps(payload), encoding="utf-8")
                if "--bundle-output" in command:
                    path = Path(command[command.index("--bundle-output") + 1])
                    if not path.is_absolute():
                        path = Path(_kwargs["cwd"]) / path
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text("{}", encoding="utf-8")
                if "--body-output" in command:
                    path = Path(command[command.index("--body-output") + 1])
                    if not path.is_absolute():
                        path = Path(_kwargs["cwd"]) / path
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text("{}", encoding="utf-8")
                return subprocess.CompletedProcess(command, 0, stdout="{}", stderr="")

            triage = EXECUTOR.execute_authoring_phase(
                assignment=self.assignment(workspace, "triage"),
                arguments={
                    "authoring_file": ".temp/triage-authoring.json",
                    "current_state_inspection_file": ".temp/inspection.json",
                },
                host_profile=profile_home,
                run_fn=run,
                triage_execute_fn=lambda **_kwargs: {
                    "valid": True,
                    "verified": True,
                    "task_id": "t_123abc",
                },
            )
            impl = EXECUTOR.execute_authoring_phase(
                assignment=self.assignment(workspace, "impl"),
                arguments={"batch_file": ".temp/batch.json"},
                host_profile=profile_home,
                run_fn=run,
            )
            review = EXECUTOR.execute_authoring_phase(
                assignment=self.assignment(workspace, "review"),
                arguments={"body_file": ".temp/review.json"},
                host_profile=profile_home,
                run_fn=run,
            )
            summary = EXECUTOR.execute_authoring_phase(
                assignment=self.assignment(workspace, "summary"),
                arguments={},
                host_profile=profile_home,
                run_fn=run,
            )

            self.assertEqual(["t_123abc"], triage["created_task_ids"])
            self.assertEqual(["t_111aaa", "t_222bbb"], impl["created_task_ids"])
            self.assertEqual(["t_333ccc"], review["created_task_ids"])
            self.assertEqual(["t_444ddd"], summary["created_task_ids"])
            self.assertEqual(
                '{\n  "rows": [\n    1,\n    2\n  ]\n}\n',
                review_body.read_text(encoding="utf-8"),
            )
            self.assertTrue(any("render-impl-batch" in command for command in commands))
            self.assertTrue(any("prepare-review-fast-path" in command for command in commands))
            render = next(command for command in commands if "render-impl-batch" in command)
            create = next(command for command in commands if "create-impl-batch-native" in command)
            render_dir = render[render.index("--output-dir") + 1]
            self.assertTrue(
                render_dir.startswith(
                    ".temp/task-graphs/151/generation-1/issue-151-authoring-"
                )
            )
            self.assertTrue(render_dir.endswith("/impl-bodies"))
            self.assertEqual(render_dir, create[create.index("--body-dir") + 1])
            graph_commands = [command for command in commands if str(graph_script) in command]
            self.assertTrue(graph_commands)
            for command in graph_commands:
                for flag in ("--output", "--bundle-output", "--body-output", "--output-dir", "--body-dir"):
                    if flag in command:
                        self.assertFalse(Path(command[command.index(flag) + 1]).is_absolute(), (flag, command))

    def test_triage_phase_is_owned_by_kwc_without_pm_helper(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            authoring = workspace / ".temp/triage-authoring.json"
            inspection = workspace / ".temp/inspection.json"
            authoring.parent.mkdir(parents=True)
            authoring.write_text("{}\n", encoding="utf-8")
            inspection.write_text("{}\n", encoding="utf-8")
            calls = []

            def execute_triage(**kwargs):
                calls.append(kwargs)
                return {"valid": True, "verified": True, "task_id": "t_123abc"}

            result = EXECUTOR.execute_authoring_phase(
                assignment=self.assignment(workspace, "triage"),
                arguments={
                    "authoring_file": ".temp/triage-authoring.json",
                    "current_state_inspection_file": ".temp/inspection.json",
                },
                host_profile=workspace / "pm-home-without-triage-script",
                run_fn=lambda *_args, **_kwargs: self.fail("PM helper must not run"),
                triage_execute_fn=execute_triage,
            )

            self.assertEqual(["t_123abc"], result["created_task_ids"])
            self.assertEqual(authoring, calls[0]["authoring_file"])
            self.assertEqual(inspection, calls[0]["current_state_inspection_file"])

    def test_impl_execution_on_different_boards_uses_distinct_run_directories(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            batch = workspace / ".temp/batch.json"
            batch.parent.mkdir(parents=True)
            batch.write_text("{}\n", encoding="utf-8")
            commands = []

            def run(command, **kwargs):
                commands.append(command)
                if "--output" in command:
                    output = Path(command[command.index("--output") + 1])
                    if not output.is_absolute():
                        output = Path(kwargs["cwd"]) / output
                    output.parent.mkdir(parents=True, exist_ok=True)
                    output.write_text(
                        json.dumps({"created": [{"id": "t_111aaa"}]}),
                        encoding="utf-8",
                    )
                return subprocess.CompletedProcess(command, 0, stdout="{}", stderr="")

            for board in ("issue-151-board-a", "issue-151-board-b"):
                assignment = self.assignment(workspace, "impl")
                assignment["assignment_id"] = f"kwa_{board}"
                assignment["board"] = board
                EXECUTOR.execute_authoring_phase(
                    assignment=assignment,
                    arguments={"batch_file": ".temp/batch.json"},
                    host_profile=workspace / "pm-home",
                    run_fn=run,
                )

            render_commands = [
                command for command in commands if "render-impl-batch" in command
            ]
            output_dirs = {
                command[command.index("--output-dir") + 1] for command in render_commands
            }
            self.assertEqual(2, len(output_dirs))
            self.assertTrue(all(path.endswith("/impl-bodies") for path in output_dirs))

    def test_rejects_artifact_outside_assignment_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "repo"
            workspace.mkdir()
            profile_home = Path(tmp) / "pm-home"
            outside = Path(tmp) / "outside.json"
            outside.write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "workspace"):
                EXECUTOR.execute_authoring_phase(
                    assignment=self.assignment(workspace, "impl"),
                    arguments={"batch_file": str(outside)},
                    host_profile=profile_home,
                    run_fn=lambda *_args, **_kwargs: None,
                )


if __name__ == "__main__":
    unittest.main()
