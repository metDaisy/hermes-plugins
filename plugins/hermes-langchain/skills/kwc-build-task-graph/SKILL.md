---
name: kwc-build-task-graph
description: KWC fresh agent가 profile SOUL·memory 없이 수행하는 "승인된 Issue에서 검증 가능한 Kanban graph를 작성한다."
version: 0.20.26
author: "Amaazon project, Hermes Agent"
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [project-management, kanban, task-graph, requirements, verification]
    related_skills: [kwc-orchestrator-contract, kwc-create-triage, kwc-service-planning, kwc-sync-docs, kwc-run-workflow, codebase-memory-mcp, semble-search]
requires_toolsets: [terminal, hermes-langchain]
---

> 이 Skill의 실행 정책과 helper는 Kanban Workflow Controller가 소유한다. PM Profile copy는 비교 기준일 뿐 실행 의존성이 아니다.

# Task graph 작성

승인된 Issue 계약을 native Kanban graph로 변환한다. PM은 graph membership, self-contained card,
최초 activation과 rework mutation을 소유한다. Native Kanban은 task ID, assignee, status, link, run,
comment와 event를 소유한다.

Impl body-only fast path를 제외한 모든 mode에서 [`references/board-contract.md`](references/board-contract.md)를 읽는다. Backend Impl을
작성할 때만 [`references/implementation-card-contract.md`](references/implementation-card-contract.md)를
추가로 읽는다. 전자는 persisted graph의 schema·invariant, 후자는 Impl body의 단일 기준이다.

모든 helper와 validator는 [`../kwc-orchestrator-contract/references/script-usage.md`](../kwc-orchestrator-contract/references/script-usage.md)의 black-box 계약을 따른다.
Backend Impl은 `template → 입력 작성 → validate`로 작성하고, graph validator 실패 시 structured error
code가 지목한 field만 수정한다. 정상 graph 작성 중 script 구현 소스를 읽어 rule을 역추론하지 않는다.

## 사용 조건

- `new`: 승인된 leaf Issue의 G1 graph가 필요하다.
- `requirement-rework`: aggregate Review 승인 전에 승인 requirement revision이 active contract를
  바꿔 G{N+1} superseding graph가 필요하다.
- `review-rework`: 완료된 aggregate Review의 structured finding에 같은 generation의 corrective Impl,
  context re-review 또는 blocked Decision이 필요하다.

aggregate Review 승인 뒤 requirement가 바뀌면 기존 graph를 수정하지 않고 새 Issue의 `new`를
사용한다.

## 조건부 Skill routing

다음 조건에서 해당 Skill을 읽고 그 완료 기준을 충족할 때까지 graph 작성을 중단한다.

- 새 leaf Issue의 G1 planning record가 없으면 `create-triage`를 사용한다. G{N+1} rework Triage는 이
  Skill이 작성한다.
- Policy·authorization·consistency·오류 의미·UI 의미에 사용자 결정이 남으면
  `service-planning`을 사용하고 Triage와 linked Planning Decision의 협업을 완료할 때까지 graph 작성을 중단한다.
- 승인 requirement와 파생 문서 또는 Issue가 불일치하면 `sync-docs`를 사용하고 mutation을
  read-back한다.
- `current-state.md`가 fresh하지 않거나 판정 근거가 부족하면 graph 작성을 중단하고
  `snapshot-refresh-required` blocker를 root `run-workflow`에 반환한다. Requirement-rework 중간에는
  snapshot 대신 committed source를 직접 확인한다.
- Behavior locator가 불명확할 때만 `codebase-memory-mcp` 또는 `semble-search`를 사용한다. 결과는
  후보이며 committed source/test/migration read-back이 구현 상태의 근거다.
- 최초 activation 뒤 checkpoint, 후속 promotion, aggregate Review, Summary와 release lifecycle은
  `run-workflow`에 인계한다. Review finding이 graph mutation을 요구하면 이 Skill의
  `review-rework`로 돌아온다.

## Impl body-only fast path

사용자가 이미 승인한 frozen Triage에서 Impl body만 생성·감사하도록 요청하고 Review·Summary·activation을
명시적으로 제외하면 이 bounded path를 사용한다. 이 경로에서는 Frozen Triage를 source-closure snapshot으로
취급한다. Triage v7의 slice, gap, acceptance, verification evidence와 `production_seam`이 card 작성에 필요한
제품 의미와 현재 상태를 이미 고정하므로 Issue·requirement·`current-state.md`·production/test source를 다시
전수 조사하지 않는다.

Fresh-agent assignment에서는 `<artifact-root>`를 exact `assignment.artifact_file`의 parent로 정의한다. 이 경로는
`.temp/task-graphs/<issue>/generation-<generation>/<board-namespace>`이며 Agent가 namespace를 계산하거나
다른 board/generation의 issue-only directory로 축약하지 않는다.

