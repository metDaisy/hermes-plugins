#!/usr/bin/env python3
"""Fail-closed PM-only activation for an already materialized task graph.

This wrapper intentionally owns only the final native transitions.  It does not
create cards, edit planning bodies, or invent policy.  A failed preflight or
native transition stops the sequence before the next mutation is attempted.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
SKILLS_ROOT = SCRIPT_DIR.parents[1]
BUILD_SCRIPTS = SKILLS_ROOT / "kwc-build-task-graph" / "scripts"
TRIAGE_SCRIPTS = SKILLS_ROOT / "kwc-create-triage" / "scripts"
sys.path[:0] = [str(BUILD_SCRIPTS), str(TRIAGE_SCRIPTS)]

from build_task_graph import validate as validate_implementation  # noqa: E402
from graph_contract import validate_graph, validate_native_readback  # noqa: E402
from triage import validate as validate_triage  # noqa: E402
from triage import decision_request_ids as covered_decision_request_ids  # noqa: E402
from triage import validate_decision_alignment  # noqa: E402
from workflow import validate_review_admission  # noqa: E402

PM_PROFILE = "project-manager"
TRIAGE_TRANSITION_SCRIPT = SCRIPT_DIR / "transition_triage.py"


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_report(path: Path | None, payload: dict[str, Any]) -> None:
    encoded = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    if path is None:
        print(encoded, end="")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(encoded, encoding="utf-8")


def _parse_show(payload: str) -> dict[str, Any]:
    parsed = json.loads(payload)
    if not isinstance(parsed, dict):
        raise ValueError("KANBAN_SHOW_NOT_OBJECT")
    task = parsed.get("task", parsed)
    if not isinstance(task, dict):
        raise ValueError("KANBAN_SHOW_MISSING_TASK")
    parents = parsed.get("parents", task.get("parents", []))
    if not isinstance(parents, list):
        raise ValueError("KANBAN_SHOW_INVALID_PARENTS")
    runs = parsed.get("runs", [])
    if not isinstance(runs, list):
        raise ValueError("KANBAN_SHOW_INVALID_RUNS")
    comments = parsed.get("comments", [])
    if not isinstance(comments, list):
        raise ValueError("KANBAN_SHOW_INVALID_COMMENTS")
    return {"task": task, "parents": parents, "runs": runs, "comments": comments}


def _run_kanban(
    hermes_bin: str,
    board: str,
    arguments: list[str],
    *,
    runner=subprocess.run,
) -> subprocess.CompletedProcess[str]:
    return runner(
        [hermes_bin, "kanban", "--board", board, *arguments],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def _hermes_python(hermes_bin: str) -> str:
    """Resolve the interpreter that owns the Hermes CLI installation."""
    executable = shutil.which(hermes_bin)
    if executable is None:
        return sys.executable
    runtime_root = _hermes_runtime_root(hermes_bin)
    if runtime_root is not None:
        install_key = hashlib.sha256(str(runtime_root.resolve()).encode("utf-8")).hexdigest()[:16]
        facts_candidates = [
            runtime_root.parent / "installs" / install_key / "facts.json",
            *(parent / "facts.json" for parent in runtime_root.parents),
        ]
        for facts_path in facts_candidates:
            try:
                facts = json.loads(facts_path.read_text(encoding="utf-8-sig"))
                environment = Path(facts["packages"]["venv"]["environment"]).resolve()
            except (FileNotFoundError, KeyError, OSError, TypeError, ValueError):
                continue
            generations = facts_path.parent / "environments"
            python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
            if (
                environment.is_relative_to(generations.resolve())
                and (environment / "pyvenv.cfg").is_file()
                and python.is_file()
            ):
                return str(python)
    scripts_dir = Path(executable).resolve().parent
    candidates = [scripts_dir / "python.exe", scripts_dir / "python3", scripts_dir / "python"]
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return sys.executable


def _hermes_runtime_root(hermes_bin: str) -> Path | None:
    executable = shutil.which(hermes_bin)
    if executable is None:
        return None
    resolved = Path(executable).resolve()
    for candidate in (resolved.parent, *resolved.parents):
        if (candidate / "hermes_cli").is_dir() and (candidate / "pm").is_dir():
            return candidate
    return None


def _transition_triage(
    hermes_bin: str,
    board: str,
    task_id: str,
    *,
    runner=subprocess.run,
) -> subprocess.CompletedProcess[str]:
    """Use Hermes's canonical no-field triage transition without an LLM rewrite."""
    environment = os.environ.copy()
    runtime_root = _hermes_runtime_root(hermes_bin)
    if runtime_root is not None:
        existing_pythonpath = environment.get("PYTHONPATH")
        environment["PYTHONPATH"] = (
            str(runtime_root)
            if not existing_pythonpath
            else str(runtime_root) + os.pathsep + existing_pythonpath
        )
    return runner(
        [
            _hermes_python(hermes_bin),
            str(TRIAGE_TRANSITION_SCRIPT),
            "--board",
            board,
            "--task-id",
            task_id,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        env=environment,
    )


def _show(hermes_bin: str, board: str, task_id: str, *, runner=subprocess.run) -> dict[str, Any]:
    completed = _run_kanban(hermes_bin, board, ["show", task_id, "--json"], runner=runner)
    if completed.returncode != 0:
        raise RuntimeError(f"KANBAN_SHOW_FAILED:{task_id}:{completed.returncode}")
    return _parse_show(completed.stdout)


def _body(task: dict[str, Any]) -> dict[str, Any]:
    raw = task.get("body")
    if not isinstance(raw, str):
        raise ValueError("NATIVE_BODY_NOT_TEXT")
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise ValueError("NATIVE_BODY_NOT_OBJECT")
    return parsed


def _has_successful_completion(envelope: dict[str, Any]) -> bool:
    return any(
        isinstance(run, dict)
        and run.get("status") in {"done", "completed"}
        and run.get("outcome") == "completed"
        for run in envelope.get("runs", [])
    )


def _native_id_by_key(graph: dict[str, Any], readback: list[Any]) -> dict[str, str]:
    by_title: dict[str, str] = {}
    for envelope in readback:
        if not isinstance(envelope, dict):
            continue
        task = envelope.get("task", envelope)
        if isinstance(task, dict) and isinstance(task.get("title"), str) and isinstance(task.get("id"), str):
            by_title[task["title"]] = task["id"]
    result: dict[str, str] = {}
    for card in graph.get("cards", []):
        if isinstance(card, dict) and isinstance(card.get("key"), str) and isinstance(card.get("title"), str):
            task_id = by_title.get(card["title"])
            if task_id is not None:
                result[card["key"]] = task_id
    return result


def _graph_card(graph: dict[str, Any], key: str) -> dict[str, Any] | None:
    for card in graph.get("cards", []):
        if isinstance(card, dict) and card.get("key") == key:
            return card
    return None


def _review_admission_errors(
    graph: dict[str, Any],
    expected_readback: list[Any],
    activation_key: str,
    admission: dict[str, Any] | None,
) -> list[str]:
    target = _graph_card(graph, activation_key)
    if not isinstance(target, dict) or target.get("card_type") != "review":
        return []
    if admission is None:
        return ["REVIEW_ADMISSION_REQUIRED"]

    errors = validate_review_admission(admission)
    native_ids = _native_id_by_key(graph, expected_readback)
    if admission.get("task_id") != native_ids.get(activation_key):
        errors.append("REVIEW_ADMISSION_TASK_MISMATCH")
    body = target.get("body")
    if not isinstance(body, dict):
        errors.append("REVIEW_CARD_BODY_INVALID")
        return sorted(set(errors))
    if admission.get("baseline_sha") != body.get("baseline_sha"):
        errors.append("REVIEW_ADMISSION_BASELINE_MISMATCH")
    expected_keys = body.get("implementation_card_keys")
    checkpoints = admission.get("checkpoints")
    if isinstance(expected_keys, list) and isinstance(checkpoints, list):
        actual_keys = [
            checkpoint.get("implementation_card_key")
            for checkpoint in checkpoints
            if isinstance(checkpoint, dict)
        ]
        if actual_keys != expected_keys:
            errors.append("REVIEW_ADMISSION_CHECKPOINT_KEYS_MISMATCH")
        for checkpoint in checkpoints:
            if not isinstance(checkpoint, dict):
                continue
            key = checkpoint.get("implementation_card_key")
            if checkpoint.get("implementation_task_id") != native_ids.get(key):
                errors.append(f"REVIEW_ADMISSION_CHECKPOINT_TASK_MISMATCH:{key}")
    return sorted(set(errors))


def _read_current_graph(
    graph: dict[str, Any],
    expected_readback: list[Any],
    hermes_bin: str,
    board: str,
    *,
    runner=subprocess.run,
) -> list[dict[str, Any]]:
    ids = _native_id_by_key(graph, expected_readback)
    current: list[dict[str, Any]] = []
    for card in graph.get("cards", []):
        if not isinstance(card, dict) or not isinstance(card.get("key"), str):
            continue
        task_id = ids.get(card["key"])
        if task_id is None:
            raise ValueError(f"NATIVE_TASK_ID_MISSING:{card.get('key')}")
        current.append(_show(hermes_bin, board, task_id, runner=runner))
    return current


def _with_native_statuses(graph: dict[str, Any], readback: list[dict[str, Any]]) -> dict[str, Any]:
    observed = copy.deepcopy(graph)
    statuses: dict[str, Any] = {}
    for envelope in readback:
        task = envelope.get("task", envelope)
        if isinstance(task, dict) and isinstance(task.get("title"), str):
            statuses[task["title"]] = task.get("status")
    for card in observed.get("cards", []):
        if isinstance(card, dict) and isinstance(card.get("title"), str):
            card["status"] = statuses.get(card["title"], card.get("status"))
    return observed


def _preflight(
    graph: dict[str, Any],
    expected_readback: list[Any],
    triage_task_id: str,
    decision_task_ids: list[str],
    activation_key: str,
    hermes_bin: str,
    board: str,
    review_admission: dict[str, Any] | None,
    *,
    runner=subprocess.run,
) -> tuple[list[str], dict[str, Any]]:
    errors = validate_graph(graph, phase="draft", validate_implementation=validate_implementation)
    errors.extend(validate_native_readback(graph, expected_readback))
    errors.extend(_review_admission_errors(graph, expected_readback, activation_key, review_admission))
    details: dict[str, Any] = {"triage_task_id": triage_task_id, "decision_task_ids": decision_task_ids}
    expected_statuses: dict[str, Any] = {}
    for envelope in expected_readback:
        if not isinstance(envelope, dict):
            continue
        task = envelope.get("task", envelope)
        if isinstance(task, dict) and isinstance(task.get("title"), str):
            expected_statuses[task["title"]] = task.get("status")
    graph_triage = next(
        (
            card
            for card in graph.get("cards", [])
            if isinstance(card, dict) and card.get("card_type") == "triage"
        ),
        None,
    )
    graph_target = _graph_card(graph, activation_key)
    try:
        triage_envelope = _show(hermes_bin, board, triage_task_id, runner=runner)
        triage_task = triage_envelope["task"]
        triage_status = triage_task.get("status")
        if triage_status not in {"triage", "todo", "ready", "done"}:
            errors.append("TRIAGE_NOT_NATIVE_TRIAGE_OR_DONE")
        if triage_status == "done" and not _has_successful_completion(triage_envelope):
            errors.append("TRIAGE_DONE_WITHOUT_SUCCESSFUL_RUN")
        triage_body = _body(triage_task)
        validation_status = "triage" if triage_status in {"todo", "ready"} else triage_status
        errors.extend(validate_triage(triage_body, validation_status))
        details["triage_status"] = triage_task.get("status")
        if (
            triage_status == "done"
            and isinstance(graph_triage, dict)
            and isinstance(graph_target, dict)
            and expected_statuses.get(graph_triage.get("title")) == "done"
            and expected_statuses.get(graph_target.get("title")) in {"ready", "running", "review"}
        ):
            errors.append("RECOVERY_REQUIRES_FRESH_TRIAGE_GATE")
    except (RuntimeError, ValueError, json.JSONDecodeError) as exc:
        errors.append(str(exc))
        triage_body = None

    if isinstance(triage_body, dict) and triage_body.get("schema") == "triage-v7":
        if graph.get("triage_digest") != triage_body.get("frozen_digest"):
            errors.append("TRIAGE_GRAPH_DIGEST_MISMATCH")
        intent = triage_body.get("intent")
        change_requirements = intent.get("change_requirements") if isinstance(intent, dict) else None
        if graph.get("change_requirements") != change_requirements:
            errors.append("TRIAGE_GRAPH_REQUIREMENTS_MISMATCH")

    requested_ids: set[str] = set()
    if isinstance(triage_body, dict):
        for request in triage_body.get("open_decisions", []):
            if isinstance(request, dict) and isinstance(request.get("id"), str):
                requested_ids.add(request["id"])

    decision_request_ids: set[str] = set()
    for task_id in decision_task_ids:
        try:
            envelope = _show(hermes_bin, board, task_id, runner=runner)
            task = envelope["task"]
            decision_body = _body(task)
            decision_status = task.get("status")
            if decision_status not in {"running", "done"}:
                errors.append(f"DECISION_NOT_RUNNING_OR_DONE:{task_id}")
            if decision_status == "done" and not _has_successful_completion(envelope):
                errors.append(f"DECISION_DONE_WITHOUT_SUCCESSFUL_RUN:{task_id}")
            entries = decision_body.get("decision_requests")
            approved = (
                isinstance(entries, list)
                and bool(entries)
                and all(isinstance(entry, dict) and entry.get("decision_status") == "approved" for entry in entries)
            ) or decision_body.get("decision_status") == "approved"
            if not approved:
                errors.append(f"DECISION_NOT_APPROVED:{task_id}")
            if isinstance(triage_body, dict):
                errors.extend(validate_decision_alignment(triage_body, decision_body))
            decision_request_ids.update(covered_decision_request_ids(decision_body))
        except (RuntimeError, ValueError, json.JSONDecodeError) as exc:
            errors.append(str(exc))
    if requested_ids != decision_request_ids:
        errors.append("TRIAGE_DECISION_CARDS_MISMATCH")

    candidate = copy.deepcopy(graph)
    triage_card = next((card for card in candidate.get("cards", []) if isinstance(card, dict) and card.get("card_type") == "triage"), None)
    target = _graph_card(candidate, activation_key)
    if not isinstance(triage_card, dict):
        errors.append("GRAPH_TRIAGE_MISSING")
    elif isinstance(target, dict):
        triage_card["status"] = "done"
        target["status"] = "ready"
        errors.extend(validate_graph(candidate, phase="native", validate_implementation=validate_implementation))
    else:
        errors.append("ACTIVATION_CARD_MISSING")
    details["activation_key"] = activation_key
    return sorted(set(errors)), details


def activate(
    graph_path: Path,
    native_readback_path: Path,
    triage_task_id: str,
    decision_task_ids: list[str],
    activation_key: str,
    hermes_bin: str,
    board: str,
    *,
    apply: bool,
    review_admission: dict[str, Any] | None = None,
    runner=subprocess.run,
) -> dict[str, Any]:
    graph = _load_json(graph_path)
    readback = _load_json(native_readback_path)
    if not isinstance(graph, dict) or not isinstance(readback, list):
        return {"valid": False, "errors": ["INVALID_ACTIVATION_INPUT"], "mutated": False}
    errors, details = _preflight(
        graph,
        readback,
        triage_task_id,
        decision_task_ids,
        activation_key,
        hermes_bin,
        board,
        review_admission,
        runner=runner,
    )
    if errors or not apply:
        return {"valid": not errors, "errors": errors, "mutated": False, "details": details}

    try:
        current_readback = _read_current_graph(graph, readback, hermes_bin, board, runner=runner)
    except (RuntimeError, ValueError, json.JSONDecodeError) as exc:
        return {"valid": False, "errors": [str(exc)], "mutated": False, "details": details}
    live_errors = validate_native_readback(graph, current_readback)
    partial_triage_recovery = False
    if live_errors:
        recovery_graph = copy.deepcopy(graph)
        recovery_triage = next(
            (
                card
                for card in recovery_graph.get("cards", [])
                if isinstance(card, dict) and card.get("card_type") == "triage"
            ),
            None,
        )
        live_triage = next(
            (
                envelope.get("task", envelope)
                for envelope in current_readback
                if isinstance(envelope, dict)
                and isinstance(envelope.get("task", envelope), dict)
                and envelope.get("task", envelope).get("title")
                == (recovery_triage or {}).get("title")
            ),
            None,
        )
        live_triage_status = live_triage.get("status") if isinstance(live_triage, dict) else None
        if (
            isinstance(recovery_triage, dict)
            and recovery_triage.get("status") == "triage"
            and live_triage_status in {"todo", "ready", "done"}
        ):
            recovery_triage["status"] = live_triage_status
            recovery_errors = validate_native_readback(recovery_graph, current_readback)
            partial_triage_recovery = not recovery_errors
        if not partial_triage_recovery:
            return {
                "valid": False,
                "errors": [f"LIVE_GRAPH_DRIFT:{error}" for error in live_errors],
                "mutated": False,
                "details": details,
            }
    details["partial_triage_recovery"] = partial_triage_recovery

    target_id = _native_id_by_key(graph, readback).get(activation_key)
    if target_id is None:
        return {"valid": False, "errors": ["ACTIVATION_TASK_ID_MISSING"], "mutated": False, "details": details}
    mutated = False
    if review_admission is not None:
        encoded_admission = json.dumps(review_admission, ensure_ascii=False, sort_keys=True)
        try:
            target_envelope = _show(hermes_bin, board, target_id, runner=runner)
        except (RuntimeError, ValueError, json.JSONDecodeError) as exc:
            return {"valid": False, "errors": [str(exc)], "mutated": False, "details": details}
        present = any(
            isinstance(comment, dict)
            and comment.get("author") == PM_PROFILE
            and comment.get("body") == encoded_admission
            for comment in target_envelope.get("comments", [])
        )
        if not present:
            commented = _run_kanban(
                hermes_bin,
                board,
                ["comment", target_id, encoded_admission, "--author", PM_PROFILE],
                runner=runner,
            )
            if commented.returncode != 0:
                return {
                    "valid": False,
                    "errors": [f"REVIEW_ADMISSION_COMMENT_FAILED:{commented.returncode}"],
                    "mutated": False,
                    "details": details,
                }
            mutated = True
            try:
                target_envelope = _show(hermes_bin, board, target_id, runner=runner)
            except (RuntimeError, ValueError, json.JSONDecodeError) as exc:
                return {"valid": False, "errors": [str(exc)], "mutated": True, "details": details}
            if not any(
                isinstance(comment, dict)
                and comment.get("author") == PM_PROFILE
                and comment.get("body") == encoded_admission
                for comment in target_envelope.get("comments", [])
            ):
                return {
                    "valid": False,
                    "errors": ["REVIEW_ADMISSION_COMMENT_READBACK_FAILED"],
                    "mutated": True,
                    "details": details,
                }

    pending_decision_ids: list[str] = []
    for task_id in decision_task_ids:
        try:
            current = _show(hermes_bin, board, task_id, runner=runner)["task"]
        except (RuntimeError, ValueError, json.JSONDecodeError) as exc:
            return {"valid": False, "errors": [str(exc)], "mutated": False, "details": details}
        if current.get("status") == "running":
            pending_decision_ids.append(task_id)

    try:
        triage_status = _show(hermes_bin, board, triage_task_id, runner=runner)["task"].get("status")
    except (RuntimeError, ValueError, json.JSONDecodeError) as exc:
        return {"valid": False, "errors": [str(exc)], "mutated": False, "details": details}
    mutated = mutated or bool(pending_decision_ids)
    if pending_decision_ids:
        completed = _run_kanban(
            hermes_bin,
            board,
            ["complete", "--result", "planning decision approved and graph materialized", *pending_decision_ids],
            runner=runner,
        )
        if completed.returncode != 0:
            diagnostic = (completed.stderr or completed.stdout).strip().replace("\n", " ")
            return {
                "valid": False,
                "errors": [f"NATIVE_DECISION_COMPLETE_FAILED:{completed.returncode}:{diagnostic}"],
                "mutated": True,
                "details": details,
            }
    for task_id in pending_decision_ids:
        try:
            current = _show(hermes_bin, board, task_id, runner=runner)["task"]
            if current.get("status") != "done":
                return {
                    "valid": False,
                    "errors": [f"NATIVE_COMPLETE_READBACK_FAILED:{task_id}"],
                    "mutated": True,
                    "details": details,
                }
        except (RuntimeError, ValueError, json.JSONDecodeError) as exc:
            return {"valid": False, "errors": [str(exc)], "mutated": True, "details": details}

    if triage_status == "triage":
        try:
            before = _show(hermes_bin, board, triage_task_id, runner=runner)["task"]
        except (RuntimeError, ValueError, json.JSONDecodeError) as exc:
            return {"valid": False, "errors": [str(exc)], "mutated": mutated, "details": details}
        specified = _transition_triage(hermes_bin, board, triage_task_id, runner=runner)
        mutated = True
        if specified.returncode != 0:
            diagnostic = (specified.stderr or specified.stdout).strip().replace("\n", " ")
            return {
                "valid": False,
                "errors": [f"NATIVE_TRIAGE_TRANSITION_FAILED:{specified.returncode}:{diagnostic}"],
                "mutated": True,
                "details": details,
            }
        try:
            after = _show(hermes_bin, board, triage_task_id, runner=runner)["task"]
        except (RuntimeError, ValueError, json.JSONDecodeError) as exc:
            return {"valid": False, "errors": [str(exc)], "mutated": True, "details": details}
        if after.get("status") not in {"ready", "todo"}:
            return {
                "valid": False,
                "errors": [f"NATIVE_TRIAGE_TRANSITION_READBACK_FAILED:{after.get('status')}"],
                "mutated": True,
                "details": details,
            }
        for field in ("title", "body", "assignee"):
            if after.get(field) != before.get(field):
                return {
                    "valid": False,
                    "errors": [f"NATIVE_TRIAGE_TRANSITION_MUTATED_{field.upper()}"],
                    "mutated": True,
                    "details": details,
                }
        triage_status = after.get("status")
    if triage_status in {"todo", "ready"}:
        completed = _run_kanban(
            hermes_bin,
            board,
            ["complete", "--result", "frozen planning graph materialized and activation verified", triage_task_id],
            runner=runner,
        )
        if completed.returncode != 0:
            diagnostic = (completed.stderr or completed.stdout).strip().replace("\n", " ")
            return {
                "valid": False,
                "errors": [f"NATIVE_TRIAGE_COMPLETE_FAILED:{completed.returncode}:{diagnostic}"],
                "mutated": True,
                "details": details,
            }
        try:
            current = _show(hermes_bin, board, triage_task_id, runner=runner)["task"]
            if current.get("status") != "done":
                return {
                    "valid": False,
                    "errors": [f"NATIVE_COMPLETE_READBACK_FAILED:{triage_task_id}"],
                    "mutated": True,
                    "details": details,
                }
        except (RuntimeError, ValueError, json.JSONDecodeError) as exc:
            return {"valid": False, "errors": [str(exc)], "mutated": True, "details": details}

    try:
        target_status = _show(hermes_bin, board, target_id, runner=runner)["task"].get("status")
    except (RuntimeError, ValueError, json.JSONDecodeError) as exc:
        return {"valid": False, "errors": [str(exc)], "mutated": True, "details": details}
    if target_status == "todo":
        promoted = _run_kanban(hermes_bin, board, ["promote", target_id, "approved triage graph"], runner=runner)
        if promoted.returncode != 0:
            return {
                "valid": False,
                "errors": [f"NATIVE_PROMOTE_FAILED:{promoted.returncode}"],
                "mutated": True,
                "details": details,
            }
        mutated = True
    elif target_status != "ready":
        return {
            "valid": False,
            "errors": [f"ACTIVATION_TARGET_INVALID_STATUS:{target_id}:{target_status}"],
            "mutated": True,
            "details": details,
        }
    try:
        current_readback = _read_current_graph(graph, readback, hermes_bin, board, runner=runner)
        observed_graph = _with_native_statuses(graph, current_readback)
        errors = validate_graph(observed_graph, phase="native", validate_implementation=validate_implementation)
        errors.extend(validate_native_readback(observed_graph, current_readback))
        return {"valid": not errors, "errors": sorted(set(errors)), "mutated": mutated, "details": details}
    except (RuntimeError, ValueError, json.JSONDecodeError) as exc:
        return {"valid": False, "errors": [str(exc)], "mutated": True, "details": details}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--board", required=True)
    parser.add_argument("--graph", required=True, type=Path)
    parser.add_argument("--native-readback", required=True, type=Path)
    parser.add_argument("--triage-task-id", required=True)
    parser.add_argument("--decision-task-id", action="append", default=[])
    parser.add_argument("--activation-key", required=True)
    parser.add_argument("--review-admission", type=Path)
    parser.add_argument("--hermes-bin", default="hermes")
    parser.add_argument("--apply", action="store_true", help="perform completion and promotion after a successful preflight")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    report = activate(
        args.graph,
        args.native_readback,
        args.triage_task_id,
        args.decision_task_id,
        args.activation_key,
        args.hermes_bin,
        args.board,
        apply=args.apply,
        review_admission=_load_json(args.review_admission) if args.review_admission else None,
    )
    _write_report(args.report, report)
    return 0 if report["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
