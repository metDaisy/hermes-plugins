"""Durable, Project Manager-authorized state machine for card authoring."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from pathlib import Path
from typing import Any, Callable

from ..paths import PLUGIN_ROOT
from . import langgraph_router


_ASSIGNMENT_SCHEMA = "kwc-assignment-v1"
_PHASES = ("triage", "impl", "review", "summary", "complete")
_TRIAGE_STAGES = ("intake", "discovery", "source_confirmation", "analysis", "materialize")
_TRIAGE_ARTIFACT_SCHEMAS = {
    "intake": "kwc-triage-intake-authoring-v1",
    "discovery": "kwc-triage-discovery-v2",
    "source_confirmation": "kwc-triage-source-confirmation-v2",
    "analysis": "kwc-triage-analysis-authoring-v1",
    "materialize": None,
}
_TOOL_BY_PHASE = {
    "triage": "kwc_create_triage",
    "impl": "kwc_create_impl_graph",
    "review": "kwc_create_review",
    "summary": "kwc_create_summary",
}
_SKILL_BY_PHASE = {
    "triage": None,
    "impl": None,
    "review": None,
    "summary": None,
}
_TASK_ID = re.compile(r"^t_[A-Za-z0-9]{6,64}$")



def _now() -> int:
    return int(time.time())


def _required_text(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field} is required")
    return text


def _positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a positive integer")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a positive integer") from exc
    if result <= 0:
        raise ValueError(f"{field} must be a positive integer")
    return result


def _connect(state_path: Path) -> sqlite3.Connection:
    path = Path(state_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS authoring_workflow (
            board TEXT PRIMARY KEY,
            schema TEXT NOT NULL,
            issue_number INTEGER NOT NULL,
            generation INTEGER NOT NULL,
            workspace_path TEXT NOT NULL,
            pm_session_key TEXT NOT NULL,
            phase TEXT NOT NULL,
            revision INTEGER NOT NULL,
            assignment_id TEXT NOT NULL,
            triage_id TEXT,
            impl_ids_json TEXT NOT NULL,
            review_id TEXT,
            summary_id TEXT,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS authoring_workflow_history (
            board TEXT NOT NULL,
            generation INTEGER NOT NULL,
            snapshot_json TEXT NOT NULL,
            archived_at INTEGER NOT NULL,
            PRIMARY KEY(board, generation)
        )
        """
    )
    columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(authoring_workflow)")}
    if "triage_stage" not in columns:
        conn.execute("ALTER TABLE authoring_workflow ADD COLUMN triage_stage TEXT NOT NULL DEFAULT 'intake'")
    if "triage_artifacts_json" not in columns:
        conn.execute(
            "ALTER TABLE authoring_workflow ADD COLUMN triage_artifacts_json TEXT NOT NULL DEFAULT '{}'"
        )
    return conn


def _assignment_id(board: str, issue_number: int, generation: int, phase: str, revision: int) -> str:
    raw = f"{board}\0{issue_number}\0{generation}\0{phase}\0{revision}".encode("utf-8")
    return f"kwa_{hashlib.sha256(raw).hexdigest()[:24]}"


def _triage_instruction(stage: str) -> str:
    resource = PLUGIN_ROOT / "prompts" / f"triage-{stage.replace('_', '-')}.md"
    try:
        return resource.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise RuntimeError(f"triage prompt resource is unavailable: {resource}") from exc


def _sources(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "triage_id": row["triage_id"],
        "impl_ids": json.loads(str(row["impl_ids_json"])),
        "review_id": row["review_id"],
        "summary_id": row["summary_id"],
    }


def _artifact_file(row: sqlite3.Row, triage_stage: str | None) -> str | None:
    if not triage_stage or triage_stage == "materialize":
        return None
    stage = triage_stage.replace("_", "-")
    return (
        f".temp/kwc-authoring/{int(row['issue_number'])}/"
        f"generation-{int(row['generation'])}/{stage}-authoring.json"
    )


