"""Metadata consistency checks for the unified agent-audit plugin."""

from __future__ import annotations

import json
import re
from pathlib import Path


_ROOT = Path(__file__).parent


def _yaml_metadata() -> dict[str, str]:
    values: dict[str, str] = {}
    for line in (_ROOT / "plugin.yaml").read_text(encoding="utf-8").splitlines():
        key, separator, value = line.partition(":")
        if separator and key in {"name", "version"}:
            values[key] = value.strip().strip("'\"")
    return values


def test_unified_plugin_metadata_matches() -> None:
    plugin = _yaml_metadata()
    dashboard = json.loads((_ROOT / "dashboard" / "manifest.json").read_text(encoding="utf-8"))

    assert plugin["name"] == "agent-audit"
    assert re.fullmatch(r"0\.\d+\.\d+", plugin["version"])
    assert dashboard["name"] == plugin["name"]
    assert dashboard["version"] == plugin["version"]


if __name__ == "__main__":
    test_unified_plugin_metadata_matches()
    print("agent-audit metadata tests: passed")