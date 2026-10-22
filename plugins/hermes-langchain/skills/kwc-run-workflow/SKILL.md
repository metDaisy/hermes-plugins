---
name: kwc-run-workflow
description: KWC fresh agent가 profile SOUL·memory 없이 수행하는 "Kanban 중심 PM workflow를 시작부터 release까지 실행한다."
version: 1.6.0
author: "Amaazon project, Hermes Agent"
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [project-management, kanban, checkpoint, review, release, recovery]
    related_skills: [kwc-orchestrator-contract, kwc-create-triage, kwc-build-task-graph, kwc-sync-docs, kwc-update-current-state]
requires_toolsets: [terminal, hermes-langchain]
---

> 이 Skill의 실행 정책과 helper는 Kanban Workflow Controller가 소유한다. PM Profile copy는 비교 기준일 뿐 실행 의존성이 아니다.

# 워크플로 실행

Issue admission부터 Kanban graph 실행, PM checkpoint, aggregate Review, Summary release와 recovery까지
하나의 root workflow로 운영한다. Kanban task·run·event·comment와 Git/GitHub read-back이 실행 상태의
기준이며 별도 workflow ledger를 만들지 않는다. PM은 source·test·migration을 구현하거나 Reviewer
finding을 대신 판단하지 않는다.

Persisted runtime schema와 결정론적 invariant는
[`references/workflow-contract.md`](references/workflow-contract.md), same-card checkpoint schema는
[`references/execution-contract.md`](references/execution-contract.md)가 소유한다. Graph/card 작성은
`scripts/workflow.py`와 `scripts/checkpoint.py`는 read-only validator이며 Git, Kanban,
GitHub를 변경하지 않는다. `scripts/activate_graph.py`만 frozen Triage/Planning Decision의 complete와
single-target promotion을 fail-closed로 수행한다. 설치된 Skill 경로를 resolve해서 실행한다.

모든 script는 [`../kwc-orchestrator-contract/references/script-usage.md`](../kwc-orchestrator-contract/references/script-usage.md)의 black-box 계약으로 사용한다. 공개 schema에
맞춘 입력을 실행하고 error code가 지목한 입력 field만 수정한다. Validator나 activation adapter의 구현
소스를 읽어 숨은 rule을 추론하지 않으며, mutation script는 read-only preflight 통과 뒤에만 apply한다.

## 시작과 재개

1. 새 authoring workflow는 host session에서 `hermes_langchain_begin_authoring`을 정확히 한 번 호출해
   시작한다. `hermes_langchain_route`를 시작·권한 부여 도구로 사용하지 않고, 과거 `.temp` artifact나
   assignment id를 검색해 수동 재사용하지 않는다. 이 도구가 반환한 assignment와 dedicated fresh-agent
   session만 staged Triage/Impl/Review/Summary authoring을 소유한다.
2. Native Kanban tool schema를 확인하고 exact board의 active·archived task를 모두 read-back한다. Native
   tool이 없는 일반 PM session은 공식 `hermes kanban` CLI의 현재 `--help`를 읽은 뒤 사용한다.
3. Branch, `HEAD`, 전체 Git status, `current-state.md` freshness와 active marker를 읽는다.
4. Marker가 없고 repository가 clean이면 새 leaf Issue의 canonical delivery branch를 선택하고
   `create-triage`부터 시작한다. Planning Decision이 있으면 사용자 comment/running과 PM reply loop를
   완료한 뒤 frozen Triage를 `build-task-graph`로 materialize한다.
5. Marker가 있으면 Issue·branch·committed checkpoint·Kanban history를 비교해 정상 재개, `restart-task`,
   base-sync 또는 사용자 결정을 선택한다. Dirty 상태를 새 planning 입력으로 사용하지 않는다. Native
   card create 자체가 dirty tree에서 성공하더라도 normal workflow admission으로 인정하지 않는다.
6. `build-task-graph-v6` wrapper와 정규화한 native read-back,
   Git read-back을 각각 JSON으로 저장하고
   다음을 실행한다.

   `python scripts/workflow.py validate-state <graph.json> <board.json> <git.json>`

완료 기준: validator가 현재 phase, eligible/ready task와 **하나의** `allowed_transition`을 반환하고 실제
board·branch·marker와 일치한다. `clean_worktree: false`는 validated `restart-task-v1` admission 경로가
아니면 `DIRTY_WORKTREE_REQUIRES_RESTART_ADMISSION`으로 mutation 전에 block한다.

## Planning graph activation

