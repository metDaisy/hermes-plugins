---
name: kwc-review-delivery-runtime
description: Use when a fresh controller Agent evaluates one accepted aggregate Review card from immutable staged artifacts.
---

# Controller Review Delivery Runtime

Evaluate exactly one controller-claimed aggregate Review card.

## Inputs

- Treat `artifact_directory` as read-only.
- Read and verify `manifest.json`, `graph.json`, `activation.json`, and `dispatch.json`.
- Select only the graph card whose `idempotency_key` matches the activated task.
- Inspect the controller-pinned workspace and available accepted result artifacts only.

## Execution

- Perform the selected review checks and required verification without changing product code.
- Do not read or mutate Kanban, choose transitions, create lifecycle objects, inspect Profile SOUL/memory, or use prior conversations.
- Report evidence honestly; missing evidence is a blocked result, not permission to infer success.

## Output

Return only one JSON object matching the invocation's supplied delivery schema.
Each result entry is a logical result name plus the exact controller execution `step_id`, canonical payload, and SHA-256 of that payload. The controller owns persisted artifact IDs and completion.
