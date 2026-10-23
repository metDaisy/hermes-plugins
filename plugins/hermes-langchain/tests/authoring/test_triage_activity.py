from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace


_ROOT = Path(__file__).resolve().parents[2]
from tests.support import load_module

ACTIVITY = load_module("authoring.triage_activity")


class FakeKanbanPort:
    def __init__(self) -> None:
        self.created = []
        self.archived = []
        self.mutations = []

    def list_active_triage(self, board: str):
        return []

    def create_triage(self, *, board: str, card: dict):
        self.mutations.append(("create", board, "t_123abc"))
        self.created.append((board, card))
        return "t_123abc"

    def read_task(self, *, board: str, task_id: str):
        card = self.created[0][1]
        return {
            "id": task_id,
            "title": card["title"],
            "status": "triage",
            "assignee": card["assignee"],
            "body": card["body_file"].read_text(encoding="utf-8"),
            "skills": [],
            "workspace_kind": "dir",
            "workspace_path": card["workspace"].as_posix(),
        }

    def archive_task(self, *, board: str, task_id: str) -> None:
        self.mutations.append(("archive", board, task_id))
        self.archived.append((board, task_id))


class FakeDomain:
    _loads_json = staticmethod(json.loads)

    @staticmethod
    def build_triage_module():
        identity_seen = []

        class Contract:
            def template(self, identity):
                identity_seen.append(identity)
                return {
                    "phase": "intake",
                    "issue": {"number": identity.issue_number},
                    "baseline": {
                        "planning_sha": identity.planning_sha,
                        "current_state": {
                            "snapshot_sha": identity.current_state_sha,
                            "freshness": "fresh",
                        },
                    },
                }

        class Composer:
            def __init__(self, phase):
                self.phase = phase

            def execute(self, body_or_identity, authoring):
                if self.phase == "intake":
                    body = Contract().template(body_or_identity)
                else:
                    body = dict(body_or_identity)
                body[self.phase] = authoring
                body["phase"] = self.phase
                return body

        class Validator:
            def validate_repository_locators(self, body, repo):
                return []

            def validate(self, body, status=None):
                return []

            def validate_current_state_inspection(self, body, inspection):
                return []

        def current_state_admission(inspection):
            return "committed-baseline-safe" if inspection.get("freshness") == "insufficient" else "fresh"

        class Freezer:
            def execute(self, body, inspection):
                return {**body, "phase": "frozen", "frozen_digest": "digest"}

        return SimpleNamespace(
            contract=Contract(),
            compose_intake=Composer("intake"),
            compose_analysis=Composer("analysis"),
            validator=Validator(),
            freezer=Freezer(),
            current_state_admission=current_state_admission,
        )

    class TriageIdentity:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    @staticmethod
    def validate_card(*_args, **_kwargs):
        return []


