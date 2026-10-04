# Kanban Workflow Controller

Kanban Workflow Controller is a passive event router for one existing Project Manager workflow session. It replaces PM-side `sleep N` polling with Kanban event delivery. It does not create boards or tasks, interpret comments, choose workflow transitions, or mutate Kanban state.

## Versioning

`plugin.yaml` is the canonical version source. This plugin is a new product line starting at `0.1.0`; it does not continue the retired `kanban-control` `/kcp` command contract.

- patch: compatible bug fixes and routing corrections;
- minor: backward-compatible event or session-routing capabilities;
- major: incompatible state, wake-envelope, configuration, or installation contracts.

Every functional change must update `plugin.yaml` and add the matching newest release to `CHANGELOG.md`. Documentation-only corrections may keep the current version.

## Runtime model

```text
PM session A in the configured coordinator Profile invokes /run-workflow
→ pre_llm_call reads HERMES_SESSION_KEY and binds board → durable gateway/TUI session key
→ gateway Kanban dispatcher emits a post-lock dispatch tick
→ controller reads new durable task_events
→ actionable event is injected into session A
→ PM loads run-workflow and reads the task, runs, comments, events, and board
```

The plugin routes these events:

- `blocked` when the event's persisted task run was owned by a Profile other than the configured coordinator;
- `completed` when the event's persisted task run was owned by a Profile other than the configured coordinator;
- `commented` only when the task is currently `blocked`, assigned to the configured coordinator Profile, has body schema `policy-decision-card-v1`, and the author exactly matches `decision_comment_author`.

It intentionally ignores `review_requested`: native Kanban review dispatch owns the same-card PM checkpoint. It also ignores claims, heartbeats, and ordinary comments.

## Passive boundary

The controller does not:

- register `/kcp` or any other slash command;
- create or assign cards;
- complete, unblock, promote, or archive cards;
- analyze comment text;
- copy comment bodies, prompts, reasoning, or worker output into the wake message;
- replace the `run-workflow` Skill or its validators.

A wake message contains only:

```json
{
  "schema": "kanban-workflow-wakeup-v1",
  "board": "issue151",
  "source_task_id": "t_...",
  "source_event_id": 42,
  "event_kind": "blocked",
  "instruction": "Load run-workflow, then read back the source task, runs, comments, events, and current board state before choosing the next transition."
}
```

## Configuration

Enable the plugin in both places that participate in routing:

1. the `project-manager` Profile, which observes `/run-workflow` and binds the coordinator session;
2. the gateway/dispatcher host Profile, which observes dispatch ticks and injects wake messages.

Configure the same board in both Profiles:

```bash
hermes -p project-manager config set plugins.entries.kanban-workflow-controller.settings.board issue151
hermes -p main config set plugins.entries.kanban-workflow-controller.settings.board issue151
```

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

The session binding and event cursor are stored machine-wide under:

```text
$HERMES_ROOT/plugin-data/kanban-workflow-controller/state.db
```

This file is only a routing index. Kanban remains the workflow and lifecycle authority.

## Retry behavior

The cursor advances through ignored events immediately. For an actionable event, it advances only after `ctx.inject_message(...)` accepts the wake request. A rejected injection leaves that event pending for the next dispatcher tick. SQLite serializes the binding check, wake acceptance, and cursor update, so concurrent dispatcher ticks cannot deliver the same event or route from a stale binding. Rebinding on the same board never moves the committed cursor backward, and refreshing the same durable session preserves pending events.

The stored target is the durable gateway/TUI `HERMES_SESSION_KEY`, not the ephemeral internal agent session id. Binding also requires `HERMES_SESSION_PROFILE` to equal the configured coordinator Profile. Context compression therefore does not require descendant-session rebinding. A descendant can refresh only the already-bound durable key; it cannot replace the route with another session key.

Delivery is **at least once** across a host crash: if the process terminates after the host accepts the queued wake but before SQLite commits the cursor, the same minimal wake envelope can be delivered again. The envelope carries a stable `source_event_id`, and the coordinator must always read canonical Kanban state before choosing a transition, so duplicate wakes are safe and contain no copied task/comment content.

## Verification

From the repository root:

```bash
python plugins/kanban-workflow-controller/test_kanban_workflow_controller.py -v
python -m py_compile plugins/kanban-workflow-controller/__init__.py plugins/kanban-workflow-controller/test_kanban_workflow_controller.py
hermes plugins doctor plugins/kanban-workflow-controller --ci
```
