---
name: kwc-impl-authoring-runtime
description: Use when authoring one Impl batch from frozen Triage. Runs the bounded body-only fast path.
version: 0.1.0
author: Amaazon project, Hermes Agent
license: MIT
platforms: [windows, linux, macos]
metadata:
  hermes:
    tags: [project-management, kanban, implementation, authoring, performance]
requires_toolsets: [terminal, hermes-langchain]
---

# Impl authoring runtime

승인된 frozen `triage-v7`에서 **Impl batch 한 개만** 생성한다. Triage의 slice, gap, acceptance,
verification, evidence와 production seam이 정본이다. Review·Summary·activation·dispatch는 수행하지 않는다.

## 불변조건

- assignment의 `board`, `issue_number`, `generation`, `sources.triage_id`, `workspace_path`,
  `artifact_file`, `assignment_id`, `next_action`을 그대로 사용한다.
- `next_action`은 `kwc_create_impl_graph`여야 한다. KWC mutation은 이 도구를 정확히 한 번 호출한다.
- 모든 frozen `implementation_slices[].id`를 정확히 한 번 materialize한다. subset, duplicate, single-card
  shortcut은 금지한다.
- 각 matching slice의 acceptance ID와 acceptance가 요구하는 verification ID를 보존한다.
  Wrapper의 `required_verification_ids`도 누락·추가·중복 없이 유지한다.
- Triage slice의 `depends_on`을 복사하지 않는다. Impl dependency는 semantic proof로만 새로 결정한다.
- unresolved `unknown`, `unresolved`, `todo`, `tbd` symbol을 제출하지 않는다.
  Prefill symbol이 `unknown`이면 completion에서 다른 artifact를 끼워 넣지 말고 같은 path의 concrete symbol을
  source Triage에서 확정한 뒤 새 batch를 준비한다.

## 절차

### 1. Fast-path source를 한 번 준비한다

첫 terminal call에서 assignment의 exact `workspace_path`를 `workdir`로 사용한다. 다음 helper를 정확히 한 번
실행한다. Issue, requirement, current-state, production/test source, historical board, archived card, stale
`.temp` artifact를 먼저 읽지 않는다.

```text
python "$HERMES_KWC_PLUGIN_ROOT/skills/kwc-build-task-graph/scripts/build_task_graph.py" prepare-impl-fast-path --profile "${HERMES_KWC_FRESH_HOST_PROFILE:-${HERMES_PROFILE:-main}}" --board <board> --triage-id <triage-id> --issue <issue> --generation <generation> --output-dir <assignment.artifact_file의 parent>/fast-path --batch-output <assignment.artifact_file>
```

Helper가 issue, digest, workspace, frozen contract와 active Impl 부재를 검증한다. 성공 뒤에는
`<assignment.artifact_file>`만 읽는다. `source-triage.json`, `triage-body.json`, archived card body나 helper
source를 별도로 읽지 않고 `impl-batch-template`도 다시 실행하지 않는다.

### 2. Compact completion을 한 번 작성한다

Prefilled batch의 immutable fact를 재작성하지 않는다. ordinal별 mutable field만
`<assignment.artifact_file의 parent>/impl-card-completions.json`에 한 번의 batched write로 작성한다.

```json
{"cards":[{"ordinal":1,"obligations":[],"verification":[],"depends_on_ordinals":[]}],"dependency_audit":{"edges":[]}}
```

각 card에 필요한 내용:

- `obligations`: prefilled obligation ID와 순서를 exact하게 보존한 전체 list를 작성한다. 각 row의 `kind`는
  `behavior | preservation`, `given`은 실제 precondition이다. `operation.symbol`은 helper가 frozen Triage
  production seam에서 채운 exact `FQCN#method`를 그대로 유지하고 `invocation_count`를 positive integer로
  완성한다. Exact repository path는 immutable `constraints[].source.locator`와 `boundary`에서 확인한다.
  Prefilled `kind`, non-empty `operation.symbol`, `expected`의 acceptance criterion 문자열은 immutable semantic
  anchor다. 삭제·대체하지 않고 필요한 구체 조건과 결과만 추가한다. Named exception/error, 입력 종류,
  error code/status/message와 observable identity를 일반 표현으로 축약하지 않는다.
  `expected`는 operation 결과를 구체적으로 적고 `observables`는 결과를 어디서 읽는지
  (`source`), 무엇을 비교하는지(`comparison`), identity/value와 expected outcome을 분리해 기록한다.
  제품 의미는 구체적인 한국어 설명으로 작성하며 Generic "동작한다"나 파일명-only symbol은 허용하지 않는다.
