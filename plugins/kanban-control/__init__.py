"""Kanban Control: deterministic workflow entrypoint for Hermes Kanban."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

_PLUGIN_ID = "kanban-control"
_SCHEMA = "kanban-control/run-request@1"
_MAX_REQUEST_CHARS = 12_000
_USAGE = "Usage: /kcp [run] [--board <existing-slug> | --new-board <slug>] <request>"
_CLIENT_RUN_ID = re.compile(r"^kcp-\d{8}T\d{6}Z-[0-9a-f]{8}$")


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _settings(ctx: Any) -> dict[str, str]:
    return {
        "manager": _text(ctx.get_config("manager_profile", "")),
        "worker": _text(ctx.get_config("worker_profile", "")),
        "reviewer": _text(ctx.get_config("reviewer_profile", "")),
        "board": _text(ctx.get_config("board", "")),
        "workspace": _text(ctx.get_config("workspace", "")),
        "project": _text(ctx.get_config("project", "")),
    }


def _installed_profile_names() -> set[str]:
    from hermes_cli.profiles import list_profiles

    return {profile.name for profile in list_profiles(lazy_skill_count=True)}


def _ensure_kanban_tool_registered() -> None:
    """Load Hermes' native Kanban tool module before plugin-side dispatch."""
    import importlib

    importlib.import_module("tools.kanban_tools")


def _has_session_notification_target() -> bool:
    """Whether native Kanban can route task events back to this live session."""
    try:
        from gateway.session_context import get_session_env
    except ImportError:
        return False
    platform = _text(get_session_env("HERMES_SESSION_PLATFORM", ""))
    chat_id = _text(get_session_env("HERMES_SESSION_CHAT_ID", ""))
    session_key = _text(get_session_env("HERMES_SESSION_KEY", ""))
    return bool((platform and chat_id) or session_key)


def _configuration_error(ctx: Any, missing: list[str]) -> str:
    profile = _text(getattr(ctx, "profile_name", "")) or "main"
    prefix = f"hermes -p {profile} config set plugins.entries.{_PLUGIN_ID}.settings"
    commands = "\n".join(f"- {prefix}.{key} <profile-name>" for key in missing)
    return (
        "Kanban Control is not configured. Set every role binding, then retry:\n"
        f"{commands}\n"
        f"- {prefix}.workspace <absolute-path>  # or set .project <project-id-or-slug>"
    )


def _validate_settings(ctx: Any, settings: dict[str, str]) -> str | None:
    missing = [f"{role}_profile" for role in ("manager", "worker", "reviewer") if not settings[role]]
    if missing:
        return _configuration_error(ctx, missing)

    bindings = [settings[role] for role in ("manager", "worker", "reviewer")]
    if len(set(bindings)) != len(bindings):
        return "Kanban Control requires distinct manager, worker, and reviewer profiles."

    installed = _installed_profile_names()
    unknown = [name for name in bindings if name not in installed]
    if unknown:
        return "Kanban Control profile binding does not exist: " + ", ".join(sorted(set(unknown)))

    workspace, project = settings["workspace"], settings["project"]
    if workspace and project:
        return "Kanban Control accepts either workspace or project, not both."
    if workspace:
        path = Path(workspace).expanduser()
        if not path.is_absolute():
            return "Kanban Control workspace must be an absolute path."
        if not path.is_dir():
            return f"Kanban Control workspace does not exist or is not a directory: {workspace}"
    elif not project:
        return _configuration_error(ctx, [])
    return None


def _after_metadata(remainder: str, end: int) -> str:
    remainder = remainder[end:]
    return remainder[1:] if remainder[:1].isspace() else remainder


def _parse_run_options(raw_args: str) -> tuple[str, str, bool, str, str | None]:
    remainder = raw_args or ""
    board = ""
    create_board = False
    client_run_id = ""
    legacy_run_seen = False
    while remainder:
        legacy_run = re.match(r"(?is)^run(?=\s|$)", remainder)
        if legacy_run and not legacy_run_seen:
            legacy_run_seen = True
            remainder = _after_metadata(remainder, legacy_run.end())
            continue

        run_id_option = re.match(r"(?is)^--run-id\s+(?P<run_id>\S+)(?=\s|$)", remainder)
        if run_id_option and not client_run_id:
            candidate = run_id_option.group("run_id")
            if not _CLIENT_RUN_ID.fullmatch(candidate):
                return "", "", False, "", _USAGE
            client_run_id = candidate
            remainder = _after_metadata(remainder, run_id_option.end())
            continue

        board_option = re.match(
            r'''(?is)^--(?P<kind>new-board|board)\s+(?:"(?P<double>[^"]+)"|'(?P<single>[^']+)'|(?P<plain>\S+))(?=\s|$)''',
            remainder,
        )
        if board_option and not board:
            board = (
                board_option.group("double")
                or board_option.group("single")
                or board_option.group("plain")
                or ""
            ).strip()
            create_board = board_option.group("kind").lower() == "new-board"
            remainder = _after_metadata(remainder, board_option.end())
            continue
        break

    if remainder.lower().startswith(("--board", "--new-board", "--run-id")):
        return "", "", False, "", _USAGE
    if not remainder.strip():
        return "", "", False, "", _USAGE
    return remainder, board, create_board, client_run_id, None


def _run_id() -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"kcp-{timestamp}-{uuid4().hex[:8]}"


def _title(request: str, run_id: str) -> str:
    summary = " ".join(request.split())
    if len(summary) > 92:
        summary = summary[:89].rstrip() + "..."
    return f"KCP run {run_id}: {summary}"


