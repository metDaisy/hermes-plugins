"""Profile-independent Coder and Reviewer workflow-role tests."""

from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.support import load_module


CONTROLLER = load_module("controller")


class QueueProducer:
    def __init__(self, outputs: dict[str, dict], *, workspace: Path, attempt_mutation: bool = False) -> None:
        self.outputs = outputs
        self.workspace = workspace.resolve()
        self.attempt_mutation = attempt_mutation
        self.mutation_blocked = False
        self.calls = []

    def invoke(self, invocation):
        self.calls.append(invocation)
        if self.attempt_mutation:
            try:
                invocation.input["verification_contract"]["focused"][0][
                    "verification_id"
                ] = "FORGED"
            except TypeError:
                self.mutation_blocked = True
        return CONTROLLER.AgentResult(
            agent_id=f"role:{invocation.step_id}",
            output=self.outputs[invocation.step_id],
        )


class ControllerDeliveryRoleTests(unittest.TestCase):
    def test_topology_and_minimum_role_projections(self) -> None:
        workflow = CONTROLLER.delivery_roles_workflow()
        initial = self._initial(Path("C:/workspace"))
        initial.update(
            {
                "profile": "coder",
                "soul": "must-not-project",
                "memory": {"private": True},
                "kanban": {"task_id": "t_forbidden"},
                "raw_triage": {"schema": "triage-v7"},
                "prior_conversation": ["secret"],
            }
        )
        coder_artifact = {
            "artifact_id": "delivery:coder_execute:1",
            "payload": self._coder_result(),
        }

        self.assertEqual(
            ["coder_execute", "reviewer_review"],
            [step.step_id for step in workflow.steps],
        )
        self.assertEqual(
            [("coder_execute", "reviewer_review")],
            [
                (transition.source_step_id, transition.target_step_id)
                for transition in workflow.transitions
            ],
        )
        self.assertEqual((), workflow.steps[1].allowed_toolsets)
        coder_projection = workflow.steps[0].projector(initial, {})
        reviewer_projection = workflow.steps[1].projector(
            initial,
            {"coder_execute": coder_artifact},
        )

        self.assertEqual(
            {"implementation_card", "evidence", "workspace", "verification_contract"},
            set(coder_projection),
        )
        self.assertEqual(
            {"accepted_plan", "diff", "verification_evidence", "rubric"},
            set(reviewer_projection),
        )
        self.assertEqual(initial["implementation_card"], reviewer_projection["accepted_plan"])
        self.assertEqual(coder_artifact["payload"]["diff"], reviewer_projection["diff"])
        self.assertEqual(
            coder_artifact["payload"]["verification_evidence"],
            reviewer_projection["verification_evidence"],
        )
        serialized = json.dumps(
            {"coder": coder_projection, "reviewer": reviewer_projection},
            ensure_ascii=False,
        )
        for forbidden in ("must-not-project", "t_forbidden", "raw_triage", "prior_conversation"):
            self.assertNotIn(forbidden, serialized)

    def test_coder_admission_is_pinned_and_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            reviewer_workspace = workspace / "reviewer"
            reviewer_workspace.mkdir()
            initial = self._initial(workspace)
            coder_producer = QueueProducer(
                {"coder_execute": self._coder_result()},
                workspace=workspace,
            )
            reviewer_producer = QueueProducer({}, workspace=reviewer_workspace)
            executor = CONTROLLER.DeliveryRoleExecutor(
                coder_producer=coder_producer,
                reviewer_producer=reviewer_producer,
                git_head_fn=lambda _workspace: "b" * 40,
                verification_readback_fn=lambda _workspace, _contract: {},
            )
            runtime = CONTROLLER.ControllerRuntime.open(
                workflow=CONTROLLER.delivery_roles_workflow(),
                checkpoint_path=workspace / "controller.db",
                artifact_store_path=workspace / "artifacts.db",
                executor_registry=CONTROLLER.ExecutorRegistry({"agent": executor}),
            )
            try:
                result = runtime.start(run_id="stale-coder", initial_input=initial)
            finally:
                runtime.close()

            self.assertEqual("blocked", result["status"])
            self.assertEqual([], coder_producer.calls)
            self.assertEqual([], reviewer_producer.calls)

    def test_coder_rejects_workspace_below_repository_root(self) -> None:
        with (
            tempfile.TemporaryDirectory() as repository_directory,
            tempfile.TemporaryDirectory() as controller_directory,
        ):
            repository = Path(repository_directory).resolve()
            controller = Path(controller_directory).resolve()
            nested_workspace = repository / "nested"
            reviewer_workspace = controller / "reviewer"
            nested_workspace.mkdir()
            reviewer_workspace.mkdir()
            subprocess.run(
                ["git", "init", "-q", str(repository)],
                check=True,
                capture_output=True,
            )
            coder_producer = QueueProducer(
                {"coder_execute": self._coder_result()},
                workspace=nested_workspace,
            )
            executor = CONTROLLER.DeliveryRoleExecutor(
                coder_producer=coder_producer,
                reviewer_producer=QueueProducer({}, workspace=reviewer_workspace),
                git_head_fn=lambda _workspace: "a" * 40,
                verification_readback_fn=lambda _workspace, _contract: {},
            )
            runtime = CONTROLLER.ControllerRuntime.open(
                workflow=CONTROLLER.delivery_roles_workflow(),
                checkpoint_path=controller / "controller.db",
                artifact_store_path=controller / "artifacts.db",
                executor_registry=CONTROLLER.ExecutorRegistry({"agent": executor}),
            )
            try:
                result = runtime.start(
                    run_id="nested-coder-workspace",
                    initial_input=self._initial(nested_workspace),
                )
            finally:
                runtime.close()

            self.assertEqual("blocked", result["status"])
            self.assertEqual([], coder_producer.calls)

    def test_typed_role_results_and_restart_resume(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            reviewer_workspace = workspace / "reviewer"
            reviewer_workspace.mkdir()
            initial = self._initial(workspace)
            coder_result = self._coder_result()
            review_result = self._review_result(coder_result)
            coder_producer = QueueProducer(
                {"coder_execute": coder_result},
                workspace=workspace,
                attempt_mutation=True,
            )
            reviewer_producer = QueueProducer(
                {"reviewer_review": review_result},
                workspace=reviewer_workspace,
            )
            verification_state = {"read_back": False}

            def verification_readback(_workspace, _contract):
                verification_state["read_back"] = True
                return coder_result["verification_evidence"]

            def final_diff_snapshot(_workspace, _baseline):
                self.assertTrue(verification_state["read_back"])
                return coder_result["diff"]

            executor = CONTROLLER.DeliveryRoleExecutor(
                coder_producer=coder_producer,
                reviewer_producer=reviewer_producer,
                git_head_fn=lambda _workspace: "a" * 40,
                diff_snapshot_fn=final_diff_snapshot,
                verification_readback_fn=verification_readback,
            )
            registry = CONTROLLER.ExecutorRegistry({"agent": executor})
            checkpoint = workspace / "controller.db"
            artifact_store = workspace / "artifacts.db"
            runtime = CONTROLLER.ControllerRuntime.open(
                workflow=CONTROLLER.delivery_roles_workflow(),
                checkpoint_path=checkpoint,
                artifact_store_path=artifact_store,
                executor_registry=registry,
            )
            try:
                first = runtime.start(run_id="delivery-restart", initial_input=initial)
            finally:
                runtime.close()

            runtime = CONTROLLER.ControllerRuntime.open(
                workflow=CONTROLLER.delivery_roles_workflow(),
                checkpoint_path=checkpoint,
                artifact_store_path=artifact_store,
                executor_registry=registry,
            )
            try:
                resumed = runtime.resume(run_id="delivery-restart")
            finally:
                runtime.close()

            self.assertEqual("awaiting_resume", first["status"], first)
            self.assertEqual("completed", resumed["status"])
            self.assertEqual(
                ["coder_execute", "reviewer_review"],
                [coder_producer.calls[0].step_id, reviewer_producer.calls[0].step_id],
            )
            self.assertTrue(coder_producer.mutation_blocked)
            coder_call = coder_producer.calls[0]
            reviewer_call = reviewer_producer.calls[0]
            self.assertTrue(coder_call.goal.startswith("CONTROLLER_CODER_ROLE_V1\n"))
            self.assertTrue(reviewer_call.goal.startswith("CONTROLLER_REVIEWER_ROLE_V1\n"))
            self.assertNotIn("terminal", coder_call.allowed_toolsets)
            self.assertNotIn("terminal", reviewer_call.allowed_toolsets)
            self.assertEqual((), reviewer_call.allowed_toolsets)
            self.assertEqual(
                {"implementation_card", "evidence", "workspace", "verification_contract"},
                set(coder_call.input),
            )
            self.assertEqual(
                {"accepted_plan", "diff", "verification_evidence", "rubric"},
                set(reviewer_call.input),
            )
            self.assertNotIn("workspace", reviewer_call.input)
            self.assertIn("diff --git", reviewer_call.input["diff"]["unified_diff"])
            self.assertEqual(
                {
                    "schema",
                    "status",
                    "diff",
                    "verification_evidence",
                    "documentation_impact",
                    "residual_risks",
                },
                set(coder_call.output_schema["properties"]),
            )
            self.assertFalse(coder_call.output_schema["properties"]["diff"]["additionalProperties"])
            self.assertFalse(reviewer_call.output_schema["properties"]["findings"]["items"]["additionalProperties"])
            self.assertEqual(review_result, json.loads(json.dumps(resumed["artifacts"][-1]["payload"])))

    def test_coder_requires_matching_workspace_and_controller_verification_readback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory).resolve()
            mismatch_workspace = root / "mismatch"
            reviewer_workspace = root / "reviewer"
            mismatch_workspace.mkdir()
            reviewer_workspace.mkdir()
            coder_result = self._coder_result()
            repository_reviewer = QueueProducer(
                {},
                workspace=Path(__file__).resolve().parents[1],
            )
            with self.assertRaisesRegex(ValueError, "REVIEWER_WORKSPACE_MUST_BE_ISOLATED"):
                CONTROLLER.DeliveryRoleExecutor(
                    coder_producer=QueueProducer(
                        {"coder_execute": coder_result},
                        workspace=root,
                    ),
                    reviewer_producer=repository_reviewer,
                    git_head_fn=lambda _workspace: "a" * 40,
                    verification_readback_fn=lambda _workspace, _contract: coder_result[
                        "verification_evidence"
                    ],
                )
            mismatch_producer = QueueProducer(
                {"coder_execute": coder_result},
                workspace=mismatch_workspace,
            )
            reviewer_producer = QueueProducer({}, workspace=reviewer_workspace)
            mismatch_executor = CONTROLLER.DeliveryRoleExecutor(
                coder_producer=mismatch_producer,
                reviewer_producer=reviewer_producer,
                git_head_fn=lambda _workspace: "a" * 40,
                diff_snapshot_fn=lambda _workspace, _baseline: coder_result["diff"],
                verification_readback_fn=lambda _workspace, _contract: coder_result[
                    "verification_evidence"
                ],
            )
            mismatch_runtime = CONTROLLER.ControllerRuntime.open(
                workflow=CONTROLLER.delivery_roles_workflow(),
                checkpoint_path=root / "mismatch-controller.db",
                artifact_store_path=root / "mismatch-artifacts.db",
                executor_registry=CONTROLLER.ExecutorRegistry({"agent": mismatch_executor}),
            )
            try:
                mismatch = mismatch_runtime.start(
                    run_id="workspace-mismatch",
                    initial_input=self._initial(root),
                )
            finally:
                mismatch_runtime.close()

            self.assertEqual("blocked", mismatch["status"])
            self.assertEqual([], mismatch_producer.calls)

            forged_producer = QueueProducer(
                {"coder_execute": coder_result},
                workspace=root,
            )
            failed_readback = json.loads(json.dumps(coder_result["verification_evidence"]))
            failed_readback["full_backend"]["task"] = "assemble"
            forged_executor = CONTROLLER.DeliveryRoleExecutor(
                coder_producer=forged_producer,
                reviewer_producer=reviewer_producer,
                git_head_fn=lambda _workspace: "a" * 40,
                diff_snapshot_fn=lambda _workspace, _baseline: coder_result["diff"],
                verification_readback_fn=lambda _workspace, _contract: failed_readback,
            )
            forged_runtime = CONTROLLER.ControllerRuntime.open(
                workflow=CONTROLLER.delivery_roles_workflow(),
                checkpoint_path=root / "forged-controller.db",
                artifact_store_path=root / "forged-artifacts.db",
                executor_registry=CONTROLLER.ExecutorRegistry({"agent": forged_executor}),
            )
            try:
                forged = forged_runtime.start(
                    run_id="forged-verification",
                    initial_input=self._initial(root),
                )
            finally:
                forged_runtime.close()

            self.assertEqual("blocked", forged["status"])
            self.assertEqual(1, len(forged_producer.calls))
            self.assertEqual([], forged["artifacts"])

            oversized_result = json.loads(json.dumps(coder_result))
            oversized_result["diff"]["unified_diff"] += "x" * 40_000
            oversized_result["diff"]["sha256"] = hashlib.sha256(
                oversized_result["diff"]["unified_diff"].encode("utf-8")
            ).hexdigest()
            oversized_producer = QueueProducer(
                {"coder_execute": oversized_result},
                workspace=root,
            )
            oversized_executor = CONTROLLER.DeliveryRoleExecutor(
                coder_producer=oversized_producer,
                reviewer_producer=reviewer_producer,
                git_head_fn=lambda _workspace: "a" * 40,
                diff_snapshot_fn=lambda _workspace, _baseline: oversized_result["diff"],
                verification_readback_fn=lambda _workspace, _contract: oversized_result[
                    "verification_evidence"
                ],
            )
            oversized_runtime = CONTROLLER.ControllerRuntime.open(
                workflow=CONTROLLER.delivery_roles_workflow(),
                checkpoint_path=root / "oversized-controller.db",
                artifact_store_path=root / "oversized-artifacts.db",
                executor_registry=CONTROLLER.ExecutorRegistry({"agent": oversized_executor}),
            )
            try:
                oversized = oversized_runtime.start(
                    run_id="oversized-reviewer-envelope",
                    initial_input=self._initial(root),
                )
            finally:
                oversized_runtime.close()

            self.assertEqual("blocked", oversized["status"])
            self.assertEqual([], oversized["artifacts"])

    def test_incoherent_role_outputs_are_not_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            workflow = CONTROLLER.delivery_roles_workflow()
            initial = self._initial(root)
            coder_result = self._coder_result()
            bad_review = self._review_result(coder_result)
            admitted_review = {
                "accepted_plan": initial["implementation_card"],
                "diff": coder_result["diff"],
                "verification_evidence": coder_result["verification_evidence"],
                "rubric": initial["rubric"],
            }
            delivery_module = load_module("controller.delivery_roles")
            self.assertTrue(
                delivery_module._review_result_valid(
                    bad_review,
                    admitted=admitted_review,
                )
            )
            for digest_field in ("accepted_plan_sha256", "diff_sha256"):
                mismatched = json.loads(json.dumps(bad_review))
                mismatched[digest_field] = "2" * 64
                self.assertFalse(
                    delivery_module._review_result_valid(
                        mismatched,
                        admitted=admitted_review,
                    )
                )
            valid_finding = {
                "finding_id": "F-001",
                "axis": "spec",
                "disposition": "correction-required",
                "path": "src/main/java/com/example/orders/application/OrderService.java",
                "line": 1,
                "observed": "The changed line violates the accepted plan.",
                "impact": "The requested behavior remains incorrect.",
            }
            changes_required = json.loads(json.dumps(bad_review))
            changes_required["result"] = "changes-required"
            changes_required["findings"] = [valid_finding]
            self.assertTrue(
                delivery_module._review_result_valid(
                    changes_required,
                    admitted=admitted_review,
                )
            )
            for invalid_finding in (
                {**valid_finding, "path": "not-in-diff.txt"},
                {**valid_finding, "line": 999999},
            ):
                invalid_review = json.loads(json.dumps(changes_required))
                invalid_review["findings"] = [invalid_finding]
                self.assertFalse(
                    delivery_module._review_result_valid(
                        invalid_review,
                        admitted=admitted_review,
                    )
                )
            duplicate_review = json.loads(json.dumps(changes_required))
            duplicate_review["findings"] = [valid_finding, valid_finding]
            self.assertFalse(
                delivery_module._review_result_valid(
                    duplicate_review,
                    admitted=admitted_review,
                )
            )
            admitted_coder, _workspace, _baseline = delivery_module._admit_coder(
                {
                    key: initial[key]
                    for key in (
                        "implementation_card",
                        "evidence",
                        "workspace",
                        "verification_contract",
                    )
                },
                git_head_fn=lambda _workspace: "a" * 40,
            )
            duplicate_evidence = json.loads(
                json.dumps(coder_result["verification_evidence"])
            )
            duplicate_check = json.loads(json.dumps(duplicate_evidence["checks"][0]))
            duplicate_check["selectors"] = ["undeclared.selector"]
            duplicate_evidence["checks"].insert(0, duplicate_check)
            duplicate_result = json.loads(json.dumps(coder_result))
            duplicate_result["verification_evidence"] = duplicate_evidence
            self.assertFalse(
                delivery_module._coder_result_valid(
                    duplicate_result,
                    admitted=admitted_coder,
                    snapshot=coder_result["diff"],
                    verification_readback=duplicate_evidence,
                )
            )
            poisoned_diff = {
                "changed_paths": ["src/example.txt"],
                "unified_diff": (
                    "diff --git a/src/example.txt b/src/example.txt\n"
                    "--- a/src/example.txt\n"
                    "+++ b/src/example.txt\n"
                    "@@ -1 +1,2 @@\n"
                    "-old\n"
                    "+new\n"
                    "+++ content that resembles a file header\n"
                    "@@ -10 +10 @@\n"
                    "-before\n"
                    "+after\n"
                ),
            }
            self.assertTrue(
                delivery_module._review_findings_valid(
                    [{"path": "src/example.txt", "line": 10}],
                    poisoned_diff,
                )
            )
            deleted_diff = {
                "changed_paths": ["src/deleted.txt"],
                "unified_diff": (
                    "diff --git a/src/deleted.txt b/src/deleted.txt\n"
                    "--- a/src/deleted.txt\n"
                    "+++ /dev/null\n"
                    "@@ -3,2 +0,0 @@\n"
                    "-old one\n"
                    "-old two\n"
                ),
            }
            self.assertTrue(
                delivery_module._review_findings_valid(
                    [{"path": "src/deleted.txt", "line": 3}],
                    deleted_diff,
                )
            )
            rejected_digest = "1" * 64
            bad_review["verification_evidence_sha256"] = rejected_digest
            reviewer_workspace = root / "reviewer"
            reviewer_workspace.mkdir()
            coder_producer = QueueProducer(
                {"coder_execute": coder_result},
                workspace=root,
            )
            reviewer_producer = QueueProducer(
                {"reviewer_review": bad_review},
                workspace=reviewer_workspace,
            )
            executor = CONTROLLER.DeliveryRoleExecutor(
                coder_producer=coder_producer,
                reviewer_producer=reviewer_producer,
                git_head_fn=lambda _workspace: "a" * 40,
                diff_snapshot_fn=lambda _workspace, _baseline: coder_result["diff"],
                verification_readback_fn=lambda _workspace, _contract: coder_result[
                    "verification_evidence"
                ],
            )
            checkpoint_path = root / "controller.sqlite3"
            artifact_path = root / "artifacts.sqlite3"
            registry = CONTROLLER.ExecutorRegistry({"agent": executor})
            runtime = CONTROLLER.ControllerRuntime.open(
                workflow=workflow,
                checkpoint_path=checkpoint_path,
                artifact_store_path=artifact_path,
                executor_registry=registry,
            )
            try:
                first = runtime.start(run_id="bad-review", initial_input=initial)
                result = runtime.resume(run_id="bad-review")
            finally:
                runtime.close()

            self.assertEqual("awaiting_resume", first["status"])
            self.assertEqual("blocked", result["status"])
            self.assertEqual(["coder_execute"], [artifact["step_id"] for artifact in result["artifacts"]])
            self.assertEqual(["coder_execute"], [call.step_id for call in coder_producer.calls])
            self.assertEqual(["reviewer_review"], [call.step_id for call in reviewer_producer.calls])
            self.assertNotIn(rejected_digest, checkpoint_path.read_bytes().decode("latin-1"))

    def test_default_diff_snapshot_includes_tracked_and_untracked_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace = Path(temporary_directory)
            self._git(workspace, "init")
            self._git(workspace, "config", "user.email", "controller@example.invalid")
            self._git(workspace, "config", "user.name", "Controller Test")
            tracked = workspace / "tracked.txt"
            tracked.write_text("before\n", encoding="utf-8")
            self._git(workspace, "add", "tracked.txt")
            self._git(workspace, "commit", "-m", "baseline")
            baseline = self._git(workspace, "rev-parse", "HEAD").stdout.strip().lower()
            tracked.write_text("after\n", encoding="utf-8")
            (workspace / "new.txt").write_text("new\n", encoding="utf-8")

            snapshot = load_module("controller.delivery_roles")._git_diff_snapshot(
                workspace,
                baseline,
            )

            self.assertEqual(["new.txt", "tracked.txt"], snapshot["changed_paths"])
            self.assertIn("tracked.txt", snapshot["unified_diff"])
            self.assertIn("new.txt", snapshot["unified_diff"])
            self.assertNotIn(str(workspace), snapshot["unified_diff"])
            self.assertNotIn(workspace.as_posix(), snapshot["unified_diff"])
            self.assertEqual(
                hashlib.sha256(snapshot["unified_diff"].encode("utf-8")).hexdigest(),
                snapshot["sha256"],
            )

    @staticmethod
    def _git(workspace: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", str(workspace), *arguments],
            check=True,
            capture_output=True,
            text=True,
        )

    @staticmethod
    def _card() -> dict:
        return {
            "schema": "backend-implementation-card-v6",
            "trace": {
                "triage_digest": "f" * 64,
                "slice_id": "SLICE-001",
                "requirement_ids": ["CHANGE-001"],
                "observable_ids": ["OBS-001"],
                "gap_ids": ["GAP-001"],
            },
            "objective": {"outcome": "Cart 변경을 aggregate behavior로 위임한다."},
            "boundary": {
                "in_scope": ["Cart service와 직접 검증 테스트"],
                "out_of_scope": ["공개 API 변경"],
            },
            "obligations": [
                {
                    "id": "OB-CART-DELEGATE",
                    "kind": "behavior",
                    "given": ["수량 변경 요청이 존재한다."],
                    "operation": {
                        "symbol": "com.example.orders.application.OrderService#changeQuantity",
                        "invocation_count": 1,
                    },
                    "expected": ["Cart aggregate behavior를 한 번 호출한다."],
                    "observables": [
                        {
                            "target": "Cart quantity transition",
                            "source": "in-memory-state",
                            "comparison": "exact",
                            "identity": ["CartItem.id"],
                            "values": ["quantity"],
                            "expected": "requested quantity",
                        }
                    ],
                }
            ],
            "constraints": [
                {
                    "id": "CONSTRAINT-CART-OWNER",
                    "rule": "Cart aggregate가 item 상태 전이를 소유한다.",
                    "source": {
                        "locator": "docs/development-guide.md#Domain 구현 규칙",
                        "claim": "Aggregate owns state transitions.",
                    },
                }
            ],
            "verification": [
                {
                    "id": "VERIFY-CART-DELEGATE",
                    "covers": ["OB-CART-DELEGATE", "CONSTRAINT-CART-OWNER"],
                    "level": "unit",
                    "selectors": [
                        "com.example.orders.application.OrderServiceTest#changeQuantityDelegatesToCart"
                    ],
                }
            ],
        }

    def _initial(self, workspace: Path) -> dict:
        workspace = workspace.resolve()
        selector = self._card()["verification"][0]["selectors"][0]
        return {
            "implementation_card": self._card(),
            "evidence": {
                "schema": "controller-evidence-bundle-v1",
                "workspace_path": workspace.as_posix(),
                "git_head": "a" * 40,
                "codebase_memory": [],
                "semble": [],
                "direct_source": [
                    {
                        "locator": "src/main/java/com/example/orders/application/OrderService.java#changeQuantity",
                        "claim": "current implementation seam",
                    }
                ],
            },
            "workspace": {
                "schema": "controller-workspace-v1",
                "path": workspace.as_posix(),
                "baseline_head_sha": "a" * 40,
            },
            "verification_contract": {
                "schema": "controller-verification-contract-v1",
                "runner": "gradle-mcp",
                "focused": [
                    {
                        "verification_id": "VERIFY-CART-DELEGATE",
                        "selectors": [selector],
                        "level": "unit",
                    }
                ],
                "full_backend": {"task": "test"},
                "quality_tasks": ["checkstyleMain", "checkstyleTest"],
            },
            "rubric": {
                "schema": "controller-review-rubric-v1",
                "axes": [
                    "spec",
                    "maintainability",
                    "persistence",
                    "architecture",
                    "evolution-compatibility",
                    "security",
                ],
                "finding_dispositions": [
                    "correction-required",
                    "context-required",
                    "decision-required",
                ],
            },
        }

    @staticmethod
    def _coder_result() -> dict:
        unified_diff = (
            "diff --git a/src/main/java/com/example/orders/application/OrderService.java "
            "b/src/main/java/com/example/orders/application/OrderService.java\n"
            "--- a/src/main/java/com/example/orders/application/OrderService.java\n"
            "+++ b/src/main/java/com/example/orders/application/OrderService.java\n"
            "@@ -1 +1 @@\n-old\n+new\n"
        )
        return {
            "schema": "controller-coder-result-v1",
            "status": "completed",
            "diff": {
                "baseline_head_sha": "a" * 40,
                "result_head_sha": "a" * 40,
                "changed_paths": [
                    "src/main/java/com/example/orders/application/OrderService.java",
                    "src/test/java/com/example/orders/application/OrderServiceTest.java",
                ],
                "unified_diff": unified_diff,
                "sha256": hashlib.sha256(unified_diff.encode("utf-8")).hexdigest(),
            },
            "verification_evidence": {
                "checks": [
                    {
                        "verification_id": "VERIFY-CART-DELEGATE",
                        "runner": "gradle-mcp",
                        "selectors": [
                            "com.example.orders.application.OrderServiceTest#changeQuantityDelegatesToCart"
                        ],
                        "level": "unit",
                        "result": "passed",
                    }
                ],
                "full_backend": {
                    "runner": "gradle-mcp",
                    "task": "test",
                    "result": "passed",
                },
                "quality": [
                    {"runner": "gradle-mcp", "task": "checkstyleMain", "result": "passed"},
                    {"runner": "gradle-mcp", "task": "checkstyleTest", "result": "passed"},
                ],
            },
            "documentation_impact": [],
            "residual_risks": [],
        }

    def _review_result(self, coder_result: dict) -> dict:
        accepted_plan_digest = self._digest(self._card())
        diff_digest = coder_result["diff"]["sha256"]
        verification_digest = hashlib.sha256(
            json.dumps(
                coder_result["verification_evidence"],
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        return {
            "schema": "controller-review-result-v1",
            "result": "approved",
            "baseline_head_sha": coder_result["diff"]["baseline_head_sha"],
            "reviewed_head_sha": coder_result["diff"]["result_head_sha"],
            "accepted_plan_sha256": accepted_plan_digest,
            "diff_sha256": diff_digest,
            "verification_evidence_sha256": verification_digest,
            "rubric_axes": list(self._initial(Path("C:/workspace"))["rubric"]["axes"]),
            "findings": [],
            "unverified": [],
        }

    @staticmethod
    def _digest(value: dict) -> str:
        return hashlib.sha256(
            json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()


if __name__ == "__main__":
    unittest.main()
