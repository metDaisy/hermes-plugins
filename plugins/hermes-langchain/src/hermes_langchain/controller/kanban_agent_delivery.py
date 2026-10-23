"""Fresh-Agent invocation and immutable typed delivery provenance."""

from __future__ import annotations

import json
import re
import threading
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol

from .artifact_store import (
    ArtifactRecord,
    ArtifactStoreError,
    SQLiteArtifactStore,
    canonical_json,
    canonical_json_sha256,
)
from .kanban_activation import AcceptedTaskActivator, HermesNativeKanbanActivationPort
from .kanban_dispatch import (
    AcceptedTaskDispatcher,
    HermesNativeKanbanDispatchPort,
    KanbanDispatchError,
)
from .kanban_graph_materialization import AcceptedTaskGraphMaterializer
from .agent_contracts import AgentExecutor, StepInvocation

_REQUEST_SCHEMA = "controller-task-agent-invocation-v1"
_RECEIPT_SCHEMA = "controller-task-agent-invocation-receipt-v1"
_DELIVERY_SCHEMA = "controller-task-agent-delivery-v1"
_RESULT_SCHEMA = "controller-task-agent-result-v1"
_REQUEST_FIELDS = {
    "schema",
    "artifact_id",
    "dispatch_artifact_id",
    "dispatch_artifact_sha256",
}
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_DELIVERY_VERIFICATION_TOOLSETS = (
    "file",
    "gradle-mcp",
    "codebase-memory-mcp",
    "semble-mcp",
)
_IMPLEMENTATION_GOAL = (
    "CONTROLLER_IMPLEMENTATION_DELIVERY_V2\n"
    "Read the exact staged skill.md and hydrate only files in the supplied immutable artifact directory. "
    "Execute the accepted implementation card in the pinned workspace using the plugin-owned Coder "
    "contract. Validate card meaning against repository facts before mutation, preserve exact observable "
    "and verification coverage, and block contradictions instead of inventing policy. Do not access "
    "Kanban, Profile SOUL, memory, prior conversation, or select workflow transitions. Return only one "
    "JSON object matching the supplied delivery schema."
)
_REVIEW_GOAL = (
    "CONTROLLER_REVIEW_DELIVERY_V2\n"
    "Read the exact staged skill.md and hydrate only files in the supplied immutable artifact directory. "
    "Execute the accepted aggregate Review card in the pinned workspace using the plugin-owned Reviewer "
    "contract. Independently inspect the fixed diff and repository evidence, complete every review axis "
    "and required verification, and remain read-only. Do not access Profile SOUL, memory, prior "
    "conversation, or select workflow transitions. Return only one JSON object matching the supplied "
    "delivery schema."
)
_SUMMARY_GOAL = (
    "CONTROLLER_SUMMARY_DELIVERY_V1\n"
    "Read the exact staged skill.md and hydrate only files in the supplied immutable artifact directory. "
    "Execute the accepted Summary card in the pinned workspace. Do not access Profile SOUL, memory, "
    "prior conversation, or select workflow transitions. Return only one JSON object matching the "
    "supplied delivery schema."
)


def _role_execution_contract(role: Any) -> dict[str, Any]:
    """Return the closed Profile-independent execution contract for one card role."""

    contracts = {
        "implementation": {
            "step_id": "implementation_delivery",
            "goal": _IMPLEMENTATION_GOAL,
            "toolsets": _DELIVERY_VERIFICATION_TOOLSETS,
            "skills": ("kwc-implementation-delivery-runtime",),
            "acceptance_criteria": (
                "Use only accepted ArtifactRefs and the pinned workspace; block "
                "CARD_EXPECTED_VALUE_CONTRADICTION or CARD_SCENARIO_SELECTOR_CONTRADICTION "
                "before product mutation.",
                "Prove every obligation, constraint, and observable leaf with exact assertion "
                "coverage; verify the external producer public contract and adapter execution path.",
                "Complete the declared focused, full backend, and quality gates without weakening "
                "selectors or moving required scenarios into residual risk.",
                "Return a typed delivery bound to the exact native task, run, and dispatch key.",
            ),
        },
        "review": {
            "step_id": "review_delivery",
            "goal": _REVIEW_GOAL,
            "toolsets": _DELIVERY_VERIFICATION_TOOLSETS,
            "skills": ("kwc-review-delivery-runtime",),
            "acceptance_criteria": (
                "Reconstruct the fixed baseline-to-reviewed-head diff and independently inspect "
                "source, tests, configuration, migration, and predecessor evidence.",
                "Complete the five review axes plus security with exact evidence and disposition "
                "every prior finding as resolved or continues.",
                "Run the applicable Gradle, Modulith, and database verification without changing "
                "product or workflow state; missing evidence is a blocker.",
                "Return a typed delivery bound to the exact native task, run, and dispatch key.",
            ),
        },
        "summary": {
            "step_id": "summary_delivery",
            "goal": _SUMMARY_GOAL,
            "toolsets": ("file",),
            "skills": ("kwc-summary-delivery-runtime",),
            "acceptance_criteria": (
                "Use only accepted ArtifactRefs and the pinned workspace contract.",
                "Return a typed delivery bound to the exact native task, run, and dispatch key.",
            ),
        },
    }
    contract = contracts.get(role)
    if contract is None:
        raise KanbanAgentDeliveryError("EXECUTION_CONTRACT_INVALID", "task role is not canonical")
    return dict(contract)


