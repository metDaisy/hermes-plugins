from __future__ import annotations

import unittest
from pathlib import Path


_ROOT = Path(__file__).resolve().parents[1]
from tests.support import load_module

DOMAIN = load_module("authoring.triage_domain")


class TriageDomainTests(unittest.TestCase):
    def identity(self):
        return DOMAIN.TriageIdentity(
            issue_number=151,
            title="Cart refactoring",
            issue_url="https://example.test/issues/151",
            planning_sha="a" * 40,
            current_state_sha="b" * 40,
            requirement_locators=("docs/requirement/cart.md#Cart",),
            current_state_locators=("docs/current-state.md#Cart",),
        )

    def test_composition_root_builds_immutable_object_api(self) -> None:
        module = DOMAIN.build_triage_module()
        body = module.contract.template(self.identity())

        self.assertEqual("triage-v7", body["schema"])
        self.assertEqual("intake", body["phase"])
        self.assertIn("MISSING_INTENT_GOAL", module.validator.validate(body))
        with self.assertRaises(Exception):
            self.identity().issue_number = 152

    def test_object_composer_preserves_function_contract(self) -> None:
        module = DOMAIN.build_triage_module()
        authoring = {
            "intent": {
                "goal": "Preserve behavior while moving ownership.",
                "change_requirements": [{
                    "id": "CHANGE-001",
                    "outcome": "The application delegates to the aggregate.",
                    "source": {"locator": "docs/requirement/cart.md#Cart", "claim": "Aggregate owns changes."},
                }],
                "preserved_observables": [],
                "quality_gates": [],
                "finalization_requirements": [],
                "out_of_scope": ["Public API changes"],
            },
            "investigation_targets": [],
        }

        via_object = module.compose_intake.execute(self.identity(), authoring)
        via_function = DOMAIN.compose_intake(module.contract.template(self.identity()), authoring)

        self.assertEqual(via_function, via_object)


if __name__ == "__main__":
    unittest.main()
