"""Tests for llama.cpp runtime adapters."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from dashboard.runtimes import get_runtime, runtime_ids


class RuntimeAdapterTests(unittest.TestCase):
    def test_registry_keeps_official_alias_and_exposes_prism(self) -> None:
        self.assertEqual(get_runtime("official").key, "llamacpp")
        self.assertEqual(get_runtime("llamacpp").key, "llamacpp")
        self.assertEqual(get_runtime("custom").key, "prism_ml")
        self.assertEqual(get_runtime("prism_ml").key, "prism_ml")
        self.assertEqual(runtime_ids(), ("llamacpp", "prism_ml"))

    def test_official_runtime_resolves_a_direct_server(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            executable = Path(raw_root) / "llama-server.exe"
            executable.write_bytes(b"placeholder")
            self.assertEqual(get_runtime("llamacpp").resolve_executable(executable), executable)

    def test_prism_runtime_discovers_demo_binary_layout(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            demo_root = Path(raw_root)
            executable = demo_root / "bin" / "cuda" / "llama-server.exe"
            executable.parent.mkdir(parents=True)
            executable.write_bytes(b"placeholder")
            self.assertEqual(get_runtime("prism_ml").resolve_executable(demo_root), executable)

    def test_prism_bonsai2_command_enables_jinja_without_changing_official(self) -> None:
        executable = Path("C:/prism/llama-server.exe")
        entry = {"hf_repo": "prism-ml/Ternary-Bonsai-2-27B-gguf", "hf_file": "model.gguf"}
        prism_command = get_runtime("prism_ml").build_command(executable, 18434, "model", entry, {})
        official_command = get_runtime("llamacpp").build_command(executable, 18434, "model", entry, {})
        self.assertIn("--jinja", prism_command)
        self.assertNotIn("--jinja", official_command)
        self.assertEqual(prism_command[-2:], ["--hf-file", "model.gguf"])


if __name__ == "__main__":
    unittest.main()
