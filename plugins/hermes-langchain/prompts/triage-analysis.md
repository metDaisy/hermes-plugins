Triage 단계: analysis

검증된 intake와 source confirmation을 근거로 gap, target disposition, 독립 검증 가능한 implementation slice, verification matrix, contract/document impact, open decision을 작성한다. Gap evidence, verification evidence, production seam과 contract-change evidence에는 source confirmation에서 제출한 locator만 사용한다. 요구사항·정책 충돌 또는 미확정 의미는 구현으로 추측하지 말고 open_decisions에 기록한다.

Target의 layer, package, contract ownership 또는 convention 질문은 source confirmation이 governing document와 production locator를 대조해 각 dimension을 명시적으로 충족했다고 확인한 경우에만 `compliant`로 닫는다. Symbol·adapter·caller가 존재한다는 사실은 capability existence 근거일 뿐 placement·ownership compliance 근거가 아니다. Published producer seam이 존재해도 consumer policy port의 잘못된 layer/package는 별도 gap과 independently verifiable slice로 유지한다.

Cross-module public seam은 source confirmation에서 exact producer-owned production symbol이 `implemented`이고 `public_contract.status=published`로 확인된 경우에만 existing capability로 소비한다. Published producer locator는 이 Issue의 scope에서 `preserve|verify`만 가능하며 `create|modify|move-source|move-target|delete` 대상으로 넣지 않는다. Producer 자체 변경이 필요하면 source confirmation의 `published` 판정이 잘못된 것이므로 먼저 source stage를 고친다. Requirement 문서의 code/message 정의만으로 public implementation contract가 존재한다고 추론하지 않는다.

`public_contract.status=approved-unimplemented`이면 blocker로 바꾸지 않는다. `contract_changes`에 승인된 minimum contract와 현재 absent/partial producer seam evidence를 함께 기록하고, producer public bridge와 consumer integration follow-up을 implementation slice에 포함한다. Producer 전체 내부 구현을 자동 prerequisite로 만들지 않는다. Issue의 `내부 구현·Repository 직접 변경` 제외는 application public API bridge 제외와 같지 않다. `public API bridge`가 정말 out-of-scope인지 문구를 정확히 구분한다.

`public_contract.status=undecided`이거나 필요한 seam이 `unknown`이면 open decision/blocker 후보로 검토한다. 단, open decision은 user-owned product policy, observable semantics, authorization, consistency 또는 cross-domain contract 의미가 requirement/Issue에서 실제로 미확정이고 서로 다른 선택이 target behavior를 바꿀 때만 작성한다. Source confirmation의 `open_questions`를 자동으로 open decision으로 복사하지 않는다. Published contract를 사용하는 내부 port 분리, package·method shape, aggregate method 설계, 내부 class 삭제·유지, test strategy처럼 여러 구현이 가능한 리팩터링 선택은 implementation slice의 scope·acceptance·verification으로 닫고 사용자 결정으로 만들지 않는다. 이미 승인된 minimum contract를 production symbol 부재만으로 다시 사용자 결정으로 열지 않는다. 이 조건을 충족하는 unresolved decision이 있을 때만 `handoff.graph_allowed=false`로 중단한다.

Open decision의 모든 option은 `preserves_observable_ids`에 적은 observable을 실제로 그대로 보존해야 한다. 공개 error code, HTTP status, message 또는 API 의미를 바꾸는 선택지를 해당 observable 보존으로 표시하지 않는다. 현재 Issue가 그 observable을 preserved로 고정했다면 그런 선택지는 Triage decision으로 제시하지 말고 upstream requirement rework가 필요한 별도 blocker로 분류한다. 예를 들어 published producer error를 consumer-owned error로 바꾸는 선택은 기존 error observable을 보존하지 않으므로 보존 선택지로 허용하지 않는다.

`kwc-triage-analysis-authoring-v1` JSON을 작성한다. top-level은 `schema`, `analysis`이며 `analysis`에는 정확히 `intake_amendments`, `confirmed_gaps`, `target_dispositions`, `implementation_slices`, `verification_matrix`, `contract_changes`, `document_impacts`, `open_decisions`를 둔다.

필드 계약은 다음과 같다. 아래 필드만 사용한다.

