"""Cross-process plugin adapter probe against the real Hermes lifecycle API."""

from __future__ import annotations

import json
import os
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

PLUGIN_ROOT = Path(__file__).resolve().parents[3]
CORE_ROOT = Path(os.environ["HERMES_CORE_ROOT"]).resolve()
sys.path.insert(0, str(PLUGIN_ROOT))
sys.path.insert(0, str(CORE_ROOT))
sys.path.insert(0, str(PLUGIN_ROOT / "src"))

from agent.subagent_lifecycle import SubagentLifecycleService
from hermes_langchain.controller.agent_contracts import StepInvocation
from hermes_langchain.controller.hermes_executor import HermesSubagentExecutor
import tools.delegate_tool as delegate_tool


class FakeChild:
    def __init__(self, *, subagent_id: str, session_id: str) -> None:
        self._subagent_id = subagent_id
        self._delegate_role = "leaf"
        self._delegate_depth = 1
        self.provider = "fixture"
        self.model = "fixture-model"
        self.session_id = session_id
        self._session_db = SimpleNamespace(
            get_messages_as_conversation=lambda _session_id, **_kwargs: [
                {"role": "user", "content": "persisted adapter task"}
            ]
        )

    def _resume_history_limit_error(self, *, tip_only=False):
        del tip_only
        return None


class LifecycleProbe:
    def __init__(self, service: SubagentLifecycleService, state_path: Path, *, crash: bool) -> None:
        self._service = service
        self._state_path = state_path
        self._crash = crash

    def _state(self) -> dict[str, int]:
        if not self._state_path.exists():
            return {"launch_count": 0, "reconnect_count": 0, "build_count": 0}
        return json.loads(self._state_path.read_text(encoding="utf-8"))

    def _update(self, key: str) -> None:
        state = self._state()
        state[key] = int(state[key]) + 1
        self._state_path.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")

    def launch(self, request):
        self._update("launch_count")
        return self._service.launch(request)

    def reconnect(self, handle, request=None):
        self._update("reconnect_count")
        return self._service.reconnect(handle, request)

    def wait(self, handle, *, timeout_seconds=None):
        if self._crash:
            os._exit(73)
        return self._service.wait(handle, timeout_seconds=timeout_seconds)

    def result(self, handle):
        return self._service.result(handle)


def main() -> int:
    root = Path(sys.argv[1]).resolve()
    mode = sys.argv[2]
    root.mkdir(parents=True, exist_ok=True)
    os.environ["HERMES_HOME"] = str(root)
    state_path = root / "core-lifecycle-state.json"
    parent = SimpleNamespace(
        session_id="parent-plugin-core-restart",
        enabled_toolsets=None,
        _session_db=SimpleNamespace(
            db_path=root / "state.db",
            get_compression_tip=lambda session_id: session_id,
            assert_resume_safe=lambda *_args, **_kwargs: None,
        ),
    )

    def build(**kwargs):
        state = (
            json.loads(state_path.read_text(encoding="utf-8"))
            if state_path.exists()
            else {"launch_count": 0, "reconnect_count": 0, "build_count": 0}
        )
        state["build_count"] = int(state["build_count"]) + 1
        state_path.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
        return FakeChild(
            subagent_id=kwargs.get("subagent_id") or "sa-plugin-core-restart",
            session_id=kwargs.get("session_id") or "child-plugin-core-restart",
        )

    delegate_tool._build_child_agent = build
    delegate_tool._load_config = lambda: {}
    delegate_tool._resolve_delegation_credentials = lambda _cfg, _parent: {
        "provider": "fixture",
        "model": "fixture-model",
        "base_url": "http://127.0.0.1:1/v1",
        "api_key": "fixture",
        "api_mode": "chat_completions",
        "request_overrides": {},
    }
    if mode == "crash":
        blocked = threading.Event()
        delegate_tool._run_child_lifecycle = lambda *_args, **_kwargs: (
            blocked.wait(60) or {"status": "interrupted", "summary": None}
        )
    elif mode == "resume":
        delegate_tool._run_child_lifecycle = lambda *_args, **_kwargs: {
            "status": "completed",
            "summary": '{"requirement":"accepted"}',
            "schema_valid": True,
            "schema_retries": 0,
            "api_calls": 1,
            "duration_seconds": 0.01,
        }
    else:
        raise ValueError(f"unknown mode: {mode}")

    lifecycle = LifecycleProbe(
        SubagentLifecycleService(lambda: parent),
        state_path,
        crash=mode == "crash",
    )
    invocation = StepInvocation(
        run_id="run-plugin-core-process",
        invocation_id="run-plugin-core-process:extract:1",
        step_id="extract",
        goal="Extract one accepted requirement.",
        allowed_toolsets=("file",),
        acceptance_criteria=("Return exactly one requirement string.",),
        output_schema={
            "type": "object",
            "properties": {"requirement": {"type": "string"}},
            "required": ["requirement"],
            "additionalProperties": False,
        },
        input={"artifact_directory": root.as_posix()},
    )
    result = HermesSubagentExecutor(
        lifecycle,
        durable_root=root / "controller-private-lifecycle",
    ).invoke(invocation)
    return 0 if result.output == {"requirement": "accepted"} else 4


if __name__ == "__main__":
    raise SystemExit(main())