def _route_authoring(phase: str, triage_stage: str | None) -> str:
    result = langgraph_router.route_authoring(phase=phase, triage_stage=triage_stage)
    errors = list(result.get("errors") or [])
    next_action = result.get("next_action")
    if errors or not isinstance(next_action, str) or not next_action:
        raise RuntimeError(f"LangGraph authoring route failed: {errors or ['MISSING_NEXT_ACTION']}")
    return next_action


def _public(row: sqlite3.Row) -> dict[str, Any]:
    phase = str(row["phase"])
    triage_stage = str(row["triage_stage"]) if phase == "triage" else None
    tool = (
        "kwc_advance_triage"
        if phase == "triage" and triage_stage != "materialize"
        else _TOOL_BY_PHASE.get(phase)
    )
    skill = _SKILL_BY_PHASE.get(phase)
    next_action = _route_authoring(phase, triage_stage)
    if tool and next_action != tool:
        raise RuntimeError(
            f"LangGraph route {next_action!r} does not match assignment tool {tool!r}"
        )
    return {
        "schema": _ASSIGNMENT_SCHEMA,
        "assignment_id": str(row["assignment_id"]),
        "board": str(row["board"]),
        "issue_number": int(row["issue_number"]),
        "generation": int(row["generation"]),
        "workspace_path": str(row["workspace_path"]),
        "phase": phase,
        "triage_stage": triage_stage,
        "profile": "project-manager",
        "skill": {"name": skill} if skill else None,
        "allowed_tools": [tool] if tool else [],
        "next_action": next_action,
        "artifact_schema": _TRIAGE_ARTIFACT_SCHEMAS.get(triage_stage) if triage_stage else None,
        "artifact_file": _artifact_file(row, triage_stage),
        "instruction": _triage_instruction(triage_stage) if triage_stage else None,
        "stage_artifacts": (
            json.loads(str(row["triage_artifacts_json"])) if phase == "triage" else {}
        ),
        "sources": _sources(row),
        "revision": int(row["revision"]),
    }


