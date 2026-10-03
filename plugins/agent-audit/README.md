# agent-audit

Hermes용 privacy-safe audit plugin입니다. 개인정보 보호를 고려한 lifecycle 및 검증 메타데이터를 프로젝트의 `.hermes/audit.db`(SQLite)에 기록합니다. Workflow evidence를 관찰하고 검증 알림을 제공하지만 Checkstyle, JUnit, ArchUnit, Spring Modulith 검증 또는 CI를 대체하지 않습니다.

## 소유 범위

- `plugin.yaml`: 플러그인 식별 정보와 Python hook 등록
- `__init__.py`: SQLite event schema, Rule 매핑, redaction, freshness 추적 및 CLI/Gateway hook
- `shell_hook.py`: privacy-safe writer로 전달하는 Desktop/TUI/dashboard shell-hook adapter
- `dashboard/plugin_api.py`: Desktop UI용 읽기 전용 SQLite query API
- `desktop/plugin.js`: `agent-audit` 페이지와 Profile-aware timeline
- `test_agent_audit.py`: Python hook 동작 계약
- `test_shell_hook.py`: Desktop shell-hook bridge 계약
- `test_metadata.py`: plugin.yaml과 dashboard manifest의 metadata 일치 검증
- `test_plugin_api.py`: SQLite dashboard query 계약

플러그인 설치는 Desktop/TUI/dashboard shell hook도 설정합니다. 이 surface들은 Python plugin hook을 직접 호출하지 않으므로, shell adapter가 sanitized shell-hook envelope를 동일한 SQLite writer로 전달합니다.

## 버전 관리

현재 버전은 `0.10.4`입니다. 버전의 기준값은 `plugin.yaml`의 `version`이며, 다음
metadata에도 같은 SemVer 값을 유지합니다.

- `dashboard/manifest.json`의 `version`
- `pack.yml`의 `version`
기능·계약 변경 시 SemVer 규칙에 따라 버전을 올리고
`python plugins/agent-audit/test_metadata.py`로 metadata 일치를 확인합니다.

## Desktop에서 보이는 정보

이벤트 목록은 내부 hook 이름보다 사용자가 이해할 수 있는 행동을 우선 표시합니다. 예를 들어
`search_files · tool_call` 대신 “프로젝트 파일을 검색했습니다”와 대상 경로 요약을 표시합니다.
추가 정보가 있는 항목을 선택하면 상세 내용이 페이지 위로 이동하지 않고 **선택한 항목 바로
아래**에 펼쳐지며, 한 번에 하나의 상세만 열립니다. Skill 확인처럼 목록에 필요한 정보가 모두
표시된 항목은 빈 상세 화면을 열지 않습니다.

상세 화면은 다음 순서로 정보를 제공합니다.

1. Terminal 실행 명령의 접힌 더보기 영역과 실패 원인
2. 대상 경로와 검증 Rule
3. 실패 또는 절차 이탈 시 다음 행동

목록에 이미 표시된 행동명, 요약, 결과, Profile, 모델은 상세에서 반복하지 않으며, 내부 도구명과
Skill 이름을 다시 나열하던 `기술 정보` 영역도 표시하지 않습니다.
소요 시간은 상세 영역을 열지 않아도 확인할 수 있도록 각 로그 본문의 오른쪽 아래에 표시합니다.

API는 기존 SQLite row를 변경하지 않고 Desktop용 `schema_version: 2` projection을 생성합니다.
주요 body는 `actor`, `model`, `activity`, `outcome`, `scope`, `explanation`, `correlation`,
`evidence`, `privacy`로 구성됩니다. 이전 Desktop copy와의 한 버전 호환을 위해 기존 flat field도
함께 반환합니다.

화면 최상단에서 Project 하나를 먼저 선택합니다. 선택 전에는 여러 Project의 기록을 섞어 표시하지
않습니다. 선택 후에는 `기록`, `세션`, `성공률`, `실패` 네 지표와 해당 Project의 타임라인을
표시합니다. 보조 필터는 `Session → Profile → 활동 유형 → 결과` 순서입니다. Project는 세션의
Git 저장소 또는 작업 디렉터리 basename으로 보정하며 절대 경로는 노출하지 않습니다.
목록의 각 이벤트에는 색상으로 구분한 Profile 이름, 사용 모델과 `reasoning-effort`를 표시합니다.
Project 선택 항목에는 Project 이름만 표시하고, Session 필터에는 세션 이름만 표시합니다.
선택 항목에 반복적인 기록 수는 표시하지 않습니다. 모델 context가 없는 과거 기록은
`모델 미확인`으로 명시합니다.

Skill 지침 확인 기록은 `skill_view` 입력에서 allowlist된 Skill 이름을 함께 표시합니다. Terminal
기록은 credential과 절대 경로를 redaction한 전체 실행 명령을 저장하고 상세 화면의 더보기 안에
표시합니다. 실패한 도구 호출은 최대 600자의 sanitized 원인을 저장하며, 원인을 제공하지 않은
과거 기록이나 도구에는 원인 정보가 기록되지 않았다는 사실을 상세 화면에 명시합니다. 실패한
Terminal 호출은 exit code와 분류도 함께 표시합니다.

## Rule mapping 설정

