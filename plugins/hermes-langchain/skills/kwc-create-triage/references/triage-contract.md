# create-triage JSON 계약 v7

Triage Card는 **Issue 구현에 필요한 사실과 경계를 고정하는 구현 설계 계약**이다. 조사 과정 전체를
보존하는 문서가 아니며, frozen body는 Impl Card·Review Card 생성의 정본 입력이다.

`intake`는 조기 가시성을 위해 native card에 저장하고, `analysis`는 `.temp`의 검증된 중간 artifact로만
유지한다. Native card의 다음 body mutation은 `frozen` 정본 저장 한 번이다. "이번 단계/세션/Triage에서는
downstream card를 만들지 않는다" 같은 일시적인 coordinator 실행 범위는 제품·구현 계약이 아니므로
frozen body에서 `EPHEMERAL_COORDINATOR_CONTEXT`로 거부한다.

Kanban task의 `body`는 UTF-8 strict JSON object 하나다. Duplicate key와 non-finite number는 허용하지
않는다. Native Kanban은 task ID, status, assignee, link, comment, event와 run을 소유한다.

## Phase

```text
intake → analysis → frozen
```

- `intake`: Issue 계약과 조사 경계가 사용자에게 보이는 단계
- `analysis`: confirmed gap, slice, verification과 실제 Decision이 완성된 단계
- `frozen`: digest와 downstream handoff가 고정된 단계

모든 phase는 같은 native `triage` card를 사용한다.

## 전체 구조

```json
{
  "schema": "triage-v7",
  "phase": "intake",
  "frozen_digest": null,
  "issue": {"number": 151, "title": "tracker 원본 제목", "url": "https://.../issues/151"},
  "baseline": {
    "planning_sha": "<40 lowercase hex>",
    "requirement_locators": ["docs/requirement/...#..."],
    "current_state": {
      "snapshot_sha": "<40 lowercase hex>",
      "freshness": "fresh",
      "usage": "read-only",
      "locators": ["docs/current-state.md#..."]
    }
  },
  "intent": {
    "goal": "...",
    "change_requirements": [],
    "preserved_observables": [],
    "quality_gates": [],
    "finalization_requirements": [],
    "out_of_scope": []
  },
  "investigation_targets": [],
  "confirmed_gaps": [],
  "target_dispositions": [],
  "implementation_slices": [],
  "verification_matrix": [],
  "contract_changes": [],
  "document_impacts": [],
  "open_decisions": [],
  "handoff": {"graph_allowed": false, "blocker": "analysis-incomplete"}
}
```

## Source shape

모든 목표·판정·근거는 다음 shape를 사용한다.

```json
{"locator": "path#heading-or-symbol", "claim": "이 locator가 직접 지지하는 사실"}
```

`required_state`, acceptance 또는 preserved observable은 Issue, requirement, architecture/ADR 또는 승인된
Decision으로 추적돼야 한다. Source가 없는 목표 동작은 semantic audit에서
`UNSOURCED_TARGET_BEHAVIOR`로 거부한다.

## Intent

Issue 문장을 성격에 따라 분리한다.

```json
{
  "change_requirements": [
    {"id": "CHANGE-001", "outcome": "달라져야 하는 결과", "source": {"locator": "...", "claim": "..."}}
  ],
  "preserved_observables": [
    {"id": "OBS-001", "behavior": "유지할 외부 동작", "source": {"locator": "...", "claim": "..."}}
  ],
  "quality_gates": [
    {"id": "GATE-001", "outcome": "검증 종류", "source": {"locator": "...", "claim": "..."}}
  ],
  "finalization_requirements": [
    {"id": "FINAL-001", "outcome": "종료 조건", "source": {"locator": "...", "claim": "..."}}
  ]
}
```

Quality gate와 finalization item을 억지 implementation finding으로 바꾸지 않는다.

## Investigation target

Issue가 직접 지목했거나 change requirement 판정에 꼭 필요한 시작점만 기록한다.

```json
{
  "id": "TARGET-001",
  "locator": "src/main/.../CartService.java#changeQuantity",
  "question": "aggregate 내부 상태를 직접 변경하는가?",
  "source": {"locator": "Issue URL#heading", "claim": "Issue가 이 symbol을 조사 대상으로 지정한다."}
}
```

Analysis phase에서는 모든 target에 disposition 하나가 필요하다.

