---
name: kwc-create-triage
description: KWC fresh agent가 profile SOUL·memory 없이 수행하는 새 leaf Issue를 검증된 구현 입력 문서로 변환한다.
version: 0.8.26
author: Amaazon project, Hermes Agent
license: MIT
platforms: [windows, linux, macos]
metadata:
  hermes:
    tags: [project-management, triage, kanban, requirements]
    related_skills: [kwc-orchestrator-contract, kwc-update-current-state, kwc-service-planning, kwc-sync-docs, kwc-build-task-graph, codebase-memory-mcp, semble-search]
requires_toolsets: [terminal, hermes-langchain]
---

> 이 Skill의 실행 정책과 helper는 Kanban Workflow Controller가 소유한다. PM Profile copy는 비교 기준일 뿐 실행 의존성이 아니다.

# Issue Triage 작성

Triage Card는 조사 일지나 가능성 목록이 아니다. Issue의 승인된 요구사항, 현재 구현의 확인된 gap,
보존할 observable, 구현 slice와 검증 기준을 연결하는 **검증된 구현 입력 문서**다. PM은 frozen
Triage만 사용해 self-contained Impl·Review 계약을 만들며, Coder가 Triage를 다시 해석하지 않게 한다.

Body의 정본 schema와 field 의미는
[`references/triage-contract.md`](references/triage-contract.md)가 소유한다. 이 Skill은 생성 순서와
완료 기준만 소유한다. 모든 helper는 [`../kwc-orchestrator-contract/references/script-usage.md`](../kwc-orchestrator-contract/references/script-usage.md)의 black-box
계약을 따른다. 정상 Triage 생성 중 helper source를 읽거나 검색하지 않는다.

이 Skill이 Triage-only 실행의 진입점이면 `run-workflow`를 추가로 load하지 않는다. 아래 순서는 권고가
아니라 tool-order contract다. 특히 MCP checkpoint를 통과하기 전 source read tool call을 구성하지 않는다.
첫 terminal command contract를 지키지 않았거나 아래 호출 예산·artifact mutation contract를 위반한
attempt는 body가 타당해도 성공이 아니다.

## 완료 상태

```text
intake → analysis → frozen
```

- `intake`: Issue identity, baseline, 목표, 변경 요구, 보존 observable, 제외 범위와 조사 시작점이 보인다.
- `analysis`: 직접 근거가 있는 gap, target disposition, 구현 slice와 verification matrix가 완성됐다.
- `frozen`: 미해결 결정이 없고 digest와 graph handoff가 고정됐다.

세 단계는 **같은 Triage card** identity를 사용한다. Native card에는 사용자에게 조기 checkpoint를
제공하는 valid `intake`와 downstream 정본인 `frozen`만 저장한다. `analysis`는 `.temp`의 검증된
중간 artifact이며 card에 별도로 저장·read-back하지 않는다. Native status는 graph authoring이 끝날
때까지 `triage`다.

## 사전 조건

1. Invocation prompt가 제공한 literal `PLANNING_ROOT`를 유일한 planning workspace로 고정한다. Hermes
   one-shot의 terminal tool은 process launch directory와 다른 이전 workspace에서 시작할 수 있으므로
   기본 terminal cwd를 신뢰하지 않는다. 첫 terminal 호출은 별도 `workdir` 없이
   `PLANNING_ROOT='<literal>'; cd "$PLANNING_ROOT" && pwd && git rev-parse --show-toplevel && git status --short --branch`
   를 실행한다. 첫 terminal 호출은 `pwd` 결과와 Git root를 함께 반환해야 한다. 출력 root가 literal path와
   다르면 mutation 없이 차단한다. 이 호출로 terminal session의
   현재 working directory를 planning workspace로 고정하고 이후 다른 `workdir`를 지정한 terminal 호출은 금지한다.
   archived card·active-work marker·다른 checkout에서 workspace를 복원하지 않는다.
2. `AGENTS.md`, Issue, `docs/current-state.md`, `docs/requirement/index.md`와 Issue가 지정한 requirement·guide
   문서만 먼저 직접 읽는다. 이 단계에서 읽을 수 있는 것은 정책·요구사항·현재 상태 **문서**뿐이다.
   문서 읽기와 production/test source 읽기를 같은 단계로 취급하지 않는다.
   `src/main/**`, `src/test/**`, migration, `package-info.java`와 그 밖의 구현 파일은 Issue가 exact path를
   제공해도 아직 읽지 않는다.
   Issue를 읽기 전 workspace filename search를 실행하지 않는다. 위치가 이미 고정된 문서를 찾기 위한
   `search_files`도 실행하지 않는다.
   Leaf Issue 본문·title·URL은 `issue_read(method=get)` 한 번으로 충분하다. Triage가 parent/sub-issue
   hierarchy 자체를 변경하거나 판정하는 요청이 아니면 `get_parent`와 `get_sub_issues`를 호출하지 않는다.
3. branch, planning HEAD와 clean worktree를 같은 planning workspace에서 read-back한다. 다른 checkout의
   dirty 상태는 이 실행의 admission 근거로 사용하지 않는다.
