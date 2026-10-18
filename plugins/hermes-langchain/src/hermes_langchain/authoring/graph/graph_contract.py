#!/usr/bin/env python3
"""그래프 초안과 requirement-rework 비교 산출물을 검증·생성한다."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path
from typing import Any, Callable

from .behavior_link_validation import validate_behavior_implementation_links
from .card_body_validation import (
    REVIEW_GUIDE_LOCATORS,
    SUMMARY_RELEASE_EXCLUDED_ACTIONS,
    SUMMARY_REQUIRED_CONDITIONS,
    validate_card_body,
)
from .card_record_validation import CardRecordIndex, TITLE
from .contract_common import is_canonical_https_url
from .graph_metadata_validation import validate_graph_metadata
from .graph_state_validation import validate_graph_state
from .implementation_card_validation import (
    ImplementationCardContext,
    validate_implementation_card,
)
from .native_readback_validation import validate_native_readback
from .requirement_behavior_validation import validate_requirement_behaviors
from .review_link_validation import validate_review_links
from .summary_link_validation import validate_summary_links
from .topology_validation import validate_card_topology


GRAPH_SCHEMA = "build-task-graph-v6"


def validate_graph(
    graph: Any,
    *,
    phase: str = "draft",
    validate_implementation: Callable[[Any], list[str]] | None = None,
    source_review_result: Any = None,
) -> list[str]:
    """결정론적으로 graph wrapper와 persisted card body를 검증한다."""
    if phase not in {"draft", "native"}:
        return ["INVALID_PHASE"]
    errors: list[str] = []
    if not isinstance(graph, dict):
        return ["GRAPH_NOT_OBJECT"]
    metadata = validate_graph_metadata(
        graph,
        source_review_result=source_review_result,
    )
    errors.extend(metadata.errors)
    mode = metadata.mode
    issue_number = metadata.issue_number
    issue_url = metadata.issue_url
    generation = metadata.generation
    workspace = metadata.workspace
    planning_baseline_sha = metadata.planning_baseline_sha
    triage_digest = metadata.triage_digest

    requirement_behavior = validate_requirement_behaviors(graph)
    errors.extend(requirement_behavior.errors)
    behaviors = requirement_behavior.behaviors
    requirement_ids = set(requirement_behavior.requirement_ids)
    behavior_ids = set(requirement_behavior.behavior_ids)
    implementation_context = ImplementationCardContext(
        issue_number=issue_number,
        issue_url=issue_url,
        triage_digest=graph.get("triage_digest"),
        behavior_ids=frozenset(behavior_ids),
        requirement_ids=frozenset(requirement_ids),
        behaviors=tuple(behaviors or ()),
    )

    cards = graph.get("cards")
    if not isinstance(cards, list) or not cards:
        return errors + ["MISSING_CARDS"]
    card_index = CardRecordIndex(
        generation=generation,
        issue_number=issue_number,
        workspace=workspace,
    )
    for index, raw_card in enumerate(cards):
        record = card_index.add(index, raw_card)
        errors.extend(record.errors)
        if record.card is None or record.key is None:
            continue
        card = record.card
        errors.extend(validate_card_body(card, behavior_ids))
        errors.extend(
            validate_implementation_card(
                card,
                implementation_context,
                validate_external=validate_implementation,
            )
        )

    records = card_index.records
    topology = validate_card_topology(graph, records, mode=mode)
    errors.extend(topology.errors)
    triages = topology.triages
    summaries = topology.summaries
    reviews = topology.reviews

    behavior_links = validate_behavior_implementation_links(
        behaviors,
        records,
        mode=mode,
    )
    errors.extend(behavior_links.errors)
    implementation_keys = set(behavior_links.implementation_keys)
    planned_impl_keys = set(behavior_links.planned_impl_keys)
    review_links = validate_review_links(
        records,
        reviews,
        behavior_ids=behavior_ids,
        implementation_keys=implementation_keys,
        triage_digest=triage_digest,
        planning_baseline_sha=planning_baseline_sha,
    )
    errors.extend(review_links.errors)
    errors.extend(
        validate_summary_links(
            graph,
            records,
            summaries,
            triage_digest=triage_digest,
            issue_number=issue_number,
            issue_url=issue_url,
        )
    )

    errors.extend(
        validate_graph_state(
            graph,
            records,
            triages=triages,
            summaries=summaries,
            planned_impl_keys=planned_impl_keys,
            mode=mode,
            phase=phase,
        )
    )
    return errors


def validate_review_graph(
    graph: Any,
    *,
    validate_implementation: Callable[[Any], list[str]] | None = None,
) -> list[str]:
    """Validate a Review body against frozen source cards without revalidating graph topology."""
    if not isinstance(graph, dict) or graph.get("schema") != GRAPH_SCHEMA:
        return ["REVIEW_GRAPH_WRAPPER_REQUIRED"]
    errors = validate_graph(graph, validate_implementation=validate_implementation)
    review_prefixes = (
        "CARD_BODY_SCHEMA:review",
        "EPHEMERAL_COORDINATOR_CONTEXT:review",
        "UNEXPECTED_REVIEW_",
        "INVALID_REVIEW_",
        "MISSING_REVIEW_",
        "DUPLICATE_REVIEW_",
        "UNKNOWN_REVIEW_",
        "REVIEW_TRIAGE_",
        "REVIEW_BASELINE_",
        "REVIEW_IMPL_",
        "REVIEW_BEHAVIOR_",
        "REVIEW_ACCEPTANCE_",
        "REVIEW_CHECK_",
        "REVIEW_IMPLEMENTATION_",
        "REVIEW_TEST_",
        "REVIEW_ACTIVE_PLAN_",
        "REVIEW_CROSS_",
        "REVIEW_PRESERVATION_",
        "LOW_QUALITY_REVIEW_",
        "LATEST_REVIEW_",
        "MISSING_IMPLEMENTATION_BODY:",
        "IMPLEMENTATION_SCHEMA:",
        "IMPLEMENTATION:",
        "IMPLEMENTATION_ISSUE_",
        "IMPLEMENTATION_TRIAGE_DIGEST_MISMATCH:",
    )
    return [error for error in errors if error.startswith(review_prefixes)]


def _git(repository: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repository, check=True, text=True, encoding="utf-8", capture_output=True).stdout


def requirement_diff(repository: Path, base: str, revised: str, paths: list[str]) -> dict[str, Any]:
    """Git을 변경하지 않고 stable hunk ID를 가진 비교 산출물을 만든다."""
    for revision in (base, revised):
        _git(repository, "rev-parse", "--verify", f"{revision}^{{commit}}")
    command = ["diff", "--find-renames", "--find-copies", "--unified=0", "--no-ext-diff", base, revised, "--", *paths]
    raw = _git(repository, *command)
    hunks: list[dict[str, Any]] = []
    old_path = new_path = ""
    current: dict[str, Any] | None = None
    for line in raw.splitlines():
        if line.startswith("diff --git "):
            parts = line.split(" ", 3)
            old_path, new_path = parts[2][2:], parts[3][2:]
            current = None
        elif line.startswith("rename from ") or line.startswith("copy from "):
            old_path = line.split(" ", 2)[2]
        elif line.startswith("rename to ") or line.startswith("copy to "):
            new_path = line.split(" ", 2)[2]
        elif line.startswith("@@ "):
            match = re.match(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", line)
            if not match:
                continue
            current = {
                "old_path": old_path,
                "new_path": new_path,
                "old_start": int(match.group(1)),
                "old_count": int(match.group(2) or "1"),
                "new_start": int(match.group(3)),
                "new_count": int(match.group(4) or "1"),
                "lines": [],
            }
            hunks.append(current)
        elif current is not None and line.startswith(("+", "-")) and not line.startswith(("+++", "---")):
            current["lines"].append(line)
    for hunk in hunks:
        canonical = json.dumps(hunk, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        hunk["id"] = "h_" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
    return {
        "schema": "requirement-rework-comparison-v1",
        "provenance": {"base_revision": base, "revised_revision": revised, "paths": paths, "git_args": command},
        "hunks": hunks,
        "behavior_deltas": [],
        "behavior_delta_status": "human-review-required",
    }
