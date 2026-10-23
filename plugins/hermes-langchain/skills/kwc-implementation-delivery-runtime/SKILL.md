---
name: kwc-implementation-delivery-runtime
description: Use when a fresh controller Agent executes one accepted Implementation card from immutable staged artifacts.
---

# Controller Implementation Delivery Runtime

하나의 controller-claimed Implementation card만 구현한다. 이 Skill이 Coder 행동의 정본이며 host Profile의
SOUL, memory, session은 입력이 아니다. Controller가 Kanban claim, transition, artifact persistence와 completion을
소유한다.

## 1. Immutable input admission

1. `artifact_directory`를 read-only로 유지하고 `manifest.json`, `graph.json`, `activation.json`,
   `dispatch.json`, `predecessors.json`의 identity와 digest를 확인한다.
2. Activated task의 `idempotency_key`와 일치하는 graph card 하나만 선택하고 body의 closed
   `backend-implementation-card-v6`를 실행 계약으로 사용한다. Requirement나 Triage를 다시 기획하지 않는다.
3. Repository 규칙, 실제 source/test/configuration/migration과 card를 대조한다. Card에 없는 business policy,
   authorization, consistency, public API 또는 error 의미가 필요하면 발명하지 않고 `blocked`로 반환한다.
4. 각 obligation의 given/operation/expected/observables, 각 constraint의 source, verification의 covers/level/
   selectors가 실행 가능한지 확인한다. 모든 observable의 `identity`, `values`, `expected` leaf를 실제 fixture와
   production mapping에 materialize한다. Production mapping과 기대값이 충돌하면
   `CARD_EXPECTED_VALUE_CONTRADICTION`으로 파일 수정 전에 block한다.
5. 각 scenario를 actor, operation, input boundary, result/error, side effect로 분해한 **assertion coverage checklist**를
   만든다. 지정 selector가 scenario operation을 소유하지 않거나 test level/scope에서 실행할 수 없으면
   `CARD_SCENARIO_SELECTOR_CONTRADICTION`으로 파일 수정 전에 block한다.
6. External-system obligation은 consumer port 이름이나 mock으로 admission하지 않는다. producer public contract,
   adapter execution path와 unavailable/error semantics를 직접 확인한다. 항상 empty/default/unsupported를 반환하는
   placeholder adapter는 완료 근거가 아니다.

완료 기준: card의 모든 causal proof와 source-backed constraint가 실제 repository seam과 selector에 연결된다.
하나라도 모순·누락이면 product file을 수정하지 않고 typed blocker를 반환한다.

## 2. Minimal implementation and proof

1. Existing architecture와 owner convention을 먼저 읽고 card 범위 안에서 가장 작은 production/test delta를 만든다.
   요청되지 않은 refactor, optimization, abstraction, documentation 변경은 하지 않는다.
2. Test-first loop를 사용한다. 기존 assertion을 약화·삭제하거나 validator를 우회하지 않는다.
3. Exact observable은 exact assertion으로 증명한다.
   - 여러 error/result code는 generic conflict 하나로 대체하지 않는다.
   - named boundary마다 앞선 guard를 중립화하고 해당 boundary의 exact code/result를 검증한다.
   - complete/unchanged state는 각 entity identity와 value를 전후 비교한다.
   - concurrent/lock scenario는 순차 호출로 대체하지 않는다.
   - HTTP status/body/non-exposure는 실제 transport mapping에서 검증한다.
   - log 비노출은 public execution 중 capture한 output으로 검증한다.
4. 외부 producer/adapter가 없거나 public contract가 card와 충돌하면 partial implementation을 완료로 포장하지 않는다.
5. 모든 Gradle 작업은 `gradle-mcp`로만 실행한다. Card의 canonical `Class#method` selector를 보존하고 실행 filter를
   만들 때만 마지막 `#`를 `.`으로 변환한다. Selector를 생략하거나 넓혀 통과시키지 않는다.
6. Card의 focused verification을 모두 실행하고 backend 전체 `test`를 실행한다. Java production/test path가
   바뀌면 `checkstyleMain`과 `checkstyleTest`도 실행한다. Module seam은 Modulith 구조 검증, persistence/migration은
   관련 database integration test를 추가한다.
7. Gate 실패 시 범위 안 root cause를 수정하고 affected focused gate부터 다시 실행한다. Gradle MCP나 필수
   environment가 unavailable이면 실행하지 않은 결과를 pass로 쓰지 않는다.

## 3. Completion boundary

Completed delivery는 다음이 모두 참일 때만 작성한다.

- 모든 obligation과 constraint가 card의 verification으로 정확히 cover되고 assertion coverage checklist가 닫혔다.
- Focused, full backend와 필요한 quality/Modulith/database gate가 현재 실행에서 통과했다.
- Actual changed path는 card scope 또는 필수 propagation으로 모두 설명되며 문서 path가 없다.
- External fact를 요구하는 behavior는 실제 producer public contract와 adapter execution path로 증명됐다.
- 필수 scenario를 `residual_risks`, `out_of_scope` 또는 설명으로 이동해 pass 처리하지 않았다.
- 실행하지 않은 검증, credential, raw output, prompt 또는 hidden reasoning을 결과에 기록하지 않았다.

Card/source contradiction, stale input, unsupported external contract, unavailable verification 또는 설명할 수 없는 delta가
남으면 `status: blocked`로 반환하고 관찰 사실, 영향, 마지막 성공 gate와 필요한 조치만 sanitized payload에 남긴다.
Commit, reset, restore, checkout, stash, clean, push, merge, Kanban mutation 또는 transition 선택을 수행하지 않는다.

## 4. Output

호출에 제공된 delivery schema와 exact native task/run/dispatch identity를 만족하는 JSON object 하나만 반환한다.
Completed result artifact에는 구현 obligation, exact changed paths, focused/full/quality verification과 남은 risk를
재현 가능한 canonical payload로 담는다. Controller가 persisted artifact ID와 native completion을 소유한다.
