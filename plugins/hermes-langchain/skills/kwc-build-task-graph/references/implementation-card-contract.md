# PM backend Impl card authoring contract v6

이 문서는 Project Manager가 current writer `backend-implementation-card-v6` body를 작성하는 의미와 책임을
소유한다. 결정론적 validation은 `implementation_card.py`와 `validate-impl-batch`가 소유한다. v5는
historical card read-back 호환 schema이며 새 Impl의 기본 writer가 아니다.

## 목적

Impl card는 frozen `triage-v7`의 한 implementation slice를 Coder가 요구사항을 다시 기획하지 않고 실행할 수
있는 동작 계약으로 바꾼다. 파일 목록과 자유 형식 설명을 쌓는 대신 **operation → observable → verification**을
연결한다. Runner, CWD, success oracle, native status와 실행 결과는 workflow가 소유하므로 body에 넣지 않는다.

## Closed body

Top-level field는 다음 일곱 개뿐이다.

| Field | 의미 |
|---|---|
| `schema` | 정확히 `backend-implementation-card-v6` |
| `trace` | Triage digest와 slice/requirement/observable/gap ID closure |
| `objective` | frozen slice의 독립 outcome |
| `boundary` | 구현 scope와 명시적 out-of-scope |
| `obligations` | behavior 또는 preservation 동작과 관찰 결과 |
| `constraints` | source-backed 구조·계약·보존 규칙 |
| `verification` | contract ID를 증명할 test level과 selector |

Baseline SHA, assignee, workspace, priority, dependency, run과 result는 body field가 아니다.

## `trace`, `objective`, `boundary`

- `trace.triage_digest`는 valid 64자 SHA-256 digest다.
- `trace.slice_id`, `requirement_ids`, `observable_ids`, `gap_ids`는 frozen source closure를 중복·누락 없이
  보존한다.
- `objective.outcome`은 matching frozen slice의 outcome이다.
- `boundary.in_scope`와 `boundary.out_of_scope`는 제품·구조 책임 경계를 구체적으로 설명한다.
- Current writer가 만든 body는 새로운 behavior ID를 발명하지 않는다.

## `obligations`

각 obligation은 `id`, `kind`, `given`, `operation`, `expected`, `observables`를 가진다.

- `kind`: `behavior | preservation`.
- `given`: operation 전에 참이어야 하는 제품 상태다.
- `operation`: `symbol`, `invocation_count`를 가진다. `symbol`은 concrete `FQCN#method`이고
  `invocation_count`는 positive integer다.
- `expected`: operation이 만들어야 하는 구체적인 제품 결과다.
- Frozen Triage acceptance에서 prefill된 `kind`, non-empty `operation.symbol`, `expected` criterion은 semantic
  anchor다. Completion은 이를 삭제하거나 일반화하지 않고 precondition·observable 세부만 보강한다.
- Gap `required_state`의 named exception/error, 입력 종류, error code/status/message와 identity는 matching
  acceptance와 obligation에 literal로 남긴다.
- `observables`: `target`, `source`, `comparison`, `identity`, `values`, `expected`를 가진다. 결과를 어디서 읽고
  무엇으로 동일성을 판정하며 어떤 값이 기대되는지 분리한다.
- Acceptance는 matching obligation 또는 constraint에 materialize되고, preservation observable은
  preservation obligation과 verification으로 닫힌다.
- HTTP request/response, application command/result, aggregate named behavior, port source/target, duplicate
  consumer/producer처럼 별도 책임인 artifact를 하나의 vague operation으로 합치지 않는다.

## `constraints`

각 constraint는 `id`, `rule`, `source {locator, claim}`을 가진다.

- Gap evidence, package/module boundary, transaction/locking, public contract와 cross-domain contract change를
  source-backed rule로 보존한다.
- `locator`는 repository-relative `path#symbol-or-heading`, `claim`은 그 source가 직접 지지하는 구체 사실이다.
- Planned artifact target은 `rule` 또는 `boundary`에 exact package/path와 symbol을 명시하고 current source
  locator와 함께 해석 가능해야 한다.