- `intake_amendments`: object. 변경이 없어도 `requirement_locators`, `current_state_locators`, `intent_entries`, `investigation_targets`를 모두 빈 배열로 둔다.
  - `intent_entries[]`: `section`, `id`, `changes`
  - `section`은 정확히 `change_requirements|preserved_observables|quality_gates` 중 하나다. `intent.` prefix를 붙이지 않는다.
  - `changes`는 string이나 배열이 아니라 non-empty object이며 `changes`에는 `source`만 허용한다. Analysis는 intake의 outcome·behavior·quality gate 의미를 수정하지 않는다. Source 확인으로 intake 의미가 잘못됐거나 빠진 사실이 드러나면 축약한 amendment로 덮어쓰지 말고 현재 attempt를 block하여 새 intake에서 requirement를 원자적으로 다시 작성한다.
  - `investigation_targets[]`: `id`, `changes`
- `confirmed_gaps[]`: `id`, `subject`, `current_state`, `required_state`, `evidence`, `rules`, `requirement_ids`, `affected_observable_ids`
  - `requirement_ids`는 intake `change_requirements[].id`만 참조하고, `affected_observable_ids`는 `preserved_observables[].id`만 참조한다.
- `target_dispositions[]`: `target_id`, `disposition`, `finding_ids`, `rationale`
  - `disposition`: `gap|preserve|compliant|out-of-scope`
- `verification_matrix[]`: `id`, `observable_id`, `current_coverage`, `evidence`, `production_seam`, `coverage_gap`, `required_verification`
  - preserved observable마다 정확히 한 행을 작성한다. `observable_id`는 `preserved_observables[].id`이고 `required_verification`은 비어 있지 않은 문자열 배열이다.
  - `current_coverage`: `covered|partial|missing`
  - `production_seam`: source object(`locator`, `claim`). `locator`는 반드시 exact `src/main/java/.../Type.java#method`이며 file-only 또는 class-only locator는 허용하지 않는다. Planned method라면 confirmed gap의 exact required-state symbol과 일치해야 한다.
  - `covered`이면 test evidence가 필요하고 `coverage_gap=null`, `partial`이면 test evidence와 non-empty `coverage_gap`, `missing`이면 `evidence=[]`와 non-empty `coverage_gap`을 사용한다.
  - 같은 verification row에서 같은 evidence locator를 두 번 사용하지 않는다. 여기서 locator의 동일성은 문자열 전체가 같을 때뿐 아니라 같은 `path#symbol` 또는 같은 `path#test-class`를 서로 다른 claim으로 반복하는 경우도 포함한다. 같은 규칙은 confirmed gap·contract change·document impact·Decision의 evidence/rules 목록에도 적용한다. 한 locator가 여러 사실을 직접 확인하면 evidence object 하나의 claim에 그 사실을 함께 적고, 다른 confirmed locator의 claim을 옮겨 적지 않는다. 제출 전 각 evidence-bearing row의 evidence/rules를 locator 기준으로 dedupe하고 중복 개수가 0인지 세어 확인한다.