Graph native read-back을 만든 뒤 먼저 `activate_graph.py`를 `--apply` 없이 실행한다. report가
`valid: true`일 때만 `--apply`를 실행한다. native complete/read-back 실패 뒤 script는 promote나 다른
graph mutation을 실행하지 않는다.
`--apply`는 mutation 직전에 모든 graph card를 다시 읽고 draft read-back과 body/status/assignee/skills/parents를
대조한다. 차이가 있으면 `LIVE_GRAPH_DRIFT:*`로 아무것도 변경하지 않는다. 단, 직전 실행이 frozen Triage의
canonical `triage -> todo|ready` 전이 직후 중단됐고 다른 모든 card field/status가 그대로인 exact partial
activation, 또는 Triage complete까지 성공하고 target promotion 전후에 중단된 상태만 idempotent recovery로
인정해 남은 Triage complete와 target promotion을 이어서 수행한다.
`triage-v7` flow에서는 frozen Triage의 SHA-256 digest와 graph의 `triage_digest`, 그리고
`intent.change_requirements`와 graph의 `change_requirements`가 각각 exact match해야 한다. 다르면 `TRIAGE_GRAPH_DIGEST_MISMATCH` 또는
`TRIAGE_GRAPH_REQUIREMENTS_MISMATCH`로 모든 lifecycle mutation을 중단한다.
이전 malformed graph를 archive한 recovery에서 source Triage가 이미 `done`이면 새 execution card를 그
task에 직접 연결하지 않는다. `build-task-graph`가 exact frozen body의 새 recovery Triage gate를 native
`triage`로 만든 뒤 activation helper가 그 gate를 완료해야 한다. Post-activation 상태를 preflight 입력으로
제출하면 `RECOVERY_REQUIRES_FRESH_TRIAGE_GATE`로 차단하며, native auto-promotion을 activation receipt로
간주하지 않는다.

Activation target이 no-Impl Review이면 PM은 actual Review task ID, card의 `baseline_sha`, current clean
HEAD와 빈 `checkpoints`를 가진 valid `aggregate-review-admission-v1` JSON을 먼저 만들고
`--review-admission <path>`로 preflight와 `--apply` 모두에 전달한다. Script는 admission을 PM-owned comment로
기록하고 exact read-back한 뒤에만 Triage completion과 Review promotion을 수행한다. Review body의
`implementation_card_keys`가 비어 있을 때만 빈 `checkpoints`가 허용된다.

Native `complete`는 `triage`를 직접 종료하지 않는다. PM은 draft/native/Triage/Decision preflight가
통과한 경우에만 `activate_graph.py --apply`를 실행한다. Script는 running Planning Decision을 먼저
완료한 뒤 `transition_triage.py` adapter로 Hermes core의 canonical no-field transition을 적용하고
title/body/assignee 불변성을 read-back하여 `ready`인지 확인한 후 native `complete`한다. Public
`kanban specify`는 auxiliary LLM이 frozen body를 재작성하므로 사용하지 않는다. 이미 `done`인 Planning Decision은 successful terminal run과 approved
payload/Triage alignment를 다시 검증한 뒤 재사용하며 다시 complete하지 않는다.

## Kanban 실행 반복

Impl을 promotion/claim하기 직전에 PM은 Git `HEAD`와 전체 dirty path를 다시 읽고 다음 exact field의
`backend-implementation-admission-v2`를 만든다: `schema`, `task_id`, `issue`, `workspace`, `card_schema`,
`baseline {head_sha, dirty_paths}`, `restart`. Initial run은 `baseline.dirty_paths=[]`, `restart=null`이어야 한다.
Restart recovery는 `baseline.head_sha/current_head_sha`와 `baseline.dirty_paths/restart.dirty_paths`가 각각 exact
match해야 한다. 다음 validator가 통과한 payload를 PM-owned comment로 남기고 exact read-back한 뒤에만 worker를
시작한다.

`python scripts/workflow.py validate-implementation-admission <implementation-admission.json>`

1. `allowed_transition`이 `promote`이면 계산된 task 하나만 promotion하고 task ID, assignee, parents,
   status와 ready count를 다시 읽는다. `claim`이면 해당 assignee의 worker만 claim한다.
2. Impl은 `coder`가 card contract를 구현·self-verify하고 native same-card PM review를
   요청한다. PM은 dispatcher가 만든 active review run에서만 checkpoint를 수행한다.
3. Impl 완료 뒤 다시 `validate-state`를 실행한다. Cached ready candidate나 title 순서로 다음 task를
   추측하지 않는다.
