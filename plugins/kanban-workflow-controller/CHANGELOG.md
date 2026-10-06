# Changelog

All notable Kanban Workflow Controller changes are recorded here. Versions follow Semantic Versioning.

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
