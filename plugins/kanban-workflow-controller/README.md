# Kanban Workflow Controller

Kanban Workflow Controller routes native Kanban lifecycle events to the Project Manager and adds a KWC-owned comment conversation path between Profiles. It keeps native Kanban as the source of truth: every KWC comment is persisted by `kanban_db.add_comment()` and therefore appears in the ordinary task-card thread and `commented` event history.

## Versioning

`plugin.yaml` is the canonical version source. This plugin is a new product line that started at `0.1.0`; it does not continue the retired `kanban-control` `/kcp` command contract.

- patch: compatible bug fixes and routing corrections;
- minor: backward-compatible event, tool, endpoint, or session-routing capabilities;
- major: incompatible state, wake-envelope, configuration, or installation contracts.

Every functional change must update `plugin.yaml` and add the matching newest release to `CHANGELOG.md`. Documentation-only corrections may keep the current version.

## Runtime model

Lifecycle routing remains passive:

```text
PM invokes /run-workflow
→ KWC binds the durable Project Manager session
→ native completion/block hooks or dispatcher reconciliation read task_events
→ actionable event wakes the bound Project Manager session
```

Comment routing is KWC-owned and Core-independent:

```text
Desktop calls POST /api/plugins/kanban-workflow-controller/comments
  or an Agent calls kwc_comment
→ native kanban_db.add_comment() persists the comment + commented event
→ KWC records a delivery request without copying the comment body
→ explicit @{profile} mentions override the task assignee
→ KWC wakes each target Profile's latest bound top-level session
→ the Agent reads the canonical task/comments and answers through kwc_reply
→ kwc_reply persists a native comment but suppresses automatic re-routing
```

The endpoint schedules an immediate native dispatcher nudge after the write. Regular dispatcher ticks process any delivery that was missed, rejected, or waiting for a target Profile session, so reconciliation remains the recovery path rather than the primary latency mechanism.

## Comment API and tools

### Human/Desktop comment

```http
POST /api/plugins/kanban-workflow-controller/comments
Content-Type: application/json

{
  "board": "issue151",
  "task_id": "t_impl",
  "body": "@{reviewer} 이 변경을 확인해주세요",
  "idempotency_key": "ui-7f5d21"
}
```

The endpoint owns the author as `desktop`; a client-provided `author` value is ignored.

The current Desktop Plugin SDK cannot intercept or replace the built-in Kanban comment composer. That composer does not call the KWC endpoint, so ordinary task-card comments do not receive this path's immediate routing. A human client must call the endpoint explicitly; native CLI/tool/plugin comments remain visible in Kanban but bypass the immediate KWC queue.

### Agent comment

```text
kwc_comment(
  board="issue151",
  task_id="t_impl",
  body="@{reviewer} 확인해주세요",
  idempotency_key="agent-7f5d21"
)
```

Use this when an Agent intentionally starts or forwards a comment conversation. If the body has no explicit mention, KWC routes to the task's current assignee.

Reuse the same `idempotency_key` when retrying the same request. KWC stores only a SHA-256 digest of the body in the request ledger; a completed replay returns the original comment and delivery IDs instead of creating a duplicate native comment.

### Agent reply

```text
kwc_reply(
  board="issue151",
  task_id="t_impl",
  reply_to_comment_id=481,
  body="확인했습니다. 수정이 필요합니다.",
  idempotency_key="reply-481-a"
)
```

`kwc_reply` records the reply relationship in KWC state and does not create another automatic wake. A reply can therefore not loop endlessly between Profiles. Use a new idempotency key for each intentional reply, and reuse that key only when retrying the same reply.

## Routing and privacy boundary

- `@{profile}` accepts letters, digits, `.`, `_`, and `-`; duplicate mentions are delivered once in appearance order.
- One or more explicit mentions replace the assignee fallback.
- No mention routes to the task's current assignee.
- No target or a loop-suppressed reply is persisted but not woken.
- A missing target session stays `retry_pending` until a later dispatcher tick can deliver it.
- Target Profiles are materialized once before the first injection, so reassignment during a retry cannot add a second recipient.
- KWC stores comment IDs, task/board identity, target Profile, reply relationship, and delivery state. It does not copy comment bodies into `state.db` or wake envelopes.

KWC does not require a custom `kanban_task_commented` Core hook, monkey patching, or a duplicate Kanban comment table. Comments created through unrelated native CLI/tool/plugin paths still bypass the immediate KWC queue; existing dispatcher event reconciliation continues to handle only the established workflow-decision comment contract.

A comment wake contains only locators and instructions:

```json
{
  "schema": "kanban-comment-wakeup-v1",
  "board": "issue151",
  "source_task_id": "t_impl",
  "source_comment_id": 481,
  "reply_to_comment_id": null,
  "target_profile": "reviewer",
  "instruction": "Read the source task and canonical comment thread, answer the comment, then use kwc_reply with source_comment_id as reply_to_comment_id."
}
```

## Configuration

Enable the plugin in every place that participates in routing:

1. the `project-manager` Profile, which observes `/run-workflow` and binds the coordinator session;
2. each Profile listed in `comment_profiles`, so its latest top-level session can be bound;
3. the gateway/dispatcher host Profile, which observes dispatch ticks and injects wake messages.

