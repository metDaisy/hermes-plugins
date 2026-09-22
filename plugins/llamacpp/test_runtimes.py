"""Tests for llama.cpp backend adapters."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from dashboard.backends import backend_view, get_runtime, runtime_ids


class RuntimeAdapterTests(unittest.TestCase):
    def test_registry_keeps_official_alias_and_exposes_prism(self) -> None:
        self.assertEqual(get_runtime("official").key, "official")
        self.assertEqual(get_runtime("llamacpp").key, "official")
        self.assertEqual(get_runtime("custom").key, "prism_ml")
        self.assertEqual(get_runtime("prism_ml").key, "prism_ml")
        self.assertEqual(runtime_ids(), ("official", "prism_ml"))

    def test_backend_contract_exposes_managed_roots_and_versions(self) -> None:
        machine_root = Path("C:/Users/leee/AppData/Local/hermes")
        official = backend_view("official", {"installed_tag": "b10976"}, machine_root)
        prism = backend_view("prism_ml", {"prism_release_tag": "prism-b10709-9a9394a"}, machine_root)
        self.assertEqual(official.label, "llama.cpp official")
        self.assertEqual(official.managed_root, str(machine_root / "runtimes" / "llamacpp"))
        self.assertEqual(official.version, "b10976")
        self.assertEqual(prism.label, "Prism-ML llama.cpp")
        self.assertEqual(prism.managed_root, str(machine_root / "runtimes" / "prism-ml"))
        self.assertEqual(prism.version, "prism-b10709-9a9394a")

    def test_prism_backend_accepts_only_prism_bonsai_gguf(self) -> None:
        prism = get_runtime("prism_ml")
        self.assertTrue(prism.accepts_model("prism-ml/Ternary-Bonsai-2-27B-gguf", ["Ternary-Bonsai-2-27B-PQ2_0.gguf"]))
        self.assertTrue(prism.accepts_model("prism-ml/Bonsai-8B-gguf", ["Bonsai-8B-Q1_0.gguf"]))
        self.assertFalse(prism.accepts_model("ornith-ai/Ornith-1.5-35B-A3B-GGUF", ["Ornith-1.5-35B-Q6_K.gguf"]))
        self.assertFalse(prism.accepts_model("prism-ml/Bonsai-8B-mlx-1bit", ["model.safetensors"]))

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
        local_command = get_runtime("prism_ml").build_command(
            executable, 18434, "Ternary-Bonsai-2-27B-PQ2_0", {"paths": ["model.gguf"]}, {}
        )
        self.assertIn("--jinja", prism_command)
        self.assertIn("--jinja", local_command)
        self.assertNotIn("--jinja", official_command)
        self.assertEqual(prism_command[-2:], ["--hf-file", "model.gguf"])


if __name__ == "__main__":
    unittest.main()
