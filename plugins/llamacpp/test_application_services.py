"""Unit tests for cohesive llamacpp dashboard application modules."""
from __future__ import annotations

import unittest
import threading
import os
from pathlib import Path


class ManagedEndpointConfigTests(unittest.TestCase):
    def test_desktop_leases_expire_and_wait_for_active_execution(self) -> None:
        from dashboard.application.desktop_leases import DesktopLeaseRegistry

        now = [100.0]
        leases = DesktopLeaseRegistry(timeout_seconds=8, clock=lambda: now[0])

        self.assertFalse(leases.should_shutdown())
        self.assertEqual(leases.touch("desktop-a"), 1)
        self.assertEqual(leases.touch("desktop-b"), 2)
        leases.release("desktop-a")
        self.assertFalse(leases.should_shutdown())
        now[0] += 9
        self.assertFalse(leases.should_shutdown(execution_busy=True))
        self.assertTrue(leases.should_shutdown(execution_busy=False))

    def test_desktop_lease_rejects_missing_or_oversized_client_id(self) -> None:
        from dashboard.application.desktop_leases import DesktopLeaseRegistry

        leases = DesktopLeaseRegistry()
        with self.assertRaisesRegex(ValueError, "client_id"):
            leases.touch("")
        with self.assertRaisesRegex(ValueError, "client_id"):
            leases.touch("x" * 129)

    def test_execution_profiles_store_main_and_compression_independently(self) -> None:
        from dashboard.application.execution_profiles import ExecutionProfileService

        state = {
            "active_model_id": "main-model",
            "runtime_kind": "official",
            "models": {"main-model": {}, "Ternary-Bonsai-small": {}},
        }
        service = ExecutionProfileService(
            load_state=lambda: state,
            save_state=lambda value: state.update(value),
            accepts=lambda kind, model_id: not (kind == "prism_ml" and model_id == "main-model"),
        )

        service.save("main", {"runtime_kind": "official", "model_id": "main-model"})
        result = service.save("compression", {"runtime_kind": "official", "model_id": "Ternary-Bonsai-small"})

        self.assertEqual(result["execution_mode"], "exclusive_swap")
        self.assertEqual(result["profiles"]["main"]["runtime_kind"], "official")
        self.assertEqual(result["profiles"]["compression"]["runtime_kind"], "prism_ml")
        self.assertEqual(result["profiles"]["main"]["logical_model"], "main-local")
        self.assertEqual(result["profiles"]["compression"]["logical_model"], "compression-local")

    def test_execution_profiles_allow_official_runtime_for_both_roles(self) -> None:
        from dashboard.application.execution_profiles import ExecutionProfileService

        state = {
            "models": {"main-model": {}, "small-model": {}},
            "runtime_kind": "official",
        }
        service = ExecutionProfileService(
            load_state=lambda: state,
            save_state=lambda value: state.update(value),
            accepts=lambda _kind, _model_id: True,
        )

        service.save("main", {"runtime_kind": "official", "model_id": "main-model"})
        result = service.save("compression", {"runtime_kind": "official", "model_id": "small-model"})

        self.assertEqual(result["profiles"]["main"]["runtime_kind"], "official")
        self.assertEqual(result["profiles"]["compression"]["runtime_kind"], "official")

    def test_execution_profiles_derive_runtime_from_model_name(self) -> None:
        from dashboard.application.execution_profiles import ExecutionProfileService

        for model_id, expected_runtime in (
            ("regular-model", "official"),
            ("Ternary-Bonsai-2-27B-PQ2_0", "prism_ml"),
        ):
            with self.subTest(model=model_id):
                state = {"models": {model_id: {}}}
                service = ExecutionProfileService(
                    load_state=lambda: state,
                    save_state=lambda value: state.update(value),
                    accepts=lambda _kind, _model_id: True,
                )
                result = service.save("main", {"runtime_kind": "prism_ml", "model_id": model_id})

                self.assertEqual(result["profiles"]["main"]["runtime_kind"], expected_runtime)

    def test_execution_profiles_migrate_saved_runtime_to_model_rule(self) -> None:
        from dashboard.application.execution_profiles import ExecutionProfileService

        state = {
            "models": {"Ternary-Bonsai-2-27B-PQ2_0": {}},
            "execution_profiles": {
                "main": {
                    "runtime_kind": "official",
                    "model_id": "Ternary-Bonsai-2-27B-PQ2_0",
                },
            },
        }
        service = ExecutionProfileService(
            load_state=lambda: state,
            save_state=lambda value: state.update(value),
            accepts=lambda _kind, _model_id: True,
        )

        result = service.snapshot()

        self.assertEqual(result["profiles"]["main"]["runtime_kind"], "prism_ml")

    def test_execution_profiles_allow_each_role_to_be_cleared_independently(self) -> None:
        from dashboard.application.execution_profiles import ExecutionProfileService

        state = {
            "active_model_id": "main-model",
            "models": {"main-model": {}, "small-model": {}},
            "execution_profiles": {
                "main": {"runtime_kind": "official", "model_id": "main-model", "preset_id": ""},
                "compression": {"runtime_kind": "official", "model_id": "small-model", "preset_id": ""},
            },
        }
        service = ExecutionProfileService(
            load_state=lambda: state,
            save_state=lambda value: state.update(value),
            accepts=lambda _kind, _model_id: True,
        )

        result = service.save("main", {"model_id": ""})

        self.assertFalse(result["profiles"]["main"]["configured"])
        self.assertTrue(result["profiles"]["compression"]["configured"])
        self.assertNotIn("main", state["execution_profiles"])
        self.assertIsNone(state["active_model_id"])

    def test_execution_profile_start_route_uses_main_or_aux_from_one_server_action(self) -> None:
        from fastapi import HTTPException
        from dashboard.routes.execution_profile_routes import ExecutionProfileRouteContext, create_router

        events = []
        job = {"job_id": "job-1"}
        context = ExecutionProfileRouteContext(
            snapshot=lambda: {},
            save=lambda role, body: events.append(("save", role, body)) or {},
            start=lambda role: events.append(("start", role)),
            create_job=lambda kind, detail: events.append(("job", kind, detail)) or job,
            launch=lambda _job, run, _name: run(),
            finish=lambda _job, detail: events.append(("finish", detail)),
        )
        router = create_router(context)
        endpoint = next(route.endpoint for route in router.routes if route.path == "/profiles/start")
        direct = next(route.endpoint for route in router.routes if route.path == "/profiles/{role}/start")

        result = endpoint({"main_model_id": "large-model", "compression_model_id": "small-model"})

        self.assertEqual(result["job_id"], "job-1")
        self.assertEqual(events[0], ("save", "main", {"model_id": "large-model"}))
        self.assertEqual(events[1], ("save", "compression", {"model_id": "small-model"}))
        self.assertEqual(events[3], ("start", "main"))

        events.clear()
        result = endpoint({"main_model_id": "", "compression_model_id": "small-model"})
        self.assertEqual(result["job_id"], "job-1")
        self.assertEqual(events[3], ("start", "compression"))

        with self.assertRaises(HTTPException) as rejected:
            endpoint({"main_model_id": "", "compression_model_id": ""})
        self.assertEqual(rejected.exception.status_code, 422)

        with self.assertRaises(HTTPException) as rejected:
            direct("compression", {"model_id": "small-model"})
        self.assertEqual(rejected.exception.status_code, 409)

    def test_legacy_active_model_is_persisted_as_main_execution_profile(self) -> None:
        from dashboard.application.execution_profiles import ExecutionProfileService

        state = {
            "active_model_id": "legacy-main",
            "runtime_kind": "official",
            "models": {"legacy-main": {}},
        }

        def mutate(callback):
            callback(state)
            return state

        service = ExecutionProfileService(
            load_state=lambda: state,
            save_state=lambda value: state.update(value),
            accepts=lambda _kind, _model_id: True,
            mutate_state=mutate,
        )

        snapshot = service.snapshot()

        self.assertEqual(snapshot["profiles"]["main"]["model_id"], "legacy-main")
        self.assertEqual(state["execution_profiles"]["main"]["model_id"], "legacy-main")

    def test_execution_profiles_reject_incompatible_binding(self) -> None:
        from dashboard.application.execution_profiles import ExecutionProfileService

        state = {"models": {"model": {}}}
        service = ExecutionProfileService(
            load_state=lambda: state,
            save_state=lambda value: state.update(value),
            accepts=lambda _kind, _model_id: False,
        )

        with self.assertRaisesRegex(ValueError, "호환"):
            service.save("compression", {"runtime_kind": "prism_ml", "model_id": "model"})

    def test_execution_profile_uses_registered_runtime_for_custom_alias(self) -> None:
        from dashboard.application.execution_profiles import ExecutionProfileService

        state: dict[str, object] = {
            "models": {"my-compressor": {"runtime_kind": "prism_ml"}},
        }
        service = ExecutionProfileService(
            load_state=lambda: state,
            save_state=lambda value: state.update(value),
            accepts=lambda kind, _model_id: kind == "prism_ml",
        )

        result = service.save("compression", {"model_id": "my-compressor"})

        self.assertEqual(result["profiles"]["compression"]["runtime_kind"], "prism_ml")

    def test_concurrent_execution_profile_saves_preserve_both_roles(self) -> None:
        import tempfile
        from dashboard.application.execution_profiles import ExecutionProfileService
        from dashboard.application.state_store import StateStore

        with tempfile.TemporaryDirectory() as raw_root:
            store = StateStore(
                Path(raw_root) / "state.json",
                lambda: {"models": {"main-model": {}, "small-model": {}}},
            )
            store.save({"models": {"main-model": {}, "small-model": {}}})
            service = ExecutionProfileService(store.load, store.save, lambda _kind, _model: True, store.mutate)
            barrier = threading.Barrier(2)

            def save(role: str, model_id: str) -> None:
                barrier.wait()
                service.save(role, {"runtime_kind": "official", "model_id": model_id})

            threads = [
                threading.Thread(target=save, args=("main", "main-model")),
                threading.Thread(target=save, args=("compression", "small-model")),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=5)

            profiles = store.load()["execution_profiles"]
            self.assertEqual(profiles["main"]["model_id"], "main-model")
            self.assertEqual(profiles["compression"]["model_id"], "small-model")

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

    def test_writes_main_and_compression_logical_models_in_one_provider(self) -> None:
        from dashboard.application.profile_endpoint import ManagedEndpointConfig

        config = ManagedEndpointConfig(
            key="llamacpp-local",
            begin="# BEGIN llamacpp endpoint (managed)",
            end="# END llamacpp endpoint (managed)",
        )

        updated = config.upsert_models(
            "providers:\n",
            "http://127.0.0.1:18380/v1",
            "main-local",
            {"main-local": 32768, "compression-local": 8192},
        )

        self.assertIn('      default_model: "main-local"', updated)
        self.assertIn('        "main-local":', updated)
        self.assertIn('          context_length: 32768', updated)
        self.assertIn('        "compression-local":', updated)
        self.assertIn('          context_length: 8192', updated)


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

    def test_registration_preserves_runtime_kind_and_refuses_alias_overwrite(self) -> None:
        from dashboard.application.model_registry import RegisteredModelService

        state: dict[str, object] = {"models": {"same-name": {"paths": [], "owned": False}}}
        service = RegisteredModelService(
            load_state=lambda: state,
            save_state=lambda value: state.update(value),
            cached_files=lambda _repo: ([], None),
        )

        with self.assertRaisesRegex(RuntimeError, "이미 등록"):
            service.register("same-name", [Path("E:/models/model.gguf")], False)

        service.register(
            "custom-alias", [Path("E:/models/model.gguf")], False,
            hf_repo="owner/model-GGUF", hf_file="model.gguf", runtime_kind="official",
        )
        self.assertEqual(state["models"]["custom-alias"]["runtime_kind"], "official")

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

        registrations: list[tuple[str, list[str], str]] = []

        class Workflow:
            def search(self, _query, _limit):
                raise RuntimeError("offline")

            def register(self, repo_id, paths, alias=""):
                registrations.append((repo_id, paths, alias))
                return {"model_id": alias}

            def cached_files(self, _repo_id):
                return ([{"paths": ["model.gguf"]}], None)

            def suggested_alias(self, _repo_id):
                return "model-(2)"

        context = ModelRouteContext(
            lifecycle=lambda: object(), workflow=lambda: Workflow(),
            create_job=lambda kind, detail: {"kind": kind, "detail": detail, "job_id": "job"},
            launch=lambda _job, _work, _name: None, finish=lambda _job, _detail: None,
            begin_download=lambda _job, _detail: None,
        )
        router = create_router(context)
        paths = {route.path: route.endpoint for route in router.routes}

        self.assertTrue({"/activate", "/eject", "/models/{model_id}", "/models/{model_id}/registration", "/search", "/repo", "/download-browsed", "/register", "/sideload", "/hf-models", "/hf-models/files", "/hf-models/delete"}.issubset(paths))
        with self.assertRaises(HTTPException) as raised:
            paths["/search"]("test", 20)
        self.assertEqual(raised.exception.status_code, 502)
        self.assertEqual(
            paths["/register"]({"repo": "owner/model-GGUF", "paths": ["model.gguf"], "alias": "model-(2)"}),
            {"model_id": "model-(2)"},
        )
        self.assertEqual(registrations, [("owner/model-GGUF", ["model.gguf"], "model-(2)")])
        self.assertEqual(
            paths["/hf-models/files"]("owner/model-GGUF")["suggested_alias"],
            "model-(2)",
        )


