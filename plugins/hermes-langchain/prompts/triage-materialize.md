Triage 단계: materialize

Semantic authoring은 모두 검증되었다. 추가 조사나 JSON 재작성을 하지 않는다. 현재 assignment_id로 `kwc_create_triage`만 호출한다. KWC가 저장된 단계 artifact를 조립하고 current-state를 검사한 뒤 canonical Triage를 freeze, native 생성, exact read-back하며 실패 시 rollback한다.