4. Review를 promote하기 직전에 PM은 다음 exact field만 가진 `aggregate-review-admission-v1` comment를
   `validate-review-admission`으로 검증한 뒤 남기고 read-back한다:
   `schema`, `task_id`, `baseline_sha`, `reviewed_head_sha`, `checkpoints`.
   각 checkpoint는 `implementation_card_key`, `implementation_task_id`, `checkpoint_sha`를 가진다. 그 뒤 단일
   no-Impl Review에서는 body의 `implementation_card_keys`와 admission `checkpoints`가 모두 빈 배열이어야 한다.
   Reviewer가 이 comment의 fixed HEAD를 검증하고 전체 `baseline_sha..reviewed_head_sha` diff와 영향 closure를
   검토한다. Reviewer는 axis마다 fresh isolated
   session을 사용하고 concise `aggregate-review-axis-v1` comment와 bounded TODO progress snapshot을
   read-back 가능한 evidence로 남긴다. latest terminal run metadata에는 기존 exact
   `aggregate-review-result-v2`만 남긴다. PM은 다음으로 검증한다.

   `python scripts/workflow.py validate-review-result <review-result.json> <graph.json> <expected-prior-findings.json>`

5. `validate-state`가 `route-review-findings`를 반환하면 Summary를 promote하지 않고 completed source
   Review의 canonical `findings`를 먼저 route한다. Runtime validation은 done Review마다 full
   `aggregate-review-result-v2` schema, task identity와 prior-finding history를 검증하며, schema/result 두
   필드만 있는 payload를 canonical result로 인정하지 않는다.
6. corrective work는 completed source Review의 canonical `findings`에서만 만든다. `changes-required`의
   `correction-required`만 `build-task-graph review-rework`로 same-generation corrective Impl→next Review를
   append한다. `context-required`는 committed source를 확인한 뒤 아래 `aggregate-review-context-v1`을 next
   Review comment에 기록·검증하고 Review를 생성한다. `decision-required`는 native blocked Decision으로
   route한다. worker crash·transient error는 같은 task의 infrastructure recovery이며 새 Impl/Review 생성 근거가
   아니다. Source Review/body/run/finding을 수정하거나 삭제하지 않는다.
7. Review가 `approved`이고 prior blocking finding이 모두 explicit `resolved`이면 Summary admission으로
   이동한다. `changes-required`/`blocked` Review는 task status가 `done`이어도 Summary parent completion으로
   해석하지 않는다. Runtime validator와 native task·run·metadata read-back을 반복해 매 순간 하나의 정상
   frontier만 유지한다.

`context-required`의 PM-owned evidence는 다음 exact field를 가진다.

```json
{
  "schema": "aggregate-review-context-v1",
  "review_task_id": "t_a1b2c3",
  "source_review_task_id": "t_d4e5f6",
  "source_finding_id": "REV-2",
  "owner": "project-manager",
  "source_locators": ["docs/requirement/p9/offer.md#오류-계약"],
  "committed_sha": "0123456789abcdef0123456789abcdef01234567"
}
```

PM은 `validate-review-context` 통과 후 comment를 남기고 exact next Review에서 read-back한다. Source locator는
승인된 committed requirement·ADR·public contract를 가리켜야 하며 chat 설명이나 미커밋 파일로 대체하지
않는다.

### Coder/Reviewer problem escalation

Coder 또는 Reviewer가 문제를 발견해 PM의 확인·조치가 필요할 때는 `request-review`를 문제 알림용으로
오용하지 않는다. Worker는 다음 native handoff를 남긴다.

```text
comment("PM review requested", observed fact, impact, evidence, needed action)
→ block --kind needs_input
→ PM이 show/runs/comments/events를 read-back
```

`needs_input`은 status가 아니라 `blocked` task의 typed block reason이다. PM은 자동으로 unblock하거나
완료로 판정하지 않는다. PM이 자체적으로 해결 가능한 운영·환경 문제면 근거를 comment로 남기고 native
rework/unblock 경로를 선택한다. 사용자 판단이 필요한 policy, authorization, consistency, error semantics
또는 public contract 문제면 PM-owned Decision card를 만들고 source task와 link한다. Decision card는
`status=blocked`, native `block kind=needs_input`, 결정 문제·영향·선택지·evidence·decision owner를
보존해야 하며, cross-domain contract Decision에는 `decision_request_id`, minimum capability와
producer·consumer·integration follow-up도 보존해야 한다. PM은 사용자 결정을 먼저 Decision card에
기록·read-back하고, 같은 Triage request와 상태·approved change를 대조한 뒤 requirement read-back 후 재개한다.