4. `current_state.py inspect` artifact가 같은 planning HEAD에서 `fresh`, clean, ancestor 관계임을 확인한다.
5. 같은 Issue의 active Triage가 정확히 하나인지 확인한다. 있으면 새로 만들지 않고 재개한다.
6. 아래 명령처럼 기존 path를 건너뛰어 `.temp/triage/<board>/issue-<number>/attempt-<n>/`의 새 attempt
   경로를 선택한다. 기존 attempt artifact를 overwrite하거나 현재 run의 입력으로 재사용하지 않는다.

이 문서에 필요한 Kanban 명령이 모두 있으므로 Kanban CLI syntax 검색과 `--help` 호출을 하지 않는다. Native preflight는
`hermes kanban --board <board> list --status triage --json` 한 번만 실행한다. 별도의 전체 active list를
읽지 않는다. active task가 없는 정상 경로에서는 archived task는 조회하지 않는다.
정상 생성에서 archived task body를 읽거나 Triage 근거로 사용하지 않는다. Archive는 이전 실행을 active
preflight에서 제외하기 위한 lifecycle 상태이지 새 Triage의 입력 artifact가 아니다. Native create의
idempotency key는 현재 새 attempt에 결속해 archived attempt와 충돌하지 않게 한다. 같은 attempt 안의 retry만
같은 key를 재사용한다.

Normal one-shot에 native Kanban tool schema가 직접 노출되지 않으면 아래의 고정 명령과 helper만
사용한다. 사용 가능한 CLI fallback을 찾기 위한 `tool_search`는 하지 않는다. Kanban `tool_describe`,
`hermes kanban --help`, subcommand `--help`는 정상·복구 경로 모두에서 실행하지 않는다. 이 Skill의
고정 명령으로 수행할 수 없는 mutation이면 추측하거나 syntax를 탐색하지 말고 typed blocker를 반환한다.

완료 기준: 모든 입력이 같은 planning HEAD에 결속되거나, Kanban mutation 없이 typed blocker를
반환했다.

## 명령

```text
TRIAGE_PY="$HERMES_KWC_PLUGIN_ROOT/skills/kwc-create-triage/scripts/triage.py"
CURRENT_STATE_PY="$HERMES_KWC_PLUGIN_ROOT/skills/kwc-update-current-state/scripts/current_state.py"
RUN_BASE=.temp/triage/<board>/issue-<number>
ATTEMPT=1
while test -e "$RUN_BASE/attempt-$ATTEMPT"; do ATTEMPT=$((ATTEMPT + 1)); done
RUN_DIR="$RUN_BASE/attempt-$ATTEMPT"
mkdir -p "$RUN_DIR"

python "$CURRENT_STATE_PY" inspect --repo . --document docs/current-state.md > "$RUN_DIR/current-state-inspection.json"
python "$TRIAGE_PY" compose-intake --issue <number> --title "<tracker title>" --issue-url <url> --planning-sha <sha> --current-state-sha <sha> --requirement-locator <path#heading> --current-state-locator <path#heading> --authoring-file "$RUN_DIR/intake-authoring.json" --repo . --output "$RUN_DIR/intake.json"
python "$TRIAGE_PY" compose-analysis --intake-file "$RUN_DIR/intake.json" --authoring-file "$RUN_DIR/analysis-authoring.json" --repo . --output "$RUN_DIR/analysis.json"
python "$TRIAGE_PY" freeze "$RUN_DIR/analysis.json" --current-state-inspection "$RUN_DIR/current-state-inspection.json" --output "$RUN_DIR/frozen.json"
python "$TRIAGE_PY" replace-card-body --task-id <id> --board <board> --body-file "$RUN_DIR/frozen.json"
python "$TRIAGE_PY" validate-card --task-id <id> --board <board> --expected-status triage --expected-assignee project-manager --expected-issue-number <number> --expected-issue-title "<tracker title>" --expected-issue-url <url> --current-state-inspection "$RUN_DIR/current-state-inspection.json" --report "$RUN_DIR/validate-card.json"
```

## 절차

### 1. Intake를 먼저 만든다

Issue·requirement·current-state 문서만 근거로 새 경로 `$RUN_DIR/intake-authoring.json`을 한 번의
`write_file`로 작성한다. 기존 canonical template을 생성·읽기·overwrite하지 않는다. Authoring 파일의
top-level key는 `intent`, `investigation_targets` 정확히 두 개다. `intent` 안에는 `goal`,
`change_requirements`, `preserved_observables`, `quality_gates`, `finalization_requirements`,
`out_of_scope`를 둔다. 특히 `out_of_scope`를 top-level에 두지 않는다. `compose-intake`가 이 작은 authoring
payload를 v7 scaffold에 결합하므로 canonical field 위치를 helper가 결정하고 완성된 `intake.json`만 native
card 입력으로 사용한다.