1. `build-task-graph`와 `references/implementation-card-contract.md`만 읽는다. Full graph topology가 입력이
   아니므로 `board-contract.md`, `run-workflow`, Kanban CLI `--help`를 읽지 않는다.
2. source Triage의 native `workspace_path`를 canonical repo root로 사용하고 모든 helper terminal call의 `workdir`을
   그 exact path로 지정한다. Session 시작 cwd나 다른 repository root로 fallback하지 않는다. 그 위치에서
   installed Skill의 `prepare-impl-fast-path`를 정확히 한 번 실행한다. 이 helper가 source
   Triage `show --json`과 active board `list --json`을 각각 한 번만 읽고 exact task ID, `triage-v7`,
   `phase=frozen`, 전체 Triage validator와 digest, native `workspace_kind=dir` 및 `workspace_path=workdir`,
   issue와 active Impl 부재를 fail-closed 검증한다. Raw 응답, extracted Triage body와 prefilled batch를 각각
   `<artifact-root>/fast-path/` 및 `<artifact-root>/impl-card-batch.json`에 저장하고 stdout에는
   identity/status/count/digest만 출력한다. Agent는 compact stdout과 `impl-card-batch.json`만 읽으며
   source-triage.json이나 triage-body.json을 별도로 읽지 않는다. Custom preflight Python과 large terminal output
   spillover를 만들지 않고 terminal output log를 재파싱하지 않는다. 과거 Impl card와 archived card의 card body를 읽지 않는다.
   이전 생성물을 품질 근거나 authoring template로 사용하지 않는다.
3. 각 slice를 Triage의 matching gap, acceptance, observable, verification row와 결합한다. 모든 production
   seam과 owner class FQCN이 Triage에 있으면 MCP와 direct source read를 모두 생략한다. Verification의
   planned test method name은 authoring output이므로 기존 source에 같은 method가 아직 없어도 오류가 아니다.
   existing test method 존재를 확인하려고 source tree나 모든 test class를 조사하지 않는다. Card에 필요한
   production symbol 또는 test owner class FQCN 자체를 Triage locator만으로 결정할 수 없을 때만
   `MCP_LOOKUP_REQUIRED:<slice>:<question>`을 메모리상의 lookup 목록에 추가한다. 목록이 비어 있지 않으면
   Codebase Memory `search_graph`와 Semble `search`를 한 번의 parallel turn에 직접 호출한다. 이 repository에서
   Codebase Memory의 `project`는 repository에 등록된 project name이며 workspace absolute path를 `project`로 전달하지 않는다. MCP snippet으로
   class owner를 확정할 수 없을 때만 다음 한 turn에서 최대 4개 file의 class declaration 범위를 읽는다.
   이미 Triage가 고정한 제품 의미를 다시 검증하려고 production source를 읽거나, 같은 의미를 다른 검색어로
   반복하거나, module file listing으로 되돌아가지 않는다. Verification마다 required behavior를 판정하는
   최소 1~3개의 decisive selector만 남기며 발견한 test를 전부 나열하지 않는다.
   Completion을 쓰기 전에 acceptance별 obligation과 constraint closure를 확인한다. Helper가 acceptance의
   production seam을 `obligations[].operation.symbol`로, exact repository path와 gap·구조·contract change를
   `constraints[]`로 prefill한다. HTTP DTO request/response, application command/result, aggregate named
   behavior, port source/target, duplicate consumer/producer처럼 서로 다른 artifact는 하나의 generic 문장으로
   합치지 않는다. Prefill constraint locator나 exact symbol이 없거나 한쪽 boundary만 있으면 completion에서 추측하지 않고
   `MCP_LOOKUP_REQUIRED` 또는 source-Triage blocker로 되돌린다.
