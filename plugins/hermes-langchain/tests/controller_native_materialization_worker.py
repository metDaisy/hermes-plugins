"""Hermes-runtime worker for native materialization E2E tests."""

from __future__ import annotations

import json
import sys
from contextlib import contextmanager
from pathlib import Path


def main() -> int:
    if len(sys.argv) != 5:
        raise SystemExit("expected plugin_root artifact_store request_file mode")
    plugin_root = Path(sys.argv[1]).resolve()
    sys.path.insert(0, str(plugin_root / "src"))

    from hermes_langchain.controller.artifact_store import SQLiteArtifactStore
    from hermes_langchain.controller.kanban_materialization import (
        AcceptedMilestoneMaterializer,
        HermesNativeKanbanMaterializationPort,
        KanbanMaterializationError,
    )

    store = SQLiteArtifactStore.open(Path(sys.argv[2]).resolve())
    request = json.loads(Path(sys.argv[3]).read_text(encoding="utf-8"))
    mode = sys.argv[4]
    port = HermesNativeKanbanMaterializationPort()
    observer_port = port
    if mode == "rollback":
        port = _TamperedReadbackPort(port)
    elif mode == "post-commit-failure":
        module, connect = port._dependencies()
        port = HermesNativeKanbanMaterializationPort(
            kanban_db_module=_PostCommitFailureModule(module),
            connect_fn=connect,
        )
    elif mode == "post-commit-close-failure":
        module, connect = port._dependencies()
        port = HermesNativeKanbanMaterializationPort(
            kanban_db_module=_PostCommitFailureModule(module),
            connect_fn=_FailFirstCloseConnect(connect),
        )
    elif mode != "success":
        raise SystemExit(
            "mode must be success, rollback, post-commit-failure, "
            "or post-commit-close-failure"
        )
    materializer = AcceptedMilestoneMaterializer(
        artifact_store=store,
        kanban_port=port,
    )
    try:
        if mode == "success":
            first = materializer.materialize(request)
            replay = materializer.materialize(request)
            port.archive_task(board=request["board"], task_id=first["task_id"])
            archived_replay = materializer.materialize(request)
            task = port.read_task(board=request["board"], task_id=first["task_id"])
            result = {
                "first": first,
                "replay": replay,
                "archived_replay": archived_replay,
                "task": task,
                "snapshot": port.snapshot(board=request["board"]),
            }
        elif mode == "rollback":
            try:
                materializer.materialize(request)
            except KanbanMaterializationError as exc:
                task_id = port.created_task_id
                if task_id is None:
                    raise RuntimeError("rollback probe did not create a task") from exc
                task = port.delegate.read_task(board=request["board"], task_id=task_id)
                result = {
                    "error_code": exc.code,
                    "task": task,
                    "active_snapshot": port.delegate.snapshot(board=request["board"]),
                }
            else:
                raise RuntimeError("rollback probe unexpectedly succeeded")
        else:
            try:
                materializer.materialize(request)
            except KanbanMaterializationError as exc:
                snapshot = observer_port.snapshot(board=request["board"])
                result = {
                    "error_code": exc.code,
                    "snapshot": snapshot,
                }
            else:
                raise RuntimeError("post-commit failure probe unexpectedly succeeded")
    finally:
        store.close()
    print(
        json.dumps(
            result,
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


class _TamperedReadbackPort:
    def __init__(self, delegate: HermesNativeKanbanMaterializationPort) -> None:
        self.delegate = delegate
        self.created_task_id: str | None = None

    def snapshot(self, *, board: str) -> dict:
        return self.delegate.snapshot(board=board)

    def create_triage(
        self,
        *,
        board: str,
        card: dict,
        expected_snapshot_fingerprint: str,
    ) -> dict:
        receipt = self.delegate.create_triage(
            board=board,
            card=card,
            expected_snapshot_fingerprint=expected_snapshot_fingerprint,
        )
        self.created_task_id = str(receipt["task_id"])
        return receipt

    def read_task(self, *, board: str, task_id: str) -> dict:
        task = self.delegate.read_task(board=board, task_id=task_id)
        if task["status"] != "archived":
            task["body"] += "\n"
        return task

    def archive_task(self, *, board: str, task_id: str) -> None:
        self.delegate.archive_task(board=board, task_id=task_id)


class _PostCommitFailureModule:
    def __init__(self, delegate: object) -> None:
        self._delegate = delegate

    def __getattr__(self, name: str) -> object:
        return getattr(self._delegate, name)

    @contextmanager
    def write_txn(self, conn: object):
        with self._delegate.write_txn(conn):
            yield conn
        raise RuntimeError("injected post-commit integrity failure")


class _FailFirstCloseConnect:
    def __init__(self, delegate: object) -> None:
        self._delegate = delegate
        self._calls = 0

    def __call__(self, *, board: str):
        self._calls += 1
        conn = self._delegate(board=board)
        if self._calls == 2:
            return _CloseFailureConnection(conn)
        return conn


class _CloseFailureConnection:
    def __init__(self, delegate: object) -> None:
        self._delegate = delegate

    def __getattr__(self, name: str) -> object:
        return getattr(self._delegate, name)

    def close(self) -> None:
        self._delegate.close()
        raise RuntimeError("injected close failure")


if __name__ == "__main__":
    raise SystemExit(main())
