"""Filesystem and strict JSON helpers for task-graph authoring."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

def _temp_path(path: Path) -> Path:
    if path.is_absolute():
        raise ValueError("output path must be relative to this project's .temp directory")
    temp_root = (Path.cwd() / ".temp").resolve()
    destination = (Path.cwd() / path).resolve()
    if destination == temp_root or temp_root not in destination.parents:
        raise ValueError("output path must be relative to this project's .temp directory")
    return destination


def write_json(path: Path, payload: Any) -> None:
    destination = _temp_path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _read_json(path: Path) -> Any:
    def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise ValueError(f"DUPLICATE_JSON_KEY:{key}")
            value[key] = item
        return value

    return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=reject_duplicate_keys)
