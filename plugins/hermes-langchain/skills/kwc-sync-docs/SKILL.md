---
name: kwc-sync-docs
description: KWC fresh agent가 profile SOUL·memory 없이 수행하는 "승인 requirement 또는 검증된 구현 영향을 파생 문서와 tracker에 동기화한다."
version: 1.1.0
author: "Amaazon project"
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [project-management, documentation, github, synchronization]
    related_skills: []
---

> 이 Skill의 실행 정책과 helper는 Kanban Workflow Controller가 소유한다. PM Profile copy는 비교 기준일 뿐 실행 의존성이 아니다.

# 문서와 tracker 동기화

승인 requirement 또는 validated Coder handoff와 read-back code commit의 문서 영향을 기준으로 `current-state.md`를
제외한 파생 문서와 delivery tracker를 동기화한다. Requirement의 의미를 새로 정하지 않고, 모든 외부
mutation은 exact target을 read-back한다.

## 언제 사용하는가

- 사용자 결정이 requirement에 반영됐다.
- `create-triage` 또는 `build-task-graph`가 파생 문서·Issue 불일치를 찾았다.
- Graph authoring 또는 finalization 전에 documentation readiness 확인이 필요하다.
- Coder handoff의 `documentation_impact.detected`가 true이고 PM이 code diff와 read-back code commit에서
  그 영향을 확인했다. 아직 생성 전인 checkpoint object를 prerequisite로 요구하지 않는다.
- 새 workflow의 reference baseline을 처음 세워야 한다.

단순한 requirement 초안 작성, source 구현, `current-state.md` 갱신에는 사용하지 않는다.

## 빠른 사용 가이드

먼저 동기화 대상을 mode로 선택한다.

```text
/sync-docs derived-docs
docs/requirement/p9/p9-policy.md의 승인 변경을 파생 문서에 동기화해줘.
```

```text
/sync-docs issue-tracker
승인된 P9 scope를 parent/leaf Issue와 dependency에 반영하고 exact target을 read-back해줘.
```

둘 다 필요하면 `derived-docs`를 완료한 뒤 `issue-tracker`를 실행한다. Skill은 영향 후보를 모두
`no-change | update | needs-input`으로 분류하므로, 확인했지만 수정하지 않은 대상도 이유와 함께
결과에 남는다.

## Mode

- `derived-docs`: architecture, ADR, glossary, ERD, index와 testing/validator 문서를 동기화한다.
- `issue-tracker`: GitHub Issue tree의 scope, body, status와 dependency를 동기화하고 read-back한다.

둘 다 필요하면 `derived-docs`를 먼저 완료한다.

## 입력과 baseline

실행 전에 다음 baseline을 기록한다.

- 승인 requirement의 path, heading, revision 또는 commit SHA와 결정 근거. Implementation-impact
  실행이면 대신 source handoff ID, code commit SHA, changed paths와 확인된 impact detail
- 읽기 전용 `current-state.md` section, snapshot SHA와 freshness 판정
- originating Triage·Decision·Review 또는 workflow ID
- 선택한 mode와 후보 문서·Issue locator

초기 reference baseline을 세우는 실행이면 관련 requirement 영역의 후보 전체를 inventory한다. 일반
변경이면 바뀐 behavior와 링크로 닿는 문서·Issue만 조사한다. 완료 기준: 각 후보에 locator와 조사
이유가 있으며 `current-state.md`는 mutation 대상에서 제외된다.

## Impact matrix

다음 표는 후보를 찾는 기준이다. 행에 해당한다고 모든 문서를 무조건 수정하지 말고 실제 중복·참조·
충돌을 확인한다.

| Requirement 변화 | 반드시 확인할 대상 |
|---|---|
| 용어, 상태, 역할, 오류 의미 | `domain-glossary.md`, 참조 requirement/API 문서 |
| module 책임, 공개 seam, event, transaction 원칙 | `architecture.md`, 관련 ADR·backend guide |
| entity·value object의 논리 관계 | `domain-erd.md`, 소유 domain index |
| API, policy 또는 requirement 문서 추가·이동 | `docs/index.md`, `requirement/index.md`, domain index, inbound link |
| 검증 방법·validator contract 변경 | test guide와 해당 tool/Skill reference |
| delivery scope·순서·dependency 변경 | GitHub leaf/parent Issue와 dependency |

ADR은 장기적이고 되돌리기 어려운 구조 결정의 이유가 새로 생길 때만 추가·수정한다. Code에서 자동
추출 가능한 inventory는 문서에 복제하지 않는다.

## Disposition

각 후보를 다음 중 하나로 분류한다.

| 값 | 의미와 필요한 근거 |
|---|---|
| `no-change` | 현재 문장이 새 requirement와 이미 일치하거나 해당 concern을 소유하지 않음 |
| `update` | 승인 의미를 그대로 전파할 정확한 target과 변경문이 있음 |
| `needs-input` | Requirement만으로 architecture·policy·tracker 의미를 하나로 정할 수 없음 |

