# Changelog

All notable Hermes LangChain changes are recorded here. Versions follow Semantic Versioning.

## [0.32.0] - 2026-10-22

### Added

- Added `hermes_langchain_begin_authoring` as the closed host-session entry point for one fresh, controller-owned Triage assignment and dedicated Project Manager execution role.

### Fixed

- Prevented manual role-switched sessions from starting authoring and documented that `hermes_langchain_route`, historical `.temp` artifacts, and stale assignment ids are not workflow entry or authorization mechanisms.

## [0.31.0] - 2026-10-21

### Added

- Added accepted-Triage-bound deterministic graph projection through `hermes_langchain_project_graph` and immutable `controller-task-graph-v1` artifacts.
- Added canonical native Impl/Review/Summary graph materialization through `hermes_langchain_materialize_graph`, including bounded topology validation, source provenance, snapshot fencing, artifact-bound audit events and exact replay.
- Added disposable native-board coverage for graph creation, exact task/link/event read-back, replay and hard-delete rollback.

### Fixed

- Made post-commit and malformed-receipt failures rollback-aware, rejected duplicate or archived native identities, verified every incident link and audit event, and bounded card/link/skill/body cardinality before mutation.

## [0.30.0] - 2026-10-21

### Added

- Added the canonical `hermes_langchain_activate` tool and `controller-task-activation-v1` artifact contract for one already-existing native task.
- Added controller-owned snapshot fencing, artifact/producer/issue/generation/workspace/task binding, exact native read-back, stable public errors and exact replay without a second promotion.
- Added disposable native-board activation coverage through the real Hermes Kanban database adapter.

### Changed

- Registered `canonical-task-activation-v1` as a profile-local controller Artifact Store namespace without importing the deprecated KWC mutation service.

## [0.29.0] - 2026-10-19

### Added

- Added machine-readable PM responsibility and 38-file distribution-owned source maps, with regressions that require every former PM artifact to resolve to a KWC-owned target.
- Migrated the PM Triage, task-graph, activation, current-state, fixture, and isolated native-E2E regression assets under the KWC Plugin.

### Changed

- Service-planning fresh agents now receive `web` and `browser`; implementation checkpoint and Summary finalization receive `gradle-mcp` in addition to repository and tracker MCP servers.
- Native activation uses the current fresh-agent environment instead of adding an explicit host Profile CLI argument.
- The native lifecycle E2E now uses a temporary `HERMES_HOME`, so live Gateway dispatch and Profile state cannot claim or mutate its synthetic cards.

### Fixed

- Added bounded Windows board-cleanup retry and current-schema Triage/Review fixtures to the migrated native E2E.
- Fresh-agent Triage and task-graph artifacts now use a deterministic board namespace beneath each generation, preventing boards with the same Issue number from reading or overwriting one another's stage drafts, bodies, source bundles, or native read-back reports.

## [0.28.0] - 2026-10-18

### Added

- Added `kwc-fresh-agent-execution-v1` and generation-scoped execution manifests that record the exact prompt, KWC-owned Skill, MCP servers, toolsets, workspace, Kanban role, and output contract for each isolated run.
- Added the common `kwc-orchestrator-contract` plus KWC-owned copies of the seven PM situation Skills and their operational helpers under the Plugin `skills/kwc-*` tree.

### Changed

- PM-owned Authoring and workflow coordination now launch a new host-Profile session with `--ignore-rules` and explicit KWC inputs instead of loading or resuming the Project Manager Profile's SOUL, memory, or installed Skills.
- The infrastructure host Profile and the Kanban execution role are carried separately. The Project Manager Profile remains installed and available as a comparison baseline.
- Cross-Skill helper paths and current-state inspection resolve inside the Plugin rather than through `.hermes/profiles/project-manager`.

## [0.27.0] - 2026-10-18

### Added