- Contract change ID와 full change 의미를 다른 이름으로 바꾸지 않는다.
- Constraint는 authoring completion의 mutable field가 아니다. 빠진 constraint는 source Triage 또는 writer
  defect이며 agent가 추측해 덧붙이지 않는다.

## `verification`

각 verification은 `id`, `covers`, `level`, `selectors`를 가진다.

- `covers`는 obligation/constraint ID만 참조하며 모든 contract ID를 하나 이상의 verification이 cover한다.
- Prefilled `covers`는 frozen evidence mapping이므로 completion에서 추가·삭제·재배치하지 않는다.
- `level`: `unit | slice | repository | integration | modulith`.
- `selectors`: actual/new executable test `FQCN#method` 1~3개. Class-only selector와 production owner method는
  허용하지 않으며 test owner는 `*Test|*Tests|*Spec|*IT` 관례를 따른다.
- Selector는 Triage의 decisive scenario를 직접 판정해야 한다. unrelated existing test로 missing coverage를
  숨기지 않고 새 coverage는 concrete planned method name을 쓴다.
- `boundary.in_scope`의 모든 `verify:` selector는 하나 이상의 `verification[].selectors`에 exact하게
  materialize한다. 이를 올바른 `level`로 담을 source verification row가 없으면 Impl authoring이 추측하지
  않고 source Triage 결함으로 차단한다.
- Runner는 `gradle-mcp`이고 full backend gate는 workflow가 소유한다.

## Batch wrapper와 dependency

Wrapper는 ordinal, canonical title, priority, body, required verification IDs, contract changes,
required symbols, optional Triage parent와 `depends_on_ordinals`를 가진다.

- Triage slice의 `depends_on`을 복사하지 않는다.
- Child가 planning baseline만으로 구현·검증할 수 없을 때만 semantic parent를 둔다.
- 모든 dependency는 `dependency_audit.edges[]`에 `from_ordinal`, `to_ordinal`, `required_artifact`,
  `consumer_entry_point`, `why_baseline_cannot_satisfy`를 가진다.
- 공유 파일, 충돌 회피, 선호 실행 순서와 priority는 dependency proof가 아니다.
- `final integration/regression`이 earlier production artifact를 검증하면 **모든 owner card**를
  `depends_on_ordinals`와 `dependency_audit.edges[]`에 넣는다. Final card는 production operation을 다시
  소유하지 않고 `verify` 의미의 verification만 소유한다.

## Compact completion

`prepare-impl-fast-path`가 immutable body를 만든 뒤 author는 다음 mutable field만 제출한다.

```json
{
  "cards": [
    {
      "ordinal": 1,
      "obligations": [],
      "verification": [],
      "depends_on_ordinals": []
    }
  ],
  "dependency_audit": {"edges": []}
}
```

- Obligation ID와 순서, operation symbol을 보존하고 `given`, `invocation_count`, `expected`, `observables`를
  완성한다.
- Verification ID와 순서, `covers`를 보존하고 `level`, `selectors`를 구체화한다.
- `constraints`, `trace`, `objective`, `boundary`를 completion에서 다시 쓰지 않는다.
- `apply-impl-completions`는 authored allowlist와 ID cardinality를 검사하고 merged v6 body를 검증한 뒤에만 쓴다.
- Apply 실패 후 stale batch를 validate하지 않는다.

## Invariants

- 모든 source acceptance, gap, observable, verification과 contract change가 matching body에서 닫힌다.
- 모든 path와 symbol은 concrete하며 `unknown`, `unresolved`, `todo`, `tbd`가 없다.
- 모든 obligation/constraint ID는 verification으로 cover된다.
- 설명은 한국어로 구체적으로 작성하되 ID, path, symbol, enum과 tool name은 literal을 보존한다.
- Credential, raw tool output, prompt, hidden reasoning, runtime verdict와 release 사실을 저장하지 않는다.
- Validator 통과는 shape 증거다. PM은 card가 하나의 독립 검증 가능한 vertical change인지 별도로 검수한다.