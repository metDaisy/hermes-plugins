# Hermes LangChain workflow controller 전환 계획

> 기준일: 2026-10-24
> 상태: **`0.33.0` 전환 완료. `hermes-langchain`은 유일한 canonical 구현 경로이며 PM/Coder/Reviewer workflow는 Plugin-owned Skill과 fresh Agent로 실행된다. Profile launcher, wake/response/comment routing과 background Runtime은 제거했다. Issue별 production activation은 각 Issue의 current-state와 accepted artifact gate를 별도로 통과해야 한다.**
> 정본 위치: `plugins/hermes-langchain/docs/plan.md`

## 1. 목표

workflow의 주도권을 Agent의 대화·Profile memory가 아니라 결정론적 Machine이 가진다.

```text
Deterministic Workflow Machine
  → 현재 runnable Step 선택
  → accepted upstream artifact에서 Step input projection
  → Step 전용 fresh Agent 실행
  → typed output validation
  → immutable artifact와 checkpoint 저장
  → deterministic transition
  → 다음 Step
```

Agent는 현재 Step의 의미 판단·구현·검토만 수행한다. 다음 단계 선택, retry, resume, artifact 수락,
외부 mutation과 완료 판정은 Machine이 소유한다.

## 2. 단일 구현 경로와 deprecated 정책

workflow controller의 새 구현은 **오직 `plugins/hermes-langchain`에만 추가한다.**

| 구성요소 | 상태 | 허용되는 역할 | 금지되는 역할 |
|---|---|---|---|
| `hermes-langchain` | **canonical** | workflow definition, LangGraph controller, executor registry, artifact/checkpoint 경계, adapter, workflow pack의 유일한 구현 위치 | 다른 plugin에 새 controller 기능을 이중 구현하는 것 |
| `kanban-workflow-controller` | **deprecated migration reference** | 이전 production 구현의 snapshot/fencing/lease/idempotency/native mutation/read-back/rollback 패턴을 조사하고 필요한 부분을 검증 후 이식하는 source reference | 새 workflow 기능, 새 상태 소유권, production cutover 대상, `hermes-langchain`의 runtime dependency |
| `context-workflow-harness` | **deprecated experiment** | stateless Agent/Provider interface 아이디어를 참고하는 제한된 source reference | 별도 workflow engine, 별도 plugin product line, 기능 확장 |

Deprecated는 즉시 삭제를 뜻하지 않는다. KWC는 parity와 rollback 판단에 필요한 역사적 구현 증거로 보존한다.
단, 새 기능은 KWC에서 먼저 구현하거나 양쪽에 동시에 구현하지 않는다. 이식할 때는 다음 순서를 따른다.

1. KWC의 해당 seam과 테스트가 보장하던 invariant를 확인한다.
2. `hermes-langchain`의 현재 contract에 맞는 최소 interface로 다시 표현한다.
3. failing acceptance test 또는 gate를 먼저 고정한다.
4. source를 그대로 runtime dependency로 연결하지 않고 필요한 로직만 독립적으로 이식한다.
5. `hermes-langchain`에서 read-back과 회귀 검증을 완료한다.

두 workflow controller plugin을 동시에 활성화하지 않는다.

## 3. 상태 소유권

서로 다른 의미의 상태를 한 저장소에 중복 소유하지 않는다.

| 상태 | canonical owner |
|---|---|
| workflow definition, current Step, attempt, transition, interrupt/resume cursor | `hermes-langchain` LangGraph durable checkpoint |
| accepted semantic output | `hermes-langchain` immutable Artifact Store |
| task, assignee, native run, comment/event, 공개 execution history | Hermes native Kanban |
| source 사실 | 현재 Git checkout의 direct source read |
| requirement | repository requirement document와 Issue |

Kanban은 공개 실행·감사·역할 handoff의 정본이다. 내부 LangGraph transition의 정본은 아니다.
외부 mutation은 materialize adapter만 수행하며, exact native read-back이 없으면 다음 transition을 열지 않는다.

checkpoint와 artifact에는 raw prompt, 전체 transcript, chain-of-thought, credential, 전체 tool output을 저장하지 않는다.

## 4. 현재 기준선

2026-10-21 기준 `hermes-langchain`에는 다음 최소 controller probe가 구현·검증돼 있다.

- 실제 LangGraph `StateGraph`: project → execute → validate → persist → advance/interrupt
- SQLite checkpointer와 `thread_id=run_id`
- accepted artifact 기반 projection
- public `PluginContext.subagent_lifecycle` fresh Agent adapter
- explicit non-empty toolset, acceptance criteria와 output schema
- fixed `controller-probe-v1`의 `start`, `resume`, `status`
- completed task replay 시 duplicate Agent invocation 방지
- invalid/oversized output과 민감한 failure text의 checkpoint 유입 차단
- fresh process 사이의 2-Step start → resume → status 완료

로컬 검증 기록은 Git에서 제외된 `plugins/hermes-langchain/GATES.md`가 소유한다. 이 기준선은 production cutover가 아니다.
Triage/Coder/Reviewer/Kanban workflow와 Issue #151은 아직 기존 경로에 남아 있다.

## 5. 최종 불변조건

### Machine이 소유한다

- workflow definition과 현재 run 상태
- runnable Step과 transition 선택
- Step별 goal, model, workspace, Skill, tool/MCP allowlist
- upstream artifact 선택과 최소 context projection
- fresh Agent lifecycle fencing
- schema/contract validation
- retry, timeout, interruption, resume와 duplicate prevention
- immutable artifact, digest, attempt와 audit event
- native Kanban materialization, read-back과 rollback
- approval/admission gate

