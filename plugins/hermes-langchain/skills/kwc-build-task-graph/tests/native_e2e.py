#!/usr/bin/env python3
"""격리 board에서 build-task-graph native lifecycle을 검증한다."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

SKILL_DIR = Path(__file__).resolve().parents[1]
PROFILE_DIR = SKILL_DIR.parents[1]
sys.path.insert(0, str(SKILL_DIR / "scripts"))
sys.path.insert(0, str(PROFILE_DIR / "skills" / "kwc-run-workflow" / "scripts"))
sys.path.insert(0, str(PROFILE_DIR / "skills" / "kwc-create-triage" / "scripts"))
from activate_graph import activate  # noqa: E402
from build_task_graph import validate  # noqa: E402
from graph_contract import validate_graph  # noqa: E402
from triage import freeze, template  # noqa: E402
from workflow import validate_implementation_admission  # noqa: E402


def bind_frozen_triage_digest(graph: dict[str, Any], triage_body: dict[str, Any]) -> None:
    """Bind the real frozen Triage digest into every graph consumer."""
    digest = triage_body.get("frozen_digest")
    if not isinstance(digest, str) or len(digest) != 64:
        raise RuntimeError("frozen Triage body has no valid digest")
    graph["triage_digest"] = digest
    for card in graph.get("cards", []):
        body = card.get("body") if isinstance(card, dict) else None
        if isinstance(body, dict) and "triage_digest" in body:
            body["triage_digest"] = digest


class NativeE2E:
    def __init__(self, repository: Path) -> None:
        self.repository = repository
        self.board = f"btg-e2e-{uuid.uuid4().hex[:8]}"
        self.hermes = shutil.which("hermes")
        if self.hermes is None:
            raise RuntimeError("hermes executable is not available")
        self.hermes_home = Path(
            tempfile.mkdtemp(
                prefix="kwc-native-e2e-",
                dir=os.environ.get("TMPDIR"),
            )
        )
        self.previous_hermes_home = os.environ.get("HERMES_HOME")
        os.environ["HERMES_HOME"] = str(self.hermes_home)
        self.workspace: str | None = None
        self.issue: int | None = None
        self.body_dir = self.repository / ".temp" / "task-graphs" / "native-e2e" / self.board

    def run(self, *args: str, json_output: bool = False) -> Any:
        env = dict(os.environ)
        env["HERMES_HOME"] = str(self.hermes_home)
        completed = subprocess.run(
            [self.hermes, "kanban", *args],
            cwd=self.repository,
            check=False,
            text=True,
            encoding="utf-8",
            capture_output=True,
            env=env,
        )
        if completed.returncode != 0:
            raise RuntimeError(f"kanban command failed ({completed.returncode}): {completed.stderr or completed.stdout}")
        return json.loads(completed.stdout) if json_output else completed.stdout.strip()

    def board_run(self, *args: str, json_output: bool = False) -> Any:
        return self.run("--board", self.board, *args, json_output=json_output)

    @staticmethod
    def task_id(payload: dict[str, Any]) -> str:
        task = payload.get("task", payload)
        value = task.get("id")
        if not isinstance(value, str):
            raise RuntimeError("create response has no task id")
        return value

    def create(
        self,
        title: str,
        assignee: str,
        body: dict[str, Any] | str,
        *,
        priority: int,
        parents: list[str] | None = None,
        idempotency_key: str | None = None,
        triage: bool = False,
    ) -> str:
        if self.workspace is None:
            raise RuntimeError("workspace is not initialized")
        self.body_dir.mkdir(parents=True, exist_ok=True)
        body_key = uuid.uuid4().hex
        graph_path = self.body_dir / f"{body_key}-graph.json"
        body_path = self.body_dir / f"{body_key}-body.json"
        graph_path.write_text(
            json.dumps({"cards": [{"key": body_key, "body": body}]}, ensure_ascii=False, indent=2, allow_nan=False),
            encoding="utf-8",
        )
        completed = subprocess.run(
            [
                sys.executable,
                str(SKILL_DIR / "scripts" / "build_task_graph.py"),
                "render-body",
                str(graph_path),
                "--card-key",
                body_key,
                "--output",
                str(body_path.relative_to(self.repository)),
            ],
            cwd=self.repository,
            check=False,
            text=True,
            encoding="utf-8",
            capture_output=True,
        )
        if completed.returncode != 0:
            raise RuntimeError(f"render-body failed: {completed.stdout or completed.stderr}")
        body_text = body_path.read_bytes().decode("utf-8")
        args = [
            "create",
            title,
            "--assignee",
            assignee,
            "--body-file",
            str(body_path),
            "--workspace",
            self.workspace,
            "--priority",
            str(priority),
        ]
        for parent in parents or []:
            args.extend(["--parent", parent])
        if idempotency_key:
            args.extend(["--idempotency-key", idempotency_key])
        if triage:
            args.append("--triage")
        args.append("--json")
        task_id = self.task_id(self.board_run(*args, json_output=True))
        if self.show(task_id)["task"]["body"] != body_text:
            raise RuntimeError("native task body did not preserve the rendered body")
        if assignee == "coder":
            admission = {
                "schema": "backend-implementation-admission-v2",
                "task_id": task_id,
                "issue": self.issue,
                "workspace": self.workspace,
                "card_schema": "backend-implementation-card-v6",
                "baseline": {
                    "head_sha": "a" * 40,
                    "dirty_paths": [],
                },
                "restart": None,
            }
            failures = validate_implementation_admission(admission)
            if failures:
                raise RuntimeError(f"implementation admission invalid: {failures}")
            self.board_run(
                "comment",
                "--author",
                "project-manager",
                task_id,
                json.dumps(admission, ensure_ascii=False),
            )
        return task_id

    def show(self, task_id: str) -> dict[str, Any]:
        return self.board_run("show", task_id, "--json", json_output=True)

    def triage_body(self, fixture: dict[str, Any]) -> dict[str, Any]:
        issue = fixture["issue"]
        triage_card = next(card for card in fixture["cards"] if card["card_type"] == "triage")
        body = template(
            issue["number"],
            triage_card["title"],
            issue["url"],
            "a" * 40,
            "b" * 40,
            ["docs/requirement/p2/index.md#issue-138"],
            ["docs/current-state.md#p2"],
        )
        requirement = fixture["change_requirements"][0]
        body["phase"] = "analysis"
        body["intent"] = {
            "goal": "Seller flow behavior is ready for task graph authoring.",
            "change_requirements": fixture["change_requirements"],
            "preserved_observables": [
                {
                    "id": "OBS-SELLER-FLOW",
                    "behavior": "유효한 판매자 요청은 상품 생성 결과를 반환한다.",
                    "source": requirement["source"],
                }
            ],
            "quality_gates": [],
            "finalization_requirements": [],
            "out_of_scope": ["Unrelated P2 flows"],
        }
        body["investigation_targets"] = [
            {
                "id": "TARGET-SELLER-FLOW",
                "locator": "src/main/java/example/SellerService.java#L1-L20",
                "question": "Seller flow 책임이 올바른 계층에 있는가?",
                "source": requirement["source"],
            }
        ]
        body["confirmed_gaps"] = [
            {
                "id": "GAP-SELLER-FLOW",
                "subject": "Seller flow implementation baseline",
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
                "requirement_ids": [requirement["id"]],
                "affected_observable_ids": ["OBS-SELLER-FLOW"],
            }
        ]
        body["target_dispositions"] = [
            {
                "target_id": "TARGET-SELLER-FLOW",
                "disposition": "gap",
                "finding_ids": ["GAP-SELLER-FLOW"],
                "rationale": "현재 구현과 목표 책임 사이의 gap을 확인했다.",
            }
        ]
        body["implementation_slices"] = [
            {
                "id": "SLICE-SELLER-FLOW",
                "title": "Seller flow 책임 정리",
                "outcome": "Seller flow 책임 위반을 수정한다.",
                "gap_ids": ["GAP-SELLER-FLOW"],
                "scope": [
                    "modify: src/main/java/example/SellerService.java#create - 판매자 상품 생성 application 경계를 정리한다."
                ],
                "out_of_scope": ["관련 없는 P2 흐름 변경"],
                "acceptance": [
                    {
                        "id": "ACCEPT-SELLER-FLOW",
                        "criterion": "SellerService#create가 승인된 application boundary를 사용한다.",
                        "requirement_ids": [requirement["id"]],
                        "observable_ids": ["OBS-SELLER-FLOW"],
                        "verification_ids": ["VERIFY-SELLER-FLOW"],
                    }
                ],
                "contract_change_ids": [],
                "depends_on": [],
            }
        ]
        body["verification_matrix"] = [
            {
                "id": "VERIFY-SELLER-FLOW",
                "observable_id": "OBS-SELLER-FLOW",
                "current_coverage": "missing",
                "evidence": [],
                "production_seam": {
                    "locator": "src/main/java/example/SellerService.java#L1-L20",
                    "claim": "Seller flow의 current application owner를 직접 확인한다.",
                },
                "coverage_gap": "Focused integration coverage is absent.",
                "required_verification": [
                    "example.SellerServiceTest#createsProduct로 seller flow를 검증한다."
                ],
            }
        ]
        body["contract_changes"] = []
        body["document_impacts"] = [
            {
                "document": "docs/requirement/p2/index.md",
                "disposition": "no-change",
                "evidence": [requirement["source"]],
                "action": None,
            }
        ]
        body["open_decisions"] = []
        body["handoff"] = {"graph_allowed": False, "blocker": "freeze-required"}
        return freeze(body)

    @staticmethod
    def status(envelope: dict[str, Any]) -> str:
        return str(envelope["task"]["status"])

    def graph_wrapper(
        self,
        fixture: dict[str, Any],
        ids: dict[str, str],
    ) -> dict[str, Any]:
        reverse = {task_id: key for key, task_id in ids.items()}
        graph = json.loads(json.dumps(fixture))
        records = {card["key"]: card for card in graph["cards"]}
        for key, task_id in ids.items():
            envelope = self.show(task_id)
            task = envelope["task"]
            record = records[key]
            record["status"] = task["status"]
            record["assignee"] = task["assignee"]
            record["priority"] = task["priority"]
            # Native `kanban show` does not expose workspace. Every create call
            # above is still bound to the fixture workspace, which remains in
            # this normalized validation wrapper.
            native_parents = [parent["id"] if isinstance(parent, dict) else parent for parent in envelope["parents"]]
            record["parents"] = [reverse[parent] for parent in native_parents]
            if key != "triage":
                record["body"] = json.loads(task["body"])
            if record.get("card_type") == "implementation":
                admissions = []
                for comment in envelope.get("comments", []):
                    if not isinstance(comment, dict):
                        continue
                    text = comment.get("text", comment.get("body", comment.get("content")))
                    if not isinstance(text, str):
                        continue
                    try:
                        payload = json.loads(text)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(payload, dict) and payload.get("schema") == "backend-implementation-admission-v2":
                        admissions.append(payload)
                if not admissions or validate_implementation_admission(admissions[-1]):
                    raise RuntimeError(f"implementation admission comment read-back failed: {key}")
        return graph

    def execute(self) -> dict[str, Any]:
        fixture = json.loads((SKILL_DIR / "tests" / "fixtures" / "valid-graph-draft.json").read_text(encoding="utf-8"))
        triage_body = self.triage_body(fixture)
        bind_frozen_triage_digest(fixture, triage_body)
        self.workspace = fixture["workspace"]
        self.issue = fixture["issue"]["number"]
        cards = {card["key"]: card for card in fixture["cards"]}
        board_created = False
        try:
            self.run("boards", "create", self.board, "--name", "build-task-graph native E2E")
            board_created = True
            triage = self.create(
                cards["triage"]["title"],
                cards["triage"]["assignee"],
                triage_body,
                priority=cards["triage"]["priority"],
                triage=True,
            )
            impl = self.create(cards["impl-1"]["title"], cards["impl-1"]["assignee"], cards["impl-1"]["body"], priority=cards["impl-1"]["priority"], parents=[triage])
            review1 = self.create(cards["review-1"]["title"], cards["review-1"]["assignee"], cards["review-1"]["body"], priority=cards["review-1"]["priority"], parents=[impl])
            summary = self.create(cards["summary"]["title"], cards["summary"]["assignee"], cards["summary"]["body"], priority=cards["summary"]["priority"], parents=[impl, review1])
            ids = {"triage": triage, "impl-1": impl, "review-1": review1, "summary": summary}

            draft_errors = validate_graph(self.graph_wrapper(fixture, ids), phase="draft", validate_implementation=validate)
            if draft_errors:
                raise RuntimeError(f"draft graph invalid: {draft_errors}")
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                graph_path = root / "graph.json"
                readback_path = root / "native-readback.json"
                graph_path.write_text(json.dumps(fixture, ensure_ascii=False), encoding="utf-8")
                readback_path.write_text(json.dumps([self.show(task_id) for task_id in ids.values()], ensure_ascii=False), encoding="utf-8")
                activation = activate(
                    graph_path,
                    readback_path,
                    triage,
                    [],
                    "impl-1",
                    "hermes",
                    self.board,
                    apply=True,
                )
            if not activation.get("valid"):
                raise RuntimeError(f"activation gate failed: {activation}")
            if self.status(self.show(triage)) != "done" or self.status(self.show(impl)) != "ready":
                raise RuntimeError("activation did not complete Triage and expose exactly one ready implementation")
            native_errors = validate_graph(self.graph_wrapper(fixture, ids), phase="native", validate_implementation=validate)
            if native_errors:
                raise RuntimeError(f"native graph invalid: {native_errors}")

            self.board_run("claim", impl, "--ttl", "120")
            self.board_run("complete", "--force", impl, "--result", "implementation checkpoint verified")
            self.board_run("claim", review1, "--ttl", "120")
            active_review_run = self.show(review1)["runs"][-1]
            review_run_id = active_review_run.get("id", active_review_run.get("run_id"))
            if not isinstance(review_run_id, int) or review_run_id <= 0:
                raise RuntimeError("native Review run has no positive integer ID")
            finding_metadata = {
                "schema": "aggregate-review-result-v2",
                "review_card_key": "review-1",
                "review_task_id": review1,
                "review_run_id": review_run_id,
                "generation": 1,
                "baseline_sha": fixture["planning_baseline_sha"],
                "reviewed_head_sha": "c" * 40,
                "result": "changes-required",
                "reviewed_checkpoints": [
                    {
                        "implementation_card_key": "impl-1",
                        "implementation_task_id": impl,
                        "checkpoint_sha": "a" * 40,
                    }
                ],
                "prior_findings": [],
                "findings": [
                    {
                        "finding_id": "F-1",
                        "verdict": "correction-required",
                        "basis": "behavior-product-create",
                        "observed_fact": "회귀 시나리오 보강이 필요하다.",
                        "evidence": ["test:missing-regression"],
                        "impact": "aggregate acceptance",
                        "resolves": None,
                        "continues": None,
                    }
                ]
            }
            self.board_run(
                "complete",
                "--force",
                review1,
                "--result",
                "correction required",
                "--metadata",
                json.dumps(finding_metadata, ensure_ascii=False),
            )
            review_envelope = self.show(review1)
            latest_metadata = review_envelope["runs"][-1]["metadata"]
            if latest_metadata != finding_metadata:
                raise RuntimeError("review finding metadata read-back mismatch")
            if not any(event.get("kind") == "completed" for event in review_envelope["events"]):
                raise RuntimeError("review completion event was not read back")

            corrective_body = json.loads(json.dumps(cards["impl-1"]["body"]))
            corrective_body["slice"]["id"] = "slice-product-create-regression"
            corrective_body["slice"]["title"] = "상품 등록 회귀 보강"
            corrective_body["slice"]["outcome"] = "기존 상품 등록 동작의 회귀 시나리오를 보강한다."
            corrective_body["acceptance"][0]["id"] = "AC-PRODUCT-CREATE-2"
            corrective_body["verification"][0]["id"] = "FV-PRODUCT-CREATE-2"
            corrective_body["verification"][0]["acceptance_ids"] = ["AC-PRODUCT-CREATE-2"]
            corrective = self.create(
                "G1-Issue138-Impl2",
                "coder",
                corrective_body,
                priority=2,
                parents=[review1],
                idempotency_key="review-1:F-1:correction",
            )
            recovered = self.create(
                "G1-Issue138-Impl2",
                "coder",
                corrective_body,
                priority=2,
                parents=[review1],
                idempotency_key="review-1:F-1:correction",
            )
            if recovered != corrective:
                raise RuntimeError("idempotent recovery created a duplicate card")
            review2_body = json.loads(json.dumps(cards["review-1"]["body"]))
            review2_body["implementation_card_keys"] = ["impl-1", "impl-2"]
            for acceptance in review2_body["acceptance"]:
                acceptance["implementation_card_keys"] = ["impl-1", "impl-2"]
                acceptance["source_acceptance_ids"] = ["AC-PRODUCT-CREATE", "AC-PRODUCT-CREATE-2"]
                acceptance["verification_ids"] = ["FV-PRODUCT-CREATE", "FV-PRODUCT-CREATE-2"]
            for check in review2_body["review_checks"]:
                check["implementation_card_keys"] = ["impl-1", "impl-2"]
                check["source_acceptance_ids"] = ["AC-PRODUCT-CREATE", "AC-PRODUCT-CREATE-2"]
                check["verification_ids"] = ["FV-PRODUCT-CREATE", "FV-PRODUCT-CREATE-2"]
            seam_check = json.loads(json.dumps(review2_body["review_checks"][0]))
            seam_check.update(
                {
                    "id": "REVIEW-CHECK-SEAM",
                    "focus": "cross-card-seam",
                    "question": "Initial slice와 corrective slice의 연결 계약과 책임 경계가 일치하는가?",
                    "expected_outcome": "Corrective slice가 기존 public seam을 보존하고 정책을 중복 구현하지 않는다.",
                }
            )
            review2_body["review_checks"].append(seam_check)
            review2 = self.create(
                "G1-Issue138-Review2",
                "reviewer",
                review2_body,
                priority=1,
                parents=[corrective],
                idempotency_key="review-1:F-1:review",
            )
            self.board_run("link", corrective, summary)
            self.board_run("link", review2, summary)
            if self.status(self.show(summary)) != "todo":
                raise RuntimeError("Summary remained dispatchable after corrective parents were linked")

            review_graph = json.loads(json.dumps(fixture))
            review_graph["mode"] = "review-rework"
            review_graph["lineage"] = {
                "prior_generation": 1,
                "prior_summary": summary,
                "archived_unfinished_keys": [],
            }
            review_graph["source_review"] = {
                "review_key": "review-1",
                "dispositions": [
                    {
                        "finding_id": "F-1",
                        "verdict": "correction-required",
                        "appended_card_keys": ["impl-2", "review-2"],
                    }
                ],
                "idempotency": {
                    "impl-2": "review-1:F-1:correction",
                    "review-2": "review-1:F-1:review",
                },
            }
            review_graph["behaviors"][0]["implementation_card_key"] = "impl-2"
            review_graph["execution_order"] = ["impl-2", "review-2"]
            review_graph["ready_candidate"] = "impl-2"
            summary_card = next(card for card in review_graph["cards"] if card["key"] == "summary")
            summary_card["parents"] = ["impl-1", "review-1", "impl-2", "review-2"]
            review_graph["cards"].insert(
                -1,
                {
                    "key": "impl-2",
                    "title": "G1-Issue138-Impl2",
                    "card_type": "implementation",
                    "assignee": "coder",
                    "skills": [],
                    "workspace": "scratch",
                    "status": "ready",
                    "priority": 2,
                    "parents": ["review-1"],
                    "body": corrective_body,
                },
            )
            review_graph["cards"].insert(
                -1,
                {
                    "key": "review-2",
                    "title": "G1-Issue138-Review2",
                    "card_type": "review",
                    "assignee": "reviewer",
                    "skills": [],
                    "workspace": "scratch",
                    "status": "todo",
                    "priority": 1,
                    "parents": ["impl-2"],
                    "body": review2_body,
                },
            )
            rework_ids = {**ids, "impl-2": corrective, "review-2": review2}
            rework_errors = validate_graph(
                self.graph_wrapper(review_graph, rework_ids),
                phase="native",
                validate_implementation=validate,
                source_review_result=finding_metadata,
            )
            if rework_errors:
                raise RuntimeError(f"review-rework graph invalid: {rework_errors}")

            self.board_run("claim", corrective, "--ttl", "120")
            self.board_run("complete", "--force", corrective, "--result", "correction verified")
            self.board_run("claim", review2, "--ttl", "120")
            active_review2_run = self.show(review2)["runs"][-1]
            review2_run_id = active_review2_run.get("id", active_review2_run.get("run_id"))
            if not isinstance(review2_run_id, int) or review2_run_id <= 0:
                raise RuntimeError("native Review2 run has no positive integer ID")
            resolution_metadata = {
                "schema": "aggregate-review-result-v2",
                "review_card_key": "review-2",
                "review_task_id": review2,
                "review_run_id": review2_run_id,
                "generation": 1,
                "baseline_sha": fixture["planning_baseline_sha"],
                "reviewed_head_sha": "d" * 40,
                "result": "approved",
                "reviewed_checkpoints": [
                    {
                        "implementation_card_key": "impl-1",
                        "implementation_task_id": impl,
                        "checkpoint_sha": "a" * 40,
                    },
                    {
                        "implementation_card_key": "impl-2",
                        "implementation_task_id": corrective,
                        "checkpoint_sha": "b" * 40,
                    },
                ],
                "prior_findings": [
                    {
                        "source_review_task_id": review1,
                        "source_finding_id": "F-1",
                        "verdict": "correction-required",
                    }
                ],
                "findings": [
                    {
                        "finding_id": "F-2",
                        "basis": "Review2 재검사",
                        "observed_fact": "F-1의 corrective implementation이 확인되었다.",
                        "evidence": ["native corrective run completion"],
                        "impact": "F-1은 더 이상 blocking이 아니다.",
                        "verdict": "resolved",
                        "resolves": {
                            "source_review_task_id": review1,
                            "source_finding_id": "F-1",
                        },
                        "continues": None,
                    }
                ]
            }
            self.board_run(
                "complete",
                "--force",
                review2,
                "--result",
                "all findings resolved",
                "--metadata",
                json.dumps(resolution_metadata, ensure_ascii=False),
            )
            if self.show(review2)["runs"][-1]["metadata"] != resolution_metadata:
                raise RuntimeError("review finding resolution provenance read-back mismatch")
            if self.status(self.show(summary)) != "ready":
                raise RuntimeError("Summary was not promoted after every direct parent completed")

            obsolete = self.create("G1-Issue138-Impl99", "coder", corrective_body, priority=1)
            self.board_run("claim", obsolete, "--ttl", "120")
            self.board_run("block", obsolete, "requirement superseded", "--kind", "dependency")
            blocked = self.show(obsolete)
            last_run = blocked["runs"][-1]
            if self.status(blocked) not in {"todo", "blocked"} or last_run["ended_at"] is None or last_run["outcome"] != "blocked":
                raise RuntimeError("running worker termination was not read back before archive")
            self.board_run("archive", obsolete)
            if self.status(self.show(obsolete)) != "archived":
                raise RuntimeError("obsolete card archive was not read back")

            return {
                "draft_validation": "pass",
                "native_validation": "pass",
                "implementation_admission_comment_readback": "pass",
                "triage_activation_gate": "pass",
                "review_rework_native_validation": "pass",
                "review_completion_event_and_finding_readback": "pass",
                "idempotent_recovery": "pass",
                "summary_admission_frontier": "pass",
                "running_worker_termination_and_archive": "pass",
            }
        finally:
            try:
                shutil.rmtree(self.body_dir, ignore_errors=True)
                if board_created:
                    cleanup_error: RuntimeError | None = None
                    for _attempt in range(20):
                        try:
                            self.run("boards", "rm", "--delete", self.board)
                            cleanup_error = None
                            break
                        except RuntimeError as exc:
                            cleanup_error = exc
                            if "WinError 32" not in str(exc) and "PermissionError" not in str(exc):
                                raise
                            time.sleep(0.25)
                    if cleanup_error is not None:
                        raise cleanup_error
                    boards = self.run("boards", "list", "--json", "--all", json_output=True)
                    slugs = {board.get("slug") for board in boards if isinstance(board, dict)}
                    if self.board in slugs:
                        raise RuntimeError("E2E board cleanup read-back failed")
            finally:
                if self.previous_hermes_home is None:
                    os.environ.pop("HERMES_HOME", None)
                else:
                    os.environ["HERMES_HOME"] = self.previous_hermes_home
                shutil.rmtree(self.hermes_home, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, default=Path.cwd())
    args = parser.parse_args()
    try:
        result = NativeE2E(args.repository.resolve()).execute()
    except (OSError, RuntimeError, TypeError, KeyError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
        print(json.dumps({"valid": False, "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps({"valid": True, "checks": result}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
