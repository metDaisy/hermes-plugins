# Kanban Workflow Controller

Kanban Workflow Controller routes native Kanban lifecycle events to the Project Manager and adds a KWC-owned comment conversation path between Profiles. It keeps native Kanban as the source of truth: every KWC comment is persisted by `kanban_db.add_comment()` and therefore appears in the ordinary task-card thread and `commented` event history.

## Versioning

`plugin.yaml` is the canonical version source. This plugin is a new product line that started at `0.1.0`; it does not continue the retired `kanban-control` `/kcp` command contract.

- patch: compatible bug fixes and routing corrections;
- minor: backward-compatible event, tool, endpoint, or session-routing capabilities;
- major: incompatible state, wake-envelope, configuration, or installation contracts.

Every functional change must update `plugin.yaml` and add the matching newest release to `CHANGELOG.md`. Documentation-only corrections may keep the current version.

## Runtime model

### Desktop-owned runtime lifecycle

The Desktop status contribution renews a machine-wide singleton KWC Runtime lease through `POST /runtime/lease` every 10 seconds. The namespaced API starts `runtime_service.py` with the same Python interpreter as `desktop-serve`, records the runtime generation and process ownership in KWC's SQLite database, and reuses the live process across Profile backends.

```text
Hermes Desktop
└─ desktop-serve
   └─ KWC Runtime child
      ├─ heartbeat + parent/stop monitoring
      └─ canonical snapshot → pure WorkflowEngine → compact dry-run decision
```

Starting with `0.8.0`, the Runtime performs read-only reconciliation for every board previously observed through KWC's durable `board_heartbeat` rows. It reads tasks, direct graph links, runs, and events through canonical Hermes Kanban functions, normalizes only the fields needed by the workflow contract, and asks the pure WorkflowEngine for exactly one next action or a fail-closed error. The latest compact decision is persisted per board; task bodies, comments, prompts, logs, session keys, and reasoning are not copied into KWC state.

The Runtime still performs **no Kanban mutation** and does not run the native dispatcher. Native dispatch remains on the existing Hermes Core/Gateway path, and this release neither changes `kanban.dispatch_in_gateway` nor modifies Hermes Core. `GET /runtime/decisions` exposes the latest dry-run decisions, while `/status` reports Runtime health, privacy-safe reconciliation attempt/success/error state, and native dispatch-tick health separately as `dispatcher_running`.

Starting with `0.9.0`, KWC also has the first execution-plane contract: a durable pending invocation can accept exactly one structured Project Manager result through `kwc_pm_response`. The result is bound to the invocation id, configured coordinator Profile, durable Hermes session key, snapshot fingerprint, and an invocation-specific outcome allowlist. An identical retry is idempotent; a different replay is rejected. The response tool only records validated intent and still performs no Kanban mutation.

This slice deliberately does not create or resume a PM worker yet. The event dispatcher, serial agent lease, PM session runner, action executor, and canonical mutation/read-back adapter remain follow-up work; therefore `kanban.dispatch_in_gateway` stays unchanged.

The Runtime exits when its owning `desktop-serve` PID disappears, when `POST /runtime/stop` marks its generation for shutdown, or when another generation safely takes ownership.

Lifecycle routing remains passive:

```text
PM invokes /run-workflow
→ KWC binds the durable Project Manager session
→ native Hermes dispatch executes ready cards for their assignee
→ Coder request-review enters the native same-card PM checkpoint lane
→ Reviewer completes the aggregate Review card directly
→ KWC routes only schema-tag-matched blocked/completed workflow events that require PM coordination
```

Delivery is hybrid rather than a universal push callback. Native completed/blocked/worker lifecycle hooks provide an immediate reconciliation hint in the hook caller, while `on_kanban_dispatch_tick` scans the durable event cursor as recovery. Ordinary comments created outside KWC have no Core comment hook; the supported blocked-Decision comment is discovered by that periodic scan. The gateway dispatch interval is configurable through `kanban.dispatch_interval_seconds` and defaults to 60 seconds.

The workflow boundary is explicit:

- `backend-implementation-card-v2` and `backend-implementation-card-v3` are executed by the canonical `coder` Profile. A successful Coder run calls native `request-review`, and Hermes dispatch starts the Project Manager's same-card checkpoint run. `review_requested` is not routed by KWC.
- `aggregate-review-card-v3` is executed by the canonical `reviewer` Profile. Reviewer `completed` wakes the Project Manager so it can validate the canonical aggregate result and route findings or Summary progression.
- A blocked implementation or aggregate Review run wakes the Project Manager for canonical task/run/comment/event read-back. Workers use `blocked + needs_input`, not `request-review`, for problem escalation.
- A valid human comment on a blocked PM-owned `policy-decision-card-v1` also wakes the Project Manager.

Project Manager, Coder, and Reviewer keep independent Profile sessions and memory. KWC shares only native Kanban task, run, event, comment, and lifecycle locators; it does not merge Profile context or execute worker cards itself.

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

KWC-owned comment creation is asynchronous at the delivery boundary. The HTTP endpoint schedules a FastAPI background dispatcher nudge, and `kwc_comment` starts a process-local delivery attempt after the durable request is recorded. Both are latency optimizations, not the correctness boundary: regular dispatcher ticks retry any delivery that was missed, interrupted, rejected, or waiting for a target Profile session.

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

## Project Manager invocation response

KWC supplies an opaque invocation id and an invocation-specific outcome contract. The Project Manager completes that invocation with the tool rather than emitting JSON in prose:

