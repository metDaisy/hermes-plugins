# Kanban Control

Kanban Control registers `/kcp run <request>` as a deterministic entrypoint for an existing Hermes Project Manager workflow. It does not define a replacement workflow. It creates one ready Kanban card for the configured manager Profile and embeds the configured manager, worker, and reviewer Profile bindings in the card body.

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
/kcp run Issue #151을 구현해줘
```

Only `run` is currently registered. Run it from the Hermes Desktop chat session that should receive progress updates. The command validates configuration, Profile existence, and the live session notification target, then creates a ready manager card through the native `kanban_create` tool. Native Kanban subscribes the originating Desktop session to the task and KCP returns the run ID plus native task ID immediately.

The originating session receives native Kanban lifecycle notifications when a subscribed task is claimed, changes status, completes, blocks, crashes, times out, or gives up. KCP reports `progress: this session` only when `kanban_create` confirms that the root subscription was persisted. Native Kanban copies a parent's subscription to linked child cards, so manager-created worker and reviewer cards remain visible in the same Desktop session when they are connected to the run graph. An unlinked card is intentionally outside that progress stream.

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