class KanbanAgentDeliveryError(ValueError):
    """Stable fail-closed error for one fresh-Agent delivery invocation."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class NativeClaimLeasePort(Protocol):
    def heartbeat(
        self,
        *,
        board: str,
        task_id: str,
        run_id: int,
        dispatch_key: str,
    ) -> bool: ...


class NativePredecessorEvidencePort(Protocol):
    def completed_parents(self, *, board: str, task_id: str) -> list[dict[str, Any]]: ...


class HermesNativePredecessorEvidencePort:
    """Read controller-completed native parents without mutating Kanban state."""

    def __init__(self, *, kanban_db_module: Any = None, connect_fn: Any = None) -> None:
        self._kanban_db_module = kanban_db_module
        self._connect_fn = connect_fn

    def _dependencies(self) -> tuple[Any, Any]:
        if self._kanban_db_module is None or self._connect_fn is None:
            from ..kanban.snapshot_service import _ensure_hermes_source_root, _import_hermes_cli

            hermes_cli = _import_hermes_cli()
            _ensure_hermes_source_root(hermes_cli)
            from hermes_cli import kanban_db, kanban_db_connect

            self._kanban_db_module = self._kanban_db_module or kanban_db
            self._connect_fn = self._connect_fn or kanban_db_connect.connect
        return self._kanban_db_module, self._connect_fn

    def completed_parents(self, *, board: str, task_id: str) -> list[dict[str, Any]]:
        module, connect = self._dependencies()
        conn = connect(board=board)
        try:
            parent_rows = conn.execute(
                "SELECT parent_id FROM task_links WHERE child_id = ? ORDER BY parent_id",
                (task_id,),
            ).fetchall()
            evidence: list[dict[str, Any]] = []
            for row in parent_rows:
                parent_id = str(row["parent_id"])
                task = module.get_task(conn, parent_id)
                if task is None:
                    raise KanbanAgentDeliveryError(
                        "PREDECESSOR_EVIDENCE_INVALID", "native predecessor task is missing"
                    )
                events = conn.execute(
                    "SELECT run_id, payload FROM task_events "
                    "WHERE task_id = ? AND kind = 'completed' ORDER BY id DESC",
                    (parent_id,),
                ).fetchall()
                matches: list[dict[str, Any]] = []
                for event in events:
                    try:
                        payload = json.loads(event["payload"] or "{}")
                    except (TypeError, ValueError):
                        continue
                    metadata = payload.get("metadata") if isinstance(payload, Mapping) else None
                    if isinstance(metadata, Mapping) and metadata.get(
                        "controller_completion_key"
                    ):
                        matches.append(
                            {
                                "run_id": event["run_id"],
                                "payload": payload,
                                "metadata": dict(metadata),
                            }
                        )
                if len(matches) > 1:
                    raise KanbanAgentDeliveryError(
                        "PREDECESSOR_EVIDENCE_INVALID",
                        "native predecessor has multiple controller completions",
                    )
                task_value = HermesNativeKanbanActivationPort._task_value(task)
                task_value.update(
                    {
                        "result": getattr(task, "result", None),
                        "claim_lock": getattr(task, "claim_lock", None),
                        "claim_expires": getattr(task, "claim_expires", None),
                        "worker_pid": getattr(task, "worker_pid", None),
                        "current_run_id": getattr(task, "current_run_id", None),
                    }
                )
                completion = matches[0] if matches else None
                run = None
                if completion is not None:
                    run_row = conn.execute(
                        "SELECT id, task_id, status, outcome, claim_lock, claim_expires, "
                        "worker_pid FROM task_runs WHERE id = ? AND task_id = ?",
                        (completion["run_id"], parent_id),
                    ).fetchone()
                    if run_row is not None:
                        run = dict(run_row)
                evidence.append(
                    {
                        "task": task_value,
                        "completion": completion,
                        "run": run,
                    }
                )
            return evidence
        finally:
            conn.close()


class HermesNativeClaimLeasePort:
    """Extend only the exact controller-owned native claim."""

    def __init__(self, *, kanban_db_module: Any = None, connect_fn: Any = None) -> None:
        self._kanban_db_module = kanban_db_module
        self._connect_fn = connect_fn

    def _dependencies(self) -> tuple[Any, Any]:
        if self._kanban_db_module is None or self._connect_fn is None:
            from ..kanban.snapshot_service import _ensure_hermes_source_root, _import_hermes_cli

            hermes_cli = _import_hermes_cli()
            _ensure_hermes_source_root(hermes_cli)
            from hermes_cli import kanban_db, kanban_db_connect

            self._kanban_db_module = self._kanban_db_module or kanban_db
            self._connect_fn = self._connect_fn or kanban_db_connect.connect
        return self._kanban_db_module, self._connect_fn

    def heartbeat(
        self,
        *,
        board: str,
        task_id: str,
        run_id: int,
        dispatch_key: str,
    ) -> bool:
        module, connect = self._dependencies()
        conn = connect(board=board)
        claim_lock = f"controller:{dispatch_key}"
        try:
            task = module.get_task(conn, task_id)
            run = conn.execute(
                "SELECT status, outcome, claim_lock FROM task_runs WHERE id = ? AND task_id = ?",
                (run_id, task_id),
            ).fetchone()
            if (
                task is None
                or getattr(task, "status", None) != "running"
                or getattr(task, "current_run_id", None) != run_id
                or getattr(task, "claim_lock", None) != claim_lock
                or run is None
                or str(run["status"] or "") != "running"
                or run["outcome"] is not None
                or str(run["claim_lock"] or "") != claim_lock
            ):
                return False
            return bool(
                module.heartbeat_claim(
                    conn,
                    task_id,
                    ttl_seconds=1800,
                    claimer=claim_lock,
                )
            )
        finally:
            conn.close()


class AcceptedTaskAgentInvoker:
    """Invoke one accepted Implementation task and persist immutable provenance."""

    def __init__(
        self,
        *,
        graph_artifact_store: SQLiteArtifactStore,
        activation_artifact_store: SQLiteArtifactStore,
        dispatch_artifact_store: SQLiteArtifactStore,
        delivery_artifact_store: SQLiteArtifactStore,
        result_artifact_store: SQLiteArtifactStore,
        hydration_root: Path,
        claim_lease_port: NativeClaimLeasePort,
        executor: AgentExecutor,
        predecessor_evidence_port: NativePredecessorEvidencePort | None = None,
        heartbeat_interval_seconds: float = 60.0,
    ) -> None:
        self._graph_artifact_store = graph_artifact_store
        self._activation_artifact_store = activation_artifact_store
        self._dispatch_artifact_store = dispatch_artifact_store
        self._delivery_artifact_store = delivery_artifact_store
        self._result_artifact_store = result_artifact_store
        self._hydration_root = Path(hydration_root).resolve()
        self._claim_lease_port = claim_lease_port
        self._executor = executor
        self._predecessor_evidence_port = (
            predecessor_evidence_port or HermesNativePredecessorEvidencePort()
        )
        self._heartbeat_interval_seconds = heartbeat_interval_seconds

    def invoke(self, request: Any) -> dict[str, Any]:
        contract = self._request(request)
        dispatch_record = self._dispatch_artifact_store.get(contract["dispatch_artifact_id"])
        dispatch = self._dispatch_payload(dispatch_record, contract)
        graph_record = self._graph_artifact_store.get(dispatch["graph_artifact_id"])
        graph = self._graph(graph_record, dispatch)
        activation_record = self._activation_artifact_store.get(
            dispatch["activation_artifact_id"]
        )
        activation = self._activation(activation_record, dispatch)
        execution = self._execution_contract(graph, activation, dispatch)
        predecessor_evidence = self._predecessor_evidence(
            graph=graph,
            activation=activation,
            dispatch=dispatch,
        )
        invocation_key = canonical_json_sha256(
            {
                "dispatch_artifact_id": dispatch_record.artifact_id,
                "dispatch_artifact_sha256": dispatch_record.output_sha256,
                "task_id": dispatch["task_id"],
                "run_id": dispatch["run_id"],
            }
        )
        input_value = dict(contract)
        try:
            existing = self._delivery_artifact_store.get(contract["artifact_id"])
        except ArtifactStoreError as exc:
            if exc.code != "ARTIFACT_NOT_FOUND":
                raise
        else:
            payload = self._replay_payload(
                existing, input_value, invocation_key, dispatch, execution
            )
            return self._public_receipt(existing, payload, idempotent_replay=True)

        artifact_directory = self._stage_inputs(
            invocation_key=invocation_key,
            skill=execution["skills"][0],
            graph_record=graph_record,
            activation_record=activation_record,
            dispatch_record=dispatch_record,
            predecessor_evidence=predecessor_evidence,
        )
        agent_input = {
            "schema": "controller-task-agent-input-v1",
            "artifact_directory": artifact_directory.as_posix(),
            "accepted_artifacts": {
                "graph": {
                    "artifact_id": graph_record.artifact_id,
                    "sha256": graph_record.output_sha256,
                },
                "activation": {
                    "artifact_id": activation_record.artifact_id,
                    "sha256": activation_record.output_sha256,
                },
                "dispatch": {
                    "artifact_id": dispatch_record.artifact_id,
                    "sha256": dispatch_record.output_sha256,
                },
            },
            "native_claim": {
                "board": dispatch["board"],
                "task_id": dispatch["task_id"],
                "run_id": dispatch["run_id"],
                "dispatch_key": dispatch["dispatch_key"],
            },
            "predecessor_evidence": {
                "file": "predecessors.json",
                "sha256": canonical_json_sha256(predecessor_evidence),
            },
        }
        invocation = StepInvocation(
            run_id=f"kanban:{dispatch['board']}:{dispatch['run_id']}",
            invocation_id=invocation_key,
            step_id=execution["step_id"],
            goal=execution["goal"],
            allowed_toolsets=execution["toolsets"],
            acceptance_criteria=execution["acceptance_criteria"],
            output_schema=self._delivery_output_schema(),
            input=agent_input,
            working_directory=dispatch["workspace_path"],
            skills=execution["skills"],
        )
        result_artifact_id = f"agent-result:{invocation_key}"
        result_input = {
            "schema": _RESULT_SCHEMA,
            "invocation_key": invocation_key,
            "receipt_artifact_id": contract["artifact_id"],
            "dispatch_artifact_id": dispatch_record.artifact_id,
            "dispatch_artifact_sha256": dispatch_record.output_sha256,
        }
        try:
            result_record = self._result_artifact_store.get(result_artifact_id)
        except ArtifactStoreError as exc:
            if exc.code != "ARTIFACT_NOT_FOUND":
                raise
            try:
                result = self._invoke_with_claim_heartbeat(invocation, dispatch)
            except KanbanAgentDeliveryError:
                raise
            except Exception as invocation_exc:
                raise KanbanAgentDeliveryError(
                    "AGENT_INVOCATION_FAILED", "fresh Agent invocation failed"
                ) from invocation_exc
            agent_id = _identity(result.agent_id, "agent_id")
            delivery = self._delivery(
                result.output,
                dispatch,
                invocation_key=invocation_key,
                agent_id=agent_id,
                expected_step_id=execution["step_id"],
                persist=False,
            )
            result_payload = {
                "schema": _RESULT_SCHEMA,
                "invocation_key": invocation_key,
                "agent_id": agent_id,
                "task_id": dispatch["task_id"],
                "run_id": dispatch["run_id"],
                "dispatch_key": dispatch["dispatch_key"],
                "delivery": delivery,
            }
            self._result_artifact_store.put(
                ArtifactRecord(
                    artifact_id=result_artifact_id,
                    run_id=str(dispatch["run_id"]),
                    step_id="task_agent_result",
                    attempt=1,
                    executor_kind="agent",
                    input_sha256=canonical_json_sha256(result_input),
                    output_sha256=canonical_json_sha256(result_payload),
                    timeout_seconds=300,
                    validation_result="accepted",
                    executor_identity=agent_id,
                    payload=result_payload,
                ),
                input_value=result_input,
            )
            result_record = self._result_artifact_store.get(result_artifact_id)
            result_payload = self._result_payload(
                result_record,
                result_input,
                invocation_key,
                dispatch,
                expected_step_id=execution["step_id"],
            )
            acknowledge = getattr(
                self._executor,
                "acknowledge_persisted_result",
                None,
            )
            if callable(acknowledge):
                acknowledge(invocation.invocation_id)
        else:
            result_payload = self._result_payload(
                result_record,
                result_input,
                invocation_key,
                dispatch,
                expected_step_id=execution["step_id"],
            )
            agent_id = result_payload["agent_id"]
            delivery = result_payload["delivery"]
        delivery = self._delivery(
            delivery,
            dispatch,
            invocation_key=invocation_key,
            agent_id=agent_id,
            expected_step_id=execution["step_id"],
            persist=True,
        )
        self._require_claim_ownership(
            dispatch,
            "native claim ownership was lost before delivery acceptance",
        )
        payload = {
            "schema": _RECEIPT_SCHEMA,
            "invocation_key": invocation_key,
            "agent_id": agent_id,
            "board": dispatch["board"],
            "issue_number": dispatch["issue_number"],
            "generation": dispatch["generation"],
            "workspace_path": dispatch["workspace_path"],
            "task_id": dispatch["task_id"],
            "run_id": dispatch["run_id"],
            "dispatch_key": dispatch["dispatch_key"],
            "dispatch_artifact_id": dispatch_record.artifact_id,
            "dispatch_artifact_sha256": dispatch_record.output_sha256,
            "execution_contract": {
                "step_id": invocation.step_id,
                "allowed_toolsets": list(invocation.allowed_toolsets),
                "skills": list(invocation.skills),
                "workspace_path": invocation.working_directory,
            },
            "accepted_artifacts": agent_input["accepted_artifacts"],
            "delivery": delivery,
        }
        record = ArtifactRecord(
            artifact_id=contract["artifact_id"],
            run_id=str(dispatch["run_id"]),
            step_id="task_agent_delivery",
            attempt=1,
            executor_kind="agent",
            input_sha256=canonical_json_sha256(input_value),
            output_sha256=canonical_json_sha256(payload),
            timeout_seconds=300,
            validation_result="accepted",
            executor_identity=agent_id,
            payload=payload,
        )
        stored = self._delivery_artifact_store.put(record, input_value=input_value)
        return self._public_receipt(stored, payload, idempotent_replay=False)

    def _invoke_with_claim_heartbeat(
        self, invocation: StepInvocation, dispatch: Mapping[str, Any]
    ) -> AgentResult:
        heartbeat_args = {
            "board": str(dispatch["board"]),
            "task_id": str(dispatch["task_id"]),
            "run_id": int(dispatch["run_id"]),
            "dispatch_key": str(dispatch["dispatch_key"]),
        }
        if not self._claim_lease_port.heartbeat(**heartbeat_args):
            raise KanbanAgentDeliveryError(
                "CLAIM_OWNERSHIP_LOST", "native claim ownership was lost before Agent execution"
            )
        stop = threading.Event()
        ownership_lost = threading.Event()

        def maintain() -> None:
            while not stop.wait(self._heartbeat_interval_seconds):
                try:
                    if not self._claim_lease_port.heartbeat(**heartbeat_args):
                        ownership_lost.set()
                        return
                except Exception:
                    ownership_lost.set()
                    return

        worker = threading.Thread(
            target=maintain,
            name=f"kwc-claim-heartbeat-{dispatch['run_id']}",
            daemon=True,
        )
        worker.start()
        try:
            result = self._executor.invoke(invocation)
        finally:
            stop.set()
            worker.join(timeout=max(1.0, self._heartbeat_interval_seconds))
        if ownership_lost.is_set() or not self._claim_lease_port.heartbeat(**heartbeat_args):
            raise KanbanAgentDeliveryError(
                "CLAIM_OWNERSHIP_LOST", "native claim ownership was lost during Agent execution"
            )
        return result

    def _require_claim_ownership(
        self, dispatch: Mapping[str, Any], message: str
    ) -> None:
        if not self._claim_lease_port.heartbeat(
            board=str(dispatch["board"]),
            task_id=str(dispatch["task_id"]),
            run_id=int(dispatch["run_id"]),
            dispatch_key=str(dispatch["dispatch_key"]),
        ):
            raise KanbanAgentDeliveryError("CLAIM_OWNERSHIP_LOST", message)

    def _result_payload(
        self,
        record: ArtifactRecord,
        input_value: Mapping[str, Any],
        invocation_key: str,
        dispatch: Mapping[str, Any],
        *,
        expected_step_id: str,
    ) -> dict[str, Any]:
        if (
            record.validation_result != "accepted"
            or record.step_id != "task_agent_result"
            or record.executor_kind != "agent"
            or record.run_id != str(dispatch["run_id"])
            or record.input_sha256 != canonical_json_sha256(input_value)
            or record.output_sha256 != canonical_json_sha256(record.payload)
        ):
            raise KanbanAgentDeliveryError("CONFLICTING_REPLAY", "Agent result replay is invalid")
        payload = dict(record.payload)
        if (
            set(payload)
            != {
                "schema",
                "invocation_key",
                "agent_id",
                "task_id",
                "run_id",
                "dispatch_key",
                "delivery",
            }
            or payload.get("schema") != _RESULT_SCHEMA
            or payload.get("invocation_key") != invocation_key
            or payload.get("task_id") != dispatch["task_id"]
            or payload.get("run_id") != dispatch["run_id"]
            or payload.get("dispatch_key") != dispatch["dispatch_key"]
            or payload.get("agent_id") != record.executor_identity
        ):
            raise KanbanAgentDeliveryError("CONFLICTING_REPLAY", "Agent result replay conflicts")
        payload["agent_id"] = _identity(payload.get("agent_id"), "agent_id")
        payload["delivery"] = self._delivery(
            payload.get("delivery"),
            dispatch,
            invocation_key=invocation_key,
            agent_id=payload["agent_id"],
            expected_step_id=expected_step_id,
            persist=False,
        )
        return payload

    def _predecessor_evidence(
        self,
        *,
        graph: Mapping[str, Any],
        activation: Mapping[str, Any],
        dispatch: Mapping[str, Any],
    ) -> dict[str, Any]:
        current_key = next(
            (
                card.get("key")
                for card in graph["cards"]
                if isinstance(card, Mapping)
                and card.get("idempotency_key")
                == activation["task"].get("idempotency_key")
            ),
            None,
        )
        if not isinstance(current_key, str):
            raise KanbanAgentDeliveryError(
                "PREDECESSOR_EVIDENCE_INVALID", "current graph card is unavailable"
            )
        parent_keys = {
            link.get("parent_key")
            for link in graph["links"]
            if isinstance(link, Mapping)
            and link.get("child_key") == current_key
            and link.get("parent_key") != "source"
        }
        expected_cards = [
            card
            for card in graph["cards"]
            if isinstance(card, Mapping) and card.get("key") in parent_keys
        ]
        if not parent_keys:
            return {
                "schema": "controller-task-predecessor-evidence-v1",
                "parents": [],
            }
        native = self._predecessor_evidence_port.completed_parents(
            board=str(dispatch["board"]),
            task_id=str(dispatch["task_id"]),
        )
        by_key: dict[str, dict[str, Any]] = {}
        for item in native:
            if not isinstance(item, Mapping):
                raise KanbanAgentDeliveryError(
                    "PREDECESSOR_EVIDENCE_INVALID", "native predecessor evidence is invalid"
                )
            task = item.get("task")
            completion = item.get("completion")
            run = item.get("run")
            if (
                not isinstance(task, Mapping)
                or not isinstance(completion, Mapping)
                or not isinstance(run, Mapping)
            ):
                raise KanbanAgentDeliveryError(
                    "PREDECESSOR_EVIDENCE_INVALID", "native predecessor evidence is incomplete"
                )
            candidates = [
                card
                for card in expected_cards
                if card.get("idempotency_key") == task.get("idempotency_key")
            ]
            if len(candidates) != 1:
                raise KanbanAgentDeliveryError(
                    "PREDECESSOR_EVIDENCE_INVALID",
                    "native predecessor is not graph-bound",
                )
            card = candidates[0]
            expected_task = {
                "task_id": task.get("id"),
                "title": card.get("title"),
                "body_sha256": card.get("body_sha256"),
                "assignee": card.get("assignee"),
                "status": "done",
                "workspace_kind": "dir",
                "workspace_path": dispatch["workspace_path"],
                "skills": list(card.get("skills") or []),
                "idempotency_key": card.get("idempotency_key"),
            }
            try:
                HermesNativeKanbanDispatchPort._verify_task(
                    task,
                    expected_task,
                    allowed_statuses={"done"},
                )
            except Exception as exc:
                raise KanbanAgentDeliveryError(
                    "PREDECESSOR_EVIDENCE_INVALID",
                    "native predecessor task provenance is invalid",
                ) from exc
            metadata = completion.get("metadata")
            event_payload = completion.get("payload")
            if (
                type(completion.get("run_id")) is not int
                or completion["run_id"] < 1
                or not isinstance(metadata, Mapping)
                or set(metadata)
                != {
                    "controller_completion_key",
                    "dispatch_artifact_id",
                    "dispatch_artifact_sha256",
                    "delivery_artifact_id",
                    "delivery_artifact_sha256",
                    "result_artifacts",
                }
                or not isinstance(event_payload, Mapping)
                or set(event_payload) != {"result", "summary", "metadata"}
                or dict(event_payload.get("metadata") or {}) != dict(metadata)
            ):
                raise KanbanAgentDeliveryError(
                    "PREDECESSOR_EVIDENCE_INVALID",
                    "native predecessor completion provenance is invalid",
                )
            if (
                task.get("current_run_id") is not None
                or task.get("claim_lock") is not None
                or task.get("claim_expires") is not None
                or task.get("worker_pid") is not None
                or task.get("result")
                != f"Accepted controller delivery artifact {metadata.get('delivery_artifact_id')}"
                or run.get("id") != completion.get("run_id")
                or run.get("task_id") != task.get("id")
                or run.get("status") != "done"
                or run.get("outcome") != "completed"
                or run.get("claim_expires") is not None
            ):
                raise KanbanAgentDeliveryError(
                    "PREDECESSOR_EVIDENCE_INVALID",
                    "native predecessor terminal state is invalid",
                )
            parent_dispatch_record = self._dispatch_artifact_store.get(
                _identity(metadata.get("dispatch_artifact_id"), "parent dispatch artifact")
            )
            parent_dispatch = self._dispatch_payload(
                parent_dispatch_record,
                {
                    "dispatch_artifact_id": parent_dispatch_record.artifact_id,
                    "dispatch_artifact_sha256": metadata.get("dispatch_artifact_sha256"),
                },
            )
            if (
                parent_dispatch_record.output_sha256
                != metadata.get("dispatch_artifact_sha256")
                or parent_dispatch.get("task_id") != task.get("id")
                or parent_dispatch.get("run_id") != completion.get("run_id")
            ):
                raise KanbanAgentDeliveryError(
                    "PREDECESSOR_EVIDENCE_INVALID", "parent dispatch provenance conflicts"
                )
            parent_activation_record = self._activation_artifact_store.get(
                parent_dispatch["activation_artifact_id"]
            )
            parent_activation = self._activation(parent_activation_record, parent_dispatch)
            parent_execution = self._execution_contract(
                graph, parent_activation, parent_dispatch
            )
            parent_delivery_record = self._delivery_artifact_store.get(
                _identity(metadata.get("delivery_artifact_id"), "parent delivery artifact")
            )
            parent_delivery_input = {
                "schema": _REQUEST_SCHEMA,
                "artifact_id": parent_delivery_record.artifact_id,
                "dispatch_artifact_id": parent_dispatch_record.artifact_id,
                "dispatch_artifact_sha256": parent_dispatch_record.output_sha256,
            }
            parent_invocation_key = canonical_json_sha256(
                {
                    "dispatch_artifact_id": parent_dispatch_record.artifact_id,
                    "dispatch_artifact_sha256": parent_dispatch_record.output_sha256,
                    "task_id": parent_dispatch["task_id"],
                    "run_id": parent_dispatch["run_id"],
                }
            )
            parent_delivery = self._replay_payload(
                parent_delivery_record,
                parent_delivery_input,
                parent_invocation_key,
                parent_dispatch,
                parent_execution,
            )
            if (
                parent_delivery_record.output_sha256
                != metadata.get("delivery_artifact_sha256")
                or list(parent_delivery["delivery"]["result_artifacts"])
                != list(metadata.get("result_artifacts") or [])
            ):
                raise KanbanAgentDeliveryError(
                    "PREDECESSOR_EVIDENCE_INVALID", "parent delivery provenance conflicts"
                )
            results: list[dict[str, Any]] = []
            for ref in parent_delivery["delivery"]["result_artifacts"]:
                result_record = self._result_artifact_store.get(ref["artifact_id"])
                results.append(
                    {
                        "artifact_id": result_record.artifact_id,
                        "sha256": result_record.output_sha256,
                        "step_id": result_record.step_id,
                        "logical_id": result_record.payload["logical_id"],
                        "payload": result_record.payload["payload"],
                    }
                )
            by_key[str(card["key"])] = {
                "card_key": card["key"],
                "role": card["role"],
                "task_id": task["id"],
                "run_id": completion["run_id"],
                "dispatch_artifact_id": parent_dispatch_record.artifact_id,
                "dispatch_artifact_sha256": parent_dispatch_record.output_sha256,
                "delivery_artifact_id": parent_delivery_record.artifact_id,
                "delivery_artifact_sha256": parent_delivery_record.output_sha256,
                "results": results,
            }
        if set(by_key) != parent_keys:
            raise KanbanAgentDeliveryError(
                "PREDECESSOR_EVIDENCE_INVALID",
                "accepted predecessor set is incomplete",
            )
        return {
            "schema": "controller-task-predecessor-evidence-v1",
            "parents": [by_key[str(card["key"])] for card in expected_cards],
        }

    def _stage_inputs(
        self,
        *,
        invocation_key: str,
        skill: str,
        graph_record: ArtifactRecord,
        activation_record: ArtifactRecord,
        dispatch_record: ArtifactRecord,
        predecessor_evidence: Mapping[str, Any],
    ) -> Path:
        directory = self._hydration_root / invocation_key
        directory.mkdir(parents=True, exist_ok=True)
        records = {
            "graph.json": graph_record.payload,
            "activation.json": activation_record.payload,
            "dispatch.json": dispatch_record.payload,
            "predecessors.json": predecessor_evidence,
            "manifest.json": {
                "schema": "controller-task-agent-hydration-v1",
                "invocation_key": invocation_key,
                "skill": skill,
                "artifacts": {
                    "graph": {
                        "artifact_id": graph_record.artifact_id,
                        "step_id": graph_record.step_id,
                        "sha256": graph_record.output_sha256,
                    },
                    "activation": {
                        "artifact_id": activation_record.artifact_id,
                        "step_id": activation_record.step_id,
                        "sha256": activation_record.output_sha256,
                    },
                    "dispatch": {
                        "artifact_id": dispatch_record.artifact_id,
                        "step_id": dispatch_record.step_id,
                        "sha256": dispatch_record.output_sha256,
                    },
                    "predecessors": {
                        "file": "predecessors.json",
                        "sha256": canonical_json_sha256(predecessor_evidence),
                    },
                },
            },
        }
        for filename, payload in records.items():
            self._write_immutable(directory / filename, canonical_json(payload) + "\n")
        skill_path = Path(__file__).resolve().parents[3] / "skills" / skill / "SKILL.md"
        if not skill_path.is_file():
            raise KanbanAgentDeliveryError(
                "EXECUTION_CONTRACT_INVALID", "accepted Skill source is unavailable"
            )
        self._write_immutable(directory / "skill.md", skill_path.read_text(encoding="utf-8"))
        return directory

    @staticmethod
    def _write_immutable(path: Path, content: str) -> None:
        if path.exists():
            if path.read_text(encoding="utf-8") != content:
                raise KanbanAgentDeliveryError(
                    "HYDRATION_CONFLICT", "staged Agent artifact conflicts with accepted provenance"
                )
            return
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(path)

    @staticmethod
    def _request(value: Any) -> dict[str, Any]:
        if not isinstance(value, Mapping) or set(value) != _REQUEST_FIELDS:
            raise KanbanAgentDeliveryError("REQUEST_INVALID", "agent invocation request is invalid")
        request = dict(value)
        if request.get("schema") != _REQUEST_SCHEMA:
            raise KanbanAgentDeliveryError("REQUEST_INVALID", "agent invocation schema is invalid")
        for field in ("artifact_id", "dispatch_artifact_id"):
            request[field] = _identity(request.get(field), field)
        digest = request.get("dispatch_artifact_sha256")
        if not isinstance(digest, str) or _SHA256.fullmatch(digest) is None:
            raise KanbanAgentDeliveryError("REQUEST_INVALID", "dispatch artifact digest is invalid")
        return request

    @staticmethod
    def _dispatch_payload(record: ArtifactRecord, contract: Mapping[str, Any]) -> dict[str, Any]:
        if (
            record.artifact_id != contract["dispatch_artifact_id"]
            or record.output_sha256 != contract["dispatch_artifact_sha256"]
            or record.validation_result != "accepted"
            or record.step_id != "task_dispatch_claim"
            or record.executor_kind != "deterministic"
            or record.executor_identity != "controller-task-dispatch-v1"
            or record.output_sha256 != canonical_json_sha256(record.payload)
        ):
            raise KanbanAgentDeliveryError("DISPATCH_ARTIFACT_INVALID", "dispatch artifact is invalid")
        try:
            return AcceptedTaskDispatcher._validate_receipt_payload(record.payload)
        except KanbanDispatchError as exc:
            raise KanbanAgentDeliveryError(
                "DISPATCH_ARTIFACT_INVALID", "dispatch payload is invalid"
            ) from exc

    @staticmethod
    def _graph(record: ArtifactRecord, dispatch: Mapping[str, Any]) -> dict[str, Any]:
        try:
            return AcceptedTaskGraphMaterializer._graph(
                record,
                {
                    "artifact_sha256": dispatch["graph_artifact_sha256"],
                    "board": dispatch["board"],
                    "issue_number": dispatch["issue_number"],
                    "generation": dispatch["generation"],
                    "workspace_path": dispatch["workspace_path"],
                },
            )
        except Exception as exc:
            raise KanbanAgentDeliveryError(
                getattr(exc, "code", "GRAPH_ARTIFACT_INVALID"),
                "graph artifact is invalid for Agent invocation",
            ) from exc

    @staticmethod
    def _activation(record: ArtifactRecord, dispatch: Mapping[str, Any]) -> dict[str, Any]:
        try:
            return AcceptedTaskActivator._payload(
                record,
                {
                    "artifact_sha256": dispatch["activation_artifact_sha256"],
                    "board": dispatch["board"],
                    "issue_number": dispatch["issue_number"],
                    "generation": dispatch["generation"],
                    "workspace_path": dispatch["workspace_path"],
                    "task_id": dispatch["task_id"],
                },
            )
        except Exception as exc:
            raise KanbanAgentDeliveryError(
                getattr(exc, "code", "ACTIVATION_ARTIFACT_INVALID"),
                "activation artifact is invalid for Agent invocation",
            ) from exc

    @staticmethod
    def _execution_contract(
        graph: Mapping[str, Any], activation: Mapping[str, Any], dispatch: Mapping[str, Any]
    ) -> dict[str, Any]:
        try:
            AcceptedTaskDispatcher._expected_task(graph, activation, dispatch)
        except KanbanDispatchError as exc:
            raise KanbanAgentDeliveryError(exc.code, "accepted task identity is invalid") from exc
        task_key = activation["task"].get("idempotency_key")
        candidates = [
            card
            for card in graph["cards"]
            if isinstance(card, Mapping) and card.get("idempotency_key") == task_key
        ]
        if len(candidates) != 1:
            raise KanbanAgentDeliveryError(
                "EXECUTION_CONTRACT_INVALID", "task role is not uniquely graph-bound"
            )
        return _role_execution_contract(candidates[0].get("role"))

    def _delivery(
        self,
        value: Any,
        dispatch: Mapping[str, Any],
        *,
        invocation_key: str,
        agent_id: str,
        expected_step_id: str,
        persist: bool = True,
    ) -> dict[str, Any]:
        if not isinstance(value, Mapping):
            raise KanbanAgentDeliveryError("DELIVERY_INVALID", "Agent delivery is invalid")
        delivery = dict(value)
        if set(delivery) != {
            "schema",
            "status",
            "task_id",
            "run_id",
            "dispatch_key",
            "result_artifacts",
        } or delivery.get("schema") != _DELIVERY_SCHEMA:
            raise KanbanAgentDeliveryError("DELIVERY_INVALID", "Agent delivery schema is invalid")
        if (
            delivery.get("task_id") != dispatch["task_id"]
            or delivery.get("run_id") != dispatch["run_id"]
            or delivery.get("dispatch_key") != dispatch["dispatch_key"]
        ):
            raise KanbanAgentDeliveryError(
                "DELIVERY_IDENTITY_MISMATCH", "Agent delivery identity does not match native claim"
            )
        if delivery.get("status") not in {"completed", "blocked"}:
            raise KanbanAgentDeliveryError("DELIVERY_INVALID", "Agent delivery status is invalid")
        refs = delivery.get("result_artifacts")
        if not isinstance(refs, Sequence) or isinstance(refs, (str, bytes)) or len(refs) > 32:
            raise KanbanAgentDeliveryError("DELIVERY_INVALID", "Agent result artifacts are invalid")
        if delivery.get("status") == "completed" and not refs:
            raise KanbanAgentDeliveryError(
                "DELIVERY_INVALID", "completed delivery requires a typed result artifact"
            )
        normalized: list[dict[str, str]] = []
        for index, ref in enumerate(refs):
            if not isinstance(ref, Mapping) or set(ref) != {
                "artifact_id",
                "step_id",
                "sha256",
                "payload",
            }:
                raise KanbanAgentDeliveryError("DELIVERY_INVALID", "Agent result artifact is invalid")
            logical_id = _identity(ref.get("artifact_id"), "result artifact")
            step_id = _identity(ref.get("step_id"), "result step")
            if step_id != expected_step_id:
                raise KanbanAgentDeliveryError(
                    "DELIVERY_INVALID", "Agent result step does not match execution contract"
                )
            digest = ref.get("sha256")
            payload = ref.get("payload")
            if (
                not isinstance(payload, Mapping)
                or not isinstance(digest, str)
                or _SHA256.fullmatch(digest) is None
                or digest != canonical_json_sha256(payload)
            ):
                raise KanbanAgentDeliveryError("DELIVERY_INVALID", "Agent result artifact digest is invalid")
            result_input = {
                "schema": "controller-task-result-input-v1",
                "invocation_key": invocation_key,
                "task_id": dispatch["task_id"],
                "run_id": dispatch["run_id"],
                "dispatch_key": dispatch["dispatch_key"],
                "logical_id": logical_id,
                "result_index": index,
                "step_id": step_id,
                "payload_sha256": digest,
            }
            artifact_id = f"task-result:{invocation_key}:{index}"
            stored_payload = {
                "schema": "controller-task-result-record-v1",
                "invocation_key": invocation_key,
                "logical_id": logical_id,
                "result_index": index,
                "step_id": step_id,
                "payload_sha256": digest,
                "payload": dict(payload),
            }
            if not persist:
                normalized.append(dict(ref))
                continue
            stored = self._result_artifact_store.put(
                ArtifactRecord(
                    artifact_id=artifact_id,
                    run_id=str(dispatch["run_id"]),
                    step_id=step_id,
                    attempt=1,
                    executor_kind="agent",
                    input_sha256=canonical_json_sha256(result_input),
                    output_sha256=canonical_json_sha256(stored_payload),
                    timeout_seconds=300,
                    validation_result="accepted",
                    executor_identity=agent_id,
                    payload=stored_payload,
                ),
                input_value=result_input,
            )
            normalized.append(
                {
                    "artifact_id": stored.artifact_id,
                    "step_id": stored.step_id,
                    "sha256": stored.output_sha256,
                }
            )
        delivery["result_artifacts"] = normalized
        return delivery

    def _verified_delivery_refs(
        self,
        value: Any,
        dispatch: Mapping[str, Any],
        *,
        invocation_key: str,
        expected_step_id: str,
        expected_agent_id: str,
    ) -> dict[str, Any]:
        if not isinstance(value, Mapping):
            raise KanbanAgentDeliveryError("CONFLICTING_REPLAY", "Agent delivery is invalid")
        delivery = dict(value)
        if (
            set(delivery)
            != {
                "schema",
                "status",
                "task_id",
                "run_id",
                "dispatch_key",
                "result_artifacts",
            }
            or delivery.get("schema") != _DELIVERY_SCHEMA
        ):
            raise KanbanAgentDeliveryError("CONFLICTING_REPLAY", "Agent delivery schema conflicts")
        refs = delivery.get("result_artifacts")
        if (
            not isinstance(refs, Sequence)
            or isinstance(refs, (str, bytes))
            or len(refs) > 32
        ):
            raise KanbanAgentDeliveryError("CONFLICTING_REPLAY", "Agent result refs are invalid")
        if delivery.get("status") == "completed" and not refs:
            raise KanbanAgentDeliveryError(
                "CONFLICTING_REPLAY", "completed Agent delivery has no result refs"
            )
        for index, ref in enumerate(refs):
            if not isinstance(ref, Mapping) or set(ref) != {"artifact_id", "step_id", "sha256"}:
                raise KanbanAgentDeliveryError("CONFLICTING_REPLAY", "Agent result ref is invalid")
            record = self._result_artifact_store.get(_identity(ref.get("artifact_id"), "result artifact"))
            payload = record.payload
            logical_id = payload.get("logical_id") if isinstance(payload, Mapping) else None
            payload_value = payload.get("payload") if isinstance(payload, Mapping) else None
            payload_sha256 = (
                payload.get("payload_sha256") if isinstance(payload, Mapping) else None
            )
            expected_artifact_id = f"task-result:{invocation_key}:{index}"
            expected_input = {
                "schema": "controller-task-result-input-v1",
                "invocation_key": invocation_key,
                "task_id": dispatch["task_id"],
                "run_id": dispatch["run_id"],
                "dispatch_key": dispatch["dispatch_key"],
                "logical_id": logical_id,
                "result_index": index,
                "step_id": expected_step_id,
                "payload_sha256": payload_sha256,
            }
            if (
                record.artifact_id != expected_artifact_id
                or ref.get("artifact_id") != expected_artifact_id
                or record.step_id != expected_step_id
                or record.validation_result != "accepted"
                or record.executor_kind != "agent"
                or ref.get("step_id") != expected_step_id
                or record.executor_identity != expected_agent_id
                or record.run_id != str(dispatch["run_id"])
                or record.input_sha256 != canonical_json_sha256(expected_input)
                or record.output_sha256 != ref.get("sha256")
                or record.output_sha256 != canonical_json_sha256(record.payload)
                or not isinstance(payload, Mapping)
                or set(payload)
                != {
                    "schema",
                    "invocation_key",
                    "logical_id",
                    "result_index",
                    "step_id",
                    "payload_sha256",
                    "payload",
                }
                or payload.get("schema") != "controller-task-result-record-v1"
                or payload.get("invocation_key") != invocation_key
                or payload.get("result_index") != index
                or _identity(logical_id, "result logical id") != logical_id
                or payload.get("step_id") != expected_step_id
                or not isinstance(payload_value, Mapping)
                or payload_sha256 != canonical_json_sha256(payload_value)
            ):
                raise KanbanAgentDeliveryError("CONFLICTING_REPLAY", "Agent result ref conflicts")
        if (
            delivery.get("task_id") != dispatch["task_id"]
            or delivery.get("run_id") != dispatch["run_id"]
            or delivery.get("dispatch_key") != dispatch["dispatch_key"]
            or delivery.get("status") not in {"completed", "blocked"}
        ):
            raise KanbanAgentDeliveryError("CONFLICTING_REPLAY", "Agent delivery conflicts")
        return delivery

    def _replay_payload(
        self,
        record: ArtifactRecord,
        input_value: Mapping[str, Any],
        invocation_key: str,
        dispatch: Mapping[str, Any],
        execution: Mapping[str, Any],
    ) -> dict[str, Any]:
        if (
            record.validation_result != "accepted"
            or record.step_id != "task_agent_delivery"
            or record.executor_kind != "agent"
            or record.input_sha256 != canonical_json_sha256(input_value)
            or record.output_sha256 != canonical_json_sha256(record.payload)
        ):
            raise KanbanAgentDeliveryError("CONFLICTING_REPLAY", "Agent delivery replay is invalid")
        payload = dict(record.payload)
        expected_fields = {
            "schema",
            "invocation_key",
            "agent_id",
            "board",
            "issue_number",
            "generation",
            "workspace_path",
            "task_id",
            "run_id",
            "dispatch_key",
            "dispatch_artifact_id",
            "dispatch_artifact_sha256",
            "execution_contract",
            "accepted_artifacts",
            "delivery",
        }
        expected_accepted_artifacts = {
            "graph": {
                "artifact_id": dispatch["graph_artifact_id"],
                "sha256": dispatch["graph_artifact_sha256"],
            },
            "activation": {
                "artifact_id": dispatch["activation_artifact_id"],
                "sha256": dispatch["activation_artifact_sha256"],
            },
            "dispatch": {
                "artifact_id": input_value["dispatch_artifact_id"],
                "sha256": input_value["dispatch_artifact_sha256"],
            },
        }
        try:
            agent_id = _identity(payload.get("agent_id"), "agent_id")
        except KanbanAgentDeliveryError as exc:
            raise KanbanAgentDeliveryError(
                "CONFLICTING_REPLAY", "Agent identity replay conflicts"
            ) from exc
        if (
            set(payload) != expected_fields
            or payload.get("schema") != _RECEIPT_SCHEMA
            or payload.get("invocation_key") != invocation_key
            or payload.get("board") != dispatch["board"]
            or payload.get("issue_number") != dispatch["issue_number"]
            or payload.get("generation") != dispatch["generation"]
            or payload.get("workspace_path") != dispatch["workspace_path"]
            or payload.get("task_id") != dispatch["task_id"]
            or payload.get("run_id") != dispatch["run_id"]
            or payload.get("dispatch_key") != dispatch["dispatch_key"]
            or payload.get("dispatch_artifact_id") != input_value["dispatch_artifact_id"]
            or payload.get("dispatch_artifact_sha256")
            != input_value["dispatch_artifact_sha256"]
            or agent_id != record.executor_identity
            or payload.get("accepted_artifacts") != expected_accepted_artifacts
        ):
            raise KanbanAgentDeliveryError("CONFLICTING_REPLAY", "Agent delivery replay conflicts")
        execution_contract = payload.get("execution_contract")
        if (
            not isinstance(execution_contract, Mapping)
            or set(execution_contract)
            != {"step_id", "allowed_toolsets", "skills", "workspace_path"}
            or execution_contract.get("step_id") != execution["step_id"]
            or tuple(execution_contract.get("allowed_toolsets") or ()) != tuple(execution["toolsets"])
            or tuple(execution_contract.get("skills") or ()) != tuple(execution["skills"])
            or execution_contract.get("workspace_path") != dispatch["workspace_path"]
        ):
            raise KanbanAgentDeliveryError(
                "CONFLICTING_REPLAY", "Agent execution contract replay conflicts"
            )
        self._verified_delivery_refs(
            payload.get("delivery"),
            dispatch,
            invocation_key=invocation_key,
            expected_step_id=execution["step_id"],
            expected_agent_id=agent_id,
        )
        return payload

    @staticmethod
    def _delivery_output_schema() -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "schema": {"type": "string", "const": _DELIVERY_SCHEMA},
                "status": {"type": "string", "enum": ["completed", "blocked"]},
                "task_id": {"type": "string"},
                "run_id": {"type": "integer", "minimum": 1},
                "dispatch_key": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                "result_artifacts": {
                    "type": "array",
                    "maxItems": 32,
                    "items": {
                        "type": "object",
                        "properties": {
                            "artifact_id": {"type": "string"},
                            "step_id": {"type": "string"},
                            "sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                            "payload": {"type": "object"},
                        },
                        "required": ["artifact_id", "step_id", "sha256", "payload"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": [
                "schema",
                "status",
                "task_id",
                "run_id",
                "dispatch_key",
                "result_artifacts",
            ],
            "allOf": [
                {
                    "if": {
                        "properties": {"status": {"const": "completed"}},
                        "required": ["status"],
                    },
                    "then": {
                        "properties": {
                            "result_artifacts": {"minItems": 1},
                        }
                    },
                }
            ],
            "additionalProperties": False,
        }

    @staticmethod
    def _public_receipt(
        record: ArtifactRecord, payload: Mapping[str, Any], *, idempotent_replay: bool
    ) -> dict[str, Any]:
        return {
            "schema": _RECEIPT_SCHEMA,
            "artifact_id": record.artifact_id,
            "artifact_sha256": record.output_sha256,
            "agent_id": payload["agent_id"],
            "task_id": payload["task_id"],
            "run_id": payload["run_id"],
            "dispatch_key": payload["dispatch_key"],
            "delivery_status": payload["delivery"]["status"],
            "verified": True,
            "idempotent_replay": idempotent_replay,
        }


def _identity(value: Any, field: str) -> str:
    if not isinstance(value, str) or value != value.strip() or _ID.fullmatch(value) is None:
        raise KanbanAgentDeliveryError("REQUEST_INVALID", f"{field} is invalid")
    return value
