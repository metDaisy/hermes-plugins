# create-triage

`create-triage`는 새 leaf Issue를 바로 구현 task로 나누기 전에, **무엇을 바꾸고 무엇을 반드시 보존하며 어떻게 검증할지** 고정하는 Project Manager 절차입니다.

이 Skill의 산출물은 Coder에게 전달하는 구현 지시가 아닙니다. PM이 다음 단계에서
`build-task-graph`로 self-contained 구현·검토 card를 작성할 수 있게 하는 **검증된 구현 입력 문서**입니다.

## 언제 사용하나요?

새 leaf Issue를 시작할 때 사용합니다.

시작 전에 PM은 Issue 범위와 승인된 requirement를 확인하고, canonical current-state inspection
artifact로 fresh `current-state.md`, 같은 planning HEAD, clean working tree를 검증합니다. 같은 Issue에
진행 중인 triage card가 있으면 새로 만들지 않고 그 card를 이어서 사용합니다.

## 동작 과정

```text
Issue와 요구사항 확인
  → intake card 즉시 생성·read-back
  → bounded analysis로 confirmed gap과 preserved observable 고정
  → 구현 slice·verification matrix 작성
  → semantic audit 후 frozen handoff
```

1. **Intake** — 목표, 변경 요구, 보존 observable, 제외 범위와 조사 시작점을 먼저 card로 공개합니다.
2. **Analysis** — 직접 근거가 있는 gap만 남기고 모든 조사 target을 disposition합니다.
3. **Slice** — gap을 독립 검증 가능한 구현 단위와 acceptance로 변환합니다.
4. **Verification** — observable별 기존 coverage와 필요한 회귀 검증을 구분합니다.
5. **Freeze** — 불필요한 Decision과 scope creep가 없는지 감사한 뒤 digest와 handoff를 고정합니다.

## 결과

완료된 triage에는 다음 정보가 남습니다.

- 변경 요구와 보존 observable
- 직접 근거가 있는 confirmed gap
- self-contained 구현 slice와 acceptance
- Review Card의 입력이 되는 verification matrix
- 실제로 필요한 Decision과 document impact
- downstream graph handoff 가능 여부

Triage card는 Coder가 직접 읽는 계약은 아니지만, graph authoring 중 first eligible Impl을
`todo`로 유지하는 native scheduling parent입니다. PM은 이 기록과 승인된 문서를 바탕으로 Coder별
task card를 모두 만든 뒤 Triage를 완료해 첫 Impl 하나만 `ready`로 승격합니다.

## 막히는 경우

다음 상태에서는 triage를 완료하지 않습니다.

- `current-state.md`가 stale 또는 insufficient인 경우
- 요구사항 또는 정책이 구현에 필요한 결정을 제공하지 않는 경우
- requirement·문서·Issue 사이에 해결되지 않은 충돌이 있는 경우

이때 Triage는 native `triage`를 유지하고 graph gate를 닫습니다. 사용자 입력이 필요하면 linked Planning
Decision만 `blocked`로 두며, 결정이나 문서 동기화 뒤 같은 Triage를 재개합니다.

## 다음 단계

```text
create-triage 완료
  → build-task-graph
  → self-contained Coder child cards + root review contract
```

세부 JSON 구조와 Kanban 상태 전이는 `SKILL.md`와
[`references/triage-contract.md`](references/triage-contract.md)를 따릅니다.