- Added the standalone `hermes-langchain` Plugin identity with isolated dashboard/Desktop routes, runtime storage, module names, and `HERMES_LANGCHAIN_STATE_PATH` override.
- Added a side-effect-free LangGraph `StateGraph` that resolves each durable Authoring assignment to its exact `kwc_*` Tool and exposes `hermes_langchain_route` for diagnostics.
- `POST /authoring` now launches a dedicated named Project Manager one-shot that consumes successive Triage assignments without waiting for manual inter-stage messages and stops when Triage materialization returns the Impl assignment.

### Changed

- KWC production code, assets, prompts, fixtures, and compatibility tests are preserved under `plugins/hermes-langchain`; native Kanban remains canonical and the original Plugin source is not modified by this migration.
- Durable Authoring assignments now include LangGraph-derived `next_action` and fail closed when it disagrees with the assignment's allowed Tool.

- Authoring mutations are authorized by the `project-manager` Profile plus the current opaque assignment id instead of the initiating session id, so a compact dedicated session can resume durable work without inheriting a long-lived PM context.
- Authoring tools read each assignment once before validation and transition instead of querying once for session ownership and again for the assignment body.
- Dedicated Triage launches enable only the file, terminal, repository-discovery, GitHub, and KWC toolsets needed by the staged workflow, and steer each stage to its accepted projection and exact locators.
- Intake now states the exact identity and semantic artifact fields, requires direct repo-relative workspace output, and names the current-state/Git inputs, preventing schema-guess retries, Profile-cache copies, and broad historical-artifact scans.
- Discovery now reuses exact intake locators, avoids duplicate Codebase Memory/Semble searches, caps each target's candidate set, and states the validator's flat scalar `tool_usage` shape to prevent schema-retry loops.
- Source confirmation now binds evidence to the accepted discovery candidate pool, reuses shared producer/consumer/test seams across targets, and reports the exact target/locator for undiscovered evidence; analysis now exposes its complete closed authoring shape so an isolated PM session does not reconstruct it through validator retries.
- Discovery treats exact intake locators as accepted candidates and limits candidate expansion to one Codebase Memory call per target plus two bounded Semble fallbacks, reducing repeated search/trace work.
- Discovery now reserves candidate capacity for cross-file callers, tests, and producer public sources, permits only one global Codebase Memory completion query, and spells out the exact resolution object to prevent same-file candidate crowding and validator retries.
- Source-confirmation questions are exact non-empty factual strings, and user-owned open decisions require requirement, Issue, or architecture authority evidence so internal refactoring choices remain implementation-slice details instead of blocking Triage freeze.
- Intake keeps independently verifiable requirements and user observables as separate trace IDs instead of collapsing presentation, domain, cross-module contract, and regression semantics into broad compound entries.
- Discovery no longer paginates already-bounded graph searches, uses one combined local test lookup before a single semantic fallback, and deterministically caps five candidates per target and two symbols per path.
- Normative layer, package, and ownership questions now carry their governing document through Discovery and cannot be closed as compliant from symbol existence alone.
- Source-confirmation and analysis spell out exact approval evidence and first-pass closure checks to avoid validator-driven retries for extra fields, unconfirmed locators, unresolved alternatives, and partial rows without test evidence.
- Discovery carries shared architecture and glossary authority once for relevant layer, public-contract, terminology, and error-ownership changes; analysis must record every baseline or source-confirmed document exactly once with same-document evidence, preventing fast runs from silently dropping the document-impact matrix.
- Discovery deterministically retains the first two symbols from a repeated source path before applying the five-candidate limit, because source authorization is file-scoped; redundant same-file symbols no longer cause an LLM retry.
- Analysis canonicalizes safe projection noise before semantic validation: source-confirmation `kind` metadata is removed, known `intent.*` section aliases are normalized, and duplicate verification rows for one observable are merged with their references remapped.
- Discovery rejects a producer/cross-module target that contains only consumer-module production candidates, preventing a missing producer public source from becoming a false user-owned contract decision during analysis.
- Discovery canonicalizes structured fallback audit entries and raw Codebase Memory project projections into the validated contract, removing deterministic retries without dropping fallback provenance.
- Analysis normalizes single-value intent amendment projections into the section's canonical changed field, while semantic alternative resolution remains owned by the Project Manager.
- Discovery requires the repository glossary when an investigation explicitly concerns error ownership/contracts or duplicated code/message semantics, preserving the governing document impact in faster runs.