```text
kwc_pm_response(
  invocation_id="inv_pm_01H...",
  outcome="checkpoint-approved",
  task_id="t_impl1",
  reason_code="VERIFICATION_PASSED"
)
```

`reason_code` is optional but, when present, must be a compact uppercase identifier such as `MISSING_INPUT` or `POLICY:USER_DECISION_REQUIRED`; it is not a free-form reasoning field. The tool is available only in the configured coordinator Profile's session and KWC never returns the bound session key in the tool result. Unknown invocations, wrong Profiles, wrong sessions, non-allowlisted outcomes, and conflicting replays fail closed.

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

Lifecycle filtering is fail-closed to the workflow's canonical `coder` and `reviewer` Profile names. These worker identities are not aliases for the coordinator and are intentionally not configurable independently of the workflow contracts.

Desktop Kanban writes a human comment with author `dashboard` by default. If the actual user-facing comment surface writes another stable author, configure the same literal value on the dispatcher host:

```bash
hermes -p main config set plugins.entries.kanban-workflow-controller.settings.decision_comment_author dashboard
```

Message injection is capability-gated by Hermes and must be explicitly allowed on the Profile whose plugin instance handles dispatch ticks:

```bash
hermes -p main config set plugins.entries.kanban-workflow-controller.allow_gateway_injection true
```

The global coordinator binding, per-Profile comment-session bindings, per-board event cursors/heartbeats, Runtime owner generation/heartbeat, privacy-safe reconciliation health, latest compact WorkflowEngine decisions, Profile invocation/response ledger, comment delivery ledger, and activity log are stored machine-wide under:

```text
$HERMES_ROOT/plugin-data/kanban-workflow-controller/state.db
```

This file is only a routing/delivery index, Runtime lifecycle store, privacy-safe decision cache, invocation-response ledger, and activity log. Workflow decisions contain only board/time, fixed phase/action/reason/error codes, task ID, Profile, and Skill. Invocation rows additionally retain the durable session key required for ownership validation, but it is never returned by the response tool. No invocation row stores a task body, comment body, prompt, free-form reasoning, raw command/result, log, or credential; Kanban remains the workflow and lifecycle authority.

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
- `(running)` uses the Hermes success-green token while the Desktop-owned KWC Runtime child is alive, its parent `desktop-serve` process is alive, and its heartbeat is fresh.
- `(stopped)` uses the Hermes danger-red token when the API is unavailable or Runtime ownership is absent, stale, stopping, or dead.

The indicator first renews `POST /api/plugins/kanban-workflow-controller/runtime/lease`, then checks the namespaced `/status` endpoint every 10 seconds. The response exposes native dispatcher health independently as `dispatcher_running`, `boards`, and `last_dispatch_tick_at`; a live Runtime does not imply that Gateway-hosted native dispatch is currently active. `GET /api/plugins/kanban-workflow-controller/runtime/decisions` provides the latest compact dry-run decision for each monitored board.

## Retry behavior

Each board cursor advances through ignored events immediately. For an actionable event, that board's cursor advances only after `ctx.inject_message(...)` accepts the wake request. A rejected injection leaves that event pending for the board's next dispatcher tick. SQLite serializes the binding check, wake acceptance, and cursor update, so concurrent dispatcher ticks cannot deliver the same event or route from a stale binding. Rebinding the coordinator preserves all committed board cursors, and refreshing the same durable session preserves pending events.

The stored target is the durable gateway/TUI `HERMES_SESSION_KEY`, not the ephemeral internal agent session id. Binding also requires `HERMES_SESSION_PROFILE` to equal the configured coordinator Profile. Context compression therefore does not require descendant-session rebinding. A descendant can refresh only the already-bound durable key; it cannot replace the route with another session key.

Delivery is **at least once** across a host crash: if the process terminates after the host accepts the queued wake but before SQLite commits the cursor, the same minimal wake envelope can be delivered again. The envelope carries a stable `source_event_id`, and the coordinator must always read canonical Kanban state before choosing a transition, so duplicate wakes are safe and contain no copied task/comment content.

Comment delivery has the same at-least-once boundary around `ctx.inject_message()`. Request-level idempotency prevents duplicate native comments, while a stable `source_comment_id` lets the receiving Profile treat a repeated wake as the same request.

## Verification

From the repository root:

```bash
python plugins/kanban-workflow-controller/test_kanban_workflow_controller.py -v
python plugins/kanban-workflow-controller/test_workflow_engine.py -v
python plugins/kanban-workflow-controller/test_snapshot_service.py -v
python plugins/kanban-workflow-controller/test_runtime_service.py -v
python plugins/kanban-workflow-controller/test_plugin_runtime_api.py -v
python plugins/kanban-workflow-controller/test_invocation_service.py -v
python -m py_compile plugins/kanban-workflow-controller/__init__.py plugins/kanban-workflow-controller/comment_service.py plugins/kanban-workflow-controller/invocation_service.py plugins/kanban-workflow-controller/workflow_engine.py plugins/kanban-workflow-controller/snapshot_service.py plugins/kanban-workflow-controller/runtime_service.py plugins/kanban-workflow-controller/dashboard/plugin_api.py plugins/kanban-workflow-controller/test_kanban_workflow_controller.py plugins/kanban-workflow-controller/test_invocation_service.py plugins/kanban-workflow-controller/test_workflow_engine.py plugins/kanban-workflow-controller/test_snapshot_service.py plugins/kanban-workflow-controller/test_runtime_service.py plugins/kanban-workflow-controller/test_plugin_runtime_api.py
node --check plugins/kanban-workflow-controller/desktop/plugin.js
hermes plugins doctor plugins/kanban-workflow-controller --ci
```
