---
name: build-task-graph-board-contract
version: 8.2.0
---

# build-task-graph board contract v8.2

이 문서는 persisted graph의 schema와 machine-checkable invariant를 소유한다. Runtime 절차는
[`../SKILL.md`](../SKILL.md), Backend Impl body는
[`implementation-card-contract.md`](implementation-card-contract.md), checkpoint는
[`../../run-workflow/references/execution-contract.md`](../../run-workflow/references/execution-contract.md)가 소유한다. Native Kanban read-back이 task ID,
assignee, status, parent, run, comment와 event의 원본이다.

## Graph wrapper

graph schema는 `build-task-graph-v6`만 지원한다. 다른 schema는 authoring과 read-back 모두 거부한다.

| Field | 계약 |
|---|---|
| `mode` | `new | requirement-rework | review-rework` |
| `issue` | Issue number와 canonical URL |
| `generation` | 양의 정수 G |
| `workspace` | 모든 native card가 사용하는 exact workspace identity |
| `planning_baseline_sha` | 최초 frozen Triage의 `planning_baseline_sha`와 같은 40자리 Git SHA |
| `requirement_basis` | path와 Git SHA |
| `revised_requirement` | `requirement-rework`에서 필수인 path와 Git SHA |
| `lineage` | Rework에서 필수인 prior generation, prior Summary, archived unfinished keys |
| `source_review` | `review-rework`에서 필수인 review key, finding별 disposition과 card별 idempotency map |
| `change_requirements` | Frozen `triage-v7`의 `intent.change_requirements`를 exact copy한 집합 |
| `behaviors` | Complete effective behavior 집합 |
| `cards` | Native read-back을 정규화한 graph membership |
| `execution_order` | 아직 실행되지 않은 Impl/Review key의 생성 순서 기반 직렬 실행 순서 |
| `ready_candidate` | Activation 대상 Impl/Review key 또는 Decision-only rework의 `null` |
| `archived_card_keys` | Native archive 상태와 양방향으로 일치하는 key 목록 |

`requirement-rework`의 prior generation은 G-1이다. `review-rework`는 같은 G를 유지하고 source Review와
append된 card의 provenance를 보존한다. 각 disposition은 `finding_id`, 원래 verdict와
`appended_card_keys`를 가지며, idempotency map은 모든 appended key를 정확히 한 번 포함한다.
Verdict별 disposition은 각각 Impl(`correction-required`), Review(`context-required`),
Decision(`decision-required`)을 최소 하나 포함한다.

이 wrapper는 validator 입력이며 별도 workflow ledger가 아니다.

## Behavior contract

각 behavior는 stable `id`, 현재 전체 `outcome`, `state`, `disposition`, evidence,
`requirement_ids`와 선택적 Impl key를 가진다. `requirement_ids`는 graph의 `change_requirements`만
참조하며 모든 change requirement는 하나 이상의 behavior에 의해 cover되어야 한다. 알 수 없는 참조,
중복 참조, 미연결 requirement는 validation failure다.

각 change requirement의 `source`는 Triage와 같은 `{locator, claim}` shape를 사용한다. Graph 작성 과정에서 source를
Issue URL·heading 같은 별도 legacy shape로 변환하거나 재해석하지 않는다.

| state | disposition | 필수 계약 |
|---|---|---|
| `implemented` | `inherited` | 새 contract를 계속 충족한다고 재확인한 source/test evidence |
| `partial` / `absent` | `planned` | 실제 Implementation card를 가리키는 `implementation_card_key` |
| `unknown` | `blocked` | graph 외부 owner가 해결할 blocker |

Planned behavior의 ID/outcome은 참조 Impl body의 effective behavior와 일치한다. `new`와
`requirement-rework`에서 각 Impl body의 behavior 집합은 그 Impl에 배정된 planned behavior 집합과
정확히 같고, `review-rework`에서도 wrapper에 없는 behavior를 포함할 수 없다. Behavior ID는 중복될 수
없다.

## Card identity와 body

Title은 다음 grammar를 따른다.

```text
G<N>-Issue<M>-Triage
G<N>-Issue<M>-Impl<P>
G<N>-Issue<M>-Review<Q>
G<N>-Issue<M>-Decision<R>
G<N>-Issue<M>-Summary
```

