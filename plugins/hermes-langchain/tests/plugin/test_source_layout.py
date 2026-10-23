from __future__ import annotations

import ast
import importlib.util
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[2]
SOURCE_ROOT = ROOT / "src" / "hermes_langchain"


class SourceLayoutTests(unittest.TestCase):
    def test_tests_live_under_tests_package(self) -> None:
        tests_root = ROOT / "tests"
        self.assertEqual(
            ["__init__.py"],
            sorted(path.name for path in tests_root.glob("*.py")),
        )
        for package in ("plugin", "authoring", "kanban", "controller"):
            with self.subTest(package=package):
                self.assertTrue((tests_root / package / "__init__.py").is_file())
        self.assertTrue((tests_root / "authoring" / "graph" / "__init__.py").is_file())
        self.assertTrue((tests_root / "controller" / "workers" / "__init__.py").is_file())
        self.assertTrue((tests_root / "support" / "__init__.py").is_file())

    def test_production_python_lives_under_feature_packages(self) -> None:
        allowed_entrypoints = {
            ROOT / "__init__.py",
            ROOT / "dashboard" / "plugin_api.py",
        }
        misplaced: list[str] = []
        for path in ROOT.rglob("*.py"):
            if ".venv" in path.parts or path.name.startswith("test_") or "tests" in path.parts:
                continue
            relative = path.relative_to(ROOT)
            if (
                len(relative.parts) >= 4
                and relative.parts[0] == "skills"
                and relative.parts[2] == "scripts"
            ):
                continue
            if path in allowed_entrypoints or SOURCE_ROOT in path.parents:
                continue
            misplaced.append(relative.as_posix())
        self.assertEqual([], sorted(misplaced))

    def test_source_is_split_by_feature_module(self) -> None:
        for package in ("plugin", "authoring", "kanban", "controller"):
            with self.subTest(package=package):
                self.assertTrue((SOURCE_ROOT / package / "__init__.py").is_file())
        self.assertFalse((SOURCE_ROOT / "runtime").exists())

    def test_required_host_entrypoints_are_thin_adapters(self) -> None:
        limits = {
            ROOT / "__init__.py": 80,
            ROOT / "dashboard" / "plugin_api.py": 120,
        }
        for path, limit in limits.items():
            with self.subTest(path=path.name):
                self.assertLessEqual(len(path.read_text(encoding="utf-8").splitlines()), limit)

    def test_plugin_register_is_a_thin_composition_root(self) -> None:
        path = SOURCE_ROOT / "plugin" / "entrypoint.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        register = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "register"
        )
        self.assertLessEqual(register.end_lineno - register.lineno + 1, 40)

    def test_task_graph_script_is_a_thin_compatibility_entrypoint(self) -> None:
        graph_package = SOURCE_ROOT / "authoring" / "graph"
        path = graph_package / "build_task_graph.py"
        self.assertLessEqual(len(path.read_text(encoding="utf-8").splitlines()), 180)
        for module in (
            "implementation_card.py",
            "legacy_implementation_card.py",
            "implementation_batch.py",
            "native_authoring.py",
            "task_graph_cli.py",
            "task_graph_commands.py",
            "task_graph_io.py",
        ):
            with self.subTest(module=module):
                self.assertTrue((graph_package / module).is_file())

    def test_plugin_entrypoint_delegates_to_controller_and_tool_handlers(self) -> None:
        plugin_package = SOURCE_ROOT / "plugin"
        path = plugin_package / "entrypoint.py"
        self.assertLessEqual(len(path.read_text(encoding="utf-8").splitlines()), 500)
        for module in ("registration.py", "tool_handlers.py"):
            with self.subTest(module=module):
                self.assertTrue((plugin_package / module).is_file())
        for legacy_module in (
            "runtime_state.py",
            "routing_policy.py",
            "dispatch_service.py",
        ):
            with self.subTest(legacy_module=legacy_module):
                self.assertFalse((plugin_package / legacy_module).exists())

    def test_source_package_is_importable(self) -> None:
        package_init = SOURCE_ROOT / "__init__.py"
        spec = importlib.util.spec_from_file_location(
            "_hermes_langchain_source_layout",
            package_init,
            submodule_search_locations=[str(SOURCE_ROOT)],
        )
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader if spec else None)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        try:
            spec.loader.exec_module(module)
        finally:
            sys.modules.pop(spec.name, None)
        self.assertTrue(callable(module.register))


if __name__ == "__main__":
    unittest.main()