- `constraints`와 `trace`는 immutable prefill이다. Wrapper의 cross-domain `contract_changes`는
  `constraints[]`에 ID·source와 함께 materialize되어 있으므로 completion에서 다시 쓰거나 다른 이름으로
  바꾸지 않는다. HTTP DTO request/response, application command/result, aggregate named behavior, port의
  source/target, duplicate consumer/producer의 exact artifact closure는 각각 별도 obligation 또는 constraint로
  이미 닫혀 있어야 한다.
  빠진 artifact가 있으면 completion에서 추측하지 말고 source Triage 결함으로 block한다.
- `verification`: prefilled verification ID와 `covers`를 보존하고 `level`, `selectors`를 완성한 전체 list를 쓴다.
  `level`은 `unit | slice | repository | integration | modulith`, `selectors`는 actual/new
  `FQCN#method` 1~3개다. Class-only FQCN이나 unrelated existing test를 사용하지 않고 Triage가 요구한 decisive
  scenario를 직접 판정하는 selector를 선택하며 missing coverage는 card가 만들 planned method name으로 닫는다.
  Production owner method를 selector로 복사하지 않고 `*Test|*Tests|*Spec|*IT#method` 형태의 executable test
  member를 사용한다. Prefilled `covers`는 evidence mapping이므로 순서와 내용을 변경하지 않는다.
  Immutable `boundary.in_scope`의 모든 `verify:` selector는 `verification[].selectors`에도 exact하게 materialize한다.
  이를 올바른 level로 담을 prefilled verification row가 없으면 임의의 row에 끼워 넣지 말고 source Triage
  verification matrix 결함으로 block한다.
- `depends_on_ordinals`: baseline만으로 child를 구현·검증할 수 없을 때만 작성한다.

`final integration/regression`이 earlier card의 production artifact를 검증하면 모든 owner card를
`depends_on_ordinals`와 `dependency_audit.edges[]`에 넣는다. Final card는 해당 production operation을 다시
소유하지 않고 `verify` 목적의 verification만 소유한다. Semantic parent 생략은 금지한다.

Frozen Triage locator로 concrete artifact identity를 확정할 수 없을 때만 메모리상의 질문 목록에
`MCP_LOOKUP_REQUIRED:<slice>:<question>`을 추가한다. 질문이 있으면 Codebase Memory `search_graph`와 Semble
`search`를 한 번의 parallel turn으로 실행한다. 그래도 exact path/symbol을 확정할 수 없으면 작성물을
추측하지 말고 `MCP_LOOKUP_REQUIRED` blocker로 종료한다. 이미 Triage가 고정한 제품 의미를 재검증하거나
검색어를 바꿔 반복 탐색하지 않는다.

Dependency edge마다 `from_ordinal`, `to_ordinal`, `required_artifact`, `consumer_entry_point`,
`why_baseline_cannot_satisfy`를 모두 작성한다. Proof가 없으면 edge와 `depends_on_ordinals`를 모두 비운다.
Priority나 선호 실행 순서는 dependency proof가 아니다.

### 3. Merge와 validation을 bounded sequence로 실행한다

```text
python "$HERMES_KWC_PLUGIN_ROOT/skills/kwc-build-task-graph/scripts/build_task_graph.py" apply-impl-completions --batch <assignment.artifact_file> --completions <assignment.artifact_file의 parent>/impl-card-completions.json --output <assignment.artifact_file>
python "$HERMES_KWC_PLUGIN_ROOT/skills/kwc-build-task-graph/scripts/build_task_graph.py" validate-impl-batch <assignment.artifact_file>
```

Helper는 prefill을 보존해 merge한 뒤 semantic validation한다. 실패 시 output을 쓰지 않으며 reported
mutable field만 한 번 수정한다. Apply 실패 후 stale batch를 validate하지 않는다. Immutable fact를
고치거나 두 번째 실패를 우회하지 않는다.

최종 batch에는 다음이 모두 성립해야 한다.

- frozen slice, obligation/constraint/verification closure가 exact함
- 모든 symbol이 concrete함
- obligation별 operation·observable closure가 완전함
- request/response와 application command/result 경계가 독립적임
- dependency와 dependency audit proof가 일치함
- 각 body가 current writer schema `backend-implementation-card-v6` validator를 통과함

### 4. KWC tool을 한 번 호출하고 root workflow로 반환한다

검증된 `artifact_file`과 `assignment_id`로 `kwc_create_impl_graph`를 한 번 호출한다. Native
create/read-back/rollback은 KWC가 소유하며 수동 Kanban mutation은 금지한다.

결과의 `next_assignment`가 `review`이고 생성된 Impl ID 수가 frozen slice 수와 같은지 확인한 뒤
`next_assignment`를 이미 실행 중인 root `kwc-run-workflow` 절차로 반환한다. 별도 Profile process나 session을
실행하지 않는다.
