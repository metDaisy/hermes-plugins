"""Unit tests for cohesive llamacpp dashboard application modules."""
from __future__ import annotations

import unittest


class ManagedEndpointConfigTests(unittest.TestCase):
    def test_replaces_only_the_plugin_managed_provider_block(self) -> None:
        from dashboard.application.profile_endpoint import ManagedEndpointConfig

        config = ManagedEndpointConfig(
            key="llamacpp-local",
            begin="# BEGIN llamacpp endpoint (managed)",
            end="# END llamacpp endpoint (managed)",
        )
        existing = (
            "providers:\n"
            "  other:\n"
            "    api: https://example.test\n"
            "  # BEGIN llamacpp endpoint (managed)\n"
            "    llamacpp-local:\n"
            "      name: stale\n"
            "  # END llamacpp endpoint (managed)\n"
        )

        updated = config.upsert(existing, "http://127.0.0.1:18434/v1", "model-a", 8192)

        self.assertIn("  other:\n", updated)
        self.assertIn('      api: "http://127.0.0.1:18434/v1"\n', updated)
        self.assertIn('      default_model: "model-a"\n', updated)
        self.assertNotIn("name: stale", updated)
        removed, changed = config.remove(updated)
        self.assertTrue(changed)
        self.assertIn("  other:\n", removed)
        self.assertNotIn("llamacpp-local", removed)


class ServerOptionCatalogTests(unittest.TestCase):
    def test_parses_toggle_aliases_and_numeric_defaults(self) -> None:
        from dashboard.application.option_catalog import ServerOptionCatalog

        catalog = ServerOptionCatalog.parse_help(
            "--temp N                             temperature (default: 0.80)\n"
            "--mmproj-auto, --no-mmproj, --no-mmproj-auto\n"
            "                                        whether to use multimodal projector\n"
        )

        by_key = {option["key"]: option for option in catalog}
        self.assertEqual(by_key["temp"]["value_kind"], "number")
        self.assertFalse(by_key["no-mmproj"]["requires_value"])
        self.assertFalse(by_key["mmproj-auto"]["requires_value"])


class RegisteredModelServiceTests(unittest.TestCase):
    def test_registered_local_paths_are_deduplicated_and_resolved(self) -> None:
        from dashboard.application.model_registry import RegisteredModelService

        with self.subTest("registered local model"):
            import tempfile
            from pathlib import Path

            with tempfile.TemporaryDirectory() as raw_root:
                model_path = Path(raw_root) / "model.gguf"
                model_path.write_bytes(b"gguf")
                state = {"active_model_id": "model", "models": {}}
                service = RegisteredModelService(lambda: state, lambda value: state.update(value), lambda _: ([], None))
                service.register("model", [model_path, model_path], owned=False)

                rows = service.rows()
                self.assertEqual(rows[0]["paths"], [str(model_path.resolve())])
                self.assertEqual(service.active_path("model"), model_path.resolve())


class ParameterRouteAdapterTests(unittest.TestCase):
    def test_keeps_settings_and_preset_paths_with_search_projection(self) -> None:
        from dashboard.routes.parameter_routes import ParameterRouteContext, create_router

        class Parameters:
            def presets(self):
                return []

        option = {"key": "ctx-size", "name": "--ctx-size", "description": "context"}
        context = ParameterRouteContext(
            options=lambda: [option], executable=lambda: None, parameters=lambda: Parameters(),
            server_requires_restart=lambda: False, runtime_kind=lambda _state: "official", state=lambda: {},
        )
        routes = {route.path: route.endpoint for route in create_router(context).routes}

        self.assertTrue({"/settings", "/settings/{model_id}", "/presets", "/presets/{preset_id}/apply", "/presets/{preset_id}"}.issubset(routes))
        self.assertEqual(routes["/settings"]("ctx", 50)["options"], [option])


class ServerRouteAdapterTests(unittest.TestCase):
    def test_keeps_server_and_job_paths_and_rejects_start_without_active_model(self) -> None:
        from dashboard.routes.server_routes import ServerRouteContext, create_router
        from fastapi import HTTPException

        context = ServerRouteContext(
            state=lambda: {"active_model_id": None}, stop=lambda: None, start=lambda: None,
            create_job=lambda kind, detail: {"kind": kind, "detail": detail, "job_id": "job"},
            launch=lambda _job, _work, _name: None, finish=lambda _job, _detail: None,
            recent_jobs=lambda: [], find_job=lambda _job_id: None, logs=lambda _limit: {},
        )
        routes = {route.path: route.endpoint for route in create_router(context).routes}

        self.assertTrue({"/server", "/jobs", "/jobs/{job_id}", "/logs"}.issubset(routes))
        with self.assertRaises(HTTPException) as raised:
            routes["/server"]({"action": "start"})
        self.assertEqual(raised.exception.status_code, 400)


