"""Three-Step profile-independent controller restart acceptance tests."""

from __future__ import annotations

import http.server
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tests.support import load_module

CONTROLLER = load_module("controller")
_ROOT = Path(__file__).resolve().parents[1]


class QueueLifecycle:
    def __init__(
        self,
        outputs: list[dict[str, object]],
        *,
        identity_namespace: str = "e2e",
    ) -> None:
        self._outputs = list(outputs)
        self._identity_namespace = identity_namespace
        self.requests: list[dict[str, object]] = []
        self._accepted: dict[str, tuple[object, dict[str, object]]] = {}

    def launch(self, request: dict[str, object]) -> object:
        copied = dict(request)
        self.requests.append(copied)
        if "output_schema" in copied:
            raise ValueError("Unknown request fields: output_schema.")
        if not self._outputs:
            raise AssertionError("unexpected extra Hermes launch")
        subagent_id = f"sa-{self._identity_namespace}-{len(self._accepted) + 1}"
        handle = SimpleNamespace(subagent_id=subagent_id)
        self._accepted[subagent_id] = (handle, self._outputs.pop(0))
        return handle

    def wait(self, handle: object, *, timeout_seconds: float | None = None) -> object:
        del timeout_seconds
        self._require(handle)
        return SimpleNamespace(
            state="SUCCEEDED",
            completed=True,
            timed_out=False,
            diagnostic=None,
        )

    def result(self, handle: object) -> object:
        output = self._require(handle)
        return SimpleNamespace(
            terminal_state="SUCCEEDED",
            ready=True,
            summary=json.dumps(output, ensure_ascii=False, sort_keys=True),
            structured_payload=None,
            error_classification=None,
            error_message=None,
        )

    def accepted_requests(self) -> list[dict[str, object]]:
        return [request for request in self.requests if "output_schema" not in request]

    def _require(self, handle: object) -> dict[str, object]:
        subagent_id = str(getattr(handle, "subagent_id", ""))
        stored_handle, output = self._accepted[subagent_id]
        if stored_handle is not handle:
            raise AssertionError("unexpected Hermes lifecycle handle")
        return output


class RecordingProcessRunner:
    def __init__(self, outputs: list[dict[str, object]]) -> None:
        self._outputs = list(outputs)
        self.calls: list[dict[str, object]] = []

    def __call__(self, command: list[str], **kwargs: object) -> object:
        query_path = Path(command[command.index("--query-file") + 1])
        query = query_path.read_text(encoding="utf-8")
        environment = kwargs["env"]
        prefill = Path(environment["HERMES_PREFILL_MESSAGES_FILE"]).read_text(
            encoding="utf-8"
        )
        session_id = f"session-{len(self.calls) + 1}"
        output = self._outputs.pop(0)
        self.calls.append(
            {"command": list(command), "kwargs": kwargs, "query": query, "prefill": prefill}
        )
        result = {
            "type": "result",
            "session_id": session_id,
            "exit_code": 0,
            "text": json.dumps(output, ensure_ascii=False, sort_keys=True),
        }
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(result, ensure_ascii=False) + "\n",
            stderr=f"session_id: {session_id}\n",
        )


class FixtureChatHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length).decode("utf-8"))
        rendered = json.dumps(body, ensure_ascii=False)
        outputs = {
            "extract_requirement": {"requirement": "Resume across a process boundary."},
            "write_solution": {"solution": "Persist accepted A and B artifacts."},
            "review_solution": {
                "decision": "approved",
                "rationale": "C resumed in a new process.",
            },
        }
        step_id = next(
            (candidate for candidate in outputs if candidate in rendered),
            None,
        )
        if step_id is None:
            self.send_error(400, "unknown controller step")
            return
        self.server.seen_steps.append(step_id)
        self.server.provider_requests.append(body)
        text = json.dumps(outputs[step_id], ensure_ascii=False, sort_keys=True)
        if body.get("stream"):
            self._send_stream(step_id, text)
        else:
            self._send_response(step_id, text)

    def _send_stream(self, step_id: str, text: str) -> None:
        chunks = (
            {
                "id": f"fixture-{step_id}",
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": "fixture-model",
                "choices": [
                    {
                        "index": 0,
                        "delta": {"role": "assistant", "content": text},
                        "finish_reason": None,
                    }
                ],
            },
            {
                "id": f"fixture-{step_id}",
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": "fixture-model",
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            },
        )
        payload = "".join(
            f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n" for chunk in chunks
        ) + "data: [DONE]\n\n"
        self._write(200, "text/event-stream", payload.encode("utf-8"))

    def _send_response(self, step_id: str, text: str) -> None:
        response = {
            "id": f"fixture-{step_id}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": "fixture-model",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": text},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }
        self._write(
            200,
            "application/json",
            json.dumps(response, ensure_ascii=False).encode("utf-8"),
        )

    def _write(self, status: int, content_type: str, payload: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, _format: str, *args: object) -> None:
        del args


class ControllerProfileIndependentE2ETests(unittest.TestCase):
    def test_three_steps_receive_only_declared_projections(self) -> None:
        runner = RecordingProcessRunner(
            [
                {"requirement": "Keep accepted artifacts isolated."},
                {"solution": "Hydrate only declared projections."},
                {"decision": "approved", "rationale": "The projections are bounded."},
            ]
        )

        completed = self._run_process_with_restart(runner)

        contexts = [
            json.loads(str(call["query"]).split("\n\n", 1)[1])["input"]
            for call in runner.calls
        ]
        self.assertEqual(contexts[0], {"request": "Build it"})
        self.assertEqual(
            contexts[1],
            {"requirement": "Keep accepted artifacts isolated."},
        )
        self.assertEqual(
            contexts[2],
            {
                "requirement": "Keep accepted artifacts isolated.",
                "solution": "Hydrate only declared projections.",
            },
        )
        self.assertEqual(completed["status"], "completed")

    def test_each_step_launches_a_distinct_isolated_hermes_session(self) -> None:
        runner = RecordingProcessRunner(
            [
                {"requirement": "Use fresh sessions."},
                {"solution": "Launch one bounded child per Step."},
                {"decision": "approved", "rationale": "Each child is distinct."},
            ]
        )

        completed = self._run_process_with_restart(runner)

        self.assertEqual(len(runner.calls), 3)
        self.assertEqual(
            {item["agent_id"] for item in completed["invocations"]},
            {"session-1", "session-2", "session-3"},
        )
        for call in runner.calls:
            command = call["command"]
            self.assertEqual(
                command[command.index("--toolsets") + 1],
                "vision",
            )
            envelope = json.loads(str(call["query"]).split("\n\n", 1)[1])
            self.assertEqual(
                set(envelope),
                {
                    "protocol",
                    "run_id",
                    "invocation_id",
                    "step_id",
                    "input",
                    "acceptance_criteria",
                    "output_schema",
                    "skills",
                    "final_response_contract",
                },
            )


    def test_restart_after_b_resumes_at_c_without_duplicates(self) -> None:
        hermes_executable = shutil.which("hermes")
        self.assertIsNotNone(hermes_executable, "Hermes executable is required")
        hermes_source_root = Path(str(hermes_executable)).resolve().parents[2]
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FixtureChatHandler)
        server.seen_steps = []
        server.provider_requests = []
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            workspace.mkdir()
            isolated_home = Path(tmp) / "isolated-homes"
            checkpoint_path = Path(tmp) / "controller.db"
            artifact_store_path = Path(tmp) / "artifacts.db"
            worker = _ROOT / "tests" / "controller_e2e_process_worker.py"
            environment = os.environ.copy()
            environment["PYTHONPATH"] = str(_ROOT / "src")
            hostile_prefill = Path(tmp) / "hostile-prefill.json"
            hostile_prefill.write_text(
                '[{"role":"system","content":"HOSTILE_PREFILL"}]',
                encoding="utf-8",
            )
            environment.update(
                {
                    "HERMES_EPHEMERAL_SYSTEM_PROMPT": "HOSTILE_PROFILE_PROMPT",
                    "HERMES_PREFILL_MESSAGES_FILE": str(hostile_prefill),
                    "HERMES_HOME": "HOSTILE_HERMES_HOME",
                    "HOME": "HOSTILE_HOME",
                    "APPDATA": "HOSTILE_APPDATA",
                    "LOCALAPPDATA": "HOSTILE_LOCALAPPDATA",
                    "USERPROFILE": "HOSTILE_USERPROFILE",
                }
            )
            base_url = f"http://127.0.0.1:{server.server_port}/v1"
            try:
                common = [
                    os.fspath(Path(os.sys.executable)),
                    os.fspath(worker),
                ]
                first = subprocess.run(
                    common
                    + [
                        "before",
                        os.fspath(checkpoint_path),
                        os.fspath(artifact_store_path),
                        str(hermes_executable),
                        os.fspath(hermes_source_root),
                        os.fspath(workspace),
                        os.fspath(isolated_home),
                        base_url,
                    ],
                    cwd=_ROOT,
                    env=environment,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(first.returncode, 0, first.stderr)
                second = subprocess.run(
                    common
                    + [
                        "after",
                        os.fspath(checkpoint_path),
                        os.fspath(artifact_store_path),
                        str(hermes_executable),
                        os.fspath(hermes_source_root),
                        os.fspath(workspace),
                        os.fspath(isolated_home),
                        base_url,
                    ],
                    cwd=_ROOT,
                    env=environment,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(second.returncode, 0, second.stderr)
                after_b = json.loads(first.stdout)
                completed = json.loads(second.stdout)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

            connection = sqlite3.connect(artifact_store_path)
            try:
                rows = connection.execute(
                    "SELECT step_id, COUNT(*) FROM controller_artifacts GROUP BY step_id ORDER BY step_id"
                ).fetchall()
            finally:
                connection.close()

        self.assertEqual(
            after_b["next_step"], "review_solution", f"{after_b}\n{first.stderr}"
        )
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(
            [item["step_id"] for item in completed["invocations"]],
            ["extract_requirement", "write_solution", "review_solution"],
        )
        self.assertEqual(
            len({item["agent_id"] for item in completed["invocations"]}),
            3,
        )
        self.assertEqual(
            list(dict.fromkeys(server.seen_steps)),
            ["extract_requirement", "write_solution", "review_solution"],
        )
        requests = [json.dumps(item, ensure_ascii=False) for item in server.provider_requests]
        self.assertGreaterEqual(len(requests), 3)
        for request in requests:
            self.assertNotIn("HOSTILE_", request)
            self.assertNotIn("must not leak", request)
        extract_requests = [item for item in requests if "extract_requirement" in item]
        write_requests = [item for item in requests if "write_solution" in item]
        review_requests = [item for item in requests if "review_solution" in item]
        self.assertTrue(extract_requests and write_requests and review_requests)
        self.assertTrue(all("Persist accepted A and B artifacts." not in item for item in extract_requests))
        self.assertTrue(all("C resumed in a new process." not in item for item in write_requests))
        self.assertTrue(
            all(
                "Resume across a process boundary." in item
                and "Persist accepted A and B artifacts." in item
                for item in review_requests
            )
        )
        self.assertEqual(
            rows,
            [
                ("extract_requirement", 1),
                ("review_solution", 1),
                ("write_solution", 1),
            ],
        )

    def test_invalid_typed_c_output_blocks_without_artifact(self) -> None:
        lifecycle = QueueLifecycle(
            [
                {"requirement": "Validate typed output."},
                {"solution": "Reject invalid decisions."},
                {"decision": "maybe", "rationale": "This is outside the enum."},
            ]
        )

        with tempfile.TemporaryDirectory() as tmp:
            artifact_store_path = Path(tmp) / "artifacts.db"
            runtime = CONTROLLER.ControllerRuntime.open(
                workflow=CONTROLLER.profile_independent_e2e_workflow(),
                executor=CONTROLLER.HermesSubagentExecutor(lifecycle),
                checkpoint_path=Path(tmp) / "controller.db",
                artifact_store_path=artifact_store_path,
            )
            try:
                runtime.start(run_id="run-invalid-c", initial_input={"request": "Build it"})
                runtime.resume(run_id="run-invalid-c")
                blocked = runtime.resume(run_id="run-invalid-c")
            finally:
                runtime.close()

            connection = sqlite3.connect(artifact_store_path)
            try:
                stored_steps = [
                    row[0]
                    for row in connection.execute(
                        "SELECT step_id FROM controller_artifacts ORDER BY step_id"
                    ).fetchall()
                ]
            finally:
                connection.close()

        self.assertEqual(blocked["status"], "blocked")
        self.assertEqual(blocked["error"], "invalid output for step: review_solution")
        self.assertEqual(stored_steps, ["extract_requirement", "write_solution"])

    def test_process_executor_launches_only_isolated_declared_context(self) -> None:
        runner = RecordingProcessRunner([{"requirement": "bounded"}])
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            workspace.mkdir()
            hermes_root = Path(tmp) / "hermes-agent"
            hermes_executable = hermes_root / "venv" / "Scripts" / "hermes.exe"
            hermes_executable.parent.mkdir(parents=True)
            hermes_executable.write_text("fixture", encoding="utf-8")
            (hermes_root / "hermes_cli").mkdir()
            (hermes_root / "cli.py").write_text("", encoding="utf-8")
            (hermes_root / ".env").write_text(
                "OPENAI_BASE_URL=HOSTILE_SOURCE_URL\n"
                "ANTHROPIC_API_KEY=HOSTILE_SOURCE_KEY\n"
                "'HERMES_ENABLE_PROJECT_PLUGINS'=1\n",
                encoding="utf-8",
            )
            executor = CONTROLLER.HermesProcessExecutor(
                hermes_executable=os.fspath(hermes_executable),
                hermes_source_root=hermes_root,
                model="fixture-model",
                provider="openai",
                workspace=workspace,
                scratch_dir=Path(tmp) / "scratch",
                isolated_home=Path(tmp) / "isolated-home",
                runtime_environment={
                    "OPENAI_API_KEY": "fixture-key",
                    "OPENAI_BASE_URL": "http://127.0.0.1:1/v1",
                },
                run_fn=runner,
            )
            self.assertEqual(workspace.resolve(), executor.workspace)
            with patch.dict(
                os.environ,
                {
                    "HERMES_EPHEMERAL_SYSTEM_PROMPT": "HOSTILE_PROMPT",
                    "HERMES_TOOLSETS": "terminal",
                    "HERMES_DISABLED_TOOLSETS": "none",
                    "HERMES_SESSION_KEY": "parent-session",
                    "HERMES_HOME": "HOSTILE_HERMES_HOME",
                    "HOME": "HOSTILE_HOME",
                    "APPDATA": "HOSTILE_APPDATA",
                    "LOCALAPPDATA": "HOSTILE_LOCALAPPDATA",
                    "USERPROFILE": "HOSTILE_USERPROFILE",
                    "PYTHONPATH": "HOSTILE_PYTHONPATH",
                },
            ):
                result = executor.invoke(
                    CONTROLLER.StepInvocation(
                        run_id="run-isolation",
                        invocation_id="run-isolation:extract:1",
                        step_id="extract",
                        goal="bounded goal",
                        allowed_toolsets=("vision",),
                        acceptance_criteria=("bounded output",),
                        output_schema={"type": "object"},
                        input={"request": "bounded context"},
                    )
                )

        self.assertEqual(result.agent_id, "session-1")
        self.assertEqual(result.output, {"requirement": "bounded"})
        call = runner.calls[0]
        command = call["command"]
        self.assertEqual(command[:2], [os.fspath(hermes_executable), "chat"])
        self.assertNotIn("--profile", command)
        self.assertIn("--oneshot", command)
        self.assertIn("--quiet", command)
        self.assertIn("--ignore-rules", command)
        self.assertIn("--ignore-user-config", command)
        self.assertNotIn("--pass-session-id", command)
        self.assertIn("--format", command)
        self.assertEqual(command[command.index("--format") + 1], "stream-json")
        self.assertEqual(
            command[command.index("--toolsets") + 1],
            "vision",
        )
        self.assertNotIn("--skills", command)
        query = str(call["query"])
        self.assertIn("bounded goal", query)
        self.assertIn('"request":"bounded context"', query)
        environment = call["kwargs"]["env"]
        self.assertEqual(call["prefill"], "[]\n")
        self.assertEqual(environment["OPENAI_API_KEY"], "fixture-key")
        self.assertEqual(environment["OPENAI_BASE_URL"], "http://127.0.0.1:1/v1")
        self.assertEqual(environment["ANTHROPIC_API_KEY"], "")
        self.assertEqual(environment["HERMES_ENABLE_PROJECT_PLUGINS"], "")
        for name in ("HERMES_HOME", "HOME", "APPDATA", "LOCALAPPDATA", "USERPROFILE"):
            self.assertNotIn("HOSTILE", environment[name])
        for name in (
            "HERMES_SESSION_KEY",
            "HERMES_SESSION_ID",
            "HERMES_UI_SESSION_ID",
            "HERMES_SESSION_PROFILE",
            "HERMES_EPHEMERAL_SYSTEM_PROMPT",
            "HERMES_TOOLSETS",
            "HERMES_DISABLED_TOOLSETS",
            "PYTHONPATH",
        ):
            self.assertNotIn(name, environment)

    def test_process_executor_rejects_process_control_environment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            workspace.mkdir()
            hermes_root = Path(tmp) / "hermes-agent"
            (hermes_root / "hermes_cli").mkdir(parents=True)
            (hermes_root / "cli.py").write_text("", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "forbidden key"):
                CONTROLLER.HermesProcessExecutor(
                    hermes_executable="C:/Hermes/hermes.exe",
                    hermes_source_root=hermes_root,
                    model="fixture-model",
                    provider="openai",
                    workspace=workspace,
                    scratch_dir=Path(tmp) / "scratch",
                    isolated_home=Path(tmp) / "isolated-home",
                    runtime_environment={"PYTHONPATH": "HOSTILE_CODE_PATH"},
                )

    def test_process_executor_uses_fresh_external_home_per_invocation(self) -> None:
        runner = RecordingProcessRunner([{"ok": 1}, {"ok": 2}])
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            hermes_root = root / "hermes-agent"
            (hermes_root / "hermes_cli").mkdir(parents=True)
            (hermes_root / "cli.py").write_text("", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "outside the workspace"):
                CONTROLLER.HermesProcessExecutor(
                    hermes_executable="C:/Hermes/hermes.exe",
                    hermes_source_root=hermes_root,
                    model="fixture-model",
                    provider="openai",
                    workspace=workspace,
                    scratch_dir=root / "scratch",
                    isolated_home=workspace / "isolated-home",
                )
            executor = CONTROLLER.HermesProcessExecutor(
                hermes_executable="C:/Hermes/hermes.exe",
                hermes_source_root=hermes_root,
                model="fixture-model",
                provider="openai",
                workspace=workspace,
                scratch_dir=root / "scratch",
                isolated_home=root / "isolated-homes",
                run_fn=runner,
            )

            for attempt in (1, 2):
                executor.invoke(
                    CONTROLLER.StepInvocation(
                        run_id="fresh-home",
                        invocation_id=f"fresh-home:step:{attempt}",
                        step_id="step",
                        goal="bounded goal",
                        allowed_toolsets=("vision",),
                        acceptance_criteria=("bounded output",),
                        output_schema={"type": "object"},
                        input={"attempt": attempt},
                    )
                )

            invocation_homes = [
                Path(call["kwargs"]["env"]["HERMES_HOME"]).parent
                for call in runner.calls
            ]
            self.assertEqual(2, len(set(invocation_homes)))
            self.assertTrue(all(not home.exists() for home in invocation_homes))

            cleanup_runner = RecordingProcessRunner([{"ok": 3}])
            cleanup_executor = CONTROLLER.HermesProcessExecutor(
                hermes_executable="C:/Hermes/hermes.exe",
                hermes_source_root=hermes_root,
                model="fixture-model",
                provider="openai",
                workspace=workspace,
                scratch_dir=root / "cleanup-scratch",
                isolated_home=root / "cleanup-homes",
                run_fn=cleanup_runner,
            )
            with patch(
                "hermes_langchain.controller.process_executor.shutil.rmtree",
                return_value=None,
            ):
                with self.assertRaisesRegex(RuntimeError, "cleanup"):
                    cleanup_executor.invoke(
                        CONTROLLER.StepInvocation(
                            run_id="cleanup-failure",
                            invocation_id="cleanup-failure:step:1",
                            step_id="step",
                            goal="bounded goal",
                            allowed_toolsets=("vision",),
                            acceptance_criteria=("bounded output",),
                            output_schema={"type": "object"},
                            input={"attempt": 1},
                        )
                    )

            unlink_runner = RecordingProcessRunner([{"ok": 4}])
            unlink_executor = CONTROLLER.HermesProcessExecutor(
                hermes_executable="C:/Hermes/hermes.exe",
                hermes_source_root=hermes_root,
                model="fixture-model",
                provider="openai",
                workspace=workspace,
                scratch_dir=root / "unlink-scratch",
                isolated_home=root / "unlink-homes",
                run_fn=unlink_runner,
            )
            with patch.object(Path, "unlink", side_effect=OSError("blocked unlink")):
                with self.assertRaisesRegex(RuntimeError, "query-file cleanup"):
                    unlink_executor.invoke(
                        CONTROLLER.StepInvocation(
                            run_id="unlink-failure",
                            invocation_id="unlink-failure:step:1",
                            step_id="step",
                            goal="bounded goal",
                            allowed_toolsets=("vision",),
                            acceptance_criteria=("bounded output",),
                            output_schema={"type": "object"},
                            input={"attempt": 1},
                        )
                    )
            unlink_home = Path(
                unlink_runner.calls[0]["kwargs"]["env"]["HERMES_HOME"]
            ).parent
            self.assertFalse(unlink_home.exists())

    def test_process_executor_tool_free_mode_sends_no_tools(self) -> None:
        hermes_executable = shutil.which("hermes")
        self.assertIsNotNone(hermes_executable, "Hermes executable is required")
        hermes_source_root = Path(str(hermes_executable)).resolve().parents[2]
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FixtureChatHandler)
        server.seen_steps = []
        server.provider_requests = []
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            executor = CONTROLLER.HermesProcessExecutor(
                hermes_executable=str(hermes_executable),
                hermes_source_root=hermes_source_root,
                model="fixture-model",
                provider="openai",
                workspace=workspace,
                scratch_dir=root / "scratch",
                isolated_home=root / "isolated-homes",
                runtime_environment={
                    "OPENAI_API_KEY": "fixture-key",
                    "OPENAI_BASE_URL": f"http://127.0.0.1:{server.server_port}/v1",
                },
            )
            try:
                result = executor.invoke(
                    CONTROLLER.StepInvocation(
                        run_id="tool-free",
                        invocation_id="tool-free:extract_requirement:1",
                        step_id="extract_requirement",
                        goal="Return the bounded requirement.",
                        allowed_toolsets=(),
                        acceptance_criteria=("bounded output",),
                        output_schema={"type": "object"},
                        input={"request": "bounded"},
                    )
                )
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

        self.assertEqual({"requirement": "Resume across a process boundary."}, result.output)
        self.assertGreaterEqual(len(server.provider_requests), 1)
        self.assertTrue(
            all(not request.get("tools") for request in server.provider_requests)
        )

    def test_process_executor_removes_partial_query_on_write_failure(self) -> None:
        real_named_temporary_file = tempfile.NamedTemporaryFile
        created_paths: list[Path] = []

        class FailingQueryFile:
            def __init__(self, **kwargs: object) -> None:
                self._stream = real_named_temporary_file(**kwargs)
                self.name = self._stream.name
                created_paths.append(Path(self.name))

            def __enter__(self) -> "FailingQueryFile":
                self._stream.__enter__()
                return self

            def __exit__(self, *args: object) -> object:
                return self._stream.__exit__(*args)

            def write(self, value: str) -> None:
                self._stream.write(value[:16])
                self._stream.flush()
                raise OSError("simulated query write failure")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            hermes_root = root / "hermes-agent"
            (hermes_root / "hermes_cli").mkdir(parents=True)
            (hermes_root / "cli.py").write_text("", encoding="utf-8")
            executor = CONTROLLER.HermesProcessExecutor(
                hermes_executable="C:/Hermes/hermes.exe",
                hermes_source_root=hermes_root,
                model="fixture-model",
                provider="openai",
                workspace=workspace,
                scratch_dir=root / "scratch",
                isolated_home=root / "isolated-homes",
            )
            with patch(
                "hermes_langchain.controller.process_executor.tempfile.NamedTemporaryFile",
                side_effect=lambda **kwargs: FailingQueryFile(**kwargs),
            ):
                with self.assertRaisesRegex(OSError, "query write failure"):
                    executor._write_query("bounded but private workflow context")

            self.assertEqual(1, len(created_paths))
            self.assertFalse(created_paths[0].exists())

    @staticmethod
    def _run_process_with_restart(runner: RecordingProcessRunner) -> dict[str, object]:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            hermes_root = root / "hermes-agent"
            hermes_executable = hermes_root / "venv" / "Scripts" / "hermes.exe"
            hermes_executable.parent.mkdir(parents=True)
            hermes_executable.write_text("fixture", encoding="utf-8")
            (hermes_root / "hermes_cli").mkdir()
            (hermes_root / "cli.py").write_text("", encoding="utf-8")
            checkpoint_path = root / "controller.db"
            artifact_store_path = root / "artifacts.db"
            workflow = CONTROLLER.profile_independent_e2e_workflow()

            def executor() -> object:
                return CONTROLLER.HermesProcessExecutor(
                    hermes_executable=os.fspath(hermes_executable),
                    hermes_source_root=hermes_root,
                    model="fixture-model",
                    provider="openai",
                    workspace=workspace,
                    scratch_dir=root / "scratch",
                    isolated_home=root / "isolated-homes",
                    runtime_environment={
                        "OPENAI_API_KEY": "fixture-key",
                        "OPENAI_BASE_URL": "http://127.0.0.1:1/v1",
                    },
                    run_fn=runner,
                )

            runtime = CONTROLLER.ControllerRuntime.open(
                workflow=workflow,
                executor=executor(),
                checkpoint_path=checkpoint_path,
                artifact_store_path=artifact_store_path,
            )
            try:
                runtime.start(
                    run_id="run-profile-independent",
                    initial_input={"request": "Build it", "unrelated": "must not leak"},
                )
                runtime.resume(run_id="run-profile-independent")
            finally:
                runtime.close()

            reconstructed = CONTROLLER.ControllerRuntime.open(
                workflow=workflow,
                executor=executor(),
                checkpoint_path=checkpoint_path,
                artifact_store_path=artifact_store_path,
            )
            try:
                return reconstructed.resume(run_id="run-profile-independent")
            finally:
                reconstructed.close()


if __name__ == "__main__":
    unittest.main()