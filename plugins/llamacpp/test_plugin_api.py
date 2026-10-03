"""Regression tests for alternate llama.cpp backends and parameter presets."""
from __future__ import annotations

import importlib.util
import asyncio
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch


MODULE_PATH = Path(__file__).parent / "dashboard" / "backend_impl.py"
SPEC = importlib.util.spec_from_file_location("llamacpp_backend_impl", MODULE_PATH)
assert SPEC and SPEC.loader
api = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(api)

PROXY_PATH = Path(__file__).parent / "dashboard" / "plugin_api.py"
PROXY_SPEC = importlib.util.spec_from_file_location("llamacpp_profile_proxy", PROXY_PATH)
assert PROXY_SPEC and PROXY_SPEC.loader
profile_proxy = importlib.util.module_from_spec(PROXY_SPEC)
PROXY_SPEC.loader.exec_module(profile_proxy)


class LlamaCppManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        from dashboard import inference_proxy

        inference_proxy._transition_lock = asyncio.Lock()
        inference_proxy._counter_lock = asyncio.Lock()
        inference_proxy._active_main_requests = 0
        inference_proxy._queued_main_requests = 0

    def test_plugin_middleware_attaches_profile_and_session_only_to_local_requests(self) -> None:
        entrypoint = Path(__file__).parent / "__init__.py"
        spec = importlib.util.spec_from_file_location("llamacpp_plugin_entrypoint", entrypoint)
        assert spec and spec.loader
        plugin = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(plugin)

        with patch.dict(os.environ, {
            "HERMES_HOME": "C:/Users/leee/AppData/Local/hermes/profiles/project-manager",
        }):
            result = plugin._attach_request_context(
                request={"messages": []},
                provider="custom",
                base_url="http://127.0.0.1:18380/v1",
                model="main-local",
                session_id="20261003_164117_5bf794",
            )
            remote = plugin._attach_request_context(
                request={"messages": []},
                provider="openai-codex",
                base_url="https://chatgpt.com/backend-api/codex",
                model="gpt-5.6-luna",
                session_id="20261003_164117_5bf794",
            )

        self.assertEqual(result["request"]["extra_headers"], {
            "X-Hermes-Profile": "project-manager",
            "X-Hermes-Session": "20261003_164117_5bf794",
        })
        self.assertIsNone(remote)

    def test_plugin_manifest_versions_match(self) -> None:
        root = Path(__file__).parent
        plugin_version = next(line.split(":", 1)[1].strip() for line in (root / "plugin.yaml").read_text(encoding="utf-8").splitlines() if line.startswith("version:"))
        pack_version = next(line.split(":", 1)[1].strip() for line in (root / "pack.yml").read_text(encoding="utf-8").splitlines() if line.startswith("version:"))
        self.assertEqual(plugin_version, pack_version)

    def test_manifest_does_not_claim_unregistered_agent_tools(self) -> None:
        root = Path(__file__).parent
        manifest = (root / "plugin.yaml").read_text(encoding="utf-8")
        registration = (root / "__init__.py").read_text(encoding="utf-8")
        if "register_tool" not in registration:
            self.assertNotIn("provides_tools:", manifest)

    def test_manifest_declares_registered_auxiliary_hook(self) -> None:
        root = Path(__file__).parent
        manifest = (root / "plugin.yaml").read_text(encoding="utf-8")
        normalized = manifest.replace(chr(13) + chr(10), chr(10))
        self.assertIn("provides_hooks:\n  - pre_auxiliary_call", normalized)

    def test_hf_cache_removal_uses_typed_repository_id(self) -> None:
        completed = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        with patch.object(api.shutil, "which", return_value="hf"), \
                patch.object(api.subprocess, "run", return_value=completed) as run:
            removed = api._remove_hf_cache("Jackrong/Qwopus3.8-27B-Flash-GGUF")

        self.assertTrue(removed)
        self.assertEqual(
            run.call_args.args[0],
            [
                "hf", "cache", "rm",
                "model/Jackrong/Qwopus3.8-27B-Flash-GGUF",
                "--yes", "--format", "quiet",
            ],
        )

    def test_coordinator_start_lease_prevents_duplicate_spawn(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            lock_path = root / "backend-start.lock"
            spawned: list[list[str]] = []
            log_paths: list[str] = []

            def spawn(command, **kwargs):
                spawned.append(list(command))
                log_paths.append(str(getattr(kwargs.get("stdout"), "name", "")))
                return object()

            with patch.object(profile_proxy, "RUNTIME_ROOT", root), \
                    patch.object(profile_proxy, "START_LOCK", lock_path), \
                    patch.object(profile_proxy, "_healthy", return_value=False), \
                    patch.object(profile_proxy.subprocess, "Popen", side_effect=spawn):
                profile_proxy._try_start_coordinator()
                profile_proxy._try_start_coordinator()

            self.assertEqual(len(spawned), 1)
            self.assertTrue(lock_path.exists())
            self.assertTrue(log_paths[0].endswith("activity.log"))

    def test_stale_coordinator_start_lease_is_reacquired_in_same_request(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            lock_path = root / "backend-start.lock"
            lock_path.write_text("stale", encoding="utf-8")
            old = time.time() - 60
            os.utime(lock_path, (old, old))
            spawned: list[list[str]] = []

            with patch.object(profile_proxy, "RUNTIME_ROOT", root), \
                    patch.object(profile_proxy, "START_LOCK", lock_path), \
                    patch.object(profile_proxy, "_healthy", return_value=False), \
                    patch.object(profile_proxy.subprocess, "Popen", side_effect=lambda command, **_: spawned.append(list(command))):
                self.assertTrue(profile_proxy._try_start_coordinator())

            self.assertEqual(len(spawned), 1)

    def test_coordinator_health_accepts_compatible_protocol_across_build_updates(self) -> None:
        compatible = {
            "ok": True,
            "service": profile_proxy.COORDINATOR_SERVICE,
            "protocol": profile_proxy.COORDINATOR_PROTOCOL,
            "build": "newer-compatible-build",
            "pid": 42,
        }
        with patch.object(profile_proxy, "_health_payload", return_value=compatible), \
                patch.object(profile_proxy, "_coordinator_process_ok", return_value=True):
            self.assertTrue(profile_proxy._healthy())

    def test_coordinator_health_rejects_incompatible_protocol_or_identity(self) -> None:
        incompatible = {
            "ok": True,
            "service": profile_proxy.COORDINATOR_SERVICE,
            "protocol": profile_proxy.COORDINATOR_PROTOCOL + 1,
            "build": profile_proxy.COORDINATOR_BUILD,
            "pid": 42,
        }
        with patch.object(profile_proxy, "_health_payload", return_value=incompatible), \
                patch.object(profile_proxy, "_coordinator_process_ok", return_value=True):
            self.assertFalse(profile_proxy._healthy())
        with patch.object(profile_proxy, "_health_payload", return_value={"ok": True, "pid": 123}):
            self.assertFalse(profile_proxy._healthy())

    def test_coordinator_health_rejects_static_identity_from_untrusted_listener(self) -> None:
        import types

        class Process:
            def __init__(self, pid: int) -> None:
                self.pid = pid

            def exe(self) -> str:
                return "C:/untrusted/evil.exe"

            def cmdline(self):
                return ["C:/untrusted/evil.exe", str(profile_proxy.COORDINATOR_SCRIPT)]

            def net_connections(self, kind: str):
                return [types.SimpleNamespace(
                    status="LISTEN", laddr=types.SimpleNamespace(port=profile_proxy.COORDINATOR_PORT),
                )]

        payload = {
            "ok": True,
            "service": profile_proxy.COORDINATOR_SERVICE,
            "protocol": profile_proxy.COORDINATOR_PROTOCOL,
            "build": profile_proxy.COORDINATOR_BUILD,
            "pid": 42,
        }
        fake_psutil = types.SimpleNamespace(Process=Process, CONN_LISTEN="LISTEN")
        with patch.object(profile_proxy, "_health_payload", return_value=payload), \
                patch.dict(sys.modules, {"psutil": fake_psutil}):
            self.assertFalse(profile_proxy._healthy())

    def test_coordinator_identity_accepts_reported_python_launcher_alias(self) -> None:
        import types

        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            actual = root / "runtime" / "python.exe"
            launcher = root / "venv" / "python.exe"
            script = root / "coordinator_server.py"
            for path in (actual, launcher, script):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"test")

            class Process:
                def __init__(self, pid: int) -> None:
                    self.pid = pid

                def exe(self) -> str:
                    return str(actual)

                def cmdline(self):
                    return [str(launcher), str(script)]

                def net_connections(self, kind: str):
                    return [types.SimpleNamespace(
                        status="LISTEN", laddr=types.SimpleNamespace(port=profile_proxy.COORDINATOR_PORT),
                    )]

            fake_psutil = types.SimpleNamespace(Process=Process, CONN_LISTEN="LISTEN")
            payload = {"pid": 42, "executable": str(launcher)}
            with patch.object(profile_proxy, "COORDINATOR_SCRIPT", script), \
                    patch.dict(sys.modules, {"psutil": fake_psutil}):
                self.assertTrue(profile_proxy._coordinator_process_ok(payload))

    def test_unversioned_legacy_coordinator_is_replaced_when_script_identity_matches(self) -> None:
        import types

        terminated: list[int] = []

        class Process:
            def __init__(self, pid: int) -> None:
                self.pid = pid

            def net_connections(self, kind: str):
                self.assert_kind = kind
                return [types.SimpleNamespace(status="LISTEN", laddr=types.SimpleNamespace(port=18380))]

            def exe(self) -> str:
                return sys.executable

            def cmdline(self):
                return [
                    sys.executable,
                    str(profile_proxy.RUNTIME_ROOT.parents[1] / "profiles" / "coder" / "plugins" /
                        "llamacpp" / "dashboard" / "coordinator_server.py"),
                ]

            def terminate(self) -> None:
                terminated.append(self.pid)

            def wait(self, timeout: int) -> None:
                return

        listener = types.SimpleNamespace(
            status="LISTEN", laddr=types.SimpleNamespace(port=profile_proxy.COORDINATOR_PORT), pid=42,
        )
        fake_psutil = types.SimpleNamespace(
            Process=Process, CONN_LISTEN="LISTEN", net_connections=lambda kind: [listener],
        )
        with patch.object(profile_proxy, "_health_payload", return_value={"ok": True}), \
                patch.dict(sys.modules, {"psutil": fake_psutil}):
            profile_proxy._stop_incompatible_coordinator()

        self.assertEqual(terminated, [42])

    def test_legacy_coordinator_replacement_rejects_script_path_as_unrelated_argument(self) -> None:
        import types

        terminated: list[int] = []
        trusted_script = (
            profile_proxy.RUNTIME_ROOT.parents[1] / "profiles" / "coder" / "plugins" /
            "llamacpp" / "dashboard" / "coordinator_server.py"
        )

        class Process:
            def __init__(self, pid: int) -> None:
                self.pid = pid

            def exe(self) -> str:
                return "C:/untrusted/evil.exe"

            def cmdline(self):
                return ["C:/untrusted/evil.exe", "--note", str(trusted_script)]

            def net_connections(self, kind: str):
                return [types.SimpleNamespace(status="LISTEN", laddr=types.SimpleNamespace(port=18380))]

            def terminate(self) -> None:
                terminated.append(self.pid)

            def wait(self, timeout: int) -> None:
                return

        listener = types.SimpleNamespace(
            status="LISTEN", laddr=types.SimpleNamespace(port=profile_proxy.COORDINATOR_PORT), pid=42,
        )
        fake_psutil = types.SimpleNamespace(
            Process=Process, CONN_LISTEN="LISTEN", net_connections=lambda kind: [listener],
        )
        with patch.object(profile_proxy, "_health_payload", return_value={"ok": True}), \
                patch.dict(sys.modules, {"psutil": fake_psutil}):
            profile_proxy._stop_incompatible_coordinator()

        self.assertEqual(terminated, [])

    def test_profile_proxy_cleanup_closes_client_when_upstream_close_fails(self) -> None:
        class FakeResponse:
            status_code = 200
            headers = {}

            async def aiter_raw(self):
                yield b"chunk"

            async def aclose(self) -> None:
                raise RuntimeError("close failed")

        class FakeClient:
            def __init__(self) -> None:
                self.closed = False

            async def aclose(self) -> None:
                self.closed = True

        async def exercise() -> bool:
            client = FakeClient()
            iterator = profile_proxy._streaming_response(client, FakeResponse()).body_iterator
            self.assertEqual(await iterator.__anext__(), b"chunk")
            with self.assertRaises(StopAsyncIteration):
                await iterator.__anext__()
            return client.closed

        self.assertTrue(asyncio.run(exercise()))

    def test_coordinator_lifetime_lock_has_single_owner(self) -> None:
        from dashboard.application.coordinator_lock import MachineFileLock

        with tempfile.TemporaryDirectory() as raw_root:
            lock_path = Path(raw_root) / "coordinator.lock"
            first = MachineFileLock(lock_path)
            second = MachineFileLock(lock_path)
            self.assertTrue(first.acquire())
            try:
                self.assertFalse(second.acquire())
            finally:
                first.release()
            self.assertTrue(second.acquire())
            second.release()

    def test_cancelled_proxy_stream_closes_upstream(self) -> None:
        from dashboard.inference_proxy import streaming_response

        class FakeResponse:
            status_code = 200
            headers = {"content-type": "text/event-stream"}

            def __init__(self) -> None:
                self.closed = False
                self.started = asyncio.Event()

            async def aiter_raw(self):
                self.started.set()
                await asyncio.Event().wait()
                yield b"never"

            async def aclose(self) -> None:
                self.closed = True

        class FakeClient:
            def __init__(self) -> None:
                self.closed = False

            async def aclose(self) -> None:
                self.closed = True

        async def exercise() -> tuple[bool, bool]:
            upstream = FakeResponse()
            client = FakeClient()
            response = streaming_response(client, upstream)
            iterator = response.body_iterator
            pending = asyncio.create_task(iterator.__anext__())
            await upstream.started.wait()
            pending.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await pending
            await iterator.aclose()
            return upstream.closed, client.closed

        upstream_closed, client_closed = asyncio.run(exercise())
        self.assertTrue(upstream_closed)
        self.assertTrue(client_closed)

    def test_stream_finalizer_runs_even_when_upstream_close_fails(self) -> None:
        from dashboard.inference_proxy import streaming_response

        finalized = False

        class FakeResponse:
            status_code = 200
            headers = {}

            async def aiter_raw(self):
                yield b"done"

            async def aclose(self) -> None:
                raise RuntimeError("close failed")

        class FakeClient:
            async def aclose(self) -> None:
                return

        async def finalize() -> None:
            nonlocal finalized
            finalized = True

        async def exercise() -> None:
            response = streaming_response(FakeClient(), FakeResponse(), finalize)
            self.assertEqual([chunk async for chunk in response.body_iterator], [b"done"])

        asyncio.run(exercise())
        self.assertTrue(finalized)

    def test_downstream_disconnect_cancels_upstream_without_stopping_worker(self) -> None:
        import httpx
        import uvicorn
        from fastapi import FastAPI
        from fastapi.responses import StreamingResponse
        from dashboard import inference_proxy

        def free_port() -> int:
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                return int(sock.getsockname()[1])

        upstream_port, proxy_port = free_port(), free_port()
        upstream_closed = threading.Event()
        upstream_app = FastAPI()

        @upstream_app.post("/v1/chat/completions")
        async def stream_forever():
            async def body():
                try:
                    yield b"data: first\n\n"
                    while True:
                        await asyncio.sleep(0.02)
                        yield b"data: more\n\n"
                finally:
                    upstream_closed.set()

            return StreamingResponse(body(), media_type="text/event-stream")

        proxy_app = FastAPI()
        proxy_app.include_router(inference_proxy.router)
        upstream_server = uvicorn.Server(uvicorn.Config(upstream_app, host="127.0.0.1", port=upstream_port, log_level="error"))
        proxy_server = uvicorn.Server(uvicorn.Config(proxy_app, host="127.0.0.1", port=proxy_port, log_level="error"))
        threads = [
            threading.Thread(target=upstream_server.run, daemon=True),
            threading.Thread(target=proxy_server.run, daemon=True),
        ]
        with tempfile.TemporaryDirectory() as raw_root:
            state_path = Path(raw_root) / "state.json"
            import psutil
            process = psutil.Process(os.getpid())
            state_path.write_text(
                json.dumps({
                    "port": upstream_port,
                    "pid": os.getpid(),
                    "active_model_id": "actual",
                    "execution_profiles": {
                        "main": {"runtime_kind": "official", "model_id": "actual", "preset_id": ""},
                    },
                    "worker_identity": {
                        "pid": os.getpid(),
                        "create_time": process.create_time(),
                        "executable": process.exe(),
                    },
                }),
                encoding="utf-8",
            )

            class Backend:
                @staticmethod
                def update_execution_queue(_queued: int, _active: int) -> None:
                    return

                @staticmethod
                def ensure_execution_role(_role: str) -> None:
                    return

            with patch.object(inference_proxy, "STATE_PATH", state_path), \
                    patch.object(inference_proxy, "_execution_backend", return_value=Backend()):
                for thread in threads:
                    thread.start()
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline and not (upstream_server.started and proxy_server.started):
                    time.sleep(0.02)
                self.assertTrue(upstream_server.started and proxy_server.started)
                try:
                    with httpx.Client(timeout=5, trust_env=False) as client:
                        with client.stream(
                            "POST",
                            f"http://127.0.0.1:{proxy_port}/v1/chat/completions",
                            json={"model": "main-local", "stream": True, "messages": []},
                        ) as response:
                            self.assertEqual(response.status_code, 200)
                            next(response.iter_bytes())
                    self.assertTrue(upstream_closed.wait(3), "upstream generator survived the downstream disconnect")
                    self.assertTrue(upstream_server.started, "persistent worker should remain available after request cancellation")
                finally:
                    proxy_server.should_exit = True
                    upstream_server.should_exit = True
                    for thread in threads:
                        thread.join(timeout=5)

    def test_profile_proxy_disconnect_propagates_through_coordinator_to_worker(self) -> None:
        import httpx
        import psutil
        import uvicorn
        from fastapi import FastAPI
        from fastapi.responses import StreamingResponse
        from dashboard import inference_proxy

        def free_port() -> int:
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                return int(sock.getsockname()[1])

        worker_port, coordinator_port, profile_port = free_port(), free_port(), free_port()
        worker_closed = threading.Event()
        worker_app = FastAPI()

        @worker_app.post("/v1/chat/completions")
        async def stream_forever():
            async def body():
                try:
                    yield b"data: first\n\n"
                    while True:
                        await asyncio.sleep(0.02)
                        yield b"data: more\n\n"
                finally:
                    worker_closed.set()

            return StreamingResponse(body(), media_type="text/event-stream")

        coordinator_app = FastAPI()
        coordinator_app.include_router(inference_proxy.router)
        profile_app = FastAPI()
        profile_app.include_router(profile_proxy.router)
        servers = [
            uvicorn.Server(uvicorn.Config(worker_app, host="127.0.0.1", port=worker_port, log_level="error")),
            uvicorn.Server(uvicorn.Config(coordinator_app, host="127.0.0.1", port=coordinator_port, log_level="error")),
            uvicorn.Server(uvicorn.Config(profile_app, host="127.0.0.1", port=profile_port, log_level="error")),
        ]
        threads = [threading.Thread(target=server.run, daemon=True) for server in servers]

        with tempfile.TemporaryDirectory() as raw_root:
            process = psutil.Process(os.getpid())
            state_path = Path(raw_root) / "state.json"
            state_path.write_text(json.dumps({
                "port": worker_port,
                "pid": os.getpid(),
                "active_model_id": "actual",
                "execution_profiles": {
                    "main": {"runtime_kind": "official", "model_id": "actual", "preset_id": ""},
                },
                "worker_identity": {
                    "pid": os.getpid(),
                    "create_time": process.create_time(),
                    "executable": process.exe(),
                },
            }), encoding="utf-8")

            class Backend:
                @staticmethod
                def update_execution_queue(_queued: int, _active: int) -> None:
                    return

                @staticmethod
                def ensure_execution_role(_role: str) -> None:
                    return

            with patch.object(inference_proxy, "STATE_PATH", state_path), \
                    patch.object(inference_proxy, "_execution_backend", return_value=Backend()), \
                    patch.object(profile_proxy, "COORDINATOR_URL", f"http://127.0.0.1:{coordinator_port}"), \
                    patch.object(profile_proxy, "_ensure_coordinator", return_value=None):
                for thread in threads:
                    thread.start()
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline and not all(server.started for server in servers):
                    time.sleep(0.02)
                self.assertTrue(all(server.started for server in servers))
                try:
                    with httpx.Client(timeout=5, trust_env=False) as client:
                        with client.stream(
                            "POST", f"http://127.0.0.1:{profile_port}/v1/chat/completions",
                            json={"model": "main-local", "stream": True, "messages": []},
                        ) as response:
                            self.assertEqual(response.status_code, 200)
                            next(response.iter_bytes())
                    self.assertTrue(worker_closed.wait(3), "worker request survived the Hermes-side disconnect")
                finally:
                    for server in reversed(servers):
                        server.should_exit = True
                    for thread in threads:
                        thread.join(timeout=5)

    def test_live_compression_disconnect_closes_worker_stream_and_restores_main(self) -> None:
        import httpx
        import psutil
        import uvicorn
        from fastapi import FastAPI
        from fastapi.responses import StreamingResponse
        from dashboard import inference_proxy

        def free_port() -> int:
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                return int(sock.getsockname()[1])

        worker_port, proxy_port = free_port(), free_port()
        worker_closed = threading.Event()
        main_restored = threading.Event()
        calls: list[str] = []
        abort_errors: list[str] = []
        worker_app = FastAPI()

        @worker_app.post("/v1/chat/completions")
        async def stream_forever():
            async def body():
                try:
                    yield b"data: first\n\n"
                    while True:
                        await asyncio.sleep(0.02)
                        yield b"data: more\n\n"
                finally:
                    worker_closed.set()

            return StreamingResponse(body(), media_type="text/event-stream")

        class Backend:
            @staticmethod
            def update_execution_queue(_queued: int, _active: int) -> None:
                return

            @staticmethod
            def ensure_execution_role(role: str) -> None:
                calls.append(role)
                if role == "main":
                    main_restored.set()

            @staticmethod
            def abort_execution_transition(_role: str, error: str) -> None:
                abort_errors.append(error)

        proxy_app = FastAPI()
        proxy_app.include_router(inference_proxy.router)
        servers = [
            uvicorn.Server(uvicorn.Config(worker_app, host="127.0.0.1", port=worker_port, log_level="error")),
            uvicorn.Server(uvicorn.Config(proxy_app, host="127.0.0.1", port=proxy_port, log_level="error")),
        ]
        threads = [threading.Thread(target=server.run, daemon=True) for server in servers]

        with tempfile.TemporaryDirectory() as raw_root:
            process = psutil.Process(os.getpid())
            state_path = Path(raw_root) / "state.json"
            state_path.write_text(json.dumps({
                "port": worker_port,
                "pid": os.getpid(),
                "active_model_id": "small",
                "execution_profiles": {
                    "main": {"runtime_kind": "official", "model_id": "large", "preset_id": ""},
                    "compression": {"runtime_kind": "official", "model_id": "small", "preset_id": ""},
                },
                "worker_identity": {
                    "pid": os.getpid(),
                    "create_time": process.create_time(),
                    "executable": process.exe(),
                },
            }), encoding="utf-8")
            with patch.object(inference_proxy, "STATE_PATH", state_path), \
                    patch.object(inference_proxy, "_execution_backend", return_value=Backend()):
                for thread in threads:
                    thread.start()
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline and not all(server.started for server in servers):
                    time.sleep(0.02)
                self.assertTrue(all(server.started for server in servers))
                try:
                    with httpx.Client(timeout=5, trust_env=False) as client:
                        with client.stream(
                            "POST", f"http://127.0.0.1:{proxy_port}/v1/chat/completions",
                            json={"model": "compression-local", "stream": True, "messages": []},
                        ) as response:
                            self.assertEqual(response.status_code, 200)
                            next(response.iter_bytes())
                    self.assertTrue(worker_closed.wait(3))
                    self.assertTrue(main_restored.wait(3), f"Main restoration was not observed; calls={calls}, abort_errors={abort_errors}, locked={inference_proxy._transition_lock.locked()}")
                    deadline = time.monotonic() + 1
                    while inference_proxy._transition_lock.locked() and time.monotonic() < deadline:
                        time.sleep(0.01)
                    self.assertFalse(inference_proxy._transition_lock.locked())
                finally:
                    for server in reversed(servers):
                        server.should_exit = True
                    for thread in threads:
                        thread.join(timeout=5)

        self.assertEqual(calls, ["compression", "main"])

    def test_both_proxy_hops_strip_authorization_headers(self) -> None:
        from dashboard import inference_proxy

        headers = {"Authorization": "Bearer [REDACTED]", "Content-Type": "application/json"}

        self.assertNotIn("Authorization", profile_proxy._filtered_headers(headers))
        self.assertNotIn("Authorization", inference_proxy._filtered_headers(headers))
        self.assertEqual(profile_proxy._filtered_headers(headers)["Content-Type"], "application/json")
        self.assertEqual(inference_proxy._filtered_headers(headers)["Content-Type"], "application/json")

    def test_compression_execution_restores_main_after_stream_finishes(self) -> None:
        from dashboard import inference_proxy

        calls: list[str] = []

        class Backend:
            @staticmethod
            def update_execution_queue(_queued: int, _active: int) -> None:
                return

            @staticmethod
            def ensure_execution_role(role: str) -> None:
                calls.append(role)

        async def exercise() -> None:
            with patch.object(inference_proxy, "_execution_backend", return_value=Backend()):
                finalize = await inference_proxy._begin_execution("compression")
                self.assertIsNotNone(finalize)
                await finalize()

        asyncio.run(exercise())
        self.assertEqual(calls, ["compression", "main"])

    def test_aux_only_compression_stays_loaded_after_stream_finishes(self) -> None:
        from dashboard import inference_proxy

        calls: list[str] = []

        class Backend:
            @staticmethod
            def update_execution_queue(_queued: int, _active: int) -> None:
                return

            @staticmethod
            def execution_role_configured(role: str) -> bool:
                return role == "compression"

            @staticmethod
            def ensure_execution_role(role: str) -> None:
                calls.append(role)

        async def exercise() -> None:
            with patch.object(inference_proxy, "_execution_backend", return_value=Backend()):
                finalize = await inference_proxy._begin_execution("compression")
                self.assertIsNotNone(finalize)
                await finalize()

        asyncio.run(exercise())
        self.assertEqual(calls, ["compression"])

    def test_cancelled_compression_finalizer_restores_main_before_releasing_transition(self) -> None:
        from dashboard import inference_proxy

        calls: list[str] = []
        restore_started = threading.Event()
        allow_restore = threading.Event()

        class Backend:
            aborted: list[tuple[str, str]] = []

            @staticmethod
            def update_execution_queue(_queued: int, _active: int) -> None:
                return

            @staticmethod
            def ensure_execution_role(role: str) -> None:
                calls.append(role)
                if role == "main":
                    restore_started.set()
                    allow_restore.wait(2)

            @classmethod
            def abort_execution_transition(cls, role: str, error: str) -> None:
                cls.aborted.append((role, error))

        async def exercise() -> None:
            with patch.object(inference_proxy, "_execution_backend", return_value=Backend()):
                finish = await inference_proxy._begin_execution("compression")
                task = asyncio.create_task(finish())
                await asyncio.to_thread(restore_started.wait, 1)
                task.cancel()
                await asyncio.sleep(0.05)
                self.assertTrue(inference_proxy._transition_lock.locked())
                allow_restore.set()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                self.assertFalse(inference_proxy._transition_lock.locked())

        asyncio.run(exercise())
        self.assertEqual(calls, ["compression", "main"])
        self.assertEqual(Backend.aborted, [])

    def test_compression_start_failure_attempts_main_restoration(self) -> None:
        from dashboard import inference_proxy

        calls: list[str] = []

        class Backend:
            @staticmethod
            def update_execution_queue(_queued: int, _active: int) -> None:
                return

            @staticmethod
            def ensure_execution_role(role: str) -> None:
                calls.append(role)
                if role == "compression":
                    raise RuntimeError("compression failed")

        async def exercise() -> None:
            with patch.object(inference_proxy, "_execution_backend", return_value=Backend()):
                with self.assertRaisesRegex(RuntimeError, "compression failed"):
                    await inference_proxy._begin_execution("compression")

        asyncio.run(exercise())
        self.assertEqual(calls, ["compression", "main"])

    def test_main_request_waits_until_compression_restores_main(self) -> None:
        from dashboard import inference_proxy

        calls: list[str] = []

        class Backend:
            @staticmethod
            def update_execution_queue(_queued: int, _active: int) -> None:
                return

            @staticmethod
            def ensure_execution_role(role: str) -> None:
                calls.append(role)

        async def exercise() -> None:
            with patch.object(inference_proxy, "_execution_backend", return_value=Backend()):
                finish_compression = await inference_proxy._begin_execution("compression")
                waiting_main = asyncio.create_task(inference_proxy._begin_execution("main"))
                await asyncio.sleep(0.05)
                self.assertFalse(waiting_main.done())
                await finish_compression()
                finish_main = await asyncio.wait_for(waiting_main, timeout=1)
                await finish_main()

        asyncio.run(exercise())
        self.assertEqual(calls, ["compression", "main", "main"])

    def test_cancelled_queued_main_request_does_not_leak_queue_count(self) -> None:
        from dashboard import inference_proxy

        class Backend:
            @staticmethod
            def update_execution_queue(_queued: int, _active: int) -> None:
                return

            @staticmethod
            def ensure_execution_role(_role: str) -> None:
                return

        async def exercise() -> None:
            inference_proxy._queued_main_requests = 0
            inference_proxy._active_main_requests = 0
            with patch.object(inference_proxy, "_execution_backend", return_value=Backend()):
                await inference_proxy._transition_lock.acquire()
                waiting = asyncio.create_task(inference_proxy._begin_execution("main"))
                await asyncio.sleep(0.05)
                waiting.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await waiting
                inference_proxy._transition_lock.release()
                self.assertEqual(inference_proxy._queued_main_requests, 0)

        asyncio.run(exercise())

    def test_cancellation_during_initial_queue_publish_rolls_back_queue_count(self) -> None:
        from dashboard import inference_proxy

        async def exercise() -> None:
            publish_started = asyncio.Event()
            release_publish = asyncio.Event()
            publish_calls = 0

            async def publish() -> None:
                nonlocal publish_calls
                publish_calls += 1
                if publish_calls == 1:
                    publish_started.set()
                    await release_publish.wait()

            inference_proxy._queued_main_requests = 0
            inference_proxy._active_main_requests = 0
            with patch.object(inference_proxy, "_publish_counts", side_effect=publish):
                waiting = asyncio.create_task(inference_proxy._begin_execution("main"))
                await publish_started.wait()
                waiting.cancel()
                release_publish.set()
                with self.assertRaises(asyncio.CancelledError):
                    await waiting

            self.assertEqual(inference_proxy._queued_main_requests, 0)
            self.assertEqual(inference_proxy._active_main_requests, 0)
            self.assertFalse(inference_proxy._transition_lock.locked())

        asyncio.run(exercise())

    def test_cancellation_during_active_count_publish_rolls_back_active_count(self) -> None:
        from dashboard import inference_proxy

        class Backend:
            @staticmethod
            def update_execution_queue(_queued: int, _active: int) -> None:
                return

            @staticmethod
            def ensure_execution_role(_role: str) -> None:
                return

        async def exercise() -> None:
            active_publish_started = asyncio.Event()
            release_publish = asyncio.Event()
            publish_calls = 0

            async def publish() -> None:
                nonlocal publish_calls
                publish_calls += 1
                if publish_calls == 2:
                    active_publish_started.set()
                    await release_publish.wait()

            inference_proxy._queued_main_requests = 0
            inference_proxy._active_main_requests = 0
            with patch.object(inference_proxy, "_execution_backend", return_value=Backend()), \
                    patch.object(inference_proxy, "_publish_counts", side_effect=publish):
                waiting = asyncio.create_task(inference_proxy._begin_execution("main"))
                await active_publish_started.wait()
                waiting.cancel()
                release_publish.set()
                with self.assertRaises(asyncio.CancelledError):
                    await waiting

            self.assertEqual(inference_proxy._queued_main_requests, 0)
            self.assertEqual(inference_proxy._active_main_requests, 0)
            self.assertFalse(inference_proxy._transition_lock.locked())

        asyncio.run(exercise())

    def test_proxy_rejects_physical_model_name_that_bypasses_execution_policy(self) -> None:
        from dashboard.inference_proxy import validate_logical_model

        with self.assertRaisesRegex(Exception, "logical model"):
            validate_logical_model(b'{"model":"physical-model"}')
        with self.assertRaisesRegex(Exception, "logical model"):
            validate_logical_model(b'{"messages":[]}')
        with self.assertRaisesRegex(Exception, "logical model"):
            validate_logical_model(b'{"model":""}')

    def test_execution_role_applies_profile_runtime_model_and_role_log(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            state_path = root / "state.json"
            activity_path = root / "logs" / "activity.log"
            state_path.write_text(json.dumps({
                "models": {"large": {}, "Ternary-Bonsai-small": {}},
                "active_model_id": "large",
                "runtime_kind": "official",
                "execution_profiles": {
                    "main": {"runtime_kind": "official", "model_id": "large", "preset_id": ""},
                    "compression": {"runtime_kind": "prism_ml", "model_id": "Ternary-Bonsai-small", "preset_id": ""},
                },
            }), encoding="utf-8")
            started: list[tuple[Path, str | None]] = []

            class Startup:
                def __init__(self, path: Path, runtime_kind: str | None) -> None:
                    self.path = path
                    self.runtime_kind = runtime_kind

                def start(self) -> None:
                    started.append((self.path, self.runtime_kind))

            with patch.object(api, "STATE_PATH", state_path), \
                    patch.object(api, "TRANSITION_LOG_PATH", activity_path), \
                    patch.object(api, "ACTIVITY_LOG_PATH", activity_path), \
                    patch.object(api, "_is_server_running", return_value=False), \
                    patch.object(api, "_server_startup", side_effect=lambda path=None, runtime_kind=None: Startup(path, runtime_kind)):
                result = api.ensure_execution_role("compression")
                state = api._state()

            self.assertEqual(result["role"], "compression")
            self.assertEqual(state["active_model_id"], "Ternary-Bonsai-small")
            self.assertEqual(state["runtime_kind"], "official")
            self.assertEqual(state["active_runtime_kind"], "prism_ml")
            self.assertEqual(state["active_role"], "compression")
            self.assertEqual(state["transition_phase"], "COMPRESSING")
            self.assertEqual(started, [(activity_path, "prism_ml")])
            self.assertIn('[transition] {"event": "transition-ready"', activity_path.read_text(encoding="utf-8"))

    def test_intentionally_stopped_coordinator_is_not_restarted_by_passive_requests(self) -> None:
        from dashboard import plugin_api as profile_proxy

        with tempfile.TemporaryDirectory() as raw_root:
            marker = Path(raw_root) / "coordinator-stopped"
            marker.write_text("stopped", encoding="utf-8")
            with patch.object(profile_proxy, "COORDINATOR_STOP_MARKER", marker), \
                    patch.object(profile_proxy, "_healthy", return_value=False), \
                    patch.object(profile_proxy, "_try_start_coordinator") as start:
                with self.assertRaisesRegex(RuntimeError, "intentionally stopped"):
                    profile_proxy._ensure_coordinator()

            start.assert_not_called()

    def test_stop_marker_blocks_passive_requests_even_if_a_stale_coordinator_is_healthy(self) -> None:
        from dashboard import plugin_api as profile_proxy

        with tempfile.TemporaryDirectory() as raw_root:
            marker = Path(raw_root) / "coordinator-stopped"
            marker.write_text("stopped", encoding="utf-8")
            with patch.object(profile_proxy, "COORDINATOR_STOP_MARKER", marker), \
                    patch.object(profile_proxy, "_healthy", return_value=True), \
                    patch.object(profile_proxy, "_try_start_coordinator") as start:
                with self.assertRaisesRegex(RuntimeError, "intentionally stopped"):
                    profile_proxy._ensure_coordinator()

            start.assert_not_called()

    def test_explicit_server_start_clears_stop_marker_before_starting_coordinator(self) -> None:
        from dashboard import plugin_api as profile_proxy

        with tempfile.TemporaryDirectory() as raw_root:
            marker = Path(raw_root) / "coordinator-stopped"
            marker.write_text("stopped", encoding="utf-8")
            health = iter([False, True])
            with patch.object(profile_proxy, "COORDINATOR_STOP_MARKER", marker), \
                    patch.object(profile_proxy, "_healthy", side_effect=lambda: next(health, True)), \
                    patch.object(profile_proxy, "_stop_incompatible_coordinator"), \
                    patch.object(profile_proxy, "_try_start_coordinator", return_value=True):
                profile_proxy._ensure_coordinator(allow_start=True)

            self.assertFalse(marker.exists())

    def test_only_explicit_start_requests_wake_an_intentionally_stopped_coordinator(self) -> None:
        from dashboard import plugin_api as profile_proxy

        self.assertTrue(profile_proxy._coordinator_start_requested("POST", "profiles/start", b"{}"))
        self.assertTrue(profile_proxy._coordinator_start_requested("POST", "server", b'{"action":"start"}'))
        self.assertFalse(profile_proxy._coordinator_start_requested("POST", "server", b'{"action":"stop"}'))
        self.assertFalse(profile_proxy._coordinator_start_requested("GET", "status", b""))

    def test_main_role_change_waits_for_active_main_request_to_finish(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            state_path = root / "state.json"
            state_path.write_text(json.dumps({
                "models": {"old": {}, "new": {}},
                "active_model_id": "old",
                "active_role": "main",
                "runtime_kind": "official",
                "active_main_requests": 1,
                "execution_profiles": {
                    "main": {"runtime_kind": "official", "model_id": "new", "preset_id": ""},
                },
            }), encoding="utf-8")
            started = threading.Event()
            completed: list[dict[str, object]] = []

            class Startup:
                def start(self) -> None:
                    started.set()

            with patch.object(api, "STATE_PATH", state_path), \
                    patch.object(api, "TRANSITION_LOG_PATH", root / "activity.log"), \
                    patch.object(api, "ACTIVITY_LOG_PATH", root / "activity.log"), \
                    patch.object(api, "_is_server_running", return_value=False), \
                    patch.object(api, "_server_startup", return_value=Startup()):
                worker = threading.Thread(
                    target=lambda: completed.append(api.ensure_execution_role("main")), daemon=True,
                )
                worker.start()
                time.sleep(0.05)
                self.assertFalse(started.is_set())
                api._mutate_state(lambda state: state.update({"active_main_requests": 0}))
                worker.join(timeout=1)

            self.assertTrue(started.is_set())
            self.assertEqual(completed[0]["model_id"], "new")

    def test_clearing_the_last_execution_profile_unregisters_the_endpoint(self) -> None:
        state = {
            "custom_endpoint": {"key": "llamacpp-local"},
            "execution_profiles": {
                "compression": {"runtime_kind": "official", "model_id": "small", "preset_id": ""},
            },
        }
        events: list[str] = []

        class Service:
            @staticmethod
            def save(_role: str, _body: dict[str, object]):
                state["execution_profiles"] = {}
                return {
                    "execution_mode": "exclusive_swap",
                    "profiles": {
                        "main": {"configured": False},
                        "compression": {"configured": False},
                    },
                }

        def mutate(callback):
            callback(state)
            return state

        with patch.object(api, "_state", side_effect=lambda: json.loads(json.dumps(state))), \
                patch.object(api, "_mutate_state", side_effect=mutate), \
                patch.object(api, "_execution_profiles_service", return_value=Service()), \
                patch.object(api, "_unregister_custom_endpoint", side_effect=lambda: events.append("unregister")), \
                patch.object(api, "_sync_execution_profile_endpoint", side_effect=AssertionError("must not sync an empty endpoint")):
            result = api.save_execution_profile("compression", {"model_id": ""})

        self.assertFalse(result["profiles"]["compression"]["configured"])
        self.assertEqual(events, ["unregister"])
        self.assertIsNone(state["custom_endpoint"])

    def test_failed_profile_endpoint_sync_rolls_back_only_the_written_role(self) -> None:
        state = {
            "pid": 10,
            "execution_profiles": {
                "main": {"runtime_kind": "official", "model_id": "old", "preset_id": ""},
            },
        }
        old = dict(state["execution_profiles"]["main"])
        new = {"runtime_kind": "official", "model_id": "new", "preset_id": ""}

        class Service:
            @staticmethod
            def save(_role: str, _body: dict[str, object]):
                state["execution_profiles"]["main"] = dict(new)
                return {"execution_mode": "exclusive_swap", "profiles": {"main": {**new, "configured": True}}}

        def mutate(callback):
            callback(state)
            return state

        def fail_sync():
            state["pid"] = 99
            raise RuntimeError("sync failed")

        with patch.object(api, "_state", side_effect=lambda: json.loads(json.dumps(state))), \
                patch.object(api, "_save_state", side_effect=lambda value: (state.clear(), state.update(value))), \
                patch.object(api, "_mutate_state", side_effect=mutate), \
                patch.object(api, "_execution_profiles_service", return_value=Service()), \
                patch.object(api, "_sync_execution_profile_endpoint", side_effect=fail_sync):
            with self.assertRaisesRegex(RuntimeError, "sync failed"):
                api.save_execution_profile("main", new)

        self.assertEqual(state["pid"], 99)
        self.assertEqual(state["execution_profiles"]["main"], old)

    def test_profile_endpoint_sync_rolls_back_files_after_partial_write_failure(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            first = root / "main.yaml"
            second = root / "coder.yaml"
            first.write_text("providers:\n", encoding="utf-8")
            second.write_text("providers:\n", encoding="utf-8")
            writes = 0

            def flaky_write(path: Path, text: str) -> None:
                nonlocal writes
                writes += 1
                if writes == 2:
                    raise OSError("second profile write failed")
                path.write_text(text, encoding="utf-8")

            with patch.object(api, "_profile_config_paths", return_value=[first, second]), \
                    patch.object(api, "_profile_provider_models", return_value={"main-local": 8192}), \
                    patch.object(api, "_write_profile_config", side_effect=flaky_write):
                with self.assertRaisesRegex(OSError, "second profile"):
                    api._sync_execution_profile_endpoint()

            self.assertEqual(first.read_text(encoding="utf-8"), "providers:\n")
            self.assertEqual(second.read_text(encoding="utf-8"), "providers:\n")

    def test_profile_endpoint_sync_surfaces_rollback_write_failure(self) -> None:
        first = Path("first.yaml")
        second = Path("second.yaml")
        writes = 0

        def broken_write(path: Path, _text: str) -> None:
            nonlocal writes
            writes += 1
            if writes == 2:
                raise OSError("update failed")
            if writes >= 3:
                raise OSError("rollback failed")

        with patch.object(api, "_write_profile_config", side_effect=broken_write):
            with self.assertRaisesRegex(Exception, "rollback failed"):
                api._apply_profile_updates([
                    (first, "old-first", "new-first"),
                    (second, "old-second", "new-second"),
                ])

    def test_server_termination_refuses_reused_worker_pid_identity(self) -> None:
        import types

        terminated: list[int] = []

        class NoSuchProcess(Exception):
            pass

        class TimeoutExpired(Exception):
            pass

        class Process:
            def __init__(self, pid: int) -> None:
                self.pid = pid

            def create_time(self) -> float:
                return 200.0

            def exe(self) -> str:
                return sys.executable

            def terminate(self) -> None:
                terminated.append(self.pid)

            def wait(self, timeout: int) -> None:
                return

        fake_psutil = types.SimpleNamespace(
            Process=Process,
            NoSuchProcess=NoSuchProcess,
            TimeoutExpired=TimeoutExpired,
        )
        state = {
            "pid": 42,
            "worker_identity": {
                "pid": 42,
                "create_time": 100.0,
                "executable": sys.executable,
            },
        }
        with patch.object(api, "_state", return_value=state), \
                patch.object(api.platform, "system", return_value="Linux"), \
                patch.dict(sys.modules, {"psutil": fake_psutil}):
            api._terminate_server(42)

        self.assertEqual(terminated, [])

    def test_server_termination_refuses_identityless_worker_state(self) -> None:
        import types

        terminated: list[int] = []

        class Process:
            def __init__(self, pid: int) -> None:
                self.pid = pid

            def exe(self) -> str:
                return sys.executable

            def terminate(self) -> None:
                terminated.append(self.pid)

            def wait(self, timeout: int) -> None:
                return

        fake_psutil = types.SimpleNamespace(
            Process=Process,
            NoSuchProcess=type("NoSuchProcess", (Exception,), {}),
            TimeoutExpired=type("TimeoutExpired", (Exception,), {}),
        )
        with patch.object(api, "_state", return_value={"pid": 42, "worker_identity": None}), \
                patch.object(api, "_server_executable", return_value=Path(sys.executable)), \
                patch.object(api.platform, "system", return_value="Linux"), \
                patch.dict(sys.modules, {"psutil": fake_psutil}):
            self.assertFalse(api._terminate_server(42))

        self.assertEqual(terminated, [])

    def test_worker_identity_requires_listener_ownership(self) -> None:
        import types
        from dashboard import inference_proxy

        class Process:
            def __init__(self, pid: int) -> None:
                self.pid = pid

            def create_time(self) -> float:
                return 100.0

            def exe(self) -> str:
                return sys.executable

            def net_connections(self, kind: str):
                return [types.SimpleNamespace(status="LISTEN", laddr=types.SimpleNamespace(port=9999))]

        fake_psutil = types.SimpleNamespace(Process=Process, CONN_LISTEN="LISTEN")
        state = {
            "pid": 42,
            "port": 18434,
            "worker_identity": {"pid": 42, "create_time": 100.0, "executable": sys.executable},
        }
        with patch.dict(sys.modules, {"psutil": fake_psutil}):
            self.assertFalse(inference_proxy._worker_identity_ok(state))

    def test_worker_identity_requires_identity_pid_to_match_state_pid(self) -> None:
        import types
        from dashboard import inference_proxy

        class Process:
            def __init__(self, pid: int) -> None:
                self.pid = pid

            def create_time(self) -> float:
                return 100.0

            def exe(self) -> str:
                return sys.executable

            def net_connections(self, kind: str):
                return [types.SimpleNamespace(status="LISTEN", laddr=types.SimpleNamespace(port=18434))]

        fake_psutil = types.SimpleNamespace(Process=Process, CONN_LISTEN="LISTEN")
        state = {
            "pid": 42,
            "port": 18434,
            "worker_identity": {"pid": 99, "create_time": 100.0, "executable": sys.executable},
        }
        with patch.dict(sys.modules, {"psutil": fake_psutil}):
            self.assertFalse(inference_proxy._worker_identity_ok(state))

    def test_proxy_rewrites_logical_main_model_to_bound_worker_model(self) -> None:
        from dashboard.inference_proxy import rewrite_logical_model

        body = b'{"model":"main-local","messages":[]}'
        state = {
            "active_model_id": "large",
            "execution_profiles": {
                "main": {"runtime_kind": "official", "model_id": "large"},
                "compression": {"runtime_kind": "prism_ml", "model_id": "small"},
            },
        }

        rewritten = rewrite_logical_model(body, state)

        self.assertIn(b'"model":"large"', rewritten)

    def test_proxy_model_reasoning_effort_overrides_hermes_request_value(self) -> None:
        from dashboard.inference_proxy import rewrite_logical_model

        body = b'{"model":"main-local","reasoning_effort":"high","messages":[]}'
        state = {
            "active_model_id": "large",
            "model_settings": {"large": {"reasoning-effort": "medium"}},
            "execution_profiles": {
                "main": {"runtime_kind": "official", "model_id": "large"},
            },
        }

        rewritten = json.loads(rewrite_logical_model(body, state))

        self.assertEqual(rewritten["model"], "large")
        self.assertEqual(rewritten["reasoning_effort"], "medium")

    def test_proxy_drops_hermes_reasoning_effort_when_model_has_no_override(self) -> None:
        from dashboard.inference_proxy import rewrite_logical_model

        body = b'{"model":"main-local","reasoning_effort":"high","messages":[]}'
        state = {
            "active_model_id": "large",
            "model_settings": {"large": {}},
            "execution_profiles": {
                "main": {"runtime_kind": "official", "model_id": "large"},
            },
        }

        rewritten = json.loads(rewrite_logical_model(body, state))

        self.assertNotIn("reasoning_effort", rewritten)

    def test_native_router_preserves_alias_and_applies_model_reasoning_effort(self) -> None:
        from dashboard.inference_proxy import rewrite_logical_model

        body = b'{"model":"main-local","reasoning_effort":"high","messages":[]}'
        state = {
            "execution_mode": "native_router",
            "model_settings": {"large": {"reasoning-effort": "medium"}},
            "execution_profiles": {
                "main": {"runtime_kind": "official", "model_id": "large"},
            },
        }

        rewritten = json.loads(rewrite_logical_model(body, state))

        self.assertEqual(rewritten["model"], "main-local")
        self.assertEqual(rewritten["reasoning_effort"], "medium")

    def test_proxy_request_context_is_written_as_privacy_safe_activity_event(self) -> None:
        import tempfile
        from dashboard import inference_proxy

        with tempfile.TemporaryDirectory() as raw_root:
            log_path = Path(raw_root) / "activity.log"
            context_path = Path(raw_root) / "request-context.jsonl"
            log_path.write_text("worker output\n", encoding="utf-8")
            expected_offset = log_path.stat().st_size
            with patch.object(inference_proxy, "ACTIVITY_LOG_PATH", log_path), \
                    patch.object(inference_proxy, "REQUEST_CONTEXT_LOG_PATH", context_path):
                inference_proxy.record_request_context(
                    "main", "project-manager", "20261003_164117_5bf794"
                )

            event = json.loads(context_path.read_text(encoding="utf-8"))

        self.assertEqual(event["type"], "hermes_request_context")
        self.assertEqual(event["role"], "main")
        self.assertEqual(event["profile"], "project-manager")
        self.assertEqual(event["session"], "20261003_164117_5bf794")
        self.assertEqual(event["activity_offset"], expected_offset)
        self.assertRegex(event["request_id"], r"^[0-9a-f]{32}$")
        self.assertGreater(event["recorded_at"], 0)

    def test_proxy_rewrites_inactive_compression_profile_for_coordinated_swap(self) -> None:
        from dashboard.inference_proxy import rewrite_logical_model

        state = {
            "active_model_id": "large",
            "execution_profiles": {
                "main": {"runtime_kind": "official", "model_id": "large"},
                "compression": {"runtime_kind": "prism_ml", "model_id": "small"},
            },
        }

        rewritten = rewrite_logical_model(b'{"model":"compression-local"}', state)
        self.assertIn(b'"model":"small"', rewritten)

    def test_status_exposes_singleton_coordinator_and_exclusive_execution_policy(self) -> None:
        class Inspector:
            @staticmethod
            def status():
                return {"server_running": False, "models": [{"id": "large", "label": "large"}]}

        with patch.object(api, "_runtime_inspector", return_value=Inspector()), \
                patch.object(api, "_state", return_value={"models": {"large": {}, "small": {}}}):
            status = api._status()

        self.assertEqual(status["execution_mode"], "exclusive_swap")
        self.assertEqual(status["coordinator"]["port"], 18380)
        self.assertTrue(status["coordinator"]["singleton"])
        self.assertEqual(status["coordinator"]["inference_base_url"], "http://127.0.0.1:18380/v1")
        self.assertEqual([row["id"] for row in status["models"]], ["large", "small"])
        self.assertEqual([row["id"] for row in status["main_model_options"]], ["large", "small"])
        self.assertEqual([row["id"] for row in status["auxiliary_model_options"]], ["large", "small"])
        self.assertEqual([row["id"] for row in status["profile_model_options"]], ["large", "small"])
        self.assertEqual(list(status["logs"]), ["activity"])
        self.assertTrue(status["logs"]["activity"].endswith("activity.log"))

    def test_operations_ui_displays_singleton_proxy_and_cancellation_contract(self) -> None:
        source = (Path(__file__).parent / "desktop" / "plugin.js").read_text(encoding="utf-8")
        self.assertIn("Singleton proxy", source)
        self.assertIn("응답 중단 시 upstream 요청도 종료", source)
        self.assertIn("Exclusive swap", source)
        self.assertIn("Main", source)
        self.assertIn("서버와 Singleton proxy를 중지했습니다.", source)
        self.assertIn("queryClient.setQueryData([ID, 'status']", source)
        self.assertIn("query.state.data?.coordinator?.ok === false ? false", source)
        self.assertIn("pluginCtx.socket('/events'", source)
        self.assertIn("pushConnected ? false", source)
        self.assertIn("push 연결", source)
        self.assertIn("Auxiliary", source)
        self.assertIn("Main이 선택되어 있으면 Main을 우선 로드합니다", source)
        self.assertIn("value: 'compress'", source)
        self.assertNotIn("`/profiles/${role}/start`", source)
        self.assertIn("api('/profiles/start'", source)
        self.assertIn("api(`/profiles/${role}`", source)
        self.assertIn("children: '사용 안 함'", source)
        self.assertIn("role === 'main' ? status?.main_model_options : status?.auxiliary_model_options", source)
        self.assertIn("const serverRunning = Boolean(status?.server_running)", source)
        self.assertIn("disabled: serverRunning || saving", source)
        self.assertIn("'서버 시작'", source)
        self.assertEqual(source.count("'서버 시작'"), 1)
        profile_row = source[source.index("function ExecutionProfileRow"):source.index("function ExecutionProfilesPanel")]
        self.assertNotIn("children: '시작'", profile_row)
        self.assertNotIn("children: '중지'", profile_row)
        self.assertIn("modelId.startsWith('Ternary-Bonsai')", source)
        self.assertNotIn("compressionFallback", source)
        self.assertIn("disabled: starting || (!mainModelId && !compressionModelId)", source)
        log_panel = source[source.index("function ServerLogPanel"):source.index("function UnexpectedExitPanel")]
        self.assertNotIn("통합 로그", log_panel)
        self.assertIn("'aria-label': 'llama.cpp 로그'", log_panel)
        self.assertIn("api('/logs?limit=250')", source)
        self.assertIn("api('/metrics?window=60')", source)
        self.assertIn("function ResourceMetricsPanel", source)
        self.assertIn("children: '최근 60초 리소스'", source)
        self.assertIn("title: 'Main model'", source)
        self.assertIn("title: 'Aux model'", source)
        self.assertIn("label: 'Prompt tok/s'", source)
        self.assertIn("label: 'Generation tok/s'", source)
        self.assertIn("children: 'Token 처리량 · Main/Aux 공용 timeline'", source)
        self.assertIn("children: 'Context 사용량 · Main/Aux 공용 timeline'", source)
        self.assertIn("children: '요청 부하'", source)
        self.assertIn("label: '요청 수'", source)
        self.assertIn("children: '60초 전'", source)
        self.assertIn("children: '현재'", source)
        self.assertIn("const AUX_COLOR = 'var(--ui-cyan)'", source)
        self.assertIn("axis: 'left'", source)
        self.assertIn("axis: 'right'", source)
        self.assertNotIn("function RoleMetricCard", source)
        self.assertNotIn("분리 로그", source)
        self.assertIn("/lifecycle/lease", source)
        self.assertIn("pagehide", source)
        runtime_card = source[source.index("function RuntimeCard"):source.index("function ParameterSummary")]
        self.assertNotIn("openFolder", runtime_card)
        self.assertNotIn("children: '열기'", runtime_card)
        self.assertIn("width: '11rem', maxWidth: '11rem', flex: '0 0 11rem'", runtime_card)
        self.assertNotIn("overflow-x-auto", runtime_card)
        self.assertIn("children: '업데이트'", runtime_card)
        self.assertNotIn("children: 'backend'", runtime_card)
        self.assertNotIn("children: '활성 모델'", runtime_card)
        self.assertNotIn("children: 'server'", runtime_card)
        self.assertLess(runtime_card.index("jsx(ServerLogPanel"), runtime_card.index("jsx(CoordinatorStatusPanel"))

    def test_model_inventory_is_runtime_independent_and_marks_prism_models(self) -> None:
        source = (Path(__file__).parent / "desktop" / "plugin.js").read_text(encoding="utf-8")

        downloaded = source[source.index("function DownloadedModelRow"):source.index("function TabBar")]
        self.assertIn("jsx(PrismOnlyHint, { model: model.repo_id })", downloaded)
        self.assertNotIn("HF cache inventory", downloaded)
        self.assertNotIn("아직 plugin에 등록되지 않음", downloaded)
        self.assertIn("title: 'Prism-ML 전용'", source)
        self.assertIn("'aria-label': 'Prism-ML 전용 모델 안내'", source)

        page = source[source.index("function Page"):source.index("export default")]
        self.assertIn("queryKey: [ID, 'hf-models']", page)
        self.assertNotIn("Prism-ML 호환 다운로드 모델이 없습니다.", page)
        self.assertIn("runtime 종류와 관계없이 모두 표시합니다.", page)

    def test_parameter_preset_actions_and_flag_rows_stay_inline(self) -> None:
        source = (Path(__file__).parent / "desktop" / "plugin.js").read_text(encoding="utf-8")
        editor = source[source.index("function ModelSettingsEditor"):source.index("function RegisterWizard")]

        self.assertIn("jsxs('div', { className: 'flex items-stretch gap-2', 'data-testid': 'preset-actions'", editor)
        preset_actions = editor[editor.index("'data-testid': 'preset-actions'"):editor.index("'data-testid': 'preset-name-actions'")]
        self.assertIn("style: { ...themedSelect(), width: 'auto', minWidth: 0, flex: '1 1 auto' }", preset_actions)
        self.assertLess(preset_actions.index("parameter preset 선택"), preset_actions.index("children: '저장'"))
        self.assertLess(preset_actions.index("children: '저장'"), preset_actions.index("children: '삭제'"))

        self.assertIn("jsxs('div', { className: 'mt-2 flex items-stretch gap-2', 'data-testid': 'preset-name-actions'", editor)
        name_actions = editor[editor.index("'data-testid': 'preset-name-actions'"):editor.index("settings.isLoading")]
        self.assertIn("preset 이름", name_actions)
        self.assertIn("children: '이름 변경'", name_actions)
        self.assertIn("새 preset 이름", name_actions)
        self.assertIn("children: '새 preset 만들기'", name_actions)
        self.assertLess(name_actions.index("children: '이름 변경'"), name_actions.index("새 preset 이름"))

        control = source[source.index("function ParameterControl"):source.index("function ModelSettingsEditor")]
        self.assertIn("className: `min-w-0 flex h-8 flex-1", control)
        self.assertIn("children: 'flag'", control)
        self.assertNotIn("값 없는 flag", control)

    def test_model_cards_use_inline_download_panel_and_unregister_action(self) -> None:
        source = (Path(__file__).parent / "desktop" / "plugin.js").read_text(encoding="utf-8")

        execution = source[source.index("function ExecutionProfilesPanel"):source.index("function CoordinatorStatusPanel")]
        self.assertIn("role: 'main'", execution)
        self.assertIn("role: 'compression'", execution)
        self.assertNotIn("activeTab", execution)

        model_row = source[source.index("function ModelRow"):source.index("function SettingsAdder")]
        self.assertIn("children: 'parameter preset'", model_row)
        self.assertIn("children: '적용'", model_row)
        self.assertIn("children: '×'", model_row)
        self.assertIn("disabled: busy || (status?.server_running && isActive)", model_row)
        self.assertNotIn("absolute right-0 top-4", model_row)
        header = model_row[model_row.index("jsxs('div', { className: 'flex flex-wrap items-center"):model_row.index("jsx(ParameterSummary")]
        self.assertIn("children: '×'", header)
        self.assertNotIn("const activate", model_row)
        self.assertNotIn("api('/activate'", model_row)
        self.assertNotIn("children: '선택'", model_row)
        self.assertNotIn("children: '선택됨'", model_row)
        self.assertIn("/registration`, { method: 'DELETE'", model_row)
        self.assertNotIn("window.confirm", model_row)
        self.assertNotIn("children: '삭제'", model_row)
        self.assertNotIn("대기 중", model_row)
        self.assertIn("const compactSize = String(model.size_label || 'unknown').replace(/\\s+/g, '')", model_row)
        self.assertIn("children: '|'", header)
        self.assertIn("`크기(${compactSize})`", model_row)
        self.assertIn("`상태(${activeState})`", model_row)
        self.assertNotIn("용량 -", model_row)
        self.assertNotIn("상태 -", model_row)

        wizard = source[source.index("function RegisterWizard"):source.index("function DownloadedModelRow")]
        self.assertNotIn("fixed inset-0", wizard)
        self.assertIn("aria-labelledby': 'llama-wizard-title'", wizard)
        self.assertIn("'data-testid': 'download-actions'", wizard)
        self.assertIn("children: 'HF 검색'", wizard)
        self.assertIn("children: '선택 항목 다운로드'", wizard)
        self.assertNotIn("children: '취소'", wizard)
        self.assertEqual(wizard.count("${selected.fit}"), 1)
        self.assertNotIn("children: '선택 항목 등록'", wizard)
        self.assertIn("'data-testid': 'registration-actions'", wizard)
        registration_actions = wizard[wizard.index("'data-testid': 'registration-actions'"):]
        self.assertIn("'aria-label': '등록 이름(alias)'", registration_actions)
        self.assertIn("'aria-label': '등록할 GGUF 파일 선택'", registration_actions)
        self.assertIn("children: '선택'", registration_actions)
        self.assertLess(registration_actions.index("'aria-label': '등록 이름(alias)'"), registration_actions.index("'aria-label': '등록할 GGUF 파일 선택'"))
        self.assertLess(registration_actions.index("'aria-label': '등록할 GGUF 파일 선택'"), registration_actions.index("children: '선택'"))
        self.assertIn("body: { repo, paths: selected.paths, alias }", wizard)
        self.assertIn("setAlias(cachedFiles.data.suggested_alias)", wizard)
        self.assertIn("setActionMessage(`등록 실패: ${cause?.message || String(cause)}`)", wizard)

        page = source[source.index("function Page"):source.index("export default")]
        models_branch = page[page.index("tab === 'models'"):page.index("tab === 'parameters'")]
        self.assertIn("wizard ? jsx(RegisterWizard", models_branch)
        self.assertLess(models_branch.index("wizard ? jsx(RegisterWizard"), models_branch.index("models.length ? models.map"))
        self.assertNotIn("wizard ? jsx(RegisterWizard", page[page.index("tab === 'parameters'"):])

    def test_custom_runtime_resolves_directory_and_explicit_executable(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            runtime_dir = root / "prism-llamacpp"
            runtime_dir.mkdir()
            executable = runtime_dir / "llama-server.exe"
            executable.write_bytes(b"placeholder")

            self.assertEqual(api._resolve_server_executable_from_path(runtime_dir), executable)
            self.assertEqual(api._resolve_server_executable_from_path(executable), executable)

    def test_custom_runtime_rejects_missing_or_wrong_path(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            with self.assertRaisesRegex(RuntimeError, "llama-server"):
                api._resolve_server_executable_from_path(root / "missing")

            not_server = root / "llama-cli.exe"
            not_server.write_bytes(b"placeholder")
            with self.assertRaisesRegex(RuntimeError, "llama-server"):
                api._resolve_server_executable_from_path(not_server)

    def test_parameter_preset_can_be_created_listed_applied_and_deleted(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            state_path = root / "state.json"
            options_path = root / "model-options.json"
            runtime_root = root / "runtime"
            catalog = [{
                "key": "ctx-size",
                "name": "--ctx-size",
                "aliases": ["--ctx-size"],
                "value_hint": "N",
                "description": "context size",
                "requires_value": True,
                "default_value": "4096",
                "choices": [],
                "value_kind": "integer",
                "toggle": False,
            }, {
                "key": "no-mmproj",
                "name": "--no-mmproj",
                "aliases": ["--no-mmproj"],
                "value_hint": "",
                "description": "disable automatic mmproj",
                "requires_value": False,
                "default_value": None,
                "choices": [],
                "value_kind": "string",
                "toggle": False,
            }]

            with patch.object(api, "STATE_PATH", state_path), patch.object(api, "OPTIONS_PATH", options_path), \
                    patch.object(api, "PRESET_DB_PATH", root / "presets.db"), \
                    patch.object(api, "RUNTIME_ROOT", runtime_root), patch.object(api, "_option_list", return_value=catalog):
                api.save_model_settings("model-a", {"options": {"ctx-size": "8192"}})
                created = api.create_preset({"name": "Coding", "model_id": "model-a"})
                preset_id = created["preset"]["id"]

                listed = api.presets(model_id="model-b")
                self.assertEqual([item["name"] for item in listed["presets"]], ["Coding"])
                self.assertEqual(listed["presets"][0]["options"], {"ctx-size": "8192"})
                self.assertIsNone(listed["presets"][0]["model_id"])
                renamed = api.rename_preset(preset_id, {"name": "Coding 32K"})
                self.assertEqual(renamed["preset"]["name"], "Coding 32K")
                self.assertEqual(api.presets()["presets"][0]["name"], "Coding 32K")
                updated = api.rename_preset(preset_id, {"options": {"ctx-size": "8192", "no-mmproj": ""}})
                self.assertEqual(updated["preset"]["options"], {"ctx-size": "8192", "no-mmproj": ""})

                api.save_model_settings("model-b", {"options": {"ctx-size": "1024", "no-mmproj": ""}})
                applied = api.apply_preset(preset_id, {"model_id": "model-b"})
                self.assertEqual(applied["options"], {"ctx-size": "8192", "no-mmproj": ""})
                self.assertEqual(api.model_settings("model-b")["options"], {"ctx-size": "8192", "no-mmproj": ""})
                self.assertEqual(api._presets_store().model_preset_map(), {"model-b": preset_id})

                deleted = api.delete_preset(preset_id)
                self.assertEqual(deleted, {"ok": True, "preset_id": preset_id})
                self.assertEqual(api.presets(model_id="model-a")["presets"], [])

    def test_profile_proxy_lock_uses_official_runtime_root(self) -> None:
        proxy_source = (Path(__file__).parent / "dashboard" / "plugin_api.py").read_text(encoding="utf-8")
        self.assertIn('/ "runtimes" / "llamacpp"', proxy_source)
        self.assertNotIn('/ "backends" / "llamacpp"', proxy_source)

    def test_prism_runtime_has_its_own_machine_root(self) -> None:
        self.assertEqual(api.RUNTIME_ROOT, api.MACHINE_ROOT / "runtimes" / "llamacpp")
        self.assertEqual(api.PRISM_RUNTIME_ROOT, api.MACHINE_ROOT / "runtimes" / "prism-ml")

    def test_prism_hides_non_bonsai_registered_models(self) -> None:
        rows = [{"id": "Ornith", "size_bytes": 1, "size_label": "1 B", "hf_repo": "ornith-ai/Ornith-GGUF", "hf_file": "Ornith-Q6_K.gguf", "paths": []}, {"id": "Bonsai", "size_bytes": 1, "size_label": "1 B", "hf_repo": "prism-ml/Ternary-Bonsai-2-27B-gguf", "hf_file": "Ternary-Bonsai-2-27B-PQ2_0.gguf", "paths": []}]
        with patch.object(api, "_model_rows", return_value=rows), patch.object(api, "_runtime_kind", return_value="prism_ml"):
            self.assertEqual([row["id"] for row in api._server_rows()], ["Bonsai"])

    def test_prism_download_inventory_uses_allowed_repository_before_file_selection(self) -> None:
        downloaded = [{"repo_id": "prism-ml/Ternary-Bonsai-2-27B-gguf", "size": "6.7G"}]
        with patch.object(api, "_hf_downloaded_models", return_value=(downloaded, "hf", None)), \
                patch.object(api, "_runtime_kind", return_value="prism_ml"):
            self.assertEqual(api._local_hf_models()["models"], downloaded)

    def test_server_rows_preserve_registered_hugging_face_provenance(self) -> None:
        rows = [{"id": "Bonsai", "size_bytes": 1, "size_label": "1 B",
                 "hf_repo": "prism-ml/Ternary-Bonsai-2-27B-gguf",
                 "hf_file": "Ternary-Bonsai-2-27B-PQ2_0.gguf", "paths": []}]
        with patch.object(api, "_model_rows", return_value=rows), patch.object(api, "_runtime_kind", return_value="prism_ml"):
            row = api._server_rows()[0]
        self.assertEqual(row["hf_repo"], "prism-ml/Ternary-Bonsai-2-27B-gguf")
        self.assertEqual(row["hf_file"], "Ternary-Bonsai-2-27B-PQ2_0.gguf")

    def test_legacy_model_bound_preset_is_migrated_to_shared_scope(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            state_path = Path(raw_root) / "state.json"
            preset_db_path = Path(raw_root) / "presets.db"
            state_path.write_text('{"parameter_presets":{"legacy":{"name":"Legacy","model_id":"old-model","options":{}}}}', encoding="utf-8")
            with patch.object(api, "STATE_PATH", state_path), patch.object(api, "PRESET_DB_PATH", preset_db_path):
                listed = api.presets()
            self.assertIsNone(listed["presets"][0]["model_id"])
            self.assertTrue(preset_db_path.is_file())
            self.assertNotIn("parameter_presets", api._read_json(state_path, {}))

    def test_failed_start_cleanup_preserves_server_log(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            state_path = root / "state.json"
            log_path = root / "logs" / "llama-server.log"
            log_path.parent.mkdir(parents=True)
            log_path.write_text("fatal startup detail", encoding="utf-8")
            with patch.object(api, "STATE_PATH", state_path), patch.object(api, "SERVER_LOG_PATH", log_path), \
                    patch.object(api, "_unregister_custom_endpoint"), patch.object(api, "_pid_alive", return_value=False):
                api._stop_server(preserve_log=True)
            self.assertTrue(log_path.exists())
            self.assertEqual(log_path.read_text(encoding="utf-8"), "fatal startup detail")

    def test_prism_preset_apply_omits_unmanaged_speculative_options(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            state_path = root / "state.json"
            options_path = root / "model-options.json"
            catalog = [
                {"key": "ctx-size", "name": "--ctx-size", "aliases": ["--ctx-size"], "value_hint": "N", "description": "context", "requires_value": True, "default_value": "4096", "choices": [], "value_kind": "integer", "toggle": False},
                {"key": "spec-type", "name": "--spec-type", "aliases": ["--spec-type"], "value_hint": "TYPE", "description": "speculative type", "requires_value": True, "default_value": None, "choices": ["draft-mtp"], "value_kind": "choice", "toggle": False},
                {"key": "spec-draft-n-max", "name": "--spec-draft-n-max", "aliases": ["--spec-draft-n-max"], "value_hint": "N", "description": "draft tokens", "requires_value": True, "default_value": None, "choices": [], "value_kind": "integer", "toggle": False},
            ]
            with patch.object(api, "STATE_PATH", state_path), patch.object(api, "OPTIONS_PATH", options_path), \
                    patch.object(api, "PRESET_DB_PATH", root / "presets.db"), \
                    patch.object(api, "_option_list", return_value=catalog), patch.object(api, "_runtime_kind", return_value="prism_ml"):
                created = api.create_preset({"name": "shared", "options": {"ctx-size": "8192", "spec-type": "draft-mtp", "spec-draft-n-max": "1"}})
                applied = api.apply_preset(created["preset"]["id"], {"model_id": "bonsai"})
            self.assertEqual(applied["options"], {"ctx-size": "8192"})
            self.assertEqual(applied["omitted_options"], ["spec-draft-n-max", "spec-type"])

    def test_model_card_exposes_applied_preset_and_running_state_before_server_start(self) -> None:
        source = (Path(__file__).parent / "desktop" / "plugin.js").read_text(encoding="utf-8")
        self.assertNotIn("모델 시작 preset", source)
        self.assertIn("model_presets", source)
        self.assertIn("children: presetsQuery.isLoading ? 'preset 불러오는 중…' : 'preset 선택'", source)
        self.assertIn("/presets/${encodeURIComponent(selectedPresetId)}/apply", source)
        self.assertIn("const activeState = status?.server_running && isActive ? 'running' : 'stopped'", source)
        self.assertIn("children: `상태(${activeState})`", source)
        self.assertNotIn("children: '소스'", source)
        self.assertNotIn("server 사용 중", source)

    def test_prism_rejects_non_bonsai_registration(self) -> None:
        with self.assertRaisesRegex(Exception, "Prism-ML"):
            api._require_compatible_model("prism_ml", "ornith-ai/Ornith-1.5-35B-A3B-GGUF", ["Ornith-1.5-35B-Q6_K.gguf"])

    def test_shared_backend_uses_machine_state_and_profile_proxy(self) -> None:
        self.assertEqual(api.STATE_PATH, api.RUNTIME_ROOT / "state.json")
        proxy_path = Path(__file__).parent / "dashboard" / "plugin_api.py"
        coordinator_path = Path(__file__).parent / "dashboard" / "coordinator_server.py"
        self.assertIn("Profile-local proxy", proxy_path.read_text(encoding="utf-8"))
        coordinator_source = coordinator_path.read_text(encoding="utf-8")
        self.assertIn("from backend_impl import router", coordinator_source)
        self.assertIn("if COORDINATOR_STOP_MARKER.exists():", coordinator_source)
        from dashboard import coordinator_server, plugin_api as profile_proxy
        self.assertIn("/events", {getattr(route, "path", None) for route in coordinator_server.app.routes})
        self.assertIn("/events", {getattr(route, "path", None) for route in profile_proxy.router.routes})

    def test_coordinator_event_socket_pushes_bounded_bus_events(self) -> None:
        from fastapi.testclient import TestClient
        from dashboard import coordinator_server

        client = TestClient(coordinator_server.app)
        with client.websocket_connect("/events") as websocket:
            connected = websocket.receive_json()
            coordinator_server.EVENT_BUS.publish("probe", refresh=["metrics"])
            event = websocket.receive_json()

        self.assertEqual(connected["type"], "connected")
        self.assertEqual(event["type"], "probe")
        self.assertEqual(event["refresh"], ["metrics"])

    def test_custom_endpoint_routes_through_coordinator(self) -> None:
        with patch.object(api, "_profile_config_paths", return_value=[]), patch.object(api, "_load_options", return_value={}):
            endpoint = api._register_custom_endpoint(18434, "bonsai")
            self.assertEqual(endpoint["provider"], "custom")
            self.assertEqual(endpoint["base_url"], "http://127.0.0.1:18380/v1")
            self.assertEqual(endpoint["worker_base_url"], "http://127.0.0.1:18434/v1")

    def test_execution_profile_api_persists_official_and_prism_bindings(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            state_path = Path(raw_root) / "state.json"
            with patch.object(api, "STATE_PATH", state_path):
                api._save_state({
                    **api._default_state(),
                    "models": {
                        "large": {},
                        "small": {
                            "hf_repo": "prism-ml/Ternary-Bonsai-2-27B-gguf",
                            "hf_file": "Ternary-Bonsai-2-27B-PQ2_0.gguf",
                            "paths": [],
                        },
                    },
                })
                api.save_execution_profile("main", {"runtime_kind": "official", "model_id": "large"})
                result = api.save_execution_profile("compression", {"runtime_kind": "prism_ml", "model_id": "small"})

            self.assertEqual(result["profiles"]["main"]["model_id"], "large")
            self.assertEqual(result["profiles"]["compression"]["model_id"], "small")
            self.assertEqual(result["execution_mode"], "exclusive_swap")

        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            runtime_dir = root / "prism-llamacpp"
            runtime_dir.mkdir()
            executable = runtime_dir / "llama-server.exe"
            executable.write_bytes(b"placeholder")
            state_path = root / "state.json"

            with patch.object(api, "STATE_PATH", state_path), patch.object(api, "_server_executable", return_value=executable):
                saved = api.save_runtime({"mode": "custom", "path": str(runtime_dir)})
                self.assertEqual(saved["mode"], "custom")
                self.assertEqual(saved["executable"], str(executable))
                self.assertEqual(api.runtime_info()["mode"], "custom")
                self.assertEqual(api.runtime_info()["path"], str(runtime_dir.resolve()))
    def test_flag_parameter_accepts_empty_value(self) -> None:
        flag = {"key": "no-mmproj", "name": "--no-mmproj", "aliases": ["--no-mmproj"],
                "value_hint": "", "description": "disable automatic mmproj", "requires_value": False,
                "default_value": None, "choices": [], "value_kind": "string", "toggle": False}
        with patch.object(api, "_option_list", return_value=[flag]):
            self.assertEqual(api._normalize_options({"no-mmproj": ""}), {"no-mmproj": ""})

    def test_option_catalog_marks_decimal_and_no_value_flags(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            executable = root / "llama-server.exe"
            executable.write_bytes(b"placeholder")
            help_text = """--temp N                             temperature (default: 0.80)
--mmproj-auto, --no-mmproj, --no-mmproj-auto
                                        whether to use multimodal projector\n"""
            result = subprocess.CompletedProcess([str(executable), "--help"], 0, help_text, "")
            with patch.object(api, "_server_executable", return_value=executable), \
                    patch.object(api, "OPTION_METADATA_CACHE_PATH", root / "options.json"), \
                    patch.object(api, "_option_catalog_cache", None), \
                    patch.object(api.subprocess, "run", return_value=result):
                catalog = {item["key"]: item for item in api._option_list()}
            self.assertEqual(catalog["temp"]["value_kind"], "number")
            self.assertFalse(catalog["no-mmproj"]["requires_value"])
            self.assertFalse(catalog["mmproj-auto"]["requires_value"])

    def test_model_card_preset_and_flag_parameter_validation(self) -> None:
        desktop_source = (Path(__file__).parent / "desktop" / "plugin.js").read_text(encoding="utf-8")
        self.assertIn("import React from 'react'", desktop_source)
        self.assertIn("import pluginSdk from '@hermes/plugin-sdk'", desktop_source)
        self.assertNotIn("react/jsx-runtime", desktop_source)
        self.assertIn("const jsxs = jsx", desktop_source)
        self.assertIn("const { createElement, useEffect, useLayoutEffect, useMemo, useRef, useState } = React", desktop_source)
        self.assertIn("el.scrollTop = el.scrollHeight", desktop_source)
        self.assertIn("jsx('pre', { ref: logRef", desktop_source)
        self.assertIn("const validateDraft = (next, extraMetadata = {})", desktop_source)
        self.assertIn("persistPreset(next)", desktop_source)
        self.assertIn("if (option && !option.requires_value) return jsx('span'", desktop_source)
        self.assertIn("option?.value_kind === 'number' ? 'any'", desktop_source)
        self.assertIn("allowEmpty: true", desktop_source)
        self.assertIn("option.choices?.length ? option.choices.join(' | ')", desktop_source)
        self.assertIn("method: 'PATCH', body: { name: presetRename.trim() }", desktop_source)
        self.assertIn("children: '이름 변경'", desktop_source)
        self.assertNotIn("모델 설정 저장", desktop_source)
        self.assertIn("setDraft(next)", desktop_source)
        self.assertIn("appliedPresetId: data?.model_presets?.[activeModel.id] || ''", desktop_source)
        self.assertNotIn("queryKey: [ID, 'model-card-presets']", desktop_source)
        self.assertNotIn("queryKey: [ID, 'parameter-presets']", desktop_source)
        self.assertIn("queryKey: [ID, 'presets']", desktop_source)
        self.assertIn("refetchOnMount: 'always'", desktop_source)
        self.assertIn("queryClient.invalidateQueries({ queryKey: [ID, 'presets'] })", desktop_source)
        self.assertIn("queryClient.invalidateQueries({ queryKey: [ID, 'status'] })", desktop_source)
        self.assertIn("children: preset.name", desktop_source)
        model_row = desktop_source[desktop_source.index("function ModelRow"):desktop_source.index("function SettingsAdder")]
        self.assertLess(model_row.index("jsx(ParameterSummary"), model_row.index("'data-testid': 'model-card-presets'"))


    def test_prism_profile_resolves_managed_runtime_after_global_official_selection(self) -> None:
        class Backend:
            key = "prism_ml"

            @staticmethod
            def managed_root(machine_root: Path) -> Path:
                return machine_root / "prism-ml"

            @staticmethod
            def resolve_executable(raw_path: str | Path) -> Path:
                return Path(raw_path) / "llama-server.exe"

        state = {"runtime_kind": "prism_ml", "runtime_path": None, "custom_runtime_path": None}
        with patch.object(api, "_state", return_value=state), \
                patch.object(api, "get_backend", return_value=Backend()):
            self.assertEqual(
                api._server_executable(),
                api.MACHINE_ROOT / "prism-ml" / "llama-server.exe",
            )

    def test_server_running_rejects_health_from_listener_not_owned_by_recorded_worker(self) -> None:
        import types

        class Process:
            def __init__(self, pid: int) -> None:
                self.pid = pid

            def create_time(self) -> float:
                return 100.0

            def exe(self) -> str:
                return sys.executable

            def net_connections(self, kind: str):
                return []

        state = {
            "pid": 42,
            "port": 18434,
            "worker_identity": {"pid": 42, "create_time": 100.0, "executable": sys.executable},
        }
        fake_psutil = types.SimpleNamespace(Process=Process, CONN_LISTEN="LISTEN")
        with patch.object(api, "_state", return_value=state), \
                patch.object(api, "_pid_alive", return_value=True), \
                patch.object(api, "_health", return_value=True), \
                patch.dict(sys.modules, {"psutil": fake_psutil}):
            self.assertFalse(api._is_server_running())

    def test_activity_log_tail_projects_unified_execution_log_and_retains_raw_evidence(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as raw_root:
            log_path = Path(raw_root) / "activity.log"
            log_path.write_text(
                "I srv spawning server instance with name=main-local on port 19001\n"
                "[19001] slot prompt processing, n_tokens = 100, progress = 0.50, 25.0 tokens per second\n",
                encoding="utf-8",
            )
            with patch.object(api, "ACTIVITY_LOG_PATH", log_path):
                result = api._server_log_tail(10)

        self.assertEqual(result["lines"], [
            "[Main] 모델 로딩 중",
            "[Main] prompt 처리 50% · 100 tok · 25.0 tok/s",
        ])
        self.assertEqual(len(result["raw_lines"]), 2)
        self.assertEqual(result["role"], "activity")

    def test_desktop_lifecycle_contract_stops_coordinator_after_last_lease(self) -> None:
        source = (Path(__file__).parent / "dashboard" / "coordinator_server.py").read_text(encoding="utf-8")
        event_source = (Path(__file__).parent / "dashboard" / "application" / "event_bus.py").read_text(encoding="utf-8")
        desktop_source = (Path(__file__).parent / "desktop" / "plugin.js").read_text(encoding="utf-8")
        self.assertIn('app.post("/lifecycle/lease")', source)
        self.assertIn('app.delete("/lifecycle/lease")', source)
        self.assertIn("_desktop_leases.should_shutdown(execution_busy())", source)
        self.assertIn("shutdown_machine_runtime()", source)
        self.assertIn("server.should_exit = True", source)
        self.assertNotIn("subprocess", event_source)
        self.assertIn("daemon=True", event_source)
        self.assertIn("disposeSocket()", desktop_source)
        self.assertIn("stopHealthCheck()", desktop_source)
        self.assertIn("timers.clear()", desktop_source)

    def test_worker_spawn_is_bound_to_coordinator_lifetime(self) -> None:
        source = (Path(__file__).parent / "dashboard" / "backend_impl.py").read_text(encoding="utf-8")
        self.assertIn("bind_child_to_owner_lifetime(process)", source)

    def test_backend_exit_does_not_stop_machine_server(self) -> None:
        with patch.object(api, "_stop_server") as stop_server:
            api._shutdown_server_on_backend_exit()
        stop_server.assert_not_called()

    @unittest.skipUnless(os.name == "nt", "Windows Job Objects are Windows-only")
    def test_child_exits_when_owner_process_exits(self) -> None:
        lifecycle_path = Path(__file__).parent / "dashboard" / "child_lifecycle.py"
        self.assertTrue(lifecycle_path.exists(), "missing Windows child lifecycle helper")
        owner_script = """
import importlib.util
import subprocess
import sys
from pathlib import Path

module_path = Path(sys.argv[1])
spec = importlib.util.spec_from_file_location("llamacpp_child_lifecycle", module_path)
assert spec and spec.loader
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
module.bind_child_to_owner_lifetime(child)
print(child.pid, flush=True)
"""
        owner = subprocess.Popen(
            [sys.executable, "-c", owner_script, str(lifecycle_path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        assert owner.stdout is not None
        child_pid = int(owner.stdout.readline().strip())
        owner.terminate()
        owner.wait(timeout=10)
        if owner.stdout is not None:
            owner.stdout.close()
        if owner.stderr is not None:
            owner.stderr.close()

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {child_pid}", "/FO", "CSV", "/NH"],
                capture_output=True,
                text=False,
                check=False,
            )
            output = (result.stdout or b"").decode(errors="replace")
            if result.returncode == 0 and str(child_pid) not in output:
                return
            time.sleep(0.1)
        subprocess.run(["taskkill", "/PID", str(child_pid), "/T", "/F"], capture_output=True, check=False)
        self.fail("child process survived its owner process")


if __name__ == "__main__":
    unittest.main()