class ModelRouteAdapterTests(unittest.TestCase):
    def test_keeps_model_paths_and_maps_workflow_failure_at_http_adapter(self) -> None:
        from dashboard.routes.model_routes import ModelRouteContext, create_router
        from fastapi import HTTPException

        class Workflow:
            def search(self, _query, _limit):
                raise RuntimeError("offline")

        context = ModelRouteContext(
            lifecycle=lambda: object(), workflow=lambda: Workflow(),
            create_job=lambda kind, detail: {"kind": kind, "detail": detail, "job_id": "job"},
            launch=lambda _job, _work, _name: None, finish=lambda _job, _detail: None,
            begin_download=lambda _job, _detail: None,
        )
        router = create_router(context)
        paths = {route.path: route.endpoint for route in router.routes}

        self.assertTrue({"/activate", "/eject", "/models/{model_id}", "/search", "/repo", "/download-browsed", "/register", "/sideload", "/hf-models", "/hf-models/files", "/hf-models/delete"}.issubset(paths))
        with self.assertRaises(HTTPException) as raised:
            paths["/search"]("test", 20)
        self.assertEqual(raised.exception.status_code, 502)


class PrismRuntimeInstallerTests(unittest.TestCase):
    def test_installs_cpu_runtime_from_bonsai_setup_metadata(self) -> None:
        from dashboard.application.prism_runtime import PrismRuntimeInstaller

        import tempfile
        from pathlib import Path
        import zipfile

        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root) / "prism"
            source = Path(raw_root) / "server.zip"
            with zipfile.ZipFile(source, "w") as package:
                package.writestr("llama-server.exe", b"server")
            state: dict[str, object] = {}

            def clone(_git, _url, destination):
                (destination / "setup.ps1").parent.mkdir(parents=True)
                (destination / "setup.ps1").write_text('$ReleaseTag = "b123"\n$CudaTag = "13.3"\n', encoding="utf-8")
                return 0

            installer = PrismRuntimeInstaller(
                root=root, load_state=lambda: state, save_state=lambda value: state.update(value),
                find_git=lambda: "git", clone=clone, backend_detector=lambda: "cpu",
                download=lambda _url, destination, *_: destination.write_bytes(source.read_bytes()),
                extract=lambda archive, destination: zipfile.ZipFile(archive).extractall(destination),
                resolve_executable=lambda path: path / "bin" / "cpu" / "llama-server.exe",
            )
            job = {"job_id": "job"}

            tag, backend = installer.install(job)

            self.assertEqual((tag, backend), ("b123", "cpu"))
            self.assertEqual(state["prism_release_tag"], "b123")
            self.assertTrue((root / "bin" / "cpu" / "llama-server.exe").is_file())


class RuntimeInspectorTests(unittest.TestCase):
    def test_reconciles_dead_server_state_and_builds_runtime_status_projection(self) -> None:
        from dashboard.application.runtime_inspector import RuntimeInspector

        from pathlib import Path
        from types import SimpleNamespace
        state: dict[str, object] = {
            "runtime_kind": "official", "installed_tag": "b1", "installed_backend": "cuda",
            "pid": 42, "port": 18434, "custom_endpoint": {"key": "stale"}, "models": {},
        }
        unregistered: list[bool] = []
        inspector = RuntimeInspector(
            load_state=lambda: state, save_state=lambda value: state.update(value),
            runtime_kind=lambda current: str(current["runtime_kind"]),
            executable=lambda: Path("server.exe"), installed_target=lambda: ("b1", "cuda"),
            executable_in=lambda _root: Path("server.exe"), backend_detector=lambda: "cuda",
            backend=lambda _kind: SimpleNamespace(description="desc", repository="repo"),
            backend_view=lambda _kind, _state, _root: SimpleNamespace(label="official", managed_root="root", version="b1", install_action="install"),
            machine_root=Path("machine"), runtime_root=Path("runtime"), prism_root=Path("prism"),
            pid_alive=lambda _pid: False, health=lambda _port: False,
            unregister_endpoint=lambda: unregistered.append(True), devices=lambda: [{"id": "0"}],
            server_rows=lambda: [{"id": "model"}], models_root=Path("models"),
        )

        status = inspector.status()

        self.assertFalse(status["server_running"])
        self.assertEqual(status["runtime_version"], "b1")
        self.assertEqual(status["devices"], [{"id": "0"}])
        self.assertIsNone(state["pid"])
        self.assertEqual(unregistered, [True])