class TriageActivityTests(unittest.TestCase):
    def test_real_domain_fixture_reaches_native_port_without_pm_script(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            authoring = json.loads(
                (_ROOT / "tests/fixtures/valid-triage-authoring.json").read_text(encoding="utf-8")
            )
            issue = authoring["identity"]["issue_number"]
            inspection = json.loads(
                (_ROOT / "tests/fixtures/valid-current-state-inspection.json").read_text(encoding="utf-8")
            )

            def create_locator_files(value):
                if isinstance(value, dict):
                    locator = value.get("locator")
                    if isinstance(locator, str) and not locator.startswith(("http://", "https://")):
                        path = workspace / locator.split("#", 1)[0]
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.touch()
                    for item in value.values():
                        create_locator_files(item)
                elif isinstance(value, list):
                    for item in value:
                        create_locator_files(item)

            create_locator_files(authoring)
            for field in ("requirement_locators", "current_state_locators"):
                for locator in authoring["identity"][field]:
                    path = workspace / locator.split("#", 1)[0]
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.touch()
            authoring_file = workspace / ".temp/triage-authoring.json"
            inspection_file = workspace / ".temp/inspection.json"
            output_file = workspace / ".temp/result.json"
            authoring_file.parent.mkdir(parents=True, exist_ok=True)
            authoring_file.write_text(json.dumps(authoring, ensure_ascii=False), encoding="utf-8")
            inspection_file.write_text(json.dumps(inspection), encoding="utf-8")
            port = FakeKanbanPort()

            report = ACTIVITY.execute_triage_authoring(
                assignment={
                    "schema": "kwc-assignment-v1",
                    "board": f"issue-{issue}",
                    "issue_number": issue,
                    "generation": 1,
                    "workspace_path": str(workspace),
                    "role": "orchestrator",
                },
                authoring_file=authoring_file,
                current_state_inspection_file=inspection_file,
                output_file=output_file,
                kanban_port=port,
            )

            frozen = json.loads((workspace / report["frozen_body_file"]).read_text(encoding="utf-8"))
            self.assertEqual("triage-v7", frozen["schema"])
            self.assertEqual("frozen", frozen["phase"])
            self.assertEqual([], ACTIVITY._load_domain().validate(frozen, "triage"))

    def test_composes_freezes_and_materializes_inside_kwc(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            authoring_file = workspace / ".temp/triage-authoring.json"
            inspection_file = workspace / ".temp/inspection.json"
            output_file = workspace / ".temp/result.json"
            authoring_file.parent.mkdir(parents=True)
            authoring_file.write_text(
                json.dumps({
                    "schema": "kwc-triage-authoring-v1",
                    "identity": {
                        "issue_number": 151,
                        "title": "Cart refactoring",
                        "issue_url": "https://example.test/issues/151",
                        "planning_sha": "a" * 40,
                        "current_state_sha": "b" * 40,
                        "requirement_locators": ["docs/requirement/cart.md#Cart"],
                        "current_state_locators": ["docs/current-state.md#Cart"],
                    },
                    "intake": {"goal": "preserve behavior"},
                    "analysis": {"gaps": []},
                }),
                encoding="utf-8",
            )
            inspection_file.write_text(
                json.dumps(
                    {
                        "freshness": "insufficient",
                        "worktree_clean": False,
                        "dirty_paths": [".hermes/profile.yaml"],
                        "covered_changed_paths": [],
                        "errors": ["WORKTREE_DIRTY"],
                    }
                ),
                encoding="utf-8",
            )
            port = FakeKanbanPort()

            report = ACTIVITY.execute_triage_authoring(
                assignment={
                    "schema": "kwc-assignment-v1",
                    "board": "issue-151",
                    "issue_number": 151,
                    "generation": 2,
                    "workspace_path": str(workspace),
                    "role": "orchestrator",
                },
                authoring_file=authoring_file,
                current_state_inspection_file=inspection_file,
                output_file=output_file,
                domain_module=FakeDomain,
                kanban_port=port,
            )

            self.assertTrue(report["valid"])
            self.assertTrue(report["verified"])
            self.assertEqual("t_123abc", report["task_id"])
            self.assertEqual("G2-Issue151-Triage", report["title"])
            self.assertEqual("G2-Issue151-Triage", port.created[0][1]["title"])
            frozen = json.loads((workspace / report["frozen_body_file"]).read_text(encoding="utf-8"))
            self.assertEqual("frozen", frozen["phase"])
            self.assertEqual(
                "committed-baseline-safe",
                frozen["baseline"]["current_state"]["freshness"],
            )
            self.assertEqual(1, len(port.created))
            self.assertEqual(report, json.loads(output_file.read_text(encoding="utf-8")))

    def test_rejects_assignment_issue_mismatch_before_native_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            authoring_file = workspace / "authoring.json"
            inspection_file = workspace / "inspection.json"
            output_file = workspace / "result.json"
            authoring_file.write_text(json.dumps({
                "schema": "kwc-triage-authoring-v1",
                "identity": {
                    "issue_number": 152,
                    "title": "Wrong issue",
                    "issue_url": "https://example.test/issues/152",
                    "planning_sha": "a" * 40,
                    "current_state_sha": "b" * 40,
                    "requirement_locators": ["docs/requirement/cart.md#Cart"],
                    "current_state_locators": ["docs/current-state.md#Cart"],
                },
                "intake": {},
                "analysis": {},
            }), encoding="utf-8")
            inspection_file.write_text("{}", encoding="utf-8")
            port = FakeKanbanPort()

            with self.assertRaisesRegex(ValueError, "issue_number"):
                ACTIVITY.execute_triage_authoring(
                    assignment={
                        "schema": "kwc-assignment-v1",
                        "board": "issue-151",
                        "issue_number": 151,
                        "generation": 1,
                        "workspace_path": str(workspace),
                        "role": "orchestrator",
                    },
                    authoring_file=authoring_file,
                    current_state_inspection_file=inspection_file,
                    output_file=output_file,
                    domain_module=FakeDomain,
                    kanban_port=port,
                )
            self.assertEqual([], port.created)

    def test_recovers_exact_frozen_triage_without_refreezing_current_head(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            source_file = workspace / ".temp/frozen-triage.json"
            output_file = workspace / ".temp/recovery-result.json"
            source_file.parent.mkdir(parents=True)
            frozen = {
                "schema": "triage-v7",
                "phase": "frozen",
                "frozen_digest": "preserved-digest",
                "issue": {
                    "number": 151,
                    "title": "Cart refactoring",
                    "url": "https://example.test/issues/151",
                },
            }
            source_file.write_text(json.dumps(frozen, ensure_ascii=False), encoding="utf-8")
            port = FakeKanbanPort()

            report = ACTIVITY.recover_frozen_triage(
                assignment={
                    "schema": "kwc-assignment-v1",
                    "board": "issue-151",
                    "issue_number": 151,
                    "generation": 2,
                    "workspace_path": str(workspace),
                    "phase": "impl",
                    "role": "orchestrator",
                    "sources": {"triage_id": "t_oldtriage", "impl_ids": []},
                },
                frozen_body_file=source_file,
                output_file=output_file,
                domain_module=FakeDomain,
                kanban_port=port,
            )

            self.assertEqual([("issue-151", "t_oldtriage")], port.archived)
            self.assertEqual(
                [
                    ("create", "issue-151", "t_123abc"),
                    ("archive", "issue-151", "t_oldtriage"),
                ],
                port.mutations,
            )
            self.assertEqual("preserved-digest", report["frozen_digest"])
            self.assertEqual("t_123abc", report["task_id"])
            recovered = json.loads(port.created[0][1]["body_file"].read_text(encoding="utf-8"))
            self.assertEqual(frozen, recovered)


if __name__ == "__main__":
    unittest.main()
