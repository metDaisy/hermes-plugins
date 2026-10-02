"""Launch-to-healthy orchestration for a managed llama-server process."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

import os
import re


def _quant_base(stem: str) -> str:
    """Return the language-model base name, with a trailing -<quant> suffix stripped."""
    match = re.search(r"-[A-Za-z][A-Za-z0-9_]*$", stem)
    return stem[: match.start()] if match else stem


def _find_mmproj_near(model_path: Path | None) -> Path | None:
    """Find a `.gguf` mmproj file in the same directory as ``model_path``.

    The stock ``--mmproj-auto`` flag only auto-discovers the projector when
    ``--hf-repo`` is used; for a local ``--model`` file it is a no-op. So we
    locate the projector ourselves and emit an explicit ``--mmproj`` flag.
    """
    if model_path is None:
        return None
    try:
        if not model_path.is_file():
            return None
        parent = model_path.parent
        stem = model_path.stem
        # Prefer <base>-mmproj-<quant>.gguf (e.g. Foo-mmproj-Q8_0.gguf).
        for candidate in sorted(parent.glob(f"{_quant_base(stem)}-mmproj*.gguf")):
            if "mmproj" in candidate.name.lower():
                return candidate
        # Fall back to any mmproj gguf in the directory.
        candidates = [item for item in sorted(parent.glob("*.gguf"))
                      if "mmproj" in item.name.lower()]
        return candidates[0] if candidates else None
    except OSError:
        return None


def _terminate_owned_process(process: Any) -> bool:
    try:
        process.terminate()
        process.wait(timeout=10)
        return True
    except Exception:  # noqa: BLE001
        try:
            process.kill()
            process.wait(timeout=5)
            return True
        except Exception:  # noqa: BLE001
            try:
                return process.poll() is not None
            except Exception:  # noqa: BLE001
                return False


class ServerStartupService:
    """Own command assembly, startup persistence, health polling, and cleanup."""

    def __init__(self, load_state: Callable[[], dict[str, Any]], save_state: Callable[[dict[str, Any]], None], executable: Callable[[], Path | None], stop: Callable[..., None], active_path: Callable[[str], Path], load_options: Callable[[], dict[str, dict[str, str]]], runtime_kind: Callable[[dict[str, Any]], str], backend: Callable[[str], Any], serving_model_name: Callable[[str], str], option_args: Callable[[dict[str, str]], list[str]], watch: Callable[[Any], None], health: Callable[[int], bool], register_endpoint: Callable[[int, str], dict[str, Any]], log_tail: Callable[[int], dict[str, Any]], log_path: Path | None, spawn: Callable[..., Any], now: Callable[[], float], sleep: Callable[[float], None], mutate_state: Callable[[Callable[[dict[str, Any]], None]], dict[str, Any]] | None = None, router_plan: Callable[[dict[str, Any], Path], Any | None] | None = None, router_preset_path: Path | None = None, router_ready: Callable[[int, str], bool] | None = None) -> None:
        self._load_state, self._save_state, self._executable, self._stop = load_state, save_state, executable, stop
        self._active_path, self._load_options, self._runtime_kind, self._backend = active_path, load_options, runtime_kind, backend
        self._serving_model_name = serving_model_name
        self._option_args, self._watch, self._health, self._register_endpoint = option_args, watch, health, register_endpoint
        self._log_tail, self._log_path, self._spawn, self._now, self._sleep = log_tail, log_path, spawn, now, sleep
        self._mutate_state = mutate_state
        self._router_plan = router_plan
        self._router_preset_path = router_preset_path
        self._router_ready = router_ready

    def start(self) -> None:
        state = self._load_state()
        model_id = str(state.get("active_model_id") or "")
        if not model_id:
            raise RuntimeError("select a model before starting llama-server")
        executable = self._executable()
        if executable is None:
            raise RuntimeError("llama-server runtime is not installed")
        self._stop(preserve_log=True, keep_endpoint=True)
        options = self._load_options().get(model_id, {})
        native_router = self._router_plan(state, executable) if self._router_plan is not None else None
        if native_router is not None:
            if self._router_preset_path is None:
                raise RuntimeError("native router preset path is required")
            self._router_preset_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self._router_preset_path.with_suffix(self._router_preset_path.suffix + ".tmp")
            temporary.write_text(native_router.preset_text, encoding="utf-8", newline="\n")
            os.replace(temporary, self._router_preset_path)
            port = int(native_router.port)
            serving_model = str(native_router.default_model)
            command = list(native_router.command)
        else:
            entry = state.get("models", {}).get(model_id, {}) if isinstance(state.get("models"), dict) else {}
            try:
                port = int(options.get("port", state.get("port") or 18434))
            except (TypeError, ValueError) as exc:
                raise RuntimeError(f"invalid server port: {options.get('port')}") from exc
            model_path = self._active_path(model_id)
            serving_model = self._serving_model_name(model_id)
            command = self._backend(self._runtime_kind(state)).build_command(executable, port, model_id, entry if isinstance(entry, dict) else {}, options, model_path=model_path)
            if "mmproj-auto" in options and "no-mmproj" not in options and self._log_path is not None:
                mmproj_path = _find_mmproj_near(model_path)
                if mmproj_path is not None:
                    command.extend(["--mmproj", str(mmproj_path)])
                else:
                    print("mmproj-auto enabled but no projector gguf found next to the model; starting without image support.", flush=True)
            command.extend(["--alias", serving_model])
            command.extend(self._option_args({key: value for key, value in options.items() if key not in {"port", "alias", "mmproj-auto"}}))
        if self._log_path is None:
            raise RuntimeError("server log path is required")
        self._log_path.parent.mkdir(parents=True, exist_ok=True)
        log = self._log_path.open("ab", buffering=0)
        role = "router" if native_router is not None else str(state.get("active_role") or "server")
        log.write(f"\n[{role}] --- llama-server start: model={model_id}, port={port} ---\n".encode())
        try:
            process = self._spawn(command, log, executable)
        except OSError as exc:
            log.write(f"llama-server spawn failed: {exc}\n".encode(errors="replace")); log.close(); raise
        log.close()
        identity: dict[str, Any] = {"pid": process.pid, "executable": str(executable.resolve())}
        try:
            import psutil
            worker = psutil.Process(process.pid)
            identity["create_time"] = worker.create_time()
            identity["executable"] = str(Path(worker.exe()).resolve())
        except Exception as exc:  # noqa: BLE001 - the owned Popen handle is safe to terminate directly
            if not _terminate_owned_process(process):
                raise RuntimeError(
                    "could not verify llama-server process identity or terminate the owned process"
                ) from exc
            raise RuntimeError("could not verify llama-server process identity") from exc

        def record_worker(current: dict[str, Any]) -> None:
            current["pid"], current["port"] = process.pid, port
            current["worker_identity"] = identity
            current["execution_mode"] = "native_router" if native_router is not None else "exclusive_swap"

        def terminate_and_clear_worker() -> None:
            if not _terminate_owned_process(process):
                raise RuntimeError(
                    "could not terminate the owned llama-server; retaining worker ownership state"
                )

            def clear_worker(current: dict[str, Any]) -> None:
                if current.get("pid") == process.pid:
                    current["pid"] = None
                    current["worker_identity"] = None

            try:
                if self._mutate_state is not None:
                    self._mutate_state(clear_worker)
                else:
                    clear_worker(state)
                    self._save_state(state)
            except Exception:  # noqa: BLE001 - preserve the startup failure after process cleanup
                pass

        try:
            if self._mutate_state is not None:
                self._mutate_state(record_worker)
            else:
                record_worker(state)
                self._save_state(state)
            self._watch(process)
        except BaseException:
            terminate_and_clear_worker()
            raise
        deadline = self._now() + 60
        while self._now() < deadline:
            if process.poll() is not None:
                self._stop(preserve_log=True, keep_endpoint=True)
                detail = "\n".join(self._log_tail(40).get("lines", [])[-40:])
                raise RuntimeError("llama-server exited during startup" + (f"\n{detail}" if detail else ""))
            if self._health(port):
                if native_router is not None and self._router_ready is not None and not self._router_ready(port, serving_model):
                    self._sleep(0.5)
                    continue
                try:
                    endpoint = self._register_endpoint(port, serving_model)
                except Exception as exc:
                    try:
                        self._stop(preserve_log=True, keep_endpoint=True)
                    finally:
                        terminate_and_clear_worker()
                    raise RuntimeError(f"custom endpoint registration failed: {exc}") from exc
                def record_endpoint(current: dict[str, Any]) -> None:
                    if current.get("pid") == process.pid:
                        current["custom_endpoint"] = endpoint

                try:
                    if self._mutate_state is not None:
                        self._mutate_state(record_endpoint)
                    else:
                        state = self._load_state()
                        record_endpoint(state)
                        self._save_state(state)
                except BaseException:
                    try:
                        self._stop(preserve_log=True)
                    finally:
                        terminate_and_clear_worker()
                    raise
                return
            self._sleep(0.5)
        self._stop(preserve_log=True, keep_endpoint=True)
        raise RuntimeError("llama-server did not become healthy within 60 seconds")
