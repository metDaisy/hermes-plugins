"""KWC-owned Triage authoring and native materialization activity."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, Callable

from . import triage_domain


_AUTHORING_SCHEMA = "kwc-triage-authoring-v1"
_IDENTITY_FIELDS = {
    "issue_number",
    "title",
    "issue_url",
    "planning_sha",
    "current_state_sha",
    "requirement_locators",
    "current_state_locators",
}


def _load_sibling(unique_name: str, filename: str) -> Any:
    del unique_name
    if filename != "triage_domain.py":
        raise ImportError(f"unknown Triage dependency: {filename}")
    return triage_domain


def _load_domain() -> Any:
    return _load_sibling("_hermes_langchain_triage_domain", "triage_domain.py")


def _read_json(path: Path, domain_module: Any) -> Any:
    try:
        return domain_module._loads_json(Path(path).read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"JSON_READ_FAILED:{path}") from exc


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _required_mapping(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be an object")
    return value


def _identity(value: Any, domain_module: Any) -> Any:
    data = _required_mapping(value, "authoring.identity")
    if set(data) != _IDENTITY_FIELDS:
        raise ValueError("authoring.identity fields are invalid")
    for field in ("title", "issue_url", "planning_sha", "current_state_sha"):
        if not isinstance(data[field], str) or not data[field].strip():
            raise ValueError(f"authoring.identity.{field} is required")
    for field in ("requirement_locators", "current_state_locators"):
        if not isinstance(data[field], list) or not data[field] or not all(
            isinstance(item, str) and item.strip() for item in data[field]
        ):
            raise ValueError(f"authoring.identity.{field} is invalid")
    if isinstance(data["issue_number"], bool) or not isinstance(data["issue_number"], int):
        raise ValueError("authoring.identity.issue_number is invalid")
    return domain_module.TriageIdentity(
        issue_number=data["issue_number"],
        title=data["title"],
        issue_url=data["issue_url"],
        planning_sha=data["planning_sha"],
        current_state_sha=data["current_state_sha"],
        requirement_locators=tuple(data["requirement_locators"]),
        current_state_locators=tuple(data["current_state_locators"]),
    )


class HermesKanbanPort:
    def __init__(
        self,
        *,
        workspace: Path,
        profile: str,
        run_fn: Callable[..., Any] = subprocess.run,
        hermes_bin: str = "hermes",
    ) -> None:
        self._workspace = workspace
        self._profile = profile
        self._run_fn = run_fn
        self._hermes_bin = hermes_bin

    def _run_json(self, command: list[str]) -> Any:
        completed = self._run_fn(
            command,
            cwd=self._workspace,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        return _load_domain()._loads_json(completed.stdout)

    def list_active_triage(self, board: str) -> list[dict[str, Any]]:
        value = self._run_json(
            [self._hermes_bin, "kanban", "--board", board, "list", "--status", "triage", "--json"]
        )
        tasks = value.get("tasks") if isinstance(value, dict) else value
        if not isinstance(tasks, list):
            raise ValueError("TRIAGE_LIST_READBACK_INVALID")
        return tasks

    def create_triage(self, *, board: str, card: dict[str, Any]) -> str:
        created = self._run_json(
            [
                self._hermes_bin,
                "kanban",
                "--board",
                board,
                "create",
                card["title"],
                "--triage",
                "--assignee",
                self._profile,
                "--workspace",
                f"dir:{self._workspace.as_posix()}",
                "--body-file",
                str(card["body_file"]),
                "--idempotency-key",
                card["idempotency_key"],
                "--json",
            ]
        )
        task_id = created.get("id") if isinstance(created, dict) else None
        if not isinstance(task_id, str) or not task_id.startswith("t_"):
            raise ValueError("TRIAGE_CREATE_ID_INVALID")
        return task_id

    def read_task(self, *, board: str, task_id: str) -> dict[str, Any]:
        envelope = self._run_json(
            [self._hermes_bin, "kanban", "--board", board, "show", task_id, "--json"]
        )
        task = envelope.get("task") if isinstance(envelope, dict) else None
        if not isinstance(task, dict):
            raise ValueError("TRIAGE_READBACK_INVALID")
        return task

    def archive_task(self, *, board: str, task_id: str) -> None:
        self._run_fn(
            [self._hermes_bin, "kanban", "--board", board, "archive", task_id],
            cwd=self._workspace,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )


def recover_frozen_triage(
    *,
    assignment: Any,
    frozen_body_file: Path,
    output_file: Path,
    run_fn: Callable[..., Any] = subprocess.run,
    domain_module: Any | None = None,
    kanban_port: Any | None = None,
) -> dict[str, Any]:
    """Replace an Impl assignment's bad Triage with an exact validated frozen body."""
    assignment = _required_mapping(assignment, "assignment")
    if assignment.get("schema") != "kwc-assignment-v1" or assignment.get("phase") != "impl":
        raise ValueError("assignment is not an Impl recovery contract")
    workspace = Path(str(assignment.get("workspace_path") or "")).resolve()
    if not workspace.is_dir():
        raise ValueError("assignment workspace is unavailable")
    source_file = Path(frozen_body_file)
    if not source_file.is_absolute():
        source_file = workspace / source_file
    source_file = source_file.resolve()
    if workspace != source_file and workspace not in source_file.parents:
        raise ValueError("frozen Triage file is outside assignment workspace")
    board = str(assignment.get("board") or "").strip()
    profile = str(assignment.get("profile") or "").strip()
    issue_number = assignment.get("issue_number")
    generation = assignment.get("generation")
    sources = _required_mapping(assignment.get("sources"), "assignment.sources")
    previous_id = str(sources.get("triage_id") or "").strip()
    if (
        not board
        or profile != "project-manager"
        or not isinstance(issue_number, int)
        or isinstance(issue_number, bool)
        or not isinstance(generation, int)
        or isinstance(generation, bool)
        or not previous_id.startswith("t_")
        or sources.get("impl_ids") != []
    ):
        raise ValueError("assignment identity is invalid")

    domain = domain_module or _load_domain()
    frozen = _required_mapping(_read_json(source_file, domain), "frozen Triage")
    errors = domain.build_triage_module().validator.validate(frozen, "triage")
    if errors:
        raise ValueError(errors[0])
    issue = _required_mapping(frozen.get("issue"), "frozen Triage issue")
    if issue.get("number") != issue_number:
        raise ValueError("frozen Triage issue does not match assignment")

    run_dir = workspace / ".temp/kwc-authoring" / str(issue_number)
    recovered_body_file = run_dir / "recovered-triage-body.json"
    _write_json(recovered_body_file, frozen)
    port = kanban_port or HermesKanbanPort(
        workspace=workspace,
        profile=profile,
        run_fn=run_fn,
    )
    title = f"G{generation}-Issue{issue_number}-Triage"
    created_id: str | None = None
    try:
        created_id = port.create_triage(
            board=board,
            card={
                "title": title,
                "assignee": profile,
                "workspace": workspace,
                "body_file": recovered_body_file,
                "idempotency_key": (
                    f"{board}:generation-{generation}:issue-{issue_number}:"
                    f"triage-v7-recovery:{frozen['frozen_digest']}"
                ),
            },
        )
        task = port.read_task(board=board, task_id=created_id)
        errors = domain.validate_card(
            task,
            created_id,
            "triage",
            profile,
            int(issue_number),
            issue.get("title"),
            issue.get("url"),
        )
        expected_body = recovered_body_file.read_text(encoding="utf-8")
        if task.get("title") != title:
            errors.append("TITLE_MISMATCH")
        if task.get("body") != expected_body:
            errors.append("BODY_TEXT_MISMATCH")
        if task.get("skills") != []:
            errors.append("SKILLS_MISMATCH")
        if task.get("workspace_kind") != "dir" or task.get("workspace_path") != workspace.as_posix():
            errors.append("WORKSPACE_MISMATCH")
        if errors:
            raise ValueError(errors[0])
        port.archive_task(board=board, task_id=previous_id)
        report = {
            "valid": True,
            "verified": True,
            "previous_task_id": previous_id,
            "task_id": created_id,
            "title": title,
            "status": "triage",
            "frozen_digest": frozen["frozen_digest"],
            "frozen_body_file": recovered_body_file.relative_to(workspace).as_posix(),
        }
        _write_json(output_file, report)
        return report
    except Exception:
        if created_id is not None:
            port.archive_task(board=board, task_id=created_id)
        raise


