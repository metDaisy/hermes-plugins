import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPT_DIR = Path(__file__).resolve().parent
SKILLS_DIR = SCRIPT_DIR.parent.parent
sys.path.insert(0, str(SCRIPT_DIR.parent / "scripts"))
sys.path.insert(0, str(SKILLS_DIR / "kwc-create-triage" / "scripts"))
sys.path.insert(0, str(SKILLS_DIR / "kwc-build-task-graph" / "scripts"))

from activate_graph import _hermes_python, activate
from contract_common import serialize_task_body
from triage import freeze, template


class ActivateGraphTest(unittest.TestCase):
    def test_hermes_python_prefers_committed_dependency_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            runtime_root = home / "hermes-agent"
            (runtime_root / "hermes_cli").mkdir(parents=True)
            (runtime_root / "pm").mkdir()
            launcher = runtime_root / "hermes.exe"
            launcher.touch()

            import hashlib

            install_key = hashlib.sha256(str(runtime_root.resolve()).encode("utf-8")).hexdigest()[:16]
            environment = home / "installs" / install_key / "environments" / "generation" / "venv"
            python = environment / "Scripts" / "python.exe"
            python.parent.mkdir(parents=True)
            python.touch()
            (environment / "pyvenv.cfg").write_text("home = test\n", encoding="utf-8")
            facts = home / "installs" / install_key / "facts.json"
            facts.parent.mkdir(parents=True, exist_ok=True)
            facts.write_text(
                json.dumps({"packages": {"venv": {"environment": str(environment)}}}),
                encoding="utf-8",
            )

            with mock.patch("activate_graph.shutil.which", return_value=str(launcher)):
                self.assertEqual(str(python.resolve()), _hermes_python("hermes"))

    def triage_body(self):
        body = template(
            138,
            "G1-Issue138-Triage",
            "https://github.com/example/repository/issues/138",
            "a" * 40,
            "b" * 40,
            ["docs/requirement/p2/index.md#issue-138"],
            ["docs/current-state.md#p2"],
        )
        requirement_source = {
            "locator": "https://github.com/example/repository/issues/138#상품-등록",
            "claim": "판매자는 유효한 요청으로 상품을 등록할 수 있다.",
        }
        body["phase"] = "analysis"
        body["intent"] = {
            "goal": "Seller flow behavior is ready for task graph authoring.",
            "change_requirements": [
                {
                    "id": "CHANGE-PRODUCT-CREATE",
                    "outcome": "판매자는 유효한 요청으로 상품을 등록할 수 있다.",
                    "source": requirement_source,
                }
            ],
            "preserved_observables": [
                {
                    "id": "OBS-PRODUCT-CREATE",
                    "behavior": "유효한 판매자 요청은 상품 생성 결과를 반환한다.",
                    "source": requirement_source,
                }
            ],
            "quality_gates": [],
            "finalization_requirements": [],
            "out_of_scope": ["Unrelated P2 flows"],
        }
        body["investigation_targets"] = [
            {
                "id": "TARGET-PRODUCT-CREATE",
                "locator": "src/main/java/example/SellerService.java#L1-L20",
                "question": "Seller product creation 책임이 올바른 계층에 있는가?",
                "source": requirement_source,
            }
        ]
        body["confirmed_gaps"] = [
            {
                "id": "GAP-PRODUCT-CREATE",
                "subject": "Seller product creation flow",
                "current_state": "현재 구현이 application 책임을 우회한다.",
                "required_state": "application은 aggregate behavior를 호출한다.",
                "evidence": [
                    {
                        "locator": "src/main/java/example/SellerService.java#L1-L20",
                        "claim": "현재 구현의 책임 배치를 직접 확인했다.",
                    }
                ],
                "rules": [
                    {
                        "locator": "docs/backend-development-guide.md#Application-구현-규칙",
                        "claim": "application은 use case 순서를 조정한다.",
                    }
                ],
                "requirement_ids": ["CHANGE-PRODUCT-CREATE"],
                "affected_observable_ids": ["OBS-PRODUCT-CREATE"],
            }
        ]
        body["target_dispositions"] = [
            {
                "target_id": "TARGET-PRODUCT-CREATE",
                "disposition": "gap",
                "finding_ids": ["GAP-PRODUCT-CREATE"],
                "rationale": "현재 구현과 목표 책임 사이의 gap을 확인했다.",
            }
        ]
        body["implementation_slices"] = [
            {
                "id": "SLICE-PRODUCT-CREATE",
                "title": "Seller product creation 책임 정리",
                "outcome": "Seller product creation 책임 위반을 수정한다.",
                "gap_ids": ["GAP-PRODUCT-CREATE"],
                "scope": [
                    "modify: src/main/java/example/SellerService.java#create - 판매자 상품 생성 application 흐름 책임을 정리한다."
                ],
                "out_of_scope": ["관련 없는 P2 흐름 변경"],
                "acceptance": [
                    {
                        "id": "ACCEPT-PRODUCT-CREATE",
                        "criterion": "SellerService#create가 승인된 application boundary를 사용한다.",
                        "requirement_ids": ["CHANGE-PRODUCT-CREATE"],
                        "observable_ids": ["OBS-PRODUCT-CREATE"],
                        "verification_ids": ["VERIFY-PRODUCT-CREATE"],
                    }
                ],
                "contract_change_ids": [],
                "depends_on": [],
            }
        ]
        body["verification_matrix"] = [
            {
                "id": "VERIFY-PRODUCT-CREATE",
                "observable_id": "OBS-PRODUCT-CREATE",
                "current_coverage": "missing",
                "evidence": [],
                "production_seam": {
                    "locator": "src/main/java/example/SellerService.java#create",
                    "claim": "Seller product creation을 실행하는 current application owner다.",
                },
                "coverage_gap": "Focused integration coverage is absent.",
                "required_verification": [
                    "example.SellerServiceTest#createsProduct로 product creation을 검증한다."
                ],
            }
        ]
        body["contract_changes"] = []
        body["document_impacts"] = [
            {
                "document": "docs/requirement/p2/index.md",
                "disposition": "no-change",
                "evidence": [requirement_source],
                "action": None,
            }
        ]
        body["open_decisions"] = []
        body["handoff"] = {"graph_allowed": False, "blocker": "freeze-required"}
        return freeze(body)

    def graph_and_readback(self):
        fixture = SKILLS_DIR / "kwc-build-task-graph" / "tests" / "fixtures" / "valid-graph-draft.json"
        graph = json.loads(fixture.read_text(encoding="utf-8"))
        ids = {card["key"]: f"t_{index + 1}" for index, card in enumerate(graph["cards"])}
        frozen_triage = self.triage_body()
        graph["triage_digest"] = frozen_triage["frozen_digest"]
        next(card for card in graph["cards"] if card["card_type"] == "implementation")["body"]["triage_digest"] = graph["triage_digest"]
        next(card for card in graph["cards"] if card["card_type"] == "review")["body"]["triage_digest"] = graph["triage_digest"]
        next(card for card in graph["cards"] if card["card_type"] == "summary")["body"]["triage_digest"] = graph["triage_digest"]
        graph["change_requirements"] = copy.deepcopy(frozen_triage["intent"]["change_requirements"])
        triage_body = json.dumps(frozen_triage, ensure_ascii=False)
        readback = []
        for card in graph["cards"]:
            body = triage_body if card["card_type"] == "triage" else serialize_task_body(card["body"])
            readback.append(
                {
                    "task": {
                        "id": ids[card["key"]],
                        "title": card["title"],
                        "assignee": card["assignee"],
                        "status": card["status"],
                        "priority": card["priority"],
                        "skills": card["skills"],
                        "body": body,
                    },
                    "parents": [ids[parent] for parent in card["parents"]],
                    "comments": [],
                }
            )
        return graph, readback

    def no_implementation_graph_and_readback(self):
        graph, readback = self.graph_and_readback()
        behavior = graph["behaviors"][0]
        behavior.update({"state": "implemented", "disposition": "inherited", "evidence": ["source:test"]})
        behavior.pop("implementation_card_key")
        graph["cards"] = [card for card in graph["cards"] if card["key"] != "impl-1"]
        review = next(card for card in graph["cards"] if card["key"] == "review-1")
        review["parents"] = ["triage"]
        review["body"]["implementation_card_keys"] = []
        for acceptance in review["body"]["acceptance"]:
            acceptance["implementation_card_keys"] = []
            acceptance["source_acceptance_ids"] = []
            acceptance["verification_ids"] = []
        review["body"]["review_checks"] = [
            {
                "id": "REVIEW-CHECK-PRESERVATION-001",
                "focus": "preservation",
                "question": "이미 충족된 상품 등록 동작이 baseline에서 보존되는가?",
                "expected_outcome": "Review는 inherited 동작의 committed evidence를 확인한다.",
                "behavior_ids": [behavior["id"]],
                "implementation_card_keys": [],
                "acceptance_ids": [review["body"]["acceptance"][0]["id"]],
                "source_acceptance_ids": [],
                "verification_ids": [],
                "guide_locators": [
                    "docs/backend-development-guide.md#Module과 계층",
                    "docs/backend-test-guide.md#11. Test level 선택과 검증",
                    ".hermes/docs/plan.md#PM Workflow 계약 확정 및 KWC 점진 도입 계획",
                ],
            }
        ]
        summary = next(card for card in graph["cards"] if card["key"] == "summary")
        summary["parents"] = ["review-1"]
        review["priority"] = 1
        graph["execution_order"] = ["review-1"]
        graph["ready_candidate"] = "review-1"

        by_title = {entry["task"]["title"]: entry for entry in readback}
        by_title["G1-Issue138-Review1"]["task"]["priority"] = 1

        readback = [entry for entry in readback if entry["task"]["title"] != "G1-Issue138-Impl1"]
        by_title = {entry["task"]["title"]: entry for entry in readback}
        review_readback = by_title["G1-Issue138-Review1"]
        review_readback["parents"] = [by_title["G1-Issue138-Triage"]["task"]["id"]]
        review_readback["task"]["body"] = serialize_task_body(review["body"])
        by_title["G1-Issue138-Summary"]["parents"] = [review_readback["task"]["id"]]
        return graph, readback

    def runner_for(
        self,
        readback,
        *,
        auto_ready_after_triage_completion=False,
        fail_triage_completion_once=False,
        fail_promotion_once=False,
        drift_target_body=False,
    ):
        by_id = {entry["task"]["id"]: copy.deepcopy(entry) for entry in readback}
        calls = []
        triage_completion_failed = False
        promotion_failed = False
        target_drifted = False

        def runner(command, **kwargs):
            nonlocal promotion_failed, triage_completion_failed, target_drifted
            if len(command) > 1 and command[1] == "kanban":
                command = [command[0], "--profile", "test-host", *command[1:]]
            calls.append(command)
            if len(command) > 1 and Path(command[1]).name == "transition_triage.py":
                task_id = command[command.index("--task-id") + 1]
                self.assertEqual("triage", by_id[task_id]["task"]["status"])
                by_id[task_id]["task"]["status"] = "ready"
                return subprocess.CompletedProcess(command, 0, json.dumps({"valid": True}), "")
            action = command[6]
            if action == "show":
                task_id = command[7]
                if drift_target_body and task_id == "t_2" and not target_drifted:
                    by_id[task_id]["task"]["body"] = json.dumps({"schema": "unexpected-drift"})
                    target_drifted = True
                return subprocess.CompletedProcess(command, 0, json.dumps(by_id[task_id]), "")
            if action == "complete":
                task_ids = command[7:]
                if task_ids[:1] == ["--result"]:
                    task_ids = task_ids[2:]
                if fail_triage_completion_once and "t_1" in task_ids and not triage_completion_failed:
                    triage_completion_failed = True
                    return subprocess.CompletedProcess(command, 1, "", "injected triage completion failure")
                for task_id in task_ids:
                    if by_id[task_id]["task"]["status"] == "triage":
                        return subprocess.CompletedProcess(command, 1, "", "cannot complete triage directly")
                    by_id[task_id]["task"]["status"] = "done"
                    by_id[task_id]["runs"] = [{"status": "completed", "outcome": "completed"}]
                    if auto_ready_after_triage_completion and task_id == "t_1":
                        by_id["t_2"]["task"]["status"] = "ready"
                return subprocess.CompletedProcess(command, 0, "", "")
            if action == "promote":
                if fail_promotion_once and not promotion_failed:
                    promotion_failed = True
                    return subprocess.CompletedProcess(command, 1, "", "injected promotion failure")
                by_id[command[7]]["task"]["status"] = "ready"
                return subprocess.CompletedProcess(command, 0, "", "")
            if action == "comment":
                by_id[command[7]]["comments"].append(
                    {"author": "project-manager", "body": command[8]}
                )
                return subprocess.CompletedProcess(command, 0, "", "")
            raise AssertionError(command)

        return runner, calls

    def invoke(self, graph, readback, runner, *, apply):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            graph_path = root / "graph.json"
            readback_path = root / "readback.json"
            graph_path.write_text(json.dumps(graph, ensure_ascii=False), encoding="utf-8")
            readback_path.write_text(json.dumps(readback, ensure_ascii=False), encoding="utf-8")
            return activate(
                graph_path,
                readback_path,
                "t_1",
                [],
                "impl-1",
                "hermes",
                "activation-test",
                apply=apply,
                runner=runner,
            )

    def test_preflight_is_read_only_and_valid(self):
        graph, readback = self.graph_and_readback()
        runner, calls = self.runner_for(readback)
        report = self.invoke(graph, readback, runner, apply=False)
        self.assertTrue(report["valid"])
        self.assertFalse(report["mutated"])
        self.assertTrue(all(command[6] == "show" for command in calls))

    def test_preflight_rejects_graph_without_matching_triage_digest(self):
        graph, readback = self.graph_and_readback()
        graph.pop("triage_digest")
        runner, calls = self.runner_for(readback)

        report = self.invoke(graph, readback, runner, apply=False)

        self.assertFalse(report["valid"])
        self.assertIn("TRIAGE_GRAPH_DIGEST_MISMATCH", report["errors"])
        self.assertTrue(all(command[6] == "show" for command in calls))

    def test_preflight_rejects_graph_that_changes_triage_requirements(self):
        graph, readback = self.graph_and_readback()
        graph["change_requirements"][0]["outcome"] = "그래프가 Triage 의무를 임의로 변경했다."
        runner, calls = self.runner_for(readback)

        report = self.invoke(graph, readback, runner, apply=False)

        self.assertFalse(report["valid"])
        self.assertIn("TRIAGE_GRAPH_REQUIREMENTS_MISMATCH", report["errors"])
        self.assertTrue(all(command[6] == "show" for command in calls))


    def test_preflight_routes_done_triage_rematerialization_to_fresh_gate(self):
        graph, readback = self.graph_and_readback()
        graph["cards"][0]["status"] = "done"
        graph["cards"][1]["status"] = "ready"
        readback[0]["task"]["status"] = "done"
        readback[0]["runs"] = [{"status": "completed", "outcome": "completed"}]
        readback[1]["task"]["status"] = "ready"
        runner, calls = self.runner_for(readback)

        report = self.invoke(graph, readback, runner, apply=False)

        self.assertFalse(report["valid"])
        self.assertIn("RECOVERY_REQUIRES_FRESH_TRIAGE_GATE", report["errors"])
        self.assertTrue(all(command[6] == "show" for command in calls))

    def test_failed_preflight_never_completes_or_promotes(self):
        graph, readback = self.graph_and_readback()
        readback[0]["task"]["status"] = "running"
        runner, calls = self.runner_for(readback)
        report = self.invoke(graph, readback, runner, apply=True)
        self.assertFalse(report["valid"])
        self.assertIn("TRIAGE_NOT_NATIVE_TRIAGE_OR_DONE", report["errors"])
        self.assertFalse(report["mutated"])
        self.assertTrue(all(command[6] == "show" for command in calls))

    def test_apply_completes_triage_before_single_promotion(self):
        graph, readback = self.graph_and_readback()
        runner, calls = self.runner_for(readback)
        report = self.invoke(graph, readback, runner, apply=True)
        self.assertTrue(report["valid"])
        self.assertTrue(report["mutated"])
        mutations = [
            "transition-triage" if len(command) > 1 and Path(command[1]).name == "transition_triage.py" else command[6]
            for command in calls
            if len(command) > 1 and (Path(command[1]).name == "transition_triage.py" or command[6] != "show")
        ]
        self.assertEqual(["transition-triage", "complete", "promote"], mutations)


    def test_apply_accepts_target_auto_promoted_by_triage_completion(self):
        graph, readback = self.graph_and_readback()
        runner, calls = self.runner_for(readback, auto_ready_after_triage_completion=True)

        report = self.invoke(graph, readback, runner, apply=True)

        self.assertTrue(report["valid"])
        mutations = [
            "transition-triage" if len(command) > 1 and Path(command[1]).name == "transition_triage.py" else command[6]
            for command in calls
            if len(command) > 1 and (Path(command[1]).name == "transition_triage.py" or command[6] != "show")
        ]
        self.assertEqual(["transition-triage", "complete"], mutations)

    def test_apply_fails_closed_when_execution_card_drifts_after_preflight(self):
        graph, readback = self.graph_and_readback()
        runner, calls = self.runner_for(readback, drift_target_body=True)

        report = self.invoke(graph, readback, runner, apply=True)

        self.assertFalse(report["valid"])
        self.assertFalse(report["mutated"])
        self.assertIn("LIVE_GRAPH_DRIFT:NATIVE_BODY_MISMATCH:impl-1", report["errors"])
        self.assertTrue(all(command[6] == "show" for command in calls))

    def test_apply_resumes_after_interrupted_triage_transition(self):
        graph, readback = self.graph_and_readback()
        runner, calls = self.runner_for(readback, fail_triage_completion_once=True)

        interrupted = self.invoke(graph, readback, runner, apply=True)
        resumed = self.invoke(graph, readback, runner, apply=True)

        self.assertFalse(interrupted["valid"])
        self.assertIn("NATIVE_TRIAGE_COMPLETE_FAILED:1:injected triage completion failure", interrupted["errors"])
        self.assertTrue(resumed["valid"], resumed["errors"])
        mutations = [
            "transition-triage" if len(command) > 1 and Path(command[1]).name == "transition_triage.py" else command[6]
            for command in calls
            if len(command) > 1 and (Path(command[1]).name == "transition_triage.py" or command[6] != "show")
        ]
        self.assertEqual(["transition-triage", "complete", "complete", "promote"], mutations)

    def test_apply_resumes_after_triage_completed_before_promotion_failure(self):
        graph, readback = self.graph_and_readback()
        runner, calls = self.runner_for(readback, fail_promotion_once=True)

        interrupted = self.invoke(graph, readback, runner, apply=True)
        resumed = self.invoke(graph, readback, runner, apply=True)

        self.assertFalse(interrupted["valid"])
        self.assertIn("NATIVE_PROMOTE_FAILED:1", interrupted["errors"])
        self.assertTrue(resumed["valid"], resumed["errors"])
        mutations = [
            "transition-triage" if len(command) > 1 and Path(command[1]).name == "transition_triage.py" else command[6]
            for command in calls
            if len(command) > 1 and (Path(command[1]).name == "transition_triage.py" or command[6] != "show")
        ]
        self.assertEqual(["transition-triage", "complete", "promote", "promote"], mutations)

    def test_no_implementation_review_admission_is_written_before_promotion(self):
        graph, readback = self.no_implementation_graph_and_readback()
        runner, calls = self.runner_for(readback)
        admission = {
            "schema": "aggregate-review-admission-v1",
            "task_id": "t_3",
            "baseline_sha": "0" * 40,
            "reviewed_head_sha": "b" * 40,
            "checkpoints": [],
        }

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            graph_path = root / "graph.json"
            readback_path = root / "readback.json"
            graph_path.write_text(json.dumps(graph), encoding="utf-8")
            readback_path.write_text(json.dumps(readback), encoding="utf-8")
            missing = activate(
                graph_path,
                readback_path,
                "t_1",
                [],
                "review-1",
                "hermes",
                "activation-test",
                apply=True,
                runner=runner,
            )
        self.assertFalse(missing["valid"])
        self.assertIn("REVIEW_ADMISSION_REQUIRED", missing["errors"])
        self.assertTrue(all(command[6] == "show" for command in calls))

        calls.clear()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            graph_path = root / "graph.json"
            readback_path = root / "readback.json"
            graph_path.write_text(json.dumps(graph), encoding="utf-8")
            readback_path.write_text(json.dumps(readback), encoding="utf-8")
            report = activate(
                graph_path,
                readback_path,
                "t_1",
                [],
                "review-1",
                "hermes",
                "activation-test",
                apply=True,
                review_admission=admission,
                runner=runner,
            )

        self.assertTrue(report["valid"], report["errors"])
        mutations = [
            "transition-triage" if len(command) > 1 and Path(command[1]).name == "transition_triage.py" else command[6]
            for command in calls
            if len(command) > 1 and (Path(command[1]).name == "transition_triage.py" or command[6] != "show")
        ]
        self.assertEqual(["comment", "transition-triage", "complete", "promote"], mutations)

    def test_preflight_accepts_one_aggregate_decision_for_all_requests(self):
        graph, readback = self.graph_and_readback()
        triage_body = json.loads(readback[0]["task"]["body"])

        def approved_request(identifier, question):
            return {
                "id": identifier,
                "question": question,
                "why_unresolved": "The fixed contract allows two implementation seams.",
                "evidence": [
                    {
                        "locator": "https://github.com/example/repository/issues/138#상품-등록",
                        "claim": "승인된 Issue가 사용자 선택이 필요한 상품 등록 contract 권한을 소유한다.",
                    }
                ],
                "affected_observable_ids": ["OBS-PRODUCT-CREATE"],
                "options": [
                    {
                        "id": "A",
                        "choice": f"{question} through seam A",
                        "impact": "Preserves the contract.",
                        "preserves_observable_ids": ["OBS-PRODUCT-CREATE"],
                    },
                    {
                        "id": "B",
                        "choice": f"{question} through seam B",
                        "impact": "Also preserves the contract.",
                        "preserves_observable_ids": ["OBS-PRODUCT-CREATE"],
                    },
                ],
                "owner": "user",
                "status": "approved",
                "approved_option_id": "A",
            }

        triage_body["phase"] = "analysis"
        triage_body["frozen_digest"] = None
        triage_body["handoff"] = {"graph_allowed": False, "blocker": "freeze-required"}
        triage_body["open_decisions"] = [
            approved_request("DR-001", "Choose the data seam"),
            approved_request("DR-002", "Choose the error seam"),
        ]
        minimum_capability = {
            "contract_owner": "seller",
            "public_surface": "SellerQueryApi",
            "request_or_event": "Seller lookup request",
            "response_or_projection": "Immutable Seller facts",
            "not_found_and_error_semantics": "Missing Seller uses the existing error contract.",
            "consistency_and_transaction_semantics": "The seam does not expose persistence details.",
            "verification_plan": "Producer and consumer contract tests verify the seam.",
        }
        follow_up = {
            "producer": "Implement the seller-owned seam.",
            "consumer": "Implement the catalog adapter.",
            "integration_verification": "Run producer-consumer integration tests.",
        }
        frozen_triage = freeze(triage_body)
        graph["triage_digest"] = frozen_triage["frozen_digest"]
        implementation = next(card for card in graph["cards"] if card["card_type"] == "implementation")
        implementation["body"]["triage_digest"] = graph["triage_digest"]
        next(entry for entry in readback if entry["task"]["title"] == implementation["title"])["task"]["body"] = serialize_task_body(implementation["body"])
        review = next(card for card in graph["cards"] if card["card_type"] == "review")
        review["body"]["triage_digest"] = graph["triage_digest"]
        next(entry for entry in readback if entry["task"]["title"] == review["title"])["task"]["body"] = serialize_task_body(review["body"])
        summary = next(card for card in graph["cards"] if card["card_type"] == "summary")
        summary["body"]["triage_digest"] = graph["triage_digest"]
        next(entry for entry in readback if entry["task"]["title"] == summary["title"])["task"]["body"] = serialize_task_body(summary["body"])
        readback[0]["task"]["body"] = json.dumps(frozen_triage)
        decision = {
            "task": {
                "id": "t_decision",
                "title": "G1-Issue138-PlanningDecision",
                "assignee": "project-manager",
                "status": "running",
                "body": json.dumps(
                    {
                        "schema": "policy-decision-card-v1",
                        "decision_requests": [
                            {"id": "DR-001", "decision_status": "approved", "approved_change": "Use data seam A.", "minimum_capability": minimum_capability, "follow_up": follow_up},
                            {"id": "DR-002", "decision_status": "approved", "approved_change": "Use error seam A.", "minimum_capability": minimum_capability, "follow_up": follow_up},
                        ],
                    }
                ),
            },
            "parents": [],
            "runs": [],
        }
        runner, calls = self.runner_for([*readback, decision])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            graph_path = root / "graph.json"
            readback_path = root / "readback.json"
            graph_path.write_text(json.dumps(graph), encoding="utf-8")
            readback_path.write_text(json.dumps(readback), encoding="utf-8")
            report = activate(
                graph_path,
                readback_path,
                "t_1",
                ["t_decision"],
                "impl-1",
                "hermes",
                "activation-test",
                apply=False,
                runner=runner,
            )
        self.assertTrue(report["valid"], report["errors"])
        self.assertTrue(all(command[6] == "show" for command in calls))


if __name__ == "__main__":
    unittest.main()