Disposition 기록에는 `target`, `requirement-locator`, `reason`, `planned-change`, `verification`을
포함한다. 완료 기준: inventory의 모든 후보가 정확히 하나의 disposition을 갖는다.

## 절차

1. **Baseline을 확인한다.** 승인 여부, locator, snapshot freshness와 origin을 read-back한다. 완료
   기준: stale/insufficient snapshot은 mutation 전에 `snapshot-refresh-required`로 반환된다.
2. **영향 대상을 inventory한다.** Impact matrix와 실제 문서 link/Issue tree를 따라 후보를 만든다.
   완료 기준: 관련 후보와 제외 근거가 모두 기록된다.
3. **Disposition을 판정한다.** 모든 후보를 `no-change | update | needs-input`으로 분류한다. 완료 기준:
   불확실한 의미를 `update`로 추측하지 않는다.
4. **결정을 escalation한다.** `needs-input`에는 결정자, 충돌 문장, 영향과 가능한 선택을 기록한다.
   완료 기준: 해당 target은 수정하지 않고 origin의 blocker가 유지된다.
5. **선택한 경우에만 `derived-docs`를 적용한다.** 승인 의미를 소유 문서 한 곳에 기록하고 다른 문서는
   link로 연결한다. 이 mode를 선택하지 않았으면 명시적으로 skip한다. 완료 기준: 변경 문서와 근거
   locator가 1:1로 대응하고 문서 link·heading·용어가 일치한다.
6. **선택한 경우에만 `issue-tracker`를 적용한다.** 아래 tracker 규칙에 따라 필드를 명시적으로
   갱신한다. 이 mode를 선택하지 않았으면 외부 mutation 없이 skip한다. 완료 기준: 각 mutation의
   exact Issue를 다시 읽어 목표값과 일치한다.
7. **결과를 반환한다.** 변경·no-change·needs-input, 검증 결과와 후속 transition을 origin에 연결한다.
   완료 기준: inventory 항목이 결과에서 하나도 누락되지 않는다.

## GitHub delivery tracker 규칙

- Requirement가 소유하는 목표 behavior만 scope·acceptance text에 반영하고 구현 완료를 추정하지 않는다.
- Parent/leaf 경계와 dependency는 기존 Issue tree를 먼저 읽고, 승인된 scope 변화가 요구하는 edge만
  변경한다. 단순 문장 유사성으로 새 dependency를 만들지 않는다.
- Status·label·assignee는 requirement 변화가 해당 lifecycle 의미를 명확히 바꿀 때만 수정한다.
- 이 Skill은 문서 동기화만으로 Issue를 완료·close하지 않는다. Close는 `run-workflow` release
  lifecycle이 소유한다.
- Mutation payload에서 body, state, label, assignee와 관계 field를 provider default에 맡기지 말고
  의도한 값을 명시한다. Mutation 뒤 number, URL, title, body/scope, state와 변경한 관계를 read-back한다.

Read-back이 실패하거나 target이 동시 변경되었으면 성공으로 보고하지 않고 `blocked`와 재시도 조건을
반환한다.

## Commit 경계와 출력

파생 문서 변경은 하나의 requirement 의미를 전파하는 문서-only 논리 단위로 유지한다. Source·test·
migration 변경이나 `current-state.md` 갱신을 섞지 않는다. 실제 commit은 root workflow가 요청한
checkpoint에서만 수행한다.

`derived-docs` 검증은 항상 requirement와 최종 diff의 의미 비교, 변경한 link·path·heading 확인,
repository의 관련 문서/Profile regression test와 `git diff --check`를 포함한다. Architecture·ADR 또는
executable validator contract의 의미가 바뀌면 독립 Reviewer 확인을 요청한다. 단순 glossary·index·
link 전파는 repository가 별도 review를 요구하지 않는 한 PM self-check로 충분하다. Gradle과 code CI는
문서-only 변경의 기본 gate가 아니며, PR에 적용되는 repository CI는 `run-workflow`가 실제 PR head에서
read-back한다. 실행하지 않은 review·test·CI는 통과로 보고하지 않는다.

## 주의할 점

- Requirement보다 Issue나 파생 문서를 상위 source of truth로 취급하지 않는다.
- `no-change` 대상에 형식 변경이나 설명을 억지로 추가하지 않는다.
- `needs-input`을 임의의 architecture·policy 결정으로 해소하지 않는다.
- `issue-tracker`를 선택하지 않은 실행에서는 GitHub mutation을 만들지 않는다.

## 검증

최종 보고는 `baseline / update / no-change / needs-input / external read-back / follow-up`을 분리한다.
모든 영향 대상이 승인 requirement와 일치하거나, 남은 불일치에 결정자·영향·다음 행동이 있어야
완료다. Snapshot 갱신 필요는 `snapshot-refresh-required`로 root `run-workflow`에 반환한다.
