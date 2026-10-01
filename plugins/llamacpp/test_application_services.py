"""Unit tests for cohesive llamacpp dashboard application modules."""
from __future__ import annotations

import unittest
from pathlib import Path


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

    def test_recovers_from_an_incomplete_managed_block(self) -> None:
        from dashboard.application.profile_endpoint import ManagedEndpointConfig

        config = ManagedEndpointConfig(
            key="llamacpp-local",
            begin="# BEGIN llamacpp endpoint (managed)",
            end="# END llamacpp endpoint (managed)",
        )
        incomplete = (
            "providers:\n"
            "  llamacpp-local:\n"
            "    api: http://127.0.0.1:18434/v1\n"
            "  # BEGIN llamacpp endpoint (managed)\n"
            "fallback_providers:\n"
            "  - provider: openai-codex\n"
        )

        removed, changed = config.remove(incomplete)

        self.assertTrue(changed)
        self.assertNotIn("BEGIN llamacpp endpoint", removed)
        self.assertIn("fallback_providers:\n", removed)
        self.assertIn("  - provider: openai-codex\n", removed)

        repaired = config.upsert(incomplete, "http://127.0.0.1:18434/v1", "model-a", 8192)
        self.assertIn("END llamacpp endpoint (managed)", repaired)
        self.assertIn('default_model: "model-a"', repaired)
        self.assertIn("fallback_providers:\n", repaired)


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

    def test_parses_cache_type_allowed_values(self) -> None:
        from dashboard.application.option_catalog import ServerOptionCatalog

        catalog = ServerOptionCatalog.parse_help(
            "-ctk, --cache-type-k TYPE  KV cache data type for K allowed values: "
            "f32, f16, bf16, q8_0, q4_0, q4_1, iq4_nl, q5_0, q5_1 (default: f16)\n"
            "-ctv, --cache-type-v TYPE  KV cache data type for V allowed values: "
            "f32, f16, bf16, q8_0, q4_0, q4_1, iq4_nl, q5_0, q5_1 (default: f16)\n"
        )

        by_key = {option["key"]: option for option in catalog}
        expected = ["f32", "f16", "bf16", "q8_0", "q4_0", "q4_1", "iq4_nl", "q5_0", "q5_1"]
        self.assertEqual(by_key["cache-type-k"]["choices"], expected)
        self.assertEqual(by_key["cache-type-v"]["choices"], expected)

    def test_parses_llama_choice_descriptions_and_reasoning_effort(self) -> None:
        from dashboard.application.option_catalog import ServerOptionCatalog

        catalog = ServerOptionCatalog.parse_help(
            "--reasoning-format FORMAT               controls thoughts; one of:\n"
            "                                        - none: leave thoughts in content\n"
            "                                        - deepseek: extract reasoning\n"
            "                                        - deepseek-legacy: keep tags\n"
            "--reasoning-effort LEVEL                reasoning effort: 'default' to keep template default,\n"
            "                                        or a level such as 'minimal', 'low', 'medium', 'high', 'xhigh' or\n"
            "                                        'max' (default: default)\n"
            "--pooling {none,mean,cls,last,rank}     pooling type\n"
        )

        by_key = {option["key"]: option for option in catalog}
        self.assertEqual(by_key["reasoning-format"]["choices"], ["none", "deepseek", "deepseek-legacy"])
        self.assertEqual(by_key["reasoning-effort"]["choices"], ["minimal", "low", "medium", "high", "xhigh", "max"])
        self.assertIsNone(by_key["reasoning-effort"]["default_value"])
        self.assertEqual(by_key["pooling"]["choices"], ["none", "mean", "cls", "last", "rank"])


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

    def test_legacy_hf_registration_is_migrated_to_cached_local_paths(self) -> None:
        from dashboard.application.model_registry import RegisteredModelService

        with self.subTest("legacy Hugging Face registration"):
            import tempfile

            with tempfile.TemporaryDirectory() as raw_root:
                model_path = Path(raw_root) / "model.gguf"
                model_path.write_bytes(b"gguf")
                state = {"models": {"model": {"paths": [], "hf_repo": "owner/repo", "hf_file": "model.gguf"}}}
                service = RegisteredModelService(
                    lambda: state, lambda value: state.update(value),
                    lambda _repo: ([{"paths": ["model.gguf"]}], None),
                    lambda _repo, _paths: ([model_path], None),
                )

                self.assertEqual(service.active_path("model"), model_path.resolve())
                self.assertEqual(state["models"]["model"]["paths"], [str(model_path.resolve())])


