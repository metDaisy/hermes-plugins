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

현재 버전은 `0.5.1`입니다. 버전의 기준값은 `plugin.yaml`의 `version`이며, 다음
metadata에도 같은 SemVer 값을 유지합니다.

- `dashboard/manifest.json`의 `version`
기능·계약 변경 시 SemVer 규칙에 따라 버전을 올리고
`python plugins/agent-audit/test_metadata.py`로 metadata 일치를 확인합니다.

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

Prompt, reasoning, conversation history, raw command, raw tool argument/result, credential 또는 absolute path는 저장하지 않습니다. Credential처럼 보이는 텍스트는 failure summary에 저장되기 전에 redaction됩니다. Desktop UI는 allowlisted SQLite data만 조회하며 raw Hermes log를 읽거나 표시하지 않습니다.

Logging은 fail-open으로 동작합니다. Filesystem 또는 serialization 오류가 발생해도 coding을 중단하지 않습니다. Verification failure의 책임은 underlying validator와 project workflow에 있습니다.

## 검증

Repository Python interpreter로 plugin regression test를 실행합니다.

```text
python plugins/agent-audit/test_agent_audit.py
python plugins/agent-audit/test_shell_hook.py
python plugins/agent-audit/test_metadata.py
python plugins/agent-audit/test_plugin_api.py
```

Desktop/TUI/dashboard chat은 Hermes shell hook을 사용합니다. Profile에는 `post_tool_call`, `on_skill_lifecycle`, `pre_verify`, `on_session_end` hook entry가 있어야 하며, 각 entry는 이 plugin의 `shell_hook.py`를 가리켜야 합니다. Python plugin hook만으로는 CLI/Gateway에서만 동작합니다.

Plugin discovery 또는 manifest 동작을 변경한 경우에는 관련 Hermes Plugin Doctor/runtime 검사도 수행합니다. Active Profile policy 또는 사용자의 명시적인 선택으로 플러그인을 비활성화한 경우에는 계속 비활성화 상태로 유지합니다. 정적 문서와 regression check가 runtime 활성화를 의미하지는 않습니다.
