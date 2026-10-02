from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch


class NativeRouterPlanTests(unittest.TestCase):
    def test_builds_single_resident_native_router_plan_for_two_official_roles(self) -> None:
        from dashboard.application.native_router import build_native_router_plan

        plan = build_native_router_plan(
            executable=Path("C:/runtime/llama-server.exe"),
            port=18434,
            preset_path=Path("C:/runtime/router-models.ini"),
            profiles={
                "main": {"configured": True, "runtime_kind": "official", "logical_model": "main-local"},
                "compression": {"configured": True, "runtime_kind": "official", "logical_model": "compression-local"},
            },
            model_paths={
                "main": Path("E:/gguf/main.gguf"),
                "compression": Path("E:/gguf/compress.gguf"),
            },
            model_options={
                "main": {"ctx-size": "32768", "n-gpu-layers": "99", "port": "19999", "api-key": "secret"},
                "compression": {"ctx-size": "8192", "flash-attn": "on", "load-on-startup": "on"},
            },
        )

        self.assertIsNotNone(plan)
        assert plan is not None
        self.assertEqual(plan.command[0], "C:/runtime/llama-server.exe")
        self.assertIn("--models-max", plan.command)
        self.assertEqual(plan.command[plan.command.index("--models-max") + 1], "1")
        self.assertIn("--models-preset", plan.command)
        self.assertNotIn("--model", plan.command)
        self.assertIn("[main-local]", plan.preset_text)
        self.assertIn("[compression-local]", plan.preset_text)
        self.assertIn("model = E:/gguf/main.gguf", plan.preset_text)
        self.assertIn("load-on-startup = true", plan.preset_text)
        self.assertEqual(plan.preset_text.count("load-on-startup"), 1)
        self.assertIn("ctx-size = 32768", plan.preset_text)
        self.assertNotIn("port =", plan.preset_text)
        self.assertNotIn("api-key", plan.preset_text)
        self.assertNotIn("secret", plan.preset_text)

    def test_declines_router_when_roles_need_different_runtimes(self) -> None:
        from dashboard.application.native_router import build_native_router_plan

        plan = build_native_router_plan(
            executable=Path("C:/runtime/llama-server.exe"), port=18434,
            preset_path=Path("C:/runtime/router-models.ini"),
            profiles={
                "main": {"configured": True, "runtime_kind": "official", "logical_model": "main-local"},
                "compression": {"configured": True, "runtime_kind": "prism_ml", "logical_model": "compression-local"},
            },
            model_paths={"main": Path("E:/main.gguf"), "compression": Path("E:/compress.gguf")},
            model_options={"main": {}, "compression": {}},
        )

        self.assertIsNone(plan)


class ActivityLogProjectionTests(unittest.TestCase):
    def test_unwraps_router_forwarded_child_jsonl(self) -> None:
        import json
        from dashboard.application.activity_log_projection import project_activity_lines

        child = json.dumps({
            "type": "log", "level": "info",
            "msg": "slot prompt processing, n_tokens = 256, progress = 0.25, 50.0 tokens per second\n",
        })
        outer = json.dumps({"type": "log", "level": "info", "msg": f"[19001] {child}\n"})

        result = project_activity_lines([
            'I srv spawning server instance with name=main-local on port 19001',
            outer,
        ])

        self.assertIn("[Main] prompt 처리 25% · 256 tok · 50.0 tok/s", result["lines"])

    def test_projects_router_and_inference_events_without_prompt_content(self) -> None:
        from dashboard.application.activity_log_projection import project_activity_lines

        raw = [
            '{"type":"log","level":"info","msg":"srv llama_server: listening on http://127.0.0.1:18434\\n"}',
            '{"type":"log","level":"info","msg":"srv spawning server instance with name=main-local on port 19001\\n"}',
            '[19001] slot prompt processing, n_tokens = 4096, progress = 0.33, 1200.0 tokens per second',
            '[19001] slot n_gen = 128, tg = 73.4 t/s, tg_3s = 71.0 t/s',
            'request: {"prompt":"PRIVATE PROMPT CONTENT"}',
            'W srv evicting idle LRU name=main-local for a queued request',
            'I srv spawning server instance with name=compression-local on port 19002',
            '[19002] slot prompt eval time = 1000.00 ms / 2048 tokens',
            '[19002] slot total time = 2390.00 ms / 2176 tokens',
        ]

        result = project_activity_lines(raw)

        self.assertIn('[Server] 서버 준비 완료 · :18434', result["lines"])
        self.assertIn('[Main] prompt 처리 33% · 4096 tok · 1200.0 tok/s', result["lines"])
        self.assertIn('[Main] 생성 128 tok · 73.4 tok/s', result["lines"])
        self.assertIn('[Main] 모델 언로드 중', result["lines"])
        self.assertIn('[Compress] 모델 로딩 중', result["lines"])
        self.assertIn('[Compress] prompt 처리 완료 · 2048 tok · 1.00초', result["lines"])
        self.assertIn('[Compress] 요청 완료 · 2176 tok · 2.39초', result["lines"])
        self.assertNotIn('PRIVATE PROMPT CONTENT', "\n".join(result["lines"]))
        self.assertTrue(any(event["event"] == "prompt_progress" for event in result["events"]))