class PrismRuntimeInstallerTests(unittest.TestCase):
    def test_resolves_latest_complete_release_and_installs_versioned_cpu_runtime(self) -> None:
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

            installer = PrismRuntimeInstaller(
                root=root, load_state=lambda: state, save_state=lambda value: state.update(value),
                release_loader=lambda: [
                    {"tag_name": "prism-b123-aaaaaaa", "assets": [{"name": "llama-prism-b123-aaaaaaa-bin-win-cpu-x64.zip"}]},
                    {"tag_name": "prism-b124-bbbbbbb", "assets": [{"name": "unrelated.zip"}]},
                ],
                backend_detector=lambda: "cpu", platform_name=lambda: "windows", architecture=lambda: "x64",
            )
            job = {"job_id": "job"}

            self.assertEqual(installer.latest_target(), ("prism-b123-aaaaaaa", "cpu"))
            tag, backend = installer.install(
                "prism-b123-aaaaaaa", "cpu", job,
                download=lambda _url, destination, *_: destination.write_bytes(source.read_bytes()),
            )

            self.assertEqual((tag, backend), ("prism-b123-aaaaaaa", "cpu"))
            self.assertEqual(state["prism_release_tag"], "prism-b123-aaaaaaa")
            self.assertTrue((root / "prism-b123-aaaaaaa" / "cpu" / "llama-server.exe").is_file())
            self.assertTrue((root / "prism-b123-aaaaaaa" / "cpu" / "manifest.json").is_file())

    def test_migrates_legacy_demo_checkout_to_official_versioned_layout(self) -> None:
        from dashboard.application.prism_runtime import PrismRuntimeInstaller

        import tempfile

        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root) / "prism"
            legacy = root / "bin" / "cuda"
            legacy.mkdir(parents=True)
            (legacy / "llama-server.exe").write_bytes(b"server")
            (legacy / "ggml-cuda.dll").write_bytes(b"cuda")
            (root / ".git" / "objects").mkdir(parents=True)
            (root / ".git" / "objects" / "legacy").write_bytes(b"old")
            (root / "README.md").write_text("legacy checkout", encoding="utf-8")
            state: dict[str, object] = {
                "runtime_kind": "prism_ml",
                "runtime_path": str(root),
                "custom_runtime_path": str(root),
                "prism_release_tag": "prism-b124-bbbbbbb",
                "prism_backend": "cuda",
                "pid": None,
            }

            installer = PrismRuntimeInstaller(
                root=root, load_state=lambda: state, save_state=lambda value: state.update(value),
                release_loader=lambda: [], backend_detector=lambda: "cpu",
                platform_name=lambda: "windows", architecture=lambda: "x64",
            )

            installed = installer.migrate_legacy_layout()

            expected = root / "prism-b124-bbbbbbb" / "cuda"
            self.assertEqual(installed, expected)
            self.assertEqual(Path(str(state["runtime_path"])), expected.resolve())
            self.assertTrue((expected / "llama-server.exe").is_file())
            self.assertTrue((expected / "ggml-cuda.dll").is_file())
            self.assertTrue((expected / "manifest.json").is_file())
            self.assertFalse((root / "bin").exists())
            self.assertFalse((root / ".git").exists())
            self.assertFalse((root / "README.md").exists())

    def test_selects_cuda_assets_directly_from_prism_llamacpp_release(self) -> None:
        from dashboard.application.prism_runtime import PrismRuntimeInstaller

        installer = PrismRuntimeInstaller(
            root=Path("runtime"), load_state=lambda: {}, save_state=lambda _value: None,
            release_loader=lambda: [], backend_detector=lambda: "cuda",
            platform_name=lambda: "windows", architecture=lambda: "x64",
        )

        self.assertEqual(installer.asset_names("prism-b10743-adfffbe", "cuda"), [
            "llama-prism-b10743-adfffbe-bin-win-cuda-12.4-x64.zip",
            "cudart-llama-bin-win-cuda-12.4-x64.zip",
        ])


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
    def test_unregister_removes_only_registration_and_clears_role_bindings(self) -> None:
        from dashboard.application.model_lifecycle import ModelLifecycleService

        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            model = root / "registered.gguf"
            model.write_bytes(b"model")
            state: dict[str, object] = {
                "active_model_id": "registered",
                "models": {"registered": {"paths": [str(model)], "owned": True}},
                "execution_profiles": {
                    "main": {"model_id": "registered", "runtime_kind": "official"},
                    "compression": {"model_id": "registered", "runtime_kind": "official"},
                },
            }

            class Registry:
                def rows(self): return [{"id": "registered", "paths": [str(model)]}]
                def active_path(self, _model_id): return model
                def register(self, *_args, **_kwargs): return None

            service = ModelLifecycleService(
                registry=Registry(), load_state=lambda: state, save_state=lambda value: state.update(value),
                runtime_kind=lambda _: "official", accepts=lambda *_: True,
                server_running=lambda: False, stop_server=lambda: None,
                models_root=root, model_id=lambda path: path.stem,
            )

            result = service.unregister("registered")

            self.assertEqual(result, {"ok": True, "model_id": "registered", "unregistered": True})
            self.assertTrue(model.exists())
            self.assertNotIn("registered", state["models"])
            self.assertIsNone(state["active_model_id"])
            self.assertEqual(state["execution_profiles"]["main"]["model_id"], "")
            self.assertEqual(state["execution_profiles"]["compression"]["model_id"], "")

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
            state: dict[str, object] = {
                "pid": 42,
                "custom_endpoint": {"key": "llamacpp-local"},
                "active_role": "main",
                "transition_phase": "MAIN_READY",
                "transition_error": "stale",
            }
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
            self.assertIsNone(state["active_role"])
            self.assertEqual(state["transition_phase"], "IDLE")
            self.assertIsNone(state["transition_error"])
            self.assertEqual(unregistered, [True])
            self.assertFalse(log_path.exists())

    def test_stop_does_not_clear_worker_state_until_termination_returns(self) -> None:
        from dashboard.application.server_lifecycle import ServerLifecycleService

        import tempfile

        with tempfile.TemporaryDirectory() as raw_root:
            state: dict[str, object] = {"pid": 42, "custom_endpoint": {"key": "llamacpp-local"}}
            termination_finished = threading.Event()
            observed: list[object] = []

            def terminate(_pid: int) -> None:
                observed.append(state["pid"])
                termination_finished.set()

            service = ServerLifecycleService(
                load_state=lambda: state,
                save_state=lambda value: state.update(value),
                pid_alive=lambda pid: pid == 42,
                terminate=terminate,
                unregister_endpoint=lambda: None,
                log_path=Path(raw_root) / "server.log",
            )

            service.stop(keep_endpoint=True)

            self.assertTrue(termination_finished.is_set())
            self.assertEqual(observed, [42])
            self.assertIsNone(state["pid"])

    def test_stop_preserves_live_state_when_identity_validation_refuses_termination(self) -> None:
        from dashboard.application.server_lifecycle import ServerLifecycleService
        import tempfile

        with tempfile.TemporaryDirectory() as raw_root:
            state: dict[str, object] = {"pid": 42, "custom_endpoint": {"key": "llamacpp-local"}}
            service = ServerLifecycleService(
                load_state=lambda: state,
                save_state=lambda value: state.update(value),
                pid_alive=lambda pid: pid == 42,
                terminate=lambda _pid: False,
                unregister_endpoint=lambda: None,
                log_path=Path(raw_root) / "server.log",
            )

            with self.assertRaisesRegex(RuntimeError, "unverified process identity"):
                service.stop(keep_endpoint=True)

            self.assertEqual(state["pid"], 42)
            self.assertEqual(state["custom_endpoint"], {"key": "llamacpp-local"})

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

    def test_inventory_and_file_selection_include_complete_snapshot_omitted_by_hf_cli(self) -> None:
        from dashboard.application.huggingface_cache import HuggingFaceCacheService

        import tempfile

        with tempfile.TemporaryDirectory() as raw_root:
            hub = Path(raw_root) / "hub"
            repo = hub / "models--prism-ml--Ternary-Bonsai-2-27B-gguf"
            snapshot = repo / "snapshots" / "revision"
            snapshot.mkdir(parents=True)
            (repo / "refs").mkdir()
            (repo / "refs" / "main").write_text("revision", encoding="utf-8")
            (repo / "blobs").mkdir()
            (repo / "blobs" / "other-quant.downloadInProgress").write_bytes(b"partial")
            (snapshot / "Ternary-Bonsai-2-27B-PQ2_0.gguf").write_bytes(b"model")

            def run(argv):
                if "--revisions" in argv:
                    return 0, "[]"
                return 0, '[{"repo_id":"owner/Official-GGUF","size":"1.0G"}]'

            service = HuggingFaceCacheService(lambda: "hf", run, cache_root=lambda: hub)

            models, _executable, warning = service.downloaded_models()
            groups, files_warning = service.cached_files("prism-ml/Ternary-Bonsai-2-27B-gguf")

            self.assertIsNone(warning)
            self.assertEqual(
                [model["repo_id"] for model in models],
                ["owner/Official-GGUF", "prism-ml/Ternary-Bonsai-2-27B-gguf"],
            )
            self.assertIsNone(files_warning)
            self.assertEqual(groups[0]["paths"], ["Ternary-Bonsai-2-27B-PQ2_0.gguf"])


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
    def test_local_models_lists_every_cached_repository_regardless_of_runtime(self) -> None:
        from dashboard.application.huggingface_model_workflow import HuggingFaceModelWorkflow

        cached = [
            {"repo_id": "owner/Regular-GGUF", "size": "2 GB"},
            {"repo_id": "prism-ml/Ternary-Bonsai-2-27B-gguf", "size": "7 GB"},
        ]
        workflow = HuggingFaceModelWorkflow(
            load_state=lambda: {"runtime_kind": "prism_ml"},
            runtime_kind=lambda current: str(current["runtime_kind"]),
            accepts=lambda *_: True,
            cache_models=lambda: (cached, "hf", None),
            cached_files=lambda _repo: ([], None),
            cached_paths=lambda _repo, _paths: ([], None),
            http_json=lambda _url: [],
            download=lambda *_args, **_kwargs: None,
            register=lambda *_args, **_kwargs: None,
            remove_cache=lambda _repo: True,
            model_id=lambda path: path.stem,
            visible_repositories=lambda _kind, repositories: [
                repo for repo in repositories if repo.startswith("prism-ml/")
            ],
        )

        result = workflow.local_models()

        self.assertEqual(result["models"], cached)
        self.assertEqual(result["runtime_kind"], "prism_ml")

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
        self.assertEqual(registered_model["model_id"], "repo")
        self.assertEqual(registered[0][0], "repo")
        self.assertEqual(registered[0][1], [Path("E:/gguf/models/model.gguf")])
        with self.assertRaisesRegex(RuntimeError, "HF cache"):
            workflow.register("owner/repo", ["missing.gguf"])

    def test_register_uses_model_runtime_instead_of_selected_dashboard_runtime(self) -> None:
        from dashboard.application.huggingface_model_workflow import HuggingFaceModelWorkflow

        accepted_kinds: list[str] = []
        registered: list[tuple[object, ...]] = []
        workflow = HuggingFaceModelWorkflow(
            load_state=lambda: {"runtime_kind": "prism_ml", "prism_release_tag": "prism-test"},
            runtime_kind=lambda current: str(current["runtime_kind"]),
            accepts=lambda kind, _repo, _paths, _version: accepted_kinds.append(kind) is None or kind == "official",
            cache_models=lambda: ([], "hf", None),
            cached_files=lambda _repo: ([{"label": "Qwen", "paths": ["Qwen-Q6_K.gguf"], "total_bytes": 3, "fit": "downloaded"}], None),
            cached_paths=lambda _repo, paths: ([Path("E:/gguf/models") / path for path in paths], None),
            http_json=lambda _url: [],
            download=lambda *_args, **_kwargs: None,
            register=lambda *args, **kwargs: registered.append(args),
            remove_cache=lambda _repo: True,
            model_id=lambda path: path.stem,
            registration_runtime_kind=lambda model_id: "prism_ml" if model_id.startswith("Ternary-Bonsai") else "official",
        )

        result = workflow.register("owner/Qwen-GGUF", ["Qwen-Q6_K.gguf"])

        self.assertEqual(result["model_id"], "Qwen")
        self.assertEqual(accepted_kinds, ["official"])
        self.assertEqual(registered[0][0], "Qwen")

    def test_suggests_repo_alias_and_suffixes_existing_names_for_repeat_registration(self) -> None:
        from dashboard.application.huggingface_model_workflow import HuggingFaceModelWorkflow

        state: dict[str, object] = {
            "runtime_kind": "official",
            "models": {
                "Ornith-1.5-35B-A3B": {},
                "Ornith-1.5-35B-A3B-(2)": {},
            },
        }
        registered: list[tuple[object, ...]] = []

        def register(model_id, *args, **kwargs):
            registered.append((model_id, *args))
            state["models"][model_id] = {"paths": []}

        workflow = HuggingFaceModelWorkflow(
            load_state=lambda: state,
            runtime_kind=lambda current: str(current["runtime_kind"]),
            accepts=lambda *_args: True,
            cache_models=lambda: ([], "hf", None),
            cached_files=lambda _repo: ([{"label": "Ornith-Q6", "paths": ["Ornith-Q6.gguf"], "total_bytes": 3, "fit": "downloaded"}], None),
            cached_paths=lambda _repo, paths: ([Path("E:/gguf/models") / path for path in paths], None),
            http_json=lambda _url: [], download=lambda *_args, **_kwargs: None,
            register=register, remove_cache=lambda _repo: True, model_id=lambda path: path.stem,
            registration_runtime_kind=lambda _model_id: "official",
        )

        self.assertEqual(
            workflow.suggested_alias("ornith-ai/Ornith-1.5-35B-A3B-GGUF"),
            "Ornith-1.5-35B-A3B-(3)",
        )
        result = workflow.register(
            "ornith-ai/Ornith-1.5-35B-A3B-GGUF", ["Ornith-Q6.gguf"],
            alias="Ornith-1.5-35B-A3B",
        )
        self.assertEqual(result["model_id"], "Ornith-1.5-35B-A3B-(3)")
        self.assertEqual(registered[0][0], "Ornith-1.5-35B-A3B-(3)")


