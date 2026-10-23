import copy
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time as wall_time
import unittest
from pathlib import Path
from unittest.mock import patch

SKILL_DIR = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = SKILL_DIR.parents[1]
CREATE_TRIAGE_DIR = SKILL_DIR.parents[0] / "kwc-create-triage"
CREATE_TRIAGE_SCRIPTS = CREATE_TRIAGE_DIR / "scripts"
CREATE_TRIAGE_TESTS = CREATE_TRIAGE_DIR / "tests"
sys.path.insert(0, str(SKILL_DIR / "scripts"))
sys.path.insert(0, str(SKILL_DIR / "tests"))
sys.path.insert(0, str(CREATE_TRIAGE_SCRIPTS))
sys.path.insert(0, str(CREATE_TRIAGE_TESTS))
sys.path.insert(0, str(PLUGIN_ROOT / "src"))
import build_task_graph as task_graph  # noqa: E402
import review_fast_path as review_fast  # noqa: E402
import triage as triage_contract  # noqa: E402
import test_triage as triage_test_fixtures  # noqa: E402
from build_task_graph import (  # noqa: E402
    _implementation_acceptance_ids,
    _implementation_slice_id,
    apply_impl_completions,
    create_impl_batch_native,
    create_review_native,
    impl_batch_template,
    prepare_impl_fast_path,
    prepare_review_fast_path,
    render_impl_batch,
    template,
    validate,
    validate_impl_batch,
    validate_review_fast_path,
    write_json,
)
from graph_contract import (  # noqa: E402
    REVIEW_GUIDE_LOCATORS,
    requirement_diff,
    validate_graph,
    validate_native_readback,
    validate_review_graph,
)
from native_e2e import bind_frozen_triage_digest  # noqa: E402
from hermes_langchain.authoring.graph.implementation_batch import (  # noqa: E402
    apply_impl_completions as core_apply_impl_completions,
    impl_batch_template as legacy_impl_batch_template,
    validate_impl_batch as core_validate_impl_batch,
)
from hermes_langchain.authoring.graph.legacy_implementation_card import (  # noqa: E402
    validate as core_validate_card,
)


