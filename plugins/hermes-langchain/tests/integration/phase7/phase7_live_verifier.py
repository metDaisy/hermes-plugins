from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any, Sequence


_CUTOVER_PLUGINS = {
    "context-workflow-harness",
    "hermes-langchain",
    "kanban-workflow-controller",
}


def select_plugin_states(plugins: list[dict[str, Any]]) -> dict[str, dict[str, str]]:
    return {
        item["name"]: {"status": item["status"], "version": item["version"]}
        for item in plugins
        if item.get("name") in _CUTOVER_PLUGINS
    }


def select_issue_boards(boards: list[dict[str, Any]]) -> list[dict[str, Any]]:
    selected = [
        {
            "slug": item["slug"],
            "archived": item["archived"],
            "counts": item["counts"],
            "total": item["total"],
        }
        for item in boards
        if str(item.get("slug", "")).startswith("issue-151")
    ]
    return sorted(selected, key=lambda item: item["slug"])


def digest_issue_boards(boards: list[dict[str, Any]]) -> str:
    normalized = sorted(boards, key=lambda item: item["slug"])
    encoded = json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _run(arguments: Sequence[str], *, cwd: Path | None = None) -> str:
    completed = subprocess.run(
        list(arguments),
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if completed.returncode != 0:
        raise RuntimeError(f"read-back failed for {arguments[0]}")
    return completed.stdout


def _run_json(arguments: Sequence[str]) -> list[dict[str, Any]]:
    payload = json.loads(_run(arguments))
    if not isinstance(payload, list) or not all(isinstance(item, dict) for item in payload):
        raise RuntimeError(f"invalid read-back shape for {arguments[0]}")
    return payload


def verify_live_evidence(assessment: dict[str, Any], product_repo: Path) -> None:
    expected_plugins = {
        assessment["canonical_plugin"]["name"]: {
            "status": assessment["canonical_plugin"]["installed_status"],
            "version": assessment["canonical_plugin"]["version"],
        },
        assessment["deprecated_paths"][0]["name"]: {
            "status": assessment["deprecated_paths"][0]["installed_status"],
            "version": "0.27.0",
        },
    }
    actual_plugins = select_plugin_states(_run_json(["hermes", "plugins", "list", "--json"]))
    if actual_plugins != expected_plugins:
        raise RuntimeError("installed Plugin state no longer matches the assessment")
    if "context-workflow-harness" in actual_plugins:
        raise RuntimeError("deprecated context workflow harness is unexpectedly installed")

    before = select_issue_boards(
        _run_json(["hermes", "kanban", "boards", "list", "--all", "--json"])
    )
    expected_inventory = assessment["issue_151"]["native_board_inventory"]
    before_digest = digest_issue_boards(before)
    if len(before) != expected_inventory["issue_151_board_count"]:
        raise RuntimeError("Issue #151 board inventory count is stale")
    if before_digest != expected_inventory["snapshot_before_sha256"]:
        raise RuntimeError("Issue #151 board inventory digest is stale")
    quality = next((item for item in before if item["slug"] == "issue-151-quality-v6"), None)
    if quality is None or quality["counts"] != expected_inventory["quality_v6_status_counts"]:
        raise RuntimeError("Issue #151 quality-v6 state is stale")

    repository = assessment["issue_151"]["repository"]
    if _run(["git", "rev-parse", "HEAD"], cwd=product_repo).strip() != repository["head"]:
        raise RuntimeError("product Git HEAD is stale")
    if _run(["git", "branch", "--show-current"], cwd=product_repo).strip() != repository["branch"]:
        raise RuntimeError("product Git branch is stale")
    dirty = bool(_run(["git", "status", "--porcelain"], cwd=product_repo).strip())
    if dirty != repository["worktree_dirty"]:
        raise RuntimeError("product Git worktree state is stale")

    after = select_issue_boards(
        _run_json(["hermes", "kanban", "boards", "list", "--all", "--json"])
    )
    after_digest = digest_issue_boards(after)
    if after_digest != before_digest or after_digest != expected_inventory["snapshot_after_sha256"]:
        raise RuntimeError("native board state changed during the read-only verifier")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--assessment", type=Path, required=True)
    parser.add_argument("--product-repo", type=Path, required=True)
    args = parser.parse_args()
    assessment = json.loads(args.assessment.read_text(encoding="utf-8"))
    verify_live_evidence(assessment, args.product_repo)
    print("PHASE7_LIVE_EVIDENCE_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
