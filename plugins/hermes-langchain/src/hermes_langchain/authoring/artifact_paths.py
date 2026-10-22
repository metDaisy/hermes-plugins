"""Deterministic, board-isolated paths for fresh-agent authoring artifacts."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any


_SAFE_SEGMENT = re.compile(r"[^A-Za-z0-9._-]+")


def board_namespace(board: Any) -> str:
    text = str(board or "").strip()
    if not text:
        raise ValueError("board is required")
    slug = _SAFE_SEGMENT.sub("-", text).strip(".-_") or "board"
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
    return f"{slug[:48]}-{digest}"


def triage_output_dir(
    workspace: Path,
    *,
    board: Any,
    issue_number: int,
    generation: int,
) -> Path:
    return (
        Path(workspace)
        / ".temp"
        / "kwc-authoring"
        / str(issue_number)
        / f"generation-{generation}"
        / board_namespace(board)
    )


def task_graph_output_dir(
    workspace: Path,
    *,
    board: Any,
    issue_number: int,
    generation: int,
) -> Path:
    return (
        Path(workspace)
        / ".temp"
        / "task-graphs"
        / str(issue_number)
        / f"generation-{generation}"
        / board_namespace(board)
    )
