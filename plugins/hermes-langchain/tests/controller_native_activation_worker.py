"""Hermes-runtime worker for native activation E2E tests."""

from __future__ import annotations

import json
import sys
from pathlib import Path


def main() -> int:
    if len(sys.argv) != 4:
        raise SystemExit("expected plugin_root artifact_store request_file")
    plugin_root = Path(sys.argv[1]).resolve()
    sys.path.insert(0, str(plugin_root / "src"))

    from hermes_langchain.controller.artifact_store import SQLiteArtifactStore
    from hermes_langchain.controller.kanban_activation import (
        AcceptedTaskActivator,
        HermesNativeKanbanActivationPort,
        fingerprint_activation_snapshot,
    )

    store = SQLiteArtifactStore.open(Path(sys.argv[2]).resolve())
    request = json.loads(Path(sys.argv[3]).read_text(encoding="utf-8"))
    port = HermesNativeKanbanActivationPort()
    request["expected_snapshot_fingerprint"] = fingerprint_activation_snapshot(
        port.snapshot(board=request["board"])
    )
    activator = AcceptedTaskActivator(artifact_store=store, kanban_port=port)
    try:
        first = activator.activate(request)
        replay = activator.activate(request)
        task = port.read_task(board=request["board"], task_id=request["task_id"])
        snapshot = port.snapshot(board=request["board"])
    finally:
        store.close()
    print(
        json.dumps(
            {
                "first": first,
                "replay": replay,
                "task": task,
                "snapshot": snapshot,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