class BackendImplementationCardContractTest(unittest.TestCase):
    fixture_path = SKILL_DIR / "tests" / "fixtures" / "valid-new-delivery.json"

    @staticmethod
    def valid_frozen_triage():
        source = json.loads(
            (PLUGIN_ROOT / "tests" / "fixtures" / "valid-triage-authoring.json").read_text(
                encoding="utf-8"
            )
        )
        identity = source["identity"]
        body = triage_contract.template(
            identity["issue_number"],
            identity["title"],
            identity["issue_url"],
            identity["planning_sha"],
            identity["current_state_sha"],
            identity["requirement_locators"],
            identity["current_state_locators"],
        )
        intake = triage_contract.compose_intake(body, source["intake"])
        return triage_contract.freeze(
            triage_contract.compose_analysis(intake, source["analysis"])
        )

    def valid_legacy_wrapper(
        self,
        body=None,
        *,
        ordinal=1,
        priority=1,
        issue=138,
        generation=1,
        parent_ids=None,
    ):
        body = copy.deepcopy(body or self.valid_card())
        acceptance_ids = [
            item["id"]
            for item in body.get("acceptance", [])
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        ]
        acceptance_id = acceptance_ids[0]
        for entry_point in body.get("context", {}).get("entry_points", []):
            reason = re.sub(
                r"^(?:\[[A-Za-z0-9-]+\])+\s*",
                "",
                str(entry_point.get("reason") or "구현 진입점을 명시한다."),
            )
            entry_point["reason"] = f"[{acceptance_id}][modify] {reason}"
        verification = [
            item
            for item in body.get("verification", [])
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        ]
        wrapper = {
            "ordinal": ordinal,
            "title": f"G{generation}-Issue{issue}-Impl{ordinal}",
            "priority": priority,
            "body": body,
            "contract_changes": [],
            "required_entry_points": [],
            "required_contract_symbols": [],
            "required_verification_ids": [item["id"] for item in verification],
            "required_verification_scenarios": {
                item["id"]: list(item.get("scenarios", [])) for item in verification
            },
            "depends_on_ordinals": [],
        }
        if parent_ids is not None:
            wrapper["parent_ids"] = list(parent_ids)
        return wrapper

    def test_skill_uses_validator_as_black_box(self):
        skill = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("kwc-orchestrator-contract/references/script-usage.md", skill)
        self.assertIn("template → 입력 작성 → validate", skill)

    def test_native_v6_closure_uses_trace_and_authored_contract_ids(self):
        body = {
            "schema": "backend-implementation-card-v6",
            "trace": {"slice_id": "S-1"},
            "obligations": [],
            "constraints": [{"id": "S1-A1"}, {"id": "G-001"}],
        }

        self.assertEqual("S-1", _implementation_slice_id(body))
        self.assertEqual(
            ["S1-A1"],
            _implementation_acceptance_ids(body, ["S1-A1"]),
        )

    def test_script_review_prepare_accepts_v6_implementation_body(self):
        workspace = Path.cwd().resolve()
        triage = self.valid_frozen_triage()
        issue = triage["issue"]["number"]
        implementation = impl_batch_template(triage, triage_id="t_triage")["cards"][0]["body"]
        obligation = implementation["obligations"][0]
        obligation["given"] = ["수량을 변경할 주문이 존재한다."]
        obligation["operation"]["invocation_count"] = 1
        obligation["observables"] = [
            {
                "target": "Order items",
                "source": "in-memory-state",
                "comparison": "unchanged",
                "identity": ["OrderItem.id"],
                "values": ["quantity"],
                "expected": "zero quantity item is removed",
            }
        ]
        implementation["verification"][0]["level"] = "unit"
        tasks = [
            {
                "id": "t_triage",
                "title": f"G1-Issue{issue}-Triage",
                "body": json.dumps(triage, ensure_ascii=False),
                "assignee": "project-manager",
                "status": "triage",
                "priority": 0,
                "workspace_kind": "dir",
                "workspace_path": workspace.as_posix(),
            },
            {
                "id": "t_impl",
                "title": f"G1-Issue{issue}-Impl1",
                "body": json.dumps(implementation, ensure_ascii=False),
                "assignee": "coder",
                "status": "todo",
                "priority": 1,
                "workspace_kind": "dir",
                "workspace_path": workspace.as_posix(),
            },
        ]

        bundle = prepare_review_fast_path(
            tasks,
            triage_id="t_triage",
            implementation_ids=["t_impl"],
            issue=issue,
            generation=1,
        )

        self.assertEqual(["impl-1"], bundle["review_body_template"]["implementation_card_keys"])

    def test_skill_requires_proof_for_each_impl_dependency_edge(self):
        skill = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
        contract = (SKILL_DIR / "references" / "implementation-card-contract.md").read_text(encoding="utf-8")

        self.assertIn("dependency-audit.json", skill)
        self.assertIn("why_baseline_cannot_satisfy", skill)
        self.assertIn("공유 파일·충돌 회피·선호 실행 순서", skill)
        self.assertIn("`dependency_audit.edges[]`", contract)
        self.assertIn("`required_artifact`", contract)
        self.assertIn("`source {locator, claim}`", contract)

    def test_skill_requires_exact_artifact_closure_for_each_impl_acceptance(self):
        skill = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
        contract = (SKILL_DIR / "references" / "implementation-card-contract.md").read_text(
            encoding="utf-8"
        )

        self.assertIn("acceptance별 obligation과 constraint closure", skill)
        self.assertIn("exact repository path", skill)
        self.assertIn("application command/result", skill)
        self.assertIn("Planned artifact target", contract)

    def test_skill_has_bounded_impl_body_only_fast_path(self):
        skill = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")

        self.assertIn("Impl body-only fast path", skill)
        self.assertIn("Frozen Triage를 source-closure snapshot", skill)
        self.assertIn("과거 Impl card와 archived", skill)
        self.assertIn("card body를 읽지 않는다", skill)
        self.assertIn("MCP_LOOKUP_REQUIRED", skill)
        self.assertIn("impl-card-batch.json", skill)
        self.assertIn("한 번의 batched write", skill)
        self.assertIn("compact stdout", skill)
        self.assertIn("terminal output log를 재파싱하지 않는다", skill)
        self.assertIn("relative `.temp/` path", skill)
        self.assertIn("stale batch", skill)
        self.assertIn("`--workspace dir:<canonical-path>`", skill)
        self.assertIn("`--status`를 전달하지 않는다", skill)
        self.assertIn("G{generation}-Issue{issue}-Impl{ordinal}", skill)
        self.assertIn("validate-impl-batch", skill)
        self.assertIn("impl-batch-template", skill)
        self.assertIn("prepare-impl-fast-path", skill)

        self.assertIn("source-triage.json이나 triage-body.json을 별도로 읽지 않는다", skill)
        self.assertIn("`impl-batch-template`을 별도로 실행하지 않는다", skill)
        self.assertIn("required_contract_symbols", skill)
        self.assertIn("render-impl-batch", skill)
        self.assertIn("`create-impl-batch-native`를 정확히 한 번 실행", skill)
        self.assertIn("중간 실패 시 이번 호출에서 생성한 card를 archive", skill)
        self.assertIn("validated wrapper body와 exact하게 일치", skill)
        self.assertIn("cleanup을 검증", skill)
        self.assertIn("malformed/ambiguous", skill)
        self.assertIn("contract change를 immutable `constraints[]`", skill)
        self.assertIn("--body-dir <artifact-root>/bodies", skill)
        self.assertIn("`session_search`·`--help`를 호출하지 않는다", skill)
        self.assertIn("apply-impl-completions", skill)
        self.assertIn("custom Python fill script를 만들지 않는다", skill)
        self.assertIn("Compact completion의 closed shape", skill)
        self.assertIn("installed Skill directory, references, tests", skill)
        self.assertIn("acceptance별 obligation과 constraint closure", skill)
        self.assertIn("planned test method name은 authoring output", skill)
        self.assertIn("existing test method 존재를 확인하려고", skill)
        self.assertIn("최대 4개 file", skill)
        self.assertIn("validate-impl-batch <artifact-root>/impl-card-batch.json", skill)
        self.assertIn("generation-<generation>/<board-namespace>", skill)
        self.assertIn("top-level `parents`", skill)
        self.assertIn("`task` 내부의 `parent_ids`나 `parents`를 찾지 않는다", skill)
        self.assertIn("source Triage의 native `workspace_path`", skill)
        self.assertIn("전체 Triage validator와 digest", skill)
        self.assertIn("각각 한 번만 읽고", skill)
        self.assertIn("Prefill constraint locator나 exact symbol", skill)
        self.assertIn("required_contract_symbols", skill)
        self.assertIn("closed wrapper shape", skill)
        self.assertIn("모든 helper terminal call의 `workdir`", skill)
        self.assertIn("다른 repository root로 fallback하지 않는다", skill)
        self.assertIn("Codebase Memory의 `project`는 repository에 등록된 project name", skill)
        self.assertIn("workspace absolute path를 `project`로 전달하지 않는다", skill)

    def test_profile_has_bounded_summary_regeneration_skill(self):
        summary_skill_path = SKILL_DIR.parent / "kwc-regenerate-summary" / "SKILL.md"
        self.assertTrue(summary_skill_path.exists())
        summary_skill = summary_skill_path.read_text(encoding="utf-8")
        self.assertIn("regenerate-summary-native", summary_skill)
        self.assertIn("별도 board list", summary_skill)
        self.assertLess(len(summary_skill), 4000)

    def test_common_contract_routes_impl_authoring_without_pm_soul(self):
        contract = (SKILL_DIR.parent / "kwc-orchestrator-contract" / "SKILL.md").read_text(
            encoding="utf-8"
        )

        self.assertIn("Impl·Review graph authoring: `kwc-build-task-graph`", contract)
        self.assertIn("상황별 `kwc-*` Skill만 실행 절차로 사용", contract)
        self.assertIn("checkpoint, promotion, finding routing, finalization: `kwc-run-workflow`", contract)

    def test_impl_fast_path_batch_requires_canonical_numbered_titles(self):
        batch = {
            "issue": 138,
            "generation": 1,
            "cards": [
                self.valid_legacy_wrapper(ordinal=1, priority=2),
                self.valid_legacy_wrapper(ordinal=2, priority=1),
            ],
            "dependency_audit": {"edges": []},
        }

        self.assertEqual([], validate_impl_batch(batch))

        batch["cards"][0]["title"] = "G1-Issue138-Impl-SLICE-001"
        self.assertIn("INVALID_IMPL_TITLE:0", validate_impl_batch(batch))

        batch["cards"][0]["title"] = "G1-Issue138-Impl2"
        self.assertIn("IMPL_TITLE_ORDINAL_MISMATCH:0:2:1", validate_impl_batch(batch))

        batch["cards"][0]["title"] = "G1-Issue138-Impl1"
        batch["cards"][0]["ordinal"] = 2
        self.assertIn("IMPL_WRAPPER_ORDINAL_MISMATCH:0:2:1", validate_impl_batch(batch))
        with self.assertRaisesRegex(ValueError, "IMPL_WRAPPER_ORDINAL_MISMATCH"):
            render_impl_batch(batch)

        batch["cards"][0]["ordinal"] = 1
        batch["cards"][0]["parent_ids"] = ["t_first"]
        batch["cards"][1]["parent_ids"] = ["t_second"]
        self.assertIn("IMPL_PARENT_SET_MISMATCH", validate_impl_batch(batch))

    def test_render_impl_batch_returns_all_validated_body_files(self):
        batch = {
            "issue": 138,
            "generation": 1,
            "cards": [self.valid_legacy_wrapper(ordinal=1, priority=1)],
            "dependency_audit": {"edges": []},
        }

        rendered = render_impl_batch(batch)

        self.assertEqual(["impl-1.json"], list(rendered))
        self.assertEqual("backend-implementation-card-v5", rendered["impl-1.json"]["schema"])

    def test_create_impl_batch_native_uses_bounded_calls_and_rolls_back_failure(self):
        source_triage = self.valid_frozen_triage()
        issue = source_triage["issue"]["number"]
        body = self.valid_card()
        body["slice"]["id"] = source_triage["implementation_slices"][0]["id"]
        source_acceptance = source_triage["implementation_slices"][0]["acceptance"][0]
        body["acceptance"][0]["id"] = source_acceptance["id"]
        body["verification"][0]["id"] = source_acceptance["verification_ids"][0]
        body["verification"][0]["acceptance_ids"] = [source_acceptance["id"]]
        body["issue"] = {
            "number": issue,
            "url": source_triage["issue"]["url"],
        }
        body["triage_digest"] = source_triage["frozen_digest"]
        batch = {
            "issue": issue,
            "generation": 1,
            "cards": [self.valid_legacy_wrapper(
                body,
                ordinal=1,
                priority=1,
                issue=issue,
                parent_ids=["t_triage"],
            )],
            "dependency_audit": {"edges": []},
        }
        body = batch["cards"][0]["body"]
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory).resolve()
            body_dir = workspace / ".temp/task-graphs/151/bodies"
            body_dir.mkdir(parents=True)
            expected_body = json.dumps(body, ensure_ascii=False, indent=2) + "\n"
            (body_dir / "impl-1.json").write_text(expected_body, encoding="utf-8")
            triage_envelope = {
                "task": {
                    "id": "t_triage",
                    "status": "triage",
                    "workspace_kind": "dir",
                    "workspace_path": workspace.as_posix(),
                    "body": json.dumps(source_triage, ensure_ascii=False),
                }
            }
            triage = subprocess.CompletedProcess(
                [], 0, stdout=json.dumps(triage_envelope), stderr=""
            )
            empty_list = subprocess.CompletedProcess([], 0, stdout="[]", stderr="")
            create = subprocess.CompletedProcess(
                [], 0, stdout=json.dumps({"id": "t_new", "title": f"G1-Issue{issue}-Impl1"}), stderr=""
            )
            show = subprocess.CompletedProcess(
                [],
                0,
                stdout=json.dumps(
                    {
                        "task": {
                            "id": "t_new",
                            "title": f"G1-Issue{issue}-Impl1",
                            "body": expected_body,
                            "assignee": "coder",
                            "skills": [],
                            "status": "todo",
                            "priority": 1,
                            "workspace_kind": "dir",
                            "workspace_path": workspace.as_posix(),
                        },
                        "parents": ["t_triage"],
                    }
                ),
                stderr="",
            )
            with patch.object(task_graph.Path, "cwd", return_value=workspace), patch.object(
                task_graph.subprocess,
                "run",
                side_effect=[triage, empty_list, create, show, triage],
            ) as run:
                report = create_impl_batch_native(
                    batch,
                    profile="project-manager",
                    board="board",
                    triage_id="t_triage",
                    body_dir=body_dir,
                    workspace_path=workspace,
                )

            self.assertTrue(report["valid"])
            self.assertEqual(5, run.call_count)

            bad_show = copy.deepcopy(show)
            bad_envelope = json.loads(bad_show.stdout)
            bad_envelope["task"]["assignee"] = "project-manager"
            bad_show.stdout = json.dumps(bad_envelope)
            archived = subprocess.CompletedProcess([], 0, stdout="", stderr="")
            with patch.object(task_graph.Path, "cwd", return_value=workspace), patch.object(
                task_graph.subprocess,
                "run",
                side_effect=[triage, empty_list, create, bad_show, archived, empty_list],
            ) as run:
                with self.assertRaisesRegex(ValueError, "NATIVE_READBACK_MISMATCH"):
                    create_impl_batch_native(
                        batch,
                        profile="project-manager",
                        board="board",
                        triage_id="t_triage",
                        body_dir=body_dir,
                        workspace_path=workspace,
                    )
            self.assertEqual("archive", run.call_args_list[-2].args[0][-2])

            malformed_create = subprocess.CompletedProcess([], 0, stdout="{}", stderr="")
            recovered_list = subprocess.CompletedProcess(
                [],
                0,
                stdout=json.dumps([{"id": "t_new", "title": f"G1-Issue{issue}-Impl1"}]),
                stderr="",
            )
            with patch.object(task_graph.Path, "cwd", return_value=workspace), patch.object(
                task_graph.subprocess,
                "run",
                side_effect=[
                    triage,
                    empty_list,
                    malformed_create,
                    recovered_list,
                    archived,
                    empty_list,
                ],
            ) as run:
                with self.assertRaisesRegex(ValueError, "CREATE_ID_NOT_UNIQUE"):
                    create_impl_batch_native(
                        batch,
                        profile="project-manager",
                        board="board",
                        triage_id="t_triage",
                        body_dir=body_dir,
                        workspace_path=workspace,
                    )
            self.assertEqual("archive", run.call_args_list[-2].args[0][-2])

            stale_body = copy.deepcopy(body)
            stale_body["slice"]["outcome"] = "검증되지 않은 body file"
            (body_dir / "impl-1.json").write_text(
                json.dumps(stale_body, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            with patch.object(task_graph.Path, "cwd", return_value=workspace), patch.object(
                task_graph.subprocess, "run"
            ) as run:
                with self.assertRaisesRegex(ValueError, "NATIVE_BODY_FILE_MISMATCH"):
                    create_impl_batch_native(
                        batch,
                        profile="project-manager",
                        board="board",
                        triage_id="t_triage",
                        body_dir=body_dir,
                        workspace_path=workspace,
                    )
            run.assert_not_called()

            (body_dir / "impl-1.json").write_text(expected_body, encoding="utf-8")
            wrong_parent = copy.deepcopy(batch)
            wrong_parent["cards"][0]["parent_ids"] = ["t_other"]
            with patch.object(task_graph.Path, "cwd", return_value=workspace), patch.object(
                task_graph.subprocess, "run"
            ) as run:
                with self.assertRaisesRegex(ValueError, "IMPL_PARENT_TRIAGE_MISMATCH"):
                    create_impl_batch_native(
                        wrong_parent,
                        profile="project-manager",
                        board="board",
                        triage_id="t_triage",
                        body_dir=body_dir,
                        workspace_path=workspace,
                    )
            run.assert_not_called()

    def test_create_impl_batch_native_rejects_incomplete_triage_slice_closure(self):
        source_triage = self.valid_frozen_triage()
        issue = source_triage["issue"]["number"]
        source_slice_id = source_triage["implementation_slices"][0]["id"]
        body = self.valid_card()
        body["issue"] = {
            "number": issue,
            "url": source_triage["issue"]["url"],
        }
        body["triage_digest"] = source_triage["frozen_digest"]
        body["slice"]["id"] = "SLICE-NOT-FROM-TRIAGE"
        batch = {
            "issue": issue,
            "generation": 1,
            "cards": [self.valid_legacy_wrapper(
                body,
                ordinal=1,
                priority=1,
                issue=issue,
                parent_ids=["t_triage"],
            )],
            "dependency_audit": {"edges": []},
        }
        body = batch["cards"][0]["body"]

        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory).resolve()
            body_dir = workspace / ".temp/task-graphs/151/bodies"
            body_dir.mkdir(parents=True)
            (body_dir / "impl-1.json").write_text(
                json.dumps(body, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            triage_envelope = {
                "task": {
                    "id": "t_triage",
                    "status": "triage",
                    "workspace_kind": "dir",
                    "workspace_path": workspace.as_posix(),
                    "body": json.dumps(source_triage, ensure_ascii=False),
                }
            }
            triage = subprocess.CompletedProcess(
                [], 0, stdout=json.dumps(triage_envelope), stderr=""
            )

            with patch.object(task_graph.Path, "cwd", return_value=workspace), patch.object(
                task_graph.subprocess,
                "run",
                side_effect=[triage],
            ) as run:
                with self.assertRaisesRegex(
                    ValueError,
                    f"IMPL_SLICE_CLOSURE_MISMATCH:missing={source_slice_id}:"
                    "unexpected=SLICE-NOT-FROM-TRIAGE",
                ):
                    create_impl_batch_native(
                        batch,
                        profile="project-manager",
                        board="board",
                        triage_id="t_triage",
                        body_dir=body_dir,
                        workspace_path=workspace,
                    )
            self.assertEqual(1, run.call_count)

    def test_create_impl_batch_native_rejects_incomplete_triage_verification_closure(self):
        source_triage = self.valid_frozen_triage()
        issue = source_triage["issue"]["number"]
        source_slice = source_triage["implementation_slices"][0]
        source_acceptance = source_slice["acceptance"][0]
        body = self.valid_card()
        body["slice"]["id"] = source_slice["id"]
        body["acceptance"][0]["id"] = source_acceptance["id"]
        body["verification"][0]["id"] = "VERIFY-NOT-FROM-TRIAGE"
        body["verification"][0]["acceptance_ids"] = [source_acceptance["id"]]
        body["issue"] = {
            "number": issue,
            "url": source_triage["issue"]["url"],
        }
        body["triage_digest"] = source_triage["frozen_digest"]
        batch = {
            "issue": issue,
            "generation": 1,
            "cards": [self.valid_legacy_wrapper(
                body,
                ordinal=1,
                priority=1,
                issue=issue,
                parent_ids=["t_triage"],
            )],
            "dependency_audit": {"edges": []},
        }
        body = batch["cards"][0]["body"]

        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory).resolve()
            body_dir = workspace / ".temp/task-graphs/151/bodies"
            body_dir.mkdir(parents=True)
            (body_dir / "impl-1.json").write_text(
                json.dumps(body, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            triage_envelope = {
                "task": {
                    "id": "t_triage",
                    "status": "triage",
                    "workspace_kind": "dir",
                    "workspace_path": workspace.as_posix(),
                    "body": json.dumps(source_triage, ensure_ascii=False),
                }
            }
            triage = subprocess.CompletedProcess(
                [], 0, stdout=json.dumps(triage_envelope), stderr=""
            )

            with patch.object(task_graph.Path, "cwd", return_value=workspace), patch.object(
                task_graph.subprocess,
                "run",
                side_effect=[triage],
            ) as run:
                with self.assertRaisesRegex(
                    ValueError,
                    "IMPL_VERIFICATION_CLOSURE_MISMATCH:SLICE-001:"
                    "missing=VERIFY-001:unexpected=VERIFY-NOT-FROM-TRIAGE",
                ):
                    create_impl_batch_native(
                        batch,
                        profile="project-manager",
                        board="board",
                        triage_id="t_triage",
                        body_dir=body_dir,
                        workspace_path=workspace,
                    )
            self.assertEqual(1, run.call_count)

    def test_impl_batch_template_prefills_frozen_triage_facts_and_wrapper(self):
        triage = {
            "schema": "triage-v7",
            "phase": "frozen",
            "frozen_digest": "d" * 64,
            "issue": {"number": 151, "url": "https://github.com/example/repo/issues/151"},
            "confirmed_gaps": [
                {
                    "id": "GAP-001",
                    "current_state": "현재 구현 사실",
                    "evidence": [
                        {
                            "locator": "src/main/java/example/Cart.java#Cart#addItem",
                            "claim": "현재 구현 근거",
                        }
                    ],
                    "rules": [
                        {
                            "locator": "docs/backend-development-guide.md#application 구현 규칙",
                            "claim": "구조 제약",
                        }
                    ],
                }
            ],
            "implementation_slices": [
                {
                    "id": "SLICE-001",
                    "title": "장바구니 경계 정리",
                    "outcome": "장바구니 경계를 정리한다.",
                    "gap_ids": ["GAP-001"],
                    "scope": ["장바구니 경계를 정리한다."],
                    "out_of_scope": ["공개 API는 변경하지 않는다."],
                    "acceptance": [
                        {
                            "id": "ACCEPT-001",
                            "criterion": "경계가 분리된다.",
                            "requirement_ids": ["CHANGE-001"],
                            "observable_ids": ["OBS-001"],
                            "verification_ids": ["VERIFY-001"],
                        }
                    ],
                    "contract_change_ids": ["CONTRACT-001"],
                    "depends_on": [],
                }
            ],
            "verification_matrix": [
                {
                    "id": "VERIFY-001",
                    "production_seam": {
                        "locator": "src/main/java/example/Cart.java#addItem",
                        "claim": "[ACCEPT-001][modify] 생산 진입점",
                    },
                    "required_verification": ["장바구니 경계 분리를 검증한다."],
                }
            ],
            "contract_changes": [
                {
                    "id": "CONTRACT-001",
                    "change": "OfferQueryApi에 requireCurrentOffer(UUID)를 추가한다.",
                    "verification_ids": ["VERIFY-001"],
                }
            ],
        }

        batch = legacy_impl_batch_template(triage)

        self.assertEqual({"edges": []}, batch["dependency_audit"])
        self.assertEqual("G1-Issue151-Impl1", batch["cards"][0]["title"])
        self.assertEqual(["OBS-001"], batch["cards"][0]["body"]["slice"]["behavior_ids"])
        self.assertEqual(
            {
                "id": "ACCEPT-001",
                "criterion": "경계가 분리된다.",
                "requirement_ids": ["CHANGE-001"],
                "observable_ids": ["OBS-001"],
            },
            batch["cards"][0]["body"]["acceptance"][0],
        )
        self.assertEqual(
            "Cart#addItem",
            batch["cards"][0]["body"]["context"]["entry_points"][0]["symbol"],
        )
        self.assertEqual("", batch["cards"][0]["body"]["verification"][0]["test_level"])
        self.assertEqual([], batch["cards"][0]["body"]["verification"][0]["tests"])
        self.assertEqual(["VERIFY-001"], batch["cards"][0]["required_verification_ids"])
        incomplete_verification = copy.deepcopy(batch)
        incomplete_verification["cards"][0]["body"]["verification"] = []
        self.assertIn(
            "IMPL_REQUIRED_VERIFICATION_IDS_MISMATCH:0:missing=VERIFY-001:"
            "unexpected=-:duplicates=-",
            validate_impl_batch(incomplete_verification),
        )
        self.assertEqual(
            ["requireCurrentOffer(UUID)"], batch["cards"][0]["required_contract_symbols"]
        )
        self.assertEqual("CONTRACT-001", batch["cards"][0]["contract_changes"][0]["id"])
        self.assertIn(
            "REQUIRED_CONTRACT_SYMBOL_MISSING:0:requireCurrentOffer(UUID)",
            validate_impl_batch(batch),
        )
        batch["cards"][0]["body"]["verification"][0]["scenarios"].append(
            "requireCurrentOffer(UUID)를 검증한다."
        )
        self.assertIn(
            "REQUIRED_CONTRACT_SYMBOL_MISSING:0:requireCurrentOffer(UUID)",
            validate_impl_batch(batch),
        )
        batch["cards"][0]["body"]["implementation_contract"]["api"]["rules"] = [
            "OfferQueryApi에 requireCurrentOffer(UUID)를 추가한다."
        ]
        self.assertNotIn(
            "REQUIRED_CONTRACT_SYMBOL_MISSING:0:requireCurrentOffer(UUID)",
            validate_impl_batch(batch),
        )

    def test_apply_impl_completions_merges_only_authored_fields(self):
        triage = {
            "schema": "triage-v7",
            "phase": "frozen",
            "frozen_digest": "d" * 64,
            "issue": {"number": 151, "url": "https://github.com/example/repo/issues/151"},
            "confirmed_gaps": [{
                "id": "GAP-001",
                "evidence": [{
                    "locator": "src/main/java/example/Cart.java#Cart#addItem",
                    "claim": "현재 Cart 동작이 구현되어 있다.",
                }],
                "rules": [{
                    "locator": "docs/backend-development-guide.md#application 구현 규칙",
                    "claim": "application 경계를 분리한다.",
                }],
            }],
            "implementation_slices": [
                {
                    "id": "SLICE-001",
                    "title": "경계 정리",
                    "outcome": "경계를 정리한다.",
                    "gap_ids": ["GAP-001"],
                    "scope": ["경계 정리"],
                    "out_of_scope": ["API 변경 없음"],
                    "acceptance": [
                        {
                            "id": "ACCEPT-001",
                            "criterion": "경계가 분리된다.",
                            "requirement_ids": ["CHANGE-001"],
                            "observable_ids": ["OBS-001"],
                            "verification_ids": ["VERIFY-001"],
                        }
                    ],
                }
            ],
            "verification_matrix": [
                {
                    "id": "VERIFY-001",
                    "production_seam": {
                        "locator": "src/main/java/example/Cart.java#Cart#addItem",
                        "claim": "[ACCEPT-001][modify] 생산 진입점",
                    },
                    "required_verification": ["경계 분리를 검증한다."],
                }
            ],
        }
        batch = legacy_impl_batch_template(triage)
        contract = self.valid_card()["implementation_contract"]
        completions = {
            "cards": [
                {
                    "ordinal": 1,
                    "implementation_contract": contract,
                    "entry_points": [
                        {
                            "path": "src/main/java/example/NewCommand.java",
                            "symbol": "NewCommand",
                            "reason": "[ACCEPT-001][create] 새 command를 생성한다.",
                        }
                    ],
                    "verification": [
                        {
                            "id": "VERIFY-001",
                            "test_level": "unit",
                            "tests": ["example.CartTest#separatesBoundary"],
                            "scenarios": ["경계 분리를 검증한다."],
                        }
                    ],
                }
            ]
        }

        merged = apply_impl_completions(batch, completions)

        body = merged["cards"][0]["body"]
        self.assertEqual(contract, body["implementation_contract"])
        self.assertEqual("Cart#addItem", body["context"]["entry_points"][0]["symbol"])
        self.assertEqual(
            "[ACCEPT-001][modify] 생산 진입점",
            body["context"]["entry_points"][0]["reason"],
        )
        self.assertEqual("NewCommand", body["context"]["entry_points"][-1]["symbol"])
        self.assertEqual("unit", body["verification"][0]["test_level"])
        self.assertEqual(["example.CartTest#separatesBoundary"], body["verification"][0]["tests"])
        self.assertEqual(["OBS-001"], body["slice"]["behavior_ids"])

    def test_apply_impl_completions_deduplicates_prefilled_entry_points(self):
        seam = {
            "locator": "src/main/java/example/Cart.java#Cart#addItem",
            "claim": "[ACCEPT-001][modify] 생산 진입점",
        }
        triage = {
            "schema": "triage-v7",
            "phase": "frozen",
            "frozen_digest": "d" * 64,
            "issue": {"number": 151, "url": "https://github.com/example/repo/issues/151"},
            "confirmed_gaps": [{
                "id": "GAP-001",
                "evidence": [{
                    "locator": "src/main/java/example/Cart.java#Cart#addItem",
                    "claim": "현재 Cart 동작이 구현되어 있다.",
                }],
                "rules": [{
                    "locator": "docs/backend-development-guide.md#application 구현 규칙",
                    "claim": "application 경계를 분리한다.",
                }],
            }],
            "implementation_slices": [
                {
                    "id": "SLICE-001",
                    "title": "경계 정리",
                    "outcome": "경계를 정리한다.",
                    "gap_ids": ["GAP-001"],
                    "scope": ["경계 정리"],
                    "out_of_scope": ["API 변경 없음"],
                    "acceptance": [
                        {
                            "id": "ACCEPT-001",
                            "criterion": "경계가 분리된다.",
                            "requirement_ids": ["CHANGE-001"],
                            "observable_ids": ["OBS-001"],
                            "verification_ids": ["VERIFY-001", "VERIFY-002"],
                        }
                    ],
                }
            ],
            "verification_matrix": [
                {"id": "VERIFY-001", "production_seam": seam, "required_verification": ["첫 검증"]},
                {
                    "id": "VERIFY-002",
                    "production_seam": {
                        "locator": seam["locator"],
                        "claim": "같은 symbol을 설명하는 다른 검증 claim",
                    },
                    "required_verification": ["둘째 검증"],
                },
            ],
        }
        batch = legacy_impl_batch_template(triage)
        verification = [
            {
                "id": verification_id,
                "test_level": "unit",
                "tests": [f"example.CartTest#{verification_id.lower().replace('-', '')}"],
                "scenarios": ["경계 동작을 검증한다."],
            }
            for verification_id in ("VERIFY-001", "VERIFY-002")
        ]

        merged = apply_impl_completions(
            batch,
            {
                "cards": [
                    {
                        "ordinal": 1,
                        "implementation_contract": self.valid_card()["implementation_contract"],
                        "entry_points": [],
                        "verification": verification,
                    }
                ]
            },
        )

        self.assertEqual(1, len(merged["cards"][0]["body"]["context"]["entry_points"]))

    def test_apply_impl_completions_resolves_unknown_entry_point_by_path(self):
        body = self.valid_card()
        batch = {
            "issue": 138,
            "generation": 1,
            "cards": [self.valid_legacy_wrapper(
                body,
                ordinal=1,
                priority=1,
                parent_ids=["t_triage"],
            )],
            "dependency_audit": {"edges": []},
        }
        expected = batch["cards"][0]["body"]["context"]["entry_points"][0]
        expected["symbol"] = "unknown"
        authored_symbol = "Cart#changeQuantity"
        verification = [
            {
                "id": item["id"],
                "test_level": "unit",
                "tests": ["example.CartTest#changeQuantity"],
                "scenarios": list(item["scenarios"]),
            }
            for item in batch["cards"][0]["body"]["verification"]
        ]
        completions = {
            "cards": [
                {
                    "ordinal": 1,
                    "implementation_contract": self.valid_card()["implementation_contract"],
                    "entry_points": [
                        {
                            "path": expected["path"],
                            "symbol": authored_symbol,
                            "reason": "[AC-PRODUCT-CREATE][modify] 확인된 구현 진입점",
                        }
                    ],
                    "verification": verification,
                }
            ]
        }

        for merge in (apply_impl_completions, core_apply_impl_completions):
            with self.subTest(merge=merge.__module__):
                merged = merge(batch, completions)
                symbols = [
                    item["symbol"]
                    for item in merged["cards"][0]["body"]["context"]["entry_points"]
                ]
                self.assertEqual([authored_symbol], symbols)

    def test_apply_impl_completions_rejects_unresolved_unknown_entry_point(self):
        body = self.valid_card()
        batch = {
            "issue": 138,
            "generation": 1,
            "cards": [self.valid_legacy_wrapper(
                body,
                ordinal=1,
                priority=1,
                parent_ids=["t_triage"],
            )],
            "dependency_audit": {"edges": []},
        }
        batch["cards"][0]["body"]["context"]["entry_points"][0]["symbol"] = "unknown"
        verification = [
            {
                "id": item["id"],
                "test_level": "unit",
                "tests": ["example.CartTest#changeQuantity"],
                "scenarios": list(item["scenarios"]),
            }
            for item in batch["cards"][0]["body"]["verification"]
        ]
        completions = {
            "cards": [
                {
                    "ordinal": 1,
                    "implementation_contract": self.valid_card()["implementation_contract"],
                    "entry_points": [],
                    "verification": verification,
                }
            ]
        }

        for merge in (apply_impl_completions, core_apply_impl_completions):
            with self.subTest(merge=merge.__module__):
                with self.assertRaisesRegex(
                    ValueError, "UNRESOLVED_IMPL_ENTRY_POINT_SYMBOL:1"
                ):
                    merge(batch, completions)

    def test_impl_dependency_requires_semantic_proof_in_core_and_bundled_validator(self):
        first = self.valid_card()
        second = copy.deepcopy(first)
        second["slice"]["id"] = "slice-product-create-consumer"
        batch = {
            "issue": 138,
            "generation": 1,
            "cards": [
                {
                    "ordinal": 1,
                    "title": "G1-Issue138-Impl1",
                    "priority": 2,
                    "body": first,
                    "contract_changes": [],
                    "required_contract_symbols": [],
                    "parent_ids": ["t_triage"],
                    "depends_on_ordinals": [],
                },
                {
                    "ordinal": 2,
                    "title": "G1-Issue138-Impl2",
                    "priority": 1,
                    "body": second,
                    "contract_changes": [],
                    "required_contract_symbols": [],
                    "parent_ids": ["t_triage"],
                    "depends_on_ordinals": [1],
                },
            ],
            "dependency_audit": {
                "edges": [{"from_ordinal": 1, "to_ordinal": 2}]
            },
        }

        for validator in (validate_impl_batch, core_validate_impl_batch):
            with self.subTest(validator=validator.__module__):
                self.assertIn(
                    "IMPL_DEPENDENCY_PROOF_INCOMPLETE:1:2",
                    validator(batch),
                )

    def test_impl_dependency_accepts_complete_proof_in_core_and_bundled_validator(self):
        first = self.valid_card()
        second = copy.deepcopy(first)
        second["slice"]["id"] = "slice-product-create-consumer"
        for body in (first, second):
            body["context"]["entry_points"][0]["reason"] = (
                "[AC-PRODUCT-CREATE][create] 상품 등록 공개 경계를 소유한다."
            )
        batch = {
            "issue": 138,
            "generation": 1,
            "cards": [
                {
                    "ordinal": ordinal,
                    "title": f"G1-Issue138-Impl{ordinal}",
                    "priority": 3 - ordinal,
                    "body": body,
                    "contract_changes": [],
                    "required_contract_symbols": [],
                    "parent_ids": ["t_triage"],
                    "depends_on_ordinals": [] if ordinal == 1 else [1],
                }
                for ordinal, body in ((1, first), (2, second))
            ],
            "dependency_audit": {
                "edges": [
                    {
                        "from_ordinal": 1,
                        "to_ordinal": 2,
                        "required_artifact": "ProductController#create",
                        "consumer_entry_point": "ProductController#create",
                        "why_baseline_cannot_satisfy": (
                            "두 번째 slice는 첫 번째 slice가 생성하는 공개 경계를 직접 소비한다."
                        ),
                    }
                ]
            },
        }

        dependency_error_prefixes = (
            "IMPL_DEPENDENCY_",
            "INVALID_IMPL_DEPENDENCY",
        )
        for validator in (validate_impl_batch, core_validate_impl_batch):
            with self.subTest(validator=validator.__module__):
                counterfeit = copy.deepcopy(batch)
                counterfeit_edge = counterfeit["dependency_audit"]["edges"][0]
                counterfeit_edge["required_artifact"] = "AC-PRODUCT-CREATE"
                counterfeit_edge["consumer_entry_point"] = "AC-PRODUCT-CREATE"
                counterfeit_errors = validator(counterfeit)
                self.assertIn(
                    "IMPL_DEPENDENCY_ARTIFACT_UNPROVEN:1:2",
                    counterfeit_errors,
                )
                self.assertIn(
                    "IMPL_DEPENDENCY_CONSUMER_UNPROVEN:1:2",
                    counterfeit_errors,
                )
                errors = validator(batch)
                self.assertFalse(
                    [error for error in errors if error.startswith(dependency_error_prefixes)],
                    errors,
                )

    def test_prepare_impl_fast_path_validates_and_materializes_once(self):
        triage = self.valid_frozen_triage()
        issue = triage["issue"]["number"]
        workdir = Path.cwd().resolve()
        envelope = {
            "task": {
                "id": "t_triage",
                "title": f"G1-Issue{issue}-Triage",
                "status": "triage",
                "workspace_kind": "dir",
                "workspace_path": str(workdir),
                "body": json.dumps(triage, ensure_ascii=False),
            }
        }

        prepared = prepare_impl_fast_path(
            envelope,
            [envelope["task"]],
            triage_id="t_triage",
            issue=issue,
            generation=1,
            workdir=workdir,
        )

        self.assertEqual(triage, prepared["triage_body"])
        self.assertEqual(f"G1-Issue{issue}-Impl1", prepared["batch"]["cards"][0]["title"])
        self.assertEqual(
            {
                "triage_id": "t_triage",
                "status": "triage",
                "issue": issue,
                "generation": 1,
                "slice_count": 1,
                "active_impl_count": 0,
                "triage_digest": triage["frozen_digest"],
            },
            prepared["summary"],
        )

    def test_prepare_impl_fast_path_rejects_tampered_digest_and_wrong_workspace(self):
        triage = self.valid_frozen_triage()
        issue = triage["issue"]["number"]
        workdir = Path.cwd().resolve()
        envelope = {
            "task": {
                "id": "t_triage",
                "status": "triage",
                "workspace_kind": "dir",
                "workspace_path": str(workdir),
                "body": json.dumps(triage, ensure_ascii=False),
            }
        }
        tampered = copy.deepcopy(envelope)
        tampered_body = json.loads(tampered["task"]["body"])
        tampered_body["intent"]["goal"] = "digest 이후 변조된 목표"
        tampered["task"]["body"] = json.dumps(tampered_body, ensure_ascii=False)

        with self.assertRaisesRegex(ValueError, "FROZEN_BODY_DIGEST_MISMATCH"):
            prepare_impl_fast_path(
                tampered,
                [tampered["task"]],
                triage_id="t_triage",
                issue=issue,
                generation=1,
                workdir=workdir,
            )

        wrong_workspace = copy.deepcopy(envelope)
        wrong_workspace["task"]["workspace_path"] = str(workdir / "other")
        with self.assertRaisesRegex(ValueError, "SOURCE_TRIAGE_WORKSPACE_MISMATCH"):
            prepare_impl_fast_path(
                wrong_workspace,
                [wrong_workspace["task"]],
                triage_id="t_triage",
                issue=issue,
                generation=1,
                workdir=workdir,
            )

    def test_prepare_impl_fast_path_cli_reads_only_show_and_list_once(self):
        triage = self.valid_frozen_triage()
        issue = triage["issue"]["number"]
        with tempfile.TemporaryDirectory() as directory:
            workdir = Path(directory).resolve()
            envelope = {
                "task": {
                    "id": "t_triage",
                    "status": "triage",
                    "workspace_kind": "dir",
                    "workspace_path": str(workdir),
                    "body": json.dumps(triage, ensure_ascii=False),
                }
            }
            responses = [
                subprocess.CompletedProcess([], 0, stdout=json.dumps(envelope), stderr=""),
                subprocess.CompletedProcess([], 0, stdout=json.dumps([envelope["task"]]), stderr=""),
            ]
            argv = [
                "build_task_graph.py",
                "prepare-impl-fast-path",
                "--profile",
                "project-manager",
                "--board",
                "issue-151-test-11",
                "--triage-id",
                "t_triage",
                "--issue",
                str(issue),
                "--output-dir",
                ".temp/task-graphs/151/fast-path",
                "--batch-output",
                ".temp/task-graphs/151/impl-card-batch.json",
            ]

            with patch.object(sys, "argv", argv), patch.object(
                task_graph.Path, "cwd", return_value=workdir
            ), patch.object(task_graph.subprocess, "run", side_effect=responses) as run:
                self.assertEqual(0, task_graph.main())

        self.assertEqual(2, run.call_count)
        commands = [call.args[0] for call in run.call_args_list]
        self.assertEqual("show", commands[0][-3])
        self.assertEqual("list", commands[1][-2])

    def test_apply_impl_completions_preserves_exact_class_seam_and_contract_change(self):
        triage = self.valid_frozen_triage()
        batch = legacy_impl_batch_template(triage, triage_id="t_triage")
        card = batch["cards"][0]
        expected = card["body"]["context"]["entry_points"][0]
        expected["symbol"] = "Order"
        expected["reason"] = "[ACCEPT-001][preserve] Order aggregate 경계를 보존한다."
        card["contract_changes"] = [
            {
                "id": "CONTRACT-001",
                "change": "OfferQueryApi contract와 OFFER_NOT_FOUND 상수를 함께 보존한다.",
            }
        ]
        authored_entry_points = [
            {
                "path": expected["path"],
                "symbol": "Order#changeQuantity",
                "reason": "[ACCEPT-001][modify] Order 수량 변경 메서드의 구현 위치를 확인한다.",
            }
        ]
        authored_entry_points.extend(
            {
                "path": item["path"],
                "symbol": item["symbol"],
                "reason": "[ACCEPT-001][modify] frozen gap의 필수 구현 진입점을 반영한다.",
            }
            for item in card["required_entry_points"]
        )
        authored_verification = [
            {
                "id": item["id"],
                "test_level": "unit",
                "tests": ["example.CartTest#quantityZeroDeletes"],
                "scenarios": list(item["scenarios"]),
            }
            for item in card["body"]["verification"]
        ]

        merged = apply_impl_completions(
            batch,
            {
                "cards": [
                    {
                        "ordinal": 1,
                        "implementation_contract": self.valid_card()["implementation_contract"],
                        "entry_points": authored_entry_points,
                        "verification": authored_verification,
                    }
                ]
            },
        )

        symbols = [item["symbol"] for item in merged["cards"][0]["body"]["context"]["entry_points"]]
        self.assertEqual(
            ["Order", "Order#changeQuantity", "OrderService#changeQuantity"],
            symbols,
        )
        marker = "CONTRACT-001: OfferQueryApi contract와 OFFER_NOT_FOUND 상수를 함께 보존한다."
        self.assertIn(
            marker,
            merged["cards"][0]["body"]["implementation_contract"]["preserved_behaviors"],
        )
        self.assertNotIn("CONTRACT_CHANGE_MISSING:0:CONTRACT-001", validate_impl_batch(merged))
        merged["cards"][0]["body"]["implementation_contract"]["preserved_behaviors"].remove(marker)
        merged["cards"][0]["body"]["verification"][0]["scenarios"].append(marker)
        self.assertIn("CONTRACT_CHANGE_MISSING:0:CONTRACT-001", validate_impl_batch(merged))

    def valid_card(self):
        return json.loads(self.fixture_path.read_text(encoding="utf-8"))

    def invalid_card(self):
        path = SKILL_DIR / "tests" / "fixtures" / "invalid-missing-sections.json"
        return json.loads(path.read_text(encoding="utf-8"))

    def test_valid_backend_implementation_card_passes(self):
        self.assertEqual([], validate(self.valid_card()))

    def test_contract_change_marker_may_preserve_source_language(self):
        card = self.valid_card()
        card["implementation_contract"]["preserved_behaviors"].append(
            "CC-151-OFFER-QUERY-SEAM: Move the Cart Offer capability contract to the policy-owning inner layer."
        )

        for validator in (validate, core_validate_card):
            with self.subTest(validator=validator.__module__):
                self.assertNotIn(
                    "DESCRIPTION_NOT_KOREAN:implementation_contract:preserved_behaviors:1",
                    validator(card),
                )

    def test_lowercase_contract_change_marker_may_preserve_source_language(self):
        card = self.valid_card()
        card["implementation_contract"]["preserved_behaviors"].append(
            "cc-151-offer-error: The producer-owned OFFER-001 error remains the single public contract."
        )

        for validator in (validate, core_validate_card):
            with self.subTest(validator=validator.__module__):
                self.assertNotIn(
                    "DESCRIPTION_NOT_KOREAN:implementation_contract:preserved_behaviors:1",
                    validator(card),
                )

    def test_http_dto_boundary_requires_request_and_response_artifact_closure(self):
        card = self.valid_card()
        card["acceptance"] = [
            {
                "id": "ACC-HTTP-DTO",
                "criterion": (
                    "Cart presentation DTO와 transport-neutral application command/result가 "
                    "분리된다."
                ),
                "requirement_ids": ["CR-HTTP-DTO"],
                "observable_ids": ["OBS-HTTP"],
            }
        ]
        card["verification"][0]["acceptance_ids"] = ["ACC-HTTP-DTO"]
        card["context"]["entry_points"] = [
            {
                "path": "src/main/java/example/application/dto/AddItemRequest.java",
                "symbol": "AddItemRequest",
                "reason": "ACC-HTTP-DTO의 현재 HTTP request DTO다.",
            },
            {
                "path": "src/main/java/example/presentation/dto/AddItemRequest.java",
                "symbol": "AddItemRequest",
                "reason": "ACC-HTTP-DTO의 planned presentation request DTO다.",
            },
            {
                "path": "src/main/java/example/application/command/AddItemCommand.java",
                "symbol": "AddItemCommand",
                "reason": "ACC-HTTP-DTO의 planned application command다.",
            },
            {
                "path": "src/main/java/example/application/result/CartResult.java",
                "symbol": "CartResult",
                "reason": "ACC-HTTP-DTO의 planned application result다.",
            },
        ]

        for validator in (validate, core_validate_card):
            with self.subTest(validator=validator.__module__):
                self.assertIn(
                    "INCOMPLETE_HTTP_DTO_RESULT_BOUNDARY:ACC-HTTP-DTO:"
                    "application-response,presentation-response",
                    validator(card),
                )

        card["context"]["entry_points"].extend(
            [
                {
                    "path": "src/main/java/example/application/dto/CartResponse.java",
                    "symbol": "CartResponse",
                    "reason": "ACC-HTTP-DTO의 현재 HTTP response DTO다.",
                },
                {
                    "path": "src/main/java/example/presentation/dto/CartResponse.java",
                    "symbol": "CartResponse",
                    "reason": "ACC-HTTP-DTO의 planned presentation response DTO다.",
                },
            ]
        )

        for validator in (validate, core_validate_card):
            with self.subTest(validator=validator.__module__):
                self.assertFalse(
                    any(
                        error.startswith("INCOMPLETE_HTTP_DTO_RESULT_BOUNDARY:")
                        for error in validator(card)
                    )
                )

        card["context"]["entry_points"].append(
            {
                "path": "src/main/java/example/application/response/CartResponse.java",
                "symbol": "CartResponse",
                "reason": "ACC-HTTP-DTO의 result와 중복되는 planned application response다.",
            }
        )
        for validator in (validate, core_validate_card):
            with self.subTest(validator=validator.__module__):
                self.assertIn(
                    "AMBIGUOUS_APPLICATION_OUTPUT_BOUNDARY:ACC-HTTP-DTO",
                    validator(card),
                )

    def test_missing_required_sections_fail(self):
        errors = validate(self.invalid_card())
        self.assertIn("MISSING_SLICE", errors)
        self.assertIn("MISSING_VERIFICATION", errors)

    def test_template_is_fact_only_and_requires_authoring(self):
        card = template(138, "https://github.com/example/repository/issues/138")
        self.assertEqual("backend-implementation-card-v5", card["schema"])
        self.assertNotIn("assignee", card)
        self.assertNotIn("baseline_sha", card)
        self.assertIn("MISSING_TRIAGE_DIGEST", validate(card))
        self.assertIn("MISSING_SLICE_OUTCOME", validate(card))

    def test_v5_has_only_durable_execution_input(self):
        card = self.valid_card()
        self.assertEqual(
            {
                "schema",
                "card_type",
                "issue",
                "triage_digest",
                "slice",
                "implementation_contract",
                "context",
                "acceptance",
                "verification",
            },
            set(card),
        )
        for obsolete in (
            "goal",
            "effective_behavior",
            "scope",
            "out_of_scope",
            "implementation_context",
            "contracts",
            "acceptance_criteria",
            "focused_verification",
            "full_backend_verification",
            "traceability",
        ):
            self.assertNotIn(obsolete, card)

    def test_rejects_native_and_planning_fields_anywhere_in_body(self):
        for field in ("baseline_sha", "planning_sha", "assignee", "status", "dependency", "workspace"):
            with self.subTest(field=field):
                card = self.valid_card()
                card["context"][field] = "forbidden"
                self.assertIn(f"FORBIDDEN_FIELD:{field}", validate(card))

    def test_rejects_legacy_contract_dimensions(self):
        card = self.valid_card()
        card["contracts"] = {"api": {"applicable": True, "rules": ["legacy"]}}
        self.assertIn("UNEXPECTED_CARD_FIELD:contracts", validate(card))

    def test_requires_complete_implementation_contract(self):
        card = self.valid_card()
        del card["implementation_contract"]["error_semantics"]
        card["implementation_contract"]["events"] = {
            "applicability": "not-applicable",
            "rules": [],
        }

        errors = validate(card)

        self.assertIn("MISSING_IMPLEMENTATION_CONTRACT_DIMENSION:error_semantics", errors)
        self.assertIn("MISSING_IMPLEMENTATION_CONTRACT_RULES:events", errors)

    def test_rejects_generic_contract_text_and_file_name_symbols(self):
        card = self.valid_card()
        card["context"]["constraints"][0]["rule"] = (
            "저장소 규칙: 적용 계층과 책임 경계를 유지하고 해당 공개 계약을 임의로 넓히지 않는다."
        )
        card["context"]["entry_points"][0]["symbol"] = "ProductController.java"
        card["context"]["entry_points"][0]["reason"] = (
            "이 조사 시작점에서 해당 책임 경계와 현재 호출 관계를 확인한다."
        )
        card["verification"][0]["scenarios"] = [
            "성공·실패·경계 조건이 해당 observable의 요구된 동작과 일치하는지 확인한다."
        ]

        errors = validate(card)

        self.assertIn("GENERIC_CONTEXT_TEXT:constraints:0", errors)
        self.assertIn("ENTRY_POINT_SYMBOL_IS_FILE:0", errors)
        self.assertIn("GENERIC_ENTRY_POINT_REASON:0", errors)
        self.assertIn("GENERIC_VERIFICATION_SCENARIO:FV-PRODUCT-CREATE:0", errors)

    def test_rejects_non_current_implementation_card_schema(self):
        card = self.valid_card()
        card["schema"] = "unsupported-implementation-card"
        self.assertIn("SCHEMA_VERSION", validate(card))

    def test_requires_slice_links_and_boundaries(self):
        card = self.valid_card()
        del card["slice"]["behavior_ids"]
        card["slice"]["scope"] = []
        card["slice"]["out_of_scope"] = []
        errors = validate(card)
        self.assertIn("MISSING_SLICE_BEHAVIOR_IDS", errors)
        self.assertIn("MISSING_SLICE_SCOPE", errors)
        self.assertIn("MISSING_SLICE_OUT_OF_SCOPE", errors)

    def test_rejects_duplicate_ids(self):
        mutations = (
            ("acceptance", "DUPLICATE_ACCEPTANCE_ID:AC-PRODUCT-CREATE"),
            ("verification", "DUPLICATE_VERIFICATION_ID:FV-PRODUCT-CREATE"),
        )
        for field, error in mutations:
            with self.subTest(field=field):
                card = self.valid_card()
                card[field].append(copy.deepcopy(card[field][0]))
                self.assertIn(error, validate(card))

    def test_rejects_unknown_acceptance_reference(self):
        card = self.valid_card()
        card["verification"][0]["acceptance_ids"] = ["AC-UNKNOWN"]
        self.assertIn("UNKNOWN_ACCEPTANCE_ID:FV-PRODUCT-CREATE:AC-UNKNOWN", validate(card))

    def test_requires_complete_acceptance_coverage(self):
        card = self.valid_card()
        card["verification"][0]["acceptance_ids"] = []
        errors = validate(card)
        self.assertIn("MISSING_ACCEPTANCE_IDS:FV-PRODUCT-CREATE", errors)
        self.assertIn("UNCOVERED_ACCEPTANCE:AC-PRODUCT-CREATE", errors)

    def test_rejects_invalid_test_level(self):
        card = self.valid_card()
        card["verification"][0]["test_level"] = "e2e"
        self.assertIn("INVALID_TEST_LEVEL:FV-PRODUCT-CREATE", validate(card))

    def test_malformed_verification_values_return_errors_instead_of_crashing(self):
        card = self.valid_card()
        verification = card["verification"][0]
        verification["test_level"] = []
        verification["scenarios"] = [{}]
        errors = validate(card)
        self.assertIn("INVALID_TEST_LEVEL:FV-PRODUCT-CREATE", errors)
        self.assertIn("MISSING_REQUIRED_SCENARIOS:FV-PRODUCT-CREATE", errors)

    def test_requires_runnable_focused_verification_gate(self):
        card = self.valid_card()
        verification = card["verification"][0]
        verification["tests"] = ["planned-ProductTest"]
        errors = validate(card)
        self.assertIn("INVALID_VERIFICATION_TEST:FV-PRODUCT-CREATE", errors)

    def test_rejects_obsolete_top_level_fields(self):
        card = self.valid_card()
        card["planning"] = {"state_freshness": "fresh"}
        self.assertIn("UNEXPECTED_CARD_FIELD:planning", validate(card))

    def test_rejects_noncanonical_nested_fields(self):
        mutations = (
            (("issue",), "title", "UNEXPECTED_ISSUE_FIELD:title"),
            (("slice",), "prompt", "UNEXPECTED_SLICE_FIELD:prompt"),
            (("context",), "prompt", "UNEXPECTED_CONTEXT_FIELD:prompt"),
            (("context", "entry_points", 0), "prompt", "UNEXPECTED_ENTRY_POINT_FIELD:0:prompt"),
            (("acceptance", 0), "prompt", "UNEXPECTED_ACCEPTANCE_FIELD:0:prompt"),
            (("verification", 0), "executor", "UNEXPECTED_VERIFICATION_FIELD:0:executor"),
        )
        for path, field, error in mutations:
            with self.subTest(path=path, field=field):
                card = self.valid_card()
                target = card
                for part in path:
                    target = target[part]
                target[field] = "unexpected"
                self.assertIn(error, validate(card))

    def test_context_sources_use_repository_locators(self):
        card = self.valid_card()
        card["context"]["current_state"][0]["source"] = {"kind": "task", "task_id": "t_123"}
        self.assertIn("INVALID_SOURCE_KIND:current_state:0", validate(card))

    def test_requires_korean_descriptive_text(self):
        for goal in (
            "An authenticated seller can create a product.",
            "An authenticated seller can create a product 한.",
        ):
            with self.subTest(goal=goal):
                card = self.valid_card()
                card["slice"]["outcome"] = goal
                self.assertIn("DESCRIPTION_NOT_KOREAN:slice:outcome", validate(card))

    def test_accepts_korean_contract_with_many_literal_technical_identifiers(self):
        card = self.valid_card()
        card["slice"]["outcome"] = (
            "CartService#mergeGuestCart는 OfferQueryPort#requireCurrentOffer와 "
            "CartRepository#findByIdForUpdate를 사용해 병합을 수행한다."
        )

        self.assertNotIn("DESCRIPTION_NOT_KOREAN:slice:outcome", validate(card))

    def test_rejects_boolean_issue_number(self):
        card = self.valid_card()
        card["issue"]["number"] = True
        self.assertIn("INVALID_ISSUE_NUMBER", validate(card))

    def test_rejects_non_https_issue_url(self):
        card = self.valid_card()
        card["issue"]["url"] = "not-a-url"
        self.assertIn("INVALID_ISSUE_URL", validate(card))

    def test_rejects_malformed_https_issue_urls(self):
        for value in (
            "https:// /issues/138",
            "https://github.com",
            "https://github.com/?issue=138",
            "https://github.com/#138",
            "https://user@github.com/issues/138",
        ):
            with self.subTest(value=value):
                card = self.valid_card()
                card["issue"]["url"] = value
                self.assertIn("INVALID_ISSUE_URL", validate(card))

    def test_write_json_is_scoped_to_project_temp(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = Path(".temp") / "task-graphs" / "issue-138" / "impl-1.json"
            with patch("build_task_graph.Path.cwd", return_value=root):
                write_json(output, {"ok": True})
            self.assertEqual({"ok": True}, json.loads((root / output).read_text(encoding="utf-8")))
            with self.assertRaises(ValueError):
                write_json(Path("impl-1.json"), {"ok": True})
            with self.assertRaises(ValueError):
                write_json(Path(".temp") / ".." / "escaped.json", {"ok": True})
            self.assertFalse((root / "escaped.json").exists())

    def test_serialize_task_body_uses_readable_utf8_json(self):
        body = {"schema": "example-v1", "goal": "가독성 확인", "nested": [True, None]}

        rendered = task_graph.serialize_task_body(body)

        self.assertEqual(
            '{\n  "schema": "example-v1",\n  "goal": "가독성 확인",\n  "nested": [\n    true,\n    null\n  ]\n}\n',
            rendered,
        )
        self.assertEqual(body, json.loads(rendered))

    def test_serialize_task_body_rejects_nonfinite_numbers(self):
        for value in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                task_graph.serialize_task_body({"value": value})

    def test_cli_renders_selected_card_body_as_pretty_json(self):
        graph_path = SKILL_DIR / "tests" / "fixtures" / "valid-graph-draft.json"
        graph = json.loads(graph_path.read_text(encoding="utf-8"))
        expected_body = next(card["body"] for card in graph["cards"] if card["key"] == "impl-1")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = Path(".temp") / "task-bodies" / "impl-1.json"
            completed = subprocess.run(
                [
                    sys.executable,
                    str(SKILL_DIR / "scripts" / "build_task_graph.py"),
                    "render-body",
                    str(graph_path),
                    "--card-key",
                    "impl-1",
                    "--output",
                    str(output),
                ],
                cwd=root,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)
            rendered = (root / output).read_bytes().decode("utf-8")
            self.assertEqual(task_graph.serialize_task_body(expected_body), rendered)
            self.assertIn(f'\n  "schema": "{expected_body["schema"]}",\n', rendered)
            self.assertEqual(expected_body, json.loads(rendered))

    def test_cli_validates_fixture(self):
        completed = subprocess.run(
            [sys.executable, str(SKILL_DIR / "scripts" / "build_task_graph.py"), "validate", str(self.fixture_path)],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual({"valid": True, "errors": []}, json.loads(completed.stdout))

    def test_cli_rejects_duplicate_card_json_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "card.json"
            path.write_text(
                '{"schema":"wrong","schema":"backend-implementation-card-v1"}',
                encoding="utf-8",
            )
            completed = subprocess.run(
                [sys.executable, str(SKILL_DIR / "scripts" / "build_task_graph.py"), "validate", str(path)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(2, completed.returncode)
            self.assertIn("DUPLICATE_JSON_KEY:schema", completed.stdout)

    def test_cli_writes_template_under_temp(self):
        with tempfile.TemporaryDirectory() as directory:
            completed = subprocess.run(
                [
                    sys.executable,
                    str(SKILL_DIR / "scripts" / "build_task_graph.py"),
                    "template",
                    "--issue",
                    "138",
                    "--issue-url",
                    "https://github.com/example/repository/issues/138",
                    "--output",
                    ".temp/task-graphs/issue-138/impl-1.json",
                ],
                cwd=directory,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(0, completed.returncode, completed.stderr)
            output = Path(directory) / ".temp/task-graphs/issue-138/impl-1.json"
            self.assertEqual("backend-implementation-card-v5", json.loads(output.read_text(encoding="utf-8"))["schema"])


class ReviewFastPathTest(unittest.TestCase):
    def source_tasks(self, workspace: Path):
        triage_body = {
            "schema": "triage-v7",
            "phase": "frozen",
            "frozen_digest": "d" * 64,
            "issue": {
                "number": 151,
                "title": "장바구니 구조 개선",
                "url": "https://github.com/example/repo/issues/151",
            },
            "baseline": {"planning_sha": "a" * 40},
            "intent": {
                "goal": "장바구니의 공개 동작을 유지하면서 계층 책임과 모듈 경계를 개선한다.",
                "preserved_observables": [{"id": "OBS-001", "behavior": "관찰 가능한 동작을 유지한다."}],
                "change_requirements": [
                    {"id": "CHANGE-001", "outcome": "HTTP 요청과 application command 책임을 분리한다."}
                ],
                "out_of_scope": ["실제 리뷰 실행 결과는 본문에 기록하지 않는다."],
            },
        }
        impl_body = {
            "schema": "backend-implementation-card-v5",
            "issue": triage_body["issue"],
            "triage_digest": triage_body["frozen_digest"],
            "slice": {"behavior_ids": ["OBS-001"]},
            "acceptance": [{"id": "AC-001"}],
            "verification": [{"id": "VERIFY-001"}],
        }
        common = {
            "skills": [],
            "workspace_kind": "dir",
            "workspace_path": workspace.as_posix(),
        }
        return [
            common | {
                "id": "t_triage", "title": "G1-Issue151-Triage", "body": json.dumps(triage_body),
                "assignee": "project-manager", "status": "triage", "priority": 0,
            },
            common | {
                "id": "t_impl", "title": "G1-Issue151-Impl1", "body": json.dumps(impl_body),
                "assignee": "coder", "status": "todo", "priority": 1,
            },
        ]

    def summary_source_tasks(self, workspace: Path):
        tasks = self.source_tasks(workspace)
        triage = json.loads(tasks[0]["body"])
        review_body = {
            "schema": "aggregate-review-card-v5",
            "triage_digest": triage["frozen_digest"],
            "baseline_sha": triage["baseline"]["planning_sha"],
            "behavior_ids": ["OBS-001"],
            "implementation_card_keys": ["impl-1"],
            "acceptance": [],
            "review_checks": [],
            "scope_exclusions": [],
        }
        tasks.append(
            {
                "id": "t_review",
                "title": "G1-Issue151-Review1",
                "body": json.dumps(review_body),
                "assignee": "reviewer",
                "status": "todo",
                "priority": 0,
                "skills": [],
                "workspace_kind": "dir",
                "workspace_path": workspace.as_posix(),
            }
        )
        return tasks

    def test_prepare_prefills_only_immutable_facts_and_source_closure(self):
        workspace = Path.cwd().resolve()
        bundle = review_fast.prepare_review_fast_path(
            self.source_tasks(workspace),
            triage_id="t_triage",
            implementation_ids=["t_impl"],
            issue=151,
            generation=1,
            validate_triage=lambda body: [],
            validate_implementation=lambda body: [],
            guide_locators={"docs/backend-test-guide.md#1. 공통 규칙"},
        )

        self.assertEqual("review-fast-path-v1", bundle["schema"])
        self.assertEqual(["impl-1"], bundle["review_body_template"]["implementation_card_keys"])
        self.assertEqual([], bundle["review_body_template"]["acceptance"])
        self.assertEqual([], bundle["review_body_template"]["review_checks"])
        self.assertEqual(["AC-001"], bundle["implementation_cards"][0]["acceptance_ids"])
        self.assertEqual(["VERIFY-001"], bundle["implementation_cards"][0]["verification_ids"])

    def test_summary_body_is_deterministic_projection_of_frozen_triage(self):
        triage = json.loads(self.source_tasks(Path.cwd().resolve())[0]["body"])

        body = review_fast.summary_body_from_triage(triage)

        self.assertEqual("issue-summary-card-v3", body["schema"])
        self.assertEqual(triage["issue"], body["issue"])
        self.assertEqual(triage["intent"]["goal"], body["objective"])
        self.assertEqual(
            [{"id": "CHANGE-001", "outcome": "HTTP 요청과 application command 책임을 분리한다."}],
            body["target_outcomes"],
        )
        self.assertEqual(
            [{"id": "OBS-001", "behavior": "관찰 가능한 동작을 유지한다."}],
            body["preserved_behaviors"],
        )
        self.assertNotIn("final_implementation_sha", body)
        self.assertNotIn("review_result", body)

    def test_summary_body_rejects_missing_frozen_triage_digest(self):
        triage = json.loads(self.source_tasks(Path.cwd().resolve())[0]["body"])
        triage["frozen_digest"] = None

        with self.assertRaisesRegex(
            ValueError, "SUMMARY_SOURCE_TRIAGE_DIGEST_INVALID"
        ):
            review_fast.summary_body_from_triage(triage)

    def test_prepare_summary_uses_complete_generation_parent_closure(self):
        workspace = Path.cwd().resolve()

        bundle = review_fast.prepare_summary_fast_path(
            self.summary_source_tasks(workspace),
            triage_id="t_triage",
            issue=151,
            generation=1,
            validate_triage=lambda body: [],
        )

        self.assertEqual("summary-fast-path-v1", bundle["schema"])
        self.assertEqual(["t_impl", "t_review"], bundle["parent_ids"])
        self.assertEqual("issue-summary-card-v3", bundle["body"]["schema"])
        self.assertEqual(workspace.as_posix(), bundle["workspace"])

    def test_prepare_summary_rejects_existing_active_summary(self):
        workspace = Path.cwd().resolve()
        tasks = self.summary_source_tasks(workspace)
        tasks.append(
            {
                "id": "t_summary",
                "title": "G1-Issue151-Summary",
                "body": json.dumps({"schema": "issue-summary-card-v2", "triage_digest": "d" * 64}),
                "assignee": "project-manager",
                "status": "todo",
                "priority": 0,
                "skills": [],
                "workspace_kind": "dir",
                "workspace_path": workspace.as_posix(),
            }
        )

        with self.assertRaisesRegex(ValueError, "SUMMARY_TITLE_ALREADY_EXISTS"):
            review_fast.prepare_summary_fast_path(
                tasks,
                triage_id="t_triage",
                issue=151,
                generation=1,
                validate_triage=lambda body: [],
            )

    def test_create_summary_dry_run_revalidates_source_and_body_file(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory).resolve()
            tasks = self.summary_source_tasks(workspace)
            bundle = review_fast.prepare_summary_fast_path(
                tasks,
                triage_id="t_triage",
                issue=151,
                generation=1,
                validate_triage=lambda body: [],
            )
            body_dir = workspace / ".temp/summary"
            body_dir.mkdir(parents=True)
            body_file = body_dir / "body.json"
            body_file.write_text(task_graph.serialize_task_body(bundle["body"]), encoding="utf-8")
            listing = subprocess.CompletedProcess([], 0, stdout=json.dumps(tasks), stderr="")

            with patch.object(review_fast.subprocess, "run", return_value=listing) as run, patch.object(
                review_fast.Path, "cwd", return_value=workspace
            ):
                report = review_fast.create_summary_native(
                    bundle,
                    profile="project-manager",
                    board="board",
                    body_file=body_file,
                    workspace_path=workspace,
                    dry_run=True,
                    validate_triage=lambda body: [],
                )

            self.assertTrue(report["valid"])
            self.assertEqual(1, report["native_calls"])
            self.assertEqual(1, run.call_count)

    def test_summary_parent_readback_accepts_native_ordering_without_losing_cardinality(self):
        self.assertTrue(review_fast._same_task_ids(["t_impl2", "t_impl1"], ["t_impl1", "t_impl2"]))
        self.assertFalse(review_fast._same_task_ids(["t_impl1", "t_impl1"], ["t_impl1", "t_impl2"]))

    def test_create_summary_verifies_parent_reverse_links_concurrently(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory).resolve()
            tasks = self.summary_source_tasks(workspace)
            bundle = review_fast.prepare_summary_fast_path(
                tasks,
                triage_id="t_triage",
                issue=151,
                generation=1,
                validate_triage=lambda body: [],
            )
            body_dir = workspace / ".temp/summary"
            body_dir.mkdir(parents=True)
            body_file = body_dir / "body.json"
            body_text = task_graph.serialize_task_body(bundle["body"])
            body_file.write_text(body_text, encoding="utf-8")
            lock = threading.Lock()
            active_parent_reads = 0
            max_parent_reads = 0
            list_calls = 0

            def native(command, **kwargs):
                nonlocal active_parent_reads, max_parent_reads, list_calls
                if "list" in command:
                    list_calls += 1
                    payload = tasks
                elif "create" in command:
                    payload = {"id": "t_summary", "title": bundle["title"]}
                elif "show" in command and "t_summary" in command:
                    payload = {
                        "task": {
                            "id": "t_summary",
                            "title": bundle["title"],
                            "body": body_text,
                            "assignee": "project-manager",
                            "status": "todo",
                            "priority": 0,
                            "workspace_path": workspace.as_posix(),
                        },
                        "parents": list(reversed(bundle["parent_ids"])),
                        "children": [],
                    }
                elif "show" in command:
                    with lock:
                        active_parent_reads += 1
                        max_parent_reads = max(max_parent_reads, active_parent_reads)
                    wall_time.sleep(0.05)
                    with lock:
                        active_parent_reads -= 1
                    payload = {"task": {"id": command[-2]}, "parents": [], "children": ["t_summary"]}
                else:
                    raise AssertionError(command)
                return subprocess.CompletedProcess(command, 0, stdout=json.dumps(payload), stderr="")

            with patch.object(review_fast.subprocess, "run", side_effect=native), patch.object(
                review_fast.Path, "cwd", return_value=workspace
            ):
                report = review_fast.create_summary_native(
                    bundle,
                    profile="project-manager",
                    board="board",
                    body_file=body_file,
                    workspace_path=workspace,
                    dry_run=False,
                    validate_triage=lambda body: [],
                    active_tasks=tasks,
                )

            self.assertTrue(report["valid"])
            self.assertEqual(2, max_parent_reads)
            self.assertEqual(0, list_calls)
            self.assertEqual(4, report["native_calls"])

    def test_regenerate_summary_native_archives_and_recreates_in_one_operation(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory).resolve()
            tasks = self.summary_source_tasks(workspace)
            expected_bundle = review_fast.prepare_summary_fast_path(
                tasks,
                triage_id="t_triage",
                issue=151,
                generation=1,
                validate_triage=lambda body: [],
            )
            body_text = task_graph.serialize_task_body(expected_bundle["body"])
            old_summary = {
                "id": "t_old_summary",
                "title": expected_bundle["title"],
                "body": body_text,
                "assignee": "project-manager",
                "status": "todo",
                "priority": 0,
                "skills": [],
                "workspace_kind": "dir",
                "workspace_path": workspace.as_posix(),
            }
            active_with_summary = [*tasks, old_summary]
            list_count = 0

            def native(command, **kwargs):
                nonlocal list_count
                if "list" in command:
                    list_count += 1
                    payload = active_with_summary if list_count == 1 else tasks
                elif "archive" in command:
                    return subprocess.CompletedProcess(command, 0, stdout="Archived t_old_summary\n", stderr="")
                elif "show" in command and "t_old_summary" in command:
                    payload = {"task": old_summary | {"status": "archived"}, "parents": [], "children": []}
                elif "create" in command:
                    payload = {"id": "t_new_summary", "title": expected_bundle["title"]}
                elif "show" in command and "t_new_summary" in command:
                    payload = {
                        "task": {
                            "id": "t_new_summary",
                            "title": expected_bundle["title"],
                            "body": body_text,
                            "assignee": "project-manager",
                            "status": "todo",
                            "priority": 0,
                            "workspace_path": workspace.as_posix(),
                        },
                        "parents": expected_bundle["parent_ids"],
                        "children": [],
                    }
                elif "show" in command:
                    payload = {"task": {"id": command[-2]}, "parents": [], "children": ["t_new_summary"]}
                else:
                    raise AssertionError(command)
                return subprocess.CompletedProcess(command, 0, stdout=json.dumps(payload), stderr="")

            output_dir = workspace / ".temp/summary"
            with patch.object(review_fast.subprocess, "run", side_effect=native), patch.object(
                review_fast.Path, "cwd", return_value=workspace
            ):
                report = review_fast.regenerate_summary_native(
                    profile="project-manager",
                    board="board",
                    triage_id="t_triage",
                    issue=151,
                    generation=1,
                    workspace_path=workspace,
                    bundle_file=output_dir / "source.json",
                    body_file=output_dir / "body.json",
                    validate_triage=lambda body: [],
                )

            self.assertEqual("t_old_summary", report["archived_task_id"])
            self.assertEqual("t_new_summary", report["task_id"])
            self.assertEqual(8, report["native_calls"])
            self.assertEqual(expected_bundle, json.loads((output_dir / "source.json").read_text(encoding="utf-8")))

    def test_prepare_normalizes_relative_native_workspace_against_execution_root(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory).resolve()
            tasks = self.source_tasks(workspace)
            tasks[0]["workspace_path"] = "."

            with patch("os.getcwd", return_value=str(workspace)):
                bundle = review_fast.prepare_review_fast_path(
                    tasks,
                    triage_id="t_triage",
                    implementation_ids=["t_impl"],
                    issue=151,
                    generation=1,
                    validate_triage=lambda body: [],
                    validate_implementation=lambda body: [],
                    guide_locators=set(),
                )

            self.assertEqual(workspace.as_posix(), bundle["workspace"])
            self.assertEqual(workspace.as_posix(), bundle["triage"]["task"]["workspace_path"])

    def test_prepare_rejects_incomplete_active_implementation_closure(self):
        workspace = Path.cwd().resolve()
        tasks = self.source_tasks(workspace)
        impl2 = copy.deepcopy(tasks[1])
        impl2.update({"id": "t_impl_2", "title": "G1-Issue151-Impl2"})
        tasks.append(impl2)

        with self.assertRaisesRegex(ValueError, "REVIEW_IMPLEMENTATION_CLOSURE_MISMATCH"):
            review_fast.prepare_review_fast_path(
                tasks,
                triage_id="t_triage",
                implementation_ids=["t_impl"],
                issue=151,
                generation=1,
                validate_triage=lambda body: [],
                validate_implementation=lambda body: [],
                guide_locators=set(),
            )

    def test_prepare_fingerprint_is_independent_of_supplied_impl_order(self):
        workspace = Path.cwd().resolve()
        tasks = self.source_tasks(workspace)
        impl2 = copy.deepcopy(tasks[1])
        impl2.update({"id": "t_impl_2", "title": "G1-Issue151-Impl2"})
        tasks.append(impl2)
        kwargs = {
            "triage_id": "t_triage",
            "issue": 151,
            "generation": 1,
            "validate_triage": lambda body: [],
            "validate_implementation": lambda body: [],
            "guide_locators": set(),
        }

        first = review_fast.prepare_review_fast_path(
            tasks, implementation_ids=["t_impl", "t_impl_2"], **kwargs
        )
        reversed_order = review_fast.prepare_review_fast_path(
            tasks, implementation_ids=["t_impl_2", "t_impl"], **kwargs
        )

        self.assertEqual(first["source_fingerprint"], reversed_order["source_fingerprint"])
        self.assertEqual(first["implementation_cards"], reversed_order["implementation_cards"])

    def test_create_dry_run_uses_one_native_read_and_keeps_semantic_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory).resolve()
            tasks = self.source_tasks(workspace)
            bundle = review_fast.prepare_review_fast_path(
                tasks,
                triage_id="t_triage",
                implementation_ids=["t_impl"],
                issue=151,
                generation=1,
                validate_triage=lambda body: [],
                validate_implementation=lambda body: [],
                guide_locators=set(),
            )
            body = copy.deepcopy(bundle["review_body_template"])
            body["acceptance"] = [{"id": "REVIEW-AC-001"}]
            body["review_checks"] = [{"id": "REVIEW-CHECK-001"}]
            body_dir = workspace / ".temp/review"
            body_dir.mkdir(parents=True)
            body_file = body_dir / "body.json"
            body_file.write_text(task_graph.serialize_task_body(body), encoding="utf-8")
            listing = subprocess.CompletedProcess([], 0, stdout=json.dumps(tasks), stderr="")
            with patch.object(review_fast.subprocess, "run", return_value=listing) as run, patch.object(
                review_fast.Path, "cwd", return_value=workspace
            ):
                report = review_fast.create_review_native(
                    bundle,
                    body,
                    profile="project-manager",
                    board="board",
                    body_file=body_file,
                    workspace_path=workspace,
                    dry_run=True,
                    validate_triage=lambda value: [],
                    validate_implementation=lambda value: [],
                    validate_review=lambda graph, validate_implementation: [],
                    guide_locators=set(),
                )

            self.assertTrue(report["valid"])
            self.assertEqual(1, report["native_calls"])
            self.assertEqual(1, run.call_count)

            tampered = copy.deepcopy(bundle)
            tampered["title"] = "G1-Issue151-Review2"
            with patch.object(review_fast.subprocess, "run", return_value=listing), patch.object(
                review_fast.Path, "cwd", return_value=workspace
            ):
                with self.assertRaisesRegex(ValueError, "REVIEW_BUNDLE_IDENTITY_STALE"):
                    review_fast.create_review_native(
                        tampered,
                        body,
                        profile="project-manager",
                        board="board",
                        body_file=body_file,
                        workspace_path=workspace,
                        dry_run=True,
                        validate_triage=lambda value: [],
                        validate_implementation=lambda value: [],
                        validate_review=lambda graph, validate_implementation: [],
                        guide_locators=set(),
                    )


class TaskGraphContractTest(unittest.TestCase):
    valid_path = SKILL_DIR / "tests" / "fixtures" / "valid-graph-draft.json"

    def valid_graph(self):
        return json.loads(self.valid_path.read_text(encoding="utf-8"))

    def bind_review_contract(
        self,
        review,
        behavior_ids,
        implementation_card_keys,
        source_acceptance_ids=None,
        verification_ids=None,
    ):
        body = review["body"]
        source_acceptance_ids = source_acceptance_ids or ["AC-PRODUCT-CREATE"]
        verification_ids = verification_ids or ["FV-PRODUCT-CREATE"]
        body["behavior_ids"] = behavior_ids
        body["implementation_card_keys"] = implementation_card_keys
        body["acceptance"][0]["behavior_ids"] = behavior_ids
        body["acceptance"][0]["implementation_card_keys"] = implementation_card_keys
        body["acceptance"][0]["source_acceptance_ids"] = source_acceptance_ids
        body["acceptance"][0]["verification_ids"] = verification_ids
        for check in body["review_checks"]:
            check["behavior_ids"] = behavior_ids
            check["implementation_card_keys"] = implementation_card_keys
            check["source_acceptance_ids"] = source_acceptance_ids
            check["verification_ids"] = verification_ids
        if len(implementation_card_keys) > 1:
            seam_check = copy.deepcopy(body["review_checks"][0])
            seam_check.update(
                {
                    "id": "REVIEW-CHECK-SEAM",
                    "focus": "cross-card-seam",
                    "question": "여러 구현 slice의 연결 계약과 책임 경계가 일치하는가?",
                    "expected_outcome": "각 slice는 합의된 public seam으로 연결되고 정책을 중복 구현하지 않는다.",
                }
            )
            body["review_checks"].append(seam_check)

    def review_rework_graph(self):
        graph = self.valid_graph()
        triage, impl1, review1, summary = graph["cards"]
        triage["status"] = impl1["status"] = review1["status"] = "done"
        impl2 = copy.deepcopy(impl1)
        impl2.update({"key": "impl-2", "title": "G1-Issue138-Impl2", "status": "ready", "priority": 2, "parents": ["review-1"]})
        impl2["body"]["acceptance"][0]["id"] = "AC-PRODUCT-CREATE-2"
        impl2["body"]["verification"][0]["id"] = "FV-PRODUCT-CREATE-2"
        impl2["body"]["verification"][0]["acceptance_ids"] = ["AC-PRODUCT-CREATE-2"]
        review2 = copy.deepcopy(review1)
        review2.update({"key": "review-2", "title": "G1-Issue138-Review2", "status": "todo", "priority": 1, "parents": ["impl-2"]})
        self.bind_review_contract(
            review2,
            ["behavior-product-create"],
            ["impl-1", "impl-2"],
            ["AC-PRODUCT-CREATE", "AC-PRODUCT-CREATE-2"],
            ["FV-PRODUCT-CREATE", "FV-PRODUCT-CREATE-2"],
        )
        summary["parents"] = ["impl-1", "review-1", "impl-2", "review-2"]
        graph["cards"] = [triage, impl1, review1, impl2, review2, summary]
        graph.update(
            {
                "mode": "review-rework",
                "lineage": {"prior_generation": 1, "prior_summary": "summary", "archived_unfinished_keys": []},
                "source_review": {
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
                },
                "execution_order": ["impl-2", "review-2"],
                "ready_candidate": "impl-2",
            }
        )
        return graph

    def source_review_result(self, graph):
        source = graph["source_review"]
        return {
            "schema": "aggregate-review-result-v2",
            "review_card_key": source["review_key"],
            "review_task_id": "t_c3",
            "review_run_id": 3,
            "generation": graph["generation"],
            "baseline_sha": graph["planning_baseline_sha"],
            "reviewed_head_sha": "b" * 40,
            "result": "changes-required",
            "reviewed_checkpoints": [
                {
                    "implementation_card_key": "impl-1",
                    "implementation_task_id": "t_b2",
                    "checkpoint_sha": "a" * 40,
                }
            ],
            "prior_findings": [],
            "findings": [
                {
                    "finding_id": disposition["finding_id"],
                    "verdict": disposition["verdict"],
                    "basis": "aggregate acceptance",
                    "observed_fact": "수정이 필요한 동작이 확인되었다.",
                    "evidence": ["src/main/java/example/Target.java:10"],
                    "impact": "요구 동작을 만족하지 못한다.",
                    "resolves": None,
                    "continues": None,
                }
                for disposition in source["dispositions"]
            ],
        }

    def test_valid_graph_draft_passes(self):
        self.assertEqual([], validate_graph(self.valid_graph(), validate_implementation=validate))

    def test_rejects_non_current_graph_schemas(self):
        for schema in ("unsupported-graph-schema", None):
            with self.subTest(schema=schema):
                graph = self.valid_graph()
                graph["schema"] = schema
                self.assertIn("GRAPH_SCHEMA", validate_graph(graph, validate_implementation=validate))

    def test_invalid_graph_fixture_detects_lineage_topology_ready_and_archive(self):
        path = SKILL_DIR / "tests" / "fixtures" / "invalid-graph-topology.json"
        errors = validate_graph(json.loads(path.read_text(encoding="utf-8")), validate_implementation=validate)
        self.assertIn("TITLE_LINEAGE_MISMATCH:impl-1", errors)
        self.assertIn("SUMMARY_MEMBERSHIP_MISMATCH", errors)
        self.assertIn("DRAFT_HAS_READY_CARD", errors)
        self.assertIn("ARCHIVE_STATUS_MISMATCH:triage", errors)
        self.assertIn("ARCHIVE_TYPE_FORBIDDEN:triage", errors)

    def test_native_phase_requires_exact_ready_candidate_and_done_triage(self):
        graph = self.valid_graph()
        graph["cards"][0]["status"] = "done"
        graph["cards"][1]["status"] = "ready"
        self.assertEqual([], validate_graph(graph, phase="native", validate_implementation=validate))

    def test_draft_requires_native_triage_status(self):
        graph = self.valid_graph()
        graph["cards"][0]["status"] = "running"
        self.assertIn("DRAFT_TRIAGE_STATUS", validate_graph(graph, validate_implementation=validate))

    def test_new_graph_rejects_planning_decision_card(self):
        graph = self.valid_graph()
        graph["cards"].insert(
            1,
            {
                "key": "decision-1",
                "title": "G1-Issue138-Decision1",
                "card_type": "decision",
                "assignee": "project-manager",
                "workspace": "scratch",
                "status": "blocked",
                "parents": [],
                "body": {
                    "schema": "policy-decision-card-v1",
                    "source_review_key": "review-1",
                    "finding_ids": ["F-1"],
                    "question": "정책을 결정한다.",
                    "decision_owner": "user",
                },
            },
        )
        graph["cards"][-1]["parents"].append("decision-1")
        self.assertIn("PLANNING_DECISION_MUST_NOT_BE_GRAPH_CARD", validate_graph(graph, validate_implementation=validate))

    def test_native_readback_rejects_summary_parent_omission(self):
        graph = self.valid_graph()
        graph["cards"][0]["status"] = "done"
        graph["cards"][1]["status"] = "ready"
        ids = {card["key"]: f"t_{index + 1}" for index, card in enumerate(graph["cards"])}
        readback = [
            {
                "task": {
                    "id": ids[card["key"]],
                    "title": card["title"],
                    "assignee": card["assignee"],
                    "status": card["status"],
                    "priority": card["priority"],
                    "skills": card["skills"],
                    "body": task_graph.serialize_task_body(card["body"]) if card["body"] is not None else "",
                },
                "parents": [ids[parent] for parent in card["parents"]],
            }
            for card in graph["cards"]
        ]
        next(item for item in readback if item["task"]["title"] == "G1-Issue138-Summary")["parents"] = []
        self.assertIn("NATIVE_PARENT_MISMATCH:summary", validate_native_readback(graph, readback))

    def test_current_graph_rejects_forced_task_skills_and_native_mismatch(self):
        graph = self.valid_graph()
        graph["schema"] = "build-task-graph-v6"
        for card in graph["cards"]:
            card["skills"] = []

        self.assertEqual([], validate_graph(graph, validate_implementation=validate))

        graph["schema"] = "unsupported-legacy-graph"
        self.assertIn("GRAPH_SCHEMA", validate_graph(graph, validate_implementation=validate))
        graph["schema"] = "build-task-graph-v6"

        graph["cards"][2]["skills"] = ["run-workflow"]
        self.assertIn("TASK_SKILLS_MUST_BE_EMPTY:review-1", validate_graph(graph))

        graph["cards"][2]["skills"] = []
        ids = {card["key"]: f"t_{index + 1}" for index, card in enumerate(graph["cards"])}
        readback = [
            {
                "task": {
                    "id": ids[card["key"]],
                    "title": card["title"],
                    "assignee": card["assignee"],
                    "status": card["status"],
                    "priority": card["priority"],
                    "skills": ["run-workflow"] if card["key"] == "review-1" else [],
                    "body": task_graph.serialize_task_body(card["body"]) if card["body"] is not None else "",
                },
                "parents": [ids[parent] for parent in card["parents"]],
            }
            for card in graph["cards"]
        ]

        self.assertIn("NATIVE_SKILLS_MISMATCH:review-1", validate_native_readback(graph, readback))

    def test_native_readback_accepts_separate_triage_body(self):
        graph = self.valid_graph()
        ids = {card["key"]: f"t_{index + 1}" for index, card in enumerate(graph["cards"])}
        readback = [
            {
                "task": {
                    "id": ids[card["key"]],
                    "title": card["title"],
                    "assignee": card["assignee"],
                    "status": card["status"],
                    "priority": card["priority"],
                    "skills": card["skills"],
                    "body": (
                        '{"schema":"triage-v7"}'
                        if card["key"] == "triage"
                        else task_graph.serialize_task_body(card["body"])
                    ),
                },
                "parents": [ids[parent] for parent in card["parents"]],
            }
            for card in graph["cards"]
        ]
        self.assertEqual([], validate_native_readback(graph, readback))

    def test_cli_native_readback_supports_pre_activation_draft_phase(self):
        graph = self.valid_graph()
        ids = {card["key"]: f"t_{index + 1}" for index, card in enumerate(graph["cards"])}
        readback = [
            {
                "task": {
                    "id": ids[card["key"]],
                    "title": card["title"],
                    "assignee": card["assignee"],
                    "status": card["status"],
                    "priority": card["priority"],
                    "skills": card["skills"],
                    "body": (
                        '{"schema":"triage-v7"}'
                        if card["key"] == "triage"
                        else task_graph.serialize_task_body(card["body"])
                    ),
                },
                "parents": [ids[parent] for parent in card["parents"]],
            }
            for card in graph["cards"]
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            graph_path = root / "graph.json"
            readback_path = root / "readback.json"
            graph_path.write_text(json.dumps(graph, ensure_ascii=False), encoding="utf-8")
            readback_path.write_text(json.dumps(readback, ensure_ascii=False), encoding="utf-8")

            completed = subprocess.run(
                [
                    sys.executable,
                    str(SKILL_DIR / "scripts" / "build_task_graph.py"),
                    "validate-native-readback",
                    str(graph_path),
                    str(readback_path),
                    "--phase",
                    "draft",
                ],
                capture_output=True,
                text=True,
                check=False,
            )

        self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)
        self.assertEqual({"valid": True, "errors": []}, json.loads(completed.stdout))

    def test_native_readback_rejects_semantically_equal_minified_body(self):
        graph = self.valid_graph()
        ids = {card["key"]: f"t_{index + 1}" for index, card in enumerate(graph["cards"])}
        readback = [
            {
                "task": {
                    "id": ids[card["key"]],
                    "title": card["title"],
                    "assignee": card["assignee"],
                    "status": card["status"],
                    "priority": card["priority"],
                    "skills": card["skills"],
                    "body": (
                        '{"schema":"triage-v7"}'
                        if card["key"] == "triage"
                        else task_graph.serialize_task_body(card["body"])
                    ),
                },
                "parents": [ids[parent] for parent in card["parents"]],
            }
            for card in graph["cards"]
        ]
        next(item for item in readback if item["task"]["title"] == "G1-Issue138-Impl1")["task"]["body"] = json.dumps(
            graph["cards"][1]["body"], ensure_ascii=False
        )

        self.assertIn("NATIVE_BODY_FORMAT_MISMATCH:impl-1", validate_native_readback(graph, readback))

    def test_implemented_behavior_requires_inherited_evidence(self):
        graph = self.valid_graph()
        behavior = graph["behaviors"][0]
        behavior.update({"state": "implemented", "disposition": "inherited", "evidence": []})
        self.assertIn("MISSING_INHERITED_EVIDENCE:behavior-product-create", validate_graph(graph))

    def test_requirement_rework_requires_revision_and_next_generation(self):
        graph = self.valid_graph()
        graph.update({"mode": "requirement-rework", "generation": 1})
        self.assertIn("REWORK_REQUIRES_NEXT_GENERATION", validate_graph(graph))
        self.assertIn("REVISED_REQUIREMENT_NOT_OBJECT", validate_graph(graph))
        self.assertIn("MISSING_REWORK_LINEAGE", validate_graph(graph))

    def test_planned_behavior_must_resolve_to_matching_implementation(self):
        graph = self.valid_graph()
        graph["behaviors"][0]["implementation_card_key"] = "ghost"
        self.assertIn("BEHAVIOR_IMPL_NOT_FOUND:behavior-product-create", validate_graph(graph))
        graph = self.valid_graph()
        graph["cards"][1]["body"]["slice"]["behavior_ids"] = ["behavior-other"]
        self.assertIn("BEHAVIOR_IMPL_MISMATCH:behavior-product-create", validate_graph(graph))

    def test_change_requirement_must_be_covered_by_planned_behavior(self):
        graph = self.valid_graph()
        graph["change_requirements"] = [
            {
                "id": "CHANGE-PRODUCT-CREATE",
                "outcome": "판매자는 유효한 요청으로 상품을 등록할 수 있다.",
                "source": {
                    "locator": "https://github.com/example/repository/issues/138#상품-등록",
                    "claim": "판매자는 유효한 요청으로 상품을 등록할 수 있다.",
                },
            }
        ]
        graph["behaviors"][0]["requirement_ids"] = []

        self.assertIn(
            "UNCOVERED_CHANGE_REQUIREMENT:CHANGE-PRODUCT-CREATE",
            validate_graph(graph),
        )

    def test_implementation_identity_and_behavior_must_match_graph(self):
        graph = self.valid_graph()
        body = graph["cards"][1]["body"]
        body["issue"] = {"number": 999, "url": "https://github.com/example/repository/issues/999"}
        body["slice"]["behavior_ids"].append("behavior-unplanned")
        body["triage_digest"] = "f" * 64
        body["acceptance"][0]["requirement_ids"] = ["CHANGE-UNKNOWN"]
        errors = validate_graph(graph, validate_implementation=validate)
        self.assertIn("IMPLEMENTATION_ISSUE_NUMBER_MISMATCH:impl-1", errors)
        self.assertIn("IMPLEMENTATION_ISSUE_URL_MISMATCH:impl-1", errors)
        self.assertIn("IMPLEMENTATION_TRIAGE_DIGEST_MISMATCH:impl-1", errors)
        self.assertIn("IMPLEMENTATION_UNKNOWN_BEHAVIOR:impl-1", errors)
        self.assertIn("IMPLEMENTATION_UNKNOWN_REQUIREMENT:impl-1:CHANGE-UNKNOWN", errors)
        self.assertIn("IMPLEMENTATION_REQUIREMENT_COVERAGE:impl-1", errors)
        self.assertIn("IMPLEMENTATION_BEHAVIOR_COVERAGE:impl-1", errors)

    def test_graph_cross_checks_implementation_without_optional_validator(self):
        graph = self.valid_graph()
        graph["cards"][1]["body"]["issue"]["number"] = 999

        self.assertIn(
            "IMPLEMENTATION_ISSUE_NUMBER_MISMATCH:impl-1",
            validate_graph(graph, phase="draft"),
        )

    def test_graph_rejects_impl_assignment_on_non_planned_behavior(self):
        graph = self.valid_graph()
        behavior = graph["behaviors"][0]
        behavior["state"] = "implemented"
        behavior["disposition"] = "inherited"

        self.assertIn(
            f"UNEXPECTED_BEHAVIOR_IMPL:{behavior['id']}",
            validate_graph(graph, phase="draft", validate_implementation=validate),
        )

    def test_rejects_cycle_and_invalid_topology(self):
        graph = self.valid_graph()
        graph["cards"][1]["parents"] = ["review-1"]
        self.assertTrue(any(error.startswith("DEPENDENCY_CYCLE:") for error in validate_graph(graph)))
        graph = self.valid_graph()
        graph["cards"][2]["parents"] = ["summary"]
        self.assertIn("INVALID_REVIEW_PARENTS:review-1", validate_graph(graph))

    def test_independent_executable_cards_use_priority_order_without_fake_dependency(self):
        graph = self.valid_graph()
        impl2 = copy.deepcopy(graph["cards"][1])
        impl2.update({"key": "impl-2", "title": "G1-Issue138-Impl2", "parents": ["triage"], "priority": 2})
        impl2["body"]["acceptance"][0]["id"] = "AC-PRODUCT-CREATE-2"
        impl2["body"]["verification"][0]["id"] = "FV-PRODUCT-CREATE-2"
        impl2["body"]["verification"][0]["acceptance_ids"] = ["AC-PRODUCT-CREATE-2"]
        graph["cards"][1]["priority"] = 3
        graph["cards"][2]["priority"] = 1
        graph["cards"].insert(2, impl2)
        graph["cards"][3]["parents"].append("impl-2")
        self.bind_review_contract(
            graph["cards"][3],
            ["behavior-product-create", "behavior-product-create-2"],
            ["impl-1", "impl-2"],
            ["AC-PRODUCT-CREATE", "AC-PRODUCT-CREATE-2"],
            ["FV-PRODUCT-CREATE", "FV-PRODUCT-CREATE-2"],
        )
        graph["cards"][4]["parents"].append("impl-2")
        graph["execution_order"] = ["impl-1", "impl-2", "review-1"]
        graph["behaviors"].append(
            {
                "id": "behavior-product-create-2",
                "outcome": "독립 상품 등록 검증을 추가한다.",
                "state": "absent",
                "disposition": "planned",
                "evidence": [],
                "requirement_ids": ["CHANGE-PRODUCT-CREATE"],
                "implementation_card_key": "impl-2",
            }
        )
        impl2["body"]["slice"]["behavior_ids"] = ["behavior-product-create-2"]
        graph["cards"][4]["body"]["preserved_behaviors"].append(
            {"id": "behavior-product-create-2", "behavior": "독립 상품 등록 검증을 추가한다."}
        )

        self.assertEqual([], validate_graph(graph, validate_implementation=validate))

    def test_execution_priority_must_match_creation_order(self):
        graph = self.valid_graph()
        graph["cards"][1]["priority"] = 1

        self.assertIn("EXECUTION_PRIORITY_MISMATCH:impl-1", validate_graph(graph))

    def test_phase_rejects_non_todo_active_cards(self):
        graph = self.valid_graph()
        graph["cards"][2]["status"] = "done"
        self.assertIn("DRAFT_ACTIVE_STATUS:review-1", validate_graph(graph))
        graph["cards"][0]["status"] = "done"
        graph["cards"][1]["status"] = "ready"
        self.assertIn("NATIVE_ACTIVE_STATUS:review-1", validate_graph(graph, phase="native"))

    def test_archive_declaration_is_bidirectional(self):
        graph = self.valid_graph()
        graph["cards"][2]["status"] = "archived"
        self.assertIn("ARCHIVED_CARD_UNDECLARED:review-1", validate_graph(graph))

    def test_review_references_are_resolved(self):
        graph = self.valid_graph()
        graph["cards"][2]["body"]["implementation_card_keys"] = ["ghost"]
        self.assertIn("REVIEW_IMPL_NOT_FOUND:review-1:ghost", validate_graph(graph))

    def test_review_v5_materializes_feedback_contract(self):
        graph = self.valid_graph()
        review = graph["cards"][2]["body"]
        review.clear()
        review.update(
            {
                "schema": "aggregate-review-card-v5",
                "triage_digest": graph["triage_digest"],
                "baseline_sha": graph["planning_baseline_sha"],
                "behavior_ids": ["behavior-product-create"],
                "implementation_card_keys": ["impl-1"],
                "acceptance": [
                    {
                        "id": "REVIEW-AC-001",
                        "criterion": "상품 등록 구현 계약과 동작이 충족된다.",
                        "behavior_ids": ["behavior-product-create"],
                        "implementation_card_keys": ["impl-1"],
                        "source_acceptance_ids": ["AC-PRODUCT-CREATE"],
                        "verification_ids": ["FV-PRODUCT-CREATE"],
                    }
                ],
                "review_checks": [
                    {
                        "id": "REVIEW-CHECK-001",
                        "focus": "implementation-convention",
                        "question": "계층별 책임이 backend development guide와 일치하는가?",
                        "expected_outcome": "HTTP, use case와 invariant 책임이 올바른 계층에 남는다.",
                        "behavior_ids": ["behavior-product-create"],
                        "implementation_card_keys": ["impl-1"],
                        "acceptance_ids": ["REVIEW-AC-001"],
                        "source_acceptance_ids": ["AC-PRODUCT-CREATE"],
                        "verification_ids": ["FV-PRODUCT-CREATE"],
                        "guide_locators": [
                            "docs/backend-development-guide.md#Module과 계층",
                            ".hermes/docs/plan.md#PM Workflow 계약 확정 및 KWC 점진 도입 계획",
                        ],
                    },
                    {
                        "id": "REVIEW-CHECK-002",
                        "focus": "test-quality",
                        "question": "Test가 public contract를 적절한 level에서 검증하는가?",
                        "expected_outcome": "성공과 rejection을 관찰 가능한 assertion으로 검증한다.",
                        "behavior_ids": ["behavior-product-create"],
                        "implementation_card_keys": ["impl-1"],
                        "acceptance_ids": ["REVIEW-AC-001"],
                        "source_acceptance_ids": ["AC-PRODUCT-CREATE"],
                        "verification_ids": ["FV-PRODUCT-CREATE"],
                        "guide_locators": ["docs/backend-test-guide.md#11. Test level 선택과 검증"],
                    },
                ],
                "scope_exclusions": ["상품 이미지 업로드와 frontend는 검토 범위에서 제외한다."],
            }
        )

        self.assertEqual([], validate_graph(graph))

    def test_review_v5_requires_complete_acceptance_and_check_coverage(self):
        graph = self.valid_graph()
        review = graph["cards"][2]["body"]

        review["acceptance"][0]["behavior_ids"] = []
        for check in review["review_checks"]:
            check["implementation_card_keys"] = []
            check["source_acceptance_ids"] = []
            check["verification_ids"] = []

        errors = validate_graph(graph)

        self.assertIn("REVIEW_ACCEPTANCE_BEHAVIOR_COVERAGE:review-1", errors)
        self.assertIn("REVIEW_CHECK_IMPL_COVERAGE:review-1", errors)
        self.assertIn("REVIEW_CHECK_SOURCE_ACCEPTANCE_COVERAGE:review-1", errors)
        self.assertIn("REVIEW_CHECK_VERIFICATION_COVERAGE:review-1", errors)

    def test_review_v5_rejects_unknown_refs_duplicate_ids_and_missing_guides(self):
        graph = self.valid_graph()
        review = graph["cards"][2]["body"]
        duplicate = copy.deepcopy(review["review_checks"][0])
        duplicate["behavior_ids"] = ["unknown-behavior"]
        duplicate["source_acceptance_ids"] = ["unknown-acceptance"]
        duplicate["verification_ids"] = ["unknown-verification"]
        duplicate["guide_locators"] = ["docs/architecture.md#Module"]
        review["review_checks"].append(duplicate)

        errors = validate_graph(graph)

        self.assertIn("DUPLICATE_REVIEW_CHECK_ID:review-1:REVIEW-CHECK-001", errors)
        self.assertIn("UNKNOWN_REVIEW_CHECK_BEHAVIOR:review-1:REVIEW-CHECK-001:unknown-behavior", errors)
        self.assertIn("UNKNOWN_REVIEW_CHECK_SOURCE_ACCEPTANCE:review-1:REVIEW-CHECK-001:unknown-acceptance", errors)
        self.assertIn("UNKNOWN_REVIEW_CHECK_VERIFICATION:review-1:REVIEW-CHECK-001:unknown-verification", errors)
        self.assertIn("INVALID_REVIEW_GUIDE_LOCATOR:review-1:REVIEW-CHECK-001:docs/architecture.md#Module", errors)

    def test_review_v5_requires_backend_and_active_plan_guides_across_checks(self):
        graph = self.valid_graph()
        review = graph["cards"][2]["body"]
        review["review_checks"][1]["guide_locators"] = [
            "docs/backend-development-guide.md#변경별 확인 기준"
        ]

        errors = validate_graph(graph)
        review_errors = validate_review_graph(graph)

        self.assertIn("REVIEW_TEST_GUIDE_COVERAGE:review-1", errors)
        self.assertIn("REVIEW_TEST_GUIDE_COVERAGE:review-1", review_errors)
        self.assertNotIn("REVIEW_ACTIVE_PLAN_COVERAGE:review-1", errors)
        self.assertNotIn("REVIEW_ACTIVE_PLAN_COVERAGE:review-1", review_errors)

        review["review_checks"][0]["guide_locators"] = [
            "docs/backend-development-guide.md#Module과 계층"
        ]

        self.assertIn("REVIEW_ACTIVE_PLAN_COVERAGE:review-1", validate_graph(graph))
        self.assertIn("REVIEW_ACTIVE_PLAN_COVERAGE:review-1", validate_review_graph(graph))

    def test_review_v5_rejects_unknown_guide_headings(self):
        graph = self.valid_graph()
        review = graph["cards"][2]["body"]
        review["review_checks"][0]["guide_locators"] = [
            "docs/backend-development-guide.md#DOES-NOT-EXIST"
        ]
        review["review_checks"][1]["guide_locators"] = [
            "docs/backend-test-guide.md#DOES-NOT-EXIST"
        ]

        errors = validate_graph(graph)

        self.assertIn(
            "INVALID_REVIEW_GUIDE_LOCATOR:review-1:REVIEW-CHECK-001:docs/backend-development-guide.md#DOES-NOT-EXIST",
            errors,
        )
        self.assertIn(
            "INVALID_REVIEW_GUIDE_LOCATOR:review-1:REVIEW-CHECK-002:docs/backend-test-guide.md#DOES-NOT-EXIST",
            errors,
        )

    def test_review_v5_guide_locator_allowlist_matches_current_headings(self):
        repository_root = Path(os.environ["HERMES_KWC_TEST_REPOSITORY"])
        actual = set()
        for relative_path in (
            "docs/backend-development-guide.md",
            "docs/backend-test-guide.md",
        ):
            content = (repository_root / relative_path).read_text(encoding="utf-8")
            actual.update(
                f"{relative_path}#{line.lstrip('#').strip()}"
                for line in content.splitlines()
                if line.startswith("#")
            )

        self.assertEqual(actual, REVIEW_GUIDE_LOCATORS)

    def test_review_v5_malformed_reference_lists_fail_closed(self):
        graph = self.valid_graph()
        review = graph["cards"][2]["body"]
        review["acceptance"][0]["behavior_ids"] = None
        review["review_checks"][0]["acceptance_ids"] = None

        errors = validate_graph(graph)

        self.assertIn("INVALID_REVIEW_ACCEPTANCE_BEHAVIORS:review-1:0", errors)
        self.assertIn("INVALID_REVIEW_CHECK_ACCEPTANCE_IDS:review-1:REVIEW-CHECK-001", errors)

    def test_review_v5_references_must_belong_to_the_row_implementation_cards(self):
        graph = self.review_rework_graph()
        review = next(card for card in graph["cards"] if card["key"] == "review-2")["body"]
        review["acceptance"][0]["implementation_card_keys"] = ["impl-1"]
        review["acceptance"][0]["source_acceptance_ids"] = ["AC-PRODUCT-CREATE-2"]
        review["acceptance"][0]["verification_ids"] = ["FV-PRODUCT-CREATE-2"]

        errors = validate_graph(graph)

        self.assertIn(
            "REVIEW_ACCEPTANCE_SOURCE_NOT_OWNED:review-2:REVIEW-AC-001:AC-PRODUCT-CREATE-2",
            errors,
        )
        self.assertIn(
            "REVIEW_ACCEPTANCE_VERIFICATION_NOT_OWNED:review-2:REVIEW-AC-001:FV-PRODUCT-CREATE-2",
            errors,
        )

    def test_review_v5_allows_shared_verification_ids_across_implementation_cards(self):
        graph = self.review_rework_graph()
        impl2 = next(card for card in graph["cards"] if card["key"] == "impl-2")["body"]
        impl2["acceptance"][0]["id"] = "AC-PRODUCT-CREATE"
        impl2["verification"][0]["id"] = "FV-PRODUCT-CREATE"
        impl2["verification"][0]["acceptance_ids"] = ["AC-PRODUCT-CREATE"]

        errors = validate_graph(graph)

        self.assertIn("DUPLICATE_REVIEW_SOURCE_ACCEPTANCE_ID:review-2:AC-PRODUCT-CREATE", errors)
        self.assertNotIn("DUPLICATE_REVIEW_SOURCE_VERIFICATION_ID:review-2:FV-PRODUCT-CREATE", errors)
        self.assertNotIn(
            "REVIEW_ACCEPTANCE_VERIFICATION_NOT_OWNED:review-2:REVIEW-AC-001:FV-PRODUCT-CREATE",
            errors,
        )

    def test_review_only_validation_ignores_unrelated_reconstructed_graph_findings(self):
        graph = self.valid_graph()
        graph["cards"][1]["priority"] = 1

        self.assertIn("EXECUTION_PRIORITY_MISMATCH:impl-1", validate_graph(graph))
        self.assertEqual([], validate_review_graph(graph, validate_implementation=validate))

    def test_rejects_low_quality_review_semantic_text(self):
        graph = self.valid_graph()
        review = graph["cards"][2]["body"]
        review["acceptance"][0]["criterion"] = "확인한다."
        review["review_checks"][0]["question"] = "맞는가?"
        review["review_checks"][0]["expected_outcome"] = "맞다."

        errors = validate_graph(graph, validate_implementation=validate)
        fast_path_errors = validate_review_graph(graph, validate_implementation=validate)

        expected = {
            "LOW_QUALITY_REVIEW_ACCEPTANCE_CRITERION:review-1:0",
            "LOW_QUALITY_REVIEW_CHECK_QUESTION:review-1:REVIEW-CHECK-001",
            "LOW_QUALITY_REVIEW_CHECK_EXPECTED_OUTCOME:review-1:REVIEW-CHECK-001",
        }
        self.assertTrue(expected <= set(errors))
        self.assertTrue(expected <= set(fast_path_errors))

    def test_review_is_bound_to_graph_planning_baseline(self):
        graph = self.valid_graph()
        graph["cards"][2]["body"]["baseline_sha"] = "f" * 40
        self.assertIn("REVIEW_BASELINE_MISMATCH:review-1", validate_graph(graph))

    def test_review_is_bound_to_frozen_triage(self):
        graph = self.valid_graph()
        graph["cards"][2]["body"]["triage_digest"] = "f" * 64
        self.assertIn("REVIEW_TRIAGE_DIGEST_MISMATCH:review-1", validate_graph(graph))

    def test_review_requires_valid_baseline_sha(self):
        graph = self.valid_graph()
        graph["cards"][2]["body"]["baseline_sha"] = "not-a-sha"
        errors = validate_graph(graph)
        self.assertIn("INVALID_REVIEW_BASELINE:review-1", errors)
        self.assertIn("REVIEW_BASELINE_MISMATCH:review-1", errors)

    def test_review_rejects_legacy_execution_metadata(self):
        graph = self.valid_graph()
        review = graph["cards"][2]["body"]
        review["review_axes"] = ["spec"]
        review["verification"] = {"runner": "gradle-mcp", "CHECK": "test", "CWD": ".", "EXPECT": "BUILD SUCCESSFUL"}
        review["inherited_behavior_ids"] = []
        errors = validate_graph(graph)
        self.assertIn("UNEXPECTED_REVIEW_BODY_FIELD:review-1:review_axes", errors)
        self.assertIn("UNEXPECTED_REVIEW_BODY_FIELD:review-1:verification", errors)
        self.assertIn("UNEXPECTED_REVIEW_BODY_FIELD:review-1:inherited_behavior_ids", errors)

    def test_execution_cards_reject_ephemeral_coordinator_phase_text(self):
        graph = self.valid_graph()
        review = next(card for card in graph["cards"] if card["card_type"] == "review")
        review["body"]["acceptance"][0]["criterion"] = (
            "이번 graph recovery phase 2에서는 worker claim과 Review 실행을 수행하지 않는다."
        )

        errors = validate_graph(graph)

        self.assertIn("EPHEMERAL_COORDINATOR_CONTEXT:review-1", errors)

    def test_summary_v3_keeps_triage_based_human_summary_without_runtime_evidence(self):
        graph = self.valid_graph()
        summary = next(card for card in graph["cards"] if card["card_type"] == "summary")
        self.assertEqual([], validate_graph(graph))
        self.assertEqual(graph["issue"]["number"], summary["body"]["issue"]["number"])
        self.assertEqual(
            [row["id"] for row in graph["change_requirements"]],
            [row["id"] for row in summary["body"]["target_outcomes"]],
        )
        self.assertEqual(
            [row["id"] for row in graph["behaviors"]],
            [row["id"] for row in summary["body"]["preserved_behaviors"]],
        )

        summary["body"]["triage_digest"] = "f" * 64
        self.assertIn("SUMMARY_TRIAGE_DIGEST_MISMATCH", validate_graph(graph))

        summary = next(card for card in self.valid_graph()["cards"] if card["card_type"] == "summary")
        summary["body"]["final_implementation_sha"] = "a" * 40
        self.assertIn(
            "UNEXPECTED_SUMMARY_BODY_FIELD:summary:final_implementation_sha",
            validate_graph(self.valid_graph() | {"cards": [
                card if card["card_type"] != "summary" else summary
                for card in self.valid_graph()["cards"]
            ]}),
        )

    def test_summary_rejects_dynamic_finalization_copy(self):
        graph = self.valid_graph()
        summary = next(card for card in graph["cards"] if card["card_type"] == "summary")
        summary["body"].update(
            {
                "effective_behavior_ids": ["behavior-product-create"],
                "aggregate_acceptance": ["상품 등록 요구사항 전체가 승인되었다."],
                "finalization_checks": ["모든 직접 parent가 완료되었다."],
            }
        )

        errors = validate_graph(graph)
        self.assertIn("UNEXPECTED_SUMMARY_BODY_FIELD:summary:effective_behavior_ids", errors)
        self.assertIn("UNEXPECTED_SUMMARY_BODY_FIELD:summary:aggregate_acceptance", errors)
        self.assertIn("UNEXPECTED_SUMMARY_BODY_FIELD:summary:finalization_checks", errors)

    def test_title_type_assignee_and_workspace_must_match_native_identity(self):
        graph = self.valid_graph()
        graph["cards"][1]["title"] = "G1-Issue138-Review9"
        graph["cards"][1]["assignee"] = "project-manager"
        graph["cards"][1]["workspace"] = "other"
        errors = validate_graph(graph)
        self.assertIn("TITLE_CARD_TYPE_MISMATCH:impl-1", errors)
        self.assertIn("ASSIGNEE_MISMATCH:impl-1", errors)
        self.assertIn("WORKSPACE_MISMATCH:impl-1", errors)

    def test_review_rework_requires_same_generation_source_provenance(self):
        graph = self.valid_graph()
        graph.update(
            {
                "mode": "review-rework",
                "lineage": {"prior_generation": 1, "prior_summary": "t_summary", "archived_unfinished_keys": []},
                "source_review": {
                    "review_key": "review-1",
                    "dispositions": [{"finding_id": "F-1", "verdict": "correction-required", "appended_card_keys": ["impl-1"]}],
                    "idempotency": {"impl-1": "review-1:F-1:correction"},
                },
            }
        )
        self.assertNotIn("MISSING_SOURCE_REVIEW", validate_graph(graph))
        graph["source_review"] = None
        self.assertIn("MISSING_SOURCE_REVIEW", validate_graph(graph))

    def test_all_implemented_graph_promotes_review_without_impl(self):
        graph = self.valid_graph()
        behavior = graph["behaviors"][0]
        behavior.update({"state": "implemented", "disposition": "inherited", "evidence": ["source:test"]})
        behavior.pop("implementation_card_key")
        graph["cards"] = [card for card in graph["cards"] if card["key"] != "impl-1"]
        review = next(card for card in graph["cards"] if card["key"] == "review-1")
        review["parents"] = ["triage"]
        review["body"]["implementation_card_keys"] = []
        review["body"]["acceptance"][0]["implementation_card_keys"] = []
        review["body"]["acceptance"][0]["source_acceptance_ids"] = []
        review["body"]["acceptance"][0]["verification_ids"] = []
        review["body"]["review_checks"][0]["focus"] = "preservation"
        for check in review["body"]["review_checks"]:
            check["implementation_card_keys"] = []
            check["source_acceptance_ids"] = []
            check["verification_ids"] = []
        summary = next(card for card in graph["cards"] if card["key"] == "summary")
        summary["parents"] = ["review-1"]
        review["priority"] = 1
        graph["execution_order"] = ["review-1"]
        graph["ready_candidate"] = "review-1"
        self.assertEqual([], validate_graph(graph))
        graph["cards"][0]["status"] = "done"
        review["status"] = "ready"
        self.assertEqual([], validate_graph(graph, phase="native"))

    def test_review_rework_validates_historical_and_appended_graph(self):
        graph = self.valid_graph()
        triage, impl1, review1, summary = graph["cards"]
        triage["status"] = impl1["status"] = review1["status"] = "done"
        impl2 = copy.deepcopy(impl1)
        impl2.update({"key": "impl-2", "title": "G1-Issue138-Impl2", "status": "todo", "priority": 2, "parents": ["review-1"]})
        impl2["body"]["acceptance"][0]["id"] = "AC-PRODUCT-CREATE-2"
        impl2["body"]["verification"][0]["id"] = "FV-PRODUCT-CREATE-2"
        impl2["body"]["verification"][0]["acceptance_ids"] = ["AC-PRODUCT-CREATE-2"]
        review2 = copy.deepcopy(review1)
        review2.update({"key": "review-2", "title": "G1-Issue138-Review2", "status": "todo", "priority": 1, "parents": ["impl-2"]})
        self.bind_review_contract(
            review2,
            ["behavior-product-create"],
            ["impl-1", "impl-2"],
            ["AC-PRODUCT-CREATE", "AC-PRODUCT-CREATE-2"],
            ["FV-PRODUCT-CREATE", "FV-PRODUCT-CREATE-2"],
        )
        summary["parents"] = ["impl-1", "review-1", "impl-2", "review-2"]
        graph["cards"] = [triage, impl1, review1, impl2, review2, summary]
        graph.update(
            {
                "mode": "review-rework",
                "lineage": {"prior_generation": 1, "prior_summary": "summary", "archived_unfinished_keys": []},
                "source_review": {
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
                },
                "execution_order": ["impl-2", "review-2"],
                "ready_candidate": "impl-2",
            }
        )
        result = self.source_review_result(graph)
        self.assertEqual([], validate_graph(graph, source_review_result=result))
        impl2["status"] = "ready"
        self.assertEqual([], validate_graph(graph, phase="native", source_review_result=result))

    def test_review_rework_rejects_null_candidate_with_appended_impl(self):
        graph = self.review_rework_graph()
        graph["ready_candidate"] = None
        errors = validate_graph(graph, phase="native")
        self.assertIn("REVIEW_REWORK_ACTIVATION_MISMATCH", errors)

    def test_decision_only_review_rework_has_no_ready_card(self):
        graph = self.review_rework_graph()
        triage, impl1, review1, _, _, summary = graph["cards"]
        decision = {
            "key": "decision-1",
            "title": "G1-Issue138-Decision1",
            "card_type": "decision",
            "assignee": "project-manager",
            "skills": [],
            "workspace": "scratch",
            "status": "blocked",
            "priority": 0,
            "parents": ["review-1"],
            "body": {
                "schema": "policy-decision-card-v1",
                "source_review_key": "review-1",
                "finding_ids": ["F-1"],
                "question": "사용자가 정책을 결정해야 한다.",
                "decision_owner": "user",
            },
        }
        summary["parents"] = ["impl-1", "review-1", "decision-1"]
        graph["cards"] = [triage, impl1, review1, decision, summary]
        graph["source_review"] = {
            "review_key": "review-1",
            "dispositions": [
                {"finding_id": "F-1", "verdict": "decision-required", "appended_card_keys": ["decision-1"]}
            ],
            "idempotency": {"decision-1": "review-1:F-1:decision"},
        }
        graph["execution_order"] = []
        graph["ready_candidate"] = None
        self.assertEqual(
            [],
            validate_graph(
                graph,
                phase="native",
                source_review_result=self.source_review_result(graph),
            ),
        )

    def test_review_rework_is_bound_to_canonical_source_result(self):
        graph = self.review_rework_graph()
        result = self.source_review_result(graph)
        result["findings"][0]["finding_id"] = "OTHER"
        self.assertIn(
            "SOURCE_REVIEW_DISPOSITION_MISMATCH",
            validate_graph(graph, phase="native", source_review_result=result),
        )

    def test_review_rework_rejects_truncated_canonical_result(self):
        graph = self.review_rework_graph()
        result = self.source_review_result(graph)
        result.pop("review_run_id")
        self.assertTrue(
            any(
                error.startswith("INVALID_CANONICAL_SOURCE_REVIEW_RESULT:")
                for error in validate_graph(graph, phase="native", source_review_result=result)
            )
        )

    def test_summary_rejects_extra_parent(self):
        graph = self.valid_graph()
        graph["cards"][-1]["parents"].append("triage")
        self.assertIn("SUMMARY_MEMBERSHIP_MISMATCH", validate_graph(graph))

    def test_implementation_requires_review_child(self):
        graph = self.valid_graph()
        graph["cards"][2]["parents"] = ["triage"]
        self.assertIn("IMPLEMENTATION_WITHOUT_REVIEW_CHILD:impl-1", validate_graph(graph))

    def test_all_implemented_graph_rejects_unnecessary_impl(self):
        graph = self.valid_graph()
        behavior = graph["behaviors"][0]
        behavior.update({"state": "implemented", "disposition": "inherited", "evidence": ["source:test"]})
        behavior.pop("implementation_card_key")
        self.assertIn("IMPLEMENTATION_BEHAVIOR_COVERAGE", validate_graph(graph))

    def test_graph_identity_requires_url_sha_and_unique_title(self):
        graph = self.valid_graph()
        graph["issue"].pop("url")
        graph["requirement_basis"]["revision"] = "not-a-sha"
        graph["cards"][2]["title"] = graph["cards"][1]["title"]
        errors = validate_graph(graph)
        self.assertIn("INVALID_ISSUE_URL", errors)
        self.assertIn("INVALID_REQUIREMENT_BASIS_REVISION", errors)
        self.assertTrue(any(error.startswith("DUPLICATE_CARD_TITLE:") for error in errors))

    def test_requirement_diff_is_stable_and_keeps_behavior_decision_manual(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
            requirement = root / "requirement.md"
            requirement.write_text("# 정책\n기존 동작\n", encoding="utf-8")
            subprocess.run(["git", "add", "requirement.md"], cwd=root, check=True)
            subprocess.run(["git", "commit", "-qm", "base"], cwd=root, check=True)
            base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, check=True, text=True, capture_output=True).stdout.strip()
            requirement.write_text("# 정책\n변경된 동작\n", encoding="utf-8")
            subprocess.run(["git", "add", "requirement.md"], cwd=root, check=True)
            subprocess.run(["git", "commit", "-qm", "revised"], cwd=root, check=True)
            revised = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, check=True, text=True, capture_output=True).stdout.strip()
            first = requirement_diff(root, base, revised, ["requirement.md"])
            second = requirement_diff(root, base, revised, ["requirement.md"])
            self.assertEqual(first["hunks"], second["hunks"])
            self.assertEqual([], first["behavior_deltas"])
            self.assertEqual("human-review-required", first["behavior_delta_status"])

    def test_cli_validates_graph_fixture(self):
        completed = subprocess.run(
            [sys.executable, str(SKILL_DIR / "scripts" / "build_task_graph.py"), "validate-graph", str(self.valid_path)],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual({"valid": True, "errors": []}, json.loads(completed.stdout))


class NativeE2EContractTest(unittest.TestCase):
    def test_frozen_triage_digest_is_bound_to_graph_and_all_consumer_bodies(self):
        fixture = json.loads(
            (SKILL_DIR / "tests" / "fixtures" / "valid-graph-draft.json").read_text(encoding="utf-8")
        )
        digest = "a" * 64

        bind_frozen_triage_digest(fixture, {"frozen_digest": digest})

        self.assertEqual(digest, fixture["triage_digest"])
        consumer_bodies = [card["body"] for card in fixture["cards"] if isinstance(card.get("body"), dict)]
        self.assertTrue(consumer_bodies)
        self.assertTrue(all(body["triage_digest"] == digest for body in consumer_bodies))


if __name__ == "__main__":
    unittest.main()
