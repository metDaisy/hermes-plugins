# Kanban Control

Kanban Control registers `/kcp` as a deterministic entrypoint for an existing Hermes Project Manager workflow. It does not define a replacement workflow. It creates one ready Kanban card for the configured manager Profile and embeds the configured manager, worker, and reviewer Profile bindings in the card body.

## Versioning

`plugin.yaml` is the canonical version source. Kanban Control follows Semantic Versioning:

- patch: compatible bug fixes and internal corrections;
- minor: backward-compatible user-visible features or behavior changes;
- major: incompatible command, configuration, persisted-contract, or installation changes.

Every functional code, command-contract, configuration, or Desktop behavior change must update the version and add the matching newest entry to `CHANGELOG.md`. Documentation-only corrections that do not change behavior may keep the current version. The test suite rejects a mismatch between the manifest version and the latest changelog release.

## Profile configuration

Profile names are machine-local settings, not source-code constants. Configure the Profile where Kanban Control is enabled:

```bash
hermes -p main config set plugins.entries.kanban-control.settings.manager_profile project-manager
hermes -p main config set plugins.entries.kanban-control.settings.worker_profile coder
hermes -p main config set plugins.entries.kanban-control.settings.reviewer_profile reviewer
```

Choose one workspace mode:

```bash
# Shared existing checkout
hermes -p main config set plugins.entries.kanban-control.settings.workspace "C:/Users/leee/IdeaProjects/e-commerce-clone-coding"

# Or a registered Hermes project, which gives the task a managed worktree
hermes -p main config set plugins.entries.kanban-control.settings.project <project-id-or-slug>
```

An optional board override is available. If omitted, Hermes resolves the active board normally.

```bash
hermes -p main config set plugins.entries.kanban-control.settings.board <board-slug>
```

The three role Profiles must exist and must be distinct. `workspace` and `project` are mutually exclusive.

## Usage

```text
/kcp Issue #151을 구현해줘
/kcp --board issue-151-test-2 Issue #151을 구현해줘
/kcp --new-board issue-151-test-2 Issue #151을 구현해줘
```

`run` may be omitted; the former `/kcp run ...` form remains compatible. `--board` and `--new-board` are optional routing metadata placed before the request. `--board` requires an existing board. `--new-board` creates and reads back the board before registering the manager card, so a request for a separate board never starts on the currently active board. Without either option, the configured board override or active board is used. KCP removes only this command metadata and stores the remaining request text unchanged in the manager card's `request` field.

Run it from the Hermes Desktop chat session that should receive progress updates. The command validates configuration, Profile existence, and the live session notification target, then creates a ready manager card through the native `kanban_create` tool. Native Kanban subscribes the originating Desktop session to the task and KCP returns the run ID plus native task ID immediately.

The Project Manager executes in a separate dispatcher worker session. The originating chat does **not** receive a live stream of that worker's reasoning or tool calls. It receives native terminal lifecycle notifications such as status, completion, block, crash, timeout, or give-up events. KCP reports `terminal lifecycle notifications in this session` only when `kanban_create` confirms that the root subscription was persisted. Native Kanban copies a parent's subscription to linked child cards, so manager-created worker and reviewer cards remain visible in the same Desktop session when they are connected to the run graph. An unlinked card is intentionally outside that progress stream.

The unified Desktop half has no visible KCP UI. When the dispatcher creates the Project Manager worker session, it discovers that new cross-Profile session and opens it with Hermes' native `host.openSession(..., { intent: 'tab' })` path. The worker therefore appears in the existing top session tab strip, where Hermes already supports Profile and session inspection.

Desktop submission adds an internal unique run id as command metadata. The Python half uses the same id for the native task title and idempotency key, while the stored `request` remains unchanged. Session discovery requires a successful pre-dispatch baseline and an exact run-scoped title match. Polling timers are owned by the Desktop plugin context and are cancelled on plugin reload or disposal; timeout and session-open failures are logged instead of silently continuing.

KCP workflows execute serially. Set `kanban.max_in_progress` and
`kanban.max_in_progress_per_profile` to `1` in both the host gateway and active
Profile configuration. Manager cards must create a linear dependency chain, not
parallel sibling cards, so only the next eligible card can enter `running`.

## Run envelope

The manager card body is JSON with schema `kanban-control/run-request@1`. It contains:

- the original request;
- logical `manager`, `worker`, and `reviewer` Profile bindings;
- the expected manager checkpoint;
- explicit rules requiring serial Kanban handoffs and forbidding sleep/polling.

## Verification

From the repository root:

```bash
python -m unittest plugins/kanban-control/test_kanban_control.py -v
python -m py_compile plugins/kanban-control/__init__.py plugins/kanban-control/test_kanban_control.py
hermes plugins doctor plugins/kanban-control --ci
```
