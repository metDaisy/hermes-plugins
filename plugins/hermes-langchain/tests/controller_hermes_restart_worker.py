"""Subprocess crash/restart probe for durable Hermes lifecycle recovery."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.support import load_module

CONTROLLER = load_module("controller")


class PersistentLifecycle:
    def __init__(self, root: Path, *, mode: str) -> None:
        self._root = root
        self._mode = mode
        self._state_path = root / "lifecycle-state.json"

    def _state(self) -> dict[str, int | str]:
        if not self._state_path.exists():
            return {"launch_count": 0, "reconnect_count": 0, "subagent_id": "sa-process-1"}
        return json.loads(self._state_path.read_text(encoding="utf-8"))

    def _write(self, state: dict[str, int | str]) -> None:
        self._state_path.write_text(
            json.dumps(state, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )

    def launch(self, request: dict[str, object]) -> object:
        del request
        state = self._state()
        state["launch_count"] = int(state["launch_count"]) + 1
        self._write(state)
        return {"subagent_id": state["subagent_id"]}

    def reconnect(self, handle: object, request: object | None = None) -> object:
        del request
        state = self._state()
        subagent_id = handle.get("subagent_id") if isinstance(handle, dict) else None
        connected = subagent_id == state["subagent_id"] and int(state["launch_count"]) == 1
        state["reconnect_count"] = int(state["reconnect_count"]) + 1
        self._write(state)
        return SimpleNamespace(connected=connected, state="RUNNING", diagnostic=None)

    def wait(self, handle: object, *, timeout_seconds: float | None = None) -> object:
        del handle, timeout_seconds
        if self._mode == "crash":
            os._exit(73)
        if self._mode == "resume-hold":
            (self._root / "recovery-active").write_text("active", encoding="utf-8")
            release = self._root / "release-recovery"
            deadline = time.monotonic() + 20
            while not release.exists():
                if time.monotonic() >= deadline:
                    raise TimeoutError("recovery hold was not released")
                time.sleep(0.02)
        if self._mode == "lease-read-failure":
            leases = list(
                (self._root / "controller-private-lifecycle").glob(
                    "*/lifecycle-recovery-lease.json"
                )
            )
            if len(leases) != 1:
                raise RuntimeError("expected one durable recovery lease")
            leases[0].write_text("{", encoding="utf-8")
        return SimpleNamespace(
            state="SUCCEEDED", completed=True, timed_out=False, diagnostic=None
        )

    def result(self, handle: object) -> object:
        return SimpleNamespace(
            handle=handle,
            terminal_state="SUCCEEDED",
            ready=True,
            summary=None,
            structured_payload={"requirement": "accepted"},
            error_classification=None,
            error_message=None,
        )


def main() -> int:
    root = Path(sys.argv[1]).resolve()
    root.mkdir(parents=True, exist_ok=True)
    mode = sys.argv[2]
    lifecycle = PersistentLifecycle(root, mode=mode)
    invocation = CONTROLLER.StepInvocation(
        run_id="run-process",
        invocation_id="run-process:extract:1",
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
    if mode == "pending-replace-failure":
        def fail_pending_replace(_source: Path, _target: Path) -> None:
            raise OSError("injected pending replace failure")

        CONTROLLER.HermesSubagentExecutor._durable_replace = staticmethod(
            fail_pending_replace
        )
    result = CONTROLLER.HermesSubagentExecutor(
        lifecycle,
        durable_root=root / "controller-private-lifecycle",
        defer_release=mode in {
            "complete-unpersisted",
            "pending-replace-failure",
            "lease-read-failure",
        },
    ).invoke(invocation)
    if result.output != {"requirement": "accepted"}:
        return 4
    if mode == "complete-unpersisted":
        os._exit(74)
    if mode == "lease-read-failure":
        os._exit(75)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