4. `prepare-impl-fast-path`가 Frozen Triage의 issue/digest, slice, acceptance, gap evidence, constraint,
   production seam, verification ID와 canonical wrapper를 이미
   `<artifact-root>/impl-card-batch.json`에 채웠으므로 `impl-batch-template`을 별도로 실행하지 않는다.
   Wrapper의 `contract_changes`와 `required_contract_symbols`는 해당 verification에 연결된 Frozen Triage의
   cross-domain contract change를 보존한다. Helper는 contract change를 immutable `constraints[]`에 source와
   함께 materialize하고 required named behavior symbol을 obligation operation 또는 constraint에서 검증한다.
   `validate-impl-batch`가 contract change나 required symbol을 하나라도 누락하면 native mutation을 거부한다.
   별도 single-card `template` 실행,
   validator `--help`, stale artifact 검색을 하지 않는다. Helper가 채운 사실을 다시 타이핑하지 말고
   prefilled ID·symbol·covers를 보존한 전체 `obligations`와 `level`, actual/new `FQCN#method` selector를 담은
   전체 `verification`만 ordinal별 `<artifact-root>/impl-card-completions.json`에
   한 번의 batched write로 작성한다.
   Compact completion의 closed shape는
   `{"cards":[{"ordinal":<int>,"obligations":[<complete prefilled-ID obligation rows>],"verification":[{"id":<prefilled VERIFY id>,"covers":[<prefilled IDs>],"level":<level>,"selectors":[<FQCN#method>]}],"depends_on_ordinals":[<semantic parent ordinal>]}],"dependency_audit":{"edges":[<semantic proof>]}}`다.
   이 shape의 예시나 template을 찾으려고 installed Skill directory, references, tests 또는 stale artifact를
   `search_files`·`read_file`로 다시 조사하지 않는다.
   전체 batch를 다시 직렬화하는 custom Python fill script를 만들지 않는다. `apply-impl-completions`로 compact
   completions를 prefilled batch에 merge한 뒤 같은 `impl-card-batch.json`에 원자적으로 덮어쓴다. Helper는
   ordinal·obligation ID·verification ID cardinality와 authored field allowlist를 검사해 immutable Triage
   fact의 재작성을 차단한다. `constraints`와 `trace`는 completion에 넣지 않는다. Merge 뒤
   `unknown`·`unresolved`·`todo`·`tbd` placeholder가 하나라도 남으면 native mutation 전에 실패한다.
   `--output`은 helper가 허용하는 repo-relative `.temp/` path를
   사용하며 absolute path 실패를 먼저 시도하지 않는다. 새 attempt 시작 시 stale batch는 helper 실행 전에
   generated artifact로 제거하고 입력으로 읽거나 검색하지 않는다.
   Batch에는 처음부터 object shape의 `dependency_audit: {"edges": []}`가 있다. Triage slice의 `depends_on`을
   Impl dependency로 복사하지 않는다. 실제 semantic prerequisite가 확인된 경우에만 completion의
   `depends_on_ordinals`와 `dependency_audit.edges[]`를 함께 작성하며 각 edge는 `from_ordinal`, `to_ordinal`,
   `required_artifact`, `consumer_entry_point`, `why_baseline_cannot_satisfy`를 모두 가진다. Proof가 없으면
   dependency를 만들지 않는다. 각 body는 독립적으로 current writer
   `backend-implementation-card-v6` validator를 통과해야 한다. 정확한 batch 검증 명령은
   `validate-impl-batch <artifact-root>/impl-card-batch.json`이며 존재하지 않는 `--batch` option을
   시도하지 않는다. Wrapper title은 canonical graph naming을
   그대로 사용해 순서대로 `G{generation}-Issue{issue}-Impl{ordinal}`로 작성하며 slice ID를 title에 삽입하지
   않는다. `validate-impl-batch`로 closed wrapper shape, title·wrapper ordinal·priority·Issue·generation·parent와 모든 body를 batch 단위로 먼저 검증한 뒤 validation을
   한 terminal call의 loop로 전부 실행하고 하나라도 실패하면 native mutation을 하지 않는다.
5. Valid batch는 `render-impl-batch`를 정확히 한 번 실행해 모든 `bodies/impl-<ordinal>.json`을 생성한다.
   Custom Python 직렬화·native mutation script, full graph용 `render-body`, 존재 여부를 추측한 helper를 만들거나
   시도하지 않는다. 이어 installed Skill의 `create-impl-batch-native`를 정확히 한 번 실행한다. 이 helper가
   mutation 전에 rendered body file이 validated wrapper body와 exact하게 일치하고 모든 wrapper parent가 source
   Triage ID인지 확인한다. 또한 source Triage의 issue·digest·workspace와 full frozen contract를 다시 fail-closed
   검증한다. 그 뒤 card별 native create와 immediate `show --json`, 마지막 source Triage read-back을 bounded
   sequence로 수행하고 중간 실패 시 이번 호출에서 생성한 card를 archive한 뒤 active board read-back으로
   cleanup을 검증한다. Native create 출력이 malformed/ambiguous해도 baseline 이후 생긴 canonical Impl ID를
   회수해 rollback한다. Native create에는 `--workspace dir:<canonical-path>`를 사용하고
   source Triage의 모든 `implementation_slices[].id`가 batch body에 정확히 한 번씩 존재하는지도 mutation 전에
   검사한다. 각 slice의 acceptance ID와 그 acceptance가 요구하는 verification ID 합집합도 matching Impl
   body에 정확히 보존해야 한다. 일부 slice만 담은 single-card/subset batch, 중복 slice batch, acceptance나
   verification row를 생략한 batch는 phase 완료로 인정하지 않는다.
   지원하지 않는 `--status`를 전달하지 않는다. 각 create는 source Triage를 실제 parent로 포함하고 native
   default `todo`, `coder`, `workspace_kind=dir`, canonical `workspace_path`, priority와 `skills=[]`를
   read-back하며 결과를 `<artifact-root>/native-readback.json`에 쓴다. `show --json` envelope에서는 card field를 top-level `task`에서, parent ID를
   top-level `parents`에서 읽는다. `task` 내부의 `parent_ids`나 `parents`를 찾지 않는다. 마지막에 source Triage가
   여전히 `triage`인지 한 번 확인한다. 이미 검증한 card를 별도
   terminal call로 다시 show하지 않는다.
   정확한 호출은
   `create-impl-batch-native --batch <artifact-root>/impl-card-batch.json --body-dir <artifact-root>/bodies --profile "${HERMES_KWC_FRESH_HOST_PROFILE:-${HERMES_PROFILE:-main}}" --board <board> --triage-id <triage-id> --workspace <canonical-path> --output <artifact-root>/native-readback.json`이다.
   `--bodies-dir` 같은 추측 option을 시도하거나 사용법을 찾기 위해 `session_search`·`--help`를 호출하지 않는다.