class ProfileModelRoutingTests(unittest.TestCase):
    BASE = """model:\n  provider: openai-codex\n  default: gpt-5.6-luna\n  base_url: https://chatgpt.com/backend-api/codex\n  api_mode: codex_responses\n  reasoning_effort: high\nfallback_providers: []\nagent:\n  max_turns: 90\nauxiliary:\n  compression:\n    provider: openai-codex\n    model: gpt-5.6-luna\n    reasoning_effort: medium\n"""

    def test_routes_main_and_compression_to_local_and_fixes_fallback(self) -> None:
        from dashboard.application.profile_model_routing import sync_profile_models

        updated = sync_profile_models(
            self.BASE, base_url="http://127.0.0.1:18380/v1",
            main_served=True, compression_served=True,
        )

        self.assertIn("  provider: llamacpp-local\n  default: main-local", updated)
        self.assertIn("  base_url: http://127.0.0.1:18380/v1", updated)
        self.assertIn("  api_mode: chat_completions", updated)
        self.assertIn("fallback_providers:\n  - provider: openai-codex\n    model: gpt-5.6-luna", updated)
        self.assertEqual(updated.count("fallback_providers:"), 1)
        self.assertIn("  compression:\n    provider: llamacpp-local\n    model: compression-local", updated)
        self.assertIn("    reasoning_effort: medium", updated)

    def test_routes_missing_local_roles_to_luna_high(self) -> None:
        from dashboard.application.profile_model_routing import sync_profile_models

        updated = sync_profile_models(
            self.BASE, base_url="http://127.0.0.1:18380/v1",
            main_served=False, compression_served=False,
        )

        self.assertIn("  provider: openai-codex\n  default: gpt-5.6-luna-high", updated)
        self.assertIn("  base_url: https://chatgpt.com/backend-api/codex", updated)
        self.assertIn("  api_mode: codex_responses", updated)
        self.assertIn("  compression:\n    provider: openai-codex\n    model: gpt-5.6-luna-high", updated)
        self.assertIn("fallback_providers:\n  - provider: openai-codex\n    model: gpt-5.6-luna", updated)

    def test_keeps_aux_on_luna_high_when_only_main_is_local(self) -> None:
        from dashboard.application.profile_model_routing import sync_profile_models

        updated = sync_profile_models(
            self.BASE, base_url="http://127.0.0.1:18380/v1",
            main_served=True, compression_served=False,
        )

        self.assertIn("  default: main-local", updated)
        self.assertIn("  compression:\n    provider: openai-codex\n    model: gpt-5.6-luna-high", updated)


