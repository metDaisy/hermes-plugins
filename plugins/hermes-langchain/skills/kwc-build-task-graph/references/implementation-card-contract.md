# PM backend Impl card authoring contract v5

이 문서는 Project Manager가 `backend-implementation-card-v5` body를 작성하는 의미와 책임을 소유한다.
결정론적 validation은 `scripts/build_task_graph.py`, canonical example은
`tests/fixtures/valid-new-delivery.json`이 소유한다. Coder의 실행·handoff 규칙은 Coder
`run-impl-card/references/implementation-card-contract.md`가 소유한다.

## 목적

Impl card는 승인된 frozen Triage에서 **한 implementation slice를 실행 가능한 입력으로 materialize**한다.
Triage의 전체 분석을 다시 복제하거나 runner/CWD/success oracle처럼 workflow가 이미 고정한 값을 반복하지 않는다.
Card body에는 Coder가 구현 전에 반드시 알아야 하고 실행 뒤에도 provenance로 남아야 하는 durable input만 저장한다.

## Required body

Body는 duplicate key를 허용하지 않는 closed JSON object이며 top-level field는 다음 아홉 개뿐이다.

| Field | 의미 |
|---|---|
| `schema` | 정확히 `backend-implementation-card-v5` |
| `card_type` | 정확히 `implementation` |
| `issue` | leaf Issue의 `number`, canonical HTTPS `url` |
| `triage_digest` | 입력으로 사용한 exact frozen Triage의 SHA-256 digest |
| `slice` | 이 card가 완성할 한 구현 단위와 graph behavior/gap 연결 |
| `implementation_contract` | Coder가 requirement를 재해석하지 않도록 materialize한 제품·구조 계약 |
| `context` | 확인된 현재 상태, 조사 시작점, source-backed 제약 |
| `acceptance` | 결과 중심 완료 조건과 requirement/observable 연결 |
| `verification` | acceptance를 증명할 test level, Gradle task, selector, scenario |

### `slice`

Closed field: `id`, `title`, `outcome`, `behavior_ids`, `gap_ids`, `scope`, `out_of_scope`.

- `behavior_ids`는 graph에서 이 Impl에 배정한 planned behavior 집합과 정확히 같다.
- Impl body-only fast path에는 별도 graph behavior wrapper가 없으므로 frozen Triage acceptance의 ordered
  `observable_ids`를 이 card의 `behavior_ids`로 사용한다. 임의의 `BEHAVIOR-*` ID를 새로 만들지 않는다.
- `gap_ids`는 frozen Triage에서 이 slice가 닫는 gap ID다.
- `scope`와 `out_of_scope`는 수정 파일 allowlist가 아니라 제품·기술 경계다.
- 한 card는 해당 behavior의 production 변경과 그 변경을 결정적으로 증명하는 test를 함께 포함하는 작은
  vertical commit이어야 한다. 더 작은 card가 독립적으로 검증·commit될 수 없으면 나누지 않는다.
- 다른 planned Impl이 만들 production symbol, public seam 또는 새 test selector가 있어야 실행 가능한 card는
  독립 card가 아니다. 두 slice를 합치거나 실제 semantic parent를 둔다. `execution_order`와 priority는
  dependency를 대신하지 않는다.
- Impl→Impl parent edge를 두려면 batch의 `dependency_audit.edges[]`에 `from_ordinal`, `to_ordinal`, parent가
  만드는 exact `required_artifact`, child의 `consumer_entry_point`, `why_baseline_cannot_satisfy`를 기록한다.
  공유 파일·충돌 회피·선호 실행 순서,
  import/package 이동 순서 또는 Triage의 기존 `depends_on` 자체는 proof가 아니다. Child가 baseline source만으로
  구현·검증 가능하면 parent edge를 제거하고 Triage sibling으로 둔다.
- 다른 Impl의 acceptance를 나중에 모아서 검증하는 test-only Impl을 만들지 않는다. Test-only Impl은 planning
  baseline에서 production behavior가 이미 완성되어 있고 verification coverage 자체만 gap이며, 다른 planned
  Impl의 변경이나 새 selector에 의존하지 않을 때만 허용한다.

### `implementation_contract`

Closed field는 `actor_and_authorization`, `api`, `state_transition`, `error_semantics`,
`persistence_and_transaction`, `events`, `preserved_behaviors`다.

- 앞의 여섯 dimension은 `{applicability, rules}`를 가지며 applicability는 정확히
  `applicable | not-applicable`이다.
- 모든 dimension은 비어 있지 않은 `rules`를 가진다. `not-applicable`도 왜 해당 slice에서 계약 변경이
  없는지 구체적으로 기록한다.
- `preserved_behaviors`는 이번 slice가 변경하지 않아야 할 기존 caller별 동작과 공개 계약을 기록한다.
- 규칙은 actor/authorization, API request·response, 상태 전이, typed error와 missing semantics,
  transaction·locking·persistence, event 발행·소비를 Coder가 requirement 문서를 다시 읽지 않고 구현할
  정도로 구체화한다.
- 모든 caller를 하나의 generic 규칙으로 합치지 않는다. 동일한 absence라도 tolerant query와 invariant
  판정 command처럼 의미가 다르면 각각 보존한다.

### `context`

- `current_state[]`: `fact`와 repository `source {kind, path, heading}`.
- `entry_points[]`: `path`, `symbol`, `reason`. 조사 시작점이지 caller/consumer 조사 제한이 아니다.
- `constraints[]`: `rule`과 repository `source {kind, path, heading}`.
- `current_state[]`와 `constraints[]`의 `source.kind`는 정확히 `repository`다. Source가 production code,
  test, requirement 또는 guide인지에 따라 다른 kind를 추측하거나 validator 후보를 반복 탐색하지 않는다.