- `intent.goal`
- `change_requirements`: 실제로 달라져야 하는 결과
- `preserved_observables`: 리팩터링 중 유지할 API·error·cookie·transaction·persistence 의미
- `quality_gates`: 구현 뒤 실행할 검증 종류
- `finalization_requirements`: 문서·Issue lifecycle 종료 조건
- `out_of_scope`: source object가 아니라 non-empty string list다. 각 문자열에 범위 경계를 직접 적고
  `id`, `outcome`, `boundary`, `source` object를 만들지 않는다. downstream card 생성·activation 같은 실행 제한은
  제품 범위가 아니므로 이 목록이나 다른 body field에 넣지 않는다.
- Issue가 직접 지목했거나 위 항목 판정에 꼭 필요한 `investigation_targets`

`out_of_scope`를 제외한 structured 항목은 `locator`와 그 source가 실제 말하는 `claim`을 가진다. 요구사항에 이미 결정된 behavior는
Decision 후보가 아니라 preserved observable 또는 change requirement다.

`compose-intake`는 authoring shape, 전체 intake schema와 local locator를 한 번에 검사하고 성공할 때만
`intake.json`을 쓴다. 실패하면 report의 모든 error를 함께 읽고 `intake-authoring.json`에 consolidated
V4A patch를 최대 한 번 적용한 뒤 compose를 한 번만 재실행한다. 두 번째 compose도 실패하면 반복 보정하지
않고 typed blocker를 반환한다. 이 correction budget은 본문을 축약하기 위한 것이 아니라 field 위치나 locator
오류를 한 번에 바로잡기 위한 것이다.

모든 consolidated correction patch는 적용 전에 hunk를 유일하게 만든다. `observable_id`, `locator`처럼
반복 key만 단독 hunk로 사용하지 않는다. 같은 object의 `id`와 앞뒤 field를 context에 포함한다. JSON array의 마지막 항목을 삭제할
때는 바로 앞 항목의 trailing comma까지 같은 hunk에서 제거해 중간 문법 오류를 만들지 않는다. Patch가 적용되지
않은 경우도 correction call budget을 소비하므로 모호하거나 JSON을 깨뜨리는 첫 patch를 제출하지 않는다.

`compose-intake`가 통과하면 별도의 중복 intake validation 없이, active card가 없을 때만 다음처럼 생성하고
즉시 read-back한다.

```text
hermes kanban --board <board> create G1-Issue<number>-Triage --triage --assignee project-manager --workspace dir:<repo> --body-file "$RUN_DIR/intake.json" --idempotency-key <board>:issue-<number>:triage-v7:attempt-$ATTEMPT --json
```

완료 기준: source 분석을 깊게 시작하기 전에 native `triage` card 하나가 보이고, body가 valid intake다.
native intake create와 immediate read-back을 완료한 뒤에만 MCP checkpoint로 이동한다. create 응답과
immediate `show`에서 같은 task ID·`status=triage`·`assignee=project-manager`·`skills=[]`를 확인한다.
intake task ID가 없으면 MCP나 source confirmation을 시작하지 않는다.

### 2. Bounded analysis를 수행한다

항상 `triage-contract.md`를 기준으로 삼는다. `triage-decision-collaboration.md`는 실제 unresolved Decision
후보가 생겼을 때만, `cross-domain-minimum-capability.md`는 Issue/source 확인 뒤 cross-domain seam이
실제로 범위에 포함될 때만 읽는다. 가능성을 이유로 모든 reference를 선로드하지 않는다.

다음 stage를 순서대로 닫는다. 이전 stage가 끝나기 전 다음 stage의 tool call을 같은 turn에 섞지 않는다.

1. **Document/intake stage**: Issue와 위에서 허용한 문서만 읽고 intake를 완성·검증한다.
2. **Native intake checkpoint**: intake card를 생성하고 immediate read-back에서 task ID와 native fields를 확인한다.
3. **MCP checkpoint**: `index_status`를 먼저 호출한다. Fresh index일 때만 `search_graph`를 호출하고,
   Semble `search`는 두 경로 모두에서 호출한다.
4. **Source confirmation stage**: MCP가 좁힌 production source·test 후보를 한 번에 직접 읽는다.

MCP checkpoint 이전에는 production/test source path가 prompt나 Issue에 직접 적혀 있어도 읽지 않는다.
`tool_describe`, related Skill load, filename search는 MCP 호출을 대신하지 못한다. checkpoint를 건너뛰었다면
그 실행은 즉시 `TRIAGE_BLOCKED`이며, 뒤늦게 MCP를 호출해서 같은 attempt를 성공으로 바꾸지 않는다.

Runtime이 MCP를 deferred generic tool로만 노출하면 MCP checkpoint 직전에 필요한 MCP schema 전체를
**한 번의 batched `tool_describe`**로 확인할 수 있다. 대상은 `index_status`, `search_graph`, `trace_path`,
Semble `search`, `find_related`뿐이다. Schema describe는 실제 MCP 호출이 아니며, 같은 실행에서 두 번째
MCP `tool_describe`를 하지 않는다.