class RuntimeRouteAdapterTests(unittest.TestCase):
    def test_keeps_runtime_control_and_inventory_paths(self) -> None:
        from dashboard.routes.runtime_routes import RuntimeRouteContext, create_router

        context = RuntimeRouteContext(
            status=lambda: {}, runtime_info=lambda: {}, save=lambda _body: {},
            hardware=lambda: {}, catalog=lambda: {}, open_runtime=lambda _body: {}, install=lambda _body: {},
        )
        paths = {route.path for route in create_router(context).routes}

        self.assertTrue({"/status", "/runtime", "/hardware", "/catalog", "/runtime/open", "/runtime/install"}.issubset(paths))


class RuntimeManagementWorkflowTests(unittest.TestCase):
    def test_selects_custom_runtime_and_projects_installed_state(self) -> None:
        from dashboard.application.runtime_management import RuntimeManagementWorkflow

        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            executable = root / "llama-server.exe"
            executable.write_bytes(b"server")
            state: dict[str, object] = {}

            class Backend:
                key = "prism_ml"
                def managed_root(self, _machine_root): return root
                def resolve_executable(self, raw_path):
                    path = Path(raw_path)
                    if path == root:
                        return executable
                    raise RuntimeError("missing")

            workflow = RuntimeManagementWorkflow(
                load_state=lambda: state, save_state=lambda value: state.update(value),
                get_backend=lambda _kind: Backend(), machine_root=root,
                runtime_kind=lambda _state: "official", runtime_info=lambda: {"executable": str(executable), "mode": "custom"},
                pid_alive=lambda _pid: False, open_directory=lambda _path: None,
                install_prism=lambda: {"job_id": "prism"}, runtime_target=lambda *_: ("b1", "cpu"),
                create_job=lambda kind, detail: {"kind": kind, "detail": detail, "job_id": "job"},
                launch=lambda _job, _work, _name: None, finish=lambda _job, _detail: None,
                install_official=lambda *_args: None, download_archive=lambda *_args: None,
            )

            selected = workflow.select({"kind": "prism_ml", "path": str(root)})

            self.assertEqual(selected["mode"], "custom")
            self.assertEqual(state["runtime_kind"], "prism_ml")
            self.assertEqual(state["runtime_path"], str(root.resolve()))


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

    def test_preserves_locked_previous_runtime_and_installs_to_fallback_path(self) -> None:
        from dashboard.application.prism_runtime import PrismRuntimeInstaller

        import shutil
        import tempfile
        from unittest.mock import patch
        import zipfile

        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root) / "prism"
            (root / ".git" / "objects" / "pack").mkdir(parents=True)
            (root / ".git" / "objects" / "pack" / "locked.idx").write_bytes(b"old")
            source = Path(raw_root) / "server.zip"
            with zipfile.ZipFile(source, "w") as package:
                package.writestr("llama-server.exe", b"server")
            state: dict[str, object] = {}

            def clone(_git, _url, destination):
                (destination / "setup.ps1").parent.mkdir(parents=True)
                (destination / "setup.ps1").write_text('$ReleaseTag = "prism-b124"\n$CudaTag = "13.3"\n', encoding="utf-8")
                return 0

            installer = PrismRuntimeInstaller(
                root=root, load_state=lambda: state, save_state=lambda value: state.update(value),
                find_git=lambda: "git", clone=clone, backend_detector=lambda: "cpu",
                download=lambda _url, destination, *_: destination.write_bytes(source.read_bytes()),
                extract=lambda archive, destination: zipfile.ZipFile(archive).extractall(destination),
                resolve_executable=lambda path: path / "bin" / "cpu" / "llama-server.exe",
            )
            original_rmtree = shutil.rmtree

            def locked_rmtree(path, *args, **kwargs):
                if Path(path) == root:
                    raise PermissionError(5, "Access is denied", str(path))
                return original_rmtree(path, *args, **kwargs)

            with patch.object(shutil, "rmtree", side_effect=locked_rmtree):
                installer.install({"job_id": "job"})

            installed = Path(str(state["runtime_path"]))
            self.assertNotEqual(installed, root.resolve())
            self.assertTrue((installed / "bin" / "cpu" / "llama-server.exe").is_file())
            self.assertTrue((root / ".git" / "objects" / "pack" / "locked.idx").is_file())

    def test_parses_remote_setup_metadata_without_cloning(self) -> None:
        from dashboard.application.prism_runtime import PrismRuntimeInstaller

        self.assertEqual(
            PrismRuntimeInstaller.release_metadata_from_text(
                '$ReleaseTag = "prism-b10743-adfffbe"\n$CudaTag = "13.3"\n'
            ),
            ("prism-b10743-adfffbe", "13.3"),
        )


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
            server_rows=lambda: [{"id": "model"}], models_root=Path("models"), model_presets=lambda: {},
        )

        status = inspector.status()

        self.assertFalse(status["server_running"])
        self.assertEqual(status["runtime_version"], "b1")
        self.assertEqual(status["devices"], [{"id": "0"}])
        self.assertIsNone(state["pid"])
        self.assertEqual(unregistered, [True])

    def test_keeps_endpoint_when_live_process_has_transient_health_failure(self) -> None:
        from dashboard.application.runtime_inspector import RuntimeInspector

        from pathlib import Path
        from types import SimpleNamespace
        state: dict[str, object] = {
            "runtime_kind": "official", "installed_tag": "b1", "installed_backend": "cuda",
            "pid": 42, "port": 18434, "custom_endpoint": {"key": "managed"}, "models": {},
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
            pid_alive=lambda _pid: True, health=lambda _port: False,
            unregister_endpoint=lambda: unregistered.append(True), devices=lambda: [{"id": "0"}],
            server_rows=lambda: [{"id": "model"}], models_root=Path("models"), model_presets=lambda: {},
        )

        status = inspector.status()

        self.assertFalse(status["server_running"])
        self.assertEqual(state["pid"], 42)
        self.assertEqual(state["custom_endpoint"], {"key": "managed"})
        self.assertEqual(unregistered, [])

    def test_does_not_infer_model_preset_from_equal_parameter_values(self) -> None:
        from dashboard.application.runtime_inspector import RuntimeInspector

        from pathlib import Path
        from types import SimpleNamespace
        general = {"ctx-size": "131072", "flash-attn": "on"}
        state: dict[str, object] = {
            "runtime_kind": "official", "installed_tag": "b1", "installed_backend": "cuda",
            "pid": 42, "port": 18434, "models": {}, "active_model_id": "tiel",
            "model_settings": {"tiel": dict(general)},
            "parameter_presets": {"general-id": {"name": "general", "options": dict(general)}},
            "model_presets": {},
        }
        inspector = RuntimeInspector(
            load_state=lambda: state, save_state=lambda value: state.update(value),
            runtime_kind=lambda current: str(current["runtime_kind"]),
            executable=lambda: Path("server.exe"), installed_target=lambda: ("b1", "cuda"),
            executable_in=lambda _root: Path("server.exe"), backend_detector=lambda: "cuda",
            backend=lambda _kind: SimpleNamespace(description="desc", repository="repo"),
            backend_view=lambda _kind, _state, _root: SimpleNamespace(label="official", managed_root="root", version="b1", install_action="install"),
            machine_root=Path("machine"), runtime_root=Path("runtime"), prism_root=Path("prism"),
            pid_alive=lambda _pid: True, health=lambda _port: True,
            unregister_endpoint=lambda: None, devices=lambda: [],
            server_rows=lambda: [{"id": "tiel"}], models_root=Path("models"), model_presets=lambda: {},
        )

        status = inspector.status()

        self.assertEqual(status["model_presets"], {})
        self.assertTrue(status["server_running"])


    def test_exposes_github_update_for_official_and_prism_runtimes(self) -> None:
        from dashboard.application.runtime_inspector import RuntimeInspector

        state: dict[str, object] = {
            "runtime_kind": "prism_ml", "installed_tag": "b10976", "installed_backend": "cuda",
            "prism_release_tag": "prism-b10709-9a9394a", "pid": None, "models": {},
        }
        inspector = RuntimeInspector(
            load_state=lambda: state, save_state=lambda value: state.update(value),
            runtime_kind=lambda current: str(current["runtime_kind"]),
            executable=lambda: Path("server.exe"), installed_target=lambda: ("b10976", "cuda"),
            executable_in=lambda _root: Path("server.exe"), backend_detector=lambda: "cuda",
            backend=lambda _kind: type("Backend", (), {"description": "desc", "repository": "repo"})(),
            backend_view=lambda kind, current, _root: type("View", (), {
                "label": kind, "managed_root": "root",
                "version": current.get("prism_release_tag") if kind == "prism_ml" else current.get("installed_tag"),
                "install_action": "update",
            })(),
            machine_root=Path("machine"), runtime_root=Path("runtime"), prism_root=Path("prism"),
            pid_alive=lambda _pid: False, health=lambda _port: False,
            unregister_endpoint=lambda: None, devices=lambda: [], server_rows=lambda: [],
            models_root=Path("models"), model_presets=lambda: {},
            latest_official=lambda: "b10982", latest_prism=lambda: "prism-b10743-adfffbe",
        )

        status = inspector.status()

        self.assertTrue(status["update_available"])
        self.assertEqual(status["latest_tag"], "prism-b10743-adfffbe")
        self.assertTrue(status["runtime_options"]["prism_ml"]["update_available"])
        self.assertTrue(status["runtime_options"]["official"]["update_available"])


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
    class _MemoryPresetStore:
        def __init__(self, state):
            self.state = state
            self.state.setdefault("parameter_presets", {})
            self.state.setdefault("model_presets", {})

        def presets(self): return self.state["parameter_presets"]
        def get(self, preset_id): return self.presets().get(preset_id)

        def create(self, preset_id, name, options, now):
            value = {"name": name, "model_id": None, "options": options, "created_at": now, "updated_at": now}
            self.presets()[preset_id] = value
            return value

        def update(self, preset_id, name, options, now):
            value = self.get(preset_id)
            if value is None: return None
            if name is not None: value["name"] = name
            if options is not None: value["options"] = options
            value["updated_at"] = now
            return value

        def delete(self, preset_id):
            if preset_id not in self.presets(): return False
            self.presets().pop(preset_id)
            self.state["model_presets"] = {model_id: assigned for model_id, assigned in self.state["model_presets"].items() if assigned != preset_id}
            return True

        def assign(self, model_id, preset_id): self.state["model_presets"][model_id] = preset_id
    def test_apply_preset_replaces_twelve_model_parameters_with_six_preset_parameters(self) -> None:
        from dashboard.application.parameter_settings import ParameterSettingsService

        preset_options = {f"option-{index}": str(index) for index in range(6)}
        stored_options = {"model": {f"option-{index}": str(index) for index in range(12)}}
        state = {"parameter_presets": {"test2": {"name": "test2", "options": preset_options}}}
        catalog = [
            {"key": f"option-{index}", "requires_value": True, "choices": [], "value_kind": "integer"}
            for index in range(12)
        ]
        service = ParameterSettingsService(
            catalog=lambda: catalog, canonical=lambda _, value: str(value),
            load_options=lambda: stored_options, save_options=lambda value: stored_options.update(value),
            preset_store=self._MemoryPresetStore(state),
            error=lambda message: ValueError(message), now=lambda: 1.0, new_id=lambda: "new",
        )

        applied = service.apply_preset("test2", "model", runtime_kind="official", requires_restart=False)

        self.assertEqual(applied["options"], preset_options)
        self.assertEqual(stored_options["model"], preset_options)

    def test_prism_preset_apply_replaces_existing_values_and_omits_spec_options(self) -> None:
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
            preset_store=self._MemoryPresetStore(state),
            error=lambda message: ValueError(message),
            now=lambda: 1.0,
            new_id=lambda: "new",
        )

        applied = service.apply_preset("shared", "model", runtime_kind="prism_ml", requires_restart=False)

        self.assertEqual(applied["options"], {"ctx-size": "8192"})
        self.assertEqual(applied["omitted_options"], ["spec-type"])
        self.assertEqual(state["model_presets"], {"model": "shared"})

    def test_delete_preset_clears_explicit_model_assignments(self) -> None:
        from dashboard.application.parameter_settings import ParameterSettingsService

        state = {
            "parameter_presets": {"shared": {"name": "shared", "options": {}}},
            "model_presets": {"model-a": "shared", "model-b": "other"},
        }
        service = ParameterSettingsService(
            catalog=lambda: [], canonical=lambda _, value: str(value),
            load_options=lambda: {}, save_options=lambda _: None,
            preset_store=self._MemoryPresetStore(state),
            error=lambda message: ValueError(message), now=lambda: 1.0, new_id=lambda: "new",
        )

        service.delete_preset("shared")

        self.assertEqual(state["model_presets"], {"model-b": "other"})


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

    def test_stop_retries_a_transient_windows_log_lock_without_failing(self) -> None:
        from dashboard.application.server_lifecycle import ServerLifecycleService
        from unittest.mock import patch

        import tempfile

        with tempfile.TemporaryDirectory() as raw_root:
            log_path = Path(raw_root) / "llama-server.log"
            log_path.write_text("diagnostic", encoding="utf-8")
            state: dict[str, object] = {"pid": None, "custom_endpoint": {"key": "llamacpp-local"}}
            service = ServerLifecycleService(
                load_state=lambda: state,
                save_state=lambda value: state.update(value),
                pid_alive=lambda _pid: False,
                terminate=lambda _pid: None,
                unregister_endpoint=lambda: None,
                log_path=log_path,
            )

            with patch.object(Path, "unlink", side_effect=[PermissionError("locked"), None]) as unlink:
                service.stop()

            self.assertEqual(unlink.call_count, 2)
            self.assertIsNone(state["pid"])
            self.assertIsNone(state["custom_endpoint"])


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
            cached_paths=lambda repo, paths: ([Path("E:/gguf/models") / path for path in paths], None),
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
        self.assertEqual(registered[0][1], [Path("E:/gguf/models/model.gguf")])
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
            backend=lambda _: None, serving_model_name=lambda model_id: model_id, option_args=lambda _: [], watch=lambda _: None,
            health=lambda _: False, register_endpoint=lambda _, __: {}, log_tail=lambda _: {"lines": []},
            log_path=None, spawn=lambda *_: None, now=lambda: 0.0, sleep=lambda _: None,
        )

        with self.assertRaisesRegex(RuntimeError, "select a model"):
            service.start()

    def test_uses_a_stable_serving_alias_for_command_and_endpoint(self) -> None:
        from dashboard.application.server_startup import ServerStartupService

        import tempfile

        class Backend:
            def __init__(self): self.command = []
            def build_command(self, _executable, _port, _model_id, _entry, _options, model_path=None):
                self.command = ["llama-server", "--model", str(model_path)]
                return self.command

        class Process:
            pid = 99
            def poll(self): return None

        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            model = root / "Ternary-Bonsai-2-27B-PQ2_0.gguf"
            model.write_bytes(b"gguf")
            log_path = root / "llama-server.log"
            state: dict[str, object] = {
                "active_model_id": "Ternary-Bonsai-2-27B-PQ2_0",
                "models": {"Ternary-Bonsai-2-27B-PQ2_0": {}},
            }
            endpoint_models: list[str] = []
            backend = Backend()
            service = ServerStartupService(
                load_state=lambda: state, save_state=lambda value: state.update(value),
                executable=lambda: root / "llama-server.exe", stop=lambda **_: None,
                active_path=lambda _model_id: model, load_options=lambda: {"Ternary-Bonsai-2-27B-PQ2_0": {}},
                runtime_kind=lambda _state: "prism_ml", backend=lambda _kind: backend,
                serving_model_name=lambda _model_id: "Ternary-Bonsai-2-27B",
                option_args=lambda _: [], watch=lambda _: None, health=lambda _: True,
                register_endpoint=lambda _port, model_name: endpoint_models.append(model_name) or {"model": model_name},
                log_tail=lambda _: {"lines": []}, log_path=log_path,
                spawn=lambda *_: Process(), now=lambda: 0.0, sleep=lambda _: None,
            )

            service.start()

            self.assertEqual(backend.command[-2:], ["--alias", "Ternary-Bonsai-2-27B"])
            self.assertEqual(endpoint_models, ["Ternary-Bonsai-2-27B"])



