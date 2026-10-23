# Hermes LangChain

`hermes-langchain`은 workflow controller의 **유일한 canonical 구현 대상**입니다. 새 controller 기능은 이
Plugin에만 추가하며, `kanban-workflow-controller`와 `context-workflow-harness`는 deprecated source
reference로만 취급합니다. 현재 전환 순서, 상태 소유권과 milestone non-goal의 정본은
[`docs/plan.md`](docs/plan.md)입니다.

내부 workflow definition, Step/attempt, transition과 interrupt/resume cursor는 LangGraph durable
checkpoint가 소유하고, accepted semantic output은 독립 immutable Artifact Store가 소유하는 방향으로
전환합니다. Hermes native Kanban은 공개 task/run/comment/event와 역할 handoff의 정본이며, 외부 mutation
뒤 exact read-back이 있어야만 controller transition을 허용합니다.

아래 `0.32.x` 이하 Runtime 설명은 migration 이력을 보존한 historical reference입니다. `0.33.0`의 실행
계약과 상태 소유권은 이 문서의 앞부분, Plugin Skill과 `docs/plan.md`가 우선합니다.

## Profile-independent fresh agents

KWC의 PM 역할 실행은 특정 Profile의 SOUL, memory, 또는 설치 Skill을 행동 입력으로 사용하지 않습니다. KWC는 상황별 `kwc-fresh-agent-execution-v1` manifest에 prompt, 공통 `kwc-orchestrator-contract`, 상황 Skill, MCP server, toolset, workspace, Kanban role, allowed response tools를 고정하고 새 Hermes session을 실행합니다. `--ignore-rules`는 host Profile의 SOUL·memory·project rules·auto-loaded Skills를 제외하며, host Profile은 model/provider/credential/Plugin registration infrastructure만 제공합니다.

`kwc-orchestrator-contract`는 PM 책임·capability coverage와 38개 distribution-owned source artifact의 KWC target을 machine-readable reference로 고정합니다. Service planning은 `web`·`browser`, Gradle 검증 단계는 `gradle-mcp`를 situation별로만 주입합니다.

Plugin은 설치된 `project-manager`, `coder`, `reviewer` Profile의 SOUL·memory·Skill을 실행 입력으로 요구하지 않습니다. 같은 문자열은 native Kanban의 task role/assignee identity로만 유지합니다.

## LangGraph integration

- canonical controller 경로는 `src/hermes_langchain/controller/`의 `StateGraph`, SQLite checkpointer와 public Hermes fresh-Agent adapter입니다.
- authoring과 delivery lifecycle은 Plugin-owned Skill, immutable artifact, bounded toolset과 native Kanban read-back으로 구성합니다.
- legacy `runtime/`, Profile launcher와 KWC-compatible background worker는 shipped source에서 제거했으며 새 기능을 deprecated Plugin에 복제하지 않습니다.

## Kanban board 사용 여부

**사용합니다.** Hermes native Kanban board는 공개 task/run/comment/event와 역할 handoff의 source of truth입니다.
LangGraph는 controller-private workflow cursor와 transition을 소유하며, Plugin은 외부 mutation 뒤 exact Kanban
read-back과 artifact provenance가 확인된 경우에만 다음 transition을 허용합니다.

```text
Native Kanban board (canonical state)
  → snapshot + fingerprint
  → deterministic KWC decision/outbox
  → LangGraph authoring route
  → fenced native mutation
  → exact Kanban read-back
```

새 workflow는 일반 host session에서 `hermes_langchain_begin_authoring`을 호출해 시작합니다. 입력은 exact
board slug, Issue 번호, fresh generation과 absolute workspace path입니다. 이 Tool은 Plugin이 설치된 host
Profile을 infrastructure로 유지한 채 `project-manager` execution role과 assignment-bound fresh session을
생성합니다. `hermes_langchain_route`는 진단용 next-action resolver일 뿐 workflow start/claim 권한을 만들지
않으며, 사용자는 과거 `.temp` artifact나 assignment id를 검색해 직접 재개하지 않습니다.

## KWC migration and cutover