### Agent가 소유한다

- 현재 Step goal의 의미 판단
- 허용된 evidence 조사
- 구현 또는 검토
- 정해진 output schema에 맞는 결과 제출

### Agent가 소유하지 않는다

- 다음 Step 또는 Tool 선택
- 자기 결과 승인
- 이전 session/memory/history 재구성
- 허용되지 않은 Tool/MCP 사용
- process exit나 성공 prose로 완료 선언
- hidden state 전달

## 6. 목표 구조

```text
plugins/hermes-langchain/
├── src/hermes_langchain/
│   ├── controller/          # LangGraph state, graph, transition, checkpoint
│   ├── workflow/            # definition, projection, validation, executor registry
│   ├── runtime/             # fresh Agent port, artifact store, invocation
│   ├── adapters/            # Kanban, Git, Codebase Memory, Semble
│   └── workflow_packs/      # issue delivery workflow
└── docs/
    └── plan.md              # 이 문서
```

현재 source 배치는 점진적으로 이 경계에 수렴시킨다. 구조 정리 자체를 별도 대규모 리팩터링으로 수행하지 않고,
각 milestone에서 필요한 최소 이동만 허용한다.

## 7. 구현 순서

순서를 건너뛰지 않는다.

### Phase 0. 방향과 문서 경계 고정 — working tree 완료

- canonical implementation target을 `hermes-langchain`으로 고정한다.
- KWC와 context harness를 deprecated source reference로 고정한다.
- 상태 소유권과 Kanban 경계를 이 문서에 고정한다.
- project-local plan은 이 문서를 가리키는 pointer로 축소한다.
- Issue #151 Generation 재생성, activation과 dispatch 중단을 유지한다.

이 파일은 Git 관리 repository 안에 생성됐지만 현재 작업 정책에 따라 아직 commit하지 않는다. 다른
`hermes-langchain` 변경과 논리적 마감 시점에 commit된 뒤 source-control 보호까지 완료된다.

### Phase 1. Pure workflow contracts — working tree 완료

- immutable `WorkflowDefinition`, `StepDefinition`, `Transition`을 정의한다.
- immutable `WorkflowRun`, `StepRun`, `ArtifactRef`를 정의한다.
- duplicate ID, missing edge, cycle, unreachable Step, invalid initial frontier를 검증한다.
- 외부 I/O 없이 다음 runnable Step과 typed failure를 결정하는 unit test를 작성한다.

Pure contract는 accepted Step history를 `WorkflowRun`으로 표현한다. LangGraph checkpoint의 실행 cursor는 기존
`ControllerState`가 소유하므로 같은 의미의 별도 immutable `WorkflowState`를 중복 추가하지 않는다.

완료 기준: Agent, Kanban, plugin boundary 없이 pure graph contract만으로 frontier가 결정된다.

검증 기록: `GATES.md` M3 G15~G19 통과. 전체 456 tests, source compile, Plugin validation과
Plugin Doctor가 통과했다.

### Phase 2. Executor와 artifact boundary — 완료

- `agent`, `deterministic`, `gateway`, `materialize` executor registry를 정의한다.
- 별도 immutable Artifact Store를 도입한다.
- input/output digest, attempt, timeout과 validation result를 저장한다.
- invalid output은 artifact로 accept하지 않고 bounded correction 또는 blocked edge로 보낸다.
- fake executor로 crash/restart, retry와 duplicate prevention을 검증한다.

M4에서 네 executor kind의 pure registry와 별도 immutable SQLite Artifact Store를 구현했다. M5에서 기존
controller runtime과 plugin service를 registry/Artifact Store에 연결했다. accepted payload는 Artifact Store만
소유하고 checkpoint에는 `ArtifactRef`와 실행 metadata만 남는다. projector와 public response는 Artifact Store
read-back을 통해 payload를 복원한다. projected input은 adapter 호출 전에 immutable copy로 고정하고 digest를
계산하며, 저장 payload는 매 read마다 digest를 다시 검증한다.

artifact write 직후 process failure를 주입한 검증에서 재구성 runtime은 executor를 다시 호출하지 않고 동일
artifact를 exact replay했다. 기존 M2/M3 checkpoint는 accepted payload를 자체 보유하여 새 Artifact Store와
호환되지 않으므로 자동 해석하지 않는다. M5 service는 `controller/v2` persistence layout을 사용해 기존 probe
checkpoint와 명시적으로 격리한다.

### Phase 3. 3-Step profile-independent E2E

```text
Step A: 입력에서 요구사항 추출
→ Step B: A의 accepted artifact만 받아 결과 작성
→ Step C: B 결과와 필요한 A projection만 받아 검토
```

검증 조건:

- 세 Step이 모두 다른 Hermes session을 사용한다.
- Profile SOUL, memory, 이전 대화, project rules와 auto-loaded Skill을 상속하지 않는다.
- 각 Step은 선언된 goal, tools, MCP, context와 output schema만 받는다.
- B 완료 뒤 process를 종료해도 C부터 resume한다.
- replay/retry가 duplicate invocation 또는 artifact를 만들지 않는다.
- typed output validation만 완료를 판정한다.

