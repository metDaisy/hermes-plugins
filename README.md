# agent-audit

Project-local Hermes hook plugin that records privacy-safe lifecycle and validation metadata in `.hermes/audit.db` (SQLite). It observes workflow evidence and gives verification nudges; it does not replace Checkstyle, JUnit, ArchUnit, Spring Modulith verification or CI.

## Ownership

- `plugin.yaml`: plugin identity and hook registration
- `__init__.py`: SQLite event schema, rule mapping, redaction, freshness tracking and hooks
- `dashboard/plugin_api.py`: read-only SQLite query API for Desktop UI
- `desktop/plugin.js`: Audit Explorer page and profile-aware timeline
- `test_agent_audit.py`: executable behavior contract
- `.hermes/README.md`: project-level activation and capability setup

Implementation and tests are authoritative. This README describes the supported behavior and does not maintain a separate roadmap.

## Observed rules

| Rule ID | Trigger | Recognized evidence |
|---|---|---|
| `STYLE-JAVA-001` | Java source/test mutation | Checkstyle invocation after the latest mutation |
| `TEST-JAVA-001` | Java source/test mutation | JUnit/test invocation after the latest mutation |
| `ARCH-MOD-001` | recognized production module mutation | `ModularityTest`/Modulith verification |
| `ARCH-LAYER-001` | recognized layer mutation in the plugin's configured domain set | `ModularityTest`/architecture verification |
| `GRADLE-MCP-001` | direct terminal Gradle invocation | policy-deviation event; direct invocation is not accepted |

A mutation increments the session generation and makes affected evidence stale. `pre_verify` reports missing or failed evidence for the latest generation. Rule selection is advisory metadata; actual pass/fail comes from the validator output and CI.

## Privacy and failure behavior

The SQLite store may contain event type, profile name, tool or Skill name, status, duration, opaque correlation IDs, project-relative paths, rule IDs, generation and a bounded sanitized failure summary.

It does not persist prompts, reasoning, conversation history, raw commands, raw tool arguments/results, credentials or absolute paths. Credential-like text is redacted before a failure summary is retained. The Desktop UI queries only this allowlisted data; it never reads or displays raw Hermes logs.

Logging is fail-open: filesystem or serialization failure does not stop coding. Verification failure still belongs to the underlying validator and project workflow.

## Verification

Run the plugin regression test with the repository Python interpreter:

```text
python .hermes/plugins/agent-audit/test_agent_audit.py
```

When plugin discovery or manifest behavior changes, also perform the relevant Hermes Plugin Doctor/runtime check. Keep this plugin disabled when the active profile policy or explicit user choice disables it; static documentation and regression checks do not imply runtime activation.
