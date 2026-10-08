# Changelog

All notable Kanban Workflow Controller changes are recorded here. Versions follow Semantic Versioning.

## [0.9.0] - 2026-10-08

### Added

- Added a durable `kwc-profile-invocation-v1` ledger for one pending Profile invocation and one accepted structured response.
- Added `kwc_pm_response`, bound to the invocation's configured coordinator Profile and durable Hermes session key.
- Added allowlisted outcomes, compact reason-code validation, exact idempotent replay, conflicting-replay rejection, and WAL-backed cross-process access.

### Changed

- PM results are now submitted as typed tool arguments instead of requiring natural-language JSON parsing. The tool records intent only and performs no Kanban mutation.
- This release does not yet create PM workers, resume PM sessions, execute WorkflowEngine actions, or replace the native Core/Gateway dispatcher.

## [0.8.0] - 2026-10-08

### Added

- Added a pure, non-mutating WorkflowEngine that validates PM/Coder/Reviewer task contracts and computes one deterministic next action or a fail-closed error.
- Added canonical read-only Kanban snapshot normalization for task schemas, graph links, forced Skills, run provenance, review results, and claimed source status.
- Added Runtime reconciliation of every monitored board and persisted only compact privacy-safe decisions in the plugin SQLite database.
- Added `GET /runtime/decisions` for latest per-board dry-run decision read-back without task bodies, comments, logs, prompts, or session content.
- Added privacy-safe reconciliation health (`attempted_at`, `succeeded_at`, and exception-type error code) to `/status`, so Runtime import/database failures do not remain invisible.

### Changed

- The Desktop-owned Runtime now evaluates workflow state on each owned tick, but still performs no Kanban mutation and does not replace the native Core/Gateway dispatcher.
- Latest aggregate Review selection is based on canonical run ID instead of task list order.

## [0.7.0] - 2026-10-08

### Added

- Added a machine-wide singleton KWC Runtime child launched through the Desktop plugin API and owned by the active `desktop-serve` process.
- Added durable runtime generation, PID, parent PID, heartbeat, stop-request, and exit-reason state in the plugin SQLite database.
- Added `POST /runtime/lease` and `POST /runtime/stop` control endpoints, with the Desktop status contribution renewing the runtime lease every 10 seconds.
- Added parent-process death detection, stale/orphan takeover, generation-safe shutdown, and a non-destructive Windows process-liveness probe.

### Changed

- Split `/status` into Desktop-owned runtime health (`running`) and native dispatcher health (`dispatcher_running`).
- Kept native Kanban dispatch on its existing Gateway/Core path; this release does not switch `dispatch_in_gateway` or modify Hermes Core.

## [0.6.0] - 2026-10-07

### Changed

- Aligned lifecycle routing with the PM workflow contract: native Hermes dispatch owns ready-card execution and implementation `review_requested` handoff.
- Restricted implementation-card wakeups to blocked Coder runs on `backend-implementation-card-v2` or `backend-implementation-card-v3`.
- Restricted aggregate Review wakeups to blocked or completed Reviewer runs on `aggregate-review-card-v3`.
- Kept lifecycle actors fail-closed to the canonical `coder` and `reviewer` Profile identities and prevented coordinator self-wake through overlapping role configuration.
- Stopped treating arbitrary non-PM worker completion as a coordinator event.

## [0.5.0] - 2026-10-06

### Added

- Added `POST /comments`, which persists through native `kanban_db.add_comment()` and schedules an immediate dispatcher nudge.
- Added `kwc_comment` and `kwc_reply` Agent tools for routed comments and loop-safe replies.
- Added `@{profile}` routing with explicit mentions overriding the current task assignee.
- Added per-Profile durable session bindings, stable request idempotency keys, and per-target delivery tracking.

### Changed

- Removed the proposed `kanban_task_commented` Core-hook dependency; dispatch ticks remain the recovery path for queued KWC comments.
- Agent replies record `reply_to_comment_id` and suppress automatic re-routing to prevent reply loops.

## [0.4.0] - 2026-10-04

### Added

- Persisted privacy-safe controller activity for board monitoring, session binding, event routing, filtering, retry, and missing-binding diagnostics.
- Added a bounded `/activity` API and a Desktop `KWC logs` page.
- Retained activity for 30 days without storing comment bodies, prompts, session keys, reasoning, raw tool output, or credentials.
- Replaced the single configured-board restriction with one coordinator binding and independent cursor/heartbeat tracking for every observed Kanban board.

## [0.3.0] - 2026-10-04

### Added

- Subscribed to native completion, block, comment, worker-exit, and stale-claim hooks for immediate event processing.
- Kept dispatch ticks as the durable reconciliation path for missed or rejected wake delivery.
- Declared the new Core `kanban_task_commented` observer contract without exposing comment bodies.

## [0.2.0] - 2026-10-04

### Added

- Added a Desktop status-bar indicator on the bottom-right.
- Added a runtime health API backed by recent configured-board dispatcher heartbeats.
- Displayed a neutral `KWC` label with only `(running)` in green or `(stopped)` in red according to configured-board dispatcher heartbeat health.

## [0.1.0] - 2026-10-04

### Added

- Added passive registration of the existing `run-workflow` coordinator session.
- Added post-dispatch Kanban event routing to that exact session.
- Added minimal wake envelopes without copying card content or comments.
- Bound routing to the durable gateway/TUI session key rather than the rotating internal agent session id.
- Added SQLite-serialized binding, injection, and cursor updates for cross-Profile process safety.
- Restricted human comment wakeups to blocked `policy-decision-card-v1` cards and the configured comment author.
- Restricted workflow binding to exact invocation shapes and prevented delegated child sessions from replacing the durable route.
- Routed worker lifecycle events by their persisted run Profile rather than mutable current assignment.
- Required the active session Profile to match the configured coordinator and preserved pending/committed cursors across refreshes and rebinding.
- Documented the intentional at-least-once delivery edge across a host crash after queue acceptance.
