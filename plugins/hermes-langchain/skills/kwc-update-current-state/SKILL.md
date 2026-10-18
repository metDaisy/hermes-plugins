---
name: kwc-update-current-state
description: KWC fresh agent가 profile SOUL·memory 없이 수행하는 "백엔드 src 구현 snapshot을 판정하고 갱신할 때 사용한다."
version: 1.0.1
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [project-management, current-state, snapshot, recovery]
    related_skills: []
---

> 이 Skill의 실행 정책과 helper는 Kanban Workflow Controller가 소유한다. PM Profile copy는 비교 기준일 뿐 실행 의존성이 아니다.

# Current state 갱신

`docs/current-state.md`를 platform-independent committed backend snapshot으로 관리한다. 문서의 구현
주장은 저장소 루트 `src/**`만 다루며, 상세 schema·freshness·marker 계약은
`references/current-state-contract.md`를 따른다. 이 Skill은 source·test·resource·requirement·ADR을
수정하지 않는다.

Helper는 [`../kwc-orchestrator-contract/references/script-usage.md`](../kwc-orchestrator-contract/references/script-usage.md)의 black-box 계약으로 실행한다. 정상 snapshot
판정에서는 helper 구현 소스는 읽지 않는다. 공개 contract에 맞춘 입력으로 inspect/validate를 실행하고,
structured error code가 지목한 입력 field만 수정한다.

## 사용할 때

- Committed `src/**` 변경으로 snapshot이 stale할 때
- Workflow finalization에서 final implementation SHA를 기록할 때
- `insufficient` snapshot을 clean committed backend evidence로 재조사할 때

Requirement-rework 중간의 source 확인, 프런트엔드·CI·Gradle 구성 현황 정리, 일반 문서 동기화에는
사용하지 않는다.

## Freshness 판정

- `fresh`: recorded snapshot SHA 이후 committed `src/**` 변경이 없고 worktree가 clean하다.
- `stale`: `src/**`에 committed drift가 있다.
- `insufficient`: recorded SHA, ancestry, path 범위 또는 clean state를 확인할 수 없다.

`src/**` 외 변경만으로 snapshot이 stale해지지는 않는다. 미커밋 변경은 경로와 무관하게
`insufficient`이며 graph authoring blocker다.

## Read-only preflight

먼저 helper를 실행한다.

```text
python "$HERMES_KWC_PLUGIN_ROOT/skills/kwc-update-current-state/scripts/current_state.py" inspect --repo . --document docs/current-state.md
```

출력은 조사 후보이지 구현 사실이 아니다. Helper는 Git·문서·Kanban을 변경하지 않으며 test 통과나
구현 완료를 추론하지 않는다. 현재 문서 schema만 검사할 때는 `validate-document` subcommand를 쓴다.

## 절차

1. 전체 repository의 clean state, inspection HEAD, 이전 snapshot SHA와 ancestry를 read-back한다.
2. `snapshot..HEAD` changed path를 `src/**`와 excluded path로 분류하고 freshness를 판정한다.
3. `fresh`이면 SHA, ancestry, clean state, covered drift 부재와 optional recovery marker 상태를 반환한다.
4. `stale`이면 changed `src/**`를 main Java, resource/migration, test로 grouping한다. Helper가 제시한 모든
   candidate section을 committed main/test/resource evidence로 재조사하고, 공개 API·module seam·migration
   변화는 sibling path와 관련 test까지 확인한다.
5. `references/current-state-contract.md`의 필수 section을 갱신한다. P1~P12 진행, module 책임,
   HTTP entry point, 핵심 rule, DB 계약, test coverage와 requirement gap을 agent가 source 전체를 다시
   읽지 않아도 현재 backend를 찾고 이해할 수 있는 수준으로 기록한다. 프런트엔드·CI·build tooling은
   구현 현황에 포함하지 않는다.
6. 실행한 검증과 실행하지 않은 검증을 구분한다. 실행하지 않은 test의 통과·coverage를 주장하지 않는다.
7. Snapshot을 갱신할 때 final implementation SHA와 이후 docs-only commit SHA를 구분한다. Optional
   recovery marker의 최초 SHA는 덮어쓰지 않고 finalization에서만 marker를 제거한다.
8. 문서 외 변경이 없는지 확인하고 사용자에게 commit 승인을 받았거나 상위 lifecycle이 commit을
   명시적으로 요구한 경우에만 docs-only commit을 만든다. Commit 뒤 문서 내용과 commit SHA를 read-back한다.

완료 기준: `fresh | stale | insufficient`, inspection/snapshot SHA, ancestry, clean state, covered/excluded
drift, 실행·미실행 검증과 marker 상태가 확인된다. 문서를 갱신했다면 필수 schema가 유효하고 모든 변경
사실이 committed `src/**` evidence에 연결되며 implementation SHA와 docs commit SHA를 각각 read-back했다.

## Pitfalls

- `build.gradle`, `.github/**`, `amaazon-front/**` 변화로 stale 판정을 만들지 않는다.
- `src/**`에 없다는 결론을 한 번의 symbol search로 내리지 않는다. 관련 module과 test/resource 범위를
  확인한 뒤 `확인되지 않았다`로 기록한다.
- Helper의 candidate section을 자동 생성 문안으로 복사하지 않는다.
- Requirement-rework 중간에는 snapshot을 갱신하지 않는다. `build-task-graph`가 필요한 committed source를
  직접 확인하며 최종 갱신은 Summary finalization 뒤 수행한다.