def _body(run_id: str, request: str, settings: dict[str, str], board: str) -> str:
    payload = {
        "schema": _SCHEMA,
        "run_id": run_id,
        "request": request,
        "control_board": board or "active",
        "assigned_role": "manager",
        "profile_bindings": {
            "manager": settings["manager"],
            "worker": settings["worker"],
            "reviewer": settings["reviewer"],
        },
        "execution_contract": {
            "workflow_authority": "installed-profile-workflows",
            "lifecycle_authority": "hermes-kanban",
            "expected_boundary": "manager-intake-checkpoint",
            "rules": [
                "Execute the installed manager workflow; do not invent a replacement workflow.",
                "Use profile_bindings when assigning worker and reviewer cards.",
                "Persist every handoff and blocker on Kanban.",
                "The control_board is already selected; never create or route cards to another board.",
                "Link every downstream card into this run graph so origin-session progress delivery propagates.",
                "Execute this run serially: never create parallel sibling cards, and link each next card to the completed predecessor.",
                "At most one card from this run may be running at any time.",
                "Before promoting an aggregate reviewer card, write and read back the PM-owned aggregate-review-admission-v1 comment required by the installed workflow.",
                "Do not sleep or poll for another profile; finish at the required checkpoint.",
            ],
        },
    }
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)


def _create_args(
    run_id: str, request: str, settings: dict[str, str], board: str,
) -> dict[str, Any]:
    args: dict[str, Any] = {
        "title": _title(request, run_id),
        "body": _body(run_id, request, settings, board),
        "assignee": settings["manager"],
        "idempotency_key": run_id,
        "completion_contract": "local-only",
    }
    if board:
        args["board"] = board
    if settings["project"]:
        args["project"] = settings["project"]
    else:
        args["workspace_kind"] = "dir"
        args["workspace_path"] = str(Path(settings["workspace"]).expanduser().resolve())
    return args


def _result_message(
    run_id: str, settings: dict[str, str], board: str, raw_result: str,
) -> str:
    try:
        result = json.loads(raw_result)
    except (TypeError, json.JSONDecodeError):
        return "Kanban Control failed to register the run: invalid kanban_create response."
    if not isinstance(result, dict) or result.get("ok") is not True:
        detail = result.get("error") or result.get("message") if isinstance(result, dict) else None
        return f"Kanban Control failed to register the run: {detail or 'kanban_create failed'}"
    task_id = _text(result.get("task_id"))
    if not task_id:
        return "Kanban Control failed to register the run: kanban_create returned no task id."
    board = board or "active"
    status = _text(result.get("status")) or "ready"
    subscribed = result.get("subscribed") is True
    heading = (
        "Kanban Control run registered"
        if subscribed
        else "Kanban Control run registered, but current-session progress subscription failed"
    )
    return (
        f"{heading}\n"
        f"- run: {run_id}\n"
        f"- task: {task_id}\n"
        f"- board: {board}\n"
        f"- manager: {settings['manager']}\n"
        f"- state: {status}\n"
        "- execution: separate project-manager worker session\n"
        f"- progress: {'terminal lifecycle notifications in this session' if subscribed else 'not subscribed'}"
        + (" (not a live reasoning/tool stream)" if subscribed else "")
    )


def _create_new_board(board: str, workspace: str) -> None:
    from hermes_cli import kanban_db as kb

    normalized = kb._normalize_board_slug(board)
    if not normalized:
        raise ValueError("board slug is required")
    if kb.board_exists(normalized):
        raise ValueError(f"board {normalized!r} already exists; use --board to reuse it")
    kb.create_board(
        normalized,
        name=normalized,
        description="Kanban Control workflow board",
        default_workdir=workspace or None,
    )
    if not kb.board_exists(normalized):
        raise RuntimeError(f"board {normalized!r} was not visible after creation")


def _validate_board_target(board: str, create_board: bool, workspace: str) -> str | None:
    if not board:
        return None
    try:
        if create_board:
            _create_new_board(board, workspace)
            return None
        from hermes_cli import kanban_db as kb

        normalized = kb._normalize_board_slug(board)
        if not normalized or not kb.board_exists(normalized):
            return f"Kanban Control board {board!r} does not exist; use --new-board {board} to create it."
    except (RuntimeError, ValueError) as exc:
        return f"Kanban Control could not prepare board {board!r}: {exc}"
    return None


def _handle_kcp(ctx: Any, raw_args: str) -> str:
    request, board_override, create_board, client_run_id, parse_error = _parse_run_options(raw_args)
    if parse_error:
        return parse_error
    if not request:
        return _USAGE
    if len(request) > _MAX_REQUEST_CHARS:
        return f"Kanban Control request is too long (max {_MAX_REQUEST_CHARS} characters)."

    settings = _settings(ctx)
    error = _validate_settings(ctx, settings)
    if error:
        return error
    if not _has_session_notification_target():
        return "Kanban Control must be started from a live Hermes Desktop or messaging session."

    board = board_override or settings["board"]
    board_error = _validate_board_target(board, create_board, settings["workspace"])
    if board_error:
        return board_error

    run_id = client_run_id or _run_id()
    try:
        _ensure_kanban_tool_registered()
        raw_result = ctx.dispatch_tool("kanban_create", _create_args(run_id, request, settings, board))
    except Exception as exc:
        return f"Kanban Control failed to register the run: {type(exc).__name__}: {exc}"
    return _result_message(run_id, settings, board, raw_result)


def register(ctx: Any) -> None:
    ctx.register_command(
        "kcp",
        handler=lambda raw_args: _handle_kcp(ctx, raw_args),
        description="Register a profile-configured workflow run on Hermes Kanban.",
        args_hint="[run] [--board <slug> | --new-board <slug>] <request>",
        argument_mode="text",
    )
