"""Coder/Reviewer Profile semantic-port contract tests."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from tests.support import load_module


DELIVERY = load_module("controller.kanban_agent_delivery")
PLUGIN_ROOT = Path(__file__).resolve().parents[2]


class DeliveryProfileParityTests(unittest.TestCase):
    def test_every_profile_asset_has_an_explicit_plugin_target(self) -> None:
        expected = {"coder": ("0.16.2", 8), "reviewer": ("0.9.2", 16)}
        for role, (version, count) in expected.items():
            with self.subTest(role=role):
                path = (
                    PLUGIN_ROOT
                    / "skills"
                    / f"kwc-{'implementation' if role == 'coder' else 'review'}-delivery-runtime"
                    / "references"
                    / f"{role}-source-asset-map.json"
                )
                payload = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(role, payload["source_profile"])
                self.assertEqual(version, payload["baseline_distribution_version"])
                self.assertEqual(count, payload["source_distribution_owned_count"])
                self.assertEqual(count, len(payload["source_distribution_owned"]))
                self.assertEqual(
                    set(payload["source_distribution_owned"]),
                    set(payload["mappings"]),
                )
                for source, mapping in payload["mappings"].items():
                    with self.subTest(role=role, source=source):
                        self.assertIn(mapping["mode"], {"ported", "adapted", "controller-owned"})
                        targets = mapping["targets"]
                        self.assertTrue(targets)
                        self.assertTrue(all((PLUGIN_ROOT / target).is_file() for target in targets))

    def test_every_executable_responsibility_is_owned_and_present(self) -> None:
        for role, skill_name in (
            ("coder", "kwc-implementation-delivery-runtime"),
            ("reviewer", "kwc-review-delivery-runtime"),
        ):
            with self.subTest(role=role):
                skill_root = PLUGIN_ROOT / "skills" / skill_name
                coverage = json.loads(
                    (skill_root / "references" / f"{role}-responsibility-coverage.json").read_text(
                        encoding="utf-8"
                    )
                )
                self.assertEqual(role, coverage["source_profile"])
                self.assertFalse(coverage["runtime_profile_dependency"])
                self.assertEqual(
                    {"memory", "session_history", "profile_soul_loading", "profile_runtime_paths"},
                    set(coverage["intentionally_excluded_profile_state"]),
                )
                for responsibility in coverage["responsibilities"]:
                    with self.subTest(role=role, responsibility=responsibility["id"]):
                        self.assertIn(
                            responsibility["status"],
                            {"ported", "adapted", "controller-owned"},
                        )
                        owners = responsibility["plugin_owners"]
                        self.assertTrue(owners)
                        owner_text = "\n".join(
                            (PLUGIN_ROOT / owner).read_text(encoding="utf-8") for owner in owners
                        )
                        for marker in responsibility["runtime_markers"]:
                            self.assertIn(marker, owner_text)

    def test_canonical_role_contracts_preserve_profile_semantics(self) -> None:
        implementation = DELIVERY._role_execution_contract("implementation")
        review = DELIVERY._role_execution_contract("review")

        self.assertEqual(
            "CONTROLLER_IMPLEMENTATION_DELIVERY_V2",
            implementation["goal"].splitlines()[0],
        )
        self.assertEqual(
            "CONTROLLER_REVIEW_DELIVERY_V2",
            review["goal"].splitlines()[0],
        )
        self.assertEqual(
            ("file", "gradle-mcp", "codebase-memory-mcp", "semble-mcp"),
            implementation["toolsets"],
        )
        self.assertEqual(implementation["toolsets"], review["toolsets"])
        self.assertEqual(
            ("kwc-implementation-delivery-runtime",),
            implementation["skills"],
        )
        self.assertEqual(("kwc-review-delivery-runtime",), review["skills"])

        implementation_acceptance = "\n".join(implementation["acceptance_criteria"])
        for marker in (
            "CARD_EXPECTED_VALUE_CONTRADICTION",
            "CARD_SCENARIO_SELECTOR_CONTRADICTION",
            "external producer public contract",
            "focused, full backend, and quality gates",
        ):
            self.assertIn(marker, implementation_acceptance)

        review_acceptance = "\n".join(review["acceptance_criteria"])
        for marker in (
            "baseline-to-reviewed-head diff",
            "five review axes plus security",
            "prior finding",
            "Gradle, Modulith, and database",
        ):
            self.assertIn(marker, review_acceptance)

    def test_runtime_skills_are_compact_self_contained_behavior_boundaries(self) -> None:
        expected = {
            "kwc-implementation-delivery-runtime": (
                "CARD_EXPECTED_VALUE_CONTRADICTION",
                "CARD_SCENARIO_SELECTOR_CONTRADICTION",
                "assertion coverage checklist",
                "producer public contract",
                "placeholder adapter",
                "residual_risks",
                "checkstyleMain",
            ),
            "kwc-review-delivery-runtime": (
                "baseline_sha..reviewed_head_sha",
                "direct source",
                "maintainability",
                "persistence",
                "architecture",
                "evolution-compatibility",
                "security",
                "prior_findings",
                "resolved",
                "continues",
                "Modulith",
                "database",
            ),
        }
        for skill_name, markers in expected.items():
            with self.subTest(skill=skill_name):
                content = (
                    PLUGIN_ROOT / "skills" / skill_name / "SKILL.md"
                ).read_text(encoding="utf-8")
                self.assertLess(len(content), 12_000)
                self.assertNotIn(".hermes/profiles/coder", content)
                self.assertNotIn(".hermes/profiles/reviewer", content)
                for marker in markers:
                    self.assertIn(marker, content)


if __name__ == "__main__":
    unittest.main()