Issue는 positive number와 canonical HTTPS URL을 가지며 requirement revision은 40자리 Git SHA다.
Title의 generation, Issue, kind는 wrapper와 `card_type`에 일치한다. Impl body의 Issue도 wrapper Issue와
정확히 같다. Card key와 title은 고유하고 모든 card workspace는 wrapper workspace와 같다. 모든 card는
integer `priority`와 `skills: []`를 명시한다. 이 필드는 worker runtime dependency만 표현하며 graph authoring provenance나
PM이 사용한 Skill 목록을 저장하지 않는다. Self-contained card에 forced Skill을 부여하거나 native
read-back의 `task.skills`가 wrapper와 다르면 validation failure다. Assignee는
Impl=`coder`, Review=`reviewer`, Triage·Decision·Summary=`project-manager`로 고정한다. Parent는 존재하는
다른 card를 가리키며 graph는 cycle이 없다.

모든 native card는 wrapper의 `workspace`를 exact `kanban create --workspace` 인자로 사용한다. 현재
native `kanban show`가 workspace를 반환하지 않으므로 PM은 생성 호출과 normalized wrapper에 그
identity를 보존하고, Coder는 claim 직후 실제 process cwd/worktree를 다시 확인한다. Validator가 native
workspace read-back까지 증명한다고 보고해서는 안 된다.

Native materialization에서 Triage 이외의 모든 card는 생성 시점부터 wrapper의 direct parent를 가져야 한다.
PM은 parent key를 앞서 생성하고 read-back한 actual task ID로 해석해 repeatable `kanban create --parent`
인자로 원자적으로 전달한다. Parentless create 후 별도 `link`로 보정하는 방식은 dependency-free card를
native `ready`로 auto-promote해 dispatcher에 노출할 수 있으므로 금지한다. 생성 직후 `status=todo`와
exact parent set을 read-back하지 못하면 해당 card를 malformed history로 archive하고 graph activation을
중단한다.

Native read-back 검증은 lifecycle phase를 명시한다. 모든 execution card가 `todo`이고 Triage가 `triage`인
materialization 직후에는 `validate-native-readback --phase draft`를 사용해 identity/body/parent와
pre-activation 비노출을 함께 검증한다. Activation adapter가 Triage를 `done`, candidate 하나를 `ready`로
만든 뒤에는 fresh show envelopes로 `validate-native-readback --phase native`를 다시 실행한다. Native
phase를 materialization 전 검증에 사용하거나 draft phase 결과를 activation receipt로 재사용할 수 없다.

Persisted body schema는 다음과 같다.

- Impl: `backend-implementation-card-v5`만 지원한다. 필드 의미와 validation은
  `implementation-card-contract.md`가 소유한다.
- Review: `aggregate-review-card-v5`. Frozen Triage와 같은 `triage_digest`, Graph의
  `planning_baseline_sha`와 같은 `baseline_sha`, complete `behavior_ids`, 참조 Impl keys, structured
  `acceptance`, `review_checks`, explicit `scope_exclusions`를 보존한다. 각 acceptance는 stable ID와 criterion,
  behavior·Impl·source acceptance·verification 연결을 가진다. 각 review check는 stable ID, focus, concrete
  question과 expected outcome, 같은 coverage references 및 `docs/backend-development-guide.md#...` 또는
  `docs/backend-test-guide.md#...` locator를 가진다. 구현이 있으면 implementation-convention과 test-quality를,
  Impl이 둘 이상이면 두 card 이상을 함께 참조하는 cross-card-seam check를 요구한다. Inherited behavior는
  complete behavior와 Impl slice behavior의 차이로 계산하고 preservation check로 cover하며 별도 목록을
  복제하지 않는다. Review closure 안에서 Impl acceptance ID는 card 간에도 고유해야 한다. Verification ID는
  Frozen Triage verification matrix를 여러 Impl이 공유할 수 있으며, 각 acceptance/check row의 source ID는
  그 row가 참조한 Impl 중 하나 이상이 실제 소유해야 한다. Guide locator는 현재 guide의
  literal heading과 일치해야 한다. Criterion, question, expected outcome은 한국어로 구체적인 판단 기준을
  설명해야 하며 최소 설명 밀도를 충족하고 서로 중복될 수 없다. Question은 명시적 의문문이어야 한다.
  Fast path는 이 semantic text를 자동 생성하지 않고 PM이 source closure와 guide를 근거로 작성한 뒤 동일
  validator를 통과시킨다. Review 시점 HEAD·actual changed path·test result·finding·verdict·worker/session data는 body에
  넣지 않는다. Reviewer가 claim한 actual run에서 현재 HEAD를 `reviewed_head_sha`로 고정한 뒤
  `baseline_sha..reviewed_head_sha` 전체 diff와 영향 closure를 조사한다.