완료 기준: historical/source 재조사 없이 Triage lineage가 정확한 Impl body를 생성하고, 모든 validator와
native read-back이 통과하며 source Triage와 downstream lifecycle은 변경되지 않는다.

## Review body-only fast path

사용자가 fixed frozen Triage와 기존 Impl card를 그대로 두고 aggregate Review만 생성하도록 요청하면 full
graph topology를 재구성해 `validate-graph`로 판정하지 않는다. 먼저 단 한 번의 board `list --json`으로 source
closure와 immutable Review envelope를 materialize한다.

```text
python scripts/build_task_graph.py prepare-review-fast-path --profile "${HERMES_KWC_FRESH_HOST_PROFILE:-${HERMES_PROFILE:-main}}" --board <board> --triage-id <triage-id> --implementation-id <impl-id>... --issue <issue> --generation <generation> --bundle-output <artifact-root>/review-source.json --body-output <artifact-root>/review-body.json
```

Helper가 채우는 값은 schema, Triage digest, baseline, complete behavior IDs, Impl key와 source
acceptance/verification closure뿐이다. `acceptance`, `review_checks`의 criterion·question·expected outcome은 PM이
Triage/Impl 의미와 development/test guide 및 `.hermes/docs/plan.md`의 active handoff를 근거로 직접 작성한다. 속도를 위해 generic 문구를 자동 생성하거나
source row를 기계적으로 한 줄씩 복제하지 않는다. Validator는 complete closure와 owner 관계 외에도 한국어
설명 밀도, 중복되지 않은 criterion/question/outcome, question 형식, guide exact heading, cross-card seam,
test-quality, implementation-convention, inherited behavior preservation을 fail-closed로 검사한다. Review 전체의
`guide_locators`에는 backend development guide, backend test guide, active plan locator가 각각 하나 이상 있어야 한다.
`review-body.json`은 helper가 만든 `review_body_template`를 직접 수정하며 top-level에는 `schema`,
`triage_digest`, `baseline_sha`, `behavior_ids`, `implementation_card_keys`, `acceptance`, `review_checks`,
`scope_exclusions`만 둔다. `issue`, `card_type`, `generation`, `review_card_key`를 추가하지 않는다. Review check
`focus`는 `behavior | implementation-convention | cross-card-seam | test-quality | preservation` 중 하나만 사용하고,
guide heading은 prepared bundle의 exact `guide_locators` 값만 복사한다. 여기에 non-empty
`.hermes/docs/plan.md#<active handoff>` locator를 최소 하나 포함한다. JSON 파일은 native helper가 canonical
serialization으로 정규화하므로 의미 필드와 closure 작성에만 집중한다.

작성 후 한 command가 current source fingerprint를 재검증하고, semantic validator를 통과한 exact body만 native
create하며, 생성 card와 모든 source 불변성을 즉시 read-back한다.

```text
python scripts/build_task_graph.py create-review-native --bundle <artifact-root>/review-source.json --body <artifact-root>/review-body.json --profile "${HERMES_KWC_FRESH_HOST_PROFILE:-${HERMES_PROFILE:-main}}" --board <board> --workspace <source-card-workspace> --output <artifact-root>/review-create-result.json
```

실제 mutation 없이 동일 validation과 native source freshness 비용을 측정하려면 `--dry-run`을 추가한다. 성공
create는 board list 2회, create 1회, created Review show 1회의 bounded native call만 사용한다. Failure가 create
뒤 발생하면 새 Review를 archive하고 실패를 반환한다. Verification ID는 Frozen Triage verification matrix를
여러 Impl이 공유할 수 있으므로 card 간 중복 자체를 거부하지 않고, 각 Review row가 그 ID를 소유한 Impl을
하나 이상 참조하는지 검사한다. Source acceptance ID는 card 간 고유해야 한다. 기존
`validate-review-graph`는 이미 materialize된 validation graph의 호환 검증에만 사용한다.

## Summary fast path

Summary는 완료 시점의 실행 증거를 복제하는 보고서가 아니라, frozen Triage가 정의한 Issue 목표를 사람이
읽을 수 있게 고정하는 최종 로컬 검토 카드다. Body는 반드시 frozen Triage의 deterministic projection으로
생성한다. PR·CI·CodeRabbit·merge·Issue close는 Summary 이후 별도 release lifecycle이 소유하며 Summary
body에 포함하지 않는다.