MCP checkpoint 뒤에는 Issue·requirement와 MCP가 지목한 exact path/symbol부터
`definition → caller/consumer → 가장 가까운 test` 순서로 읽는다. Exact locator가 없을 때만 해당 module
subtree에서 filename/symbol 검색을 한 번 한다.
Evidence locator의 repository-relative path는 direct read/MCP 결과의 path를 그대로 복사하고 기억으로
재조립하지 않는다. `validate --repo .`가 모든 local locator의 실제 path 존재를 검사한다.
Repository root의 Issue 번호 검색, 전체 requirement 파일 inventory, module 전체 file listing, 모든
`package-info.java` 검색은 하지 않는다. Issue와 known requirement locator를 먼저 읽고 누락된 exact
locator 하나를 찾을 때만 bounded search를 추가한다.

첫 source read 전에 Issue의 각 독립 scope bullet별로 `definition`, `caller/consumer`, `nearest test`와
필요한 DTO·public producer seam·error owner를 **메모리상의 source-closure checklist**로 먼저 열거한다.
Preserved observable과 계획한 verification scenario도 같은 checklist에 넣고, 각 항목마다 현재 동작을 실제로
실행하는 `src/main/**#caller-or-owner-symbol`을 하나 지정한다. Requirement, `current-state.md`, test 또는
provider/factory만 있고 이를 호출하는 production seam이 없으면 현재 구현된 observable로 간주하지 않는다.
파일을 하나 읽고 다음 파일을 고르는 방식으로 checklist를 확장하지 않는다. Unknown path/symbol은 한 번의
병렬 `search_files` turn에서 모두 찾고, production source는 최대 두 번의 batched read turn, test source는
한 번의 batched read turn으로 읽는다. 첫 batch 뒤 실제 import/call edge 때문에 누락된 locator가 생기면
남은 질문을 모두 열거한 단 한 번의 closure search/read turn만 허용한다. 품질 근거가 여전히 부족하면
추가 직렬 탐색 대신 typed blocker를 반환한다. 이 turn 예산은 파일 수를 줄이기 위한 것이 아니라 같은
필요한 파일을 더 적은 model round-trip으로 확인하기 위한 것이다.

Prompt·Issue·기억에서 얻은 Java path는 exact path처럼 보여도 아직 **unvalidated candidate**다. Initial
parallel search 결과나 successful direct read 결과로 존재가 확인되지 않은 candidate를 `read_file`에
전달하지 않는다. Initial search turn이 끝나면 production/test exact path 목록을 고정하고, 이후 검색은
첫 read batch가 드러낸 실제 import/call edge의 closure delta에만 사용한다. 각 search/read tool turn 직전에
`discovery_turns`, `production_read_turns`, `test_read_turns`, `closure_turns` 누적값을 확인한다. 다음 호출이
예산을 넘기면 호출하지 않고 즉시 typed blocker를 반환한다. Frozen body와 native replacement를 마친 뒤
사후에 예산 초과를 발견해 `TRIAGE_BLOCKED`로 바꾸는 것은 금지한다.

Direct read 결과의 exact `path`에 `#symbol`만 붙여 evidence locator를 만든다. FQCN, class 이름이나 기억으로
경로를 재구성하지 않고 `.java` 확장자를 생략하지 않는다. Directory locator는 조사 후보 묶음일 뿐 최종
evidence locator나 독립 investigation target으로 사용하지 않는다.

#### MCP로 조사 범위를 먼저 줄인다

Issue·requirement와 지정된 문서 locator를 직접 읽은 뒤, 첫 direct source read 전에 다음 MCP 호출을
반드시 완료한다. Related Skill을 다시 load하는 것으로 대체하지 말고 runtime에 노출된 exact tool을 직접
호출한다. MCP 결과는 locator 후보일 뿐 Triage evidence가 아니다. 최종 claim은 후보의 실제 source·test
범위를 planning workspace에서 직접 읽어 확인한다.

- `mcp__codebase_memory__index_status`
- fresh index일 때만 `mcp__codebase_memory__search_graph`
- `mcp__semble_mcp__search`

도구가 노출되지 않거나 호출이 실패하면 direct source 탐색 전에 `mcp-unavailable` typed fallback을 명시한다.
단, `index_status`가 `project not found or not indexed`를 반환하거나 indexed SHA freshness를 증명하지 못하면
그 결과가 Codebase Memory checkpoint의 종료 조건이다. 같은 project를 받는 `search_graph`는 성공할 수 없거나
신뢰할 수 없으므로 호출하지 않고 `mcp-index-unavailable` 또는 `mcp-index-stale`을 기록한 뒤 Semble과 direct
source fallback으로 진행한다. 실패가 확정된 `search_graph`를 형식적으로 호출하는 것은 정상 checkpoint가 아니다.

1. Codebase Memory `index_status`로 exact project의 indexed SHA가 planning SHA와 일치하는지 확인한다.
   일치하지 않거나 freshness를 증명할 수 없으면 graph 결과를 사용하지 않고 `mcp-index-stale`을 기록한 뒤
   direct source fallback을 사용한다. Triage 실행 중 index를 임의로 재생성하지 않는다.
2. `index_status`가 exact project와 fresh indexed SHA를 확인한 경우에만 `search_graph` 한 번에 investigation
   target의 definition·public interface·adapter·caller·nearest test 후보를 함께 찾는다. 결과가 잘렸을 때만
   query를 path/module로 좁혀 한 번 더 실행한다. 이미 exact symbol을 찾았으면 같은 질문을 `search_files`로
   반복하지 않는다.