- Summary: `issue-summary-card-v3`. Frozen Triage의 deterministic human-readable projection으로만 작성한다.
  `issue` identity, `triage_digest`, Triage objective, ordered change requirement 기반 `target_outcomes`, ordered
  observable 기반 `preserved_behaviors`, `completion_contract`, `release_boundary`를 보존한다. Complete
  implementation acceptance와 Review 판단은 Review contract가, exact membership은 native direct parents가,
  parent completion·latest Review approval·final SHA·clean worktree는 `summary-admission-v1`이 소유하므로
  Summary body에 복제하지 않는다. `completion_contract`는 `summary-admission-v1`과 그 필수 조건만 선언하고,
  `release_boundary`는 `separate-release-card`와 `push | pull-request | ci | external-review | merge | issue-close`
  제외 action을 명시한다. Runtime status, finding, verdict, SHA, test result, worktree, PR/CI/CodeRabbit/merge
  상태는 허용하지 않는다.
- Decision: `policy-decision-card-v1`. Graph 내부 Decision은 completed Review finding에서 파생된
  review-rework card이며 Source Review key, finding ID, 사용자 질문과 `decision_owner: user`를 보존한다.
  Triage와 함께 만드는 Planning Decision은 graph wrapper 밖의 linked provenance record다.

Review body는 graph 작성 호출이 끝난 뒤 다른 Profile이 실행하는 durable contract이므로
`이번 단계`, `이번 호출`, `이번 세션`, `this phase`, `this invocation`, `graph recovery phase` 같은
ephemeral coordinator context를 포함할 수 없다. 작성 중 dispatch를 막기 위한 임시 제약은 card body가
아니라 draft-first materialization과 activation gate가 소유한다. 위 문맥이 발견되면 validator는
`EPHEMERAL_COORDINATOR_CONTEXT:<key>`를 반환한다.

모든 Review의 `triage_digest`와 `baseline_sha`는 wrapper 값과 정확히 같고 behavior 집합은 wrapper
behavior 집합과 정확히 같다. Acceptance와 review check의 union은 declared behavior, Impl, source acceptance,
verification과 정확히 같고 모든 reference는 source closure 안에서 resolve되어야 한다. Latest Review의 Impl
참조는 graph의 모든 Impl key와 정확히 같다. Summary의 `triage_digest`, Issue identity, target outcomes와
preserved behaviors는 frozen Triage projection과 정확히 같다. Native task 값은 body에 복제하지 않는다.

Fixed frozen Triage와 이미 생성된 Impl을 그대로 두고 Review만 추가하는 body-only 경로는
`validate-review-graph`를 사용한다. 이 validator는 source body와 Review closure를 검증하되 기존 card로부터
재구성할 수 없는 full graph priority, requirement coverage와 단일 behavior owner invariant를 재판정하지 않는다.

## Topology

Native parent는 scheduling prerequisite다.

### `new`와 `requirement-rework`

- Triage는 graph 작성 중 native `triage`인 creator gate다.
- 이전 graph가 malformed 상태로 archive되어 source Triage가 이미 `done`이면 source task는 history로
  유지하고, exact frozen body를 가진 새 PM-owned recovery Triage를 native `triage` creator gate로 사용한다.
  Done Triage를 새 execution card의 parent로 재사용해 native auto-promotion을 activation 대용으로 삼을 수 없다.
- Planned behavior가 있으면 `ready_candidate`는 Implementation이며 Triage를 parent로 가진다.
- Planned behavior가 없으면 `ready_candidate`는 aggregate Review이며 Triage를 parent로 가진다.
- 각 Impl은 최소 하나의 Review와 Summary의 parent다.
- 모든 Review는 Summary의 parent다. Planning Decision은 Summary parent가 아니다.

### `review-rework`