일반 생성·재생성은 다음 atomic helper 한 번으로 수행한다. PM이 별도 board list, Git status, archive,
prepare, create command를 조립하지 않는다.

```text
python scripts/build_task_graph.py regenerate-summary-native --profile "${HERMES_KWC_FRESH_HOST_PROFILE:-${HERMES_PROFILE:-main}}" --board <board> --triage-id <triage-id> --issue <issue> --generation <generation> --workspace <source-card-workspace> --bundle-output <artifact-root>/summary-source.json --body-output <artifact-root>/summary-body.json --output <artifact-root>/summary-create-result.json
```

Atomic helper는 board를 읽고 active Summary가 있으면 archive한 뒤 exact status read-back과 source refresh를
병렬 실행한다. 이후 frozen Triage와
동일 generation의 complete Impl/Review closure에서 `issue-summary-card-v3` body를 생성한다. Source를 다시
검증한 후 native create를 한 번 수행하고 Summary의 body·assignee·status·priority·workspace·direct parents를
read-back한다. 모든 parent의 reverse child read-back은 병렬 실행한다. 결과의 `stages`는 list,
archive/read-back, prepare/write, create/read-back 소요시간을 기록한다. 불일치하면 이번 호출에서 생성한
Summary를 archive하고 실패한다.

Mutation 전 진단만 필요할 때에만 기존 `prepare-summary-fast-path`와 `create-summary-native --dry-run`을
사용한다. 실제 생성에서 두 helper를 PM이 수동 연결하지 않는다.

완료 기준: active Summary가 정확히 하나이고, stored body가 frozen Triage projection과 byte-for-byte 같으며,
complete Impl/Review parent closure가 양방향으로 확인된다. Runtime status, Review verdict/finding, SHA, test,
worktree, PR/CI/CodeRabbit/merge 사실은 body에 없어야 한다.

Helper가 실패하면 임시 복사본 수정, runtime monkey patch, manual native create로 우회하지 않는다. Source Skill의
계약·테스트를 수정하고 Distribution을 재설치한 뒤 기존 active Summary를 archive하고 처음부터 재시도한다.

## 공통 작성 절차

1. **입력과 mode를 고정한다.** Leaf Issue, 승인 requirement, frozen Triage의
   `planning_baseline_sha`, workflow marker,
   current-state, 기존 native card를 읽는다. 완료 기준: mode와 requirement basis가 하나로 확정되고
   누락 입력은 owner에게 routing되었다.
2. **Change requirement를 보존하고 Behavior를 분류한다.** Frozen `triage-v7`의
   `intent.change_requirements`를 순서와 내용을 포함해 graph의 `change_requirements`에 exact copy한다. 새 의무를
   추측하거나 기존 의무를 삭제·수정하지 않는다. Triage의 모든 `implementation_slices`를 하나 이상의 effective behavior로 materialize하고 각 behavior의
   `requirement_ids`로 requirement를 연결한 뒤, 각 behavior를 `implemented | partial | absent | unknown`으로
   판정한다. `implemented`는 새 contract를 계속 충족하는 source-backed evidence가 있어야 하며,
   `partial`/`absent`만 Impl을 만든다. approved minimum contract가 있으나 real seam이 없는 consumer
   behavior는 producer bridge와 consumer integration follow-up을 모두 가진 `planned` behavior로 남긴다.
   producer 전체 구현을 자동 prerequisite로 만들지 않되 real seam을 요구하는 integration acceptance는
   producer bridge의 검증 뒤에만 완료할 수 있게 link한다. 완료 기준: 모든 behavior에 state, disposition,
   근거와 완료 또는 blocker까지의 path가 있다.
   HTTP API behavior는 card authoring 전에 request DTO, framework binding/validation, application
   command/result, malformed-input mapping과 업무 error owner를 분리해 결정한다. 이 중 하나라도 requirement와
   repository convention으로 확정할 수 없으면 vague Impl card를 만들지 않고 Decision 또는 investigation으로
   route한다. 완료 기준: 모든 change requirement가 하나 이상의 behavior로 cover되고 알 수 없는 requirement
   참조가 없다.
3. **Draft를 검증한다.** `.temp/task-graphs/<issue>/graph.json`을 작성하고 `validate-graph --phase
   draft`를 실행한다. Backend Impl은 `template`로 시작해 승인된 사실만 채우고 `validate`한다.
   Review body는 worker가 나중에 그대로 실행하는 durable contract이고 Summary body는 frozen Triage lineage만
   보존한다. `이번 단계`, `이번 호출`,
   `this phase`, `graph recovery phase`처럼 graph 작성 세션에만 적용되는 coordinator 문구나 일시적인
   claim/review 금지를 persisted execution card에 넣지 않는다. 이러한 상대적 문맥은
   `EPHEMERAL_COORDINATOR_CONTEXT:<key>`로 draft를 거부한다.
   완료 기준: validator finding이 없다. Finding이 하나라도 있으면 native create/comment/link/archive를
   포함한 모든 graph mutation 전에 즉시 중단한다. 실패한 draft를 부분 materialization하지 않는다.