3. `trace_path`는 cross-module seam이나 caller 의미가 불명확할 때만 depth 2로 실행한다. 조회와 command
   call path를 분리하는 데 필요한 symbol만 대상으로 하고 broad impact inventory를 만들지 않는다.
4. 이름을 모르는 기존 convention이나 유사 공개 contract는 Semble `search` 한 번으로 top 5 후보만 찾는다.
   가장 가까운 후보 하나에만 `find_related`를 사용할 수 있다. 반환된 file/line으로 바로 이동해 직접 읽고,
   같은 의미의 검색어를 바꿔 반복하지 않는다.
5. 서로 독립적인 MCP 조회는 같은 tool turn에 병렬 요청한다. 그 결과로 확정된 source·test 후보도 다음
   tool turn에 일괄 direct read한다. 파일 하나를 읽은 뒤 모델이 다음 파일을 다시 선택하는 직렬 탐색을 피한다.

정상 analysis의 Codebase Memory 호출은 fresh index 경로에서 최대 4회(`index_status` 1, `search_graph` 1~2,
`trace_path` 0~1), unavailable/stale index 경로에서 정확히 `index_status` 1회다.
Semble 호출은 최대 2회(`search` 1, 선택적 `find_related` 1)다. 이 예산을 초과해야만 완전성을 증명할 수 있으면
검색을 계속하지 말고 누락된 질문과 필요한 추가 범위를 typed blocker로 남긴다. 최종 보고에는 MCP별 실제
호출 수, direct source read 수와 fallback 여부를 포함한다.
`search_graph` 3회째 호출은 금지한다. 호출 직전에 누적 counter를 확인하고, 이미 2회면 추가 검색 대신
typed blocker를 반환한다. 최종 성공 판정 전에 모든 MCP counter를 예산과 다시 대조한다.

Cross-domain error ownership gap에서는 producer의 현재 공개 API와 공개 error type을 exact locator로
직접 확인한다. 각 caller를 읽어 absence가 정상 조회 결과인지 command 실패인지 먼저 분류한다. 조회 화면이
missing item을 unavailable DTO로 보존할 수 있으면 tolerant query 계약을 유지하고, 추가·수량 변경·병합처럼
Offer fact 없이는 invariant를 판정할 수 없는 command 경로만 required lookup/error 계약으로 분리한다.
두 의미를 하나의 전역 exception 또는 하나의 전역 nullable/result 계약으로 합치지 않는다.

Producer 공개 error seam이 없더라도 즉시 Decision으로 보내지 않는다. 동일 저장소의 기존 공개 contract에서
같은 종류의 required lookup·producer-owned exception·error-bearing result 사용례를 한 번 bounded search하고,
repository convention과 최소 공개 seam으로 후보 하나를 제거할 수 있는지 확인한다. 그래도 둘 이상의 준수
가능한 공개 결과가 남으면 freeze하지 않고 Issue가 요구한 대로 `analysis`에 실제 contract Decision과
`decision-required` blocker를 남긴다. "producer error를 사용하거나 consumer result로 변환"처럼 둘 이상의
미결 구현 결과를 frozen requirement·gap·slice에 남기지 않는다.

본문에는 조사 과정이 아니라 구현에 필요한 결론만 남긴다.

Issue의 numbered scope와 각 bullet은 생성할 `change_requirements`의 입력 coverage다. 제목 하나를 넓은
requirement 하나로 요약한 뒤 그 안의 독립 bullet을 누락하지 않는다. 각 bullet은 최종 analysis에서
`confirmed gap + slice`, 확인된 기존 동작을 유지하는 observable/target disposition, 또는 근거 있는
out-of-scope 중 하나로 닫혀야 한다. 특히 하나의 scope가 HTTP binding과 DTO boundary처럼 서로 다른
책임을 함께 요구하면 각각 현재 source를 확인한다. Controller가 `@RequestBody` type을 application service에
직접 전달하면 그 DTO 선언과 service signature를 읽고 transport-neutral command/result 분리 위반인지
판정한다. Issue의 "조사 시작점" 목록은 허용 범위의 끝이 아니므로 시작점이 참조하는 DTO·caller·consumer를
따라가야 한다.

- `confirmed_gaps`: 현재 상태와 요구 상태가 다른 경우만 기록한다.
- `target_dispositions`: 모든 investigation target을 `gap | preserve | compliant | out-of-scope`로 닫는다.
- `preserved_observables`: 하나의 row는 함께 회귀하고 같은 verification subject로 검증할 수 있는 하나의
  응집된 동작만 가진다. 서로 다른 HTTP 계약, ownership/lifecycle, transaction/locking, cleanup/idempotency,
  query absence와 module-boundary 의미를 row 수를 줄이기 위해 하나로 묶지 않는다. 서로 다른 test family가
  필요하거나 독립적으로 깨질 수 있으면 별도 observable로 유지한다. Trigger 또는 module owner가 다르면
  같은 lifecycle 문장에 있더라도 별도 observable로 나눈다.
  Cross-module capability가 정상 조회 fact/projection과 error ownership을 함께 제공하면 별도 observable로
  나눈다. Change requirement 또는 source claim이 가격·판매 상태·재고·구매 제한 중 둘 이상을 이름 붙이면
  같은 fact들을 모두 명시하는 전용 정상 조회 observable을 만들고 Cart invariant나 error code 보존에
  흡수하지 않는다.