- Triage, source Review와 기존 history는 `done`이다.
- Append된 첫 Impl 또는 context Review는 source Review를 parent로 가진다.
- Corrective Impl은 next Review와 Summary의 parent다.
- Append된 Review와 Decision은 Summary의 parent다.
- Decision-only rework는 `ready_candidate: null`이며 Decision status는 `blocked`다.

모든 mode에서 Summary는 하나이며 semantic Issue root다. Graph membership은 Summary body 목록이 아니라
native direct parent read-back으로 검증한다. Summary direct parents는 모든 Impl, Review와 Decision의
집합과 정확히 같다.

이 workflow는 local agent의 VRAM 제약 때문에 serial execution을 전제로 한다. 실제 제품·구조 prerequisite만
native parent dependency로 표현하고 독립 card 사이에 가짜 parent를 추가하지 않는다. 아직 실행되지 않은 모든
Impl/Review는 생성 순서대로 `execution_order`에 정확히 한 번 나타나며 첫 항목부터 `N..1`의 내림차순
`priority`를 가진다. Triage·Decision·Summary priority는 `0`이다. `ready_candidate`는 non-empty
`execution_order`의 첫 key와 같아야 한다. PM은 현재 card가 terminal이 된 뒤 다음 순서의 card 하나만
promote하여 어떤 시점에도 local worker가 한 card만 실행하게 한다.

## Phase status invariant

### Draft

- `new`와 `requirement-rework`: Triage는 `triage`; 다른 execution card는 `todo`; `ready` card는 없다.
  Planning Decision은 wrapper 밖에서 관리한다.
- `review-rework`: Triage와 pre-existing history는 `done`; Summary와 appended execution card는
  `todo`, Decision은 `todo | blocked`; `ready` card는 없다.

### Native

- `new`와 `requirement-rework`: Triage는 `done`; `ready_candidate` 하나만 `ready`; 나머지 execution
  card는 `todo`다. Planning Decision은 activation 전에 `done`이어야 한다.
- `review-rework`: Triage, source Review와 pre-existing history는 `done`; candidate가 있으면 그 card
  하나만 `ready`; appended 후속 card와 Summary는 `todo`다. Decision-only면 ready card가 없고
  Decision은 `blocked`다.

`archived_card_keys`와 native `archived` status는 양방향으로 일치한다. Done history는 archive하지
않는다. Requirement supersession으로 obsolete한 unfinished card만 archive할 수 있다.

## Review finding contract

Canonical finding은 completed Review의 latest run metadata `findings`에 있다. `review-rework` validation은
이 canonical result를 별도 입력으로 받아 `source_review.dispositions`의 finding ID/verdict 집합과 정확히
대조한다. Comment는 canonical
source가 아니다. 각 finding은 source Review 범위에서 고유한 `finding_id`, `basis`, `observed_fact`,
`evidence`, `impact`와 다음 verdict를 가진다.

| verdict | graph 결과 |
|---|---|
| `correction-required` | 기존 effective behavior를 충족하는 corrective Impl |
| `context-required` | approved source를 반영한 next Review |
| `decision-required` | 같은 G의 blocked Decision |
| `resolved` | 후속 Review가 이전 finding의 해결을 확인 |

Unresolved finding은 disposition 없이 사라질 수 없다. `resolved` finding은 해결한 source Review ID와
source finding ID를 명시한다. 파생 card는 source Review/finding provenance와 idempotency key를 가지며
같은 provenance의 work는 하나만 존재한다. Summary는 모든 direct parent가 `done`이고 latest Review
finding이 이전 blocking finding을 명시적으로 resolved했으며 새 blocking finding이 없을 때만
promotion될 수 있다.

## Validation boundary

`scripts/build_task_graph.py validate-graph`는 wrapper schema, identity, lineage, change requirement→behavior
coverage, behavior reference, Review/Summary coverage, cycle, mode별 topology, phase status, archive invariant를 검증한다. Validator
통과는 semantic behavior 완료 증거가 아니다. Native persistence는 read-back으로, behavior 완료는 PM
checkpoint와 독립 Reviewer evidence로 증명한다.

`validate-native-readback`은 모든 active card의 public `kanban show --json` envelope를 배열로 모아 wrapper와
대조한다. title, assignee, status, body와 parent task ID를 함께 비교하므로 Summary가 Review를 direct parent에서
누락한 경우를 local wrapper만으로 통과시키지 않는다.
