"""Pure executor registry contract tests."""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

from tests.support import load_module

CONTROLLER = load_module("controller")


class FakeExecutor:
    def __init__(self, name: str) -> None:
        self.name = name

    def invoke(self, invocation: object) -> str:
        return f"{self.name}:{invocation}"


class ControllerExecutorRegistryTests(unittest.TestCase):
    def test_resolves_only_declared_executor_kinds(self) -> None:
        registrations = {
            kind: FakeExecutor(kind)
            for kind in ("agent", "deterministic", "gateway", "materialize")
        }
        registry = CONTROLLER.ExecutorRegistry(registrations)
        registrations.clear()

        self.assertEqual(
            registry.registered_kinds,
            ("agent", "deterministic", "gateway", "materialize"),
        )
        for kind in registry.registered_kinds:
            self.assertEqual(registry.resolve(kind).invoke("work"), f"{kind}:work")

        with self.assertRaises(CONTROLLER.ExecutorRegistryError) as unknown:
            CONTROLLER.ExecutorRegistry({"batch": FakeExecutor("batch")})
        self.assertEqual(unknown.exception.code, "UNKNOWN_EXECUTOR_KIND")

        partial = CONTROLLER.ExecutorRegistry({"agent": FakeExecutor("agent")})
        with self.assertRaises(CONTROLLER.ExecutorRegistryError) as unregistered:
            partial.resolve("gateway")
        self.assertEqual(unregistered.exception.code, "UNREGISTERED_EXECUTOR")

        with self.assertRaises(CONTROLLER.ExecutorRegistryError) as unknown_lookup:
            partial.resolve("batch")
        self.assertEqual(unknown_lookup.exception.code, "UNKNOWN_EXECUTOR_KIND")

        with self.assertRaises(CONTROLLER.ExecutorRegistryError) as non_string:
            CONTROLLER.ExecutorRegistry({1: FakeExecutor("numeric")})
        self.assertEqual(non_string.exception.code, "UNKNOWN_EXECUTOR_KIND")

        source_root = Path(__file__).resolve().parents[1] / "src"
        isolated_import = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    f"import sys; sys.path.insert(0, {str(source_root)!r}); "
                    "import builtins; real = builtins.__import__; "
                    "builtins.__import__ = lambda name, *args, **kwargs: "
                    "(_ for _ in ()).throw(AssertionError(name)) "
                    "if name.startswith('langgraph') else real(name, *args, **kwargs); "
                    "from hermes_langchain.controller.executors import ExecutorRegistry; "
                    "print(ExecutorRegistry.__name__)"
                ),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(isolated_import.returncode, 0, isolated_import.stderr)


if __name__ == "__main__":
    unittest.main()
