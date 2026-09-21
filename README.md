# hermes-plugins

metDaisy가 관리하는 Hermes Agent plugin monorepo입니다.

## Plugins

| Plugin | Version | Description |
|---|---:|---|
| [`agent-audit`](plugins/agent-audit/README.md) | `0.4.0` | Privacy-safe lifecycle, validation evidence, SQLite audit storage, dashboard API, Desktop UI |

각 plugin은 독립적인 `plugin.yaml`을 가지며 `plugins/<plugin-name>/`을 plugin root로 사용합니다.

## Repository layout

```text
plugins/
└── agent-audit/
    ├── plugin.yaml
    ├── __init__.py
    ├── shell_hook.py
    ├── dashboard/
    ├── desktop/
    └── test_*.py
```

## agent-audit 검증

Repository root에서 실행합니다.

```bash
python plugins/agent-audit/test_agent_audit.py
python plugins/agent-audit/test_shell_hook.py
python plugins/agent-audit/test_metadata.py
python plugins/agent-audit/test_plugin_api.py
node --check plugins/agent-audit/desktop/plugin.js
```

Plugin 구현과 metadata의 기준은 각 plugin 디렉터리의 `plugin.yaml`과 README입니다. 프로젝트별 Profile hook 등록과 정책 설정은 plugin을 사용하는 프로젝트에서 관리합니다.
