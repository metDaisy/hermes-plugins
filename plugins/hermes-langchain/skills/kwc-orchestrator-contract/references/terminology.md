# KWC workflow 용어

이 문서는 profile-independent KWC workflow의 개념 기준이다. 실행 순서는 각 `kwc-*` Skill,
persisted schema와 validator 규칙은 인접 `references/`가 소유한다.

## 기준과 상태

| 용어 | 의미 |
|---|---|
| requirement | 사용자가 승인한 목표 동작·정책 계약 |
| current-state | clean committed implementation을 조사한 현재 상태 요약 |
| planning baseline SHA | task contract가 근거로 삼는 committed repository 기준점 |
| inspection SHA | current-state 생성을 위해 실제 조사한 clean implementation SHA |
| fresh / stale / insufficient | snapshot을 planning에 사용할 수 있는지 나타내는 판정 |
| clean working tree | ignored 파일을 제외하고 staged·unstaged·untracked 변경이 없는 상태 |
| active workflow marker | interrupted workflow 복구를 위한 platform-independent committed 표식 |
| final implementation SHA | aggregate Review 승인 뒤 freeze한 마지막 구현 SHA |

## actor와 infrastructure

| 용어 | 의미 |
|---|---|
| KWC | workflow situation 선택, manifest 작성, fresh agent 실행과 검증을 소유하는 controller |
| fresh agent | Profile SOUL·memory 없이 KWC prompt, Skill, Toolset, MCP로만 행동하는 실행 주체 |
| host profile | model, provider, credential, plugin registration과 session storage만 공급하는 Hermes infrastructure Profile |
| execution role | Kanban assignee·comment author·workflow contract에서 쓰는 논리적 actor identity |
| PM Profile baseline | KWC 품질 비교를 위해 유지하는 기존 profile-bound 구현. KWC runtime dependency가 아님 |

`host profile=main`, `execution role=project-manager`는 정상 조합이다. 논리적 actor가
`project-manager`라는 사실은 PM Profile의 SOUL, memory 또는 Skill을 로드한다는 뜻이 아니다.

## graph와 lifecycle

| 용어 | 의미 |
|---|---|
| Triage | 새 leaf Issue의 planning provenance와 initial scheduling gate |
| Impl | Coder가 실행하는 self-contained 구현 계약 |
| PM checkpoint | Coder handoff의 evidence, selector, commit boundary와 clean SHA를 검증하는 same-card gate |
| aggregate Review | 관련 Impl과 inherited behavior를 독립 검토하는 Reviewer-owned card |
| Decision | 사용자 정책 판단이 필요한 사실을 보존하는 blocked card |
| Summary | 승인된 Review 뒤 local finalization을 수행하는 semantic Issue root |
| finding | Review result의 `correction-required`, `context-required`, `decision-required`, `resolved` record |
| generation | 하나의 승인 requirement revision을 구현하는 graph 단위 |
| requirement-rework | 승인 requirement 변경으로 G{N+1} superseding graph를 만드는 절차 |
| review-rework | completed Review finding으로 같은 generation에 corrective path를 추가하는 절차 |

## 책임 소유자

- `kwc-orchestrator-contract`: 공통 identity, authority, approval, failure, output 경계
- `kwc-create-triage`: Triage authoring과 freeze
- `kwc-build-task-graph`: graph·Impl·Review authoring
- `kwc-run-workflow`: checkpoint, promotion, finding routing, finalization과 recovery
- `kwc-service-planning`: 사용자 정책 선택지 조사
- `kwc-sync-docs`: requirement 파생 문서와 tracker 정합성
- `kwc-update-current-state`: freshness, snapshot과 marker
- 각 Skill의 `references/`: persisted schema와 deterministic validator
- native Kanban: task ID, status, links, run, event와 comment의 canonical state