class DeviceDiscoveryServiceTests(unittest.TestCase):
    def test_parses_llama_server_device_memory_into_bytes(self) -> None:
        from dashboard.application.device_discovery import DeviceDiscoveryService

        from pathlib import Path
        service = DeviceDiscoveryService(
            executable=lambda: Path("server.exe"),
            run=lambda _argv: (0, "Available devices:\n0: RTX (16 GiB, 12 GiB free)\n"),
        )

        self.assertEqual(service.devices(), [{
            "id": "0", "name": "RTX", "memory": "16 GiB, 12 GiB free",
            "vram_total_bytes": 16 * 1024 ** 3, "vram_free_bytes": 12 * 1024 ** 3,
        }])


class JobManagerTests(unittest.TestCase):
    def test_tracks_recent_jobs_and_converts_background_failures_to_terminal_state(self) -> None:
        from dashboard.application.job_manager import JobManager

        jobs: dict[str, dict[str, object]] = {}

        class InlineThread:
            def __init__(self, target, **_): self._target = target
            def start(self): self._target()

        manager = JobManager(
            jobs=jobs, lock=__import__("threading").RLock(), new_id=iter(["first", "second"]).__next__,
            now=iter([1.0, 2.0]).__next__, thread_factory=InlineThread,
        )
        first = manager.create("download", "first")
        second = manager.create("runtime", "second")
        manager.finish(first, "done")
        manager.launch(second, "test-job", lambda: (_ for _ in ()).throw(RuntimeError("boom")))

        self.assertEqual([job["job_id"] for job in manager.recent()], ["second", "first"])
        self.assertEqual(manager.find("second")["status"], "error")
        self.assertEqual(manager.find("second")["error"], "boom")
        self.assertIsNone(manager.find("missing"))


class ModelLifecycleServiceTests(unittest.TestCase):
    def test_activate_switch_delete_and_sideload_share_model_lifecycle_invariants(self) -> None:
        from dashboard.application.model_lifecycle import ModelLifecycleService

        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            model_a = root / "model-a.gguf"
            model_b = root / "model-b.gguf"
            model_a.write_bytes(b"a")
            model_b.write_bytes(b"b")
            state: dict[str, object] = {
                "active_model_id": "model-a",
                "models": {
                    "model-a": {"paths": [str(model_a)], "owned": False},
                    "model-b": {"paths": [str(model_b)], "owned": True},
                },
            }

            class Registry:
                def rows(self):
                    return [
                        {"id": "model-a", "paths": [str(model_a)], "size_bytes": 1, "size_label": "1 B", "active": True},
                        {"id": "model-b", "paths": [str(model_b)], "size_bytes": 1, "size_label": "1 B", "active": False},
                    ]
                def active_path(self, model_id):
                    if model_id == "model-a": return model_a
                    if model_id == "model-b": return model_b
                    raise RuntimeError("model is not registered: " + model_id)
                def register(self, model_id, paths, owned, **_):
                    state["models"][model_id] = {"paths": [str(path) for path in paths], "owned": owned}

            stopped: list[bool] = []
            service = ModelLifecycleService(
                registry=Registry(), load_state=lambda: state, save_state=lambda value: state.update(value),
                runtime_kind=lambda _: "official", accepts=lambda *_: True,
                server_running=lambda: True, stop_server=lambda: stopped.append(True),
                models_root=root, model_id=lambda path: path.stem,
            )

            with self.assertRaisesRegex(RuntimeError, "stop llama-server"):
                service.activate("model-b")
            service.eject("model-a")
            self.assertEqual(stopped, [True])
            self.assertIsNone(state["active_model_id"])
            service.delete("model-b")
            self.assertFalse(model_b.exists())
            self.assertNotIn("model-b", state["models"])
            side = root / "side.gguf"
            side.write_bytes(b"s")
            self.assertEqual(service.sideload(side)["model_id"], "side")