- `plugins/hermes-langchain`은 canonical controller, authoring/delivery Skill, prompt, contract와 regression test를 독립적으로 소유합니다.
- 기존 `plugins/kanban-workflow-controller` source는 migration 과정에서 수정하거나 삭제하지 않습니다.
- deprecated KWC와 context harness는 frozen source reference이며 runtime dependency가 아닙니다.
- controller checkpoint와 Artifact Store는 Plugin-local state를 사용하고, native Kanban board 자체는 계속 canonical external state로 사용합니다.
- 설치 시 deprecated controller를 disable하고 `hermes-langchain`만 enable합니다.

Phase 7 read-only assessment와 Issue별 실행 증거는 프로젝트·환경 종속 로컬 자료이므로 Git에 포함하지
않습니다. `0.33.0`은 Profile 기반 compatibility hook/response/worker와 중복 background Runtime을 제거하고,
Plugin-owned fresh Agent와 explicit workflow contract를 canonical 실행 경로로 사용합니다.

`0.30.0`은 첫 canonical activation primitive를 추가합니다. `hermes_langchain_activate`는
`controller-task-activation-v1` accepted Artifact Store record와 exact native snapshot에 결속된 기존
`todo`/`blocked` task 하나만 `ready`로 promote합니다. Controller-owned port가 mutation 직전 snapshot을
다시 확인하고 task identity, body digest, assignee, workspace, skills와 idempotency key를 exact read-back합니다.
이미 progress 상태인 동일 task의 retry는 두 번째 mutation 없이 같은 receipt를 반환합니다. 이 increment는
아직 graph card 생성, worker claim/dispatch, Impl/Review/Summary lifecycle completion을 수행하지 않습니다.


Hermes LangChain routes native Kanban lifecycle events to the Project Manager and adds a KWC-owned comment conversation path between Profiles. It keeps native Kanban as the source of truth: every KWC comment is persisted by `kanban_db.add_comment()` and therefore appears in the ordinary task-card thread and `commented` event history.

## Versioning

`plugin.yaml` is the canonical version source. The migration starts at `0.27.0`; `0.28.0` moves PM execution behavior into profile-independent KWC fresh-agent contracts, `0.29.0` closes PM responsibility/capability/test coverage while preserving the PM Profile as a comparison baseline, `0.30.0` adds the first controller-owned accepted-task activation primitive, and `0.32.0` adds the canonical host-session authoring entry point. It does not restore the retired `kanban-control` `/kcp` command contract.

- patch: compatible bug fixes and routing corrections;
- minor: backward-compatible event, tool, endpoint, or session-routing capabilities;
- major: incompatible state, wake-envelope, configuration, or installation contracts.

Every functional change must update `plugin.yaml` and add the matching newest release to `CHANGELOG.md`. Documentation-only corrections may keep the current version.

## Removed Runtime model (`0.32.x` 이하 historical reference)

> 다음 Runtime/comment/Profile-response/Desktop 설명은 제거된 compatibility surface의 이력입니다.
> `0.33.0`에서는 해당 hook, endpoint, worker와 background service를 등록하거나 제공하지 않습니다.

### Desktop-owned runtime lifecycle

The Desktop status contribution renews a machine-wide singleton KWC Runtime lease through `POST /runtime/lease` every 10 seconds. The namespaced API starts `hermes_langchain.runtime.runtime_service` as a Python module with the same interpreter as `desktop-serve`, records the runtime generation and process ownership in KWC's SQLite database, and reuses the live process across Profile backends.

```text
Hermes Desktop
└─ desktop-serve
   └─ KWC Runtime child
      ├─ heartbeat + parent/stop monitoring
      ├─ canonical snapshot → pure WorkflowEngine → durable action outbox
      └─ capacity-one action executor → fenced native adapter / Profile worker
```

Starting with `0.8.0`, the Runtime performs read-only reconciliation for every board previously observed through KWC's durable `board_heartbeat` rows. It reads tasks, direct graph links, runs, and events through canonical Hermes Kanban functions, normalizes only the fields needed by the workflow contract, and asks the pure WorkflowEngine for exactly one next action or a fail-closed error. The latest compact decision is persisted per board; task bodies, comments, prompts, logs, session keys, and reasoning are not copied into KWC state.

