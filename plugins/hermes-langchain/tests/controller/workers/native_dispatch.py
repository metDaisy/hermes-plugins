"""Hermes-runtime worker for native dispatch claim and replay tests."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path


def _record(*, artifact_id: str, run_id: str, step_id: str, executor_identity: str, payload: dict):
    from hermes_langchain.controller.artifact_store import ArtifactRecord, canonical_json_sha256

    input_value = {"accepted": artifact_id}
    return ArtifactRecord(
        artifact_id=artifact_id,
        run_id=run_id,
        step_id=step_id,
        attempt=1,
        executor_kind="deterministic",
        input_sha256=canonical_json_sha256(input_value),
        output_sha256=canonical_json_sha256(payload),
        timeout_seconds=30,
        validation_result="accepted",
        executor_identity=executor_identity,
        payload=payload,
    ), input_value


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
    from hermes_langchain.controller.kanban_dispatch import (
        AcceptedTaskDispatcher,
        HermesNativeKanbanDispatchPort,
    )
    from hermes_langchain.controller.kanban_agent_delivery import (
        AcceptedTaskAgentInvoker,
        HermesNativeClaimLeasePort,
    )
    from hermes_langchain.controller.kanban_completion import (
        AcceptedTaskCompleter,
        HermesNativeKanbanCompletionPort,
    )
    from hermes_langchain.controller.kanban_graph_materialization import (
        AcceptedTaskGraphMaterializer,
    )
    from hermes_langchain.controller.kanban_materialization import (
        accepted_triage_idempotency_key,
    )
    from hermes_langchain.controller.agent_contracts import AgentResult

    dispatch_port = HermesNativeKanbanDispatchPort()
    kanban_db, connect = dispatch_port._dependencies()

    config = json.loads(Path(sys.argv[3]).read_text(encoding="utf-8"))
    board = config["board"]
    issue_number = config["issue_number"]
    generation = config["generation"]
    workspace_path = config["workspace_path"]
    impl_body = config["body"]
    review_body = json.dumps({"schema": "aggregate-review-card-v5"})
    summary_body = json.dumps({"schema": "issue-summary-card-v3"})
    graph_payload = {
        "schema": "controller-task-graph-v1",
        "board": board,
        "issue_number": issue_number,
        "generation": generation,
        "workspace_path": workspace_path,
        "source_task": {
            "task_id": "t-triage-source",
            "title": f"G{generation}-Issue{issue_number}-Triage",
            "body_sha256": "a" * 64,
            "status": "triage",
            "assignee": "project-manager",
            "workspace_kind": "dir",
            "workspace_path": workspace_path,
            "skills": [],
            "idempotency_key": accepted_triage_idempotency_key(
                board=board,
                issue_number=issue_number,
                generation=generation,
                artifact_sha256="b" * 64,
            ),
            "accepted_artifact_id": "triage-artifact",
            "accepted_artifact_sha256": "b" * 64,
        },
        "activation_candidate_key": "impl-1",
        "cards": [
            {
                "key": "impl-1",
                "role": "implementation",
                "title": f"G{generation}-Issue{issue_number}-Impl1",
                "body_text": impl_body,
                "body_sha256": hashlib.sha256(impl_body.encode()).hexdigest(),
                "assignee": "coder",
                "skills": [],
            },
            {
                "key": "review-1",
                "role": "review",
                "title": f"G{generation}-Issue{issue_number}-Review",
                "body_text": review_body,
                "body_sha256": hashlib.sha256(review_body.encode()).hexdigest(),
                "assignee": "reviewer",
                "skills": [],
            },
            {
                "key": "summary-1",
                "role": "summary",
                "title": f"G{generation}-Issue{issue_number}-Summary",
                "body_text": summary_body,
                "body_sha256": hashlib.sha256(summary_body.encode()).hexdigest(),
                "assignee": "project-manager",
                "skills": [],
            },
        ],
        "links": [
            {"parent_key": "source", "child_key": "impl-1"},
            {"parent_key": "impl-1", "child_key": "review-1"},
            {"parent_key": "impl-1", "child_key": "summary-1"},
            {"parent_key": "review-1", "child_key": "summary-1"},
        ],
    }
    store = SQLiteArtifactStore.open(Path(sys.argv[2]).resolve())
    graph_record, graph_input = _record(
        artifact_id="graph-artifact",
        run_id="graph-run",
        step_id="task_graph_materialize",
        executor_identity="controller-task-graph-projection-v1",
        payload=graph_payload,
    )
    graph_record = store.put(graph_record, input_value=graph_input)
    graph = AcceptedTaskGraphMaterializer._graph(
        graph_record,
        {
            "artifact_sha256": graph_record.output_sha256,
            "board": board,
            "issue_number": issue_number,
            "generation": generation,
            "workspace_path": workspace_path,
        },
    )
    candidate = next(card for card in graph["cards"] if card["key"] == "impl-1")

    conn = connect(board=board)
    try:
        task_id = kanban_db.create_task(
            conn,
            title=candidate["title"],
            body=impl_body,
            assignee=candidate["assignee"],
            workspace_kind="dir",
            workspace_path=workspace_path,
            idempotency_key=candidate["idempotency_key"],
            skills=candidate["skills"],
            initial_status="blocked",
            board=board,
        )
    finally:
        conn.close()

    activation_payload = {
        "schema": "controller-task-activation-v1",
        "board": board,
        "issue_number": issue_number,
        "generation": generation,
        "workspace_path": workspace_path,
        "reason_code": "accepted_graph_candidate",
        "task": {
            "task_id": task_id,
            "title": candidate["title"],
            "body_sha256": candidate["body_sha256"],
            "assignee": candidate["assignee"],
            "status": "blocked",
            "workspace_kind": "dir",
            "workspace_path": workspace_path,
            "skills": candidate["skills"],
            "idempotency_key": candidate["idempotency_key"],
        },
    }
    activation_record, activation_input = _record(
        artifact_id="activation-artifact",
        run_id="activation-run",
        step_id="task_activation",
        executor_identity="controller-task-activation",
        payload=activation_payload,
    )
    activation_record = store.put(activation_record, input_value=activation_input)
    activation_port = HermesNativeKanbanActivationPort()
    activation = AcceptedTaskActivator(artifact_store=store, kanban_port=activation_port).activate(
        {
            "schema": "controller-kanban-activation-v1",
            "artifact_id": activation_record.artifact_id,
            "artifact_sha256": activation_record.output_sha256,
            "board": board,
            "issue_number": issue_number,
            "generation": generation,
            "workspace_path": workspace_path,
            "task_id": task_id,
            "expected_snapshot_fingerprint": fingerprint_activation_snapshot(
                activation_port.snapshot(board=board)
            ),
        }
    )

    dispatch_request = {
        "schema": "controller-kanban-dispatch-v1",
        "artifact_id": "dispatch-artifact",
        "graph_artifact_id": graph_record.artifact_id,
        "graph_artifact_sha256": graph_record.output_sha256,
        "activation_artifact_id": activation_record.artifact_id,
        "activation_artifact_sha256": activation_record.output_sha256,
        "activation_key": activation["activation_key"],
        "board": board,
        "issue_number": issue_number,
        "generation": generation,
        "workspace_path": workspace_path,
        "task_id": task_id,
        "expected_snapshot_fingerprint": fingerprint_activation_snapshot(
            dispatch_port.snapshot(board=board)
        ),
    }
    dispatcher = AcceptedTaskDispatcher(
        graph_artifact_store=store,
        activation_artifact_store=store,
        dispatch_artifact_store=store,
        kanban_port=dispatch_port,
    )
    result_store = None
    try:
        first = dispatcher.dispatch(dispatch_request)
        replay = dispatcher.dispatch(dispatch_request)
        result_payload = {"schema": "implementation-result-v1", "status": "completed"}
        result_sha256 = hashlib.sha256(
            json.dumps(result_payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
        ).hexdigest()

        class Executor:
            @staticmethod
            def invoke(invocation):
                claim = invocation.input["native_claim"]
                return AgentResult(
                    agent_id="native-dispatch-agent",
                    output={
                        "schema": "controller-task-agent-delivery-v1",
                        "status": "completed",
                        "task_id": claim["task_id"],
                        "run_id": claim["run_id"],
                        "dispatch_key": claim["dispatch_key"],
                        "result_artifacts": [
                            {
                                "artifact_id": "native-result",
                                "step_id": "implementation_delivery",
                                "sha256": result_sha256,
                                "payload": result_payload,
                            }
                        ],
                    },
                )

        result_store = SQLiteArtifactStore.open(
            Path(sys.argv[2]).with_name("native-dispatch-results.sqlite3")
        )
        delivery = AcceptedTaskAgentInvoker(
            graph_artifact_store=store,
            activation_artifact_store=store,
            dispatch_artifact_store=store,
            delivery_artifact_store=store,
            result_artifact_store=result_store,
            hydration_root=Path(sys.argv[2]).with_name("native-dispatch-inputs"),
            claim_lease_port=HermesNativeClaimLeasePort(),
            executor=Executor(),
        ).invoke(
            {
                "schema": "controller-task-agent-invocation-v1",
                "artifact_id": "delivery-artifact",
                "dispatch_artifact_id": first["artifact_id"],
                "dispatch_artifact_sha256": first["artifact_sha256"],
            }
        )
        completion_port = HermesNativeKanbanCompletionPort()
        completion_request = {
            "schema": "controller-kanban-completion-v1",
            "artifact_id": "completion-artifact",
            "dispatch_artifact_id": first["artifact_id"],
            "dispatch_artifact_sha256": first["artifact_sha256"],
            "delivery_artifact_id": delivery["artifact_id"],
            "delivery_artifact_sha256": delivery["artifact_sha256"],
            "expected_snapshot_fingerprint": fingerprint_activation_snapshot(
                completion_port.snapshot(board=board)
            ),
        }
        completer = AcceptedTaskCompleter(
            graph_artifact_store=store,
            activation_artifact_store=store,
            dispatch_artifact_store=store,
            delivery_artifact_store=store,
            result_artifact_store=result_store,
            completion_artifact_store=store,
            kanban_port=completion_port,
        )
        completion_finalize_recovered = False
        if config.get("inject_completion_finalize_failure"):
            original_recompute_ready = kanban_db.recompute_ready
            failed_once = False

            def fail_recompute_once(conn, *args, **kwargs):
                nonlocal failed_once
                if not failed_once:
                    failed_once = True
                    raise RuntimeError("injected completion finalization failure")
                return original_recompute_ready(conn, *args, **kwargs)

            kanban_db.recompute_ready = fail_recompute_once
            try:
                try:
                    completer.complete(completion_request)
                except RuntimeError as exc:
                    if "completion finalization" not in str(exc):
                        raise
                else:
                    raise AssertionError("completion finalization injection did not fire")
                completion_first = completer.complete(completion_request)
                completion_finalize_recovered = True
            finally:
                kanban_db.recompute_ready = original_recompute_ready
        else:
            completion_first = completer.complete(completion_request)
        completion_replay = completer.complete(completion_request)
        event_payload_tamper_rejected = False
        tamper_conn = connect(board=board)
        try:
            event_row = tamper_conn.execute(
                "SELECT id, payload FROM task_events WHERE task_id = ? AND kind = 'completed' ORDER BY id DESC LIMIT 1",
                (task_id,),
            ).fetchone()
            original_event_payload = str(event_row["payload"])
            tampered_payload = json.loads(original_event_payload)
            tampered_payload["metadata"]["delivery_artifact_sha256"] = "0" * 64
            with kanban_db.write_txn(tamper_conn):
                tamper_conn.execute(
                    "UPDATE task_events SET payload = ? WHERE id = ?",
                    (json.dumps(tampered_payload, sort_keys=True), event_row["id"]),
                )
            try:
                completer.complete(completion_request)
            except Exception as exc:
                event_payload_tamper_rejected = (
                    getattr(exc, "code", None) == "NATIVE_READBACK_MISMATCH"
                )
            finally:
                with kanban_db.write_txn(tamper_conn):
                    tamper_conn.execute(
                        "UPDATE task_events SET payload = ? WHERE id = ?",
                        (original_event_payload, event_row["id"]),
                    )
        finally:
            tamper_conn.close()
        conn = connect(board=board)
        try:
            task = kanban_db.get_task(conn, task_id)
            rows = conn.execute(
                "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'claimed'",
                (task_id,),
            ).fetchall()
            matching_claim_events = sum(
                1
                for row in rows
                if json.loads(row["payload"] or "{}").get("controller_dispatch_key")
                == first["dispatch_key"]
            )
            matching_runs = conn.execute(
                "SELECT COUNT(*) AS count FROM task_runs WHERE task_id = ?",
                (task_id,),
            ).fetchone()["count"]
            completed_rows = conn.execute(
                "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'completed'",
                (task_id,),
            ).fetchall()
            matching_completion_events = sum(
                1
                for row in completed_rows
                if (json.loads(row["payload"] or "{}").get("metadata") or {}).get(
                    "controller_completion_key"
                ) == completion_first["completion_key"]
            )
        finally:
            conn.close()
    finally:
        if result_store is not None:
            result_store.close()
        store.close()
    print(
        json.dumps(
            {
                "first": first,
                "replay": replay,
                "matching_claim_events": matching_claim_events,
                "matching_runs": matching_runs,
                "completion_first": completion_first,
                "completion_replay": completion_replay,
                "matching_completion_events": matching_completion_events,
                "completion_finalize_recovered": completion_finalize_recovered,
                "event_payload_tamper_rejected": event_payload_tamper_rejected,
                "task_status": task.status,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