The unified Desktop half has a separate opt-in gate. After installation, open **Capabilities → Plugins** and enable the Desktop toggle for `kanban-workflow-controller`; enabling the Agent plugin alone does not show the status item.

KWC does not have a board allowlist. The gateway's native dispatch tick and lifecycle hooks identify boards as they appear; KWC keeps a separate durable cursor and heartbeat for each board. A newly observed board starts at its current event frontier, while an immediate lifecycle hook includes the event that triggered discovery.

The default comment targets are `project-manager,coder,reviewer`. Change the comma-separated list only when the workflow uses different literal Profile names:

```bash
hermes -p main config set plugins.entries.kanban-workflow-controller.settings.comment_profiles project-manager,coder,reviewer
```

Each listed Profile must receive at least one normal top-level turn after the plugin loads, allowing KWC to capture its durable `HERMES_SESSION_KEY`. Until then, comments targeting that Profile remain queued as `retry_pending`.

The default coordinator Profile is `project-manager`. Override it only if the workflow uses another literal Profile name:

```bash
hermes -p main config set plugins.entries.kanban-workflow-controller.settings.coordinator_profile project-manager
```

Desktop Kanban writes a human comment with author `dashboard` by default. If the actual user-facing comment surface writes another stable author, configure the same literal value on the dispatcher host:

```bash
hermes -p main config set plugins.entries.kanban-workflow-controller.settings.decision_comment_author dashboard
```

Message injection is capability-gated by Hermes and must be explicitly allowed on the Profile whose plugin instance handles dispatch ticks:

```bash
hermes -p main config set plugins.entries.kanban-workflow-controller.allow_gateway_injection true
```

The global coordinator binding, per-Profile comment-session bindings, per-board event cursors/heartbeats, comment delivery ledger, and activity log are stored machine-wide under:

```text
$HERMES_ROOT/plugin-data/kanban-workflow-controller/state.db
```

This file is only a routing/delivery index and privacy-safe activity log. It stores no comment body; Kanban remains the comment, workflow, and lifecycle authority.

## Activity log

KWC records observable controller actions for 30 days in the same `state.db`:

- board monitoring becoming active;
- coordinator session binding;
- actionable events accepted for delivery;
- non-actionable events filtered;
- rejected delivery left pending for retry;
- event hints skipped because no coordinator session is bound.

Each record contains only time, board, fixed action/outcome/reason codes, task ID, source event ID, and event kind. It never stores comment bodies, prompts, wake-message text, session keys, reasoning, raw commands/results, or credentials.

The authenticated endpoint is `/api/plugins/kanban-workflow-controller/activity?limit=100`. Desktop exposes the same records through the **KWC logs** sidebar page.

## Desktop status

The unified Desktop half contributes a compact item to `statusBar.right`, the right side of the bottom status bar, plus a **KWC logs** sidebar page.

- `KWC` uses the normal status-bar text color.
- `(running)` uses the Hermes success-green token while at least one board has a current dispatcher heartbeat.
- `(stopped)` uses the Hermes danger-red token when the API is unavailable or every board heartbeat is stale.

The indicator checks the namespaced `/api/plugins/kanban-workflow-controller/status` endpoint every 10 seconds. `running` requires a live dispatcher tick for at least one board within the last 180 seconds; the response also lists every active board. This reports dispatcher-side controller health, not whether a PM workflow session is currently bound.

## Retry behavior

Each board cursor advances through ignored events immediately. For an actionable event, that board's cursor advances only after `ctx.inject_message(...)` accepts the wake request. A rejected injection leaves that event pending for the board's next dispatcher tick. SQLite serializes the binding check, wake acceptance, and cursor update, so concurrent dispatcher ticks cannot deliver the same event or route from a stale binding. Rebinding the coordinator preserves all committed board cursors, and refreshing the same durable session preserves pending events.

The stored target is the durable gateway/TUI `HERMES_SESSION_KEY`, not the ephemeral internal agent session id. Binding also requires `HERMES_SESSION_PROFILE` to equal the configured coordinator Profile. Context compression therefore does not require descendant-session rebinding. A descendant can refresh only the already-bound durable key; it cannot replace the route with another session key.

Delivery is **at least once** across a host crash: if the process terminates after the host accepts the queued wake but before SQLite commits the cursor, the same minimal wake envelope can be delivered again. The envelope carries a stable `source_event_id`, and the coordinator must always read canonical Kanban state before choosing a transition, so duplicate wakes are safe and contain no copied task/comment content.

Comment delivery has the same at-least-once boundary around `ctx.inject_message()`. Request-level idempotency prevents duplicate native comments, while a stable `source_comment_id` lets the receiving Profile treat a repeated wake as the same request.

## Verification

From the repository root:

```bash
python plugins/kanban-workflow-controller/test_kanban_workflow_controller.py -v
python -m py_compile plugins/kanban-workflow-controller/__init__.py plugins/kanban-workflow-controller/comment_service.py plugins/kanban-workflow-controller/dashboard/plugin_api.py plugins/kanban-workflow-controller/test_kanban_workflow_controller.py
hermes plugins doctor plugins/kanban-workflow-controller --ci
```
