---
name: kwc-implementation-delivery-runtime
description: Use when a fresh controller Agent executes one accepted Implementation card from immutable staged artifacts.
---

# Controller Implementation Delivery Runtime

Execute exactly one controller-claimed Implementation card.

## Inputs

- Treat `artifact_directory` as read-only.
- Read and verify `manifest.json`, `graph.json`, `activation.json`, and `dispatch.json`.
- Select only the graph card whose `idempotency_key` matches the activated task.
- Work only in the controller-pinned workspace supplied by the invocation.

## Execution

- Implement only the selected card and its acceptance checks.
- Use repository guidance and available file, code-search, and build/test tools.
- Do not read or mutate Kanban, choose transitions, create lifecycle objects, inspect Profile SOUL/memory, or use prior conversations.
- Never weaken tests or fabricate verification output.

## Output

Return only one JSON object matching the invocation's supplied delivery schema.
Each result entry is a logical result name plus the exact controller execution `step_id`, canonical payload, and SHA-256 of that payload. The controller owns persisted artifact IDs and completion.
