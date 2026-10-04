# Changelog

All notable Kanban Workflow Controller changes are recorded here. Versions follow Semantic Versioning.

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