class ParameterSettingsServiceTests(unittest.TestCase):
    def test_prism_preset_merge_preserves_existing_values_and_omits_spec_options(self) -> None:
        from dashboard.application.parameter_settings import ParameterSettingsService

        state = {"parameter_presets": {"shared": {"name": "shared", "options": {"ctx-size": "8192", "spec-type": "draft"}}}}
        stored_options = {"model": {"no-mmproj": ""}}
        catalog = [
            {"key": "ctx-size", "requires_value": True, "choices": [], "value_kind": "integer"},
            {"key": "no-mmproj", "requires_value": False, "choices": [], "value_kind": "string"},
            {"key": "spec-type", "requires_value": True, "choices": [], "value_kind": "string"},
        ]
        service = ParameterSettingsService(
            catalog=lambda: catalog,
            canonical=lambda _, value: str(value),
            load_options=lambda: stored_options,
            save_options=lambda value: stored_options.update(value),
            load_state=lambda: state,
            save_state=lambda value: state.update(value),
            error=lambda message: ValueError(message),
            now=lambda: 1.0,
            new_id=lambda: "new",
        )

        applied = service.apply_preset("shared", "model", runtime_kind="prism_ml", requires_restart=False)

        self.assertEqual(applied["options"], {"ctx-size": "8192", "no-mmproj": ""})
        self.assertEqual(applied["omitted_options"], ["spec-type"])


class OfficialRuntimeServiceTests(unittest.TestCase):
    def test_resolves_complete_latest_build_and_installs_verified_runtime(self) -> None:
        from dashboard.application.official_runtime import OfficialRuntimeService

        import tempfile
        from pathlib import Path
        import zipfile

        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root) / "runtimes"
            state: dict[str, object] = {"tag": "latest", "installed_tag": "", "installed_backend": ""}
            source_archive = Path(raw_root) / "source.zip"
            with zipfile.ZipFile(source_archive, "w") as package:
                package.writestr("bin/llama-server.exe", b"server")

            service = OfficialRuntimeService(
                root=root,
                load_state=lambda: state,
                save_state=lambda value: state.update(value),
                release_loader=lambda: [
                    {"tag_name": "b101", "assets": [{"name": "llama-b101-bin-win-cpu-x64.zip"}]},
                    {"tag_name": "b102", "assets": [{"name": "unrelated.zip"}]},
                ],
                backend_detector=lambda: "cpu",
                platform_name=lambda: "windows",
                architecture=lambda: "x64",
            )

            self.assertEqual(service.resolve_target(force_latest=True), ("b101", "cpu"))
            job: dict[str, object] = {}
            service.install(
                "b101", "cpu", job,
                download=lambda _url, destination, _job, _floor, _ceiling: destination.write_bytes(source_archive.read_bytes()),
            )

            self.assertEqual(state["installed_tag"], "b101")
            self.assertEqual(service.installed_target(), ("b101", "cpu"))
            self.assertTrue((root / "b101" / "cpu" / "manifest.json").is_file())


class ServerLifecycleServiceTests(unittest.TestCase):
    def test_stop_clears_owned_state_unregisters_endpoint_and_removes_log(self) -> None:
        from dashboard.application.server_lifecycle import ServerLifecycleService

        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as raw_root:
            log_path = Path(raw_root) / "llama-server.log"
            log_path.write_text("diagnostic", encoding="utf-8")
            state: dict[str, object] = {"pid": 42, "custom_endpoint": {"key": "llamacpp-local"}}
            stopped: list[int] = []
            unregistered: list[bool] = []
            service = ServerLifecycleService(
                load_state=lambda: state,
                save_state=lambda value: state.update(value),
                pid_alive=lambda pid: pid == 42,
                terminate=lambda pid: stopped.append(pid),
                unregister_endpoint=lambda: unregistered.append(True),
                log_path=log_path,
            )

            service.stop()

            self.assertEqual(stopped, [42])
            self.assertIsNone(state["pid"])
            self.assertIsNone(state["custom_endpoint"])
            self.assertEqual(unregistered, [True])
            self.assertFalse(log_path.exists())


class HuggingFaceCacheServiceTests(unittest.TestCase):
    def test_groups_cached_gguf_parts_and_excludes_projectors(self) -> None:
        from dashboard.application.huggingface_cache import HuggingFaceCacheService

        import json
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as raw_root:
            snapshot = Path(raw_root)
            (snapshot / "model-00001-of-00002.gguf").write_bytes(b"a")
            (snapshot / "model-00002-of-00002.gguf").write_bytes(b"bc")
            (snapshot / "mmproj.gguf").write_bytes(b"ignore")
            service = HuggingFaceCacheService(
                find_cli=lambda: "hf",
                run=lambda _argv: (0, json.dumps([{"repo_id": "owner/repo", "snapshot_path": str(snapshot)}])),
            )

            groups, warning = service.cached_files("owner/repo")

            self.assertIsNone(warning)
            self.assertEqual(groups, [{"label": "model", "paths": ["model-00001-of-00002.gguf", "model-00002-of-00002.gguf"], "total_bytes": 3, "fit": "downloaded"}])


