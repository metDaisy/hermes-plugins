"""Hermes-runtime disposable full lifecycle worker for M13."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace


def _record(artifact_id, run_id, step_id, executor_identity, payload):
    from hermes_langchain.controller.artifact_store import ArtifactRecord, canonical_json_sha256

    input_value = {"accepted": artifact_id}
    return (
        ArtifactRecord(
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
        ),
        input_value,
    )


def main() -> int:
    if len(sys.argv) != 4:
        raise SystemExit("expected plugin_root artifact_store config_file")
    plugin_root = Path(sys.argv[1]).resolve()
    sys.path.insert(0, str(plugin_root / "src"))

    from hermes_langchain.controller.artifact_store import SQLiteArtifactStore
    from hermes_langchain.controller.hermes_executor import HermesSubagentExecutor
    from hermes_langchain.controller.kanban_activation import (
        AcceptedTaskActivator,
        HermesNativeKanbanActivationPort,
        fingerprint_activation_snapshot,
    )
    from hermes_langchain.controller.kanban_agent_delivery import (
        AcceptedTaskAgentInvoker,
        HermesNativeClaimLeasePort,
        KanbanAgentDeliveryError,
    )
    from hermes_langchain.controller.kanban_completion import (
        AcceptedTaskCompleter,
        HermesNativeKanbanCompletionPort,
    )
    from hermes_langchain.controller.kanban_dispatch import (
        AcceptedTaskDispatcher,
        HermesNativeKanbanDispatchPort,
    )
    from hermes_langchain.controller.kanban_graph_materialization import (
        AcceptedTaskGraphMaterializer,
        HermesNativeKanbanGraphMaterializationPort,
        fingerprint_graph_snapshot,
    )
    from hermes_langchain.controller.kanban_materialization import accepted_triage_idempotency_key

    config = json.loads(Path(sys.argv[3]).read_text(encoding="utf-8"))
    board = config["board"]
    issue_number = config["issue_number"]
    generation = config["generation"]
    workspace_path = config["workspace_path"]
    source_body = '{"schema":"triage-v7","phase":"frozen"}'
    bodies = {
        "implementation": config["body"],
        "review": json.dumps({"schema": "aggregate-review-card-v5"}),
        "summary": json.dumps({"schema": "issue-summary-card-v3"}),
    }

    graph_port = HermesNativeKanbanGraphMaterializationPort()
    kanban_db, connect = graph_port._dependencies()
    source_key = accepted_triage_idempotency_key(
        board=board,
        issue_number=issue_number,
        generation=generation,
        artifact_sha256="b" * 64,
    )
    conn = connect(board=board)
    try:
        source_id = kanban_db.create_task(
            conn,
            title=f"G{generation}-Issue{issue_number}-Triage",
            body=source_body,
            assignee="project-manager",
            workspace_kind="dir",
            workspace_path=workspace_path,
            triage=True,
            idempotency_key=source_key,
            board=board,
        )
    finally:
        conn.close()

    graph_payload = {
        "schema": "controller-task-graph-v1",
        "board": board,
        "issue_number": issue_number,
        "generation": generation,
        "workspace_path": workspace_path,
        "source_task": {
            "task_id": source_id,
            "title": f"G{generation}-Issue{issue_number}-Triage",
            "body_sha256": hashlib.sha256(source_body.encode()).hexdigest(),
            "status": "triage",
            "assignee": "project-manager",
            "workspace_kind": "dir",
            "workspace_path": workspace_path,
            "skills": [],
            "idempotency_key": source_key,
            "accepted_artifact_id": "triage-artifact",
            "accepted_artifact_sha256": "b" * 64,
        },
        "activation_candidate_key": "implementation",
        "cards": [
            {
                "key": role,
                "role": role,
                "title": f"G{generation}-Issue{issue_number}-{role.title()}",
                "body_text": body,
                "body_sha256": hashlib.sha256(body.encode()).hexdigest(),
                "assignee": (
                    "coder" if role == "implementation" else "reviewer" if role == "review" else "project-manager"
                ),
                "skills": [],
            }
            for role, body in bodies.items()
        ],
        "links": [
            {"parent_key": "source", "child_key": "implementation"},
            {"parent_key": "implementation", "child_key": "review"},
            {"parent_key": "implementation", "child_key": "summary"},
            {"parent_key": "review", "child_key": "summary"},
        ],
    }
    store = SQLiteArtifactStore.open(Path(sys.argv[2]).resolve())
    graph_record, graph_input = _record(
        "graph-artifact",
        "graph-run",
        "task_graph_materialize",
        "controller-task-graph-projection-v1",
        graph_payload,
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
    materialization = AcceptedTaskGraphMaterializer(
        artifact_store=store,
        kanban_port=graph_port,
    ).materialize(
        {
            "schema": "controller-kanban-graph-materialization-request-v1",
            "artifact_id": graph_record.artifact_id,
            "artifact_sha256": graph_record.output_sha256,
            "board": board,
            "issue_number": issue_number,
            "generation": generation,
            "workspace_path": workspace_path,
            "expected_snapshot_fingerprint": fingerprint_graph_snapshot(
                graph_port.snapshot(board=board)
            ),
        }
    )

    conn = connect(board=board)
    try:
        if not kanban_db.specify_triage_task(conn, source_id, assignee="project-manager"):
            raise AssertionError("source Triage could not be specified")
        if not kanban_db.complete_task(
            conn,
            source_id,
            result="Accepted graph materialized",
            summary="Accepted graph materialized",
            force=True,
            fire_lifecycle_hook=False,
        ):
            raise AssertionError("source Triage could not be completed")
    finally:
        conn.close()

    activation_port = HermesNativeKanbanActivationPort()
    dispatch_port = HermesNativeKanbanDispatchPort()
    completion_port = HermesNativeKanbanCompletionPort()

    class Lifecycle:
        def __init__(self):
            self.launches = []
            self.results = {}

        def launch(self, request):
            self.launches.append(dict(request))
            context = json.loads(request["context"])
            claim = context["input"]["native_claim"]
            predecessors = json.loads(
                (
                    Path(context["input"]["artifact_directory"])
                    / "predecessors.json"
                ).read_text(encoding="utf-8")
            )
            expected_predecessors = {
                "implementation_delivery": [],
                "review_delivery": ["implementation"],
                "summary_delivery": ["implementation", "review"],
            }[context["step_id"]]
            parents = predecessors.get("parents") or []
            if (
                predecessors.get("schema")
                != "controller-task-predecessor-evidence-v1"
                or [parent.get("card_key") for parent in parents]
                != expected_predecessors
                or any(not parent.get("results") for parent in parents)
            ):
                raise AssertionError("Agent did not receive exact predecessor evidence")
            index = len(self.launches)
            handle = SimpleNamespace(subagent_id=f"lifecycle-agent-{index}")
            result_payload = {"schema": f"{claim['task_id']}-result-v1", "status": "completed"}
            result_sha256 = hashlib.sha256(
                json.dumps(
                    result_payload,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode()
            ).hexdigest()
            self.results[handle.subagent_id] = {
                "schema": "controller-task-agent-delivery-v1",
                "status": "completed",
                "task_id": claim["task_id"],
                "run_id": claim["run_id"],
                "dispatch_key": claim["dispatch_key"],
                "result_artifacts": [
                    {
                        "artifact_id": f"result-{index}",
                        "step_id": context["step_id"],
                        "sha256": result_sha256,
                        "payload": result_payload,
                    }
                ],
            }
            return handle

        @staticmethod
        def wait(handle, *, timeout_seconds=None):
            del handle, timeout_seconds
            return SimpleNamespace(
                state="SUCCEEDED", completed=True, timed_out=False, diagnostic=None
            )

        def result(self, handle):
            return SimpleNamespace(
                terminal_state="SUCCEEDED",
                ready=True,
                summary=json.dumps(self.results[handle.subagent_id]),
                structured_payload=self.results[handle.subagent_id],
                error_classification=None,
                error_message=None,
            )

    lifecycle = Lifecycle()
    result_store = SQLiteArtifactStore.open(
        Path(sys.argv[2]).with_name("native-lifecycle-results.sqlite3")
    )
    hydration_root = Path(sys.argv[2]).with_name("native-lifecycle-inputs")

    completion_replays = []
    for role in ("implementation", "review", "summary"):
        card = next(item for item in graph["cards"] if item["role"] == role)
        task_id = materialization["task_ids"][role]
        if role == "review" and config.get("inject_out_of_band_parent"):
            conn = connect(board=board)
            try:
                extra_id = kanban_db.create_task(
                    conn,
                    title="Out-of-band parent",
                    body="not accepted graph evidence",
                    assignee="coder",
                    workspace_kind="dir",
                    workspace_path=workspace_path,
                    skills=[],
                    idempotency_key="out-of-band-parent",
                    board=board,
                )
                with kanban_db.write_txn(conn):
                    conn.execute(
                        "UPDATE tasks SET status = 'ready' WHERE id = ?",
                        (extra_id,),
                    )
                if not kanban_db.complete_task(
                    conn,
                    extra_id,
                    result="out-of-band completion",
                    force=True,
                    fire_lifecycle_hook=False,
                ):
                    raise AssertionError("failed to complete out-of-band parent")
                with kanban_db.write_txn(conn):
                    conn.execute(
                        "INSERT INTO task_links (parent_id, child_id) VALUES (?, ?)",
                        (extra_id, task_id),
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
                "title": card["title"],
                "body_sha256": card["body_sha256"],
                "assignee": card["assignee"],
                "status": "todo",
                "workspace_kind": "dir",
                "workspace_path": workspace_path,
                "skills": card["skills"],
                "idempotency_key": card["idempotency_key"],
            },
        }
        activation_record, activation_input = _record(
            f"activation-{role}",
            f"activation-run-{role}",
            "task_activation",
            "controller-task-activation",
            activation_payload,
        )
        activation_record = store.put(activation_record, input_value=activation_input)
        activation = AcceptedTaskActivator(
            artifact_store=store,
            kanban_port=activation_port,
        ).activate(
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
        dispatch = AcceptedTaskDispatcher(
            graph_artifact_store=store,
            activation_artifact_store=store,
            dispatch_artifact_store=store,
            kanban_port=dispatch_port,
        ).dispatch(
            {
                "schema": "controller-kanban-dispatch-v1",
                "artifact_id": f"dispatch-{role}",
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
        )
        invoker = AcceptedTaskAgentInvoker(
            graph_artifact_store=store,
            activation_artifact_store=store,
            dispatch_artifact_store=store,
            delivery_artifact_store=store,
            result_artifact_store=result_store,
            hydration_root=hydration_root,
            claim_lease_port=HermesNativeClaimLeasePort(),
            executor=HermesSubagentExecutor(lifecycle),
        )
        delivery_request = {
            "schema": "controller-task-agent-invocation-v1",
            "artifact_id": f"delivery-{role}",
            "dispatch_artifact_id": dispatch["artifact_id"],
            "dispatch_artifact_sha256": dispatch["artifact_sha256"],
        }
        if role == "review" and config.get("inject_out_of_band_parent"):
            try:
                invoker.invoke(delivery_request)
            except KanbanAgentDeliveryError as exc:
                if exc.code != "PREDECESSOR_EVIDENCE_INVALID":
                    raise
                print(json.dumps({"rejected": exc.code}))
                return 0
            raise AssertionError("out-of-band predecessor was accepted")
        delivery = invoker.invoke(delivery_request)
        if not invoker.invoke(delivery_request)["idempotent_replay"]:
            raise AssertionError("delivery replay relaunched the Agent")
        completion_request = {
            "schema": "controller-kanban-completion-v1",
            "artifact_id": f"completion-{role}",
            "dispatch_artifact_id": dispatch["artifact_id"],
            "dispatch_artifact_sha256": dispatch["artifact_sha256"],
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
        first = completer.complete(completion_request)
        replay = completer.complete(completion_request)
        if not replay["idempotent_replay"]:
            raise AssertionError("completion replay was not idempotent")
        completion_replays.append((completion_request, first))

    restart_replay_verified = True
    for request, first in completion_replays:
        replay = AcceptedTaskCompleter(
            graph_artifact_store=store,
            activation_artifact_store=store,
            dispatch_artifact_store=store,
            delivery_artifact_store=store,
            result_artifact_store=result_store,
            completion_artifact_store=store,
            kanban_port=HermesNativeKanbanCompletionPort(),
        ).complete(request)
        restart_replay_verified = restart_replay_verified and (
            replay["idempotent_replay"] and replay["run_id"] == first["run_id"]
        )

    conn = connect(board=board)
    try:
        statuses = {
            role: kanban_db.get_task(conn, materialization["task_ids"][role]).status
            for role in ("implementation", "review", "summary")
        }
        task_ids = tuple(materialization["task_ids"].values())
        placeholders = ",".join("?" for _ in task_ids)
        runs = conn.execute(
            f"SELECT COUNT(*) AS count FROM task_runs WHERE task_id IN ({placeholders})",
            task_ids,
        ).fetchone()["count"]
        claim_events = conn.execute(
            f"SELECT COUNT(*) AS count FROM task_events WHERE task_id IN ({placeholders}) AND kind = 'claimed'",
            task_ids,
        ).fetchone()["count"]
        completion_events = conn.execute(
            f"SELECT COUNT(*) AS count FROM task_events WHERE task_id IN ({placeholders}) AND kind = 'completed'",
            task_ids,
        ).fetchone()["count"]
    finally:
        conn.close()
        result_store.close()
        store.close()

    print(
        json.dumps(
            {
                "statuses": statuses,
                "agent_invocations": len(lifecycle.launches),
                "runs": runs,
                "claim_events": claim_events,
                "completion_events": completion_events,
                "restart_replay_verified": restart_replay_verified,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