The Runtime keeps native Hermes Kanban as the canonical mutation and dispatch surface. `GET /runtime/decisions` exposes the latest compact decision and `GET /runtime/actions` exposes the privacy-safe durable action state, while `/status` reports Runtime health and native dispatch-tick health separately as `dispatcher_running`. Native Gateway dispatch stays enabled; KWC neither changes `kanban.dispatch_in_gateway` nor modifies Hermes Core.

Starting with `0.9.0`, KWC also has the first execution-plane contract: a durable pending invocation can accept exactly one structured Project Manager result through `kwc_pm_response`. The result is bound to the invocation id, configured coordinator Profile, durable Hermes session key, snapshot fingerprint, and an invocation-specific outcome allowlist. An identical retry is idempotent; a different replay is rejected. The response tool only records validated intent and still performs no Kanban mutation.

Starting with `0.10.0`, the execution plane also owns one durable `local-agent` lease. Atomic acquire, heartbeat, release, and stale takeover serialize every future local Profile worker through one VRAM slot. A fresh opaque lease id fences each holder while the generation records replacement history; released or expired lease ids cannot be reused.

Starting with `0.11.0`, each actionable WorkflowEngine decision is materialized into a durable `kwc-workflow-action-v1` outbox row keyed by the canonical snapshot fingerprint and compact action contract. Repeated reconciliation of the same snapshot is idempotent. Action claims are capacity-one, expire safely, and support stale takeover; invalid, terminal, and `wait-for-profile` decisions are not queued. `GET /runtime/actions` exposes this privacy-safe outbox.

Starting with `0.12.0`, KWC has a fenced canonical adapter for `promote-task`. It recomputes the current canonical snapshot fingerprint before mutation, calls Hermes Kanban's native `promote_task`, and reads the task back. A retry is idempotent after the task has already reached `ready`, `running`, `review`, or `done`. Hermes Core remains unmodified.

Starting with `0.13.0`, invocation-bound typed responses cover all execution Profiles. The coordinator keeps the compatibility `kwc_pm_response` tool, while configured non-coordinator execution Profiles use `kwc_profile_response`. Each session sees only its applicable response tool, and the same durable Profile/session/outcome/replay checks protect both paths.

Starting with `0.14.0`, the Desktop Runtime claims and executes at most one durable action per successful reconciliation tick. `promote-task` uses a snapshot-fenced native promotion adapter. `dispatch-task` revalidates the canonical task/run/profile contract, starts the native run, creates a task-scoped Hermes session in the target Profile, and launches a detached worker. `wake-profile` resumes the coordinator's bound durable session without starting a second Hermes process. Action, local-agent lease, and native run claims are heartbeated and generation-fenced; Desktop parent exit, timeout, missing typed response, stale snapshot, or lost ownership fail closed.

Starting with `0.15.0`, KWC owns card authoring as a durable four-phase assignment. The Project Manager authors semantic JSON artifacts with the assigned Skill, then calls exactly one phase-specific tool. KWC supplies the canonical board, Issue, generation, workspace, and source-card identities; invokes the installed PM renderer/validator/native helper; reads back exact native results; and only then advances Triage → Impl → Review → Summary. Generic card creation and legacy v2/v3 routing schemas are not accepted.

Starting with `0.16.0`, the first `create-triage` migration increment moves the pure `triage-v7` contract, composition, validation, and freezing behavior into KWC. The installed PM helper remains a compatibility facade for the unchanged CLI and native adapter while KWC's project-independent activity and port contracts establish the later materialization boundary.

Starting with `0.17.0`, `kwc_create_triage` accepts one `kwc-triage-authoring-v1` semantic artifact plus current-state inspection. KWC composes and freezes the canonical `triage-v7` body, performs native materialization and exact read-back through its Kanban port, and rolls back a failed creation. The PM `triage.py` helper is no longer part of the runtime path.

Starting with `0.18.0`, Triage authoring is a resumable KWC-owned sub-state machine: `intake → discovery → source_confirmation → analysis → materialize`. Each assignment exposes only one stage prompt and `kwc_advance_triage`; KWC validates and records the canonical stage artifact before issuing a fresh fenced assignment for the next stage. The final `kwc_create_triage` accepts only the assignment id because KWC owns current-state inspection, artifact assembly, freeze, native creation, rollback, and exact read-back. The Project Manager no longer loads a `create-triage` Skill.