- `verification_matrix`: preserved observable마다 **정확히 한 row**로 기존 coverage와 남은 검증을 분리한다.
  하나의 observable에 현재 test와 새 contract/structure 검증처럼 여러 확인 축이 있어도 row를 나누지 말고,
  같은 row의 `evidence`와 `required_verification`에 합친다. 동일 `observable_id`를 가진 두 번째
  `VERIFY-*` row를 만들지 않는다. 각 row의 `production_seam`은 provider나 DTO가 아니라 해당 observable을
  실제 실행하는 현재 committed caller 또는 behavior owner여야 한다.
- `contract_changes`: 이번 Issue가 실제 cross-domain seam을 변경할 때만 기록한다.
- `document_impacts`: 실제로 검토하거나 갱신할 문서만 기록한다.

`covered`는 정확한 test scenario가 해당 observable을 직접 assert할 때만 사용한다. 일부만 검증하면
`partial`, test가 없으면 `missing`이다. Source method나 test class 이름만으로 `covered`로 판정하지 않는다.
Requirement가 다른 module의 caller에게 맡긴 behavior인데 target Issue에 그 production seam이 없으면 local
coverage gap이나 test-only slice를 만들지 않는다. Requirement가 owner를 이미 고정했다면 해당 Issue의
out-of-scope/cross-domain follow-up으로 분류하고, source로 owner를 하나로 줄일 수 없을 때만 Decision으로 올린다.

Evidence batch가 끝나면 새 경로 `$RUN_DIR/analysis-authoring.json`을 한 번의 `write_file`로 작성한다.
Authoring 파일에는 intake에서 이미 확정한 identity, baseline, `intent`, `investigation_targets`, `phase`,
`frozen_digest`, `handoff` 전체를 반복하지 않는다. top-level key는 `intake_amendments`, `confirmed_gaps`, `target_dispositions`,
`implementation_slices`, `verification_matrix`, `contract_changes`, `document_impacts`, `open_decisions` 정확히
여덟 개다. `intake_amendments`는 `requirement_locators`, `current_state_locators`, `intent_entries`,
`investigation_targets` 정확히 네 field를 가지며, 변경이 없으면 각 값을 빈 list로 둔다. Source 확인 뒤 locator
list는 최종 전체 list로 교체할 수 있고, `intent_entries`는 `section + id + changes`로 해당 stable ID의
`source`만 정정한다. Analysis에서는 change requirement outcome, preserved observable behavior와 quality gate
의미를 수정하지 않는다. Intake가 독립 책임을 합쳤거나 요구사항을 빠뜨렸다면 짧은 amendment로 덮어쓰지 않고
현재 attempt를 block하여 새 intake에서 원자적으로 다시 작성한다. `investigation_targets`는 `id + changes`로
해당 stable ID만 수정하며 전체 `intent`나
target list를 다시 쓰지 않는다. `compose-analysis`가 검증된
`intake.json`을 재사용하고 amendment를 적용한 뒤 phase·handoff를 결정론적으로 설정해
canonical `analysis.json`을 만든다. 따라서 본문 내용을 줄이지 않으면서 중복 authoring output만 제거한다.

필드나 collection을 하나씩 추가하는 incremental patch를 금지한다. 첫 `compose-analysis` 결과와 아래 semantic
audit 결과를 같은 correction batch로 수집한다. 둘 중 하나라도 실패하면
모든 structural·locator·semantic error를 한 번에 분석해 consolidated patch 한 번으로 수정한다.
`UNRESOLVED_IMPLEMENTATION_ALTERNATIVE`에는 validator가 함께 반환한
`UNRESOLVED_IMPLEMENTATION_ALTERNATIVE:<field-path>:<marker>`를 정본 locator로 사용한다. broad text search로
다른 조건문이나 근거 문장을 추측해 고치지 말고, 표시된 authoring field의 실제 미결 구현 선택만 확정한다.
두 번째 compose 또는 semantic audit도 실패하면 반복
보정하지 않고 typed blocker를 반환한다.
보정은 반드시 V4A `patch` tool call 한 번으로 수행한다. terminal·Python rewrite로
`analysis-authoring.json`이나 `analysis.json`을 수정하지 않는다.
실패한 rewrite 시도도 mutation contract 위반으로 계산하며 같은 attempt를 성공으로 보고하지 않는다.
Patch target은 `analysis-authoring.json`을 작성한 `write_file` 결과의 exact absolute `resolved_path`를 그대로 사용한다.
`RUN_DIR` 문자열을 기억으로 다시 조합하지 않는다. Patch tool이 `success=false`를 반환하면 correction budget이
소진된 것이므로 즉시 `TRIAGE_BLOCKED`로 끝내며, 고친 path로 두 번째 patch를 제출하지 않는다.
`DUPLICATE_VERIFICATION_OBSERVABLE:<OBS-ID>`를 보정할 때는 중복 row의 evidence·coverage gap·required verification을
하나의 기존 row로 합치고, acceptance와 contract의 `verification_ids`를 그 surviving ID로 바꾼 뒤 중복 object 전체를
삭제한다. reference만 지우거나 duplicate row를 남기면 같은 오류가 유지된다.
Windows command-line 길이를 맞추려고 `investigation_targets`, `target_dispositions`, `document_impacts` 또는
evidence를 삭제·축약하지 않는다. `replace-card-body`가 frozen JSON을 `--body-file`로 전달하므로 body 크기는
CLI 인자 길이의 제약을 받지 않는다. 카드가 커서 mutation이 실패하면 내용을 훼손하지 말고 typed blocker로
중단한다.