class MmprojAutoTests(unittest.TestCase):
    """Custom mmproj-auto behavior: discover a projector gguf next to the model
    and emit an explicit --mmproj flag (stock --mmproj-auto is a no-op for --model)."""

    def test_find_prefers_base_mmproj_then_falls_back_to_any(self):
        import tempfile
        from dashboard.application.server_startup import _find_mmproj_near as find

        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            model = root / "Ternary-Bonsai-2-27B-PQ2_0.gguf"
            projector = root / "Ternary-Bonsai-2-27B-mmproj-Q8_0.gguf"
            decoy = root / "Qwen-1.5-14B-mmproj.gguf"
            for p in (model, projector, decoy):
                p.write_bytes(b"gguf")
            self.assertEqual(find(model), projector)

        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            model = root / "model.gguf"
            projector = root / "model-mmproj.gguf"
            model.write_bytes(b"gguf")
            projector.write_bytes(b"gguf")
            self.assertEqual(find(model), projector)

    def test_find_none_without_projector_or_missing_model(self):
        import tempfile
        from dashboard.application.server_startup import _find_mmproj_near as find

        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            model = root / "model.gguf"
            model.write_bytes(b"gguf")
            self.assertIsNone(find(model))
        self.assertIsNone(find(None))

    def _command(self, options, model):
        """Return the exact command list that ServerStartupService.start() assembles
        and passes to spawn. The caller creates the model file (and any sibling
        projector) on disk; we just feed that real path in. start() extends the
        recorded command list in place, so it is exactly what would be spawned."""
        import tempfile
        from dashboard.application.server_startup import ServerStartupService

        root = Path(tempfile.mkdtemp(prefix="llama_startup_"))
        state = {"active_model_id": "model", "models": {"model": {}}}
        recorded = {}

        class Backend:
            def build_command(self, executable, port, model_id, entry, opts, model_path=None):
                command = [executable, "--model", str(model_path)]
                recorded["command"] = command
                return command

        service = ServerStartupService(
            load_state=lambda: state, save_state=lambda _: None,
            executable=lambda: root / "llama-server.exe", stop=lambda **_: None,
            active_path=lambda _model_id: model,
            load_options=lambda: {"model": options},
            runtime_kind=lambda _state: "prism_ml",
            backend=lambda _kind: Backend(),
            serving_model_name=lambda _model_id: "served", option_args=lambda _: [],
            watch=lambda _: None, health=lambda _: True,
            register_endpoint=lambda _port, _name: {}, log_tail=lambda _: {"lines": []},
            log_path=root / "llama-server.log",
            spawn=lambda *_: _FakeProcess(), now=lambda: 0.0, sleep=lambda _: None,
        )
        service.start()
        return recorded["command"]

    def test_emits_mmproj_when_auto_enabled_and_projector_present(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            model = root / "Ternary-Bonsai-2-27B-PQ2_0.gguf"
            projector = root / "Ternary-Bonsai-2-27B-mmproj-Q8_0.gguf"
            for p in (model, projector):
                p.write_bytes(b"gguf")
            cmd = self._command({"mmproj-auto": "", "ctx-size": "8192"}, model)
            self.assertIn("--mmproj", cmd)
            self.assertIn(str(projector), cmd)
            self.assertNotIn("--mmproj-auto", cmd)
            self.assertEqual(cmd[cmd.index("--mmproj") + 1], str(projector))

    def test_omits_mmproj_when_auto_disabled(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            model = root / "model.gguf"
            projector = root / "model-mmproj.gguf"
            for p in (model, projector):
                p.write_bytes(b"gguf")
            cmd = self._command({"ctx-size": "8192"}, model)
            self.assertNotIn("--mmproj", cmd)
            self.assertNotIn("--mmproj-auto", cmd)

    def test_respects_no_mmproj_override(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            model = root / "model.gguf"
            projector = root / "model-mmproj.gguf"
            for p in (model, projector):
                p.write_bytes(b"gguf")
            cmd = self._command({"mmproj-auto": "", "no-mmproj": ""}, model)
            self.assertNotIn("--mmproj", cmd)

    def test_none_when_no_projector(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            model = root / "model.gguf"
            model.write_bytes(b"gguf")
            cmd = self._command({"mmproj-auto": ""}, model)
            self.assertNotIn("--mmproj", cmd)
            self.assertNotIn("--mmproj-auto", cmd)


class _FakeProcess:
    pid = 99

    def poll(self):
        return None

if __name__ == "__main__":
    unittest.main()
