---
name: kwc-service-planning
description: KWC fresh agent가 profile SOUL·memory 없이 수행하는 "정책·사용자 흐름·UI 선택지를 조사해 사용자 결정을 받을 때 사용한다."
version: 1.0.0
author: "Amaazon project"
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [project-management, service-planning, requirements, user-decision]
    related_skills: [kwc-sync-docs, kwc-build-task-graph]
---

> 이 Skill의 실행 정책과 helper는 Kanban Workflow Controller가 소유한다. PM Profile copy는 비교 기준일 뿐 실행 의존성이 아니다.

# 서비스 기획

PM이 사용자와 대화하며 business policy와 서비스 방향을 결정하도록 돕는다. 확정 사실, 외부 관찰,
추론과 제안을 분리하고, 결정 tree의 질문을 순서대로 좁혀 shared understanding에 도달한다. PM은
조사하고 추천하지만 최종 product decision은 사용자가 내린다.

## 언제 사용하는가

- 새 기능·business policy·권한·공개 API 의미·오류 의미를 정해야 한다.
- 사용자 흐름, 화면 목적, 우선순위 또는 UX trade-off를 정해야 한다.
- Triage나 Review finding이 `decision-required`이고 source 조사만으로 해결할 수 없다.
- Amazon 또는 다른 e-commerce 서비스의 공개 동작을 비교해야 한다.

단순한 source 위치·구현 범위 조사, 이미 승인된 requirement의 문서 전파에는 사용하지 않는다.

## 빠른 사용 가이드

사용자는 결정하려는 주제만 제시해도 된다. 관련 사실과 문서는 PM이 조사한다.

```text
/service-planning
상품 상세에서 품절 옵션을 숨길지 비활성화할지 사용자 흐름과 운영 영향을 비교해서 결정하자.
```

Triage나 Review에서 시작했다면 card ID와 `decision-required` 내용을 함께 받는다. Skill은 즉시 결론을
쓰지 않고, 조사 가능한 사실을 먼저 확인한 뒤 현재 답할 수 있는 결정 질문을 round 단위로 묻는다.

## 입력과 preflight

다음을 확보하고 누락 항목은 질문한다.

- 결정할 질문, decision owner, 응답이 필요한 시점
- 관련 requirement locator와 승인 상태
- originating Triage·Decision·Review card가 있으면 그 ID와 blocker
- 현재 구현이 선택에 영향을 줄 때만 freshness가 확인된 `current-state.md` section과 snapshot SHA

Requirement가 아직 승인되지 않았거나 질문이 여러 독립 결정을 섞으면, 질문별로 선택지를 분리한다.
완료 기준: 각 질문에 owner와 영향을 받는 requirement locator가 하나 이상 연결된다.

## 근거 규칙

결정에 필요한 최소 근거만 조사한다. 공개 서비스 비교가 필요하면 공식 문서와 실제 공개 화면을
우선하고, 인증·개인정보·결제 정보가 보이는 화면은 수집하지 않는다.

각 근거는 다음 필드를 갖는다.

| 필드 | 내용 |
|---|---|
| `kind` | `approved-fact | external-observation | inference` |
| `claim` | 결정과 직접 관련된 한 문장 |
| `locator` | requirement heading, URL 또는 repository path/line |
| `observed-at` | 외부 관찰 날짜; repository 사실이면 commit/SHA |
| `limit` | 지역·계정·실험군·확인 불가 조건 |

화면 배치나 상호작용이 판단 근거일 때만 screenshot을 남기고 URL, 화면 영역, 관찰 날짜를 함께
기록한다. Screenshot은 장식이 아니라 해당 claim의 근거여야 한다. 완료 기준: 보고서의 모든 사실성
주장에 locator가 있고, 추론은 사실처럼 표현되지 않는다.

## 대화 절차

1. **Root question을 고정한다.** 승인 requirement에서 확정 사실과 미결정 사항을 분리하고 이번
   결정의 목표, 범위와 제외 범위를 쓴다. 완료 기준: 선택 결과에 따라 달라지는 behavior가 명시된다.
2. **사실을 조사한다.** Repository나 공개 자료에서 확인할 수 있는 사실은 사용자에게 묻지 않고 직접
   조사한다. 완료 기준: 선택지를 평가할 근거와 한계가 위 형식으로 기록된다.