플러그인은 특정 프로젝트의 module 이름, package prefix 또는 layer를 코드에 내장하지 않습니다. Rule mapping은 Hermes 설정의 `plugins.entries.agent-audit.rule_mapping`에서 읽습니다. 설정이 없으면 path 기반 verification gate를 만들지 않고 lifecycle/audit event만 기록합니다.

```yaml
plugins:
  entries:
    agent-audit:
      rule_mapping:
        path_rules:
          - rule_id: STYLE-JAVA-001
            prefixes:
              - src/main/java/
              - src/test/java/
          - rule_id: TEST-JAVA-001
            prefixes:
              - src/main/java/
              - src/test/java/
          - rule_id: ARCH-MOD-001
            prefixes:
              - src/main/java/com/example/orders/
          - rule_id: ARCH-LAYER-001
            prefixes:
              - src/main/java/com/example/orders/presentation/

        # 위에서부터 처음 일치하는 validator rule만 적용합니다.
        validator_rules:
          - contains: [checkstyle]
            rule_ids: [STYLE-JAVA-001]
          - contains: [modularitytest, modularity]
            rule_ids: [ARCH-MOD-001, ARCH-LAYER-001]
          - contains: [test, junit, integrationtest]
            rule_ids: [TEST-JAVA-001]
```

`path_rules`의 `prefixes`는 project-relative POSIX 경로 prefix이고, `validator_rules`의 `contains`는 validator tool name과 인자를 소문자로 변환한 문자열에 적용됩니다. 절대 경로와 `..`를 포함한 prefix는 무시됩니다. Project별 Rule ID와 경로 계약은 plugin을 사용하는 repository가 소유합니다.

## 관찰하는 Rule

| Rule ID | Trigger | 인식하는 Evidence |
|---|---|---|
| `STYLE-JAVA-001` | Java source/test 변경 | 최신 변경 이후 Checkstyle 실행 |
| `TEST-JAVA-001` | Java source/test 변경 | 최신 변경 이후 JUnit/test 실행 |
| `ARCH-MOD-001` | 인식된 production module 변경 | `ModularityTest`/Modulith 검증 |
| `ARCH-LAYER-001` | 플러그인에 설정된 domain 범위의 인식된 layer 변경 | `ModularityTest`/architecture 검증 |
| `GRADLE-MCP-001` | 직접적인 terminal Gradle 실행 | policy-deviation event; 직접 실행은 허용된 검증으로 인정하지 않음 |

변경이 발생하면 session generation이 증가하고 영향을 받는 evidence가 stale 상태가 됩니다. `pre_verify`는 최신 generation에 대해 누락되었거나 실패한 evidence를 보고합니다. Rule 선택은 advisory metadata이며, 실제 pass/fail은 validator output과 CI가 결정합니다.

## Privacy 및 실패 동작

SQLite store에는 event type, Profile 이름, tool 또는 Skill 이름, status, duration, opaque correlation ID, project-relative path, Rule ID, generation 및 길이가 제한된 sanitized failure summary가 저장될 수 있습니다.

Prompt, reasoning 원문, conversation history, raw tool argument/result, credential 또는 absolute path는 저장하지 않습니다. Terminal command만 credential과 absolute path를 redaction한 전체 문자열로 저장합니다. `reasoning-effort`는 원문이 아닌 실행 설정값만 저장합니다. Credential처럼 보이는 텍스트는 failure summary와 command에 저장되기 전에 redaction됩니다. Desktop UI는 allowlisted SQLite data만 조회하며 raw Hermes log를 읽거나 표시하지 않습니다.

상세 이벤트와 모델 연결 정보는 30일 동안 보존합니다. 만료된 상세 기록은 삭제 전에
Project·Profile별 일간 event, 성공, 실패, 세션 수로 집계하여 `audit_daily_rollups`에 영구
보존합니다. 이 집계에는 command, 실패 원문, Session ID가 포함되지 않습니다.

Logging은 fail-open으로 동작합니다. Filesystem 또는 serialization 오류가 발생해도 coding을 중단하지 않습니다. Verification failure의 책임은 underlying validator와 project workflow에 있습니다.

## 검증

Repository Python interpreter로 plugin regression test를 실행합니다.

```text
python plugins/agent-audit/test_agent_audit.py
python plugins/agent-audit/test_shell_hook.py
python plugins/agent-audit/test_metadata.py
python plugins/agent-audit/test_plugin_api.py
python plugins/agent-audit/test_desktop_contract.py
```

Desktop/TUI/dashboard chat은 Hermes shell hook을 사용합니다. Profile에는 `pre_api_request`, `post_tool_call`, `on_skill_lifecycle`, `pre_verify`, `on_session_end` hook entry가 있어야 하며, 각 entry는 이 plugin의 `shell_hook.py`를 가리켜야 합니다. Python plugin hook만으로는 CLI/Gateway에서만 동작합니다.

Plugin discovery 또는 manifest 동작을 변경한 경우에는 관련 Hermes Plugin Doctor/runtime 검사도 수행합니다. Active Profile policy 또는 사용자의 명시적인 선택으로 플러그인을 비활성화한 경우에는 계속 비활성화 상태로 유지합니다. 정적 문서와 regression check가 runtime 활성화를 의미하지는 않습니다.