Starting with `0.19.0`, `kwc-triage-discovery-v2` does not trust a fixed Codebase Memory project name. The submitted `index_status` projection is resolved against the assignment workspace, actual root existence, and current Git HEAD. Only one exact match is accepted; orphaned, stale, or ambiguous projects produce a typed unavailable result and require bounded fallback discovery.

Starting with `0.20.0`, a completed board can begin exactly its next authoring generation from a fresh Project Manager session. KWC archives the completed assignment snapshot before fencing the new `triage:intake` assignment. Incomplete workflows, skipped generations, and Issue/workspace changes remain rejected.

The same release permits planning-only Triage against the exact committed baseline when the inspection proves that every dirty path is KWC/Profile metadata under `.hermes`, no implementation-covered path changed, and `WORKTREE_DIRTY` is the only inspection error. Product-source, requirement, build, or mixed dirty state remains a blocker, and Coder admission/final quality gates still require their own clean or explicitly fenced recovery contract.

Starting with `0.27.0`, `POST /authoring` introduced a dedicated Project Manager one-shot for each board and generation.

Starting with `0.28.0`, Authoring and PM workflow actions instead launch profile-independent fresh agents. Every run receives an explicit KWC manifest, the common `kwc-orchestrator-contract`, one situation Skill, MCP/toolset allowlist, and output contract; it runs with `--ignore-rules` in a fresh host-Profile session and never resumes the PM Profile session. The `project-manager` value remains the Kanban role/assignee and typed-response identity, not the behavior source.

Workers receive only compact locators on their command line and environment. They do not persist task bodies, prompts, transcripts, reasoning, raw commands/output, or credentials in KWC state. A typed Profile response is accepted only from the invocation-bound Profile/session/outcome. The worker must submit the typed response before the terminal Kanban mutation because native terminal handoff tools end the turn. KWC completes the durable action only after both that response and the matching canonical task/run handoff are visible; it never trusts process exit or an unrelated board snapshot change.

The Runtime exits when its owning `desktop-serve` PID disappears, when `POST /runtime/stop` marks its generation for shutdown, or when another generation safely takes ownership.

Lifecycle routing remains passive:

```text
KWC observes a PM-owned workflow situation
→ KWC writes a fresh-agent execution manifest
→ a new host-Profile session loads only the explicit common contract, situation Skill, and toolsets
→ native Hermes dispatch executes ready cards for their assignee
→ Coder request-review enters the native same-card PM checkpoint lane
→ Reviewer completes the aggregate Review card directly
→ KWC routes only schema-tag-matched blocked/completed workflow events that require PM coordination
```

Delivery is hybrid rather than a universal push callback. Native completed/blocked/worker lifecycle hooks provide an immediate reconciliation hint in the hook caller, while `on_kanban_dispatch_tick` scans the durable event cursor as recovery. Ordinary comments created outside KWC have no Core comment hook; the supported blocked-Decision comment is discovered by that periodic scan. The gateway dispatch interval is configurable through `kanban.dispatch_interval_seconds` and defaults to 60 seconds.

The workflow boundary is explicit:

- `backend-implementation-card-v5` is executed by the canonical `coder` Profile. A successful Coder run calls native `request-review`, and Hermes dispatch starts the Project Manager's same-card checkpoint run. `review_requested` is not routed by KWC.
- `aggregate-review-card-v5` is executed by the canonical `reviewer` Profile. Reviewer `completed` wakes the Project Manager so it can validate the canonical aggregate result and route findings or Summary progression.
- A blocked implementation or aggregate Review run wakes the Project Manager for canonical task/run/comment/event read-back. Workers use `blocked + needs_input`, not `request-review`, for problem escalation.
- A valid human comment on a blocked PM-owned `policy-decision-card-v1` also wakes the Project Manager.

Coder and Reviewer keep independent Profile sessions and memory. PM-role coordination runs as isolated KWC fresh agents; the preserved Project Manager Profile is comparison-only. KWC shares only native Kanban task, run, event, comment, lifecycle locators, and explicit execution manifests.

