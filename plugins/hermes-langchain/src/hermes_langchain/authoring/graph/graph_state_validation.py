"""Graph activation, native status, and archive validation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ReworkState:
    source_review: dict[str, Any]
    dispositions: list[Any]
    appended: set[str]
    decision_only: bool


def _error(errors: list[str], condition: bool, code: str) -> None:
    if not condition:
        errors.append(code)


def _rework_state(
    graph: dict[str, Any],
    records: dict[str, dict[str, Any]],
    mode: Any,
) -> ReworkState:
    source_review = graph.get("source_review") if isinstance(graph.get("source_review"), dict) else {}
    dispositions = source_review.get("dispositions", []) if isinstance(source_review, dict) else []
    appended = {
        key
        for disposition in dispositions
        if isinstance(disposition, dict)
        for key in disposition.get("appended_card_keys", [])
    }
    appended_types = {records[key].get("card_type") for key in appended if key in records}
    decision_only = mode == "review-rework" and bool(appended) and appended_types == {"decision"}
    return ReworkState(
        source_review=source_review,
        dispositions=dispositions,
        appended=appended,
        decision_only=decision_only,
    )


def _expected_disposition_types(verdict: Any) -> set[str]:
    if verdict == "correction-required":
        return {"implementation"}
    if verdict == "context-required":
        return {"review"}
    return {"decision"}


def _validate_rework_dispositions(
    records: dict[str, dict[str, Any]],
    state: ReworkState,
    ready_candidate: Any,
    errors: list[str],
) -> None:
    _error(errors, (ready_candidate is None) == state.decision_only, "REVIEW_REWORK_ACTIVATION_MISMATCH")
    _error(errors, set(state.source_review.get("idempotency", {})) == state.appended, "IDEMPOTENCY_CARD_COVERAGE")
    for disposition in state.dispositions:
        if not isinstance(disposition, dict):
            continue
        keys = disposition.get("appended_card_keys", [])
        types = {records[key].get("card_type") for key in keys if key in records}
        verdict = disposition.get("verdict")
        _error(
            errors,
            bool(types & _expected_disposition_types(verdict)),
            f"SOURCE_VERDICT_CARD_TYPE:{disposition.get('finding_id')}",
        )
        if verdict == "decision-required":
            _validate_decision_disposition(records, state.source_review, disposition, keys, errors)


def _validate_decision_disposition(
    records: dict[str, dict[str, Any]],
    source_review: dict[str, Any],
    disposition: dict[str, Any],
    keys: list[Any],
    errors: list[str],
) -> None:
    for key in keys:
        card = records.get(key, {})
        if card.get("card_type") != "decision" or not isinstance(card.get("body"), dict):
            continue
        _error(
            errors,
            card["body"].get("source_review_key") == source_review.get("review_key"),
            f"DECISION_SOURCE_REVIEW_MISMATCH:{key}",
        )
        _error(
            errors,
            disposition.get("finding_id") in card["body"].get("finding_ids", []),
            f"DECISION_SOURCE_FINDING_MISMATCH:{key}",
        )


def _validate_ready_candidate(
    records: dict[str, dict[str, Any]],
    triages: tuple[str, ...],
    planned_impl_keys: set[str],
    mode: Any,
    state: ReworkState,
    ready_candidate: Any,
    errors: list[str],
) -> None:
    _error(errors, state.decision_only or ready_candidate in records, "READY_CANDIDATE_NOT_FOUND")
    if ready_candidate not in records or not triages:
        return
    candidate = records[ready_candidate]
    _error(errors, candidate.get("card_type") in {"implementation", "review"}, "READY_CANDIDATE_TYPE")
    if mode == "review-rework":
        source_key = state.source_review.get("review_key")
        _error(errors, source_key in candidate.get("parents", []), "READY_CANDIDATE_NOT_SOURCE_REVIEW_CHILD")
        _error(errors, ready_candidate in state.appended, "READY_CANDIDATE_NOT_APPENDED")
        return
    _error(errors, candidate.get("parents") == [triages[0]], "READY_CANDIDATE_PARENT_SET")
    if planned_impl_keys:
        _error(errors, candidate.get("card_type") == "implementation", "READY_CANDIDATE_NOT_IMPL")
    else:
        _error(errors, candidate.get("card_type") == "review", "NO_IMPL_REQUIRES_REVIEW_CANDIDATE")


def _is_historical_card(
    key: str,
    summaries: tuple[str, ...],
    mode: Any,
    appended: set[str],
) -> bool:
    return mode == "review-rework" and key not in appended and key != summaries[0]


def _validate_draft_statuses(
    records: dict[str, dict[str, Any]],
    triages: tuple[str, ...],
    summaries: tuple[str, ...],
    mode: Any,
    state: ReworkState,
    ready: list[str],
    errors: list[str],
) -> None:
    _error(errors, not ready, "DRAFT_HAS_READY_CARD")
    if triages:
        expected_triage = "done" if mode == "review-rework" else "triage"
        _error(errors, records[triages[0]].get("status") == expected_triage, "DRAFT_TRIAGE_STATUS")
    for key, card in records.items():
        if key in triages or card.get("status") == "archived":
            continue
        if _is_historical_card(key, summaries, mode, state.appended):
            _error(errors, card.get("status") == "done", f"HISTORICAL_CARD_NOT_DONE:{key}")
            continue
        expected = {"todo", "blocked"} if card.get("card_type") == "decision" else {"todo"}
        _error(errors, card.get("status") in expected, f"DRAFT_ACTIVE_STATUS:{key}")
    source_key = state.source_review.get("review_key")
    if mode == "review-rework" and source_key in records:
        _error(errors, records[source_key].get("status") == "done", "SOURCE_REVIEW_NOT_DONE")


def _validate_native_statuses(
    records: dict[str, dict[str, Any]],
    triages: tuple[str, ...],
    summaries: tuple[str, ...],
    mode: Any,
    state: ReworkState,
    ready_candidate: Any,
    ready: list[str],
    errors: list[str],
) -> None:
    expected_ready = [] if state.decision_only else [ready_candidate]
    _error(errors, ready == expected_ready, "NATIVE_READY_COUNT_OR_ID")
    if triages:
        _error(errors, records[triages[0]].get("status") == "done", "NATIVE_TRIAGE_NOT_DONE")
    for key, card in records.items():
        historical = _is_historical_card(key, summaries, mode, state.appended)
        if key in triages or key == ready_candidate or card.get("status") == "archived" or historical:
            if historical:
                _error(errors, card.get("status") == "done", f"HISTORICAL_CARD_NOT_DONE:{key}")
            continue
        expected = {"todo", "blocked"} if card.get("card_type") == "decision" else {"todo"}
        _error(errors, card.get("status") in expected, f"NATIVE_ACTIVE_STATUS:{key}")
    if state.decision_only:
        _error(errors, not ready, "DECISION_ONLY_HAS_READY_CARD")


def _validate_rework_references(
    records: dict[str, dict[str, Any]],
    state: ReworkState,
    errors: list[str],
) -> None:
    source_key = state.source_review.get("review_key")
    _error(
        errors,
        source_key in records and records[source_key].get("card_type") == "review",
        "SOURCE_REVIEW_NOT_FOUND",
    )
    for key in state.appended:
        _error(errors, key in records, f"APPENDED_CARD_NOT_FOUND:{key}")


def _validate_archive(
    graph: dict[str, Any],
    records: dict[str, dict[str, Any]],
    errors: list[str],
) -> None:
    archived = graph.get("archived_card_keys")
    _error(errors, isinstance(archived, list), "INVALID_ARCHIVED_CARD_KEYS")
    if not isinstance(archived, list):
        return
    for key in archived:
        card = records.get(key)
        _error(errors, card is not None, f"ARCHIVED_CARD_NOT_FOUND:{key}")
        if card:
            _error(errors, card.get("status") == "archived", f"ARCHIVE_STATUS_MISMATCH:{key}")
            _error(errors, card.get("card_type") in {"implementation", "review", "summary"}, f"ARCHIVE_TYPE_FORBIDDEN:{key}")
    for key, card in records.items():
        if card.get("status") == "archived" and key not in archived:
            errors.append(f"ARCHIVED_CARD_UNDECLARED:{key}")


def validate_graph_state(
    graph: dict[str, Any],
    records: dict[str, dict[str, Any]],
    *,
    triages: tuple[str, ...],
    summaries: tuple[str, ...],
    planned_impl_keys: set[str],
    mode: Any,
    phase: str,
) -> tuple[str, ...]:
    """Validate activation, statuses, review-rework appendages, and archives."""

    errors: list[str] = []
    ready_candidate = graph.get("ready_candidate")
    state = _rework_state(graph, records, mode)
    if mode == "review-rework":
        _validate_rework_dispositions(records, state, ready_candidate, errors)
    _validate_ready_candidate(
        records,
        triages,
        planned_impl_keys,
        mode,
        state,
        ready_candidate,
        errors,
    )
    ready = [key for key, card in records.items() if card.get("status") == "ready"]
    if phase == "draft":
        _validate_draft_statuses(records, triages, summaries, mode, state, ready, errors)
    else:
        _validate_native_statuses(
            records,
            triages,
            summaries,
            mode,
            state,
            ready_candidate,
            ready,
            errors,
        )
    if mode == "review-rework" and isinstance(graph.get("source_review"), dict):
        _validate_rework_references(records, state, errors)
    _validate_archive(graph, records, errors)
    return tuple(errors)