- `contract_changes[]`: `id`, `consumer`, `producer`, `change`, `evidence`, `verification_ids`
- `implementation_slices[]`: `id`, `title`, `outcome`, `gap_ids`, `scope`, `out_of_scope`, `acceptance`, `contract_change_ids`, `depends_on`
  - `scope`는 변경 계획의 executable inventory다. Gap evidence의 production method/file이 gap 설명에 등장하면 `create|modify|move-source|move-target|delete|preserve|verify` 중 하나와 exact locator를 같은 항목에 적는다. 예: `modify: src/main/java/.../OrderService.java#changeQuantity - ...`. 누락하면 `METHOD_PLAN_DISPOSITION_REQUIRED` 또는 `ARTIFACT_PLAN_DISPOSITION_REQUIRED`다.
  - `required_state`에 evidence에 없는 새 `Owner#method`를 적었다면 `scope`에 parameter를 포함한 exact `Owner#method(...)` signature를 쓰고, verification row 하나의 planned `production_seam.locator`도 같은 owner/method로 맞춘다. 누락하면 `PLANNED_METHOD_SIGNATURE_REQUIRED` 또는 `PLANNED_METHOD_VERIFICATION_SEAM_REQUIRED`다.
  - HTTP·DTO·command·result gap의 `scope`에는 repository의 source-confirmed architecture와 naming convention을 따르는 HTTP request, HTTP response, application command, application result artifact의 exact planned `src/main/java/.../*.java` path를 각각 적는다. 특정 `presentation|application` directory나 `Request|Response|Command|Result|Service` suffix를 가정하지 않는다. Evidence에 있는 production `Type.java#method`는 `src/main/java/.../Type.java#method(...)` signature로 적고, 연결한 contract change의 consumer/producer/change에는 request, response, command, result를 모두 명시한다. 누락하면 `HTTP_DTO_OPERATION_SIGNATURE_PLAN_REQUIRED` 계열 오류다.
  - `acceptance[]`: `id`, `criterion`, `requirement_ids`, `observable_ids`, `verification_ids`
  - 각 acceptance criterion은 연결된 gap `required_state`의 구체적 입력 종류, named exception/error type, error code/status/message, identity와 observable result를 그대로 보존한다. `DomainInputException`처럼 이름이 있는 오류를 "잘못된 입력을 거부한다"로 축약하거나 cookie/header/path variable처럼 서로 다른 입력 owner를 하나의 일반 DTO 표현으로 합치지 않는다.
  - acceptance의 `requirement_ids`는 `change_requirements[].id`, `observable_ids`는 `preserved_observables[].id`, `verification_ids`는 `verification_matrix[].id`만 참조한다.
  - verification-only slice가 아닌 acceptance가 하나의 production seam으로 수렴하면 criterion 본문에 exact `Owner#method` token을 직접 적는다. 누락하면 `ACCEPTANCE_PRODUCTION_METHOD_REQUIRED`다.
  - 각 acceptance가 참조하는 verification row의 `production_seam.locator`는 하나의 동일한 operation으로 수렴해야 한다. 서로 다른 operation을 검증하면 acceptance를 operation별로 분리한다.
  - HTTP binding/input-format outcome과 application command/result boundary outcome은 서로 다른 artifact owner와 검증 seam을 가지므로 같은 slice에 넣지 않는다. 같은 Issue bullet에서 출발했더라도 각각 독립 slice로 작성한다.
  - production 변경을 소유하지 않는 최종 integration/regression slice만 `scope`에 exact marker `production change 없음; verification만 소유`를 포함할 수 있으며, 이 slice는 여러 production seam을 함께 검증할 수 있다.
  - Change requirement가 여러 production slice 완료 뒤의 Modulith·layer·cross-slice 구조 검증을 요구하면 마지막 finalization slice 하나로 분리한다. 이 slice의 scope에는 exact marker `production change 없음; verification만 소유`와 `verify:` 항목만 두고 production `create|modify|move|delete`를 넣지 않으며 `depends_on=[]`를 유지한다.
  - Finalization의 모든 `verify:` selector는 이 slice acceptance가 참조하는 `verification_matrix[].required_verification`에도 exact하게 존재해야 한다. 구조 검증을 scope에만 적고 verification matrix에서 누락하지 않는다.
  - Package/module seam을 변경하면 repository의 실제 architecture test source에서 적용 가능한 test method selector를 모두 확인해 별도 `verify:` 항목과 `required_verification`에 기록한다. 서로 다른 boundary·layer 규칙을 검증하는 method가 여러 개면 모두 포함하고 하나로 축약하지 않는다.
  - verification-only slice에서도 모든 `verify:` scope 항목은 selector만 적지 말고 무엇을 검증하는지 한국어 설명을 함께 쓴다. 예: `verify: 모듈 경계를 검증한다 - com.example.ArchitectureTest#verifyModules`.
  - `required_verification`의 executable test selector는 canonical `FQCN#method`를 한 번만 기록한다. 동일 selector의 path·short-name·괄호 variant를 함께 나열하지 않는다.
- `document_impacts[]`: `document`, `disposition`, `evidence`, `action`
  - `evidence`는 source object 배열이다. `not-applicable`을 제외한 disposition은 하나 이상의 evidence가 필요하다.
  - `disposition`: `update|no-change|not-applicable|blocked`; `update`만 non-null `action`을 사용하고 나머지는 `action=null`이다.
  - baseline의 모든 requirement/current-state document와 source confirmation에서 직접 확인한 모든 `kind=document` evidence를 정확히 한 행으로 닫는다. `document`는 fragment 없는 repo-relative path이고, `not-applicable`이 아닌 각 행의 evidence에는 같은 document의 locator를 하나 이상 둔다. 검토했지만 수정이 필요 없으면 생략하지 말고 `no-change`로 기록한다.