`entry_points[].symbol`의 `unknown`, `unresolved`, `todo`, `tbd` placeholder는 허용하지 않는다. Triage seam이
path만 확인해 `unknown`으로 prefill되면 같은 path의 실제 repository symbol을 확인해 completion에서 대체한다.
각 acceptance가 실행 중 수정·생성·이동·삭제할 exact artifact closure도 `entry_points`에 남긴다. 현재 seam만
나열하지 말고 생성·이동·삭제할 exact path와 symbol을 함께 적는다. 새 DTO/command/result, 새 aggregate behavior,
port의 planned target, 제거할 duplicate type과 대체 producer type을 일반적인 책임 문장으로 대신하지 않는다.
`reason`은 artifact가 충족하는 acceptance ID와 create/move/remove/modify 역할을 명시한다.
HTTP DTO 분리는 request와 response를 별개로 검증한다. 기존 application request/response DTO와 planned
presentation request/response DTO, planned application command/result가 모두 closure에 있어야 한다.
Planned application output은 `application/result` 하나로 고정하며 같은 acceptance에 별도
`application/response` target을 병행하지 않는다. 기존 application response DTO는 move/remove source이고
planned application result가 transport-neutral output target이다.

현재 사실이나 제약에 source가 없으면 추측해서 채우지 않고 Triage/decision으로 되돌린다. `constraints`는
repository 구조 근거를 소유하고, `implementation_contract`는 frozen Triage에서 materialize한 제품 의미를
소유한다. `entry_points.symbol`에는 파일명이 아니라 `Type#method` 같은 실제 symbol을 기록한다.

### `acceptance`

각 항목은 `id`, `criterion`, `requirement_ids`, `observable_ids`를 가진다. ID는 고유하며 모든 acceptance는
하나 이상의 verification에 연결된다. `criterion`은 구현 방법이 아니라 관찰 가능한 결과다.

### `verification`

각 항목은 `id`, `acceptance_ids`, `test_level`, `task`, `tests`, `scenarios`를 가진다.

- `test_level`: `unit | slice | repository | integration | modulith`.
- `task`: `gradle-mcp`로 실행할 Gradle task.
- `tests`: actual FQCN 또는 `FQCN#method`. 새 test는 card가 만들 정확한 selector여야 한다.
- `scenarios`: pass 여부를 handoff에서 그대로 read-back할 필수 관찰 시나리오.

Runner는 `gradle-mcp`, CWD는 repository root, success oracle은 `BUILD SUCCESSFUL`, full backend gate는
root `test`로 workflow에 고정되어 있으므로 body에 반복하지 않는다.

## Invariants

- `issue`, `triage_digest`, `slice.behavior_ids`는 graph wrapper와 정확히 일치한다.
- 종류별 ID와 list 값은 중복되지 않는다.
- 모든 acceptance ID는 verification으로 완전히 cover되고 알 수 없는 ID를 참조하지 않는다.
- 설명문은 한국어로 작성하되 ID, path, symbol, enum과 도구 이름은 literal을 보존한다. 기술 identifier가
  많다는 이유로 구체 계약을 generic 한국어 문장으로 희석하지 않는다.
- Baseline/planning SHA, assignee, status, dependency, workspace, run, result는 native Kanban/PM admission이
  소유하므로 body에 넣지 않는다.
- Credential, raw tool output, prompt, coordinator session 문구와 hidden reasoning을 기록하지 않는다.
- `goal`, `effective_behavior`, top-level `scope`, `implementation_context`, `contracts`,
  `acceptance_criteria`, `focused_verification`, `full_backend_verification`, `traceability`는 v3 legacy이며
  v5에서 허용하지 않는다. 구현 전 card에는 run/result/proof placeholder, gate ledger 또는 `unlazy` evidence를
  추가하지 않는다.

## Authoring procedure

1. Frozen Triage digest와 선택한 implementation slice를 확정한다.
2. 모든 planned behavior에 대해 production seam과 decisive test의 **단일 owner card**를 정한다. Card별로
   planning baseline에 이미 존재하는 symbol/test와 다른 card가 새로 만들 symbol/test를 구분하고, 후자에
   의존하면 merge 또는 semantic parent로 수정한다. 동일 acceptance/test를 여러 Impl이 소유하거나 여러
   behavior의 회귀 검증만 모은 catch-all card를 만들지 않는다.
   각 semantic parent 후보는 `dependency-audit.json`에 parent가 만드는 exact artifact와 child가 baseline만으로
   완료될 수 없는 이유를 적어 반증한다. 이 proof가 비면 edge를 만들지 않는다.
3. `template`로 `.temp` 아래 scaffold를 생성한다.
4. 승인 근거로 `implementation_contract`를 materialize하고 직접 확인한 repository 사실로 context를 작성한다.
   Generic 반복 문장, file-name symbol, 의미 없는 성공·실패·경계 scenario는 허용하지 않는다.
5. 각 card를 **그 card의 semantic parents만 완료된 planning baseline**에서 실행한다고 가정하고 body를 다시
   읽는다. 필요한 public method, DTO, exception, test fixture와 selector가 존재하거나 이 card scope 안에서
   생성되는지 확인한다. 생성 순서에만 기대는 숨은 prerequisite가 있으면 draft를 확정하지 않는다.
6. `validate`와 `validate-graph --phase draft`를 통과시킨다.
7. Native task 생성 후 exact body, assignee, status, parent와 admission comment를 read-back한다.

중간 분석 body를 native card에 저장하지 않는다. Validator 통과는 shape 증거일 뿐이므로 PM은 slice가 다른
문서의 재해석 없이 실행 가능하고 작은 commit으로 끝나는지도 별도로 검수한다.