## 같은 카드의 PM checkpoint

1. Queued task가 `review`였고 dispatcher claim 뒤 현재 task가 `running`, assignee가 `project-manager`,
   active run의 claimed event `source_status`가 `review`인지 확인한다.
2. Immutable `backend-implementation-card-v5`와 latest Coder run의
   `backend-implementation-handoff-v2` metadata를 read-back한다. Handoff source run, card의 literal
   `runner`/`CHECK`/`CWD`/`EXPECT`, exact tests,
   `test_levels`, scenario result, implementation convention readback과 full backend result를 검수한다. 이는 Coder self-verification record이며
   PM checkpoint 실행을 대체하지 않는다.
3. Workspace·branch·Git status와 전체 diff를 읽고, PM review run에서 handoff의 exact focused tests,
   required scenarios와 full backend `test`를 `gradle-mcp`로 직접 재실행한 뒤 다음을 실행한다.

   `python scripts/checkpoint.py handoff <card.json> <handoff.json>`

4. 실제 changed/staged/untracked path가 handoff와 정확히 같고 문서가 포함되지 않으며,
   acceptance·focused verification·required scenario·`gradle-mcp test` evidence가 모두 pass인지 검수한다.
5. 수정이 필요하면 immutable card, latest handoff, normalized active PM review run과 함께
   `backend-implementation-change-request-v1`을 검증해 comment로 남기고 native
   `request-changes` 후 original Coder ownership과 종료된 PM run을 read-back한다.
6. 통과하면 `git config --get core.hooksPath`가 `.githooks`인지 확인하고 검증된 path만 stage한다. staged
   diff/name/check를 읽은 뒤
   [`docs/commit-message-convention.md`](../../../../../docs/commit-message-convention.md)를 다시 읽는다.
   실제 staged diff를 근거로 type·50자 이내 한국어 명령형 subject·본문·명시된 Issue만 작성하고 하나의
   self-contained commit을 만든다. SHA, committed path, message와 clean worktree를 read-back한다.
   Codebase Memory 재색인은 별도 승인된 maintenance operation이며 checkpoint admission 조건이 아니다.
7. Handoff가 documentation impact를 보고했으면 code commit 뒤 `sync-docs derived-docs`를 별도
   docs-only commit으로 실행하고 path·commit SHA·clean state를 read-back한다. 영향이 없으면
   `not-applicable`을 기록한다.
8. `python scripts/checkpoint.py checkpoint <card.json> <handoff.json> <active-review-run.json> <checkpoint.json>` 통과 후 checkpoint
   payload를 **같은 complete 호출의 closing metadata**로 전달해 Impl을 완료하고 `done`을 read-back한다.
   먼저 complete한 뒤 terminal run metadata를 backfill하지 않는다. 과거 terminal run이 checkpoint 없이
   닫혔다면 원본 run을 수정하지 않고 별도 recovery run/card로 복구한다.

완료 기준: task `done`, checkpoint SHA/path, clean Git state가 같은 native run과 일치한다. PM checkpoint는
aggregate Review를 대체하지 않는다.

## Summary와 사용자 최종 검토

Summary는 사용자 최종 검토를 위한 local evidence card다. Native dependency가 Summary를 `ready`로 만들었다는
사실만으로 완료하지 않으며, remote push, PR 생성·merge, GitHub Issue close를 수행하지 않는다.

1. 모든 direct parent가 `done`, latest Review의 native run metadata가 `approved`, prior blocking finding이 모두
   explicit `resolved`인지 read-back한다. 하나라도 아니면 Summary를 unblock·promote·claim·dispatch하지 않는다.
   Latest implementation checkpoint의 code commit을 `final_implementation_sha`로 freeze한다.
   그 뒤의 checkpoint documentation commit을 ordered `documentation_commit_shas`로 모으고, 각각이
   distinct descendant이며 recorded literal docs path만 변경했는지 Git으로 확인한다. Clean `HEAD`가 docs
   commit이 없으면 code SHA, 있으면 마지막 docs SHA인지 확인해 `summary-admission-v1`을 검증한다.

   `python scripts/workflow.py validate-summary-admission <admission.json> <graph.json> <native-board.json> <expected-prior-findings.json>`

2. Summary admission, final implementation SHA, documentation commit lineage와 clean local worktree를
   concise evidence로 사용자에게 제시한다. 사용자가 최종 검토할 때까지 Summary를 완료하지 않는다.
3. 사용자가 local evidence를 승인하면 Summary에 승인 범위와 native read-back을 남기고 `done`으로 완료한다.
   이 승인도 remote publication 권한을 부여하지 않는다.