### Fixed

- Every semantic stage now receives and must submit one exact generation-scoped `.temp/kwc-authoring/<issue>/generation-<generation>/...-authoring.json` path. This prevents a stage draft from dirtying the repository root, failing current-state admission, or overwriting another generation's canonical artifacts.

## [0.26.0] - 2026-10-17

### Added

- Added the isolated `backend-implementation-card-v6` template and deterministic validator with only trace, objective, boundary, atomic obligations, constraints, and verification sections.
- V6 obligations bind one production operation and invocation count to explicit observables, and persisted unchanged-state proof requires exact identity and value projections.
- Added a digest-checked frozen `triage-v7` adapter that derives V6 traceability, objective, boundaries, operation candidates, grounded constraints, and runnable selector candidates without changing the Triage schema.

### Changed

- V6 verification maps runnable selectors directly to atomic obligation and constraint IDs instead of copying broad acceptance and free-form scenario prose. This release does not yet route or materialize V6 cards.
- The V6 skeleton intentionally leaves invocation count, observable projections/comparisons, and test level unresolved for PM authoring, and omits RED/GREEN, generic verification, rollback, and handoff instructions owned by worker workflow.

## [0.25.1] - 2026-10-16

### Fixed

- Graph wrappers now represent one preserved behavior across multiple implementation slices without mutating immutable card bodies.
- Execution priorities now preserve implementation ordering while aggregate Review cards keep native priority `0`.
- Per-card requirement coverage accepts the card's exact requirement subset when a shared observable spans additional slices.

## [0.25.0] - 2026-10-16

### Changed

- Impl, Review, and Summary graph/card validators and native materialization helpers are now owned by KWC instead of an installed Project Manager `build-task-graph` Skill.
- KWC authoring assignments no longer force a graph-building Skill; PM supplies only the current assignment's semantic artifact while KWC validates topology, applies native mutations, rolls back failures, and performs exact read-back.
- `validate-review-graph` now rejects a `review-fast-path-v1` source bundle with `REVIEW_GRAPH_WRAPPER_REQUIRED` instead of the misleading rework-lineage error reserved for full graph wrappers.

## [0.24.3] - 2026-10-16

### Fixed

- Analysis now rejects immutable implementation-slice descriptions that cannot satisfy the Korean Impl-card contract before a Triage card is materialized.
- Every confirmed gap must carry at least one repository-backed constraint, preventing later Impl fast-path generation from producing an empty `context.constraints` section.

## [0.24.2] - 2026-10-16

### Fixed

- Every cross-module `contract_changes` entry must be explicitly mapped by `contract_change_ids` to the implementation slices that produce or consume it, preventing verification-sharing from leaking a contract into unrelated cards.
- Frozen slice dependencies are preserved as deterministic implementation dependency edges and native parent links instead of flattening every Impl card directly under Triage.

## [0.24.1] - 2026-10-16

### Fixed

- PM authoring tool visibility no longer depends on session context that is unavailable while a CLI toolset is assembled.
- Every authoring handler verifies assignment-session ownership before validation or native card side effects, so another Profile can see the schemas but cannot cross the session fence.
- Authoring ownership accepts the actual CLI session ID when a child CLI inherited its parent Desktop session key, while still passing only the matched owner ID into the mutation fence.

## [0.24.0] - 2026-10-16

### Fixed

- Source confirmation now classifies cross-module public contracts as `published`, `approved-unimplemented`, or `undecided` instead of conflating an absent production seam with an unresolved product decision.
- An `approved-unimplemented` seam must materialize a source-backed `contract_changes` producer public bridge before analysis can advance; missing bridge coverage fails deterministically.
- Issue exclusions for producer internal implementation or repositories are no longer broadened into an exclusion of an approved application public API bridge.

## [0.23.0] - 2026-10-16

### Fixed