구현된 M6 경로는 host Profile 대신 controller-owned empty Hermes home을 사용하고, 각 Step을
`--ignore-rules --ignore-user-config`가 적용된 별도 Hermes CLI process/session으로 실행한다.
model/provider와 credential environment는 명시적 runtime transport contract로만 전달하고, 그 외
process environment는 provider transport key allowlist와 격리된 HOME/APPDATA/TEMP로 제한한다.
Hermes source-tree `.env`의 key는 Hermes와 동일한 `python-dotenv` parser로 식별한 뒤 값 없이 선점해
quoted key를 포함한 fallback 주입을 막고, managed scope는 격리된 empty directory로 override한다.
실제 Hermes CLI와 통제된 로컬 provider fixture를 사용한 A/B process 종료 후 C resume 검증은
complete provider request에서 hostile ambient prompt/prefill/home sentinel과
undeclared projection이 없음을 확인했다. 세 session identity는 모두 달랐고, Step별 artifact는 정확히
하나씩 유지되며 이전 Step 재호출은 발생하지 않았다.

### Phase 4. Existing Triage parity — 완료

- 기존 5개 Triage stage를 coarse Step으로 감싼다.
- 기존 prompt/schema/semantic validator를 adapter로 사용한다.
- body, digest, failure와 native read-back parity를 비교한다.
- Codebase Memory/Semble 결과를 bounded `EvidenceBundle`로 정규화한다.

M7에서 intake → discovery → source-confirmation → analysis → materialize를 canonical controller의 선형
five-Step workflow로 감쌌다. assignment projector는 선언된 identity field만 전달하고, `EvidenceBundle`은
비어 있지 않은 absolute workspace/Git HEAD를 고정하며 Codebase Memory·Semble·direct-source
locator/claim을 bounded record로 정규화한다. 정규화된 값만 invocation digest와 producer projection에
들어간다. 네 authoring Step은 legacy prompt를 controller-only JSON producer contract로 감싼 뒤 기존
schema와 semantic validator를 그대로 호출한다. accepted Artifact Store payload는 canonical body와 exact
legacy file bytes를 함께 소유하며, restart 후 다음 Step 전에 digest를 확인하고 predecessor file을
재구성한다. run-local scratch 경로는 정규화한 ID에 full run ID digest를 붙여 충돌을 막는다.
materialize Step은 명시적으로 주입된 port가 없으면 fail closed하고 기존 freeze와 injected native
read-back activity를 재사용한다. accepted materialization은 body text SHA-256, body의 frozen digest,
report의 digest/path 및 `valid`/`verified`가 모두 일치해야 한다. 독립 workspace의 baseline/controller
실행에서 frozen body, file SHA-256, frozen digest와 read-back report가 일치했고 invalid discovery
failure는 controller public boundary에서 sanitization된 동일 `ValueError` 분류로 유지됐다. production
Kanban 연결은 Phase 6까지 보류한다.

### Phase 5. Coder와 Reviewer 전환

- `coder`, `reviewer`를 Profile 이름이 아니라 workflow role로 취급한다.
- Coder에는 implementation card, bounded evidence, workspace와 verification contract만 제공한다.
- Reviewer에는 accepted plan, diff, verification evidence와 rubric만 제공한다.
- 기존 Profile 경로는 비교 baseline으로만 유지한다.

M8에서 `coder_execute → reviewer_review`를 canonical delivery workflow로 구현했다. Coder는
immutable implementation card, bounded evidence, repository-root workspace identity와 closed Gradle
verification contract만 받고, controller가 독립적으로 read-back한 verification evidence와 최종 workspace
diff가 일치해야 accepted artifact가 된다. Reviewer는 accepted plan/diff/evidence와 closed rubric만 받으며
isolated non-repository workspace와 명시적 tool-free process mode에서 실행된다. review result는 plan/diff/
evidence digest, changed path와 실제 diff hunk line에 결속된다. 각 fresh process의 Hermes home과 query file은
호출별로 격리·정리되고 cleanup 실패도 성공으로 처리하지 않는다. restart는 accepted Coder artifact에서
Reviewer만 재개하며 Coder를 다시 호출하지 않는다. 전체 491 tests와 plugin 검증 후 독립 review는
high/medium finding이 없다고 판정했다.

### Phase 6. Kanban materialization — 완료

- accepted milestone만 native Kanban에 materialize한다.
- exact read-back 없이는 transition하지 않는다.
- 이전 Generation은 historical evidence로만 사용한다.
- disposable run과 independent semantic audit 이후에만 Issue #151 activation을 검토한다.

M9에서 accepted `triage_materialize` Artifact Store record에
`controller-triage-milestone-v1` projection을 결속하고, profile-local
`triage-parity-v1` store를 사용하는 public `hermes_langchain_materialize` action까지 연결했다. Controller-owned
native port는 board/issue/generation/workspace, artifact digest와 snapshot fingerprint를 mutation 전과 native
write transaction 경계에서 검증한다. Exact replay는 active 또는 archived 상태에서도 같은 task를 반환하고
새 task를 만들지 않으며, 같은 generation의 다른 accepted bytes는 거부한다. Success는 native task의 title,
body bytes, assignee, workspace, status와 idempotency key exact read-back을 요구한다. Malformed receipt,
read-back mismatch, post-commit integrity failure와 동시에 발생한 connection-close failure까지 생성 task를
archive하고 rollback을 read-back한다. Disposable native board와 public plugin 경로를 포함한 최종 506 tests,
compileall, plugin validate/doctor, diff-check가 통과했고 독립 review는 high/medium finding이 없다고 판정했다.
Issue #151 activation/dispatch는 이 gate에서 수행하지 않았다.

### Phase 7. Profile 제거 판정 — 완료

controller-owned E2E, restart/resume, duplicate prevention, native read-back, source/runtime parity,
quality/time/token benchmark와 rollback 경로가 모두 증명된 뒤에만 Profile 제거 또는 thin host화를 검토한다.
Profile 삭제 자체가 목표가 아니다.

