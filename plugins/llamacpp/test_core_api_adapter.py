"""Contract tests for the Core-native llama.cpp plugin adapter."""
from __future__ import annotations

import asyncio
import importlib.util
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).parent / "dashboard" / "core_api.py"


class CoreApiAdapterTests(unittest.TestCase):
    def load_module(self):
        spec = importlib.util.spec_from_file_location("llamacpp_core_api", MODULE_PATH)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_snapshot_projects_core_status_hardware_and_role_assignments(self) -> None:
        module = self.load_module()
        calls: list[tuple[str, str, object]] = []

        async def request(method: str, path: str, body=None):
            calls.append((method, path, body))
            if path == "/api/local-models/status":
                return {
                    "runtime_installed": True,
                    "runtime_backend": "cuda",
                    "server_running": True,
                    "active_model_id": "main-model",
                    "models": [{"id": "main-model"}, {"id": "small-model"}],
                    "loaded_models": {"main-model": "loaded"},
                }
            if path == "/api/local-models/hardware":
                return {"gpu_name": "RTX", "vram_total_bytes": 16 * 1024**3}
            if path == "/api/model/auxiliary":
                return {
                    "main": {"provider": "llamacpp", "model": "main-model"},
                    "tasks": [
                        {"task": "compression", "provider": "llamacpp", "model": "small-model"}
                    ],
                }
            if path == "/api/config":
                return {"local_runtime": {"models_max": 1, "enabled": True}}
            raise AssertionError(path)

        result = asyncio.run(module.CoreApiAdapter(request).snapshot())

        self.assertEqual(result["mode"], "core_native")
        self.assertEqual(result["policy"], "models_max_1_autoload")
        self.assertEqual(result["profiles"]["main"]["model_id"], "main-model")
        self.assertEqual(result["profiles"]["compression"]["model_id"], "small-model")
        self.assertEqual(result["hardware"]["gpu_name"], "RTX")
        self.assertEqual(result["models_max"], 1)
        self.assertTrue(result["exclusive_residency"])
        self.assertEqual([path for _, path, _ in calls], [
            "/api/local-models/status",
            "/api/local-models/hardware",
            "/api/model/auxiliary",
            "/api/config",
        ])

    def test_assign_uses_core_model_assignment_contract(self) -> None:
        module = self.load_module()
        calls: list[tuple[str, str, object]] = []

        async def request(method: str, path: str, body=None):
            calls.append((method, path, body))
            return {"ok": True}

        adapter = module.CoreApiAdapter(request)
        asyncio.run(adapter.assign("main", "main-model"))
        asyncio.run(adapter.assign("compression", "small-model"))

        self.assertEqual(calls, [
            ("POST", "/api/model/set", {
                "scope": "main", "task": "", "provider": "llamacpp", "model": "main-model",
                "base_url": "", "api_key": "",
            }),
            ("POST", "/api/model/set", {
                "scope": "auxiliary", "task": "compression", "provider": "llamacpp", "model": "small-model",
                "base_url": "", "api_key": "",
            }),
        ])

    def test_control_actions_delegate_to_core_local_models_routes(self) -> None:
        module = self.load_module()
        calls: list[tuple[str, str, object]] = []

        async def request(method: str, path: str, body=None):
            calls.append((method, path, body))
            return {"ok": True}

        adapter = module.CoreApiAdapter(request)
        asyncio.run(adapter.server("stop"))
        asyncio.run(adapter.eject("main-model"))
        asyncio.run(adapter.activate("small-model"))

        self.assertEqual(calls, [
            ("POST", "/api/local-models/server", {"action": "stop"}),
            ("POST", "/api/local-models/eject", {"model_id": "main-model"}),
            ("POST", "/api/local-models/activate", {"model_id": "small-model"}),
        ])

    def test_plugin_api_exposes_only_core_native_routes(self) -> None:
        plugin_path = Path(__file__).parent / "dashboard" / "plugin_api.py"
        spec = importlib.util.spec_from_file_location("llamacpp_plugin_api_contract", plugin_path)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        paths = [route.path for route in module.router.routes]
        expected = {
            "/core/status",
            "/core/jobs",
            "/core/jobs/{job_id}",
            "/core/profiles/{role}",
            "/core/server",
            "/core/eject",
            "/core/activate",
        }
        self.assertTrue(expected.issubset(paths))
        self.assertNotIn("/{path:path}", paths)

    def test_core_request_forwards_loopback_session_auth_without_logging_it(self) -> None:
        plugin_path = Path(__file__).parent / "dashboard" / "plugin_api.py"
        spec = importlib.util.spec_from_file_location("llamacpp_plugin_api_headers", plugin_path)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        request = type("Request", (), {"headers": {
            "X-Hermes-Session-Token": "[REDACTED]",
            "Authorization": "Bearer [REDACTED]",
            "X-Unrelated": "not-forwarded",
        }})()

        self.assertEqual(module._forward_headers(request), {
            "x-hermes-session-token": "[REDACTED]",
            "authorization": "Bearer [REDACTED]",
        })

    def test_desktop_uses_core_native_routes_without_starting_coordinator(self) -> None:
        source = (Path(__file__).parent / "desktop" / "plugin.js").read_text(encoding="utf-8")

        self.assertIn("/core/status", source)
        self.assertIn("/core/profiles/", source)
        self.assertIn("Core Local Models", source)
        self.assertIn("exclusive_residency", source)
        self.assertIn("models_max", source)
        self.assertNotIn("refetchInterval: 5000", source)
        self.assertNotIn("startDesktopLease", source)
        self.assertNotIn("Singleton proxy", source)
        self.assertNotIn("Prism-ML", source)
    def test_plugin_backend_does_not_start_or_proxy_legacy_coordinator(self) -> None:
        source = (Path(__file__).parent / "dashboard" / "plugin_api.py").read_text(encoding="utf-8")

        self.assertNotIn("COORDINATOR_PORT", source)
        self.assertNotIn("_ensure_coordinator", source)
        self.assertNotIn("coordinator_server.py", source)
        self.assertNotIn("StreamingResponse", source)
    def test_manifest_metadata_describes_core_native_plugin(self) -> None:
        root = Path(__file__).parent
        plugin_yaml = (root / "plugin.yaml").read_text(encoding="utf-8")
        manifest = (root / "dashboard" / "manifest.json").read_text(encoding="utf-8")
        readme = (root / "README.md").read_text(encoding="utf-8")

        self.assertIn("version: 0.3.0", plugin_yaml)
        self.assertIn("Core-native", plugin_yaml)
        self.assertIn("Core-native", manifest)
        self.assertIn("Hermes Core", readme)
        self.assertNotIn("Standalone llama.cpp", readme)


if __name__ == "__main__":
    unittest.main()
