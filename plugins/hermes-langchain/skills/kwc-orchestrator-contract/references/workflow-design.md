# KWC profile-independent workflow design

> 상태: KWC runtime contract. 기존 PM Profile은 비교 baseline으로 유지하지만 이 workflow의 실행 의존성이 아니다.

## 목표

KWC는 service planning, requirement, current implementation, Issue, Kanban execution, independent review와
local finalization을 연결한다. 각 situation마다 fresh agent에 정확한 prompt, 공통·상황별 Skill, Toolset,
MCP, workspace, output schema와 verification contract를 주입한다.

```text
approved requirement + fresh current-state + leaf Issue
→ KWC Triage authoring
→ KWC self-contained graph authoring
→ Coder implementation + self-verification
→ KWC implementation checkpoint
→ independent aggregate Review
→ KWC finding routing 또는 Summary finalization
→ current-state snapshot과 승인된 release transition
```

## identity 분리

```text
host Profile: model/provider/credential/plugin/session infrastructure
execution role: Kanban의 project-manager actor identity
behavior: KWC manifest prompt + kwc-orchestrator-contract + situation Skill
```

Fresh execution은 `--ignore-rules`, `load_soul=false`, `load_memory=false`를 사용한다. 따라서 host Profile을
바꿔도 동일 manifest와 repository state라면 같은 workflow contract를 수행해야 한다.

## planning admission

새 planning과 graph mutation은 clean repository, 승인 requirement, fresh current-state와 leaf Issue identity가
확인된 뒤에만 시작한다. 문서가 충분하면 broad source discovery를 하지 않는다. policy·ownership·authorization·
consistency·error semantics가 미결정이면 Decision으로 route하고 graph authoring을 중단한다.

## authoring

1. `kwc-create-triage`가 하나의 native Triage intake를 먼저 생성·read-back한다.
2. bounded analysis를 canonical frozen Triage로 materialize한다.
3. `kwc-build-task-graph`가 self-contained Impl, Review, Summary topology를 validator-valid artifact로 만든다.
4. native creation 뒤 body, assignee, workspace, links, status와 single-ready frontier를 read-back한다.
5. activation은 read-only preflight 뒤 한 target만 promote한다.

## execution과 checkpoint

Coder는 Impl card를 구현하고 focused/full verification evidence를 typed handoff로 남긴다. KWC checkpoint fresh
agent는 canonical card, run, handoff, Git diff와 selector를 다시 읽고 필요한 검증을 독립 실행한다. 통과한
경우에만 명시적으로 승인된 commit boundary를 적용하고 SHA, committed paths와 clean worktree를 read-back한다.

## aggregate Review와 rework

Review promote 직전에 `aggregate-review-admission-v1`을 검증·comment·read-back한다. Reviewer는 fixed
`baseline_sha..reviewed_head_sha`와 impact closure를 검토해 `aggregate-review-result-v2`를 남긴다.

- `approved`: prior blocking finding이 모두 `resolved`일 때만 Summary 가능
- `correction-required`: canonical finding에서 corrective Impl→next Review 생성
- `context-required`: committed source evidence를 next Review context로 전달
- `decision-required`: user-owned Decision으로 route
- crash/timeout: 같은 task infrastructure recovery; corrective graph 근거가 아님

## finalization과 release boundary

Summary는 local evidence와 final implementation SHA를 freeze하고 current-state 갱신·marker 종료를 검증한다.
Remote push, PR 생성·merge와 Issue close는 별도 명시 승인과 외부 상태 read-back이 있는 경우에만 수행한다.
KWC는 Profile prose나 성공적인 exit code만으로 완료를 선언하지 않는다.

## 비교

PM baseline과 KWC fresh agent는 동일 prompt hash, workspace, model route와 expected result로 실행한다.
Comparison artifact는 session/profile/model metadata, duration, token count, exact-result verdict와 redacted inputs를
기록한다. 단일 prompt 비교는 특정 계약 회수 능력만 증명하므로, authoring·checkpoint·finding routing·finalization
각 situation의 실제 artifact와 mutation read-back을 별도로 검증한다.
