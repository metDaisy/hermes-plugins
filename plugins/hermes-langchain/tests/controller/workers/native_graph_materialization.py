"""Hermes-runtime worker for native task-graph materialization E2E tests."""

from __future__ import annotations

import json
import os
import sys
from contextlib import contextmanager
from pathlib import Path


def main() -> int:
    if len(sys.argv) != 4:
        raise SystemExit("expected plugin_root artifact_store request_file")
    plugin_root = Path(sys.argv[1]).resolve()
    sys.path.insert(0, str(plugin_root / "src"))

    from hermes_langchain.controller.artifact_store import SQLiteArtifactStore
    from hermes_langchain.controller.kanban_graph_materialization import (
        AcceptedTaskGraphMaterializer,
        HermesNativeKanbanGraphMaterializationPort,
        fingerprint_graph_snapshot,
    )

    store = SQLiteArtifactStore.open(Path(sys.argv[2]).resolve())
    request = json.loads(Path(sys.argv[3]).read_text(encoding="utf-8"))
    port = HermesNativeKanbanGraphMaterializationPort()
    unrelated_task_id = None
    if os.environ.get("HERMES_LANGCHAIN_TEST_POST_COMMIT_FAILURE") == "1":
        kanban_db, connect = port._dependencies()
        conn = connect(board=request["board"])
        try:
            unrelated_task_id = kanban_db.create_task(
                conn,
                title="Unrelated promotable task",
                initial_status="blocked",
                workspace_kind="scratch",
                board=request["board"],
            )
            with kanban_db.write_txn(conn):
                conn.execute(
                    "UPDATE tasks SET status = 'todo' WHERE id = ?",
                    (unrelated_task_id,),
                )
        finally:
            conn.close()
    request["expected_snapshot_fingerprint"] = fingerprint_graph_snapshot(
        port.snapshot(board=request["board"])
    )
    materializer = AcceptedTaskGraphMaterializer(
        artifact_store=store,
        kanban_port=port,
    )
    if os.environ.get("HERMES_LANGCHAIN_TEST_POST_COMMIT_FAILURE") == "1":
        from hermes_cli import kanban_db

        original_write_txn = kanban_db.write_txn
        injected = False

        @contextmanager
        def fail_after_commit(conn, *args, **kwargs):
            nonlocal injected
            with original_write_txn(conn, *args, **kwargs):
                yield
            if not kwargs.get("allow_nested", False) and not injected:
                injected = True
                raise RuntimeError("injected post-commit integrity failure")

        baseline = port.snapshot(board=request["board"])
        kanban_db.write_txn = fail_after_commit
        try:
            try:
                materializer.materialize(request)
            except Exception as exc:
                failure_code = getattr(exc, "code", type(exc).__name__)
            else:
                raise AssertionError("post-commit failure was not propagated")
            restored = port.snapshot(board=request["board"])
        finally:
            kanban_db.write_txn = original_write_txn
            store.close()
        print(
            json.dumps(
                {
                    "failure_code": failure_code,
                    "baseline": baseline,
                    "restored": restored,
                    "unrelated_task_id": unrelated_task_id,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0
    try:
        first = materializer.materialize(request)
        replay = materializer.materialize(request)
        graph = port.read_graph(
            board=request["board"],
            source_task_id=first["source_task_id"],
            task_ids=first["task_ids"],
        )
        port.rollback_graph(
            board=request["board"],
            task_ids=list(first["task_ids"].values()),
            expected_snapshot_fingerprint=request["expected_snapshot_fingerprint"],
        )
        rolled_back = port.snapshot(board=request["board"])
    finally:
        store.close()
    print(
        json.dumps(
            {
                "first": first,
                "replay": replay,
                "graph": graph,
                "rolled_back": rolled_back,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