4. **Native graph를 생성한다.** 각 card의 validated body object를 `render-body`로 `.temp` 아래에
   UTF-8, two-space-indented JSON으로 한 번 직렬화한다. `hermes ... kanban --board <board> create
   <title> --body-file <rendered-file> ...`로 그 파일의 exact text를 전달하며, multiline body를 `--body`
   shell argument로 다시 조립하거나 KWC/`pre_tool_call`이 저장 직전에 body를 다시 쓰게 하지 않는다.
   Self-contained body로 충분한 모든 card는 runtime dependency인 forced Skill을 갖지 않으므로 `--skill`을
   사용하지 않는다. Triage가 아닌 card는 dependency-free 상태로 먼저 만들고 나중에 link하지 않는다.
   Wrapper 순서에서 이미 read-back한 모든 direct parent의 actual `t_<hex>` ID를 반복 `--parent` 인자로
   **create 명령에 함께 전달**한다. Parentless create가 native auto-promotion으로 `ready`를 노출하면 즉시
   malformed materialization으로 comment 후 archive하고 activation을 진행하지 않는다. Card를 하나씩
   만들며 body text, ID, assignee, `skills: []`, wrapper `priority`, status=`todo`와 parent를 각각 read-back한다.
   Wrapper priority는 create의 `--priority`에 그대로 전달한다. 각 Impl에는 PM-owned comment로
   `backend-implementation-admission-v1`을 남긴다.
   이 closed object는 `schema`, actual `task_id`, positive wrapper Issue number인 `issue`, exact `workspace`,
   `card_schema: backend-implementation-card-v6`, `restart: null`만 가지며 Coder가 native show에서 읽는
   durable execution intent다. 모든 native card의 full `show --json` envelope를 배열로 모아
   `validate-native-readback --phase draft`로 pre-activation lifecycle과
   title/body/assignee/skills/priority/parent를 wrapper와 대조한 뒤
   `run-workflow/scripts/workflow.py validate-implementation-admission`으로
   comment payload를 검증한다. Wrapper를 read-back 값으로 갱신한다. 완료 기준: 신규 execution card가
   dispatchable하지 않고 wrapper와 native 상태가 일치하며 admission comment가 exact task에 존재한다.
   이전 malformed graph를 archive한 뒤 source Triage가 이미 `done`이면 그 task를 새 graph의 parent로
   재사용하지 않는다. Exact frozen body와 workspace를 복제한 PM-owned recovery Triage를 새 native
   `triage` card로 만들고, source Triage ID와 recovery 사유를 comment로 남긴 뒤 `create-triage` validator로
   read-back한다. 새 graph wrapper의 Triage는 이 recovery gate여야 한다. Done Triage를 parent로 사용해
   target이 자동 `ready`가 된 상태를 activation 성공으로 간주하지 않는다.
5. **Activation을 검증한다.** `run-workflow/scripts/activate_graph.py`의 read-only preflight를 먼저
   통과하고, `--apply`가 Triage/Planning Decision completion과 target 하나의 promotion을 모두 read-back한
   뒤 full native show를 다시 수집해 `validate-native-readback --phase native`를 통과한 경우에만 activation을
   완료로 인정한다. 완료 기준: 정확히 하나의 activation target만
   `ready`이거나, review-rework의 unresolved Decision-only graph에는 `ready` card가 없다.
   Public `kanban specify`는 auxiliary LLM이 title/body를 재작성하므로 frozen Triage에 사용하지 않는다.
   Activation은 PM adapter가 Hermes core의 canonical no-field transition으로 `ready`를 만들고 frozen
   title/body/assignee가 그대로인지 확인한 뒤 native `complete`한다. 실패하면 graph를 dispatch하지 않는다.
   `RECOVERY_REQUIRES_FRESH_TRIAGE_GATE`가 반환되면 방금 만든 unfinished execution card를 comment 후
   archive하고 위 recovery Triage gate부터 다시 materialize한다.

Built-in decomposer는 이 계약을 표현하지 못하므로 사용하지 않는다.

## `new` 절차

1. `create-triage`가 freeze한 native `triage` G1 Triage와 handoff를 읽는다. 완료 기준:
   `build_task_graph.allowed: true`이고 미해결 policy finding이 없다. Planning Decision이 있었다면 하나의
   aggregate card가 Triage request ID 전체를 중복·누락 없이 cover하는지와 request별 approved change를
   native read-back으로 대조한다. Cross-domain `absent-or-partial` request에만 minimum capability와
   producer·consumer follow-up을 요구한다. 불일치하면 graph를 만들지 않는다.
