from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from pathlib import Path
from typing import Any

PLUGIN_ROOT = Path(__file__).resolve().parents[1]


def load_plugin():
    name = "_hermes_langchain_test_plugin"
    spec = importlib.util.spec_from_file_location(
        name,
        PLUGIN_ROOT / "__init__.py",
        submodule_search_locations=[str(PLUGIN_ROOT)],
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("plugin entrypoint could not be loaded")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class FakeContext:
    def __init__(self) -> None:
        self.tools: dict[str, dict[str, Any]] = {}
        self.skills: dict[str, dict[str, Any]] = {}

    def register_hook(self, _name: str, _handler: Any) -> None:
        return None

    def register_tool(
        self,
        name: str,
        group: str,
        schema: dict[str, Any],
        handler: Any,
        **metadata: Any,
    ) -> None:
        self.tools[name] = {
            "group": group,
            "schema": schema,
            "handler": handler,
            "metadata": metadata,
        }

    def register_skill(
        self,
        name: str,
        path: Path,
        description: str = "",
        frontmatter: dict[str, Any] | None = None,
    ) -> None:
        self.skills[name] = {
            "path": Path(path),
            "description": description,
            "frontmatter": dict(frontmatter or {}),
        }


class PluginTests(unittest.TestCase):
    def test_registers_all_kwc_owned_pm_responsibility_skills(self) -> None:
        plugin = load_plugin()
        context = FakeContext()

        plugin.register(context)

        self.assertEqual(
            {
                "kwc-orchestrator-contract",
                "kwc-create-triage",
                "kwc-triage-authoring-runtime",
                "kwc-impl-authoring-runtime",
                "kwc-review-authoring-runtime",
                "kwc-implementation-delivery-runtime",
                "kwc-review-delivery-runtime",
                "kwc-summary-delivery-runtime",
                "kwc-build-task-graph",
                "kwc-run-workflow",
                "kwc-service-planning",
                "kwc-sync-docs",
                "kwc-update-current-state",
                "kwc-regenerate-summary",
            },
            set(context.skills),
        )
        for registration in context.skills.values():
            self.assertTrue(registration["path"].is_file())
            self.assertTrue(registration["description"])

    def test_registers_a_side_effect_free_authoring_route_tool(self) -> None:
        plugin = load_plugin()
        context = FakeContext()

        plugin.register(context)

        registration = context.tools["hermes_langchain_route"]
        self.assertEqual(registration["group"], "hermes-langchain")
        result = json.loads(
            registration["handler"](
                {"phase": "triage", "triage_stage": "analysis"}
            )
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["next_action"], "kwc_advance_triage")
        self.assertEqual(result["errors"], [])

    def test_registered_tool_schemas_do_not_share_mutable_properties(self) -> None:
        plugin = load_plugin()
        context = FakeContext()

        plugin.register(context)

        pm_properties = context.tools["kwc_pm_response"]["schema"]["parameters"]["properties"]
        profile_properties = context.tools["kwc_profile_response"]["schema"]["parameters"][
            "properties"
        ]
        self.assertIsNot(pm_properties, profile_properties)


if __name__ == "__main__":
    unittest.main()
