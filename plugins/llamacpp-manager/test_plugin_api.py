"""Regression tests for alternate llama.cpp runtimes and parameter presets."""
from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


MODULE_PATH = Path(__file__).parent / "dashboard" / "plugin_api.py"
SPEC = importlib.util.spec_from_file_location("llamacpp_manager_plugin_api", MODULE_PATH)
assert SPEC and SPEC.loader
api = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(api)


class LlamaCppManagerTests(unittest.TestCase):
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
            }]

            with patch.object(api, "STATE_PATH", state_path), patch.object(api, "OPTIONS_PATH", options_path), \
                    patch.object(api, "RUNTIME_ROOT", runtime_root), patch.object(api, "_option_list", return_value=catalog):
                api.save_model_settings("model-a", {"options": {"ctx-size": "8192"}})
                created = api.create_preset({"name": "Coding", "model_id": "model-a"})
                preset_id = created["preset"]["id"]

                listed = api.presets(model_id="model-a")
                self.assertEqual([item["name"] for item in listed["presets"]], ["Coding"])
                self.assertEqual(listed["presets"][0]["options"], {"ctx-size": "8192"})

                api.save_model_settings("model-a", {"options": {"ctx-size": "1024"}})
                applied = api.apply_preset(preset_id, {"model_id": "model-a"})
                self.assertEqual(applied["options"], {"ctx-size": "8192"})
                self.assertEqual(api.model_settings("model-a")["options"], {"ctx-size": "8192"})

                deleted = api.delete_preset(preset_id)
                self.assertEqual(deleted, {"ok": True, "preset_id": preset_id})
                self.assertEqual(api.presets(model_id="model-a")["presets"], [])

    def test_custom_endpoint_advertises_model_provider_profile(self) -> None:
        with patch.object(api, "_profile_config_paths", return_value=[]), patch.object(api, "_load_options", return_value={}):
            endpoint = api._register_custom_endpoint(18434, "bonsai")
            self.assertEqual(endpoint["provider"], "custom")
            self.assertEqual(endpoint["provider_profile"], "llamacpp-local")
            self.assertEqual(endpoint["base_url"], "http://127.0.0.1:18434/v1")

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


if __name__ == "__main__":
    unittest.main()
