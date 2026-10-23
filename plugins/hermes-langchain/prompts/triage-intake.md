Triage 단계: intake

Issue, 지정된 요구사항 문서, `docs/current-state.md`, 현재 Git HEAD만 읽고 목표 동작과 planning identity를 정리한다. 아직 production source를 읽거나 구현 상태를 판정하지 않는다.

`kwc-triage-intake-authoring-v1` JSON을 assignment의 repo-relative `artifact_file`(`.temp/kwc-authoring/<issue>/generation-<generation>/<board-namespace>/intake-authoring.json`)에 직접 작성하고 `kwc_advance_triage.artifact_file`에도 그 값을 그대로 전달한다. `<board-namespace>`는 KWC가 board slug와 digest로 생성하므로 Agent가 계산하거나 다른 board의 generation directory를 재사용하지 않는다. 다른 경로를 선택하거나 Profile cache에 쓴 뒤 복사하지 않는다.

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

각 ID는 artifact 안에서 유일하게 작성한다. `change_requirements`는 서로 독립적으로 구현·acceptance·verification할 수 있는 outcome마다 한 항목을 사용한다. Presentation binding, application orchestration, aggregate invariant, cross-module public contract/error ownership, public behavior preservation처럼 owner나 verification seam이 다른 결과를 하나의 긴 outcome으로 합치지 않는다. 특히 Issue 하나의 scope에 HTTP binding과 DTO boundary가 함께 있더라도 transport 입력 형식 책임과 application command/result 경계는 서로 다른 change requirement로 작성한다. Controller binding을 조사한다는 이유로 HTTP request DTO와 transport-neutral application command/result 분리 요구를 같은 ID에 묶거나 생략하지 않는다. `preserved_observables`도 ownership/cookie, item quantity·limit, merge atomicity, public API status·error, cross-module error origin처럼 실패 원인과 회귀 검증이 다른 사용자 관찰 동작마다 한 항목을 사용한다. Cross-module capability가 정상 조회 fact/projection과 error ownership을 함께 제공하면 둘을 서로 다른 preserved observable로 작성한다. Change requirement 또는 그 source claim이 가격·판매 상태·재고·구매 제한 중 둘 이상을 이름 붙이면, 같은 fact들을 모두 명시하는 전용 정상 조회 preserved observable이 반드시 있어야 한다. 정상 응답 fact/projection 보존을 Cart invariant나 error code 보존 문장에 흡수하지 않는다. 한 acceptance가 실패했을 때 어느 requirement/observable이 깨졌는지 ID만으로 식별할 수 있는 granularity를 유지한다. 변경 요구사항은 하나 이상 필요하며, 요구사항·보존 observable·quality gate·finalization requirement·out-of-scope·조사 질문의 근거를 source locator와 claim으로 보존한다.
`change_requirements[].outcome`과 그 source claim에는 `사용하거나`, `선택하거나`, `and/or`, `use either`처럼 구현 선택지를 남기지 않는다. Intake에서는 아직 source를 읽지 않았더라도 Issue가 이미 요구한 최종 계약을 하나의 결과로 적고, 구현 선택지가 둘 이상 남는 경우에만 analysis의 실제 `open_decisions` 후보로 승격한다. 공개 producer seam이 Issue·요구사항 문서에 고정되어 있으면 producer-owned contract를 사용한다는 단일 결과를 작성하며 대체 consumer result를 함께 쓰지 않는다. 각 change requirement는 같은 책임 어휘를 가진 investigation target으로 source 확인 경로를 가져야 한다. HTTP DTO boundary 요구에는 request DTO, response DTO, application service signature를 각각 확인하는 target 질문을 포함한다. 특히 repository·persistence·transaction·locking 요구는 repository 구현과 가장 가까운 repository test를 묻는 독립 target을 반드시 둔다. 이 규칙을 어기면 source 조사 전에 intake가 거부되므로 다음 단계로 진행하지 않는다.

작성이 끝나면 `kwc_advance_triage`에 현재 assignment_id와 artifact_file을 제출한다. KWC가 검증하여 다음 단계 assignment를 반환하기 전에는 다음 단계로 진행하지 않는다.