```json
{
  "target_id": "TARGET-001",
  "disposition": "gap | preserve | compliant | out-of-scope",
  "finding_ids": ["GAP-001"],
  "rationale": "직접 근거에 따른 판정"
}
```

`gap`만 non-empty `finding_ids`를 요구한다. `compliant`는 직접 근거가 충분할 때만 사용한다.

## Confirmed gap

구현 작업이 필요한 차이만 기록한다.

```json
{
  "id": "GAP-001",
  "subject": "CartService aggregate 우회",
  "current_state": "현재 직접 확인한 사실",
  "required_state": "source가 요구하는 상태",
  "evidence": [{"locator": "src/...#symbol", "claim": "현재 사실"}],
  "rules": [{"locator": "docs/...#rule", "claim": "적용 규칙"}],
  "requirement_ids": ["CHANGE-001"],
  "affected_observable_ids": ["OBS-001"]
}
```

모든 change requirement는 하나 이상의 gap에 연결된다. 정상 구현을 inventory로 만들거나 미확정 질문을
gap처럼 표현하지 않는다.

preserved observable의 현재 동작 자체를 gap으로 분류하지 않는다. 일반 architecture 또는 계층 규칙이
더 깔끔한 대안을 암시하더라도 Issue·requirement·승인 Decision이 현재 observable 보존을 요구하면 그 동작은
`preserve` 또는 `compliant` disposition이다. 변경이 필요한 주변 책임만 별도 gap으로 기록한다. 특히 HTTP
transport의 입력 종류를 구분해 path variable binding 규칙을 cookie parsing에 그대로 적용하지 않는다.
Cookie의 malformed/null-tolerant 의미가 preserved observable이면 typed path binding을 근거로 제거하지 않는다.

## Implementation slice

Frozen slice는 그대로 self-contained Impl Card를 만들 수 있어야 한다.

```json
{
  "id": "SLICE-001",
  "title": "Cart aggregate behavior 위임",
  "outcome": "독립 검증 가능한 완료 결과",
  "gap_ids": ["GAP-001"],
  "scope": ["..."],
  "out_of_scope": ["..."],
  "acceptance": [
    {
      "id": "ACCEPT-001",
      "criterion": "검증 가능한 완료 조건",
      "requirement_ids": ["CHANGE-001"],
      "observable_ids": ["OBS-001"],
      "verification_ids": ["VERIFY-001"]
    }
  ],
  "depends_on": []
}
```

모든 gap은 slice에 연결되고 모든 acceptance는 requirement와 verification을 가진다. Dependency는 실제
slice ID만 사용한다.

Frozen requirement, `required_state`, slice outcome·scope·acceptance에는 `A 또는 B`, `use A or B`처럼
서로 다른 구현 결과를 남기지 않는다. Source만으로 선택할 수 없는 공개 contract 결과라면 analysis의
`open_decisions`와 `decision-required` blocker로 표현하며 freeze하지 않는다.

## Verification matrix

Preserved observable마다 정확히 한 row를 둔다.

```json
{
  "id": "VERIFY-001",
  "observable_id": "OBS-001",
  "current_coverage": "covered | partial | missing",
  "evidence": [{"locator": "src/test/...#scenario", "claim": "실제 assertion 범위"}],
  "production_seam": {
    "locator": "src/main/...#caller-or-owner-symbol",
    "claim": "이 production caller 또는 behavior owner가 observable을 실제 실행한다"
  },
  "coverage_gap": null,
  "required_verification": ["구현 후 확인할 scenario"]
}
```

- `covered`: 직접 test scenario evidence가 있고 `coverage_gap`은 null
- `partial`: 기존 test evidence와 non-empty `coverage_gap`
- `missing`: evidence는 빈 배열이고 non-empty `coverage_gap`

모든 row의 `production_seam`은 현재 committed `src/main/**`의 **실제 caller 또는 behavior owner**를
가리킨다. Requirement, `current-state.md`, test, DTO, cookie/provider factory처럼 결과를 만들 수 있지만
해당 observable을 호출하지 않는 utility는 production seam을 대신하지 못한다. Observable이 여러 trigger나
module owner를 포함하면 owner별 row가 되도록 observable 자체를 나눈다. Requirement가 다른 module의
caller에게 맡긴 behavior인데 현재 target Issue에 production seam이 없으면 이를 local coverage gap이나
test-only slice로 만들지 않는다. Existing requirement가 owner를 결정하면 해당 Issue의 out-of-scope 또는
cross-domain follow-up으로 남기고, 둘 이상의 준수 가능한 owner/contract가 남을 때만 Decision으로 올린다.

