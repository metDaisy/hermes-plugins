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
- 각 matching slice의 acceptance ID와 acceptance가 요구하는 verification ID를 exact하게 보존한다.
  Wrapper의 `required_verification_ids`도 누락·추가·중복 없이 유지한다.
- Triage slice의 `depends_on`을 복사하지 않는다. Impl dependency는 semantic proof로만 새로 결정한다.
- unresolved `unknown`, `unresolved`, `todo`, `tbd` symbol을 제출하지 않는다.

## 절차

### 1. Fast-path source를 한 번 준비한다

첫 terminal call에서 assignment의 exact `workspace_path`를 `workdir`로 사용한다. 다음 helper를 정확히 한 번
실행한다. Issue, requirement, current-state, production/test source, historical board, archived card, stale
`.temp` artifact를 먼저 읽지 않는다.

```text
python "$HERMES_KWC_PLUGIN_ROOT/skills/kwc-build-task-graph/scripts/build_task_graph.py" prepare-impl-fast-path --profile "${HERMES_KWC_FRESH_HOST_PROFILE:-${HERMES_PROFILE:-main}}" --board <board> --triage-id <triage-id> --issue <issue> --generation <generation> --output-dir .temp/task-graphs/<issue>/fast-path --batch-output <assignment.artifact_file>
```

Helper가 issue, digest, workspace, frozen contract와 active Impl 부재를 검증한다. 성공 뒤에는
`<assignment.artifact_file>`만 읽는다. `source-triage.json`, `triage-body.json`, archived card body나 helper
source를 별도로 읽지 않고 `impl-batch-template`도 다시 실행하지 않는다.

### 2. Compact completion을 한 번 작성한다

Prefilled batch의 immutable fact를 재작성하지 않는다. ordinal별 mutable field만
`.temp/task-graphs/<issue>/impl-card-completions.json`에 한 번의 batched write로 작성한다.

```json
{"cards":[{"ordinal":1,"implementation_contract":{},"entry_points":[],"verification":[],"depends_on_ordinals":[]}],"dependency_audit":{"edges":[]}}
```

각 card에 필요한 내용:

- `implementation_contract`: actor/authorization, API, state transition, error semantics,
  persistence/transaction, event applicability, preserved behavior를 concrete하게 닫는다. 각 dimension의
  `applicability`는 반드시 `applicable` 또는 `not-applicable` enum으로 작성하고 설명을 그 dimension의
  rule에 둔다.
- 모든 rule과 preserved behavior에는 한국어 설명을 포함한다. Wrapper의 각 `contract_changes[].id`를
  그대로 시작 marker로 보존하되 full change 의미를 한국어로 설명한 항목을 `preserved_behaviors`에 직접
  작성한다. 이를 생략하면 merge helper가 영문 source 문구를 자동 추가해 한국어 validator가 실패한다.
- `entry_points`: acceptance별 **exact artifact closure**를 만든다. 현재 또는 planned path, concrete symbol,
  해당 artifact가 acceptance를 충족하는 이유를 적는다. Prefill symbol이 placeholder이면 그 항목을
  생략하지 말고 **같은 path**의 concrete symbol을 completion에 작성해 교체한다.
- `verification`: prefilled verification ID를 exact하게 사용하고 최소 test level, actual/new FQCN selector,
  decisive scenario 1~3개만 작성한다. `test_level`은 반드시 `unit`, `slice`, `repository`, `integration`,
  `modulith` 중 하나를 사용한다. `web-layer`는 `slice`, application/cross-module integration은 `integration`으로
  표현한다.
- `depends_on_ordinals`: baseline만으로 child를 구현·검증할 수 없을 때만 작성한다.

HTTP DTO acceptance는 request와 response closure를 독립적으로 닫는다. 기존 application request/response DTO,
planned presentation request/response DTO, planned application command/result를 각각 exact path와 symbol로 쓴다.
Planned application output은 transport-neutral `application/result` 하나만 사용하며 같은 acceptance에 별도
planned `application/response`를 만들지 않는다. 기존 application response DTO는 이동·삭제 source다.

Aggregate 위임은 named behavior symbol, port 이동은 source/target path, duplicate 제거는 삭제할 consumer
type과 사용할 producer type을 모두 기록한다. `command/result`, `inner layer` 같은 vague reason은 금지한다.

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
python "$HERMES_KWC_PLUGIN_ROOT/skills/kwc-build-task-graph/scripts/build_task_graph.py" apply-impl-completions --batch <assignment.artifact_file> --completions .temp/task-graphs/<issue>/impl-card-completions.json --output <assignment.artifact_file>
python "$HERMES_KWC_PLUGIN_ROOT/skills/kwc-build-task-graph/scripts/build_task_graph.py" validate-impl-batch <assignment.artifact_file>
```

`apply-impl-completions`는 prefilled entry point를 보존하고 exact path/symbol reason을 교체하며 중복을 제거한다.
Validation이 실패하면 structured error가 지목한 mutable completion field만 consolidated correction 한 번으로
수정하고 merge/validate를 한 번 재실행한다. Immutable wrapper/body fact, source acceptance,
`required_verification_ids`, Triage digest를 직접 고치지 않는다. 두 번째 validation도 실패하면 block한다.

최종 batch에는 다음이 모두 성립해야 한다.

- frozen slice, acceptance, verification closure가 exact함
- 모든 symbol이 concrete함
- acceptance별 exact artifact closure가 완전함
- request/response와 application command/result 경계가 독립적임
- dependency와 dependency audit proof가 일치함
- 각 body가 `backend-implementation-card-v5` validator를 통과함

### 4. KWC tool을 한 번 호출하고 중단한다

검증된 exact `assignment.artifact_file`을 현재 `assignment_id`와 함께 `kwc_create_impl_graph`에 전달한다.
KWC가 batch 전체 validation, native create, parent/workspace/body read-back과 rollback을 소유한다. Agent가
`render-impl-batch`, `create-impl-batch-native`, 수동 Kanban create/show를 별도로 실행하지 않는다.

결과의 `next_assignment`가 `review`이고 생성된 Impl ID 수가 frozen slice 수와 같은지 확인한 뒤 중단한다.
Review 또는 Summary를 이어서 작성하지 않는다.