Comment routing is KWC-owned and Core-independent:

```text
Desktop calls POST /api/plugins/hermes-langchain/comments
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
POST /api/plugins/hermes-langchain/comments
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

## Profile invocation responses

KWC supplies an opaque invocation id and an invocation-specific outcome contract. Profiles complete that invocation with a typed tool rather than emitting JSON in prose. The coordinator uses the compatibility tool:

```text
kwc_pm_response(
  invocation_id="inv_pm_01H...",
  outcome="completed",
  task_id="t_impl1",
  reason_code="CHECKPOINT_VERIFIED"
)
```

Coder and Reviewer sessions use the generic execution-Profile tool:

```text
kwc_profile_response(
  invocation_id="inv_coder_01H...",
  outcome="completed",
  task_id="t_impl1",
  reason_code="IMPLEMENTATION_VERIFIED"
)
```

`reason_code` is optional but, when present, must be a compact uppercase identifier such as `MISSING_INPUT` or `POLICY:USER_DECISION_REQUIRED`; it is not a free-form reasoning field. Runtime-created worker invocations currently allow only `completed`, `blocked`, or `failed`. The worker must submit the typed response before the terminal Kanban mutation, then perform the matching native handoff (`done`/`completed`, `review`/`review_requested`, or `blocked` with a compatible run outcome). `kwc_pm_response` is available only in the configured coordinator Profile, while `kwc_profile_response` is available only in configured non-coordinator `execution_profiles`. KWC never returns the bound session key in either tool result. Unknown invocations, wrong Profiles, wrong sessions, non-allowlisted outcomes, and conflicting replays fail closed.

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

The unified Desktop half has a separate opt-in gate. After installation, open **Capabilities → Plugins** and enable the Desktop toggle for `hermes-langchain`; enabling the Agent plugin alone does not show the status item.

KWC does not have a board allowlist. The gateway's native dispatch tick and lifecycle hooks identify boards as they appear; KWC keeps a separate durable cursor and heartbeat for each board. A newly observed board starts at its current event frontier, while an immediate lifecycle hook includes the event that triggered discovery.

The default comment targets are `project-manager,coder,reviewer`. Change the comma-separated list only when the workflow uses different literal Profile names:

```bash
hermes -p main config set plugins.entries.hermes-langchain.settings.comment_profiles project-manager,coder,reviewer
```

Each listed Profile must receive at least one normal top-level turn after the plugin loads, allowing KWC to capture its durable `HERMES_SESSION_KEY`. Until then, comments targeting that Profile remain queued as `retry_pending`.

The default coordinator Profile is `project-manager`. Override it only if the workflow uses another literal Profile name:

```bash
hermes -p main config set plugins.entries.hermes-langchain.settings.coordinator_profile project-manager
```

Lifecycle filtering is fail-closed to the workflow's canonical `coder` and `reviewer` Profile names. These worker identities are not aliases for the coordinator and are intentionally not configurable independently of the workflow contracts.

Desktop Kanban writes a human comment with author `dashboard` by default. If the actual user-facing comment surface writes another stable author, configure the same literal value on the dispatcher host:

```bash
hermes -p main config set plugins.entries.hermes-langchain.settings.decision_comment_author dashboard
```

Message injection is capability-gated by Hermes and must be explicitly allowed on the Profile whose plugin instance handles dispatch ticks:

```bash
hermes -p main config set plugins.entries.hermes-langchain.allow_gateway_injection true
```

The global coordinator binding, per-Profile comment-session bindings, per-board event cursors/heartbeats, Runtime owner generation/heartbeat, privacy-safe reconciliation health, latest compact WorkflowEngine decisions, durable action outbox/claims, Profile invocation/response ledger, durable Profile wake queue, capacity-one local-agent lease, comment delivery ledger, and activity log are stored machine-wide under:

```text
$HERMES_ROOT/plugin-data/hermes-langchain/state.db
```

This file is only a routing/delivery index, Runtime lifecycle store, privacy-safe decision cache, action outbox, invocation-response ledger, Profile wake queue, local-agent lease, and activity log. Workflow decisions, action rows, and wake rows contain only board/time, snapshot hash, fixed phase/action/reason/error codes, task ID, Profile, Skill, invocation ID, claim state, and compact result code. Invocation rows additionally retain the durable session key required for ownership validation, but it is never returned by the response tool. Lease rows retain only opaque lease/holder ids, status, generation, and timestamps. These contracts store no task body, comment body, prompt, free-form reasoning, raw command/result, log, or credential; Kanban remains the workflow and lifecycle authority.

## Activity log

KWC records observable controller actions for 30 days in the same `state.db`:

- board monitoring becoming active;
- coordinator session binding;
- actionable events accepted for delivery;
- non-actionable events filtered;
- rejected delivery left pending for retry;
- event hints skipped because no coordinator session is bound.

Each record contains only time, board, fixed action/outcome/reason codes, task ID, source event ID, and event kind. It never stores comment bodies, prompts, wake-message text, session keys, reasoning, raw commands/results, or credentials.

The authenticated endpoint is `/api/plugins/hermes-langchain/activity?limit=100`. Desktop exposes the same records through the **LangChain logs** sidebar page.

## Desktop status

The unified Desktop half contributes a compact item to `statusBar.right`, the right side of the bottom status bar, plus a **LangChain logs** sidebar page.

- `LangChain` uses the normal status-bar text color.
- `(running)` uses the Hermes success-green token while the Desktop-owned KWC Runtime child is alive, its parent `desktop-serve` process is alive, and its heartbeat is fresh.
- `(stopped)` uses the Hermes danger-red token when the API is unavailable or Runtime ownership is absent, stale, stopping, or dead.

The indicator first renews `POST /api/plugins/hermes-langchain/runtime/lease`, then checks the namespaced `/status` endpoint every 10 seconds. The response exposes native dispatcher health independently as `dispatcher_running`, `boards`, and `last_dispatch_tick_at`; a live Runtime does not imply that Gateway-hosted native dispatch is currently active. `GET /api/plugins/hermes-langchain/runtime/decisions` provides the latest compact decision for each monitored board and `GET /api/plugins/hermes-langchain/runtime/actions` provides privacy-safe action claim/result state.

## Retry behavior

Each board cursor advances through ignored events immediately. For an actionable event, that board's cursor advances only after `ctx.inject_message(...)` accepts the wake request. A rejected injection leaves that event pending for the board's next dispatcher tick. SQLite serializes the binding check, wake acceptance, and cursor update, so concurrent dispatcher ticks cannot deliver the same event or route from a stale binding. Rebinding the coordinator preserves all committed board cursors, and refreshing the same durable session preserves pending events.

The stored target is the durable gateway/TUI `HERMES_SESSION_KEY`, not the ephemeral internal agent session id. Binding also requires `HERMES_SESSION_PROFILE` to equal the configured coordinator Profile. Context compression therefore does not require descendant-session rebinding. A descendant can refresh only the already-bound durable key; it cannot replace the route with another session key.

Delivery is **at least once** across a host crash: if the process terminates after the host accepts the queued wake but before SQLite commits the cursor, the same minimal wake envelope can be delivered again. The envelope carries a stable `source_event_id`, and the coordinator must always read canonical Kanban state before choosing a transition, so duplicate wakes are safe and contain no copied task/comment content.

Comment delivery has the same at-least-once boundary around `ctx.inject_message()`. Request-level idempotency prevents duplicate native comments, while a stable `source_comment_id` lets the receiving Profile treat a repeated wake as the same request.

## Verification

From the repository root:

```bash
env -u PYTHONPATH plugins/hermes-langchain/.venv/Scripts/python.exe -m unittest discover -s plugins/hermes-langchain/tests -t plugins/hermes-langchain -p 'test_*.py' -v
plugins/hermes-langchain/.venv/Scripts/python.exe -m compileall -q plugins/hermes-langchain/__init__.py plugins/hermes-langchain/dashboard plugins/hermes-langchain/src plugins/hermes-langchain/tests
node --check plugins/hermes-langchain/desktop/plugin.js
hermes plugins validate plugins/hermes-langchain --json
hermes plugins doctor plugins/hermes-langchain --ci
```