4. push, PR, CI, merge, Issue close와 `issue-release-result-v1`은 Summary 이후 사용자가 별도로 명시한
   release 작업에서만 수행한다. 그 작업은 새 사용자 승인과 fresh branch/PR/Issue read-back으로 시작한다.

완료 기준: 모든 direct parent, latest approved Review, final implementation SHA, documentation lineage와 clean
worktree가 read-back되고 사용자가 local evidence를 최종 검토한 뒤 Summary가 `done`이다.

## Release finding routing

CI failure나 PR feedback마다 `release-finding-v1`을 만들고
`python scripts/workflow.py validate-release-finding <finding.json>`으로 검증한다.

- `implementation-rework`: self-contained corrective Impl과 새 Review를 append한다.
- `infrastructure-retry`: 같은 head와 동일 check의 일시적 실패만 bounded retry한다.
- `context-required`: committed context를 조사하고 새 Review contract를 만든다.
- `decision-required`: blocked Decision으로 사용자에게 올린다.
- `no-action`: evidence로 실제 비적용을 증명한다.

`target_pr`, `target_head_sha`, source URL, finding ID와 idempotency key로 중복 routing을 막는다. Rework가 PR
head의 reviewed code range를 바꾸면 이전 Review·CI 결론을 재사용하지 않는다.

## 중단된 workflow 복구

### `restart-task`

Marker는 현재 Issue/branch를 가리키고 모든 dirty path가 그 Issue에만 evidence로 귀속되지만 Kanban 기록이
유실된 경우에만 하나의 Coder recovery card를 만든다. Body는 현재 승인 requirement와 확인된 dirty
delta를 self-contained하게 materialize한 정상 `backend-implementation-card-v6` 또는 historical read-back
v5이다. PM은
task/workspace/exact dirty path를 결합한 `restart-task-v1`을
`python scripts/workflow.py validate-restart <restart.json>`으로 검증한 뒤, 해당 recovery task의 새
`backend-implementation-admission-v2` comment의 `baseline`과 `restart` 필드에 저장한다. `kanban_show`로 comment를
read-back하고 body도 Impl validator로 검증한 뒤 Coder에게 넘긴다. 아직 존재하지 않는 미래 Coder run
metadata에는 admission payload를 저장하지 않는다. Mixed·unknown path, marker mismatch, 복수 recovery
card는 사용자 결정으로 block한다.
Recovery Impl도 정상 same-card checkpoint를 거쳐 clean commit을 만든 뒤 정상 state derivation으로 돌아온다.

### `base-sync`

B가 A-owned public contract/domain rule 결함으로 blocked되면 A correction→checkpoint→B sync 순서를
유지한다. Canonical upstream/downstream branch, fixed/pre-sync SHA, `merge | rebase` 전략과 owner/rationale,
changed/conflicted path, affected B card, post-sync SHA와 verification을 `base-sync-v1`에 기록하고
`python scripts/workflow.py validate-base-sync <base-sync.json>`으로 검증한다.

공개된 shared branch history를 보존해야 하면 merge를 기본 후보로 삼고, unpublished local history를
정리하는 명시적 승인이 있을 때만 rebase를 선택한다. Git text conflict와 semantic conflict를 구분한다.
Authorization·consistency·API/error meaning 또는 effective behavior가 달라지는 semantic conflict는
`decision-required`로 사용자에게 올리고 독자 해결하지 않는다. Sync 뒤 affected B card가 stale하면
`build-task-graph`로 replacement/rework contract를 만들고 clean read-back 후 재개한다.

## 실패 처리

- Validator error, stale run, workspace/branch mismatch, unexpected dirty path: mutation 없이 block한다.
- Graph activation은 `activate_graph.py` 외의 ad-hoc complete/promote로 우회하지 않는다. Native transition
  실패 시 script가 이후 promote를 실행하지 않으며 task/run/event를 다시 읽고 종료된 run에서 재시도하지 않는다.
- Git commit 실패 또는 dirty post-commit: task를 완료하지 않고 같은 review run에서 복구한다.
- CI/PR finding: 분류 contract 없이 retry·dismiss·merge하지 않는다.
- External mutation 성공 응답만으로 완료하지 않고 exact target을 read-back한다.

## 보고 형식

```text
status: running | checkpoint-done | review-rework | release-done | blocked
issue/board:
phase/task/run:
head/final_implementation_sha:
validation:
native_readback:
external_readback:
blocker:
next_transition:
```
