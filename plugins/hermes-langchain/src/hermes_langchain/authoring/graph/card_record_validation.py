"""Stateful card indexing for build-task graph contracts."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any


TITLE = re.compile(
    r"^G(?P<generation>[1-9]\d*)-Issue(?P<issue>[1-9]\d*)-"
    r"(?P<kind>Triage|Impl[1-9]\d*|Review[1-9]\d*|Summary|Decision[1-9]\d*)$"
)
CARD_FIELDS = {
    "key",
    "title",
    "card_type",
    "assignee",
    "skills",
    "workspace",
    "status",
    "priority",
    "parents",
    "body",
}
CARD_TYPES = {"triage", "implementation", "review", "summary", "decision"}
EXPECTED_ASSIGNEES = {
    "triage": "project-manager",
    "implementation": "coder",
    "review": "reviewer",
    "summary": "project-manager",
    "decision": "project-manager",
}
STATUSES = {"triage", "running", "todo", "ready", "blocked", "done", "archived"}
TITLE_TYPES = {"Triage": "triage", "Summary": "summary"}


@dataclass(frozen=True)
class CardRecordResult:
    """One card's indexing result, preserving validation order."""

    key: str | None
    card: dict[str, Any] | None
    errors: tuple[str, ...]


class CardRecordIndex:
    """Accumulate card keys and titles while validating ordered metadata."""

    def __init__(self, *, generation: int, issue_number: int, workspace: str) -> None:
        self._generation = generation
        self._issue_number = issue_number
        self._workspace = workspace
        self._records: dict[str, dict[str, Any]] = {}
        self._titles: set[str] = set()

    @property
    def records(self) -> dict[str, dict[str, Any]]:
        return dict(self._records)

    def add(self, index: int, card: Any) -> CardRecordResult:
        if not isinstance(card, dict):
            return CardRecordResult(None, None, (f"CARD_NOT_OBJECT:{index}",))
        errors: list[str] = []
        for field in sorted(set(card) - CARD_FIELDS):
            errors.append(f"UNEXPECTED_CARD_FIELD:{index}:{field}")
        key = card.get("key")
        if not isinstance(key, str) or not key.strip():
            errors.append(f"MISSING_CARD_KEY:{index}")
            return CardRecordResult(None, None, tuple(errors))
        if key in self._records:
            errors.append(f"DUPLICATE_CARD_KEY:{key}")
        self._records[key] = card

        title = card.get("title")
        match = TITLE.fullmatch(str(card.get("title", "")))
        if title in self._titles:
            errors.append(f"DUPLICATE_CARD_TITLE:{title}")
        if isinstance(title, str):
            self._titles.add(title)
        if not match:
            errors.append(f"INVALID_TITLE:{key}")
        elif (
            int(match.group("generation")) != self._generation
            or int(match.group("issue")) != self._issue_number
        ):
            errors.append(f"TITLE_LINEAGE_MISMATCH:{key}")
        else:
            kind = match.group("kind")
            expected_type = TITLE_TYPES.get(
                kind,
                "implementation"
                if kind.startswith("Impl")
                else "review"
                if kind.startswith("Review")
                else "decision",
            )
            if expected_type != card.get("card_type"):
                errors.append(f"TITLE_CARD_TYPE_MISMATCH:{key}")

        card_type = card.get("card_type")
        if card_type not in CARD_TYPES:
            errors.append(f"INVALID_CARD_TYPE:{key}")
        if card.get("assignee") != EXPECTED_ASSIGNEES.get(card_type):
            errors.append(f"ASSIGNEE_MISMATCH:{key}")
        if card.get("skills") != []:
            errors.append(f"TASK_SKILLS_MUST_BE_EMPTY:{key}")
        if card.get("workspace") != self._workspace:
            errors.append(f"WORKSPACE_MISMATCH:{key}")
        if card.get("status") not in STATUSES:
            errors.append(f"INVALID_CARD_STATUS:{key}")
        priority = card.get("priority")
        if not isinstance(priority, int) or isinstance(priority, bool):
            errors.append(f"INVALID_CARD_PRIORITY:{key}")
        if not isinstance(card.get("parents"), list):
            errors.append(f"INVALID_PARENTS:{key}")
        return CardRecordResult(key, card, tuple(errors))
