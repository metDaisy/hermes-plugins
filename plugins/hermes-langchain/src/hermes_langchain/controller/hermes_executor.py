"""Hermes public subagent lifecycle adapter for controller steps."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

from .agent_contracts import AgentResult, StepInvocation

_MAX_CONTEXT_CHARS = 32_000
_SUCCEEDED = "SUCCEEDED"


class HermesSubagentExecutor:
    """Execute one step through ``PluginContext.subagent_lifecycle``."""

    _handle_lock = threading.RLock()
    _handle_cache: OrderedDict[tuple[Any, str], tuple[str, Any]] = OrderedDict()
    _handle_cache_limit = 512

    def __init__(
        self,
        lifecycle: Any,
        *,
        durable_root: Path | None = None,
        defer_release: bool = False,
    ) -> None:
        self._lifecycle = lifecycle
        self._durable_root = durable_root.resolve() if durable_root is not None else None
        self._defer_release = defer_release
        self._pending_leases: dict[str, tuple[Path, str, Any, str]] = {}

    @classmethod
    def from_plugin_context(
        cls,
        ctx: Any,
        *,
        durable_root: Path | None = None,
        defer_release: bool = False,
    ) -> "HermesSubagentExecutor":
        return cls(
            ctx.subagent_lifecycle,
            durable_root=durable_root,
            defer_release=defer_release,
        )

    def invoke(self, invocation: StepInvocation) -> AgentResult:
        if not invocation.allowed_toolsets:
            raise ValueError(
                "allowed_toolsets must be non-empty; Hermes treats an empty contract as inheritance"
            )

        context = self._context(invocation)
        request: dict[str, Any] = {
            "goal": invocation.goal,
            "context": context,
            "role": "leaf",
            "allowed_toolsets": tuple(invocation.allowed_toolsets),
            "correlation_id": invocation.invocation_id,
            "metadata": {
                "run_id": invocation.run_id,
                "step_id": invocation.step_id,
                "invocation_id": invocation.invocation_id,
            },
            "output_schema": dict(invocation.output_schema),
        }
        handle, lease = self._launch_or_resume(
            request,
            invocation.invocation_id,
            durable_directory=self._durable_directory(invocation),
        )
        try:
            terminal = self._lifecycle.wait(handle)
            self._require_terminal_success(terminal)
            if self._defer_release and lease is not None:
                pending_lease = lease
                with self._handle_lock:
                    self._pending_leases[invocation.invocation_id] = pending_lease
                lease = None
                self._write_result_pending(*pending_lease)
            result = self._lifecycle.result(handle)
            output = self._output(result)
            agent_id = str(self._field(handle, "subagent_id") or "")
            if not agent_id:
                raise RuntimeError("Hermes subagent returned no stable identity")
            return AgentResult(agent_id=agent_id, output=output)
        finally:
            if lease is not None:
                self._release_recovery_lease(*lease)

    def acknowledge_persisted_result(self, invocation_id: str) -> None:
        """Release a recovery fence only after the caller durably stores the result."""

        with self._handle_lock:
            lease = self._pending_leases.get(invocation_id)
        if lease is not None:
            self._clear_result_pending(*lease)
            self._release_recovery_lease(*lease)
            with self._handle_lock:
                if self._pending_leases.get(invocation_id) == lease:
                    self._pending_leases.pop(invocation_id, None)

    def _launch_or_resume(
        self,
        request: dict[str, Any],
        invocation_id: str,
        *,
        durable_directory: Path | None,
    ) -> tuple[Any, tuple[Path, str, Any, str] | None]:
        key = (self._lifecycle, invocation_id)
        request_digest = hashlib.sha256(
            json.dumps(
                request,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        lease: tuple[Path, str, Any, str] | None = None
        with self._handle_lock:
            cached = self._handle_cache.get(key)
            if cached is not None:
                cached_digest, handle = cached
                if cached_digest != request_digest:
                    raise ValueError("Hermes invocation_id conflicts with an existing launch")
                self._handle_cache.move_to_end(key)
                if durable_directory is not None:
                    self._ensure_private_directory(durable_directory)
                    lease = self._acquire_recovery_lease(
                        durable_directory,
                        request_digest,
                    )
                return handle, lease
            if durable_directory is not None:
                self._ensure_private_directory(durable_directory)
                lease = self._acquire_recovery_lease(durable_directory, request_digest)
                try:
                    handle_path = durable_directory / "lifecycle-handle.json"
                    if handle_path.exists():
                        handle = self._reconnect_durable(
                            handle_path,
                            request_digest,
                            request,
                        )
                        self._cache_handle(key, request_digest, handle)
                        return handle, lease
                    lock_path = durable_directory / "lifecycle-launch.lock"
                    try:
                        descriptor = os.open(
                            lock_path,
                            os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                            0o600,
                        )
                    except FileExistsError as exc:
                        raise RuntimeError(
                            "Hermes invocation recovery unavailable; durable launch is unresolved"
                        ) from exc
                    os.close(descriptor)
                    launched = False
                    try:
                        handle = self._launch_with_schema_compatibility(request)
                        launched = True
                        self._write_durable_handle(handle_path, request_digest, handle)
                    except Exception:
                        if not launched and not handle_path.exists():
                            lock_path.unlink(missing_ok=True)
                        raise
                except Exception:
                    self._release_recovery_lease(*lease)
                    raise
            else:
                handle = self._launch_with_schema_compatibility(request)
            self._cache_handle(key, request_digest, handle)
            return handle, lease

    @classmethod
    def _cache_handle(
        cls,
        key: tuple[Any, str],
        request_digest: str,
        handle: Any,
    ) -> None:
        cls._handle_cache[key] = (request_digest, handle)
        cls._handle_cache.move_to_end(key)
        while len(cls._handle_cache) > cls._handle_cache_limit:
            cls._handle_cache.popitem(last=False)

    def _reconnect_durable(
        self,
        path: Path,
        request_digest: str,
        request: dict[str, Any],
    ) -> Any:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RuntimeError("Hermes durable lifecycle handle is invalid") from exc
        if (
            not isinstance(value, Mapping)
            or set(value) != {"schema", "request_digest", "handle"}
            or value.get("schema") != "hermes-subagent-handle-v2"
            or value.get("request_digest") != request_digest
            or not isinstance(value.get("handle"), Mapping)
        ):
            raise RuntimeError("Hermes durable lifecycle handle conflicts with invocation")
        handle = dict(value["handle"])
        reconnect = self._lifecycle.reconnect(handle, request)
        if not bool(self._field(reconnect, "connected", False)):
            raise RuntimeError(
                "Hermes invocation recovery unavailable; refusing duplicate launch"
            )
        return handle

    @staticmethod
    def _write_durable_handle(path: Path, request_digest: str, handle: Any) -> None:
        if isinstance(handle, Mapping):
            handle_value = dict(handle)
        elif hasattr(handle, "to_dict"):
            handle_value = dict(handle.to_dict())
        elif is_dataclass(handle):
            handle_value = asdict(handle)
        else:
            handle_value = dict(vars(handle))
        payload = {
            "schema": "hermes-subagent-handle-v2",
            "request_digest": request_digest,
            "handle": handle_value,
        }
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )
        if os.name != "nt":
            os.chmod(temporary, 0o600)
        temporary.replace(path)

    def _durable_directory(self, invocation: StepInvocation) -> Path | None:
        if self._durable_root is None:
            return None
        self._durable_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if os.name != "nt":
            os.chmod(self._durable_root, 0o700)
        invocation_digest = hashlib.sha256(invocation.invocation_id.encode()).hexdigest()
        return self._durable_root / invocation_digest

    @staticmethod
    def _ensure_private_directory(directory: Path) -> None:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if os.name != "nt":
            os.chmod(directory, 0o700)

    @classmethod
    def _acquire_recovery_lease(
        cls,
        directory: Path,
        request_digest: str,
    ) -> tuple[Path, str, Any, str]:
        pending_path = directory / "lifecycle-result-pending.json"
        pending_candidates = (pending_path, pending_path.with_suffix(".json.tmp"))
        for candidate in pending_candidates:
            if not candidate.exists():
                continue
            try:
                pending = json.loads(candidate.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise RuntimeError("Hermes unresolved result fence is invalid") from exc
            if (
                not isinstance(pending, Mapping)
                or set(pending) != {"schema", "owner_token", "request_digest"}
                or pending.get("schema") != "hermes-subagent-result-pending-v1"
                or pending.get("request_digest") != request_digest
                or not isinstance(pending.get("owner_token"), str)
                or not pending["owner_token"]
            ):
                raise RuntimeError("Hermes unresolved result fence conflicts with invocation")
            raise RuntimeError("Hermes result persistence is unresolved; refusing Agent replay")
        lock_path = directory / "lifecycle-recovery.lock"
        lock_stream = lock_path.open("a+b")
        if os.name != "nt":
            os.chmod(lock_path, 0o600)
        try:
            cls._lock_recovery_stream(lock_stream)
        except Exception:
            lock_stream.close()
            raise RuntimeError("Hermes invocation recovery is active in another process")
        path = directory / "lifecycle-recovery-lease.json"
        token = uuid.uuid4().hex
        payload = {
            "schema": "hermes-subagent-recovery-lease-v1",
            "owner_token": token,
            "pid": os.getpid(),
            "request_digest": request_digest,
            "created_at": time.time(),
        }
        try:
            if path.exists():
                try:
                    current = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError) as exc:
                    raise RuntimeError("Hermes durable recovery lease is invalid") from exc
                if (
                    not isinstance(current, Mapping)
                    or current.get("request_digest") != request_digest
                ):
                    raise RuntimeError("Hermes durable recovery lease conflicts with invocation")
            temporary = path.with_suffix(".json.tmp")
            temporary.write_text(
                json.dumps(payload, sort_keys=True, separators=(",", ":")),
                encoding="utf-8",
            )
            if os.name != "nt":
                os.chmod(temporary, 0o600)
            temporary.replace(path)
            return path, token, lock_stream, request_digest
        except Exception:
            cls._unlock_recovery_stream(lock_stream)
            lock_stream.close()
            raise

    @staticmethod
    def _lock_recovery_stream(stream: Any) -> None:
        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write(b"\0")
            stream.flush()
            os.fsync(stream.fileno())
        stream.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            return
        import fcntl

        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    @staticmethod
    def _unlock_recovery_stream(stream: Any) -> None:
        with contextlib.suppress(Exception):
            stream.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def _write_result_pending(
        lease_path: Path,
        token: str,
        _lock_stream: Any,
        request_digest: str,
    ) -> None:
        path = lease_path.parent / "lifecycle-result-pending.json"
        payload = {
            "schema": "hermes-subagent-result-pending-v1",
            "owner_token": token,
            "request_digest": request_digest,
        }
        temporary = path.with_suffix(".json.tmp")
        descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            stream.flush()
            os.fsync(stream.fileno())
        HermesSubagentExecutor._fsync_directory(path.parent)
        HermesSubagentExecutor._write_and_fsync(
            temporary,
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode(),
        )
        if os.name != "nt":
            os.chmod(temporary, 0o600)
        HermesSubagentExecutor._durable_replace(temporary, path)

    @staticmethod
    def _write_and_fsync(path: Path, payload: bytes) -> None:
        with path.open("wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())

    @staticmethod
    def _durable_replace(source: Path, target: Path) -> None:
        if os.name == "nt":
            import ctypes

            replace_existing = 0x1
            write_through = 0x8
            if not ctypes.windll.kernel32.MoveFileExW(
                str(source),
                str(target),
                replace_existing | write_through,
            ):
                raise ctypes.WinError()
        else:
            source.replace(target)
        with target.open("r+b") as stream:
            os.fsync(stream.fileno())
        HermesSubagentExecutor._fsync_directory(target.parent)

    @staticmethod
    def _fsync_directory(directory: Path) -> None:
        if os.name == "nt":
            return
        descriptor = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @staticmethod
    def _clear_result_pending(
        lease_path: Path,
        token: str,
        _lock_stream: Any,
        request_digest: str,
    ) -> None:
        path = lease_path.parent / "lifecycle-result-pending.json"
        try:
            pending = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RuntimeError("Hermes unresolved result fence is invalid") from exc
        if (
            pending.get("owner_token") != token
            or pending.get("request_digest") != request_digest
        ):
            raise RuntimeError("Hermes unresolved result fence ownership was lost")
        path.unlink()
        HermesSubagentExecutor._fsync_directory(path.parent)

    @classmethod
    def _release_recovery_lease(
        cls,
        path: Path,
        token: str,
        lock_stream: Any,
        request_digest: str,
    ) -> None:
        try:
            try:
                current = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                current = None
            if (
                isinstance(current, Mapping)
                and current.get("owner_token") == token
                and current.get("request_digest") == request_digest
            ):
                path.unlink(missing_ok=True)
                cls._fsync_directory(path.parent)
        finally:
            cls._unlock_recovery_stream(lock_stream)
            lock_stream.close()

    def _launch_with_schema_compatibility(self, request: dict[str, Any]) -> Any:
        try:
            return self._lifecycle.launch(request)
        except Exception as exc:
            if not self._native_schema_unsupported(exc):
                raise
        fallback = dict(request)
        fallback.pop("output_schema", None)
        return self._lifecycle.launch(fallback)

    @staticmethod
    def _native_schema_unsupported(exc: Exception) -> bool:
        text = str(exc).lower()
        return "output_schema" in text and (
            "unknown request field" in text
            or "unknown request fields" in text
            or "unexpected keyword" in text
            or "not supported" in text
        )

    @staticmethod
    def _context(invocation: StepInvocation) -> str:
        envelope = {
            "protocol": "hermes-langchain/controller-step-v1",
            "run_id": invocation.run_id,
            "invocation_id": invocation.invocation_id,
            "step_id": invocation.step_id,
            "input": invocation.input,
            "acceptance_criteria": list(invocation.acceptance_criteria),
            "output_schema": invocation.output_schema,
            "workspace_path": invocation.working_directory,
            "skills": list(invocation.skills),
            "final_response_contract": "Return only one JSON object matching output_schema.",
        }
        rendered = json.dumps(
            envelope,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        if len(rendered) > _MAX_CONTEXT_CHARS:
            raise ValueError("Hermes projected context exceeds 32000 characters")
        return rendered

    def _require_terminal_success(self, terminal: Any) -> None:
        state = self._state(self._field(terminal, "state"))
        completed = bool(self._field(terminal, "completed", False))
        timed_out = bool(self._field(terminal, "timed_out", False))
        if not completed or timed_out or state != _SUCCEEDED:
            diagnostic = self._field(terminal, "diagnostic")
            raise RuntimeError(
                f"Hermes subagent failed: state={state or 'UNKNOWN'}"
                + (f" diagnostic={diagnostic}" if diagnostic else "")
            )

    def _output(self, result: Any) -> dict[str, Any]:
        state = self._state(self._field(result, "terminal_state"))
        ready = bool(self._field(result, "ready", False))
        if not ready or state != _SUCCEEDED:
            classification = self._field(result, "error_classification")
            message = self._field(result, "error_message")
            detail = classification or message or state or "UNKNOWN"
            raise RuntimeError(f"Hermes subagent failed: {detail}")

        structured = self._field(result, "structured_payload")
        if isinstance(structured, Mapping):
            if "schema_valid" in structured:
                if structured.get("schema_valid") is not True:
                    errors = structured.get("schema_errors")
                    note = structured.get("schema_note")
                    detail = errors or note or "unvalidated output"
                    raise ValueError(
                        f"Hermes native output schema validation failed: {detail}"
                    )
                return self._validated_summary_object(self._field(result, "summary"))
            return dict(structured)

        summary = self._field(result, "summary")
        return self._exact_json_object(summary)

    @staticmethod
    def _exact_json_object(summary: Any) -> dict[str, Any]:
        try:
            parsed = json.loads(summary) if isinstance(summary, str) else None
        except json.JSONDecodeError as exc:
            raise ValueError("Hermes subagent summary must be a valid JSON object") from exc
        if not isinstance(parsed, dict):
            raise ValueError("Hermes subagent summary must be a valid JSON object")
        return parsed

    @staticmethod
    def _validated_summary_object(summary: Any) -> dict[str, Any]:
        if not isinstance(summary, str):
            raise ValueError("Hermes validated subagent result has no JSON summary")
        raw = summary.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1]
            if raw.rstrip().endswith("```"):
                raw = raw.rstrip()[:-3]
            raw = raw.strip()
            if raw.lower().startswith("json\n"):
                raw = raw.split("\n", 1)[1]
        start, end = raw.find("{"), raw.rfind("}")
        candidate = raw[start : end + 1] if start >= 0 and end > start else raw
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError as exc:
            raise ValueError("Hermes validated subagent summary contains no JSON object") from exc
        if not isinstance(parsed, dict):
            raise ValueError("Hermes validated subagent summary contains no JSON object")
        return parsed

    @staticmethod
    def _field(value: Any, name: str, default: Any = None) -> Any:
        if isinstance(value, Mapping):
            return value.get(name, default)
        return getattr(value, name, default)

    @staticmethod
    def _state(value: Any) -> str:
        raw = getattr(value, "value", value)
        return str(raw or "").upper()