2. Impl, Review1, Summary body와 topology를 작성한다. Graph `planning_baseline_sha`와 모든 Review body의
   `baseline_sha`를 frozen Triage baseline으로 고정한다. Impl은 frozen Triage digest에 결합된 한
   implementation slice의 durable input만 가진 self-contained current writer
   `backend-implementation-card-v6`이다. `obligations`는 behavioral/preservation 동작, exact operation과
   observable 결과를 materialize하고 `constraints`는 구조·계약·근거를 source와 함께 보존한다. Coder가
   requirement를 다시 읽어 제품 의미를 재해석해야 하는 generic 문장, file-name symbol, caller 의미 병합은
   허용하지 않는다. 각 verification에는 actual FQCN selector와 최소 test level만 넣고 runner/CWD/success oracle/full
   backend gate는 workflow 고정값으로 body에 복제하지 않는다. Review body는
   `aggregate-review-card-v5`로 frozen Triage digest, planning baseline, complete behavior scope, Impl checkpoint
   keys, structured aggregate acceptance, concrete `review_checks`와 explicit exclusion을 둔다. Acceptance와
   check는 behavior·Impl·source acceptance·verification ID를 source closure 안에서 연결한다. Review check는
   `docs/backend-development-guide.md` 또는 `docs/backend-test-guide.md`의 exact heading locator와 기대 결과를
   가져 Reviewer가 requirement를 다시 기획하지 않고 구현 책임·test level·cross-card seam·preservation을
   판단하게 한다. 다섯 runtime review axis와 root Gradle 실행 규칙은 `run-review`가 소유하며 현재 HEAD,
   actual diff/path/test result/finding/verdict/session 문구는 body에 넣지 않는다. Summary body는
   `issue-summary-card-v3`로 frozen Triage의 Issue identity, objective, target outcomes와 preserved behaviors를
   deterministic projection한다. Runtime behavior acceptance, parent completion, latest Review approval,
   SHA/test/worktree/release 사실은 복제하지 않고 graph/native topology와 `summary-admission-v1`이 검증한다.
   Draft를 확정하기 전에 behavior별 production seam과 decisive test의 단일 owner card를 정한다. 다른
   planned Impl이 새로 만들 symbol, public contract, fixture 또는 test selector가 필요한 card는 merge하거나
   실제 semantic parent를 둔다. Priority와 `execution_order`만으로 숨은 prerequisite를 만족했다고 보지
   않는다. Production 변경과 그 acceptance를 증명하는 test를 같은 vertical card에 두며, 다른 Impl의
   verification을 뒤에서 모으는 catch-all test card를 만들지 않는다. Baseline에서 behavior가 이미
   구현되어 verification coverage만 독립 gap인 경우에만 test-only Impl을 허용한다.
   모든 Impl→Impl edge는 native mutation 전에 `.temp/task-graphs/<issue>/dependency-audit.json`에
   `parent`, `child`, `required_artifact`, `consumer_entry_point`, `why_baseline_cannot_satisfy`를 기록한다.
   `required_artifact`는 parent가 만드는 exact symbol/public contract/fixture/test selector이고 child body도
   그 artifact의 의미를 명시해야 한다. 공유 파일·충돌 회피·선호 실행 순서·import churn·Triage의 기존
   `depends_on`만으로는 semantic dependency가 아니다. Baseline만으로 child를 구현·검증할 수 있으면 두
   card는 Triage의 독립 child로 두고 priority로 생성 순서만 표현한다. Proof를 쓸 수 없으면 edge를 제거하거나
   두 card를 하나의 vertical slice로 합친다.
3. Planned behavior가 있으면 첫 Impl을 Triage child로 둔다. Planned behavior가 없으면 전체
   inherited behavior를 재검증할 Review1을 Triage child이자 activation target으로 둔다.
4. `dependency-audit.json`의 edge proof를 body와 대조하고 실제 prerequisite만 parent로 연결한다. 독립 Impl을 가짜 dependency로 연결하지 않고 아직 실행되지 않은
   Impl/Review 생성 순서를 `execution_order`에 기록하며 첫 항목부터 `N..1` priority를 부여한다.
   Triage/Decision/Summary priority는 `0`이다.
5. 공통 작성 절차를 완료한 뒤 PM-only activation gate로 Triage를 완료한다.

완료 기준: Triage는 `done`이고 첫 Impl 또는 no-Impl Review 하나만 `ready`다.

## `requirement-rework` 절차

1. 승인 revised requirement와 파생 문서/Issue read-back을 확인한다. `requirement-diff`로
   basis→revised 비교 산출물을 만든 뒤 각 hunk를 behavior delta evidence로 해석한다. Helper가
   behavior 결론을 자동 확정하지 않는다.
2. Affected running card를 실제 supersession 사유로 block하고 worker 종료를 read-back한다. `done`
   history는 보존한다.
3. Prior Summary, archived unfinished work, inherited done evidence, revised requirement와 complete
   effective behavior를 가진 G{N+1} draft를 작성한다.
