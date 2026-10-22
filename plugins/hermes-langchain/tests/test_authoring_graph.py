from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PLUGIN_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))


class AuthoringGraphTests(unittest.TestCase):
    def test_triage_intake_routes_to_the_intake_action(self) -> None:
        from hermes_langchain.authoring.route_graph import build_authoring_graph

        graph = build_authoring_graph()

        result = graph.invoke({"phase": "triage", "triage_stage": "intake"})

        self.assertEqual(result["next_action"], "kwc_advance_triage")
        self.assertEqual(result["errors"], [])

    def test_each_authoring_state_routes_to_exactly_one_action(self) -> None:
        from hermes_langchain.authoring.route_graph import build_authoring_graph

        graph = build_authoring_graph()
        cases = {
            ("triage", "discovery"): "kwc_advance_triage",
            ("triage", "source_confirmation"): "kwc_advance_triage",
            ("triage", "analysis"): "kwc_advance_triage",
            ("triage", "materialize"): "kwc_create_triage",
            ("impl", None): "kwc_create_impl_graph",
            ("review", None): "kwc_create_review",
            ("summary", None): "kwc_create_summary",
            ("complete", None): "complete",
        }

        for (phase, triage_stage), expected in cases.items():
            with self.subTest(phase=phase, triage_stage=triage_stage):
                state = {"phase": phase}
                if triage_stage is not None:
                    state["triage_stage"] = triage_stage

                result = graph.invoke(state)

                self.assertEqual(result["next_action"], expected)
                self.assertEqual(result["errors"], [])

    def test_invalid_authoring_state_fails_closed(self) -> None:
        from hermes_langchain.authoring.route_graph import build_authoring_graph

        graph = build_authoring_graph()

        for state in ({"phase": "triage"}, {"phase": "unknown"}):
            with self.subTest(state=state):
                result = graph.invoke(state)

                self.assertIsNone(result["next_action"])
                self.assertEqual(result["errors"], ["INVALID_AUTHORING_STATE"])


class AuthoringRouterFallbackTests(unittest.TestCase):
    def test_routes_without_langgraph_in_the_host_runtime(self) -> None:
        from hermes_langchain.authoring import langgraph_router

        missing = ModuleNotFoundError("No module named 'langgraph'", name="langgraph")
        with mock.patch.object(langgraph_router, "_graph", side_effect=missing):
            result = langgraph_router.route_authoring(
                phase="triage",
                triage_stage="analysis",
            )

        self.assertEqual(result["next_action"], "kwc_advance_triage")
        self.assertEqual(result["errors"], [])

    def test_fallback_fails_closed_for_invalid_state(self) -> None:
        from hermes_langchain.authoring import langgraph_router

        missing = ModuleNotFoundError("No module named 'langgraph'", name="langgraph")
        with mock.patch.object(langgraph_router, "_graph", side_effect=missing):
            result = langgraph_router.route_authoring(phase="unknown")

        self.assertIsNone(result["next_action"])
        self.assertEqual(result["errors"], ["INVALID_AUTHORING_STATE"])


if __name__ == "__main__":
    unittest.main()