Test class 존재, production source 또는 mock interaction만으로 observable 전체를 `covered`로 만들 수 없다.

## Conditional contract change

이번 Issue가 실제 cross-domain seam을 변경할 때만 작성한다. 관련 없으면 빈 배열이 정상이다.

```json
{
  "id": "CONTRACT-001",
  "consumer": "cart",
  "producer": "offer",
  "change": "이번 slice가 요구하는 최소 공개 seam",
  "evidence": [{"locator": "...", "claim": "..."}],
  "verification_ids": ["VERIFY-002"]
}
```

모든 capability에 data/error/consistency 표를 강제하지 않는다. Transaction annotation이나 repository
구현을 공개 consistency contract로 승격하지 않는다.

동일 resource의 absence도 use case에 따라 의미가 다를 수 있다. 조회 화면·batch enrichment가 missing item을
unavailable DTO나 누락 map entry로 보존하는 계약이면 absence는 정상 결과다. 반대로 추가·수량 변경·병합처럼
producer fact 없이는 invariant를 판정할 수 없는 command 경로에서는 absence가 producer-owned error일 수 있다.
조회 use case와 command use case를 하나의 missing semantics로 합치지 않는다. `contract_changes`와 slice는
각 caller가 요구하는 tolerant query와 required lookup을 분리해 기록한다.

## Document impact

실제로 검토한 관련 문서만 기록한다.

```json
{
  "document": "docs/architecture.md",
  "disposition": "update | no-change | not-applicable | blocked",
  "evidence": [{"locator": "docs/architecture.md#...", "claim": "..."}],
  "action": null
}
```

`update`만 non-empty `action`을 가진다. `current-state.md`는 baseline input이며 Triage에서 수정하지 않는다.

## Decision

모든 고정 제약 적용 뒤 둘 이상의 준수 가능한 public/product 결과가 남을 때만 작성한다.

```json
{
  "id": "DECISION-001",
  "question": "남은 실제 선택 질문",
  "why_unresolved": "source만으로 제거할 수 없는 이유",
  "evidence": [{"locator": "...", "claim": "..."}],
  "affected_observable_ids": ["OBS-001"],
  "options": [
    {"id": "A", "choice": "...", "impact": "...", "preserves_observable_ids": ["OBS-001"]},
    {"id": "B", "choice": "...", "impact": "...", "preserves_observable_ids": ["OBS-001"]}
  ],
  "owner": "user",
  "status": "pending",
  "approved_option_id": null
}
```

모든 option은 연결된 preserved observable을 보존해야 한다. 하나라도 누락하면
`FIXED_OBSERVABLE_REOPENED`다. 이미 정해진 error/status/ownership을 다른 값으로 바꾸는 option은 Decision이
아니라 invalid option이다. Pending/deferred Decision은 freeze를 막는다.

## Phase invariant

### Intake

- analysis 배열은 모두 비어 있음
- `frozen_digest: null`
- `handoff: {"graph_allowed": false, "blocker": "analysis-incomplete"}`

### Analysis

- 모든 change requirement가 gap에 연결됨
- 모든 investigation target이 disposition됨
- 모든 gap이 slice에 연결됨
- 모든 observable이 verification row에 연결됨
- Decision이 없으면 blocker `freeze-required`
- pending/deferred Decision이 있으면 blocker `decision-required`
- `graph_allowed: false`, `frozen_digest: null`

### Frozen

- pending/deferred Decision 없음
- blocker null, `graph_allowed: true`
- `frozen_digest`는 해당 field를 null로 한 canonical JSON의 SHA-256
- body 변경 시 `FROZEN_BODY_DIGEST_MISMATCH`

Freeze는 `current-state-inspection-v1`의 `fresh`, clean, ancestor, planning SHA와 snapshot SHA 일치를
다시 검증한다.

## 구조 검증과 의미 감사

Validator는 closed schema, ID 참조, phase, coverage shape, digest와 fixed-observable 보존을 검사한다.
다음은 PM의 별도 semantic audit 대상이다.

- locator가 claim을 실제로 지지하는지
- `required_state`가 source에서 유래했는지
- test evidence가 observable 전체를 직접 assert하는지
- gap과 slice의 책임 경계가 적절한지
- 새로운 product behavior가 발명되지 않았는지

구조 validator pass는 semantic approval이 아니다.
