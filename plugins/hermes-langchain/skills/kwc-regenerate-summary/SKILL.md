---
name: kwc-regenerate-summary
description: KWC fresh agent가 profile SOUL·memory 없이 수행하는 "Use when a completed Issue Summary must be created or regenerated. Runs one bounded native helper."
version: 0.1.0
author: "Amaazon project, Hermes Agent"
license: MIT
platforms: [windows]
metadata:
  hermes:
    tags: [project-management, kanban, summary, fast-path]
---

> 이 Skill의 실행 정책과 helper는 Kanban Workflow Controller가 소유한다. PM Profile copy는 비교 기준일 뿐 실행 의존성이 아니다.

# Regenerate Summary

완료된 Issue의 frozen Triage 기반 `issue-summary-card-v3`를 생성하거나 교체할 때만 사용한다.

## 입력

- board slug
- frozen Triage native task ID
- Issue 번호와 generation
- complete Impl/Review가 공유하는 canonical workspace
- repository root

필수 입력이 없으면 추측하지 않고 중단한다.

## 실행

repository root를 terminal `workdir`로 지정하고 다음 helper를 **정확히 한 번** 실행한다.

```text
python "$HERMES_KWC_PLUGIN_ROOT/skills/kwc-build-task-graph/scripts/build_task_graph.py" regenerate-summary-native --profile "${HERMES_KWC_FRESH_HOST_PROFILE:-${HERMES_PROFILE:-main}}" --board <board> --triage-id <triage-id> --issue <issue> --generation <generation> --workspace <canonical-workspace> --bundle-output .temp/task-graphs/<issue>/summary-source.json --body-output .temp/task-graphs/<issue>/summary-body.json --output .temp/task-graphs/<issue>/summary-create-result.json
```

이 helper가 active Summary 탐색·archive·archive read-back, frozen Triage projection, source freshness 검증,
create, exact body/parent read-back과 reverse child 검증을 모두 소유한다.

별도 board list/show/archive, Git status, `prepare-summary-fast-path`, `create-summary-native`, custom Python,
임시 복사본 수정, runtime monkey patch, manual native create를 실행하지 않는다. Helper 실패 시 우회하지 않고
오류를 그대로 반환한다.

## 완료

결과 JSON의 `valid=true`, `task_id`, `archived_task_id`, `stages`, `elapsed_ms`만 확인한다. 사용자에게는 새
Summary ID, 총 helper 시간과 stage별 시간을 보고한다. 추가 read-back은 helper가 이미 수행하므로 반복하지 않는다.