M10 판정에 사용한 Phase 7 assessment는 로컬 실행 증거이므로 Git에서 제외한다. 설치 read-back에서
`hermes-langchain`은 enabled, standalone KWC는 disabled였으며 canonical source에는 KWC/context harness runtime
import가 없다. KWC와 context harness source는 삭제하지 않고 새 기능을 받지 않는 frozen reference로 보존한다.

`0.33.0`에서 Profile launcher, wake/response/comment routing과 background Runtime을 제거하고 PM/Coder/Reviewer
workflow를 Plugin-owned Skill, immutable artifact, bounded toolset과 fresh Agent 실행으로 이전했다. Kanban의
`project-manager`, `coder`, `reviewer` 값은 행동 Profile이 아니라 task role/assignee identity로만 유지한다.

Issue #151 재개도 **blocked**다. GitHub Issue는 open이고 focused Cart/Offer/Modulith Gradle MCP 28 tests는
통과했지만, accepted quality-v6 Triage baseline은 현재 `aeb4df958a29f6544fa6bb784fa7234220aa6269`
checkout보다 오래됐다. Product worktree의 `.hermes` Profile 변경은 Coder admission을 막고 있으며, 34개의
Issue #151 board와 여러 incomplete generation이 남아 있다. Direct source read에서도 guest UUID 수동 parsing,
application의 CartItem 직접 mutation과 aggregate invariant 중복, direct merge mutation, Cart 소유 중복
`OFFER-001` 예외와 Offer exact assertion 미완료가 확인됐다. Phase 7은 production mutation을 요청하지 않았고,
closure verifier의 전후 board snapshot digest도 동일했다.

### Phase 8. Canonical activation primitive — 완료

accepted artifact와 canonical Kanban snapshot에 의해 이미 식별된 단일 task만 `todo`에서 native
progress 상태로 promote한다. Controller가 request validation, snapshot fencing, mutation-boundary recheck,
exact read-back, idempotent replay와 public error sanitization을 소유한다. 기존
`hermes_langchain.kanban.mutation_service`를 runtime dependency로 호출하지 않고 controller port 뒤에서
재구성한다. 첫 increment는 graph 생성이나 dispatch를 수행하지 않으며 disposable board만 변경한다.

M11에서 `canonical-task-activation-v1`과 public `hermes_langchain_activate`를 추가했다. Complete
task/link snapshot, accepted artifact/task identity와 parent eligibility를 하나의 native write transaction에서
다시 확인하고, status CAS와 artifact-bound audit receipt를 함께 commit한다. Exact retry는 receipt를 요구하며
task가 원래 `todo`/`blocked` 상태로 돌아온 경우에도 두 번째 promotion을 거부한다. Public failure는 closed
reason code만 노출한다. Disposable board E2E, 525 tests, `compileall`, Plugin validation/Doctor,
`git diff --check`가 통과했고 최종 독립 review는 `no high/medium findings`를 보고했다.

### Phase 9. Accepted task-graph authoring and materialization — 완료

accepted graph artifact에서 Impl/Review/Summary card와 dependency를 native Kanban에 materialize한다. 모든
card identity, body digest, parent link, workspace, generation과 ready candidate를 exact read-back하고 partial
failure를 rollback한다. Agent output이나 launcher exit만으로 성공을 판정하지 않는다.

M12/G53~G57 구현은 accepted Triage provenance에 묶인 graph authoring, projection, native materialization,
rollback, replay와 disposable-board 검증까지 도달했다. Public caller가 graph bytes나 workspace file을
선택하는 경로는 제거하고, controller가 fresh Agent authoring 결과를 immutable accepted authoring artifact로
저장한 뒤 canonical `build-task-graph-v6` validator를 통과한 artifact만 projection한다. Authoring artifact는
accepted Triage digest, planning baseline, issue/generation/workspace, executor identity와 exact input/output digest에
묶인다.

독립 review에서 발견된 canonical Summary dependency closure, caller-selected authoring file, read-before-limit,
execution priority, parallel source-root Implementation, post-commit rollback 결함을 remediation했다. Summary는 모든
Impl과 Review를 parent로 갖고, modular validator priority는 installed Skill validator와 동일하게 전체 executable
order를 사용한다. `ready_candidate`가 source-root Implementation인지만 요구하므로 병렬 root를 허용한다. Native
port는 transaction exit 전에 attempt-owned task ID를 보존하며 post-commit integrity failure에서도 hard-delete
rollback과 restored snapshot을 검증한다. Rollback은 native `delete_task()`의 global readiness recomputation을
사용하지 않고 attempt-owned rows를 하나의 write transaction에서 hard-delete하므로 unrelated promotable task의
status/event를 변경하지 않는다. Caller-selected file intake는 제거했고, untouched canonical v6 graph를
validator mock 없이 projection→materialization하는 회귀 test를 추가했다. 최종 검증은 29개 focused
graph/topology/public tests, 538개 full plugin tests, 128개 Skill helper tests, compileall, Plugin validation/Doctor,
scoped diff check를 통과했고 독립 review는 `no high/medium findings`를 보고했다. Graph materialization은 M11
activation과 분리하며 이 Phase에서 claim, worker launch, dispatch 또는 production Issue #151 mutation을 수행하지
않았다.

### Phase 10. Canonical dispatch and lifecycle completion

