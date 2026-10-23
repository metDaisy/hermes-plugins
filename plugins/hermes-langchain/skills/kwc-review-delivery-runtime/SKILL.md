---
name: kwc-review-delivery-runtime
description: Use when a fresh controller Agent evaluates one accepted aggregate Review card from immutable staged artifacts.
---

# Controller Review Delivery Runtime

하나의 controller-claimed aggregate Review card를 구현자와 독립적으로 검토한다. 이 Skill이 Reviewer 행동의
정본이며 host Profile의 SOUL, memory, session은 입력이 아니다. Product와 workflow state는 read-only다.
Controller가 Kanban claim, transition, artifact persistence와 completion을 소유한다.

## 1. Immutable review scope

1. `artifact_directory`를 read-only로 유지하고 `manifest.json`, `graph.json`, `activation.json`,
   `dispatch.json`, `predecessors.json`의 identity와 digest를 확인한다.
2. Activated task의 `idempotency_key`와 일치하는 Review card 하나를 선택한다. Card의 `baseline_sha`, accepted
   checkpoints와 현재 controller-pinned reviewed HEAD를 고정하고 `baseline_sha..reviewed_head_sha` 전체 변경을
   검토 범위로 사용한다. Identity, predecessor result 또는 fixed range가 불완전하면 결론을 만들지 않고 block한다.
3. Predecessor의 설명을 결론으로 재사용하지 않는다. Changed production source, tests, configuration, migration,
   `package-info.java`, architecture/ADR와 caller/consumer를 **direct source**에서 독립적으로 확인한다.
4. Codebase Memory와 Semble은 후보 탐색에만 사용하고 source에서 확정한다. External-system behavior는 producer
   public contract, consumer port, adapter와 unavailable/error path를 추적한다. Placeholder adapter나 mock만으로
   acceptance를 승인하지 않는다.
5. Review 중 scope identity 또는 workspace facts가 바뀌거나 필요한 evidence를 읽을 수 없으면 `blocked`로 반환한다.

## 2. Independent review passes

한 fresh delivery Agent 안에서 아래 pass를 순서대로 수행하되, 각 pass는 같은 immutable scope에서 독립적인
question/evidence/conclusion을 작성한다. 이전 pass의 결론을 다음 pass의 근거로 복사하지 않는다.

1. **spec** — obligation, constraint, observable, aggregate acceptance와 preserved behavior가 실제 public 동작으로
   충족되는지 확인한다. Transport binding, DTO, error ownership과 public result까지 추적한다.
2. **maintainability** — repository convention, naming, duplication, test readability와 변경 범위가 장기 유지보수성을
   해치지 않는지 검토한다. 단순 취향이나 speculative abstraction은 finding으로 만들지 않는다.
3. **persistence** — transaction, locking, query/write path, migration, identity, rollback과 concurrency behavior를
   검토한다. Persistence 영향이 없으면 source 근거와 함께 not-applicable로 닫는다.
4. **architecture** — module boundary, allowed dependency, layer owner, event/public contract와 requirement impact closure를
   검토한다. 별도 승인 없는 repository-wide audit로 확장하지 않는다.
5. **evolution-compatibility** — caller, API/serialization, data/schema, error/event compatibility와 향후 변경 seam을
   검토한다. Existing contract 회귀와 새 정책 제안을 구분한다.
6. **security** — changed trust boundary의 credential/PII 노출, authentication 이후 authorization/ownership,
   injection/path/template/deserialization/external-call 경계와 error/log exposure를 검토한다.

각 pass는 exact `path:line`, symbol, test/validator identity 또는 checkpoint SHA를 evidence로 남긴다. 같은
basis + observed fact + impact만 통합하고 축이 다른 위험은 보존한다.

## 3. Independent verification

1. Card scenario와 finding을 재현하는 가장 가까운 test를 `gradle-mcp`로 직접 실행한다.
2. Module seam이면 Modulith 구조 검증, persistence/migration이면 관련 database integration test를 실행한다.
   Aggregate acceptance가 요구하면 backend 전체 `test`를 실행한다.
3. Coder verification을 독립 검증으로 대체하지 않는다. Gradle MCP나 prerequisite가 unavailable이면 pass로
   추정하지 않고 blocker와 재개 조건을 남긴다.
4. 검토 전후 product file, clean workspace와 fixed reviewed HEAD가 유지되어야 한다. Source, test, configuration,
   migration, documentation, card body를 편집하지 않는다.

## 4. Findings and prior lineage

- 정상 구현 결함은 `correction-required`, 승인된 context 부족은 `context-required`, 새 business policy,
  authorization, consistency, error/public contract 결정은 `decision-required`다.
- `prior_findings`의 각 item은 현재 evidence로 정확히 한 번 disposition한다. 해소됐을 때만 `resolved`와 source
  identity를 기록하고, 동일 결함이 남으면 blocking finding의 `continues`로 연결한다.
- 새 blocking finding이 없고 모든 prior finding이 explicit `resolved`일 때만 `approved`다. Blocking finding이
  하나라도 있으면 `changes-required`다.
- Input/workspace/tool prerequisite 문제는 incomplete terminal verdict로 꾸미지 않고 `blocked` delivery로 반환한다.
- Finding은 basis, observed fact, non-empty exact evidence와 concrete impact를 포함한다. Preference, 질문,
  측정되지 않은 가능성은 blocking finding이 아니다.

## 5. Output

호출에 제공된 delivery schema와 exact native task/run/dispatch identity를 만족하는 JSON object 하나만 반환한다.
Completed result artifact에는 canonical `aggregate-review-result-v2` 의미를 보존한 review verdict, reviewed
checkpoints, `prior_findings`와 findings를 담는다. Axis progress, raw diff/tool output, prompt, credential 또는 hidden
reasoning을 canonical result에 섞지 않는다. Commit, push, merge, reset, stash, clean, Kanban mutation, transition
선택, Summary promotion 또는 implementation 수정은 수행하지 않는다. Controller가 persisted artifact ID와 native
completion을 소유한다.
