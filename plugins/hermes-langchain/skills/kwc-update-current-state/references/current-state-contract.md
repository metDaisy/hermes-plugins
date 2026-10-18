# current-state 계약

## 문서 목적과 범위

`docs/current-state.md`는 inspection SHA에 커밋된 **백엔드 `src/` 구현**을 사람이 빠르게 이해하도록 요약한 파생 스냅샷이다. 구현 사실의 기준은 계속 코드이며, 이 문서는 요구사항의 목표 상태나 GitHub·Kanban 상태를 구현 완료로 바꾸지 않는다.

Implementation-covered path는 저장소 루트의 `src/**` 하나다.

- 포함: `src/main/java/**`, `src/main/resources/**`, `src/test/java/**`, `src/test/resources/**`
- 제외: 프런트엔드, `build.gradle`과 Gradle 설정, `.github/**`, `config/**`, `docs/**`, `.hermes/**`, 그 밖의 `src/` 외부 경로
- Flyway와 런타임 설정은 `src/main/resources/**`에 있으므로 포함한다.
- 테스트 코드와 fixture는 `src/test/**`에 있으므로 포함한다.
- 실제 실행한 검증 명령과 결과는 snapshot evidence로 기록할 수 있지만, `src/` 외 자동화 구성 자체를 구현 현황으로 설명하지 않는다.

프런트엔드·CI·Checkstyle/JaCoCo 구성·Profile/Kanban inventory를 별도 구현 section으로 두지 않는다. Requirement는 미구현 차이를 해석하는 보조 locator일 뿐 구현 증거가 아니다.

## 필수 문서 schema

다음 H2 section을 이 순서로 유지한다.

1. `스냅샷`
2. `전체 진행 요약`
3. `백엔드 구조`
4. `확인된 HTTP 진입점`
5. `주요 구현 규칙`
6. `데이터베이스`
7. `테스트·검증`
8. `알려진 차이와 다음 작업`

`진행 중 구현 작업`은 recovery marker가 있을 때만 `스냅샷` 다음에 둘 수 있는 선택 section이다.

### 스냅샷 필수 사실

- 확인일
- Git 브랜치
- 40자리 implementation snapshot SHA
- 이전 snapshot SHA와 ancestry 판정. history rewrite로 ancestor가 아니면 그 사실을 명시한다.
- inspection 범위가 `src/**`임과 미커밋 변경 제외 여부
- 실제 실행한 검증과 실행하지 않은 검증. 실행하지 않은 테스트의 통과·커버리지를 주장하지 않는다.

### 구현 section의 최소 정보

- 진행 요약: 각 P1~P12의 `구현 | 부분 구현 | 스키마만 존재 | 미구현` 상태와 `src/**` 근거
- 백엔드 구조: 현재 모듈, 책임, 공개 module seam과 주요 의존 방향
- HTTP 진입점: method, 최종 path, authorization/보호 조건, 핵심 응답 상태
- 주요 구현 규칙: 상태 전이, validation, transaction/locking, event, authorization, cross-module 규칙
- 데이터베이스: Flyway 순서, 주요 table/constraint/index와 Java model의 중요한 정합성 차이
- 테스트·검증: `src/test/**`가 보장하는 범위와 이번 snapshot에서 실제 실행한 검증 결과
- 알려진 차이: requirement 대비 `src/**`에서 확인되지 않은 behavior. 구현 순서를 새로 결정하지 않는다.

클래스·메서드 전체 목록을 복제하지 않는다. 대신 agent가 관련 모듈과 public behavior를 찾을 수 있는 수준의 entry point, rule, seam을 기록한다. 부정 사실은 해당 범위를 충분히 검색한 경우에만 `확인되지 않았다`로 표현한다.

## Freshness 계약

- `fresh`: snapshot SHA가 유효하고 inspection HEAD의 ancestor이며, `snapshot..HEAD`에서 `src/**` 변경이 없고 worktree가 clean하다.
- `stale`: 위 ancestry가 성립하고 `snapshot..HEAD`에 하나 이상의 committed `src/**` 변경이 있으며 worktree가 clean하다.
- `insufficient`: snapshot/HEAD/ancestry를 확인할 수 없거나 worktree가 dirty하다.

`src/**` 밖의 committed drift만 있으면 snapshot은 `fresh`다. 단, clean gate는 저장소 전체의 staged·unstaged·untracked 파일에 적용한다. Dirty 상태에서는 어떤 파일도 문서에 반영하지 않고 경로만 보고한다.

## Read-only draft helper

```text
python "$HERMES_KWC_PLUGIN_ROOT/skills/kwc-update-current-state/scripts/current_state.py" inspect --repo . --document docs/current-state.md
python "$HERMES_KWC_PLUGIN_ROOT/skills/kwc-update-current-state/scripts/current_state.py" validate-document --document docs/current-state.md
```

`inspect`는 `current-state-inspection-v1` JSON을 stdout에 출력한다. 주요 field는 `freshness`, `inspection_sha`, `snapshot_sha`, `snapshot_is_ancestor`, `worktree_clean`, `dirty_paths`, `covered_changed_paths`, `excluded_changed_paths`, `candidate_sections`, `checklist`, `errors`다. helper는 파일·Git·Kanban을 수정하지 않고 구현 완료나 test 통과를 추론하지 않는다.

## Recovery marker

선택 section `## 진행 중 구현 작업`은 다음 field만 가진다.

- 추적 기준: platform-independent Issue URL 또는 `owner/repo#number`
- 계보: predecessor/successor Issue reference; 없으면 `없음`
- delivery branch
- 최초 기준 SHA
- 최초 implementation snapshot SHA
- requirement 위치: repo-relative path와 heading
- 작업 요약: 한 문장

Board/task/profile ID, card body, assignee, native status는 금지한다. 최초 SHA 둘은 historical anchor이며 base sync 때 변경하지 않는다.

### Start transition

Clean repository와 `fresh` snapshot을 read-back한 뒤 delivery branch에서 marker만 추가해 docs-only checkpoint를 만든다. Commit 뒤 field와 SHA를 다시 읽는다. `update-current-state`는 marker schema만 소유하며 branch·card를 만들지 않는다.

### End transition

Summary finalization에서 final implementation SHA와 clean state를 먼저 freeze한다. 그 SHA의 `src/**`를 조사해 snapshot 전체를 갱신하고 marker section을 제거한 뒤 docs-only commit을 만든다. Implementation SHA와 docs commit SHA를 별도로 read-back한다. 중간 requirement rework나 base sync에서는 marker를 종료하지 않는다.

## Evidence와 완료 기준

변경된 각 서술은 다음 중 하나에 연결한다.

- 해당 inspection SHA의 `src/main/**`
- 해당 inspection SHA의 `src/test/**`
- 해당 SHA에서 실제 실행한 검증 결과

최소 regression evidence는 changed `src/**`를 모두 grouping하고, 영향받은 필수 section마다 관련 main/test/resource를 조사하며, 공개 API·module seam·migration 변화가 있으면 sibling path와 관련 테스트까지 확인하는 것이다. Helper 후보 목록은 조사 시작점이지 사실 증명이 아니다.

완료하려면 필수 schema, scope, SHA, ancestry, clean state, 실행/미실행 검증, marker lifecycle을 확인한다. 문서를 수정했다면 docs-only diff인지 확인하고 commit 뒤 문서 SHA와 Git commit을 read-back한다.