controller-owned action이 ready/review task를 generation-fenced native run으로 claim하고 fresh Hermes process를
launch한다. Typed delivery result, native run/task handoff와 accepted ArtifactRef를 함께 확인한 뒤에만 다음
transition을 선택한다. crash/restart, stale takeover와 duplicate prevention을 full lifecycle에서 검증한다.

M13의 첫 vertical slice는 하나의 ready implementation task에 한정한다. Controller가 snapshot/generation과
accepted graph receipt를 다시 확인하고 native run claim을 원자적으로 기록한 뒤, 고정된 toolset·Skill·workspace와
ArtifactRef만으로 fresh Agent를 launch한다. 완료 결과는 typed delivery artifact와 native run/task exact read-back이
모두 일치할 때만 accepted 된다. 이어서 aggregate Review와 Summary completion을 같은 primitive에 연결하고,
`start → Triage → graph → activation → Impl → Review → Summary → complete → restart → resume` 전체 disposable
workflow에서 duplicate task/run/Agent invocation이 없음을 증명한다. 이 gate가 완료되는 시점부터 Plugin-only
전체 workflow를 제한적으로 사용할 수 있다.

#### M13 완료 — 2026-10-22 Core lifecycle completion

- **Claim과 provenance:** `controller-kanban-dispatch-v1`은 accepted graph/activation ArtifactRef,
  issue/generation/workspace, `activation_key`와 mutation-boundary snapshot을 다시 확인한 뒤 exact native run 하나만
  claim한다. Claim 이후 receipt write가 실패해도 동일 run을 재사용하며 duplicate run을 만들지 않는다.
- **Fresh Agent와 delivery:** canonical graph card는 `skills: []`를 유지하고 implementation, aggregate Review,
  Summary의 Skill·goal·toolset·workspace는 validated role에서 controller가 도출한다. Public lifecycle request에는
  지원하지 않는 `working_directory` field를 보내지 않고, workspace는 bounded context envelope에만 둔다. Agent는
  invocation-keyed immutable directory의 exact `skill.md`, graph/activation/dispatch JSON과 digest manifest를 읽으며
  semantic request body, Profile SOUL, memory, prior conversation 또는 transition 선택권을 받지 않는다. Hermes
  lifecycle launch handle은 동일 runtime의 controller restart에서 correlation별로 재사용한다.
- **Claim ownership과 result recovery:** Agent 실행 전·실행 중·결과 수락 직전에 exact native claim을 heartbeat하고
  task/run/claim-lock ownership이 사라지면 delivery를 fail closed한다. Agent result payload는 canonical digest와
  producer/run/role-derived step provenance를 가진 별도 result Artifact Store record로 resolve되며 completion 전에
  다시 읽고 검증한다. Agent가 반환한 logical result name은 identity로 직접 사용하지 않고
  `task-result:<invocation-key>:<index>` persisted ID로 namespace하므로 서로 다른 task의 자연스러운 같은 이름이
  충돌하지 않는다.
  Immutable provisional result를 final delivery receipt보다 먼저 저장하므로 receipt write 실패 후에도 Agent를
  다시 launch하지 않는다.
- **Completion:** `controller-kanban-completion-v1`은 typed delivery와 resolved result ArtifactRef를 exact claimed
  `run_id`에 결합하고 native task `done`, run `done`, outcome `completed`, `current_run_id=null`과 completion event의
  dispatch/delivery/result metadata를 exact read-back한다. Native completion commit 뒤 failure-counter/readiness
  finalization이 실패해도 matching event replay가 finalization을 재수행한다. Completion receipt write 실패와 replay는
  두 번째 completion event나 run을 만들지 않으며 event payload tamper는 거부한다.
- **Dependency transition:** Implementation completion이 Review/Summary readiness를 native dependency로 승격하면
  activation은 두 번째 promotion을 수행하지 않고 existing dependency promotion을 receipt로 attest한다.
- **Public Plugin surface:** accepted graph materialization은 모든 materialized task에 대해 activation ArtifactRef를
  public receipt에 projection한다. `hermes_langchain_dispatch`, `hermes_langchain_invoke_agent`,
  `hermes_langchain_complete`를 closed schema와 sanitized stable reason-code boundary로 등록했다. Canonical service는
  graph, activation, dispatch, delivery, result, completion Artifact Store를 분리해 연결한다.
- **Full lifecycle evidence:** fresh subprocess의 disposable native board에서 Triage source를 graph로 materialize한 뒤
  Implementation → Review → Summary를 순차 activation/claim/Hermes lifecycle delivery/completion했다. 최종 세 task는
  모두 `done`, Agent launch 3회, native run 3개, claim event 3개, completion event 3개이며 restart replay 후에도
  count가 증가하지 않는다. Crash boundary는 claim 후 artifact write, lifecycle launch 후 controller interruption,
  Agent result 후 final receipt write, native completion 후 artifact write를 각각 주입해 검증했다.
- **두 번째 독립 review remediation:** 첫 반복 review는 authoring/mutation Skill을 delivery Agent에 재사용한 문제,
  result step/producer가 invocation에 완전히 결속되지 않은 문제, profile-global Agent-selected result ID 충돌,
  claim/completion event provenance의 부분 read-back을 지적했다. Implementation/Review/Summary 전용 delivery Skill을
  추가하고 lifecycle mutation toolset을 제거했다. Result record는 invocation-scoped controller ID, authenticated
  `agent_id`, exact role step과 wrapped semantic payload digest를 소유한다. Native claim은 claim lock과
  graph/activation provenance 전체를, completion event는 중복 top-level field가 없는 단일 canonical metadata
  representation 전체를 initial read-back과 replay에서 비교한다.