4. 새 graph를 완전히 read-back한 뒤 obsolete unfinished Impl/Review/Summary만 archive한다. Planned
   behavior가 있으면 첫 Impl, 없으면 aggregate Review를 Triage child와 activation target으로 둔다.
5. 공통 작성 절차를 완료하고 새 Triage를 완료한다.

완료 기준: G{N+1}만 dispatchable하며 모든 desired behavior가 inherited, planned 또는 blocked다.

## `review-rework` 절차

1. Done Review의 terminal completion event, latest run metadata, body, children과 Summary direct parents를
   read-back한다. Run metadata의 canonical `findings`만 routing input으로 사용한다.
2. `correction-required`는 기존 effective behavior를 충족하는 Impl, `context-required`는 approved
   source를 재확인하는 Review, `decision-required`는 같은 G의 blocked Decision으로 materialize한다.
3. Completed card/body/link를 그대로 두고 source Review→새 work→next Review/Summary를 append한다.
   Next Review는 graph의 최초 `planning_baseline_sha`를 유지해 corrective commit만이 아니라 Issue 전체
   누적 diff를 다시 검토한다. 모든 blocking finding의 후속 contract가 정해진 뒤 next Review를 만든다.
4. `source_review`에 review key, finding별 verdict·appended card와 card별 idempotency key를 기록한다.
   Native child와 Summary parents를 read-back해 이미 존재하는 work를 재생성하지 않는다.
5. Appended Impl이 있으면 첫 Impl을 activation target으로 둔다. Context-only rework이면 next Review를
   activation target으로 둔다. Unresolved Decision-only rework이면 `ready_candidate: null`로 두고
   Decision을 `blocked`로 유지한다. Triage와 source history는 `done` 상태를 유지한다.
6. 공통 작성 절차의 draft/native 검증을 완료한다. Review-rework에서는 Triage completion을 다시
   실행하지 않는다.

완료 기준: 모든 finding에 disposition이 있고, historical card는 immutable·`done`, append된 graph가
검증되며 ready card는 최대 하나다.

## Helper

다음 명령은 `terminal`로 실행한다. `prepare-impl-fast-path`만 repo root에서 실행하고 나머지는 Skill
directory에서 실행한다. Fast path에서 `prepare-impl-fast-path` 성공 뒤 `impl-batch-template`을 별도로 다시
실행하지 않는다.

```text
python scripts/build_task_graph.py template --issue <number> --issue-url <url> --output .temp/task-graphs/<issue>/impl-1.json
python "$HERMES_KWC_PLUGIN_ROOT/skills/kwc-build-task-graph/scripts/build_task_graph.py" prepare-impl-fast-path --profile "${HERMES_KWC_FRESH_HOST_PROFILE:-${HERMES_PROFILE:-main}}" --board <board> --triage-id <triage-id> --issue <issue> --generation <generation> --output-dir <artifact-root>/fast-path --batch-output <artifact-root>/impl-card-batch.json
python scripts/build_task_graph.py impl-batch-template --triage <artifact-root>/fast-path/triage-body.json --triage-id <triage-id> --generation <generation> --output <artifact-root>/impl-card-batch.json
python scripts/build_task_graph.py apply-impl-completions --batch <artifact-root>/impl-card-batch.json --completions <artifact-root>/impl-card-completions.json --output <artifact-root>/impl-card-batch.json
python scripts/build_task_graph.py render-impl-batch --batch <artifact-root>/impl-card-batch.json --output-dir <artifact-root>/bodies
python scripts/build_task_graph.py validate-impl-batch <artifact-root>/impl-card-batch.json
python scripts/build_task_graph.py validate .temp/task-graphs/<issue>/impl-1.json
python scripts/build_task_graph.py validate-graph .temp/task-graphs/<issue>/graph.json --phase draft
python scripts/build_task_graph.py render-body .temp/task-graphs/<issue>/graph.json --card-key <card-key> --output .temp/task-graphs/<issue>/bodies/<card-key>.json
python scripts/build_task_graph.py validate-graph .temp/task-graphs/<issue>/graph.json --phase draft --source-review-result .temp/task-graphs/<issue>/source-review-result.json  # review-rework only
python scripts/build_task_graph.py requirement-diff --base <sha> --revised <sha> --path <requirement-path> --output .temp/requirement-rework/<issue>/comparison.json
python scripts/build_task_graph.py validate-graph .temp/task-graphs/<issue>/graph.json --phase native --source-review-result .temp/task-graphs/<issue>/source-review-result.json  # review-rework only
python tests/native_e2e.py
```

Validator는 schema·topology 일관성을 증명한다. Native status/link/ID는 read-back으로, behavior 완료는
checkpoint와 Reviewer evidence로 별도 증명한다.

## 보고

한국어로 mode, generation/root, requirement basis/revision, behavior classification, inherited evidence,
created/archived cards, native read-back, activation target, blocker와 next transition을 보고한다. 근거 없이
behavior를 implemented, card를 done, graph를 dispatchable이라고 보고하지 않는다.
