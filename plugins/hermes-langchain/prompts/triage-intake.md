Triage 단계: intake

Issue, 지정된 요구사항 문서, `docs/current-state.md`, 현재 Git HEAD만 읽고 목표 동작과 planning identity를 정리한다. 아직 production source를 읽거나 구현 상태를 판정하지 않는다.

`kwc-triage-intake-authoring-v1` JSON을 assignment의 repo-relative `artifact_file`(`.temp/kwc-authoring/<issue>/generation-<generation>/intake-authoring.json`)에 직접 작성하고 `kwc_advance_triage.artifact_file`에도 그 값을 그대로 전달한다. 다른 경로를 선택하거나 Profile cache에 쓴 뒤 복사하지 않는다.

필드 계약은 다음과 같다. 아래에 적힌 필드만 사용한다.

- top-level: `schema`, `identity`, `intake`
- `identity`: `issue_number`, `title`, `issue_url`, `planning_sha`, `current_state_sha`, `requirement_locators`, `current_state_locators`
  - `planning_sha`는 현재 Git HEAD, `current_state_sha`는 `docs/current-state.md`가 기록한 snapshot SHA다.
  - locator 배열에는 실제로 읽은 repo-relative 문서 경로를 넣는다.
- `intake`: `intent`, `investigation_targets`
- `intent`: `goal`, `change_requirements`, `preserved_observables`, `quality_gates`, `finalization_requirements`, `out_of_scope`
  - `change_requirements[]`: `id`, `outcome`, `source`
  - `preserved_observables[]`: `id`, `behavior`, `source`
  - `quality_gates[]`: `id`, `outcome`, `source`
  - `finalization_requirements[]`: `id`, `outcome`, `source`
  - 모든 `source`: `locator`, `claim`
  - `out_of_scope`: 비어 있지 않은 문자열 배열
- `investigation_targets[]`: `id`, `locator`, `question`, `source`

각 ID는 artifact 안에서 유일하게 작성한다. `change_requirements`는 서로 독립적으로 구현·acceptance·verification할 수 있는 outcome마다 한 항목을 사용한다. Presentation binding, application orchestration, aggregate invariant, cross-module public contract/error ownership, public behavior preservation처럼 owner나 verification seam이 다른 결과를 하나의 긴 outcome으로 합치지 않는다. `preserved_observables`도 ownership/cookie, item quantity·limit, merge atomicity, public API status·error, cross-module error origin처럼 실패 원인과 회귀 검증이 다른 사용자 관찰 동작마다 한 항목을 사용한다. 한 acceptance가 실패했을 때 어느 requirement/observable이 깨졌는지 ID만으로 식별할 수 있는 granularity를 유지한다. 변경 요구사항은 하나 이상 필요하며, 요구사항·보존 observable·quality gate·finalization requirement·out-of-scope·조사 질문의 근거를 source locator와 claim으로 보존한다.

작성이 끝나면 `kwc_advance_triage`에 현재 assignment_id와 artifact_file을 제출한다. KWC가 검증하여 다음 단계 assignment를 반환하기 전에는 다음 단계로 진행하지 않는다.
