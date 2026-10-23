from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path


_ROOT = Path(__file__).resolve().parents[2]
from tests.support import load_module

WORKFLOW = load_module("authoring.triage_workflow")


class TriageWorkflowTests(unittest.TestCase):
    def test_published_contract_producer_is_not_a_mutation_target(self) -> None:
        producer_locator = (
            "src/main/java/com/example/catalog/application/CatalogQueryApi.java#findRequired"
        )
        confirmation = {
            "targets": [
                {
                    "target_id": "TARGET-001",
                    "evidence": [
                        {
                            "locator": producer_locator,
                            "claim": "Catalog publishes the required lookup.",
                            "kind": "production",
                        }
                    ],
                    "public_contract": {
                        "consumer": "orders",
                        "producer": "catalog",
                        "status": "published",
                        "approval_evidence": [],
                    },
                }
            ]
        }
        analysis = {
            "target_dispositions": [],
            "contract_changes": [],
            "open_decisions": [],
            "implementation_slices": [
                {
                    "scope": [
                        "modify: src/main/java/com/example/catalog/application/"
                        "CatalogQueryApi.java#findRequired - published producer를 수정한다."
                    ]
                }
            ],
        }

        with self.assertRaisesRegex(
            ValueError, "PUBLISHED_CONTRACT_PRODUCER_MUTATION:TARGET-001"
        ):
            WORKFLOW._validate_public_contract_analysis(confirmation, analysis)

    def test_planned_production_seam_requires_confirmed_owner_and_named_behavior(self) -> None:
        confirmation = {
            "targets": [
                {
                    "evidence": [
                        {
                            "locator": "src/main/java/example/Cart.java",
                            "claim": "Cart owns aggregate behavior.",
                            "kind": "production",
                        }
                    ]
                }
            ]
        }
        analysis = {
            "confirmed_gaps": [
                {
                    "required_state": "계획된 Cart#mergeItems가 병합 상태 전이를 소유한다."
                }
            ],
            "verification_matrix": [
                {
                    "id": "VERIFY-001",
                    "production_seam": {
                        "locator": "src/main/java/example/Cart.java#mergeItems",
                        "claim": "Cart merge operation.",
                    },
                }
            ],
        }

        WORKFLOW._validate_production_seam_confirmation(confirmation, analysis)

        analysis["verification_matrix"][0]["production_seam"]["locator"] = (
            "src/main/java/example/Cart.java#invented"
        )
        with self.assertRaisesRegex(ValueError, "UNCONFIRMED_PRODUCTION_SEAM"):
            WORKFLOW._validate_production_seam_confirmation(confirmation, analysis)

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name)
        self.authoring = json.loads(
            (_ROOT / "tests/fixtures/valid-triage-authoring.json").read_text(encoding="utf-8")
        )
        self.inspection = json.loads(
            (_ROOT / "tests/fixtures/valid-current-state-inspection.json").read_text(encoding="utf-8")
        )
        self.assignment = {
            "schema": "kwc-assignment-v1",
            "assignment_id": "kwa_test",
            "board": "issue-42",
            "issue_number": self.authoring["identity"]["issue_number"],
            "generation": 1,
            "workspace_path": str(self.workspace),
            "phase": "triage",
            "triage_stage": "intake",
            "stage_artifacts": {},
        }
        self._touch_locators(self.authoring)
        for guide in ("docs/backend-development-guide.md", "docs/backend-test-guide.md"):
            path = self.workspace / guide
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _touch_locators(self, value) -> None:
        if isinstance(value, dict):
            locator = value.get("locator")
            if isinstance(locator, str) and not locator.startswith(("http://", "https://")):
                path = self.workspace / locator.split("#", 1)[0]
                path.parent.mkdir(parents=True, exist_ok=True)
                path.touch()
            for item in value.values():
                self._touch_locators(item)
        elif isinstance(value, list):
            for item in value:
                self._touch_locators(item)
        for field in ("requirement_locators", "current_state_locators"):
            identity = self.authoring.get("identity", {})
            for locator in identity.get(field, []):
                path = self.workspace / locator.split("#", 1)[0]
                path.parent.mkdir(parents=True, exist_ok=True)
                path.touch()

    def _write(self, name: str, payload: dict) -> Path:
        path = self.workspace / ".temp/input" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return path

    def test_intake_is_composed_and_current_state_is_inspected_by_kwc(self) -> None:
        artifact = self._write(
            "intake.json",
            {
                "schema": "kwc-triage-intake-authoring-v1",
                "identity": self.authoring["identity"],
                "intake": self.authoring["intake"],
            },
        )

        result = WORKFLOW.advance_stage(
            assignment=self.assignment,
            artifact_file=artifact,
            inspection_fn=lambda *_args: self.inspection,
        )

        self.assertEqual("intake", result["stage"])
        self.assertTrue(result["artifact"]["path"].endswith("triage-intake.json"))
        intake = json.loads((self.workspace / result["artifact"]["path"]).read_text(encoding="utf-8"))
        self.assertEqual("intake", intake["phase"])
        inspection_path = (
            self.workspace
            / Path(result["artifact"]["path"]).parent
            / "current-state-inspection.json"
        )
        self.assertEqual(self.inspection, json.loads(inspection_path.read_text(encoding="utf-8")))

    def test_intake_records_committed_baseline_safe_instead_of_false_freshness(self) -> None:
        artifact = self._write(
            "intake-committed-baseline.json",
            {
                "schema": "kwc-triage-intake-authoring-v1",
                "identity": self.authoring["identity"],
                "intake": self.authoring["intake"],
            },
        )
        inspection = dict(self.inspection)
        inspection.update(
            {
                "freshness": "insufficient",
                "worktree_clean": False,
                "dirty_paths": ["hermes/profiles/project-manager/SOUL.md"],
                "covered_changed_paths": [],
                "errors": ["WORKTREE_DIRTY"],
            }
        )

        result = WORKFLOW.advance_stage(
            assignment=self.assignment,
            artifact_file=artifact,
            inspection_fn=lambda *_args: inspection,
        )

        intake = json.loads((self.workspace / result["artifact"]["path"]).read_text(encoding="utf-8"))
        self.assertEqual(
            "committed-baseline-safe",
            intake["baseline"]["current_state"]["freshness"],
        )

    def test_assignment_artifact_file_is_the_only_accepted_stage_input(self) -> None:
        artifact = self._write(
            "intake.json",
            {
                "schema": "kwc-triage-intake-authoring-v1",
                "identity": self.authoring["identity"],
                "intake": self.authoring["intake"],
            },
        )
        assignment = {
            **self.assignment,
            "artifact_file": ".temp/kwc-authoring/42/generation-1/intake-authoring.json",
        }

        with self.assertRaisesRegex(ValueError, "artifact_file does not match assignment"):
            WORKFLOW.advance_stage(
                assignment=assignment,
                artifact_file=artifact,
                inspection_fn=lambda *_args: self.inspection,
            )

    def test_discovery_must_cover_exact_intake_targets_and_existing_candidates(self) -> None:
        self._prepare_intake()
        self.assignment["triage_stage"] = "discovery"
        discovery = {
            "schema": "kwc-triage-discovery-v2",
            "targets": [],
            "tool_usage": {"codebase_memory": 1, "semble": 1},
            "fallbacks": [],
            "codebase_memory": self._available_codebase_memory(),
        }
        artifact = self._write("discovery.json", discovery)

        with self.assertRaisesRegex(ValueError, "TARGET_COVERAGE"):
            WORKFLOW.advance_stage(
                assignment=self.assignment,
                artifact_file=artifact,
                git_head_fn=lambda _workspace: "a" * 40,
            )

    def test_resolves_canonical_codebase_memory_project_and_ignores_orphan(self) -> None:
        result = WORKFLOW.resolve_codebase_memory_project(
            workspace=self.workspace,
            projects=[
                {
                    "project": "e-commerce-clone-coding",
                    "root_path": str(self.workspace / "deleted-scratch"),
                    "root_exists": False,
                    "git_head": None,
                },
                {
                    "project": "C-Users-leee-IdeaProjects-e-commerce-clone-coding",
                    "root_path": str(self.workspace),
                    "root_exists": True,
                    "git_head": "a" * 40,
                },
            ],
            git_head_fn=lambda _workspace: "a" * 40,
        )

        self.assertEqual(
            {
                "status": "available",
                "project": "C-Users-leee-IdeaProjects-e-commerce-clone-coding",
                "reason": "workspace-and-head-match",
                "git_head": "a" * 40,
            },
            result,
        )

    def test_returns_typed_unavailable_when_only_orphan_project_exists(self) -> None:
        result = WORKFLOW.resolve_codebase_memory_project(
            workspace=self.workspace,
            projects=[
                {
                    "project": "e-commerce-clone-coding",
                    "root_path": str(self.workspace / "deleted-scratch"),
                    "root_exists": False,
                    "git_head": None,
                }
            ],
            git_head_fn=lambda _workspace: "a" * 40,
        )

        self.assertEqual(
            {
                "status": "unavailable",
                "project": None,
                "reason": "no-workspace-match",
                "git_head": "a" * 40,
            },
            result,
        )

    def test_discovery_rejects_stale_or_fabricated_project_resolution(self) -> None:
        self._prepare_intake()
        self.assignment["triage_stage"] = "discovery"
        discovery = self._discovery_payload()
        discovery["codebase_memory"]["resolution"]["project"] = "e-commerce-clone-coding"
        artifact = self._write("stale-project.json", discovery)

        with self.assertRaisesRegex(ValueError, "CODEBASE_MEMORY_RESOLUTION_MISMATCH"):
            WORKFLOW.advance_stage(
                assignment=self.assignment,
                artifact_file=artifact,
                git_head_fn=lambda _workspace: "a" * 40,
            )

    def test_discovery_canonicalizes_repeated_same_path_candidates(self) -> None:
        self._prepare_intake()
        assignment = {**self.assignment, "triage_stage": "discovery"}
        discovery = self._discovery_payload()
        candidate = next(
            item
            for item in discovery["targets"][0]["candidates"]
            if item["kind"] == "production"
        )
        discovery["targets"][0]["candidates"] = [
            {**candidate, "symbol": f"symbol-{index}"}
            for index in range(3)
        ] + [
            {
                "path": "docs/backend-development-guide.md",
                "symbol": "Backend development guide",
                "kind": "document",
                "reason": "backend implementation convention",
            },
            {
                "path": "docs/backend-test-guide.md",
                "symbol": "Backend test guide",
                "kind": "document",
                "reason": "backend verification convention",
            },
        ]
        artifact = self._write("same-path-candidates.json", discovery)

        result = WORKFLOW.advance_stage(
            assignment=assignment,
            artifact_file=artifact,
            git_head_fn=lambda _workspace: "a" * 40,
        )

        canonical = json.loads(
            (self.workspace / result["artifact"]["path"]).read_text(encoding="utf-8")
        )
        self.assertEqual(4, len(canonical["targets"][0]["candidates"]))
        self.assertEqual(
            ["symbol-0", "symbol-1"],
            [
                item["symbol"]
                for item in canonical["targets"][0]["candidates"]
                if item["path"] == candidate["path"]
            ],
        )

    def test_discovery_canonicalizes_safe_tool_projection_noise(self) -> None:
        self._prepare_intake()
        assignment = {**self.assignment, "triage_stage": "discovery"}
        discovery = self._discovery_payload()
        project = discovery["codebase_memory"]["projects"][0]
        discovery["codebase_memory"]["projects"] = [{
            "name": project["project"],
            "root_path": project["root_path"],
            "branch": "p3/issue151",
            "nodes": 5207,
            "edges": 26833,
            "size_bytes": 32178176,
        }]
        discovery["fallbacks"] = {
            "producer_completion": {
                "used": True,
                "reason": "producer public source completion",
                "target_ids": ["TARGET-001"],
            },
            "unused": {"used": False, "reason": None, "target_ids": []},
        }
        artifact = self._write("discovery-projection-noise.json", discovery)

        result = WORKFLOW.advance_stage(
            assignment=assignment,
            artifact_file=artifact,
            git_head_fn=lambda _workspace: "a" * 40,
        )

        canonical = json.loads(
            (self.workspace / result["artifact"]["path"]).read_text(encoding="utf-8")
        )
        self.assertEqual(
            [{
                "kind": "producer_completion",
                "reason": "producer public source completion",
                "target_ids": ["TARGET-001"],
            }],
            canonical["fallbacks"],
        )
        self.assertEqual(
            {
                "project": project["project"],
                "root_path": project["root_path"],
                "root_exists": True,
                "git_head": "a" * 40,
            },
            canonical["codebase_memory"]["projects"][0],
        )

    def test_discovery_requires_external_producer_for_cross_module_target(self) -> None:
        target = self.authoring["intake"]["investigation_targets"][0]
        target["locator"] = (
            "src/main/java/com/example/orders/application/ConsumerPort.java"
            "#ConsumerPort"
        )
        target["question"] = (
            "consumer가 producer 공개 API를 통해 cross-module 호출을 수행하는가?"
        )
        self._touch_locators(target)
        self._prepare_intake()
        assignment = {**self.assignment, "triage_stage": "discovery"}
        discovery = self._discovery_payload()
        discovery["targets"][0]["candidates"] = [
            candidate
            for candidate in discovery["targets"][0]["candidates"]
            if candidate["kind"] == "document"
            or candidate["path"] == target["locator"].split("#", 1)[0]
        ]
        artifact = self._write("missing-producer-candidate.json", discovery)

        with self.assertRaisesRegex(
            ValueError, "DISCOVERY_PRODUCER_CANDIDATE_REQUIRED:TARGET-001"
        ):
            WORKFLOW.advance_stage(
                assignment=assignment,
                artifact_file=artifact,
                git_head_fn=lambda _workspace: "a" * 40,
            )

    def test_discovery_requires_glossary_for_error_ownership_target(self) -> None:
        target = self.authoring["intake"]["investigation_targets"][0]
        target["question"] = "duplicated code/message의 error ownership이 올바른가?"
        glossary = self.workspace / "docs/domain-glossary.md"
        glossary.parent.mkdir(parents=True, exist_ok=True)
        glossary.write_text("# Domain glossary\n", encoding="utf-8")
        self._prepare_intake()
        assignment = {**self.assignment, "triage_stage": "discovery"}
        discovery = self._discovery_payload()
        discovery["targets"][0]["candidates"] = discovery["targets"][0]["candidates"][:1]
        artifact = self._write("missing-glossary-candidate.json", discovery)

        with self.assertRaisesRegex(
            ValueError,
            "DISCOVERY_GOVERNING_DOCUMENT_REQUIRED:docs/domain-glossary.md",
        ):
            WORKFLOW.advance_stage(
                assignment=assignment,
                artifact_file=artifact,
                git_head_fn=lambda _workspace: "a" * 40,
            )

    def test_intake_requires_persistence_requirement_to_have_persistence_target(self) -> None:
        intake = json.loads(json.dumps(self.authoring["intake"]))
        intake["intent"]["change_requirements"][0]["outcome"] = (
            "Cart 병합의 repository transaction과 비관적 잠금 경계를 검증 가능하게 유지한다."
        )
        artifact = self._write(
            "intake-missing-persistence-target.json",
            {
                "schema": "kwc-triage-intake-authoring-v1",
                "identity": self.authoring["identity"],
                "intake": intake,
            },
        )

        with self.assertRaisesRegex(
            ValueError,
            "UNCOVERED_CHANGE_REQUIREMENT_TARGET:CHANGE-001:persistence",
        ):
            WORKFLOW.advance_stage(
                assignment=self.assignment,
                artifact_file=artifact,
                inspection_fn=lambda *_args: self.inspection,
            )

    def test_discovery_requires_backend_guides_for_backend_target(self) -> None:
        for document in (
            "docs/backend-development-guide.md",
            "docs/backend-test-guide.md",
        ):
            path = self.workspace / document
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("# Guide\n", encoding="utf-8")
        self._prepare_intake()
        assignment = {**self.assignment, "triage_stage": "discovery"}
        discovery = self._discovery_payload()
        discovery["targets"][0]["candidates"] = [
            candidate
            for candidate in discovery["targets"][0]["candidates"]
            if candidate["path"]
            not in {"docs/backend-development-guide.md", "docs/backend-test-guide.md"}
        ]
        artifact = self._write("missing-backend-guides.json", discovery)

        with self.assertRaisesRegex(
            ValueError,
            "DISCOVERY_GOVERNING_DOCUMENT_REQUIRED:docs/backend-development-guide.md",
        ):
            WORKFLOW.advance_stage(
                assignment=assignment,
                artifact_file=artifact,
                git_head_fn=lambda _workspace: "a" * 40,
            )
        discovery["targets"][0]["candidates"].append(
            {
                "path": "docs/backend-development-guide.md",
                "symbol": "Guide",
                "kind": "document",
                "reason": "backend implementation convention",
            }
        )
        artifact = self._write("missing-backend-test-guide.json", discovery)
        with self.assertRaisesRegex(
            ValueError,
            "DISCOVERY_GOVERNING_DOCUMENT_REQUIRED:docs/backend-test-guide.md",
        ):
            WORKFLOW.advance_stage(
                assignment=assignment,
                artifact_file=artifact,
                git_head_fn=lambda _workspace: "a" * 40,
            )

    def test_discovery_requires_repository_and_test_for_persistence_target(self) -> None:
        target = self.authoring["intake"]["investigation_targets"][0]
        target["question"] = (
            "repository transaction과 pessimistic write lock이 병합 경로를 보호하는가?"
        )
        self._prepare_intake()
        assignment = {**self.assignment, "triage_stage": "discovery"}
        discovery = self._discovery_payload()
        artifact = self._write("missing-persistence-closure.json", discovery)

        with self.assertRaisesRegex(
            ValueError,
            "DISCOVERY_PERSISTENCE_REPOSITORY_REQUIRED:TARGET-001",
        ):
            WORKFLOW.advance_stage(
                assignment=assignment,
                artifact_file=artifact,
                git_head_fn=lambda _workspace: "a" * 40,
            )

    def test_discovery_rejects_more_than_eight_distinct_candidates(self) -> None:
        self._prepare_intake()
        assignment = {**self.assignment, "triage_stage": "discovery"}
        discovery = self._discovery_payload()
        candidates = discovery["targets"][0]["candidates"]
        while len(candidates) < 9:
            index = len(candidates)
            path = self.workspace / f"src/main/java/example/Candidate{index}.java"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch()
            candidates.append({
                "path": path.relative_to(self.workspace).as_posix(),
                "symbol": f"Candidate{index}",
                "kind": "production",
                "reason": "bounded candidate",
            })
        artifact = self._write("too-many-candidates.json", discovery)

        with self.assertRaisesRegex(ValueError, "DISCOVERY_CANDIDATE_LIMIT:TARGET-001"):
            WORKFLOW.advance_stage(
                assignment=assignment,
                artifact_file=artifact,
                git_head_fn=lambda _workspace: "a" * 40,
            )

    def test_source_confirmation_evidence_is_restricted_to_discovery_candidates(self) -> None:
        self._prepare_intake()
        self._prepare_discovery()
        self.assignment["triage_stage"] = "source_confirmation"
        undiscovered = self.workspace / "src/main/java/other/NotDiscovered.java"
        undiscovered.parent.mkdir(parents=True, exist_ok=True)
        undiscovered.touch()
        confirmation = {
            "schema": "kwc-triage-source-confirmation-v2",
            "targets": [{
                "target_id": "TARGET-001",
                "disposition": "partial",
                "rationale": "direct inspection",
                "public_contract": None,
                "evidence": [{
                    "locator": "src/main/java/other/NotDiscovered.java#method",
                    "claim": "not a discovered candidate",
                    "kind": "production",
                }],
            }],
            "open_questions": [],
        }
        artifact = self._write("confirmation.json", confirmation)

        with self.assertRaisesRegex(
            ValueError,
            r"UNDISCOVERED_EVIDENCE:TARGET-001:src/main/java/other/NotDiscovered.java#method",
        ):
            WORKFLOW.advance_stage(assignment=self.assignment, artifact_file=artifact)

    def test_source_confirmation_requires_exact_service_method_for_http_dto_boundary(self) -> None:
        target = self.authoring["intake"]["investigation_targets"][0]
        target["question"] = (
            "controller가 import하는 request DTO와 response DTO 및 이를 교차하는 "
            "application service method signature를 확인한다."
        )
        self._prepare_intake()
        discovery = self._discovery_payload()
        service_path, service_symbol = target["locator"].split("#", 1)
        discovery["targets"][0]["candidates"] = [
            {
                "path": service_path,
                "symbol": service_symbol,
                "kind": "production",
                "reason": "application service method signature",
            },
            {
                "path": "docs/backend-development-guide.md",
                "symbol": "Backend development guide",
                "kind": "document",
                "reason": "backend implementation convention",
            },
            {
                "path": "docs/backend-test-guide.md",
                "symbol": "Backend test guide",
                "kind": "document",
                "reason": "backend verification convention",
            },
        ]
        for path, symbol in (
            ("src/main/java/example/InboundPayload.java", "InboundPayload"),
            ("src/main/java/example/OutboundPayload.java", "OutboundPayload"),
        ):
            source = self.workspace / path
            source.parent.mkdir(parents=True, exist_ok=True)
            source.touch()
            discovery["targets"][0]["candidates"].append({
                "path": path,
                "symbol": symbol,
                "kind": "production",
                "reason": "HTTP DTO boundary evidence",
            })
        discovery_artifact = self._write("http-dto-discovery.json", discovery)
        discovery_result = WORKFLOW.advance_stage(
            assignment={**self.assignment, "triage_stage": "discovery"},
            artifact_file=discovery_artifact,
            git_head_fn=lambda _workspace: "a" * 40,
        )
        self.assignment["stage_artifacts"]["discovery"] = discovery_result["artifact"]
        confirmation = {
            "schema": "kwc-triage-source-confirmation-v2",
            "targets": [{
                "target_id": target["id"],
                "disposition": "partial",
                "rationale": "HTTP DTO boundary is not separated.",
                "public_contract": None,
                "evidence": [
                    {
                        "locator": "src/main/java/example/InboundPayload.java#InboundPayload",
                        "claim": "The request DTO crosses the HTTP boundary.",
                        "kind": "production",
                    },
                    {
                        "locator": "src/main/java/example/OutboundPayload.java#OutboundPayload",
                        "claim": "The response DTO crosses the HTTP boundary.",
                        "kind": "production",
                    },
                    {
                        "locator": service_path,
                        "claim": "The service receives the HTTP DTO directly.",
                        "kind": "production",
                    },
                ],
            }],
            "open_questions": [],
        }
        artifact = self._write("http-dto-confirmation.json", confirmation)

        with self.assertRaisesRegex(
            ValueError,
            "SOURCE_HTTP_DTO_OPERATION_SIGNATURE_EVIDENCE_REQUIRED:TARGET-001",
        ):
            WORKFLOW.advance_stage(
                assignment={**self.assignment, "triage_stage": "source_confirmation"},
                artifact_file=artifact,
            )

    def test_source_confirmation_reuses_candidate_discovered_for_another_target(self) -> None:
        second_target = {
            "id": "TARGET-002",
            "locator": "src/main/java/example/ProducerApi.java#ProducerApi",
            "question": "Confirm the shared producer public seam.",
            "source": self.authoring["intake"]["intent"]["change_requirements"][0]["source"],
        }
        self.authoring["intake"]["investigation_targets"].append(second_target)
        self._touch_locators(second_target)
        self._prepare_intake()
        first_target = self.authoring["intake"]["investigation_targets"][0]
        discovery = {
            "schema": "kwc-triage-discovery-v2",
            "targets": [
                {
                    "target_id": first_target["id"],
                    "candidates": [
                        {
                            "path": first_target["locator"].split("#", 1)[0],
                            "symbol": first_target["locator"].split("#", 1)[1],
                            "kind": "production",
                            "reason": "exact consumer target",
                        },
                        {
                            "path": "docs/backend-development-guide.md",
                            "symbol": "Backend development guide",
                            "kind": "document",
                            "reason": "backend implementation convention",
                        },
                        {
                            "path": "docs/backend-test-guide.md",
                            "symbol": "Backend test guide",
                            "kind": "document",
                            "reason": "backend verification convention",
                        },
                    ],
                },
                {
                    "target_id": second_target["id"],
                    "candidates": [{
                        "path": second_target["locator"].split("#", 1)[0],
                        "symbol": second_target["locator"].split("#", 1)[1],
                        "kind": "production",
                        "reason": "shared producer public source",
                    }],
                },
            ],
            "tool_usage": {"codebase_memory": 1, "semble": 0},
            "fallbacks": [],
            "codebase_memory": self._available_codebase_memory(),
        }
        discovery_artifact = self._write("cross-target-discovery.json", discovery)
        discovery_result = WORKFLOW.advance_stage(
            assignment={**self.assignment, "triage_stage": "discovery"},
            artifact_file=discovery_artifact,
            git_head_fn=lambda _workspace: "a" * 40,
        )
        self.assignment["stage_artifacts"]["discovery"] = discovery_result["artifact"]
        shared_evidence = {
            "locator": second_target["locator"],
            "claim": "The shared producer public source was accepted during discovery.",
            "kind": "production",
        }
        confirmation = {
            "schema": "kwc-triage-source-confirmation-v2",
            "targets": [
                {
                    "target_id": first_target["id"],
                    "disposition": "implemented",
                    "rationale": "consumer target reuses the discovered producer seam",
                    "public_contract": None,
                    "evidence": [shared_evidence],
                },
                {
                    "target_id": second_target["id"],
                    "disposition": "implemented",
                    "rationale": "producer target confirms its public source",
                    "public_contract": None,
                    "evidence": [shared_evidence],
                },
            ],
            "open_questions": [],
        }
        artifact = self._write("cross-target-confirmation.json", confirmation)

        accepted = WORKFLOW.advance_stage(
            assignment={**self.assignment, "triage_stage": "source_confirmation"},
            artifact_file=artifact,
        )

        self.assertEqual("source_confirmation", accepted["stage"])

    def test_source_confirmation_rejects_structured_open_question(self) -> None:
        self._prepare_intake()
        self._prepare_discovery()
        target = self.authoring["intake"]["investigation_targets"][0]
        discovery = json.loads(
            (self.workspace / self.assignment["stage_artifacts"]["discovery"]["path"]).read_text(
                encoding="utf-8"
            )
        )
        candidate = discovery["targets"][0]["candidates"][0]
        confirmation = {
            "schema": "kwc-triage-source-confirmation-v2",
            "targets": [{
                "target_id": target["id"],
                "disposition": "partial",
                "rationale": "bounded source confirmation",
                "public_contract": None,
                "evidence": [{
                    "locator": f'{candidate["path"]}#{candidate["symbol"]}',
                    "claim": "confirmed candidate",
                    "kind": candidate["kind"],
                }],
            }],
            "open_questions": [{"question": "implementation choice"}],
        }
        artifact = self._write("structured-open-question.json", confirmation)

        with self.assertRaisesRegex(ValueError, "OPEN_QUESTIONS_INVALID"):
            WORKFLOW.advance_stage(
                assignment={**self.assignment, "triage_stage": "source_confirmation"},
                artifact_file=artifact,
            )

    def test_analysis_assembles_final_authoring_for_materialization(self) -> None:
        self._prepare_intake()
        self._prepare_discovery()
        self._prepare_confirmation()
        self.assignment["triage_stage"] = "analysis"
        artifact = self._write(
            "analysis.json",
            {
                "schema": "kwc-triage-analysis-authoring-v1",
                "analysis": self.authoring["analysis"],
            },
        )

        result = WORKFLOW.advance_stage(assignment=self.assignment, artifact_file=artifact)
        self.assignment["stage_artifacts"]["analysis"] = result["artifact"]
        self.assignment["triage_stage"] = "materialize"

        assembled_relative = Path(result["artifact"]["path"]).parent / "triage-authoring.json"
        assembled = self.workspace / assembled_relative
        payload = json.loads(assembled.read_text(encoding="utf-8"))
        self.assertEqual("kwc-triage-authoring-v1", payload["schema"])
        self.assertEqual(self.authoring["identity"], payload["identity"])
        self.assertEqual(self.authoring["analysis"], payload["analysis"])
        self.assertEqual("analysis", result["stage"])
        materialize = WORKFLOW.materialization_arguments(self.assignment)
        self.assertEqual(
            assembled_relative.as_posix(),
            materialize["authoring_file"],
        )
        self.assertEqual(
            (assembled_relative.parent / "current-state-inspection.json").as_posix(),
            materialize["current_state_inspection_file"],
        )

    def test_analysis_rejects_implementation_evidence_not_confirmed_by_source_stage(self) -> None:
        self._prepare_intake()
        self._prepare_discovery()
        self._prepare_confirmation()
        self.assignment["triage_stage"] = "analysis"
        payload = json.loads(json.dumps(self.authoring["analysis"]))
        undiscovered = self.workspace / "src/main/java/other/Unconfirmed.java"
        undiscovered.parent.mkdir(parents=True, exist_ok=True)
        undiscovered.touch()
        payload["confirmed_gaps"][0]["evidence"][0]["locator"] = (
            "src/main/java/other/Unconfirmed.java#method"
        )
        artifact = self._write(
            "analysis-unconfirmed.json",
            {"schema": "kwc-triage-analysis-authoring-v1", "analysis": payload},
        )

        with self.assertRaisesRegex(ValueError, "UNCONFIRMED_ANALYSIS_EVIDENCE"):
            WORKFLOW.advance_stage(assignment=self.assignment, artifact_file=artifact)

    def test_analysis_requires_named_exception_in_acceptance(self) -> None:
        self._prepare_intake()
        self._prepare_discovery()
        self._prepare_confirmation()
        self.assignment["triage_stage"] = "analysis"
        analysis = json.loads(json.dumps(self.authoring["analysis"]))
        analysis["confirmed_gaps"][0]["required_state"] += (
            " 잘못된 수량은 OrderInputException으로 거부한다."
        )
        artifact = self._write(
            "analysis-missing-exception-acceptance.json",
            {"schema": "kwc-triage-analysis-authoring-v1", "analysis": analysis},
        )

        with self.assertRaisesRegex(
            ValueError, "ACCEPTANCE_REQUIRED_SYMBOL_MISSING:SLICE-001:OrderInputException"
        ):
            WORKFLOW.advance_stage(assignment=self.assignment, artifact_file=artifact)

    def test_analysis_requires_every_reviewed_document_impact(self) -> None:
        self._prepare_intake()
        self._prepare_discovery()
        self._prepare_confirmation()
        self.assignment["triage_stage"] = "analysis"
        payload = json.loads(json.dumps(self.authoring["analysis"]))
        payload["document_impacts"] = [
            item
            for item in payload["document_impacts"]
            if item["document"] != "docs/project-state.md"
        ]
        artifact = self._write(
            "analysis-missing-document-impact.json",
            {"schema": "kwc-triage-analysis-authoring-v1", "analysis": payload},
        )

        with self.assertRaisesRegex(
            ValueError, "DOCUMENT_IMPACT_COVERAGE:docs/project-state.md"
        ):
            WORKFLOW.advance_stage(assignment=self.assignment, artifact_file=artifact)

    def test_analysis_document_impact_evidence_must_match_document(self) -> None:
        self._prepare_intake()
        self._prepare_discovery()
        self._prepare_confirmation()
        self.assignment["triage_stage"] = "analysis"
        payload = json.loads(json.dumps(self.authoring["analysis"]))
        payload["document_impacts"][0]["evidence"] = [
            {
                "locator": "docs/project-state.md#Orders",
                "claim": "Mismatched document evidence.",
            }
        ]
        artifact = self._write(
            "analysis-mismatched-document-evidence.json",
            {"schema": "kwc-triage-analysis-authoring-v1", "analysis": payload},
        )

        with self.assertRaisesRegex(
            ValueError,
            "DOCUMENT_IMPACT_EVIDENCE_MISMATCH:docs/requirements/orders.md",
        ):
            WORKFLOW.advance_stage(assignment=self.assignment, artifact_file=artifact)

    def test_analysis_canonicalizes_safe_projection_noise(self) -> None:
        self._prepare_intake()
        self._prepare_discovery()
        self._prepare_confirmation()
        self.assignment["triage_stage"] = "analysis"
        payload = json.loads(json.dumps(self.authoring["analysis"]))
        payload["confirmed_gaps"][0]["evidence"][0]["kind"] = "production"
        requirement = self.authoring["intake"]["intent"]["change_requirements"][0]
        payload["intake_amendments"]["intent_entries"] = [{
            "section": "intent.change_requirements",
            "id": requirement["id"],
            "changes": {"source": requirement["source"]},
        }]
        duplicate = json.loads(json.dumps(payload["verification_matrix"][0]))
        duplicate["id"] = "VERIFY-DUPLICATE"
        duplicate["required_verification"] = ["추가 구조 검증을 실행한다."]
        payload["verification_matrix"].append(duplicate)
        payload["implementation_slices"][0]["acceptance"][0]["verification_ids"].append(
            duplicate["id"]
        )
        artifact = self._write(
            "analysis-projection-noise.json",
            {"schema": "kwc-triage-analysis-authoring-v1", "analysis": payload},
        )

        result = WORKFLOW.advance_stage(assignment=self.assignment, artifact_file=artifact)

        assembled = json.loads(
            (self.workspace / result["artifact"]["path"]).read_text(encoding="utf-8")
        )
        canonical = assembled["analysis"]
        self.assertNotIn("kind", canonical["confirmed_gaps"][0]["evidence"][0])
        self.assertEqual(
            "change_requirements",
            canonical["intake_amendments"]["intent_entries"][0]["section"],
        )
        self.assertEqual(
            {"source": requirement["source"]},
            canonical["intake_amendments"]["intent_entries"][0]["changes"],
        )
        self.assertEqual(1, len(canonical["verification_matrix"]))
        self.assertIn(
            "추가 구조 검증을 실행한다.",
            canonical["verification_matrix"][0]["required_verification"],
        )

    def test_analysis_requires_contract_change_for_approved_unimplemented_public_contract(self) -> None:
        self._prepare_intake()
        self._prepare_discovery()
        target = self.authoring["intake"]["investigation_targets"][0]
        discovery = json.loads(
            (self.workspace / self.assignment["stage_artifacts"]["discovery"]["path"]).read_text(
                encoding="utf-8"
            )
        )
        confirmation = {
            "schema": "kwc-triage-source-confirmation-v2",
            "targets": [{
                "target_id": target["id"],
                "disposition": "absent",
                "rationale": "approved public contract has no production seam",
                "public_contract": {
                    "consumer": "cart",
                    "producer": "offer",
                    "status": "approved-unimplemented",
                    "approval_evidence": [
                        self.authoring["intake"]["intent"]["change_requirements"][0]["source"]
                    ],
                },
                "evidence": [
                    {
                        "locator": f'{candidate["path"]}#{candidate["symbol"]}',
                        "claim": "confirmed absent producer seam",
                        "kind": candidate["kind"],
                    }
                    for candidate in discovery["targets"][0]["candidates"]
                ],
            }],
            "open_questions": [],
        }
        artifact = self._write("confirmation-approved-unimplemented.json", confirmation)
        confirmation_assignment = {**self.assignment, "triage_stage": "source_confirmation"}
        result = WORKFLOW.advance_stage(assignment=confirmation_assignment, artifact_file=artifact)
        self.assignment["stage_artifacts"]["source_confirmation"] = result["artifact"]
        self.assignment["triage_stage"] = "analysis"
        analysis_artifact = self._write(
            "analysis-missing-contract-change.json",
            {"schema": "kwc-triage-analysis-authoring-v1", "analysis": self.authoring["analysis"]},
        )

        with self.assertRaisesRegex(ValueError, "MISSING_APPROVED_CONTRACT_CHANGE:TARGET-001"):
            WORKFLOW.advance_stage(assignment=self.assignment, artifact_file=analysis_artifact)

        analysis = json.loads(json.dumps(self.authoring["analysis"]))
        analysis["contract_changes"] = [{
            "id": "CONTRACT-001",
            "consumer": "cart",
            "producer": "offer",
            "change": "Add the approved producer public bridge before consumer integration.",
            "evidence": [{
                "locator": confirmation["targets"][0]["evidence"][0]["locator"],
                "claim": "The approved producer seam is absent and must be implemented.",
            }, confirmation["targets"][0]["public_contract"]["approval_evidence"][0]],
            "verification_ids": ["VERIFY-001"],
        }]
        analysis["implementation_slices"][0]["contract_change_ids"] = ["CONTRACT-001"]
        valid_artifact = self._write(
            "analysis-with-contract-change.json",
            {"schema": "kwc-triage-analysis-authoring-v1", "analysis": analysis},
        )

        accepted = WORKFLOW.advance_stage(assignment=self.assignment, artifact_file=valid_artifact)
        self.assertEqual("analysis", accepted["stage"])

    def test_source_confirmation_allows_published_contract_for_partial_consumer_target(self) -> None:
        self._prepare_intake()
        self._prepare_discovery()
        target = self.authoring["intake"]["investigation_targets"][0]
        discovery_path = (
            self.workspace / self.assignment["stage_artifacts"]["discovery"]["path"]
        )
        discovery = json.loads(discovery_path.read_text(encoding="utf-8"))
        producer_path = (
            "src/main/java/com/example/catalog/application/CatalogQueryApi.java"
        )
        path = self.workspace / producer_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
        discovery["targets"][0]["candidates"].append(
            {
                "path": producer_path,
                "symbol": "findRequired",
                "kind": "production",
                "reason": "published producer contract",
            }
        )
        discovery_path.write_text(
            json.dumps(discovery, ensure_ascii=False), encoding="utf-8"
        )
        consumer_candidate = next(
            item
            for item in discovery["targets"][0]["candidates"]
            if item["kind"] == "production" and "/orders/" in item["path"]
        )
        confirmation = {
            "schema": "kwc-triage-source-confirmation-v2",
            "targets": [{
                "target_id": target["id"],
                "disposition": "partial",
                "rationale": "The producer contract is published while the consumer integration is incomplete.",
                "public_contract": {
                    "consumer": "orders",
                    "producer": "catalog",
                    "status": "published",
                    "approval_evidence": [
                        self.authoring["intake"]["intent"]["change_requirements"][0]["source"]
                    ],
                },
                "evidence": [
                    {
                        "locator": (
                            f'{consumer_candidate["path"]}#'
                            f'{consumer_candidate["symbol"]}'
                        ),
                        "claim": "The consumer integration remains partial.",
                        "kind": "production",
                    },
                    {
                        "locator": f"{producer_path}#findRequired",
                        "claim": "Catalog publishes the required lookup contract.",
                        "kind": "production",
                    },
                ],
            }],
            "open_questions": [],
        }
        artifact = self._write("confirmation-published-partial.json", confirmation)
        assignment = {**self.assignment, "triage_stage": "source_confirmation"}

        accepted = WORKFLOW.advance_stage(assignment=assignment, artifact_file=artifact)

        self.assertEqual("source_confirmation", accepted["stage"])

    def test_source_confirmation_requires_published_producer_symbol(self) -> None:
        self._prepare_intake()
        self._prepare_discovery()
        target = self.authoring["intake"]["investigation_targets"][0]
        discovery = json.loads(
            (
                self.workspace
                / self.assignment["stage_artifacts"]["discovery"]["path"]
            ).read_text(encoding="utf-8")
        )
        consumer_candidate = next(
            item
            for item in discovery["targets"][0]["candidates"]
            if item["kind"] == "production" and "/orders/" in item["path"]
        )
        confirmation = {
            "schema": "kwc-triage-source-confirmation-v2",
            "targets": [
                {
                    "target_id": target["id"],
                    "disposition": "partial",
                    "rationale": "Consumer evidence alone cannot prove a published producer.",
                    "public_contract": {
                        "consumer": "orders",
                        "producer": "catalog",
                        "status": "published",
                        "approval_evidence": [
                            self.authoring["intake"]["intent"]["change_requirements"][0]["source"]
                        ],
                    },
                    "evidence": [
                        {
                            "locator": (
                                f'{consumer_candidate["path"]}#'
                                f'{consumer_candidate["symbol"]}'
                            ),
                            "claim": "Only the consumer implementation was inspected.",
                            "kind": "production",
                        }
                    ],
                }
            ],
            "open_questions": [],
        }
        artifact = self._write("confirmation-missing-producer.json", confirmation)
        assignment = {**self.assignment, "triage_stage": "source_confirmation"}

        with self.assertRaisesRegex(
            ValueError,
            "PUBLISHED_CONTRACT_PRODUCER_EVIDENCE_REQUIRED:TARGET-001",
        ):
            WORKFLOW.advance_stage(assignment=assignment, artifact_file=artifact)

    def test_materialization_rejects_tampered_stage_artifact(self) -> None:
        self._prepare_intake()
        self._prepare_discovery()
        self._prepare_confirmation()
        self.assignment["triage_stage"] = "analysis"
        artifact = self._write(
            "analysis.json",
            {
                "schema": "kwc-triage-analysis-authoring-v1",
                "analysis": self.authoring["analysis"],
            },
        )
        result = WORKFLOW.advance_stage(assignment=self.assignment, artifact_file=artifact)
        self.assignment["stage_artifacts"]["analysis"] = result["artifact"]
        self.assignment["triage_stage"] = "materialize"
        canonical = self.workspace / result["artifact"]["path"]
        canonical.write_text("{}\n", encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "TRIAGE_ARTIFACT_DIGEST_MISMATCH"):
            WORKFLOW.materialization_arguments(self.assignment)

    def _prepare_intake(self) -> None:
        artifact = self._write(
            "intake.json",
            {
                "schema": "kwc-triage-intake-authoring-v1",
                "identity": self.authoring["identity"],
                "intake": self.authoring["intake"],
            },
        )
        result = WORKFLOW.advance_stage(
            assignment=self.assignment,
            artifact_file=artifact,
            inspection_fn=lambda *_args: self.inspection,
        )
        self.assignment["stage_artifacts"]["intake"] = result["artifact"]

    def _prepare_discovery(self) -> None:
        artifact = self._write("discovery.json", self._discovery_payload())
        discovery_assignment = {**self.assignment, "triage_stage": "discovery"}
        result = WORKFLOW.advance_stage(
            assignment=discovery_assignment,
            artifact_file=artifact,
            git_head_fn=lambda _workspace: "a" * 40,
        )
        self.assignment["stage_artifacts"]["discovery"] = result["artifact"]

    def _discovery_payload(self) -> dict:
        target = self.authoring["intake"]["investigation_targets"][0]
        locators = {
            target["locator"],
            self.authoring["analysis"]["confirmed_gaps"][0]["evidence"][0]["locator"],
            self.authoring["analysis"]["verification_matrix"][0]["evidence"][0]["locator"],
            self.authoring["analysis"]["verification_matrix"][0]["production_seam"]["locator"],
            *self.authoring["identity"]["requirement_locators"],
            *self.authoring["identity"]["current_state_locators"],
            "docs/backend-development-guide.md#Backend development guide",
            "docs/backend-test-guide.md#Backend test guide",
        }
        return {
            "schema": "kwc-triage-discovery-v2",
            "targets": [{
                "target_id": target["id"],
                "candidates": [
                    {
                        "path": locator.split("#", 1)[0],
                        "symbol": locator.split("#", 1)[1],
                        "kind": (
                            "document"
                            if locator.startswith("docs/")
                            else "test"
                            if locator.startswith("src/test/")
                            else "production"
                        ),
                        "reason": "target implementation evidence",
                    }
                    for locator in sorted(locators)
                ],
            }],
            "tool_usage": {"codebase_memory": 1, "semble": 1},
            "fallbacks": [],
            "codebase_memory": self._available_codebase_memory(),
        }

    def _available_codebase_memory(self) -> dict:
        return {
            "projects": [{
                "project": "orders-project",
                "root_path": str(self.workspace),
                "root_exists": True,
                "git_head": "a" * 40,
            }],
            "resolution": {
                "status": "available",
                "project": "orders-project",
                "reason": "workspace-and-head-match",
                "git_head": "a" * 40,
            },
        }

    def _prepare_confirmation(self) -> None:
        target = self.authoring["intake"]["investigation_targets"][0]
        discovery = json.loads(
            (self.workspace / self.assignment["stage_artifacts"]["discovery"]["path"]).read_text(
                encoding="utf-8"
            )
        )
        artifact = self._write(
            "confirmation.json",
            {
                "schema": "kwc-triage-source-confirmation-v2",
                "targets": [{
                    "target_id": target["id"],
                    "disposition": "partial",
                    "rationale": "direct inspection",
                    "public_contract": None,
                    "evidence": [
                        {
                            "locator": f'{candidate["path"]}#{candidate["symbol"]}',
                            "claim": "confirmed implementation evidence",
                            "kind": candidate["kind"],
                        }
                        for candidate in discovery["targets"][0]["candidates"]
                    ],
                }],
                "open_questions": [],
            },
        )
        confirmation_assignment = {**self.assignment, "triage_stage": "source_confirmation"}
        result = WORKFLOW.advance_stage(assignment=confirmation_assignment, artifact_file=artifact)
        self.assignment["stage_artifacts"]["source_confirmation"] = result["artifact"]


if __name__ == "__main__":
    unittest.main()