- **검증:** 두 번째 review remediation 뒤 dispatch suite 20 tests, focused public/adapter/plugin 36 tests,
  full plugin 563 tests,
  `kwc-build-task-graph` helper 128 tests와 `compileall`, `git diff --check`가 통과했다. Disposable E2E는 canonical
  `skills: []` graph, heartbeat, completion-finalization crash recovery, exact event tamper rejection과 result ArtifactRef
  resolution을 포함한다. Plugin validation/Doctor와 `git diff --check` 최종 재검증도 통과했다. 최종 독립 review가
  `no high/medium findings`를 보고해야 M13을 완료로 전환한다.
- **세 번째 독립 review remediation:** lifecycle launch handle의 process crash 내구성, partial result-set write,
  invocation-scoped result replay contract, terminal task exact read-back, Review/Summary predecessor hydration을 다시
  점검했다. Provisional Agent result를 complete set으로 먼저 저장한 뒤 개별 result ArtifactRef를 idempotently
  materialize하므로 partial write retry가 Agent를 재실행하지 않는다. Delivery replay와 completion은 namespaced
  result ID, invocation key/index/logical ID, semantic payload digest, authenticated Agent, dispatch/accepted refs와
  role-derived execution contract를 다시 검증한다. Completion replay는 activation-derived title/body digest/assignee/
  workspace/skills/idempotency key와 canonical terminal result를 exact read-back한다. Review는 완료된 Implementation,
  Summary는 완료된 Implementation과 Review의 native completion, dispatch/delivery receipt와 result payload를 모두
  검증한 `predecessors.json`을 immutable hydration으로 받는다.
- **현재 검증:** focused crash/result/completion/predecessor tests 5개, dispatch/lifecycle suite 24개,
  public/adapter/plugin/profile-independent suite 39개가 통과했다. 이어진 독립 review의 4개 finding도 TDD로
  remediation했다. Launch 성공 후 handle persistence가 실패하면 exclusive lock을 유지해 재실행을 차단한다.
  Delivery receipt는 top-level field set, board/issue/generation/workspace, dispatch와 accepted ArtifactRef,
  role-derived execution contract 전체를 replay/completion/predecessor hydration에서 exact 비교한다. Native
  predecessor는 graph parent 전체 집합과 일치해야 하며 task `done`, cleared task ownership/current run, canonical result,
  run `done/completed`, 단일 controller completion event를 요구한다. Completed delivery는 최소 하나의 typed result
  ArtifactRef 없이는 provisional persistence나 native completion으로 진행하지 않는다.
- 후속 독립 review의 4개 finding도 remediation했다. Stored completed delivery는 exact nested schema/identity와
  non-empty result set을 요구하고 모든 result record가 `accepted`/`agent`인지 검증한다. Completion은 nested
  delivery의 task/run/dispatch identity를 canonical dispatch에 결속하며, replay receipt의 모든 board/issue/generation,
  workspace, dispatch/delivery ArtifactRef, terminal status, result refs를 재구성한 canonical payload와 비교한다.
  Dispatch receipt는 모든 consumer에서 단일 closed field set과 provenance validator를 통과해야 한다.
- **최신 검증:** 신규 finding 회귀 5개, dispatch/lifecycle 32개, public/adapter/plugin/profile-independent 포함
  focused suite 72개, full plugin 579개, Skill helper 128개가 통과했다. `compileall`, Plugin validation/Doctor와
  `git diff --check`도 통과했다.
- 마지막 독립 review의 4개 추가 finding도 remediation했다. Provisional Agent result recovery는 exact closed payload와
  dispatched native run을 검증한다. Stored delivery와 completion은 `result_artifacts`를 최대 32개로 제한하고,
  canonical non-empty `agent_id`가 delivery/result record의 `executor_identity`와 일치해야 한다. Completion의 dispatch
  provenance는 `executor_kind=deterministic`도 요구한다.
- **최신 재검증:** 신규 회귀 4개, dispatch+adapter 48개, full plugin 583개, Skill helper 128개가 통과했다.
  `compileall`, Plugin validation/Doctor와 `git diff --check`도 통과했다.
- **Hermes Core completion:** public lifecycle handle에 immutable request digest, persisted child `session_id`와
  profile-scoped opaque recovery capability를 추가했다. `reconnect(handle, original_request)`는 capability와 request를
  검증하고 동일 `subagent_id`/`session_id`의 durable history를 불러와 continuation turn 하나를 실행한다. Changed
  request, missing capability/history, 다른 parent session은 모두 fail closed하며 새 lifecycle identity를 만들지
  않는다. Native `output_schema`도 child construction과 result `structured_payload`까지 전달된다.
- **실제 cross-process evidence:** Hermes Core 자체 subprocess와 `HermesSubagentExecutor → PluginContext public
  lifecycle` 결합 subprocess를 각각 `launch → process exit 73 → fresh process reconnect → complete`로 실행했다.
  Plugin/Core 결합 read-back은 `launch_count=1`, `reconnect_count=1`, `build_count=2`, 동일 child session, non-empty
  request digest/recovery capability를 확인했다. Full Plugin suite는 Core 소스와 결합한 이 probe를 포함해 585 tests,
  Skill helper는 128 tests가 통과했다. Core lifecycle/delegation 관련 suite는 55 tests, plugin-host lifecycle wire
  test와 compileall도 통과했다.
