---
name: kwc-summary-delivery-runtime
description: Use when a fresh controller Agent produces one accepted Issue Summary result from immutable staged artifacts.
---

# Controller Summary Delivery Runtime

Produce exactly one controller-claimed Issue Summary result.

## Inputs

- Treat `artifact_directory` as read-only.
- Read and verify `manifest.json`, `graph.json`, `activation.json`, and `dispatch.json`.
- Select only the graph card whose `idempotency_key` matches the activated task.
- Use only the pinned workspace and controller-staged accepted evidence.

## Execution

- Summarize verified implementation and review evidence required by the selected card.
- Do not mutate product code or Kanban, choose transitions, create lifecycle objects, inspect Profile SOUL/memory, or use prior conversations.
- Never describe an unverified check as passed.

## Output

Return only one JSON object matching the invocation's supplied delivery schema.
Each result entry is a logical result name plus the exact controller execution `step_id`, canonical payload, and SHA-256 of that payload. The controller owns persisted artifact IDs and completion.