- Triage decision options must now truthfully preserve every observable listed in `preserves_observable_ids`; changing a public error code, HTTP status, message, or API meaning is routed to upstream requirement rework instead of being mislabeled as preservation.
- The Cart/Offer example explicitly rejects treating a Cart-owned replacement for `OFFER-001/404` as an observable-preserving option.

## [0.22.0] - 2026-10-16

### Fixed

- Triage source confirmation now requires direct producer-source evidence before treating a cross-module error contract, NamedInterface, port, or capability as implemented.
- Analysis no longer treats requirement-document code/message definitions as proof of a public implementation seam; absent or unknown seams must become an explicitly allowed producer bridge or a closed-handoff decision/blocker.

## [0.21.0] - 2026-10-16

### Fixed

- Triage titles and idempotency keys now use the fenced authoring generation instead of hardcoding Generation 1.
- Summary generation no longer receives a mismatched Generation 1 Triage after a later-generation retry.

### Added

- An explicitly requested failed incomplete authoring attempt can be preserved in history and replaced by exactly the next generation; replacement remains fail-closed by default.

## [0.20.0] - 2026-10-16

### Added

- A completed board authoring contract can start exactly the next generation from a fresh Project Manager session.
- The replaced completed assignment is retained in `authoring_workflow_history` before the new generation is fenced.

### Changed

- Incomplete workflows, skipped generations, different Issues, and different workspaces still fail closed instead of silently replacing authoring state.
- Planning-only Triage may use the exact committed baseline when every dirty path is KWC/Profile metadata under `.hermes`, no implementation-covered path changed, and `WORKTREE_DIRTY` is the only inspection error; product or mixed dirty paths still fail closed.

## [0.19.0] - 2026-10-16

### Fixed

- Triage discovery now resolves Codebase Memory projects from `root_path`, `root_exists`, Git HEAD, and the fenced assignment workspace instead of trusting a fixed project name.
- Stale, orphaned, mismatched, and ambiguous project metadata now produces a typed unavailable result so discovery can fail closed and use bounded fallback search.

### Changed

- Discovery artifacts use `kwc-triage-discovery-v2`, which carries the normalized Codebase Memory project snapshot and deterministic resolution result.
- Project discovery now enumerates all `list_projects` pages, then calls `index_status(verbose=true)` only for workspace-path candidates before KWC validates canonical root and HEAD.

## [0.18.0] - 2026-10-16

### Added

- Added the resumable `intake → discovery → source_confirmation → analysis → materialize` Triage sub-state machine.
- Added `kwc_advance_triage`, stage-specific semantic prompts, artifact schemas, ordering fences, verified durable artifact digests, and source-confirmed analysis evidence.

### Changed

- Project Manager no longer loads `create-triage`; KWC returns only the current stage instruction and validates each stage before exposing the next one.
- `kwc_create_triage` now accepts only the assignment id and materializes the KWC-assembled semantic artifact with a KWC-generated current-state inspection.

## [0.17.0] - 2026-10-15

### Added

- Added the KWC-owned Triage authoring activity and native Kanban port.
- Added `kwc-triage-authoring-v1`, allowing PM to submit semantic identity, intake, and analysis in one artifact.

### Changed

- `kwc_create_triage` now performs compose, repository/current-state validation, freeze, digest, native creation, rollback, and exact read-back inside KWC.
- Removed the runtime dependency on the PM `create-triage/scripts/triage.py` helper.

## [0.16.0] - 2026-10-15

### Added

- Added project-independent `Activity`, `ActivityExecutor`, port protocols, and a fail-fast object container as the first KWC composition contracts.
- Added the KWC-owned `triage-v7` domain module with immutable identity, composer, validator, and freezer objects.

### Changed

- The installed `create-triage` helper is now a compatibility facade over the KWC domain module while preserving the existing function, CLI, schema, error-code, digest, and JSON contracts.

## [0.15.0] - 2026-10-15

### Added