def begin_authoring(
    *,
    state_path: Path,
    board: Any,
    issue_number: Any,
    generation: Any,
    workspace_path: Path,
    pm_session_key: Any,
    replace_incomplete: bool = False,
    now_fn: Callable[[], int] = _now,
) -> dict[str, Any]:
    board = _required_text(board, "board")
    issue_number = _positive_int(issue_number, "issue_number")
    generation = _positive_int(generation, "generation")
    session_key = _required_text(pm_session_key, "pm_session_key")
    if not isinstance(replace_incomplete, bool):
        raise ValueError("replace_incomplete must be a boolean")
    workspace = Path(workspace_path).resolve()
    if not workspace.is_dir():
        raise ValueError("workspace_path must be an existing directory")
    now = int(now_fn())
    assignment_id = _assignment_id(board, issue_number, generation, "triage:intake", 1)
    conn = _connect(state_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT * FROM authoring_workflow WHERE board=?", (board,)
        ).fetchone()
        if existing is not None:
            same = (
                int(existing["issue_number"]) == issue_number
                and int(existing["generation"]) == generation
                and Path(str(existing["workspace_path"])) == workspace
            )
            if not same:
                existing_generation = int(existing["generation"])
                restartable = (
                    (str(existing["phase"]) == "complete" or replace_incomplete)
                    and int(existing["issue_number"]) == issue_number
                    and Path(str(existing["workspace_path"])) == workspace
                    and generation == existing_generation + 1
                )
                if not restartable:
                    raise ValueError("board already has a different authoring contract")
                conn.execute(
                    """
                    INSERT OR IGNORE INTO authoring_workflow_history(
                        board, generation, snapshot_json, archived_at
                    ) VALUES(?, ?, ?, ?)
                    """,
                    (
                        board,
                        existing_generation,
                        json.dumps(
                            dict(existing),
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                        now,
                    ),
                )
                conn.execute(
                    """
                    UPDATE authoring_workflow
                    SET schema=?, issue_number=?, generation=?, workspace_path=?,
                        pm_session_key=?, phase='triage', revision=1, assignment_id=?,
                        triage_id=NULL, impl_ids_json='[]', review_id=NULL, summary_id=NULL,
                        created_at=?, updated_at=?, triage_stage='intake',
                        triage_artifacts_json='{}'
                    WHERE board=?
                    """,
                    (
                        _ASSIGNMENT_SCHEMA,
                        issue_number,
                        generation,
                        str(workspace),
                        session_key,
                        assignment_id,
                        now,
                        now,
                        board,
                    ),
                )
                restarted = conn.execute(
                    "SELECT * FROM authoring_workflow WHERE board=?", (board,)
                ).fetchone()
                conn.commit()
                return _public(restarted)
            conn.commit()
            return _public(existing)
        conn.execute(
            """
            INSERT INTO authoring_workflow(
                board, schema, issue_number, generation, workspace_path, pm_session_key,
                phase, revision, assignment_id, triage_id, impl_ids_json, review_id,
                summary_id, created_at, updated_at, triage_stage, triage_artifacts_json
            ) VALUES(?, ?, ?, ?, ?, ?, 'triage', 1, ?, NULL, '[]', NULL, NULL, ?, ?, 'intake', '{}')
            """,
            (
                board,
                _ASSIGNMENT_SCHEMA,
                issue_number,
                generation,
                str(workspace),
                session_key,
                assignment_id,
                now,
                now,
            ),
        )
        row = conn.execute(
            "SELECT * FROM authoring_workflow WHERE board=?", (board,)
        ).fetchone()
        conn.commit()
        return _public(row)
    finally:
        conn.close()


def read_assignment(*, state_path: Path, board: Any) -> dict[str, Any] | None:
    board = _required_text(board, "board")
    conn = _connect(state_path)
    try:
        row = conn.execute(
            "SELECT * FROM authoring_workflow WHERE board=?", (board,)
        ).fetchone()
        return _public(row) if row is not None else None
    finally:
        conn.close()


def read_assignment_by_id(*, state_path: Path, assignment_id: Any) -> dict[str, Any] | None:
    assignment_id = _required_text(assignment_id, "assignment_id")
    conn = _connect(state_path)
    try:
        row = conn.execute(
            "SELECT * FROM authoring_workflow WHERE assignment_id=?", (assignment_id,)
        ).fetchone()
        return _public(row) if row is not None else None
    finally:
        conn.close()


def complete_triage_stage(
    *,
    state_path: Path,
    assignment_id: Any,
    caller_profile: Any,
    caller_session_key: Any,
    stage: Any,
    artifact: Any,
    now_fn: Callable[[], int] = _now,
) -> dict[str, Any]:
    assignment_id = _required_text(assignment_id, "assignment_id")
    if _required_text(caller_profile, "caller_profile") != "project-manager":
        raise ValueError("caller profile is not project-manager")
    session_key = _required_text(caller_session_key, "caller_session_key")
    stage = _required_text(stage, "stage")
    if stage not in _TRIAGE_STAGES[:-1]:
        raise ValueError("stage is not advanceable")
    if not isinstance(artifact, dict) or set(artifact) != {"path", "sha256"}:
        raise ValueError("artifact metadata is invalid")
    artifact_path = _required_text(artifact.get("path"), "artifact.path")
    artifact_digest = _required_text(artifact.get("sha256"), "artifact.sha256")

    conn = _connect(state_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM authoring_workflow WHERE assignment_id=?", (assignment_id,)
        ).fetchone()
        if row is None:
            raise ValueError("assignment is stale or unknown")
        if str(row["phase"]) != "triage":
            raise ValueError("assignment phase is stale")
        if str(row["triage_stage"]) != stage:
            raise ValueError("triage stage is stale")

        next_stage = _TRIAGE_STAGES[_TRIAGE_STAGES.index(stage) + 1]
        revision = int(row["revision"]) + 1
        artifacts = json.loads(str(row["triage_artifacts_json"]))
        artifacts[stage] = {"path": artifact_path, "sha256": artifact_digest}
        next_assignment_id = _assignment_id(
            str(row["board"]),
            int(row["issue_number"]),
            int(row["generation"]),
            f"triage:{next_stage}",
            revision,
        )
        conn.execute(
            """
            UPDATE authoring_workflow
            SET triage_stage=?, triage_artifacts_json=?, revision=?, assignment_id=?, updated_at=?
            WHERE board=? AND assignment_id=?
            """,
            (
                next_stage,
                json.dumps(artifacts, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                revision,
                next_assignment_id,
                int(now_fn()),
                str(row["board"]),
                assignment_id,
            ),
        )
        updated = conn.execute(
            "SELECT * FROM authoring_workflow WHERE board=?", (str(row["board"]),)
        ).fetchone()
        conn.commit()
        return _public(updated)
    finally:
        conn.close()


def replace_impl_triage_source(
    *,
    state_path: Path,
    assignment_id: Any,
    caller_profile: Any,
    caller_session_key: Any,
    expected_triage_id: Any,
    replacement_triage_id: Any,
    now_fn: Callable[[], int] = _now,
) -> dict[str, Any]:
    """Replace a bad frozen Triage before any Impl task has been materialized."""
    assignment_id = _required_text(assignment_id, "assignment_id")
    if _required_text(caller_profile, "caller_profile") != "project-manager":
        raise ValueError("caller profile is not project-manager")
    session_key = _required_text(caller_session_key, "caller_session_key")
    expected = _required_text(expected_triage_id, "expected_triage_id")
    replacement = _required_text(replacement_triage_id, "replacement_triage_id")
    if _TASK_ID.fullmatch(expected) is None or _TASK_ID.fullmatch(replacement) is None:
        raise ValueError("triage task ids are invalid")
    if expected == replacement:
        raise ValueError("replacement triage must be different")

    conn = _connect(state_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM authoring_workflow WHERE assignment_id=?", (assignment_id,)
        ).fetchone()
        if row is None:
            raise ValueError("assignment is stale or unknown")
        if str(row["phase"]) != "impl":
            raise ValueError("assignment phase is stale")
        if str(row["triage_id"] or "") != expected:
            raise ValueError("triage source is stale")
        if json.loads(str(row["impl_ids_json"])):
            raise ValueError("implementation tasks already exist")

        revision = int(row["revision"]) + 1
        next_assignment_id = _assignment_id(
            str(row["board"]),
            int(row["issue_number"]),
            int(row["generation"]),
            "impl",
            revision,
        )
        conn.execute(
            """
            UPDATE authoring_workflow
            SET triage_id=?, revision=?, assignment_id=?, updated_at=?
            WHERE board=? AND assignment_id=?
            """,
            (
                replacement,
                revision,
                next_assignment_id,
                int(now_fn()),
                str(row["board"]),
                assignment_id,
            ),
        )
        updated = conn.execute(
            "SELECT * FROM authoring_workflow WHERE board=?", (str(row["board"]),)
        ).fetchone()
        conn.commit()
        return _public(updated)
    finally:
        conn.close()


def reopen_unreviewed_impl_graph(
    *,
    state_path: Path,
    assignment_id: Any,
    caller_profile: Any,
    caller_session_key: Any,
    expected_impl_ids: Any,
    now_fn: Callable[[], int] = _now,
) -> dict[str, Any]:
    """Return an unreviewed Impl graph to authoring after its cards are rejected."""
    assignment_id = _required_text(assignment_id, "assignment_id")
    if _required_text(caller_profile, "caller_profile") != "project-manager":
        raise ValueError("caller profile is not project-manager")
    session_key = _required_text(caller_session_key, "caller_session_key")
    if not isinstance(expected_impl_ids, list) or not expected_impl_ids:
        raise ValueError("expected_impl_ids must be a non-empty list")
    expected = [_required_text(value, "expected_impl_id") for value in expected_impl_ids]
    if len(set(expected)) != len(expected) or any(_TASK_ID.fullmatch(value) is None for value in expected):
        raise ValueError("expected implementation task ids are invalid")

    conn = _connect(state_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM authoring_workflow WHERE assignment_id=?", (assignment_id,)
        ).fetchone()
        if row is None:
            raise ValueError("assignment is stale or unknown")
        if str(row["phase"]) != "review":
            raise ValueError("assignment phase is stale")
        if json.loads(str(row["impl_ids_json"])) != expected:
            raise ValueError("implementation sources are stale")
        if row["review_id"] is not None or row["summary_id"] is not None:
            raise ValueError("review or summary task already exists")

        revision = int(row["revision"]) + 1
        next_assignment_id = _assignment_id(
            str(row["board"]),
            int(row["issue_number"]),
            int(row["generation"]),
            "impl",
            revision,
        )
        conn.execute(
            """
            UPDATE authoring_workflow
            SET phase='impl', impl_ids_json='[]', revision=?, assignment_id=?, updated_at=?
            WHERE board=? AND assignment_id=?
            """,
            (
                revision,
                next_assignment_id,
                int(now_fn()),
                str(row["board"]),
                assignment_id,
            ),
        )
        updated = conn.execute(
            "SELECT * FROM authoring_workflow WHERE board=?", (str(row["board"]),)
        ).fetchone()
        conn.commit()
        return _public(updated)
    finally:
        conn.close()


def complete_phase(
    *,
    state_path: Path,
    assignment_id: Any,
    caller_profile: Any,
    caller_session_key: Any,
    phase: Any,
    created_task_ids: Any,
    now_fn: Callable[[], int] = _now,
) -> dict[str, Any]:
    assignment_id = _required_text(assignment_id, "assignment_id")
    if _required_text(caller_profile, "caller_profile") != "project-manager":
        raise ValueError("caller profile is not project-manager")
    session_key = _required_text(caller_session_key, "caller_session_key")
    phase = _required_text(phase, "phase")
    if phase not in _TOOL_BY_PHASE:
        raise ValueError("phase is not authorable")
    if not isinstance(created_task_ids, list) or not created_task_ids:
        raise ValueError("created_task_ids must be a non-empty list")
    task_ids = [_required_text(value, "created_task_id") for value in created_task_ids]
    if len(set(task_ids)) != len(task_ids) or any(_TASK_ID.fullmatch(value) is None for value in task_ids):
        raise ValueError("created_task_ids are invalid")
    if phase != "impl" and len(task_ids) != 1:
        raise ValueError(f"{phase} must create exactly one task")

    conn = _connect(state_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM authoring_workflow WHERE assignment_id=?", (assignment_id,)
        ).fetchone()
        if row is None:
            raise ValueError("assignment is stale or unknown")
        if str(row["phase"]) != phase:
            raise ValueError("assignment phase is stale")
        if phase == "triage" and str(row["triage_stage"]) != "materialize":
            raise ValueError("triage stages are incomplete")
        phase_index = _PHASES.index(phase)
        next_phase = _PHASES[phase_index + 1]
        revision = int(row["revision"]) + 1
        fields: dict[str, Any] = {
            "triage": task_ids[0] if phase == "triage" else row["triage_id"],
            "impl": task_ids if phase == "impl" else json.loads(str(row["impl_ids_json"])),
            "review": task_ids[0] if phase == "review" else row["review_id"],
            "summary": task_ids[0] if phase == "summary" else row["summary_id"],
        }
        next_assignment_id = _assignment_id(
            str(row["board"]),
            int(row["issue_number"]),
            int(row["generation"]),
            next_phase,
            revision,
        )
        conn.execute(
            """
            UPDATE authoring_workflow
            SET phase=?, revision=?, assignment_id=?, triage_id=?, impl_ids_json=?,
                review_id=?, summary_id=?, updated_at=?
            WHERE board=? AND assignment_id=?
            """,
            (
                next_phase,
                revision,
                next_assignment_id,
                fields["triage"],
                json.dumps(fields["impl"], separators=(",", ":")),
                fields["review"],
                fields["summary"],
                int(now_fn()),
                str(row["board"]),
                assignment_id,
            ),
        )
        updated = conn.execute(
            "SELECT * FROM authoring_workflow WHERE board=?", (str(row["board"]),)
        ).fetchone()
        conn.commit()
        return _public(updated)
    finally:
        conn.close()