class JsonHttpClientTests(unittest.TestCase):
    def test_sends_hf_token_only_to_huggingface_hosts(self) -> None:
        from dashboard.application.json_http import JsonHttpClient

        seen: list[object] = []

        class Response:
            def __enter__(self): return self
            def __exit__(self, *_): return False
            def read(self): return b'{"ok": true}'

        client = JsonHttpClient(lambda: "token", lambda request: (seen.append(request) or Response()), lambda _: None)

        self.assertEqual(client.get("https://huggingface.co/api/models/a"), {"ok": True})
        self.assertEqual(client.get("https://api.github.com/repos/ggml-org/llama.cpp"), {"ok": True})
        self.assertEqual(seen[0].get_header("Authorization"), "Bearer token")
        self.assertIsNone(seen[1].get_header("Authorization"))


class HuggingFaceModelWorkflowTests(unittest.TestCase):
    def test_groups_remote_files_filters_search_and_registers_only_cached_selection(self) -> None:
        from dashboard.application.huggingface_model_workflow import HuggingFaceModelWorkflow

        state: dict[str, object] = {"runtime_kind": "official", "prism_release_tag": "", "models": {}}
        registered: list[tuple[object, ...]] = []
        workflow = HuggingFaceModelWorkflow(
            load_state=lambda: state,
            runtime_kind=lambda current: str(current["runtime_kind"]),
            accepts=lambda _kind, repo, paths, _version: repo != "blocked/repo" and bool(paths),
            cache_models=lambda: ([{"repo_id": "owner/repo", "size": "1 GB"}], "hf", None),
            cached_files=lambda repo: ([{"label": "model", "paths": ["model.gguf"], "total_bytes": 3, "fit": "downloaded"}], None),
            http_json=lambda url: (
                [{"path": "model.gguf", "size": 3}]
                if "/tree/main" in url else [
                    {"id": "owner/repo", "downloads": 12},
                    {"id": "blocked/repo", "downloads": 99},
                ]
            ),
            download=lambda *_: None,
            register=lambda *args, **kwargs: registered.append(args),
            remove_cache=lambda _repo: True,
            model_id=lambda path: path.stem,
        )

        self.assertEqual(workflow.search("model", 20), {"hits": [{"repo": "owner/repo", "downloads": 12}]})
        self.assertEqual(workflow.repository("owner/repo"), {"files": [{"label": "model", "paths": ["model.gguf"], "total_bytes": 3, "fit": "available"}]})
        registered_model = workflow.register("owner/repo", ["model.gguf"])
        self.assertEqual(registered_model["model_id"], "model")
        self.assertEqual(registered[0][0], "model")
        with self.assertRaisesRegex(RuntimeError, "HF cache"):
            workflow.register("owner/repo", ["missing.gguf"])


class HuggingFaceDownloadServiceTests(unittest.TestCase):
    def test_validates_the_exact_cli_reported_path(self) -> None:
        from dashboard.application.huggingface_download import HuggingFaceDownloadService

        import tempfile
        from pathlib import Path

        class Stream:
            def __init__(self, value): self.value = value
            def read(self, _): value, self.value = self.value, b""; return value

        class Process:
            stdout = None
            stderr = None
            def wait(self, timeout=None): return 0

        with tempfile.TemporaryDirectory() as raw_root:
            model = Path(raw_root) / "model.gguf"
            model.write_bytes(b"gguf")
            process = Process()
            process.stdout = Stream((str(model) + "\n").encode())
            process.stderr = Stream(b"")
            service = HuggingFaceDownloadService(lambda _argv: process, cwd=lambda: Path(raw_root))

            self.assertEqual(service.download("hf", "owner/repo", "model.gguf"), model.resolve())


class ServerStartupServiceTests(unittest.TestCase):
    def test_requires_an_active_model_before_resolving_runtime(self) -> None:
        from dashboard.application.server_startup import ServerStartupService

        service = ServerStartupService(
            load_state=lambda: {"active_model_id": None}, save_state=lambda _: None,
            executable=lambda: None, stop=lambda preserve_log=False: None,
            active_path=lambda _: None, load_options=lambda: {}, runtime_kind=lambda _: "official",
            backend=lambda _: None, option_args=lambda _: [], watch=lambda _: None,
            health=lambda _: False, register_endpoint=lambda _, __: {}, log_tail=lambda _: {"lines": []},
            log_path=None, spawn=lambda *_: None, now=lambda: 0.0, sleep=lambda _: None,
        )

        with self.assertRaisesRegex(RuntimeError, "select a model"):
            service.start()


if __name__ == "__main__":
    unittest.main()