class ProductionIntegrationTests(unittest.TestCase):
    def test_unexpected_worker_exit_unregisters_local_models_and_restores_remote_policy(self) -> None:
        import threading
        from dashboard.application.server_lifecycle import ServerLifecycleService

        state = {"pid": 99, "worker_identity": {"pid": 99}, "custom_endpoint": {"model": "main-local"}}
        unregistered = threading.Event()

        class Process:
            pid = 99
            def wait(self): return 1

        service = ServerLifecycleService(
            load_state=lambda: state,
            save_state=lambda value: state.update(value),
            pid_alive=lambda _pid: False,
            terminate=lambda _pid: True,
            unregister_endpoint=unregistered.set,
            log_path=Path("unused.log"),
            mutate_state=lambda update: update(state) or state,
        )

        service.watch(Process())

        self.assertTrue(unregistered.wait(1.0))
        self.assertIsNone(state["pid"])
        self.assertIsNone(state["custom_endpoint"])

    def test_intentional_swap_exit_does_not_unregister_the_shared_endpoint(self) -> None:
        import threading
        import time
        from dashboard.application.server_lifecycle import ServerLifecycleService

        state = {"pid": 99, "worker_identity": {"pid": 99}, "custom_endpoint": {"model": "main-local"}}
        exited = threading.Event()
        watcher_returned = threading.Event()
        unregistered: list[bool] = []

        class Process:
            pid = 99
            def wait(self):
                exited.wait(1.0)
                watcher_returned.set()
                return 0

        def terminate(_pid: int) -> bool:
            exited.set()
            self.assertTrue(watcher_returned.wait(1.0))
            time.sleep(0.05)
            return True

        watcher_service = ServerLifecycleService(
            load_state=lambda: state,
            save_state=lambda value: state.update(value),
            pid_alive=lambda pid: pid == 99 and state.get("pid") == 99,
            terminate=terminate,
            unregister_endpoint=lambda: unregistered.append(True),
            log_path=Path("unused.log"),
            mutate_state=lambda update: update(state) or state,
        )
        stopper_service = ServerLifecycleService(
            load_state=lambda: state,
            save_state=lambda value: state.update(value),
            pid_alive=lambda pid: pid == 99 and state.get("pid") == 99,
            terminate=terminate,
            unregister_endpoint=lambda: unregistered.append(True),
            log_path=Path("unused.log"),
            mutate_state=lambda update: update(state) or state,
        )

        watcher_service.watch(Process())
        stopper_service.stop(preserve_log=True, keep_endpoint=True)

        self.assertEqual(unregistered, [])
        self.assertEqual(state["custom_endpoint"], {"model": "main-local"})

    def test_execution_profile_snapshot_reports_native_router_mode(self) -> None:
        from dashboard.application.execution_profiles import ExecutionProfileService

        state = {
            "execution_mode": "native_router",
            "execution_profiles": {
                "main": {"runtime_kind": "official", "model_id": "main-model", "preset_id": ""},
                "compression": {"runtime_kind": "official", "model_id": "compress-model", "preset_id": ""},
            },
        }
        service = ExecutionProfileService(
            load_state=lambda: state,
            save_state=lambda value: state.update(value),
            accepts=lambda _runtime, _model: True,
        )

        self.assertEqual(service.snapshot()["execution_mode"], "native_router")

    def test_server_startup_uses_native_router_plan_and_persists_its_preset(self) -> None:
        import sys
        import tempfile
        import types
        from dashboard.application.native_router import NativeRouterPlan
        from dashboard.application.server_startup import ServerStartupService

        class Process:
            pid = 321

            def poll(self): return None
            def terminate(self): return None
            def kill(self): return None
            def wait(self, timeout=None): return 0

        class Identity:
            def __init__(self, _pid): pass
            def create_time(self): return 123.0
            def exe(self): return "C:/runtime/llama-server.exe"

        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            executable = root / "llama-server.exe"
            executable.write_bytes(b"exe")
            preset = root / "router-models.ini"
            state = {"active_model_id": "main-model", "models": {"main-model": {}}, "port": 18434}
            spawned: list[list[str]] = []
            endpoints: list[str] = []
            plan = NativeRouterPlan(
                command=[str(executable), "--models-max", "1"],
                preset_text="version = 1\n[main-local]\nmodel = E:/main.gguf\n",
            )
            service = ServerStartupService(
                load_state=lambda: state, save_state=lambda value: state.update(value),
                executable=lambda: executable, stop=lambda **_: None,
                active_path=lambda _: root / "unused.gguf", load_options=lambda: {},
                runtime_kind=lambda _: "official", backend=lambda _: None,
                serving_model_name=lambda model_id: model_id, option_args=lambda _: [], watch=lambda _: None,
                health=lambda _: True,
                register_endpoint=lambda _port, model: endpoints.append(model) or {"model": model},
                log_tail=lambda _: {"lines": []}, log_path=root / "activity.log",
                spawn=lambda command, _log, _exe: spawned.append(list(command)) or Process(),
                now=lambda: 0.0, sleep=lambda _: None,
                mutate_state=lambda update: update(state) or state,
                router_plan=lambda _state, _executable: plan,
                router_preset_path=preset,
            )

            with patch.dict(sys.modules, {"psutil": types.SimpleNamespace(Process=Identity)}):
                service.start()

            self.assertEqual(spawned, [plan.command])
            self.assertEqual(preset.read_text(encoding="utf-8"), plan.preset_text)
            self.assertEqual(endpoints, ["main-local"])
            self.assertEqual(state["execution_mode"], "native_router")

    def test_router_http_health_does_not_register_before_main_model_is_loaded(self) -> None:
        import sys
        import tempfile
        import types
        from dashboard.application.native_router import NativeRouterPlan
        from dashboard.application.server_startup import ServerStartupService

        class Process:
            pid = 322
            def poll(self): return None
            def terminate(self): return None
            def kill(self): return None
            def wait(self, timeout=None): return 0

        class Identity:
            def __init__(self, _pid): pass
            def create_time(self): return 124.0
            def exe(self): return "C:/runtime/llama-server.exe"

        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            executable = root / "llama-server.exe"
            executable.write_bytes(b"exe")
            state = {"active_model_id": "main-model", "models": {"main-model": {}}, "port": 18434}
            clock = [0.0]
            ready_checks: list[str] = []
            endpoints: list[str] = []

            def router_ready(_port: int, model: str) -> bool:
                ready_checks.append(model)
                return len(ready_checks) >= 2

            service = ServerStartupService(
                load_state=lambda: state, save_state=lambda value: state.update(value),
                executable=lambda: executable, stop=lambda **_: None,
                active_path=lambda _: root / "unused.gguf", load_options=lambda: {},
                runtime_kind=lambda _: "official", backend=lambda _: None,
                serving_model_name=lambda model_id: model_id, option_args=lambda _: [], watch=lambda _: None,
                health=lambda _: True,
                register_endpoint=lambda _port, model: endpoints.append(model) or {"model": model},
                log_tail=lambda _: {"lines": []}, log_path=root / "activity.log",
                spawn=lambda *_: Process(), now=lambda: clock[0],
                sleep=lambda seconds: clock.__setitem__(0, clock[0] + seconds),
                mutate_state=lambda update: update(state) or state,
                router_plan=lambda *_: NativeRouterPlan(
                    command=[str(executable)], preset_text="version = 1\n", port=18434,
                ),
                router_preset_path=root / "router-models.ini",
                router_ready=router_ready,
            )

            with patch.dict(sys.modules, {"psutil": types.SimpleNamespace(Process=Identity)}):
                service.start()

            self.assertEqual(ready_checks, ["main-local", "main-local"])
            self.assertEqual(endpoints, ["main-local"])

    def test_native_router_role_switch_does_not_restart_the_router_process(self) -> None:
        from dashboard import backend_impl as api

        state = {
            "active_model_id": "main-model", "active_role": "main",
            "execution_mode": "native_router", "pid": 1,
        }
        profiles = {
            "profiles": {
                "main": {"configured": True, "model_id": "main-model", "runtime_kind": "official"},
                "compression": {"configured": True, "model_id": "compress-model", "runtime_kind": "official"},
            }
        }

        with patch.object(api, "execution_profiles", return_value=profiles), \
                patch.object(api, "_state", side_effect=lambda: dict(state)), \
                patch.object(api, "_is_server_running", return_value=True), \
                patch.object(api, "_mutate_state", side_effect=lambda update: update(state) or dict(state)), \
                patch.object(api, "_server_startup", side_effect=AssertionError("router must not restart")):
            result = api.ensure_execution_role("compression")

        self.assertTrue(result["already_running"])
        self.assertTrue(result["router_managed"])
        self.assertEqual(state["active_role"], "compression")
        self.assertEqual(state["transition_phase"], "ROUTER_READY")

    def test_native_router_preserves_the_logical_alias_for_llama_server_routing(self) -> None:
        from dashboard.inference_proxy import rewrite_logical_model

        body = b'{"model":"compression-local","messages":[]}'
        state = {
            "execution_mode": "native_router",
            "execution_profiles": {"compression": {"model_id": "physical-compression-model"}},
        }

        self.assertEqual(rewrite_logical_model(body, state), body)

    def test_server_log_tail_returns_projected_lines_and_keeps_raw_evidence(self) -> None:
        from dashboard import backend_impl as api

        class Lifecycle:
            def log_tail(self, _limit):
                return {
                    "path": "activity.log", "size_bytes": 100,
                    "lines": [
                        'I srv spawning server instance with name=main-local on port 19001',
                        '[19001] slot prompt processing, n_tokens = 100, progress = 0.50, 25.0 tokens per second',
                        'request prompt=PRIVATE',
                    ],
                }

        with patch.object(api, "_server_lifecycle", return_value=Lifecycle()):
            result = api._server_log_tail(250)

        self.assertEqual(result["lines"], [
            "[Main] 모델 로딩 중",
            "[Main] prompt 처리 50% · 100 tok · 25.0 tok/s",
        ])
        self.assertEqual(len(result["raw_lines"]), 3)
        self.assertNotIn("PRIVATE", "\n".join(result["lines"]))

    def test_endpoint_sync_changes_model_policy_only_for_worker_profiles(self) -> None:
        import tempfile
        from dashboard import backend_impl as api

        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            pm = root / "project-manager" / "config.yaml"
            other = root / "main" / "config.yaml"
            for path in (pm, other):
                path.parent.mkdir(parents=True)
                path.write_text(ProfileModelRoutingTests.BASE, encoding="utf-8")

            with patch.object(api, "_profile_config_paths", return_value=[pm, other]), \
                    patch.object(api, "_profile_provider_models", return_value={
                        "main-local": 32768, "compression-local": 8192,
                    }):
                api._sync_execution_profile_endpoint()

            pm_text = pm.read_text(encoding="utf-8")
            other_text = other.read_text(encoding="utf-8")
            self.assertIn("  default: main-local", pm_text)
            self.assertIn("    model: compression-local", pm_text)
            self.assertIn("  default: gpt-5.6-luna", other_text)
            self.assertIn("# BEGIN llamacpp endpoint (managed)", other_text)

    def test_endpoint_removal_restores_worker_profiles_to_remote_policy(self) -> None:
        import tempfile
        from dashboard import backend_impl as api
        from dashboard.application.profile_model_routing import sync_profile_models

        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            coder = root / "coder" / "config.yaml"
            coder.parent.mkdir(parents=True)
            local = sync_profile_models(
                ProfileModelRoutingTests.BASE,
                base_url="http://127.0.0.1:18380/v1",
                main_served=True,
                compression_served=True,
            )
            local = api._config_with_execution_profiles(
                local, "http://127.0.0.1:18380/v1", "main-local",
                {"main-local": 32768, "compression-local": 8192},
            )
            coder.write_text(local, encoding="utf-8")

            with patch.object(api, "_profile_config_paths", return_value=[coder]):
                api._unregister_custom_endpoint()

            restored = coder.read_text(encoding="utf-8")
            self.assertNotIn("# BEGIN llamacpp endpoint (managed)", restored)
            self.assertIn("  default: gpt-5.6-luna-high", restored)
            self.assertIn("    model: gpt-5.6-luna-high", restored)
            self.assertIn("fallback_providers:\n  - provider: openai-codex\n    model: gpt-5.6-luna", restored)


if __name__ == "__main__":
    unittest.main()
