# run-workflow runtime 계약

이 문서는 active Kanban graph의 실행 상태, aggregate Review 결과, Summary admission, 사용자 최종 검토와 recovery에
사용하는 persisted JSON schema와 결정론적 invariant를 소유한다. 실행 순서는 [`../SKILL.md`](../SKILL.md),
Impl checkpoint는 [`execution-contract.md`](execution-contract.md), graph/card authoring은
[`../../build-task-graph/references/board-contract.md`](../../build-task-graph/references/board-contract.md)가
소유한다. Native Kanban·Git·GitHub read-back이 원본이며 이 JSON은 별도 workflow ledger가 아니다.

## Runtime state

`scripts/workflow.py validate-state`는 다음 세 입력을 받는다.

- `build-task-graph-v6` graph wrapper
- `tasks` 배열을 가진 normalized native read-back
- `branch`, `head_sha`, `clean_worktree`를 가진 Git read-back

각 task record는 `key`, canonical `task_id`, `title`, `card_type`, `assignee`, `workspace`, `status`, graph-key
`parents`, `runs`를 가진다. `workspace`는 graph의 create intent를 보존한 normalized field이며 native
`kanban show` read-back이 아니다. Coder의 process cwd/worktree 확인과 함께 검증한다. Graph와 board
membership·identity·workspace·parent가 일치해야 하며 `done`에는 successful latest run이 필요하다.
Git read-back의 `clean_worktree`는 boolean일 뿐 아니라 normal runtime transition에서 반드시 `true`여야 한다.
`false`이면 validator는 `DIRTY_WORKTREE_REQUIRES_RESTART_ADMISSION`으로 fail-closed한다. Dirty delta를 유지한
재개는 별도 `restart-task-v1`과 그 payload를 포함한 implementation admission으로만 허용한다.
Malformed graph replacement에서 이미 `done`인 source Triage와 이미 `ready`인 새 target을 activation
preflight 입력으로 제출할 수 없다. `activate_graph.py`는 이를
`RECOVERY_REQUIRES_FRESH_TRIAGE_GATE`로 분류하며 새 native `triage` recovery gate를 요구한다.
Activation apply는 preflight 뒤 mutation 직전에 전체 native graph를 다시 읽는다. Draft snapshot과 다른
body/status/assignee/skills/parents는 `LIVE_GRAPH_DRIFT:*`이며 mutation하지 않는다. 유일한 자동 recovery는
같은 invocation sequence가 Triage의 canonical transition까지만 적용하고 중단되어 Triage만 `todo|ready`,
또는 Triage complete까지 성공해 Triage만 `done`이고 target과 나머지 card는 draft와 exact match하는
경우다. 이때 기존 Triage를 다시 specify하거나 complete하지 않고 남은 단계 뒤 target 하나만 promote한다.
Done Impl의 latest run metadata는 `backend-implementation-checkpoint-v1`, `result: pass`, valid commit SHA와
`clean_worktree: true`를 가져야 한다. Done Review는 full canonical `aggregate-review-result-v2` metadata를
가져야 하며 runtime validator는 exact field, task identity와 Review 순서에서 계산한 prior-finding history를
검증한다. 성공 문구, terminal status 또는 `schema`/`result`만 있는 축약 payload로 downstream을 열지 않는다.
`ready`와 active(`running | review`) task는 각각 최대 하나다. Validator는
`eligible_task_ids`, `ready_task_ids`, phase와 하나의 `allowed_transition`을 반환한다.
Latest completed Review가 `changes-required`이고 아직 `review-rework.source_review`로 route되지 않았으면
Summary를 eligible로 계산하지 않고 `route-review-findings`만 반환한다. Summary가 이미 `ready`이면
`SUMMARY_READY_WITHOUT_APPROVED_REVIEW`로 fail-closed한다.

## Aggregate Review 결과

Reviewer 계약이 result 의미와 producer 규칙을 소유하고, 이 문서와 `workflow.py`는 PM의 소비·routing
invariant만 소유한다. `aggregate-review-result-v2`의 PM 소비 필드는 다음과 같다.

- `review_card_key`, `review_task_id`, `review_run_id`, `generation`
- Review card와 같은 `baseline_sha`, actual Review run에서 고정한 `reviewed_head_sha`
- `result`: `approved | changes-required`
- `reviewed_checkpoints`: Impl card/task ID와 40자리 checkpoint SHA
- `prior_findings`: 이전 blocking finding의 Review task ID, finding ID, verdict
- `findings`: unique finding ID, verdict, basis, observed fact, evidence, impact, 선택적 resolution/continuation