- **공식 API audit 반영:** 공식 example의 작은 `register(ctx)`/`ctx.register_tool()` composition pattern은 기존
  registration 모듈이 이미 따르고 있어 별도 framework를 추가하지 않았다. 수동 default plugin-data 경로는
  profile-aware Core `plugin_data_dir("hermes-langchain")`로 교체했다. Lifecycle은 private delegation import 대신
  public Core API를 계속 사용한다. 반면 immutable Artifact Store, explicit state-path SQLite와 closed controller
  response envelope은 Core `plugin_db()`/`tool_result()`가 동일한 digest·cross-profile·reason-code 계약을 제공하지
  않으므로 억지로 교체하지 않았다.
- **최종 lifecycle security remediation:** lifecycle handle, recovery capability와 launch/recovery lock은 Agent에
  projection되는 hydration directory에서 제거하고 Core `plugin_private_data_dir()` host-private boundary에 저장한다.
  Hermes의 실제 generic file read/write tools는 이 전체 private root를 차단한다. Persisted envelope에는 cleartext launch
  request 대신 SHA-256 digest만 남긴다. Cross-process recovery lease는 live owner를 차단하고 dead owner만 atomic
  stale takeover한다. Agent terminal result 뒤에는 별도 unresolved-result fence를 먼저 durable write하며, typed result
  Artifact Store `put()`과 exact read-back이 모두 성공한 뒤에만 fence/lease를 해제한다. Validation·write 실패 또는
  result-before-persist process crash 뒤 restart는 reconnect/relaunch를 거부한다.
- **최종 Core recovery remediation:** recovery HMAC은 contract version, child/parent/correlation, exact timestamp,
  provider/model/role/depth, original capability, root session과 request digest 등 immutable handle 전체를 canonical
  serialization으로 인증한다. Rebuilt child의 routing identity도 handle과 exact 비교한다. Restart는 root session의
  live compression tip을 resolve하고 Core SessionDB의 `assert_resume_safe(..., tip_only=True)`를 직접 호출한 뒤
  alternation-repaired canonical history만 주입한다. Guard 부재/예외, forged field, routing drift, missing/oversized
  history는 모두 fail closed한다.
- **최종 재-review remediation:** Agent가 terminal success를 보고한 즉시 result fetch/parse/identity validation보다 먼저
  unresolved-result fence를 생성하므로 이후 어떤 실패도 recovery lease를 조기 해제하지 않는다. Cross-process ownership은
  PID 대신 process death 때 자동 해제되는 OS file lock으로 fencing해 PID reuse가 stale owner로 오인되지 않는다. Core handle은
  resolved provider/model뿐 아니라 endpoint, API mode, request overrides, fallback chain과 ACP command/arguments의 non-secret
  fingerprint까지 HMAC으로 인증한다. Rebuilt child는 resume guard/history/identity/routing 실패, registry race loss, executor submit
  rejection에서 반드시 close된다.
- **최종 correlation/durability remediation:** reconnect는 rebuild 전과 atomic registration 시점 모두
  `(parent_session_id, correlation_id)` owner를 검증해 expired old handle이 재사용된 correlation을 탈취하지 못한다. Routing HMAC은
  provider/model/endpoint/API mode/request overrides와 sanitized fallback/ACP route만 canonicalize하고 API key, compressor 설정,
  cache/client state는 제외해 credential rotation을 허용한다. Pending-result 전이는 temp marker 생성·file fsync·directory fsync와
  write-through atomic replace를 사용하며, write/replace 실패 시 temp marker와 OS lock ownership을 유지한다. Owner process가 종료된
  뒤에도 incomplete marker가 restart replay를 fail closed한다.
- **최종 launch/fallback/fence remediation:** concurrent `launch()`도 correlation owner를 registration lock 안에서 다시 확인하여 loser
  child를 submit 전에 close한다. Fallback fingerprint는 raw config가 아니라 credential-free resolved provider/model/endpoint/API mode,
  effective request overrides와 ACP command/arguments를 사용한다. Terminal success fence는 fallible lease-file 재-read 없이 lease 취득 때
  검증한 request digest를 사용하므로 lease 파일 손상 뒤에도 pending marker가 먼저 durable하게 설치되고 restart replay가 차단된다.
- **최종 credential scrub remediation:** route fingerprint의 credential key 판정은 punctuation을 제거한 명시적 key/suffix allowlist를
  사용한다. 따라서 `max_tokens` 같은 실행 의미 필드는 보존되어 drift를 차단하고, `X-API-Key` 같은 hyphenated credential은 제거되어
  같은 transport의 credential rotation을 허용한다.
- **최신 검증:** full Plugin 594 tests, Skill helper 128 tests, Core lifecycle/delegation/private-storage 151 tests(Windows/실환경 home guard와
  무관한 3개 test만 명시적 deselect), Plugin adapter 21 tests와 실제 subprocess crash/restart·unpersisted-result·pending-replace-failure·lease-corruption probes가 통과했다.
  Core plugin-host public lifecycle wire test, 양쪽 compileall, Plugin validation/Doctor와 `git diff --check`도 통과했다.
  Core plugin-host 전체 파일의 별도 model-provider profile test 1개는 host bootstrap progress text 때문에 실패하지만
  lifecycle 대상 test는 통과하며 이번 변경 경로와 무관하다.
- **최종 독립 review:** 최신 credential scrub remediation까지 포함한 combined Core+Plugin diff를 격리 review했고
  `no high/medium findings`를 받았다. 따라서 f14/G63과 M13을 완료로 닫는다.
- **사용 제한:** production Issue #151 mutation과 Profile 제거는 계속 금지한다. M13 완료 뒤에도 M14 parity
  benchmark와 M15 fresh generation gate를 별도로 통과해야 한다.

