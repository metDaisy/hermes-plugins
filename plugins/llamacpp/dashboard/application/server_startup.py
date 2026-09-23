"""Launch-to-healthy orchestration for a managed llama-server process."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable


class ServerStartupService:
    """Own command assembly, startup persistence, health polling, and cleanup."""

    def __init__(self, load_state: Callable[[], dict[str, Any]], save_state: Callable[[dict[str, Any]], None], executable: Callable[[], Path | None], stop: Callable[..., None], active_path: Callable[[str], Path], load_options: Callable[[], dict[str, dict[str, str]]], runtime_kind: Callable[[dict[str, Any]], str], backend: Callable[[str], Any], option_args: Callable[[dict[str, str]], list[str]], watch: Callable[[Any], None], health: Callable[[int], bool], register_endpoint: Callable[[int, str], dict[str, Any]], log_tail: Callable[[int], dict[str, Any]], log_path: Path | None, spawn: Callable[..., Any], now: Callable[[], float], sleep: Callable[[float], None]) -> None:
        self._load_state, self._save_state, self._executable, self._stop = load_state, save_state, executable, stop
        self._active_path, self._load_options, self._runtime_kind, self._backend = active_path, load_options, runtime_kind, backend
        self._option_args, self._watch, self._health, self._register_endpoint = option_args, watch, health, register_endpoint
        self._log_tail, self._log_path, self._spawn, self._now, self._sleep = log_tail, log_path, spawn, now, sleep

    def start(self) -> None:
        state = self._load_state()
        model_id = str(state.get("active_model_id") or "")
        if not model_id:
            raise RuntimeError("select a model before starting llama-server")
        executable = self._executable()
        if executable is None:
            raise RuntimeError("llama-server runtime is not installed")
        self._stop()
        entry = state.get("models", {}).get(model_id, {}) if isinstance(state.get("models"), dict) else {}
        options = self._load_options().get(model_id, {})
        try:
            port = int(options.get("port", state.get("port") or 18434))
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"invalid server port: {options.get('port')}") from exc
        model_path = None if isinstance(entry, dict) and entry.get("hf_repo") else self._active_path(model_id)
        command = self._backend(self._runtime_kind(state)).build_command(executable, port, model_id, entry if isinstance(entry, dict) else {}, options, model_path=model_path)
        command.extend(self._option_args({key: value for key, value in options.items() if key != "port"}))
        if self._log_path is None:
            raise RuntimeError("server log path is required")
        self._log_path.parent.mkdir(parents=True, exist_ok=True)
        log = self._log_path.open("wb")
        log.write(f"\n--- llama-server start: model={model_id}, port={port} ---\n".encode())
        try:
            process = self._spawn(command, log, executable)
        except OSError as exc:
            log.write(f"llama-server spawn failed: {exc}\n".encode(errors="replace")); log.close(); raise
        log.close()
        state["pid"], state["port"] = process.pid, port
        self._save_state(state); self._watch(process)
        deadline = self._now() + 60
        while self._now() < deadline:
            if process.poll() is not None:
                self._stop(preserve_log=True)
                detail = "\n".join(self._log_tail(40).get("lines", [])[-40:])
                raise RuntimeError("llama-server exited during startup" + (f"\n{detail}" if detail else ""))
            if self._health(port):
                try: endpoint = self._register_endpoint(port, model_id)
                except Exception as exc:
                    self._stop(preserve_log=True); raise RuntimeError(f"custom endpoint registration failed: {exc}") from exc
                state = self._load_state()
                if state.get("pid") == process.pid:
                    state["custom_endpoint"] = endpoint; self._save_state(state)
                return
            self._sleep(0.5)
        self._stop(preserve_log=True)
        raise RuntimeError("llama-server did not become healthy within 60 seconds")