Verdict는 `correction-required | context-required | decision-required | resolved`다. `resolved`의
`resolves` 또는 계속 blocking인 finding의 `continues`가 source Review/finding을 가리킨다. Native Review
history에서 계산한 expected unresolved set과 result의 `prior_findings`가 정확히 같아야 하며 각 prior는
resolve 또는 continue되어야 한다. `approved`에는 새·계속 blocking finding이 없다. Review contract가
참조하는 모든 Impl checkpoint를 정확히 한 번 검토한다.
Canonical payload는 completed Review의 latest terminal run metadata에서 읽는다.

## Context-required evidence

`aggregate-review-context-v1`은 `context-required` finding을 다음 Review로 넘기기 전에 PM이 확인한 committed
source를 보존한다. Exact field는 `schema`, next `review_task_id`, `source_review_task_id`,
`source_finding_id`, literal `owner: project-manager`, non-empty `source_locators`, `committed_sha`다. PM은
`validate-review-context` 통과 후 next Review에 comment로 기록하고 Reviewer는 task/finding identity와 현재
history를 대조한다. Chat, 미커밋 문서와 단순 PM 결론은 context evidence가 아니다.

## Summary admission

`summary-admission-v1`은 Summary task ID, canonical latest `aggregate-review-result-v2`, direct/done parent
ID, `final_implementation_sha`, ordered `documentation_commit_shas`, repository HEAD와 clean state를 가진다.
Validator는 graph와 normalized native board read-back을 함께 받아 Summary task ID, exact parent keys/task IDs,
parent `done` 상태와 latest Review identity를 derive해 대조한다. Canonical result는 expected prior-finding
history와 다시 대조한다. 모든 direct parent가 done이고 latest Review가 approved여야 한다. Documentation commit이 없으면 clean HEAD는 final
implementation SHA와 같고, 있으면
각 SHA가 implementation SHA와 구분되며 clean HEAD는 마지막 documentation commit SHA와 같다. PM은
각 documentation commit의 parent ordering과 exact docs-only changed paths를 Git read-back으로 별도
검증한다. 이는 Summary를 `done`으로 만들기 위한 사용자의 최종 검토 자격이며, release를 시작할 자격이 아니다.

## Release 결과

`issue-release-result-v1`은 다음 chain을 한 object에 묶는다.

1. leaf Issue와 delivery/default branch
2. `final_implementation_sha`
3. snapshot inspection SHA, `docs/current-state.md`만 포함한 docs commit, marker 제거와 clean state
4. PR number/URL, base/head branch, actual head SHA, closing Issue number
5. actual PR head SHA의 required CI check와 terminal conclusion
6. 같은 PR/head의 merge read-back
7. 해당 PR에 의한 Issue auto-close read-back

Snapshot inspection SHA는 final implementation SHA와 같고 docs commit은 별도 SHA다. PR base는 repository
default branch, head는 delivery branch, PR head SHA는 docs commit SHA다. Required check는 비어 있지 않고
`success | neutral`만 허용한다. Merge와 Issue close는 같은 PR number를 가리켜야 한다.

## Recovery 계약

### `restart-task-v1`

Marker Issue/branch/workspace와 baseline/snapshot SHA, current HEAD, dirty path, path별 Issue
attribution/evidence, 단일 recovery task/assignee, allowed scope, current `backend-implementation-card-v6` body 또는
historical read-back v5와
required checkpoint schema를 가진다. `restart-task-v1`은 recovery task의 PM-owned
`backend-implementation-admission-v2` comment에 저장되며 Impl body를 대체하지 않는다. Admission은 actual
task/Issue/workspace/card schema와 `baseline {head_sha, dirty_paths}`를 고정한다. Initial run은 clean baseline만,
restart는 `current_head_sha`와 exact dirty path가 baseline과 일치해야 한다. 모든 dirty
path가 현재 Issue에 정확히 귀속되고 active recovery task가 하나일
때만 통과한다.

### `base-sync-v1`

Upstream/downstream Issue·canonical branch, fixed/pre-sync SHA, `merge | rebase` 전략과 decision
owner/rationale, changed/conflicted path, semantic impact, affected downstream task, post-sync SHA,
`gradle-mcp` verification과 clean state를 가진다. Semantic impact가 `decision-required`이면 canonical
Decision task ID가 필수이며 그 외에는 Decision task를 허용하지 않는다.

## Release finding

`release-finding-v1`은 `ci | pr-review` source와 URL, target PR/head SHA, stable finding ID, evidence,
classification, 선택적 routed task ID와 idempotency key를 가진다. Classification은
`implementation-rework | infrastructure-retry | context-required | decision-required | no-action`이다.
Rework/context/decision은 canonical routed task ID가 필요하고 retry/no-action은 routed task를 갖지 않는다.

## Validator 경계

`scripts/workflow.py`는 duplicate JSON key와 unknown field를 거절하고 stable error code를 출력한다. Validator는
입력 payload의 정합성만 증명하며 실제 native task/run이 존재하거나 GitHub mutation이 성공했음을
대신하지 않는다. 호출자는 mutation 전후 exact target을 read-back하고 normalized payload를 만들어야
한다.
