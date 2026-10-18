"""Hermes hook and tool registration for the plugin boundary.

The reference plugins keep ``register(ctx)`` as a small composition root.  This
module owns the declarative Hermes contracts while runtime handlers remain in
``entrypoint``.
"""

from __future__ import annotations

import copy
from collections.abc import Callable, Mapping
from typing import Any

Handler = Callable[[dict[str, Any]], str]
Callback = Callable[..., Any]

_TOOLSET = "hermes-langchain"


def _schema(
    name: str,
    description: str,
    properties: dict[str, Any],
    required: list[str],
    *,
    additional_properties: bool | None = False,
) -> dict[str, Any]:
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": copy.deepcopy(properties),
        "required": list(required),
    }
    if additional_properties is not None:
        parameters["additionalProperties"] = additional_properties
    return {
        "name": name,
        "description": description,
        "parameters": parameters,
    }


def _register_tool(
    ctx: Any,
    *,
    name: str,
    schema: dict[str, Any],
    handler: Handler,
    description: str,
    emoji: str,
    check_fn: Callback | None = None,
) -> None:
    metadata: dict[str, Any] = {"description": description, "emoji": emoji}
    if check_fn is not None:
        metadata["check_fn"] = check_fn
    ctx.register_tool(name, _TOOLSET, schema, handler, **metadata)


def register_plugin(
    ctx: Any,
    *,
    hooks: Mapping[str, Callback],
    handlers: Mapping[str, Handler],
    checks: Mapping[str, Callback],
) -> None:
    """Register the fixed Hermes contracts against supplied runtime callbacks."""

    for name, callback in hooks.items():
        ctx.register_hook(name, callback)

    _register_tool(
        ctx,
        name="kwc_advance_triage",
        schema=_schema(
            "kwc_advance_triage",
            "Validate the current semantic Triage stage and return only the next stage contract.",
            {
                "assignment_id": {
                    "type": "string",
                    "description": "Opaque assignment id supplied by KWC.",
                },
                "artifact_file": {
                    "type": "string",
                    "description": "Repo-relative JSON artifact for the current Triage stage.",
                },
            },
            ["assignment_id", "artifact_file"],
        ),
        handler=handlers["kwc_advance_triage"],
        description="Validate and advance one KWC-owned Triage stage.",
        emoji="🧭",
    )
    _register_tool(
        ctx,
        name="kwc_recover_frozen_triage",
        schema=_schema(
            "kwc_recover_frozen_triage",
            "Replace a bad Impl source with an exact validated frozen Triage body.",
            {
                "assignment_id": {
                    "type": "string",
                    "description": "Opaque Impl assignment id supplied by KWC.",
                },
                "frozen_body_file": {
                    "type": "string",
                    "description": "Repo-relative JSON file containing the exact frozen triage-v7 body.",
                },
            },
            ["assignment_id", "frozen_body_file"],
        ),
        handler=handlers["kwc_recover_frozen_triage"],
        description="Recover an exact frozen Triage before Impl materialization.",
        emoji="🧭",
    )

    authoring_contracts = {
        "kwc_create_triage": ("triage", {}, []),
        "kwc_create_impl_graph": (
            "impl",
            {"batch_file": {"type": "string"}},
            ["batch_file"],
        ),
        "kwc_create_review": (
            "review",
            {"body_file": {"type": "string"}},
            ["body_file"],
        ),
        "kwc_create_summary": ("summary", {}, []),
    }
    for tool_name, (phase, properties, required) in authoring_contracts.items():
        _register_tool(
            ctx,
            name=tool_name,
            schema=_schema(
                tool_name,
                f"Materialize and verify the current KWC {phase} authoring assignment.",
                {
                    "assignment_id": {
                        "type": "string",
                        "description": "Opaque assignment id supplied by KWC.",
                    },
                    **properties,
                },
                ["assignment_id", *required],
            ),
            handler=handlers[tool_name],
            description=f"Create and read back the KWC-owned {phase} card phase.",
            emoji="🧭",
        )

    shared_properties = {
        "board": {"type": "string", "description": "Kanban board slug; omit for current board."},
        "task_id": {"type": "string", "description": "Native Kanban task id."},
        "body": {"type": "string", "description": "Comment body."},
        "idempotency_key": {
            "type": "string",
            "description": "Stable unique key reused when retrying the same comment request.",
        },
    }
    _register_tool(
        ctx,
        name="kwc_comment",
        schema=_schema(
            "kwc_comment",
            "Add a KWC-routed comment. Mentions override the task assignee.",
            shared_properties,
            ["task_id", "body", "idempotency_key"],
            additional_properties=None,
        ),
        handler=handlers["kwc_comment"],
        description="Add a native Kanban comment and wake the routed Profile.",
        emoji="💬",
    )
    _register_tool(
        ctx,
        name="kwc_reply",
        schema=_schema(
            "kwc_reply",
            "Reply to a KWC comment without recursively waking another agent.",
            {
                **shared_properties,
                "reply_to_comment_id": {
                    "type": "integer",
                    "description": "Source comment id being answered.",
                },
            },
            ["task_id", "reply_to_comment_id", "body", "idempotency_key"],
            additional_properties=None,
        ),
        handler=handlers["kwc_reply"],
        description="Reply in the native Kanban thread without a routing loop.",
        emoji="↩️",
    )

    response_properties = {
        "invocation_id": {
            "type": "string",
            "description": "Opaque invocation id supplied by KWC.",
        },
        "outcome": {
            "type": "string",
            "description": "One of the outcomes allowed by this invocation.",
        },
        "task_id": {
            "type": "string",
            "description": "Related native Kanban task id, when applicable.",
        },
        "reason_code": {
            "type": "string",
            "description": "Compact stable reason code, not free-form reasoning.",
        },
    }
    _register_tool(
        ctx,
        name="kwc_pm_response",
        schema=_schema(
            "kwc_pm_response",
            "Submit the structured result for the current KWC PM invocation. "
            "The invocation, Profile, session, and allowed outcome are validated.",
            response_properties,
            ["invocation_id", "outcome"],
            additional_properties=None,
        ),
        handler=handlers["kwc_pm_response"],
        check_fn=checks["kwc_pm_response"],
        description="Submit a Profile/session-bound PM result without mutating Kanban.",
        emoji="📨",
    )
    _register_tool(
        ctx,
        name="kwc_profile_response",
        schema=_schema(
            "kwc_profile_response",
            "Submit the structured result for the current KWC Profile invocation. "
            "The invocation, Profile, session, and allowed outcome are validated.",
            response_properties,
            ["invocation_id", "outcome"],
            additional_properties=None,
        ),
        handler=handlers["kwc_profile_response"],
        check_fn=checks["kwc_profile_response"],
        description="Submit a Profile/session-bound typed result without mutating Kanban.",
        emoji="📨",
    )
    _register_tool(
        ctx,
        name="hermes_langchain_route",
        schema=_schema(
            "hermes_langchain_route",
            "Resolve the next KWC-compatible authoring action without mutating external state.",
            {
                "phase": {
                    "type": "string",
                    "enum": ["triage", "impl", "review", "summary", "complete"],
                },
                "triage_stage": {
                    "type": "string",
                    "enum": [
                        "intake",
                        "discovery",
                        "source_confirmation",
                        "analysis",
                        "materialize",
                    ],
                },
            },
            ["phase"],
        ),
        handler=handlers["hermes_langchain_route"],
        description="Resolve one deterministic LangGraph authoring route.",
        emoji="🕸️",
    )