### 3. 구현 slice를 만든다

각 confirmed gap은 정확히 하나 이상의 `implementation_slices[].gap_ids`에 연결한다. Slice는 하나의
독립 검증 가능한 outcome을 가지며 다음을 포함한다.

- scope와 explicit out-of-scope
- requirement와 preserved observable을 연결한 acceptance
- acceptance마다 verification ID
- `depends_on`은 항상 빈 배열. Triage는 독립 outcome만 고정하고 semantic dependency와 실행 topology는
  `build-task-graph`의 dependency audit가 소유한다.

서로 다른 책임을 하나의 slice에 섞지 않는다. 예외 class 제거와 cross-domain port 재설계처럼 검증
범위가 다르면 분리한다.

Issue의 공통 test·regression 요구를 독립 구현 gap으로 승격해 generic verification-only gap이나 slice를 만들지 않는다.
변경 seam의 test는 해당 production slice의 scope·acceptance·verification에 함께 둔다. Test-only slice는
Issue가 이미 구현된 baseline behavior의 독립 검증을 명시적으로 요구한 경우에만 허용하며, 그때도 exact
baseline production seam과 독립 acceptance가 있어야 한다.

### 4. Decision을 엄격히 제한한다

다음 조건을 모두 만족할 때만 `open_decisions`를 만든다.

1. Issue, requirement, guide와 ADR의 고정 제약을 모두 적용했다.
2. 두 개 이상의 준수 가능한 결과가 남는다.
3. 모든 option이 연결된 preserved observable을 보존한다.
4. 선택이 공개 contract, ownership 또는 사용자 동작을 실제로 바꾼다.
5. PM이 기존 source만으로 하나를 제거할 수 없다.

Implementation seam·package 배치처럼 observable이 고정된 선택은 slice constraint이며 product
Decision이 아니다. Pending/deferred Decision이 있으면 phase는 `analysis`, blocker는
`decision-required`, graph gate는 닫힌다. `service-planning`에서 사용자 승인과 requirement read-back을
완료한 뒤 같은 Triage card를 갱신한다.

### 5. Analysis artifact를 검증한다

`phase: analysis`, `handoff.blocker: freeze-required`인 `.temp` artifact를 validator로 통과시킨다.
중간 상태를 native card에 복제하지 않고 곧바로 freeze 입력으로 사용한다.
Freeze 전에 semantic audit까지 통과해야 한다. validator를 먼저 고치고 나중에 semantic typo를 발견하는
직렬 보정은 금지한다. 첫 pass의 모든 finding을 단일 correction budget에 합친다.
Semantic audit은 required/preserved contract가 본문에 **명시적으로 존재하는지**를 positive predicate로
검사한다. 특정 금지 문구가 그대로 존재하지 않는다는 문자열 부재 검사로 계약 보존을 추론하지 않는다.
그런 brittle negative-text expression의 실패는 artifact 결함이 아니며 correction budget을 소모하게 만들지
않는다.

완료 기준:

- 모든 change requirement가 confirmed gap에 연결됨
- 모든 investigation target이 disposition됨
- 모든 gap이 slice에 연결됨
- 모든 preserved observable에 verification row가 있음
- 모든 verification row에 직접 읽은 current production seam이 있음
- 모든 acceptance가 requirement와 verification을 가짐
- unsourced target behavior, 과장된 coverage와 불필요한 Decision이 없음

### 6. Freeze하고 의미 감사를 수행한다

`freeze`는 canonical current-state inspection을 다시 검사하고 `phase: frozen`, null blocker,
`handoff.graph_allowed: true`, SHA-256 digest를 만든다. Frozen JSON으로 intake card body를 한 번
교체할 때는 위 `replace-card-body` helper만 실행한다. helper 뒤 별도 `show`를 실행하지 않고 곧바로
`validate-card`를 실행해 frozen mutation의 유일한 authoritative read-back으로 사용한다.

정상 mutation/read-back 예산은 `intake create → immediate read-back → frozen edit → validate-card read-back`
두 native write와 두 authoritative read-back이다. `analysis` native 저장, 같은 body의 별도 복사·재조회,
중복 board listing은 실패 복구가 아닌 정상 경로에서 수행하지 않는다.

구조 validation과 별개로 다음 semantic audit를 수행한다. 이를 위해 임시 audit script를 생성·수정하지
않고, frozen body와 이미 읽은 exact source evidence를 대조한다.