class HuggingFaceDownloadServiceTests(unittest.TestCase):
    def test_worker_emits_monotonic_progress_and_reserves_completion(self) -> None:
        from dashboard.application import hf_download_worker

        import io
        from unittest.mock import patch

        output = io.StringIO()
        hf_download_worker._LAST_PERCENT = -1
        with patch("sys.stderr", output):
            hf_download_worker._emit_percent(1, 4)
            hf_download_worker._emit_percent(1, 4)
            hf_download_worker._emit_percent(4, 4)

        self.assertEqual(
            output.getvalue().splitlines(),
            ["HERMES_PROGRESS:25%", "HERMES_PROGRESS:99%"],
        )

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
            root = Path(raw_root)
            model = root / "model.gguf"
            model.write_bytes(b"gguf")
            scripts = root / "Scripts"
            scripts.mkdir()
            hf = scripts / "hf.exe"
            hf.write_bytes(b"")
            (scripts / "python.exe").write_bytes(b"")
            process = Process()
            process.stdout = Stream((str(model) + "\n").encode())
            process.stderr = Stream(b"")
            service = HuggingFaceDownloadService(lambda _argv: process, cwd=lambda: Path(raw_root))

            self.assertEqual(service.download(str(hf), "owner/repo", "model.gguf"), model.resolve())

    def test_enables_cli_progress_and_parses_human_reported_path(self) -> None:
        from dashboard.application.huggingface_download import HuggingFaceDownloadService

        import tempfile
        from pathlib import Path

        class Stream:
            def __init__(self, value): self.value = value
            def read(self, _): value, self.value = self.value, b""; return value

        class AvailableStream(Stream):
            def read(self, _): return b""
            def read1(self, _): value, self.value = self.value, b""; return value

        class Process:
            stdout = None
            stderr = None
            def wait(self, timeout=None): return 0

        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            model = root / "model.gguf"
            model.write_bytes(b"gguf")
            scripts = root / "Scripts"
            scripts.mkdir()
            hf = scripts / "hf.exe"
            python = scripts / "python.exe"
            hf.write_bytes(b"")
            python.write_bytes(b"")
            process = Process()
            process.stdout = Stream((f"✓ Downloaded\n  path: {model}\n").encode())
            process.stderr = AvailableStream(b"HERMES_PROGRESS:42%\n")
            argv: list[str] = []
            progress: list[int] = []

            def start_process(command: list[str]):
                argv.extend(command)
                return process

            service = HuggingFaceDownloadService(start_process, cwd=lambda: Path(raw_root))

            self.assertEqual(
                service.download(str(hf), "owner/repo", "model.gguf", progress.append),
                model.resolve(),
            )
            self.assertEqual(argv[0], str(python))
            self.assertTrue(argv[1].endswith("hf_download_worker.py"))
            self.assertEqual(argv[2:], ["owner/repo", "model.gguf"])
            self.assertEqual(progress, [42])


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
            pid = os.getpid()
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

    def test_identity_capture_failure_terminates_owned_process_before_state_write(self) -> None:
        import sys
        import tempfile
        import types
        from unittest.mock import patch
        from dashboard.application.server_startup import ServerStartupService

        class Backend:
            def build_command(self, *_args, **_kwargs):
                return ["llama-server"]

        class Process:
            pid = 99

            def __init__(self) -> None:
                self.terminated = False
                self.waited = False

            def terminate(self) -> None:
                self.terminated = True

            def wait(self, timeout=None) -> None:
                self.waited = True

            def poll(self):
                return 1

        class IdentityLookupFails:
            def __init__(self, _pid: int) -> None:
                raise RuntimeError("identity unavailable")

        process = Process()
        writes: list[dict[str, object]] = []
        fake_psutil = types.SimpleNamespace(Process=IdentityLookupFails)
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            model = root / "model.gguf"
            model.write_bytes(b"gguf")
            state = {"active_model_id": "model", "models": {"model": {}}}
            service = ServerStartupService(
                load_state=lambda: state, save_state=lambda value: writes.append(dict(value)),
                executable=lambda: root / "llama-server.exe", stop=lambda **_: None,
                active_path=lambda _: model, load_options=lambda: {"model": {}},
                runtime_kind=lambda _: "official", backend=lambda _: Backend(),
                serving_model_name=lambda _: "served", option_args=lambda _: [], watch=lambda _: None,
                health=lambda _: False, register_endpoint=lambda *_: {}, log_tail=lambda _: {"lines": []},
                log_path=root / "server.log", spawn=lambda *_: process,
                now=lambda: 0.0, sleep=lambda _: None,
            )

            with patch.dict(sys.modules, {"psutil": fake_psutil}):
                with self.assertRaisesRegex(RuntimeError, "identity"):
                    service.start()

        self.assertTrue(process.terminated)
        self.assertTrue(process.waited)
        self.assertEqual(writes, [])

    def test_state_persistence_failure_terminates_verified_owned_process(self) -> None:
        import sys
        import tempfile
        import types
        from unittest.mock import patch
        from dashboard.application.server_startup import ServerStartupService

        class Backend:
            def build_command(self, *_args, **_kwargs):
                return ["llama-server"]

        class Process:
            pid = 99

            def __init__(self) -> None:
                self.terminated = False
                self.waited = False

            def terminate(self) -> None:
                self.terminated = True

            def wait(self, timeout=None) -> None:
                self.waited = True

            def poll(self):
                return None

        class Identity:
            def __init__(self, _pid: int) -> None:
                return

            def create_time(self) -> float:
                return 100.0

            def exe(self) -> str:
                return "C:/runtime/llama-server.exe"

        process = Process()
        fake_psutil = types.SimpleNamespace(Process=Identity)
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            model = root / "model.gguf"
            model.write_bytes(b"gguf")
            state = {"active_model_id": "model", "models": {"model": {}}}
            service = ServerStartupService(
                load_state=lambda: state, save_state=lambda _value: None,
                executable=lambda: Path("C:/runtime/llama-server.exe"), stop=lambda **_: None,
                active_path=lambda _: model, load_options=lambda: {"model": {}},
                runtime_kind=lambda _: "official", backend=lambda _: Backend(),
                serving_model_name=lambda _: "served", option_args=lambda _: [], watch=lambda _: None,
                health=lambda _: False, register_endpoint=lambda *_: {}, log_tail=lambda _: {"lines": []},
                log_path=root / "server.log", spawn=lambda *_: process,
                now=lambda: 0.0, sleep=lambda _: None,
                mutate_state=lambda _callback: (_ for _ in ()).throw(OSError("state write failed")),
            )

            with patch.dict(sys.modules, {"psutil": fake_psutil}):
                with self.assertRaisesRegex(OSError, "state write failed"):
                    service.start()

        self.assertTrue(process.terminated)
        self.assertTrue(process.waited)

    def test_endpoint_state_write_failure_terminates_worker_and_clears_pid(self) -> None:
        import sys
        import tempfile
        import types
        from unittest.mock import patch
        from dashboard.application.server_startup import ServerStartupService

        class Backend:
            def build_command(self, *_args, **_kwargs):
                return ["llama-server"]

        class Process:
            pid = 99

            def __init__(self) -> None:
                self.terminated = False
                self.waited = False

            def terminate(self) -> None:
                self.terminated = True

            def wait(self, timeout=None) -> None:
                self.waited = True

            def poll(self):
                return None

        class Identity:
            def __init__(self, _pid: int) -> None:
                return

            def create_time(self) -> float:
                return 100.0

            def exe(self) -> str:
                return "C:/runtime/llama-server.exe"

        process = Process()
        state = {"active_model_id": "model", "models": {"model": {}}}
        mutations = 0
        stops: list[dict[str, object]] = []

        def mutate(callback) -> None:
            nonlocal mutations
            mutations += 1
            if mutations == 2:
                raise OSError("endpoint state write failed")
            callback(state)

        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            model = root / "model.gguf"
            model.write_bytes(b"gguf")
            service = ServerStartupService(
                load_state=lambda: state, save_state=lambda _value: None,
                executable=lambda: Path("C:/runtime/llama-server.exe"),
                stop=lambda **kwargs: stops.append(kwargs),
                active_path=lambda _: model, load_options=lambda: {"model": {}},
                runtime_kind=lambda _: "official", backend=lambda _: Backend(),
                serving_model_name=lambda _: "served", option_args=lambda _: [], watch=lambda _: None,
                health=lambda _: True, register_endpoint=lambda *_: {"model": "served"},
                log_tail=lambda _: {"lines": []}, log_path=root / "server.log",
                spawn=lambda *_: process, now=lambda: 0.0, sleep=lambda _: None,
                mutate_state=mutate,
            )

            with patch.dict(sys.modules, {"psutil": types.SimpleNamespace(Process=Identity)}):
                with self.assertRaisesRegex(OSError, "endpoint state write failed"):
                    service.start()

        self.assertTrue(process.terminated)
        self.assertTrue(process.waited)
        self.assertIsNone(state.get("pid"))
        self.assertEqual(stops, [
            {"preserve_log": True, "keep_endpoint": True},
            {"preserve_log": True},
        ])

    def test_cleanup_failure_retains_worker_identity_state(self) -> None:
        import sys
        import tempfile
        import types
        from unittest.mock import patch
        from dashboard.application.server_startup import ServerStartupService

        class Backend:
            def build_command(self, *_args, **_kwargs):
                return ["llama-server"]

        class Process:
            pid = 99

            def terminate(self) -> None:
                raise OSError("terminate failed")

            def kill(self) -> None:
                raise OSError("kill failed")

            def wait(self, timeout=None) -> None:
                raise TimeoutError("still running")

            def poll(self):
                return None

        class Identity:
            def __init__(self, _pid: int) -> None:
                return

            def create_time(self) -> float:
                return 100.0

            def exe(self) -> str:
                return "C:/runtime/llama-server.exe"

        process = Process()
        state = {"active_model_id": "model", "models": {"model": {}}}
        mutations = 0

        def mutate(callback) -> None:
            nonlocal mutations
            mutations += 1
            if mutations == 2:
                raise OSError("endpoint state write failed")
            callback(state)

        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            model = root / "model.gguf"
            model.write_bytes(b"gguf")
            service = ServerStartupService(
                load_state=lambda: state, save_state=lambda _value: None,
                executable=lambda: Path("C:/runtime/llama-server.exe"), stop=lambda **_kwargs: None,
                active_path=lambda _: model, load_options=lambda: {"model": {}},
                runtime_kind=lambda _: "official", backend=lambda _: Backend(),
                serving_model_name=lambda _: "served", option_args=lambda _: [], watch=lambda _: None,
                health=lambda _: True, register_endpoint=lambda *_: {"model": "served"},
                log_tail=lambda _: {"lines": []}, log_path=root / "server.log",
                spawn=lambda *_: process, now=lambda: 0.0, sleep=lambda _: None,
                mutate_state=mutate,
            )

            with patch.dict(sys.modules, {"psutil": types.SimpleNamespace(Process=Identity)}):
                with self.assertRaisesRegex(RuntimeError, "retaining worker ownership state"):
                    service.start()

        self.assertEqual(state.get("pid"), 99)
        self.assertEqual(state.get("worker_identity", {}).get("pid"), 99)


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
    pid = os.getpid()

    def poll(self):
        return None

if __name__ == "__main__":
    unittest.main()