- Added the durable `kwc-assignment-v1` Triage → Impl → Review → Summary authoring state machine, fenced to the Project Manager session and exact source-card identities.
- Added phase-specific `kwc_create_triage`, `kwc_create_impl_graph`, `kwc_create_review`, and `kwc_create_summary` tools that call the installed PM validators/native helpers and advance only after exact read-back.

### Changed

- Removed legacy implementation/review routing schemas and aligned Runtime decisions with `backend-implementation-card-v5`, `aggregate-review-card-v5`, and `issue-summary-card-v3`.

## [0.14.0] - 2026-10-08

### Added

- Added a Runtime action executor that claims durable outbox rows and executes `promote-task`, `dispatch-task`, and `wake-profile` through narrow adapters.
- Added fenced native dispatch claims for `ready` and `review` tasks, task-scoped Hermes session creation for Coder/Reviewer workers, and resumable coordinator wake guards.
- Added durable Profile wake delivery, action-claim heartbeats, and worker-side heartbeats for the action claim, capacity-one local-agent lease, and native Kanban run claim.

### Changed

- Runtime reconciliation now executes at most one action per successful tick while preserving the native Hermes Kanban database and state machine as canonical.
- Profile workers submit typed invocation responses before native terminal handoff, reuse the same task/session scope for one bounded retry, and complete only after the matching canonical task/run outcome is read back; missing responses, lost fencing, dead Desktop ownership, and unrelated snapshot changes fail closed.
- A terminal native handoff without a typed response may relaunch the same bound session once so the response can be recovered without creating a new invocation or task claim.
- Agent plugin startup defers standalone sibling-module loading until the corresponding KWC tool is called, avoiding plugin-discovery iteration races.
- CLI Profile sessions may bind typed responses with their durable `HERMES_SESSION_ID` when no Gateway `HERMES_SESSION_KEY` exists.

## [0.13.0] - 2026-10-08

### Added

- Added `kwc_profile_response` for invocation-bound Coder and Reviewer results without natural-language JSON parsing.
- Added configurable `execution_profiles` visibility, while the coordinator retains the compatibility `kwc_pm_response` tool.

### Changed

- Generalized the invocation response service across Profiles while preserving exact Profile/session/outcome binding, replay fencing, and privacy-safe persistence.
- The coordinator sees only `kwc_pm_response`; non-coordinator execution Profiles see only `kwc_profile_response`, avoiding duplicate response tools in one session.

## [0.12.0] - 2026-10-08

### Added

- Added a canonical `promote-task` mutation adapter that calls Hermes Kanban functions without modifying Core.
- Added snapshot-fingerprint fencing before mutation and canonical task read-back after mutation.
- Added idempotent recovery when a previously promoted task has already advanced to `ready`, `running`, `review`, or `done`.

### Changed

- Exposed canonical workflow snapshot fingerprinting as a shared KWC action/mutation contract.
- The mutation adapter is not yet wired to the Runtime action executor; durable action claims remain the execution handoff boundary.

## [0.11.0] - 2026-10-08

### Added

- Added a durable `kwc-workflow-action-v1` outbox keyed by a canonical snapshot fingerprint and compact WorkflowEngine action contract.
- Added capacity-one action claims with expiry, stale takeover, claim generations, and exact completion replay.
- Added `GET /runtime/actions` for privacy-safe dispatch outbox read-back.

### Changed

- Runtime reconciliation now materializes actionable `dispatch-task`, `wake-profile`, and `promote-task` decisions exactly once per canonical snapshot.
- Invalid decisions, terminal decisions, and `wait-for-profile` states do not enter the outbox. No action is executed and no Kanban state is mutated in this release.

## [0.10.0] - 2026-10-08

### Added

- Added a durable `kwc-agent-lease-v1` capacity-one lease for the `local-agent` execution slot.
- Added atomic acquire, heartbeat, release, read, stale-takeover, generation tracking, and exact retry behavior on SQLite WAL.
- Added concurrent-acquire and privacy-schema regression coverage.

### Changed

- A released or expired lease id cannot be reused; each new execution generation must use a new opaque lease id.
- This release reserves the serial execution slot only. It does not yet spawn Profile workers or execute WorkflowEngine actions.

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
