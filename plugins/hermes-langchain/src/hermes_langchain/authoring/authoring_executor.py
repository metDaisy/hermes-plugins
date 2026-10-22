"""Native adapters for the four KWC-owned PM card-authoring phases."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

from . import artifact_paths, triage_activity


_PHASES = {"triage", "impl", "review", "summary"}


def _load_triage_activity() -> Any:
    return triage_activity


def _required_mapping(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be an object")
    return value


def _workspace_file(workspace: Path, value: Any, field: str) -> Path:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field} is required")
    path = Path(text)
    candidate = path.resolve() if path.is_absolute() else (workspace / path).resolve()
    try:
        candidate.relative_to(workspace)
    except ValueError as exc:
        raise ValueError(f"{field} must be inside assignment workspace") from exc
    if not candidate.is_file():
        raise ValueError(f"{field} does not exist")
    return candidate


def _canonicalize_json_file(path: Path, field: str) -> None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"{field} must contain valid JSON") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{field} must contain a JSON object")
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def _run(command: list[str], *, cwd: Path, run_fn: Callable[..., Any]) -> None:
    try:
        run_fn(
            command,
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
    except subprocess.CalledProcessError as exc:
        structured_errors: list[str] = []
        for line in str(exc.stdout or "").splitlines():
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict) or not isinstance(payload.get("errors"), list):
                continue
            structured_errors.extend(
                str(error)
                for error in payload["errors"]
                if isinstance(error, str) and error
            )
        if structured_errors:
            raise ValueError(
                "PM_HELPER_FAILED:" + "|".join(structured_errors[:8])
            ) from exc
        raise ValueError(f"PM_HELPER_EXIT_{exc.returncode}") from exc


def _read_report(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("PM helper did not write a valid JSON report") from exc
    if not isinstance(value, dict):
        raise ValueError("PM helper report must be an object")
    return value


def _task_id(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not text.startswith("t_"):
        raise ValueError(f"{field} is not a native task id")
    return text


def execute_authoring_phase(
    *,
    assignment: Any,
    arguments: Any,
    profile_home: Path,
    run_fn: Callable[..., Any] = subprocess.run,
    triage_execute_fn: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Execute exactly one assignment phase through its KWC-owned adapter."""
    assignment = _required_mapping(assignment, "assignment")
    arguments = _required_mapping(arguments, "arguments")
    if assignment.get("schema") != "kwc-assignment-v1":
        raise ValueError("assignment schema is invalid")
    if assignment.get("profile") != "project-manager":
        raise ValueError("assignment profile is invalid")
    phase = str(assignment.get("phase") or "")
    if phase not in _PHASES:
        raise ValueError("assignment phase is not authorable")
    workspace = Path(str(assignment.get("workspace_path") or "")).resolve()
    if not workspace.is_dir():
        raise ValueError("assignment workspace is unavailable")
    del profile_home
    graph_script = Path(__file__).resolve().parent / "graph" / "build_task_graph.py"
    if phase != "triage" and not graph_script.is_file():
        raise ValueError("KWC authoring helper is unavailable")
    board = str(assignment.get("board") or "").strip()
    issue = int(assignment.get("issue_number"))
    generation = int(assignment.get("generation"))
    sources = _required_mapping(assignment.get("sources"), "assignment.sources")
    output_dir = (
        artifact_paths.triage_output_dir
        if phase == "triage"
        else artifact_paths.task_graph_output_dir
    )
    run_dir = output_dir(
        workspace,
        board=board,
        issue_number=issue,
        generation=generation,
    )
    run_dir.mkdir(parents=True, exist_ok=True)
    output = run_dir / f"{phase}-result.json"
    output_arg = output.relative_to(workspace).as_posix()
    base = [sys.executable, str(graph_script)]

    if phase == "triage":
        authoring = _workspace_file(workspace, arguments.get("authoring_file"), "authoring_file")
        inspection = _workspace_file(
            workspace,
            arguments.get("current_state_inspection_file"),
            "current_state_inspection_file",
        )
        execute_triage = triage_execute_fn or _load_triage_activity().execute_triage_authoring
        report = execute_triage(
            assignment=assignment,
            authoring_file=authoring,
            current_state_inspection_file=inspection,
            output_file=output,
            run_fn=run_fn,
        )
        if report.get("valid") is not True or report.get("verified") is not True:
            raise ValueError("Triage native verification failed")
        task_ids = [_task_id(report.get("task_id"), "task_id")]
    elif phase == "impl":
        triage_id = _task_id(sources.get("triage_id"), "triage_id")
        batch = _workspace_file(workspace, arguments.get("batch_file"), "batch_file")
        body_dir = run_dir / "impl-bodies"
        body_dir_arg = body_dir.relative_to(workspace).as_posix()
        _run(
            [*base, "render-impl-batch", "--batch", str(batch), "--output-dir", body_dir_arg],
            cwd=workspace,
            run_fn=run_fn,
        )
        _run(
            [
                *base,
                "create-impl-batch-native",
                "--batch",
                str(batch),
                "--body-dir",
                body_dir_arg,
                "--profile",
                "project-manager",
                "--board",
                board,
                "--triage-id",
                triage_id,
                "--workspace",
                str(workspace),
                "--output",
                output_arg,
            ],
            cwd=workspace,
            run_fn=run_fn,
        )
        report = _read_report(output)
        created = report.get("created")
        if not isinstance(created, list) or not created:
            raise ValueError("Impl native verification report has no created cards")
        task_ids = [
            _task_id(item.get("id") if isinstance(item, dict) else None, "created.id")
            for item in created
        ]
    elif phase == "review":
        triage_id = _task_id(sources.get("triage_id"), "triage_id")
        impl_ids = sources.get("impl_ids")
        if not isinstance(impl_ids, list) or not impl_ids:
            raise ValueError("review assignment has no Impl sources")
        impl_ids = [_task_id(value, "impl_id") for value in impl_ids]
        body = _workspace_file(workspace, arguments.get("body_file"), "body_file")
        _canonicalize_json_file(body, "body_file")
        bundle = run_dir / "review-source.json"
        prefill = run_dir / "review-prefill.json"
        bundle_arg = bundle.relative_to(workspace).as_posix()
        prefill_arg = prefill.relative_to(workspace).as_posix()
        prepare = [
            *base,
            "prepare-review-fast-path",
            "--profile",
            "project-manager",
            "--board",
            board,
            "--triage-id",
            triage_id,
        ]
        for impl_id in impl_ids:
            prepare.extend(["--implementation-id", impl_id])
        prepare.extend(
            [
                "--issue",
                str(issue),
                "--generation",
                str(generation),
                "--bundle-output",
                bundle_arg,
                "--body-output",
                prefill_arg,
            ]
        )
        _run(prepare, cwd=workspace, run_fn=run_fn)
        _run(
            [
                *base,
                "create-review-native",
                "--bundle",
                bundle_arg,
                "--body",
                str(body),
                "--profile",
                "project-manager",
                "--board",
                board,
                "--workspace",
                str(workspace),
                "--output",
                output_arg,
            ],
            cwd=workspace,
            run_fn=run_fn,
        )
        report = _read_report(output)
        if report.get("valid") is not True:
            raise ValueError("Review native verification failed")
        created = _required_mapping(report.get("created"), "report.created")
        task_ids = [_task_id(created.get("id"), "created.id")]
    else:
        triage_id = _task_id(sources.get("triage_id"), "triage_id")
        bundle = run_dir / "summary-source.json"
        body = run_dir / "summary-body.json"
        bundle_arg = bundle.relative_to(workspace).as_posix()
        body_arg = body.relative_to(workspace).as_posix()
        _run(
            [
                *base,
                "regenerate-summary-native",
                "--profile",
                "project-manager",
                "--board",
                board,
                "--triage-id",
                triage_id,
                "--issue",
                str(issue),
                "--generation",
                str(generation),
                "--workspace",
                str(workspace),
                "--bundle-output",
                bundle_arg,
                "--body-output",
                body_arg,
                "--output",
                output_arg,
            ],
            cwd=workspace,
            run_fn=run_fn,
        )
        report = _read_report(output)
        if report.get("valid") is not True:
            raise ValueError("Summary native verification failed")
        task_ids = [_task_id(report.get("task_id"), "task_id")]

    return {
        "ok": True,
        "schema": "kwc-authoring-phase-result-v1",
        "assignment_id": assignment["assignment_id"],
        "phase": phase,
        "created_task_ids": task_ids,
        "report_path": str(output.relative_to(workspace).as_posix()),
    }