- `open_decisions[]`: `id`, `question`, `why_unresolved`, `evidence`, `affected_observable_ids`, `options`, `owner`, `status`, `approved_option_id`
  - `options[]`: `id`, `choice`, `impact`, `preserves_observable_ids`
  - `owner`는 `user`; `status`는 `pending|approved|rejected|deferred`; `approved`만 유효한 `approved_option_id`를 사용하고 나머지는 `null`이다.
  - 각 decision의 `evidence`에는 requirement, Issue 또는 architecture source가 실제 ambiguity를 남겼음을 보여 주는 intake authority source를 하나 이상 포함한다. Production source만으로 구현 선택을 user decision으로 만들지 않는다.
- 모든 source object는 정확히 `locator`, `claim`만 가진다. Source confirmation에서 복사할 때도 `kind`를 제거한다.

모든 target을 disposition으로 닫고, 각 gap을 Slice에 연결하며, 각 acceptance를 실행 가능한 verification에 연결한다. 각 confirmed gap의 `rules`에는 downstream Impl `context.constraints`로 그대로 사용할 수 있는 repository 문서 locator(`docs/...#heading`)와 claim을 하나 이상 포함한다. Confirmed gap의 `evidence[].claim`과 `rules[].claim`처럼 downstream Impl context로 복제되는 모든 claim은 영어 문장으로 남기지 말고 구체적인 한국어 설명으로 작성한다. `required_state`가 named behavior를 요구하면 `Order#changeQuantity`처럼 exact owner와 method symbol을 적고, layer/package 이동을 요구하면 source-confirmed target package 또는 exact planned source path를 적는다. "이름 있는 behavior", "내부 계층", "적절한 package"만 남기면 제출하지 않는다. 각 implementation slice의 `title`, `outcome`, `scope`, `out_of_scope`, acceptance `criterion`은 downstream Impl Card의 immutable 본문이므로 구체적인 한국어 설명으로 작성한다. 각 implementation slice는 `contract_change_ids`를 반드시 가지며 해당 Slice가 생산하거나 소비하는 contract change ID만 기록한다. 같은 verification을 공유한다는 이유만으로 무관한 Slice에 contract를 연결하지 않는다. Triage는 제품 의미와 독립 slice만 고정하고 실행 graph topology는 다음 단계의 dependency audit가 소유한다. 따라서 `depends_on`은 항상 빈 배열로 작성하고 Slice를 dependency 순서로 직렬화하지 않는다.

첫 제출 전에 다음을 자체 점검한다. 원래 intake intent와 `required_state`, Slice outcome과 acceptance를 함께 확인해 intake의 각 change requirement와 preserved observable이 의미 손실 없이 남았는지 확인한다. `intake_amendments.intent_entries`는 source locator·claim 정정에만 사용하고 outcome·behavior·quality gate를 변경하지 않는다. `A 또는 B`, `A/B`, `사용하거나` 같은 미해결 구현 대안을 남기지 않고 source-confirmed authority가 정한 하나의 observable outcome을 쓴다. Intake outcome이 잘못됐거나 독립 책임을 합쳤다면 amendment로 축약하지 말고 새 intake가 필요한 blocker로 종료한다. Gap·verification·production seam·contract evidence locator는 source confirmation evidence에 실제 존재하는 값만 쓰고 모든 source object에서 `kind`를 제거한다. preserved observable마다 verification row는 하나만 작성하며 구조 검증은 별도 중복 row가 아니라 해당 row의 `required_verification`에 포함한다. `current_coverage=partial`인 모든 verification row에는 source confirmation에서 확인한 test evidence를 하나 이상 포함한다. 각 verification row의 evidence locator를 path#symbol 기준으로 세고 중복이면 claim을 합쳐 object 하나만 남긴다. 같은 test class를 두 claim으로 반복하지 말고, 테스트 메서드가 확인하는 서로 다른 동작은 가능한 경우 exact method locator로 좁힌다. Slice의 `title`, `outcome`, `scope`, `out_of_scope`, acceptance `criterion` 각각은 영어 용어만 나열하지 않고 한국어 설명을 포함한다. baseline requirement/current-state document와 모든 source-confirmed document가 `document_impacts`에 한 번씩 존재하고 각 행 evidence가 같은 document를 가리키는지 확인한다. 이 점검을 통과한 artifact만 한 번 제출하고, validator 오류를 예상해 넓은 candidate를 먼저 넣지 않는다.

assignment의 repo-relative `artifact_file`에 직접 작성한다. 작성 후 `kwc_advance_triage`에 현재 assignment_id와 그 artifact_file 값을 그대로 제출한다. KWC가 materialize 단계를 반환한 뒤에만 `kwc_create_triage`를 호출한다.