def execute_triage_authoring(
    *,
    assignment: Any,
    authoring_file: Path,
    current_state_inspection_file: Path,
    output_file: Path,
    run_fn: Callable[..., Any] = subprocess.run,
    domain_module: Any | None = None,
    kanban_port: Any | None = None,
) -> dict[str, Any]:
    """Compose, freeze, materialize, and verify one Triage entirely inside KWC."""
    assignment = _required_mapping(assignment, "assignment")
    if assignment.get("schema") != "kwc-assignment-v1":
        raise ValueError("assignment schema is invalid")
    workspace = Path(str(assignment.get("workspace_path") or "")).resolve()
    if not workspace.is_dir():
        raise ValueError("assignment workspace is unavailable")
    board = str(assignment.get("board") or "").strip()
    profile = str(assignment.get("profile") or "").strip()
    issue_number = assignment.get("issue_number")
    generation = assignment.get("generation")
    if not board or profile != "project-manager":
        raise ValueError("assignment identity is invalid")
    if isinstance(generation, bool):
        raise ValueError("assignment generation is invalid")
    try:
        generation = int(generation)
    except (TypeError, ValueError) as exc:
        raise ValueError("assignment generation is invalid") from exc
    if generation <= 0:
        raise ValueError("assignment generation is invalid")

    domain = domain_module or _load_domain()
    authoring = _required_mapping(_read_json(authoring_file, domain), "authoring")
    if set(authoring) != {"schema", "identity", "intake", "analysis"}:
        raise ValueError("authoring fields are invalid")
    if authoring.get("schema") != _AUTHORING_SCHEMA:
        raise ValueError("authoring schema is invalid")
    identity = _identity(authoring.get("identity"), domain)
    if identity.issue_number != issue_number:
        raise ValueError("authoring.identity.issue_number does not match assignment")
    intake_authoring = _required_mapping(authoring.get("intake"), "authoring.intake")
    analysis_authoring = _required_mapping(authoring.get("analysis"), "authoring.analysis")
    inspection = _read_json(current_state_inspection_file, domain)

    module = domain.build_triage_module()
    intake = module.compose_intake.execute(identity, intake_authoring)
    errors = module.validator.validate_repository_locators(intake, workspace)
    if errors:
        raise ValueError(errors[0])
    analysis = module.compose_analysis.execute(intake, analysis_authoring)
    errors = module.validator.validate_repository_locators(analysis, workspace)
    if errors:
        raise ValueError(errors[0])
    frozen = module.freezer.execute(analysis, inspection)
    errors = module.validator.validate(frozen, "triage")
    if errors:
        raise ValueError(errors[0])

    run_dir = workspace / ".temp/kwc-authoring" / str(issue_number)
    frozen_body_file = run_dir / "triage-body.json"
    _write_json(frozen_body_file, frozen)
    port = kanban_port or HermesKanbanPort(
        workspace=workspace,
        profile=profile,
        run_fn=run_fn,
    )
    if port.list_active_triage(board):
        raise ValueError("ACTIVE_TRIAGE_ALREADY_EXISTS")

    title = f"G{generation}-Issue{issue_number}-Triage"
    created_id: str | None = None
    try:
        created_id = port.create_triage(
            board=board,
            card={
                "title": title,
                "assignee": profile,
                "workspace": workspace,
                "body_file": frozen_body_file,
                "idempotency_key": (
                    f"{board}:generation-{generation}:issue-{issue_number}:"
                    f"triage-v7:{frozen['frozen_digest']}"
                ),
            },
        )
        task = port.read_task(board=board, task_id=created_id)
        errors = domain.validate_card(
            task,
            created_id,
            "triage",
            profile,
            int(issue_number),
            identity.title,
            identity.issue_url,
        )
        expected_body = frozen_body_file.read_text(encoding="utf-8")
        if task.get("title") != title:
            errors.append("TITLE_MISMATCH")
        if task.get("body") != expected_body:
            errors.append("BODY_TEXT_MISMATCH")
        if task.get("skills") != []:
            errors.append("SKILLS_MISMATCH")
        if task.get("workspace_kind") != "dir":
            errors.append("WORKSPACE_KIND_MISMATCH")
        if task.get("workspace_path") != workspace.as_posix():
            errors.append("WORKSPACE_PATH_MISMATCH")
        errors.extend(module.validator.validate_current_state_inspection(frozen, inspection))
        if errors:
            raise ValueError(errors[0])
        report = {
            "valid": True,
            "verified": True,
            "task_id": created_id,
            "title": title,
            "status": "triage",
            "frozen_digest": frozen["frozen_digest"],
            "frozen_body_file": frozen_body_file.relative_to(workspace).as_posix(),
        }
        _write_json(output_file, report)
        return report
    except Exception:
        if created_id is not None:
            port.archive_task(board=board, task_id=created_id)
        raise
