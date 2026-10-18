#!/usr/bin/env python3
"""Read-only freshness and document checks for docs/current-state.md."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

SCHEMA = "current-state-inspection-v1"
SNAPSHOT_PATTERN = re.compile(r"확인 기준 Git SHA:\s*`([0-9a-f]{40})`")
REQUIRED_SECTIONS = [
    "스냅샷",
    "전체 진행 요약",
    "백엔드 구조",
    "확인된 HTTP 진입점",
    "주요 구현 규칙",
    "데이터베이스",
    "테스트·검증",
    "알려진 차이와 다음 작업",
]
FORBIDDEN_SECTIONS = {"프런트엔드", "테스트·자동화"}
MARKER_SECTION = "진행 중 구현 작업"
MARKER_FIELDS = [
    "추적 기준",
    "계보",
    "delivery branch",
    "최초 기준 SHA",
    "최초 implementation snapshot SHA",
    "requirement 위치",
    "작업 요약",
]


def run_git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def lines(value: str) -> list[str]:
    return [line.strip() for line in value.splitlines() if line.strip()]


def parse_document(document: Path) -> tuple[str | None, list[str], dict[str, str]]:
    text = document.read_text(encoding="utf-8")
    match = SNAPSHOT_PATTERN.search(text)
    headings = re.findall(r"^## (.+?)\s*$", text, flags=re.MULTILINE)
    sections: dict[str, str] = {}
    matches = list(re.finditer(r"^## (.+?)\s*$", text, flags=re.MULTILINE))
    for index, heading in enumerate(matches):
        start = heading.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        sections[heading.group(1)] = text[start:end]
    return (match.group(1) if match else None), headings, sections


def validate_document(document: Path) -> list[str]:
    errors: list[str] = []
    try:
        snapshot, headings, sections = parse_document(document)
    except (OSError, UnicodeError) as exc:
        return [f"DOCUMENT_READ_FAILED:{exc}"]

    if snapshot is None:
        errors.append("MISSING_SNAPSHOT_SHA")
    positions = []
    for section in REQUIRED_SECTIONS:
        if section not in headings:
            errors.append(f"MISSING_SECTION:{section}")
        else:
            positions.append(headings.index(section))
    if positions != sorted(positions):
        errors.append("INVALID_SECTION_ORDER")
    for section in sorted(FORBIDDEN_SECTIONS.intersection(headings)):
        errors.append(f"OUT_OF_SCOPE_SECTION:{section}")

    marker = sections.get(MARKER_SECTION)
    if marker is not None:
        if "스냅샷" not in headings or headings.index(MARKER_SECTION) != headings.index("스냅샷") + 1:
            errors.append("INVALID_MARKER_POSITION")
        for field in MARKER_FIELDS:
            if not re.search(rf"^- {re.escape(field)}:\s*\S", marker, flags=re.MULTILINE):
                errors.append(f"MISSING_MARKER_FIELD:{field}")
        marker_keys = re.findall(r"^- ([^:]+):", marker, flags=re.MULTILINE)
        for key in marker_keys:
            if key not in MARKER_FIELDS:
                errors.append(f"UNEXPECTED_MARKER_FIELD:{key}")
        for field in ("최초 기준 SHA", "최초 implementation snapshot SHA"):
            if not re.search(rf"^- {re.escape(field)}:\s*[0-9a-f]{{40}}\s*$", marker, flags=re.MULTILINE):
                errors.append(f"INVALID_MARKER_SHA:{field}")
    return errors


def candidate_sections(paths: list[str]) -> list[str]:
    candidates = {"전체 진행 요약", "알려진 차이와 다음 작업"}
    for path in paths:
        normalized = path.replace("\\", "/")
        if normalized.startswith("src/main/java/"):
            candidates.update({"백엔드 구조", "확인된 HTTP 진입점", "주요 구현 규칙"})
        elif normalized.startswith("src/main/resources/db/migration/"):
            candidates.update({"데이터베이스", "주요 구현 규칙"})
        elif normalized.startswith("src/main/resources/"):
            candidates.update({"백엔드 구조", "주요 구현 규칙"})
        elif normalized.startswith("src/test/"):
            candidates.add("테스트·검증")
    return [section for section in REQUIRED_SECTIONS if section in candidates]


def inspect(repo: Path, document: Path) -> dict[str, object]:
    result: dict[str, object] = {
        "schema": SCHEMA,
        "freshness": "insufficient",
        "inspection_sha": None,
        "snapshot_sha": None,
        "snapshot_is_ancestor": None,
        "worktree_clean": False,
        "dirty_paths": [],
        "covered_changed_paths": [],
        "excluded_changed_paths": [],
        "candidate_sections": [],
        "checklist": [],
        "errors": [],
    }
    errors: list[str] = result["errors"]  # type: ignore[assignment]

    try:
        snapshot_sha, _, _ = parse_document(document)
    except (OSError, UnicodeError) as exc:
        errors.append(f"DOCUMENT_READ_FAILED:{exc}")
        return result
    result["snapshot_sha"] = snapshot_sha
    errors.extend(validate_document(document))

    head = run_git(repo, "rev-parse", "HEAD")
    if head.returncode != 0:
        errors.append("HEAD_UNAVAILABLE")
        return result
    inspection_sha = head.stdout.strip()
    result["inspection_sha"] = inspection_sha

    status = run_git(repo, "status", "--porcelain", "--untracked-files=all")
    if status.returncode != 0:
        errors.append("WORKTREE_STATUS_UNAVAILABLE")
        return result
    dirty_paths = []
    for line in lines(status.stdout):
        path = line[3:] if len(line) > 3 else line
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        dirty_paths.append(path)
    result["dirty_paths"] = dirty_paths
    result["worktree_clean"] = not dirty_paths

    if snapshot_sha is None:
        errors.append("SNAPSHOT_SHA_UNAVAILABLE")
        return result
    verify = run_git(repo, "cat-file", "-e", f"{snapshot_sha}^{{commit}}")
    if verify.returncode != 0:
        errors.append("SNAPSHOT_COMMIT_UNAVAILABLE")
        return result

    ancestry = run_git(repo, "merge-base", "--is-ancestor", snapshot_sha, inspection_sha)
    if ancestry.returncode not in (0, 1):
        errors.append("ANCESTRY_UNAVAILABLE")
        return result
    is_ancestor = ancestry.returncode == 0
    result["snapshot_is_ancestor"] = is_ancestor
    if not is_ancestor:
        errors.append("SNAPSHOT_NOT_ANCESTOR")
        return result

    changed = run_git(repo, "diff", "--name-only", f"{snapshot_sha}..{inspection_sha}")
    if changed.returncode != 0:
        errors.append("CHANGED_PATHS_UNAVAILABLE")
        return result
    changed_paths = lines(changed.stdout)
    covered = [path for path in changed_paths if path.replace("\\", "/").startswith("src/")]
    excluded = [path for path in changed_paths if path not in covered]
    result["covered_changed_paths"] = covered
    result["excluded_changed_paths"] = excluded
    result["candidate_sections"] = candidate_sections(covered)
    result["checklist"] = [
        "inspection HEAD와 snapshot SHA의 ancestry를 확인한다.",
        "변경된 모든 src/** 경로를 main/test/resource로 grouping한다.",
        "각 candidate section의 서술을 committed src/** evidence로 재확인한다.",
        "실행한 검증과 실행하지 않은 검증을 구분한다.",
        "문서 변경 시 implementation SHA와 docs commit SHA를 분리해 read-back한다.",
    ]

    if dirty_paths:
        errors.append("WORKTREE_DIRTY")
    elif covered:
        result["freshness"] = "stale"
    else:
        result["freshness"] = "fresh"
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    inspect_parser = subparsers.add_parser("inspect")
    inspect_parser.add_argument("--repo", type=Path, default=Path("."))
    inspect_parser.add_argument("--document", type=Path, default=Path("docs/current-state.md"))

    validate_parser = subparsers.add_parser("validate-document")
    validate_parser.add_argument("--document", type=Path, default=Path("docs/current-state.md"))

    args = parser.parse_args(argv)
    if args.command == "inspect":
        payload = inspect(args.repo.resolve(), args.document.resolve())
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0 if payload["freshness"] != "insufficient" else 1

    errors = validate_document(args.document.resolve())
    print(json.dumps({"valid": not errors, "errors": errors}, ensure_ascii=False, indent=2))
    return 0 if not errors else 1


if __name__ == "__main__":
    sys.exit(main())