- 각 `required_state`와 acceptance가 Issue, requirement 또는 승인 Decision에서 유래했는가?
- Issue scope의 각 독립 bullet이 gap/slice, preserve/compliant disposition 또는 근거 있는 out-of-scope로
  닫혔는가? 넓은 requirement 문구만 있고 하위 책임 하나가 사라지지 않았는가?
- preserved observable의 현재 동작을 일반 계층 규칙만으로 제거 대상 gap으로 뒤집지 않았는가?
- preserved observable을 Decision option이 다시 열지 않는가?
Evidence locator가 claim을 직접 지지하는가?
같은 verification row에서 같은 evidence locator를 두 번 사용하거나 같은 path#symbol·path#test-class를
서로 다른 claim으로 반복하지 않았는가? 같은 규칙을 confirmed gap·contract change·document impact·Decision의
evidence/rules 목록에도 적용했는가? 한 locator가 여러 사실을 지지하면 claim 하나로 합쳤는가?
다른 confirmed locator의 claim을 옮겨 적지 않았는가?
- 모든 verification row의 `production_seam`이 observable을 실제 실행하는 caller/owner인가? Provider·DTO·test만
  존재하거나 다른 module 소유 behavior를 local test-only slice로 바꾸지 않았는가?
- `covered`가 실제 assertion 범위를 과장하지 않는가?
- slice 하나가 하나의 책임과 검증 범위를 가지는가?
- 공통 quality gate를 generic verification-only gap/slice로 중복하지 않고 각 production slice가 자기 변경과
  회귀 test를 함께 소유하는가? Test-only slice라면 Issue가 기존 baseline 검증을 독립 범위로 직접 요구하는가?
- Frozen Triage만 읽어 새 product 해석 없이 Impl·Review 계약을 만들 수 있는가?

이 semantic audit는 analysis correction 전에 한 번, correction 뒤에 한 번 수행한다. 모두 통과한 analysis만
freeze한다. Frozen audit는 freeze가 의미를 바꾸지 않았는지 확인하는 read-only 확인이며, 여기서 새 finding이
생기면 card edit을 하지 않고 `TRIAGE_BLOCKED`로 끝낸다.

### 7. 성공 admission을 마지막으로 다시 확인한다

`TRIAGE_REGENERATED`를 출력하기 전에 다음을 모두 대조한다.

- 첫 terminal command가 literal `PLANNING_ROOT` 변수, `cd "$PLANNING_ROOT"`, `pwd`,
  `git rev-parse --show-toplevel`, `git status --short --branch`를 포함했고 tool의 `workdir`를 지정하지 않았는가?
- native intake create와 immediate read-back이 MCP 및 production/test source read보다 먼저 있었는가?
- production/test source read가 MCP checkpoint 뒤에만 있었는가?
- `index_status = 1`이고 fresh index면 `search_graph = 1~2`, unavailable/stale index면 `search_graph = 0`인가?
  `trace_path <= 1`, Semble `search = 1`, `find_related <= 1`인가?
- `analysis-authoring.json`은 `write_file` 1회, 보정은 V4A patch 0~1회이고 `analysis.json`은
  `compose-analysis`의 성공 output으로만 생성됐으며 terminal rewrite가 0회인가?
- source locator discovery가 병렬 search 1회, production read 1~2회, test read 1회와 선택적 closure turn
  이내이며 exact direct-read path를 사용했는가?
- `intake-authoring.json`은 `write_file` 1회, 보정은 V4A patch 0~1회이고 archived task 조회·본문 read가 0회인가?
- native write/read-back이 각각 2회이고 failed mutation attempt가 0회인가?

하나라도 아니면 생성된 card가 있어도 `TRIAGE_BLOCKED`로 보고하고 task ID와 위반 항목을 명시한다.

## 연관 Skill

| Skill | 호출 조건 | 복귀 조건 |
|---|---|---|
| `service-planning` | 고정 제약 적용 후에도 실제 product/public-contract 선택지가 둘 이상 남음 | 사용자 승인, requirement와 Issue read-back |
| `sync-docs` | 승인 requirement와 파생 문서·Issue가 충돌함 | 영향 문서 동기화와 read-back |
| `build-task-graph` | Triage가 frozen이고 semantic audit가 통과함 | 다음 단계에서 v7 consumer 계약을 명시적으로 적용 |

## 금지된 대체

- Coordinator가 PM 대신 final body를 수동 보정하지 않는다.
- 이미 결정된 behavior를 Decision option으로 다시 열지 않는다.
- 일시적인 coordinator 실행 범위(예: "이번 Triage에서는 downstream card를 만들지 않는다")를 frozen
  제품 계약에 넣지 않는다. 그 제한은 대화·실행 경계이지 Impl·Review 입력이 아니다.
- 정상 implementation inventory를 완전성 목적으로 본문에 나열하지 않는다.
- Cross-domain data/error/consistency 표를 관련 없는 Issue에 강제하지 않는다.
- Validator 통과를 semantic approval로 보고하지 않는다.
- `current-state.md`, source, test, migration을 Triage 실행 중 수정하지 않는다.