### Phase 11. Profile parity benchmark and thin-host removal

동일한 frozen input으로 Profile compatibility path와 canonical controller path의 semantic quality, elapsed time,
token 사용량, restart/rollback 결과를 비교한다. canonical path가 열등하지 않고 legacy wake/response/worker
surface 제거 후에도 lifecycle이 보존될 때만 Profile을 thin host 또는 제거 대상으로 전환한다.

### Phase 12. Issue #151 fresh generation

clean 또는 명시적으로 격리된 product workspace와 현재 HEAD에서 authoritative board 하나를 선택해 새
accepted generation을 만든다. 모든 remaining implementation gap, aggregate Review, full Gradle gate와 final
read-back이 통과할 때만 Issue #151을 완료한다.

## 8. Source discovery adapter 원칙

- Codebase Memory: symbol, dependency, caller/callee, data flow와 blast radius 후보
- Semble: 의도 기반 위치, 유사 구현, 관련 test/document 후보
- direct source read: 최종 사실 확인

MCP 결과만으로 source 사실을 확정하지 않는다. repository root와 Git HEAD가 assignment workspace와 맞지 않으면
stale/unavailable로 fail-closed한다. raw MCP output 전체를 다음 Agent에 넘기지 않는다.

## 9. 세션·범위 제어

컨텍스트 증가가 구현 범위 증가로 이어지지 않도록 다음 규칙을 적용한다.

1. 한 작업 세션은 **하나의 Phase 또는 그보다 작은 하나의 gate**만 소유한다.
2. 구현 전에 이 계획과 관련 Skill 계약에 OWNS, acceptance check와 explicit non-goal을 고정한다. 로컬 실행 증거는 Git에서 제외된 `GATES.md`에 기록한다.
3. 현재 gate가 통과해도 같은 세션에서 다음 Phase를 자동 시작하지 않는다.
4. 인접 문제를 발견하면 blocker/evidence만 기록하고, 현재 acceptance에 필요하지 않으면 수정하지 않는다.
5. KWC에서 유용한 코드를 발견해도 현재 gate에 필요한 seam만 이식한다.
6. 대규모 rename, package 재배치, schema revision과 compatibility cleanup을 함께 수행하지 않는다.
7. context가 커지면 accepted artifact, diff, test evidence와 다음 단일 gate만 handoff하고 raw 대화를 재사용하지 않는다.
8. 완료는 구현량이 아니라 해당 gate의 실제 실행 결과와 read-back으로 판정한다.

일상적인 비유로는 공장 이전과 같다. KWC 창고에서 쓸 만한 기계를 발견해도 공장 전체를 한 번에 옮기지 않는다.
현재 생산 셀에 필요한 기계 하나만 검사·이식하고, 시운전이 끝나면 그 milestone을 닫는다.

## 10. 지금 하지 말아야 할 것

- KWC 또는 context harness에 새 workflow 기능 추가
- KWC를 `hermes-langchain` runtime dependency로 연결
- 양쪽 plugin에 같은 기능 구현
- Issue #151의 새 Triage/Impl/Review/Summary Generation 생성
- 실제 Issue #151 Coder admission, production activation 또는 dispatch
- canonical parity와 fresh-Agent E2E 증거 없이 Profile-dependent runtime을 다시 도입하는 것
- 새 card schema revision
- 특정 Issue만 통과시키는 prompt 예외
- Agent가 transition이나 다음 Tool을 선택하게 하는 것
- launcher/process exit를 성공으로 판정하는 것
- canonical read-back 없는 외부 mutation 성공 보고

## 11. 완료 판정과 후속 재개 조건

> **Phase 8 activation, M12 graph authoring/materialization, M13 dispatch/lifecycle과 `0.33.0` Profile-independent cutover가 완료됐다. Plugin-only workflow가 canonical 실행 경로다.**

M6의 profile-independent process E2E, M7의 five-Step Triage parity, M8의 Coder/Reviewer delivery workflow,
M9의 accepted Triage native materialization과 M10의 fail-closed retirement/resume 판정이 완료됐다. 후속
작업은 새 Phase를 암묵적으로 시작하지 않고 각 Issue의 다음 조건을 별도 gate로 먼저 충족해야 한다.

M11 최종 검증은 525 tests, disposable native-board activation/read-back/replay, `compileall`, Plugin
validation/Doctor와 `git diff --check`를 통과했고, 최종 독립 review는
`no high/medium findings`를 보고했다.

1. M11 accepted single-task activation은 controller-owned port와 exact read-back 뒤에서 완료됐다.
2. M12의 trusted Agent-authored artifact, Impl/Review/Summary graph materialization, exact rollback/replay와
   unmocked canonical graph E2E는 최종 review까지 완료됐다.
3. M13에서 native run claim, fresh Agent dispatch, typed handoff, completion과 full crash/restart/resume E2E를
   완료했다. 이 gate가 Plugin-only 전체 workflow의 최초 사용 가능 경계다.
4. M14 parity와 fresh Profile-free execution을 근거로 legacy Profile wake/response/worker surface를 제거했다.
5. production Issue는 clean current-HEAD snapshot, 단일 accepted generation, complete task graph, implementation,
   aggregate Review, full verification과 final read-back을 모두 통과한 경우에만 완료한다.

Deprecated KWC/context source는 frozen reference로 유지한다. 특정 Issue의 과거 assessment나 fixture를 Plugin
계약에 포함하지 않으며, production activation은 해당 Issue의 최신 current-state와 accepted artifact로 다시
판정한다.