3. **Decision tree를 만든다.** Root question을 actor와 목표, scope, business rule, 권한·오류 의미,
   사용자 flow와 화면 상태, 운영 영향, 되돌리기 비용의 관련 branch로 분해한다. 필요 없는 branch는
   만들지 않는다. 완료 기준: 각 질문의 prerequisite와 그 답에 따라 열리는 후속 질문이 구분된다.
4. **Frontier를 round로 묻는다.** Prerequisite가 모두 결정된 질문만 현재 frontier다. 한 round에는
   서로 독립적인 frontier 질문을 최대 5개까지 `clarify`로 함께 묻고, 각 질문에 구체적인 선택지와
   근거 기반 추천을 첫 선택지로 제시한다. 자유 응답이 필요한 질문만 open-ended로 묻는다. 사용자의
   응답 전에는 dependent question을 묻거나 다음 단계로 진행하지 않는다.
5. **Tree를 다시 계산한다.** 답변마다 branch를 `settled | deferred | open`으로 표시하고 새로 열린
   frontier만 다음 round에서 묻는다. 모순되거나 의미가 달라지는 답은 영향과 함께 다시 확인한다.
   완료 기준: 암묵적으로 선택된 branch 없이 frontier가 비어 있다.
6. **Shared understanding을 확인한다.** 결정, 명시적 제외, 보류 항목, 추천과 근거를 요약하고 사용자에게
   최종 확인을 요청한다. 사용자가 확인하기 전에는 requirement·Issue를 수정하거나 구현 작업을 만들지
   않는다. 완료 기준: 사용자가 요약이 합의와 일치한다고 명시적으로 확인한다.
7. **Requirement에 반영한다.** 확인된 결정만 소유 requirement의 기존 형식에 맞게 수정하고 정확한
   heading과 변경 내용을 다시 읽는다. UI 결정은 아래 필드를 빠짐없이 materialize한다. 완료 기준:
   사용자 결정과 requirement 문장이 같은 의미다.
8. **영향을 전파한다.** 파생 문서나 Issue가 영향받으면 `sync-docs`를 실행한다. 완료 기준: 동기화
   결과 또는 영향 없음의 근거를 받았다.
9. **다음 전이를 선택한다.** Originating Triage·Decision·Review가 있으면 같은 card에 결정 locator와
   동기화 결과를 연결한다. 직접 호출이고 구현 delivery가 요청됐다면 승인 requirement, fresh
   current-state와 Issue/Triage precondition을 확인해 `build-task-graph`로 넘긴다. 기획만 요청됐다면
   승인 locator와 후속 조건을 보고하고 종료한다. 완료 기준: blocker 해소 여부와 실제 다음
   transition 또는 종료 이유가 명시된다.

## UI 결정 필드

UI 방향을 requirement에 반영할 때 기존 문서 구조 안에 다음 의미를 기록한다. 별도 schema 파일을
만들거나 화면 component 설계를 대신하지 않는다.

- actor와 사용자 목표
- 진입 조건과 시작점
- happy path의 행동 순서
- loading, empty, error, permission 상태
- 각 행동의 관찰 가능한 결과와 상태 전이
- mobile/desktop 차이가 있을 때 그 차이
- 접근성 또는 노출 우선순위가 behavior에 영향을 줄 때 그 기준

## 보류와 blocker

- 현재 구현 결과를 바꾸는 미결정 항목은 originating card를 `blocked`로 유지한다.
- 현재 범위에 영향이 없는 대안은 `deferred`와 제외 이유를 기록하고 이번 requirement에 넣지 않는다.
- 사용자가 결정을 거절하거나 기한 없이 보류하면 owner와 재개 조건을 남기고 종료한다.
- 보류된 branch가 현재 behavior를 바꾸지 않으면 명시적 out-of-scope로 분리하고 나머지 tree를 계속한다.

## 주의할 점

- 사용자의 선호를 사실이나 승인된 policy로 바꾸지 않는다.
- 한 round에서 답이 서로 의존하는 질문을 함께 묻지 않는다.
- 조사 가능한 사실, source 위치나 구현 현황을 사용자에게 질문하지 않는다.
- 모든 가능한 제품 질문을 기계적으로 묻지 않고 현재 결정에 영향을 주는 branch만 연다.

## 검증

최종 보고는 `확정 사실 / 외부 관찰 / 선택지와 영향 / 추천 / 사용자 결정 / requirement 반영 /
후속 전이` 순서로 작성한다. 완료를 보고하기 전에 requirement read-back, 조건부 `sync-docs` 결과,
originating card의 실제 상태를 확인한다.

Source·test·migration을 구현하거나 승인 전 선택지를 확정 사실로 기록하지 않는다.
