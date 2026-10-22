"""Canonical three-Step workflow used to prove profile-independent execution."""

from __future__ import annotations

from typing import Any

from .contracts import StepDefinition, Transition, WorkflowDefinition

_MAX_TEXT_CHARS = 4096
_TOOLSETS = ("vision",)


def profile_independent_e2e_workflow() -> WorkflowDefinition:
    """Build the A → B → C restart/resume acceptance workflow."""

    return WorkflowDefinition(
        steps=(
            StepDefinition(
                step_id="extract_requirement",
                goal=(
                    "Extract one concise requirement from input.request. "
                    "Return only the declared JSON object."
                ),
                allowed_toolsets=_TOOLSETS,
                acceptance_criteria=(
                    "requirement is a non-empty string grounded only in input.request.",
                    "No additional output fields are present.",
                ),
                output_schema=_single_text_schema("requirement"),
                projector=_project_request,
                validator=lambda output: _exact_text(output, "requirement"),
            ),
            StepDefinition(
                step_id="write_solution",
                goal=(
                    "Write one concise solution for input.requirement. "
                    "Return only the declared JSON object."
                ),
                allowed_toolsets=_TOOLSETS,
                acceptance_criteria=(
                    "solution is a non-empty string grounded only in input.requirement.",
                    "No additional output fields are present.",
                ),
                output_schema=_single_text_schema("solution"),
                projector=_project_requirement,
                validator=lambda output: _exact_text(output, "solution"),
            ),
            StepDefinition(
                step_id="review_solution",
                goal=(
                    "Review input.solution against input.requirement. "
                    "Return only the declared JSON object."
                ),
                allowed_toolsets=_TOOLSETS,
                acceptance_criteria=(
                    "decision is exactly approved or rejected.",
                    "rationale is a non-empty string grounded only in the projected input.",
                    "No additional output fields are present.",
                ),
                output_schema={
                    "type": "object",
                    "properties": {
                        "decision": {
                            "type": "string",
                            "enum": ["approved", "rejected"],
                        },
                        "rationale": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": _MAX_TEXT_CHARS,
                        },
                    },
                    "required": ["decision", "rationale"],
                    "additionalProperties": False,
                },
                projector=_project_review_input,
                validator=_valid_review,
            ),
        ),
        transitions=(
            Transition("extract_requirement", "write_solution"),
            Transition("write_solution", "review_solution"),
        ),
        initial_step_id="extract_requirement",
    )


def _single_text_schema(field: str) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            field: {
                "type": "string",
                "minLength": 1,
                "maxLength": _MAX_TEXT_CHARS,
            }
        },
        "required": [field],
        "additionalProperties": False,
    }


def _project_request(
    initial_input: dict[str, Any],
    _artifacts: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    return {"request": str(initial_input["request"]).strip()}


def _project_requirement(
    _initial_input: dict[str, Any],
    artifacts: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    return {
        "requirement": artifacts["extract_requirement"]["payload"]["requirement"]
    }


def _project_review_input(
    _initial_input: dict[str, Any],
    artifacts: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    return {
        "requirement": artifacts["extract_requirement"]["payload"]["requirement"],
        "solution": artifacts["write_solution"]["payload"]["solution"],
    }


def _exact_text(output: dict[str, Any], field: str) -> bool:
    value = output.get(field)
    return (
        set(output) == {field}
        and isinstance(value, str)
        and bool(value.strip())
        and len(value) <= _MAX_TEXT_CHARS
    )


def _valid_review(output: dict[str, Any]) -> bool:
    rationale = output.get("rationale")
    return (
        set(output) == {"decision", "rationale"}
        and output.get("decision") in {"approved", "rejected"}
        and isinstance(rationale, str)
        and bool(rationale.strip())
        and len(rationale) <= _MAX_TEXT_CHARS
    )