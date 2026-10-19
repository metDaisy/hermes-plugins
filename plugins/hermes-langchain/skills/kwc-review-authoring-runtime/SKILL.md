---
name: kwc-review-authoring-runtime
description: Use when authoring one aggregate Review body from frozen Triage and complete Impl cards. Runs the bounded review fast path.
version: 0.1.0
author: Amaazon project, Hermes Agent
license: MIT
platforms: [windows, linux, macos]
metadata:
  hermes:
    tags: [project-management, kanban, review, authoring, performance]
requires_toolsets: [terminal, hermes-langchain]
---

# Review authoring runtime

Frozen Triage와 complete Impl source closure에서 **Review body 하나만** 작성한다. Review runtime 실행,
checkpoint, findings routing, Summary, activation은 수행하지 않는다.

## 불변조건

- assignment의 board, issue, generation, sources.triage_id, sources.impl_ids, workspace_path,
  artifact_file, assignment_id, next_action을 그대로 사용한다.
- `artifact_file`은 `.temp/task-graphs/<issue>/review-body.json`이며 `next_action`은 `kwc_create_review`다.
- Triage digest, baseline SHA, complete behavior IDs, implementation card keys와 source acceptance/
  verification closure는 helper prefill을 그대로 보존한다. source closure를 subset·duplicate·재해석하지 않는다.
- body top-level field는 `schema`, `triage_digest`, `baseline_sha`, `behavior_ids`,
  `implementation_card_keys`, `acceptance`, `review_checks`, `scope_exclusions`만 사용한다.
- Review focus enum은 `behavior`, `implementation-convention`, `cross-card-seam`, `test-quality`,
  `preservation`만 사용한다. Review check fields are id, focus, question, expected_outcome,
  behavior_ids, implementation_card_keys, acceptance_ids, source_acceptance_ids, verification_ids,
  guide_locators이며 `criterion` field는 check에 넣지 않는다. 각 check의 question과 expected_outcome은
  중복 없이 한국어로 작성한다.
- `guide_locators`는 prepared bundle의 **exact guide_locators**만 복사한다. backend development guide,
  backend test guide, active `.hermes/docs/plan.md` handoff locator를 각각 포함한다.
- acceptance는 behavior, Impl, source acceptance와 verification ID를 source closure 안에서 연결한다.
  HTTP binding/DTO, application command/result, aggregate behavior, cross-module seam과 preserved behavior를
  서로 다른 review concern으로 유지한다.
- `review-source.json`의 각 `implementation_cards[].body.acceptance[].id`와
  `implementation_cards[].body.verification[].id`를 source closure의 정본으로 사용한다. Review body의
  `source_acceptance_ids`와 `verification_ids`에는 copy source acceptance IDs and verification IDs exactly를
  적용하며, `vm-*`·`acc-*`를 추측하거나 새 source ID를 만들지 않는다. 모든 source ID는 acceptance와 check에서
  정확히 coverage하고, 각 row의 `implementation_card_keys`는 해당 ID를 실제 소유한 Impl만 가리킨다.
- runtime/result 필드, reviewed HEAD, test result, worktree, PR, CI, merge, finding, verdict를 body에 넣지 않는다.

## bounded procedure

1. assignment의 exact workspace를 `workdir`로 사용해 다음 helper를 정확히 한 번 실행한다. stale artifact,
   historical card, source production/test 재조사와 broad graph rebuild를 하지 않는다.

```text
python "$HERMES_KWC_PLUGIN_ROOT/skills/kwc-build-task-graph/scripts/build_task_graph.py" prepare-review-fast-path --profile "${HERMES_KWC_FRESH_HOST_PROFILE:-${HERMES_PROFILE:-main}}" --board <board> --triage-id <triage-id> --implementation-id <impl-id>... --issue <issue> --generation <generation> --bundle-output .temp/task-graphs/<issue>/review-source.json --body-output .temp/task-graphs/<issue>/review-body.json
```

2. helper가 생성한 `review-body.json` template과 `review-source.json`의 exact Impl acceptance/
   verification closure를 읽고 mutable semantic fields를 한 번의 bounded write로 작성한다. Helper가 채운
   immutable closure를 다시 타이핑하지 않는다. 불확실한 artifact identity나 guide locator가 있으면 추측하지
   말고 `MCP_LOOKUP_REQUIRED` blocker로 종료한다.
3. JSON을 canonical object로 유지하고 review body validator가 요구하는 exact enum과 top-level allowlist를
   지킨다. validation failure는 지목된 mutable field만 한 번 consolidated correction한다. 두 번째 실패면 block한다.
4. 검증된 exact body와 `review-source.json`을 현재 assignment와 함께 `kwc_create_review`에 전달한다. Native
   mutation과 source/body/read-back은 KWC가 소유한다. Agent가 `create-review-native`, 수동 Kanban create/show,
   Review 재생성 또는 Summary를 별도로 실행하지 않는다.
5. 결과가 Summary phase로 전환되고 Review ID가 하나인지 확인한 뒤 중단한다.

한국어 설명은 일상적인 제품 동작과 책임 경계를 직접 설명해야 하며, 단순한 generic "검증한다" 문장은 금지한다.
