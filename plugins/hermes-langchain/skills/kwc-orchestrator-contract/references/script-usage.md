# KWC helper script 사용 계약

이 문서는 KWC가 공급하는 Python helper, validator, adapter와 E2E script의 공통 실행 규칙이다.
개별 `kwc-*` Skill과 인접 reference가 공개 입력·출력·명령 계약을 소유하며 script 구현은 내부 세부사항이다.

## 기본 원칙

- 정상 workflow 실행에서는 `scripts/*.py` 구현을 읽어 숨은 규칙을 추론하지 않는다.
- 해당 Skill, 공개 reference, template 또는 fixture를 먼저 읽는다.
- 문서화된 명령과 필요한 경우의 `--help`만 사용한다.
- structured error code, field path와 message를 입력 수정의 피드백으로 사용한다.
- error가 지목한 field만 수정한다. broad source scan, 무관한 field 재작성과 validator 우회를 하지 않는다.
- 공개 계약에 없는 rule로 거부되면 `UNDOCUMENTED_VALIDATION_RULE`로 block하고 KWC contract maintenance로 route한다.

## 표준 절차

Draft를 생성하는 helper는 다음 순서를 따른다.

```text
template → bounded authoring → validate → read-only preflight → apply once → native read-back
```

1. board, task와 attempt별 새 경로를 repository `.temp/` 아래에 만든다. 다른 실행의 artifact를 입력으로 재사용하지 않는다.
2. exact locator를 먼저 읽고 repository 전체 listing이나 broad 검색으로 후보를 수집하지 않는다.
3. 문서화된 validate 또는 preflight를 실행한다.
4. 실패하면 error code와 field path에 해당하는 입력만 수정한다.
5. 같은 error를 세 번 수정해도 해소되지 않으면 입력과 report를 보존하고 block한다.
6. 통과한 artifact만 mutation 입력으로 사용한다.
7. mutation은 한 번 실행하고 task, run, comment, link, status와 body를 canonical surface에서 즉시 read-back한다.

Triage는 durable native checkpoint와 ephemeral analysis artifact를 구분한다.

```text
intake authoring → compose-intake → native create/read-back
→ bounded analysis → compose-analysis → freeze
→ replace-card-body once → validate-card final read-back
```

## Profile-independent 실행

Hermes CLI infrastructure profile은 다음 순서로 결정한다.

```text
HERMES_KWC_FRESH_HOST_PROFILE → HERMES_PROFILE → main
```

Helper의 `--profile`에는 위 fallback expression으로 해석한 값을 전달한다. Kanban의 literal assignee·comment author
`project-manager`는 workflow actor identity이며 `--profile project-manager`를 강제한다는 뜻이 아니다.
KWC plugin 내부 helper 경로는 `HERMES_KWC_PLUGIN_ROOT`에서 resolve한다.

## 구현 조사 예외

Script 자체를 수정하거나 failure를 디버깅하는 maintenance task에서만 구현을 읽는다. 먼저 exact failing test와
공개 계약의 기대 결과를 고정한 뒤 관련 symbol과 호출 경로만 조사한다. 일반 Triage, graph authoring,
checkpoint, review routing 또는 finalization은 이 예외가 아니다.
