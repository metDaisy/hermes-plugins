"""Standalone llama.cpp manager backend.

This plugin deliberately does not import Hermes core. It owns its API, state,
HF CLI downloads, runtime discovery, model registration, and llama-server
lifecycle while keeping machine-scoped assets in the conventional Hermes paths.
"""
from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from urllib.error import HTTPError, URLError
import uuid

from pathlib import Path
from typing import Any, Callable

from fastapi import APIRouter, HTTPException

try:
    from .backends import backend_view, get_backend
    from .application.device_discovery import DeviceDiscoveryService
    from .application.execution_profiles import ExecutionProfileService
    from .application.download_progress import DownloadProgressTracker
    from .application.huggingface_cache import HuggingFaceCacheService
    from .application.huggingface_download import HuggingFaceDownloadService
    from .application.huggingface_model_workflow import HuggingFaceModelWorkflow
    from .application.job_manager import JobManager
    from .application.json_http import JsonHttpClient
    from .application.model_lifecycle import ModelLifecycleService
    from .application.model_registry import RegisteredModelService
    from .application.model_policy import ModelPolicyService
    from .application.native_router import NativeRouterPlan, build_native_router_plan
    from .application.option_catalog import ServerOptionCatalog
    from .application.official_runtime import OfficialRuntimeService
    from .application.parameter_settings import ParameterSettingsService
    from .application.preset_store import PresetStore
    from .application.activity_log_projection import contextual_activity_tail, project_activity_lines
    from .application.profile_model_routing import sync_profile_models
    from .application.profile_endpoint import ManagedEndpointConfig, write_text_atomically
    from .application.prism_runtime import PrismRuntimeInstaller
    from .application.runtime_inspector import RuntimeInspector
    from .application.runtime_management import RuntimeManagementWorkflow
    from .application.server_lifecycle import ServerLifecycleService
    from .application.server_startup import ServerStartupService
    from .application.state_store import StateStore
    from .child_lifecycle import bind_child_to_owner_lifetime
    from .routes.model_routes import ModelRouteContext, create_router as create_model_router
    from .routes.execution_profile_routes import ExecutionProfileRouteContext, create_router as create_execution_profile_router
    from .routes.parameter_routes import (
        ParameterRouteContext, create_router as create_parameter_router,
        model_settings as _transport_model_settings,
        save_model_settings as _transport_save_model_settings,
        presets as _transport_presets,
        create_preset as _transport_create_preset,
        apply_preset as _transport_apply_preset,
        rename_preset as _transport_rename_preset,
        delete_preset as _transport_delete_preset,
    )
    from .routes.runtime_routes import RuntimeRouteContext, create_router as create_runtime_router
    from .routes.server_routes import ServerRouteContext, create_router as create_server_router
except ImportError:
    # Direct module loading (used by focused plugin tests and the coordinator)
    # has no package parent; expose this dashboard directory for local imports.
    _DASHBOARD_DIR = str(Path(__file__).resolve().parent)
    if _DASHBOARD_DIR not in sys.path:
        sys.path.insert(0, _DASHBOARD_DIR)
    from backends import backend_view, get_backend
    from application.device_discovery import DeviceDiscoveryService
    from application.execution_profiles import ExecutionProfileService
    from application.download_progress import DownloadProgressTracker
    from application.huggingface_cache import HuggingFaceCacheService
    from application.huggingface_download import HuggingFaceDownloadService
    from application.huggingface_model_workflow import HuggingFaceModelWorkflow
    from application.job_manager import JobManager
    from application.json_http import JsonHttpClient
    from application.model_lifecycle import ModelLifecycleService
    from application.model_registry import RegisteredModelService
    from application.model_policy import ModelPolicyService
    from application.native_router import NativeRouterPlan, build_native_router_plan
    from application.option_catalog import ServerOptionCatalog
    from application.official_runtime import OfficialRuntimeService
    from application.parameter_settings import ParameterSettingsService
    from application.preset_store import PresetStore
    from application.activity_log_projection import contextual_activity_tail, project_activity_lines
    from application.profile_model_routing import sync_profile_models
    from application.profile_endpoint import ManagedEndpointConfig, write_text_atomically
    from application.prism_runtime import PrismRuntimeInstaller
    from application.runtime_inspector import RuntimeInspector
    from application.runtime_management import RuntimeManagementWorkflow
    from application.server_lifecycle import ServerLifecycleService
    from application.server_startup import ServerStartupService
    from application.state_store import StateStore
    from child_lifecycle import bind_child_to_owner_lifetime
    from routes.model_routes import ModelRouteContext, create_router as create_model_router
    from routes.execution_profile_routes import ExecutionProfileRouteContext, create_router as create_execution_profile_router
    from routes.parameter_routes import (
        ParameterRouteContext, create_router as create_parameter_router,
        model_settings as _transport_model_settings,
        save_model_settings as _transport_save_model_settings,
        presets as _transport_presets,
        create_preset as _transport_create_preset,
        apply_preset as _transport_apply_preset,
        rename_preset as _transport_rename_preset,
        delete_preset as _transport_delete_preset,
    )
    from routes.runtime_routes import RuntimeRouteContext, create_router as create_runtime_router
    from routes.server_routes import ServerRouteContext, create_router as create_server_router

router = APIRouter()

PLUGIN_DIR = Path(__file__).resolve().parents[1]
PROFILE_ROOT = PLUGIN_DIR.parent.parent
MACHINE_ROOT = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")) / "hermes"
RUNTIME_ROOT = MACHINE_ROOT / "runtimes" / "llamacpp"
PRISM_RUNTIME_ROOT = MACHINE_ROOT / "runtimes" / "prism-ml"
HF_HOME = Path(os.environ.get("HF_HOME") or (Path.home() / ".cache" / "huggingface"))
MODELS_ROOT = HF_HOME
ASSETS_ROOT = HF_HOME / "assets"
STATE_PATH = RUNTIME_ROOT / "state.json"
OPTIONS_PATH = RUNTIME_ROOT / "model-options.json"
PRESET_DB_PATH = RUNTIME_ROOT / "presets.db"
OPTION_METADATA_CACHE_PATH = RUNTIME_ROOT / "parameter-metadata-cache.json"
ROUTER_PRESET_PATH = RUNTIME_ROOT / "router-models.ini"
ACTIVITY_LOG_PATH = RUNTIME_ROOT / "logs" / "activity.log"
REQUEST_CONTEXT_LOG_PATH = RUNTIME_ROOT / "logs" / "request-context.jsonl"
# Compatibility aliases for older callers. All runtime events now share one ordered log.
SERVER_LOG_PATH = ACTIVITY_LOG_PATH
MAIN_LOG_PATH = ACTIVITY_LOG_PATH
COMPRESSION_LOG_PATH = ACTIVITY_LOG_PATH
COORDINATOR_LOG_PATH = ACTIVITY_LOG_PATH
TRANSITION_LOG_PATH = ACTIVITY_LOG_PATH
CUSTOM_ENDPOINT_KEY = "llamacpp-local"
COORDINATOR_PORT = 18380
CUSTOM_ENDPOINT_BEGIN = "# BEGIN llamacpp endpoint (managed)"
CUSTOM_ENDPOINT_END = "# END llamacpp endpoint (managed)"
_custom_endpoint_lock = threading.RLock()
_SPLIT_RE = re.compile(r"-\d{5}-of-\d{5}$", re.IGNORECASE)
_QUANT_SUFFIX_RE = re.compile(r"-(?:P?Q\d+(?:_[A-Za-z0-9]+)*|IQ\d+(?:_[A-Za-z0-9]+)*|F(?:16|32)|BF16)$", re.IGNORECASE)
_option_catalog_cache: tuple[tuple[str, str, str, str, int, int], list[dict[str, Any]]] | None = None
_job_store: dict[str, dict[str, Any]] = {}
_jobs_lock = threading.RLock()
_state_lock = threading.RLock()
_option_cache_lock = threading.RLock()
_model_policy = ModelPolicyService()
_download_progress = DownloadProgressTracker()
_state_store: StateStore | None = None
_state_store_path: Path | None = None
_preset_store: PresetStore | None = None
_preset_store_path: Path | None = None
_official_runtime_service: OfficialRuntimeService | None = None
_official_runtime_root: Path | None = None
_prism_runtime_service: PrismRuntimeInstaller | None = None
_prism_runtime_root: Path | None = None
_endpoint_config = ManagedEndpointConfig(CUSTOM_ENDPOINT_KEY, CUSTOM_ENDPOINT_BEGIN, CUSTOM_ENDPOINT_END)


def _default_state() -> dict[str, Any]:
    return {"active_model_id": None, "tag": "latest", "backend": "auto", "port": 18434,
            "pid": None, "custom_endpoint": None, "models": {}, "model_settings": {},
            "runtime_kind": "official", "runtime_path": None,
            "runtime_mode": "official", "custom_runtime_path": None,
            "execution_mode": "exclusive_swap"}


def _store() -> StateStore:
    """Return a path-aware store so tests may replace ``STATE_PATH`` safely."""
    global _state_store, _state_store_path
    if _state_store is None or _state_store_path != STATE_PATH:
        _state_store = StateStore(STATE_PATH, _default_state)
        _state_store_path = STATE_PATH
    return _state_store


def _read_json(path: Path, fallback: Any) -> Any:
    return _store().read(fallback, path)


def _write_json(path: Path, value: Any) -> None:
    _store().write(path, value)


def _state() -> dict[str, Any]:
    with _state_lock:
        return _store().load()


def _save_state(state: dict[str, Any]) -> None:
    with _state_lock:
        _store().save(state)


def _mutate_state(update: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
    with _state_lock:
        return _store().mutate(update)


def _presets_store() -> PresetStore:
    """Return SQLite-backed presets after one-time legacy JSON migration."""
    global _preset_store, _preset_store_path
    if _preset_store is None or _preset_store_path != PRESET_DB_PATH:
        _preset_store = PresetStore(PRESET_DB_PATH)
        _preset_store_path = PRESET_DB_PATH
    state = _state()
    if _preset_store.migrate_legacy_state(state):
        state.pop("parameter_presets", None)
        state.pop("model_presets", None)
        _save_state(state)
    return _preset_store


def _hf_env() -> dict[str, str]:
    env = os.environ.copy()
    # Windows environment names are case-insensitive; this also supports a
    # lowercase variable when the plugin is run from a POSIX shell.
    if not env.get("HF_TOKEN") and env.get("hf_token"):
        env["HF_TOKEN"] = env["hf_token"]
    return env


def _hf_storage() -> tuple[Path, Path]:
    home = HF_HOME
    cache = Path(os.environ.get("HF_HUB_CACHE") or (home / "hub"))
    return home, cache


def _http_json(url: str) -> Any:
    return JsonHttpClient(
        lambda: os.environ.get("HF_TOKEN") or os.environ.get("hf_token"),
        lambda request: urllib.request.urlopen(request, timeout=60),
        time.sleep,
    ).get(url)


def _http_text(url: str) -> str:
    request = urllib.request.Request(url, headers={"Accept": "text/plain", "User-Agent": "llamacpp"})
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read().decode("utf-8", errors="replace")


def _hf_cache() -> HuggingFaceCacheService:
    def run(argv: list[str]) -> tuple[int, str]:
        result = subprocess.run(
            argv, capture_output=True, check=False, text=True, encoding="utf-8",
            errors="replace", env=_hf_env(), timeout=30,
        )
        return result.returncode, result.stdout or ""

    return HuggingFaceCacheService(
        lambda: shutil.which("hf"), run, cache_root=lambda: _hf_storage()[1],
    )


def _hf_downloaded_models() -> tuple[list[dict[str, str]], str | None, str | None]:
    return _hf_cache().downloaded_models()


def _hf_cached_files(repo_id: str) -> tuple[list[dict[str, Any]], str | None]:
    return _hf_cache().cached_files(repo_id)


def _hf_cached_paths(repo_id: str, paths: list[str]) -> tuple[list[Path], str | None]:
    resolved, warning = _hf_cache().cached_paths(repo_id, paths)
    if resolved:
        return resolved, None
    repo_directory = "models--" + repo_id.replace("/", "--").casefold()
    fallback: list[Path] = []
    for raw_path in paths:
        name = Path(raw_path).name
        candidates = [
            candidate.resolve() for candidate in HF_HOME.rglob(name)
            if candidate.is_file() and candidate.suffix.casefold() == ".gguf"
            and any(parent.name.casefold() == repo_directory for parent in candidate.parents)
        ]
        if not candidates:
            return [], warning or "선택한 GGUF가 로컬 HF 저장소에 없습니다."
        fallback.append(max(candidates, key=lambda candidate: candidate.stat().st_mtime_ns))
    return fallback, None


def _model_id(path: Path) -> str:
    return _SPLIT_RE.sub("", path.stem)


def _serving_model_name(model_id: str) -> str:
    """Expose a stable API alias without changing the registered quant identity."""
    return _QUANT_SUFFIX_RE.sub("", model_id) or model_id


def _models() -> RegisteredModelService:
    return RegisteredModelService(_state, _save_state, _hf_cached_files, _hf_cached_paths)


def _model_rows() -> list[dict[str, Any]]:
    """Return only models explicitly registered in plugin state."""
    return _models().rows()


def _register_model(model_id: str, paths: list[Path], owned: bool,
                    hf_repo: str | None = None, hf_file: str | None = None,
                    size_bytes: int = 0, runtime_kind: str | None = None) -> None:
    _models().register(
        model_id, paths, owned, hf_repo, hf_file, size_bytes,
        runtime_kind=runtime_kind,
    )


def _job_manager() -> JobManager:
    return JobManager(_job_store, _jobs_lock, lambda: uuid.uuid4().hex[:12], time.time, threading.Thread)


def _job(kind: str, detail: str) -> dict[str, Any]:
    return _job_manager().create(kind, detail)


def _finish(job: dict[str, Any], detail: str) -> None:
    _job_manager().finish(job, detail)


def _fail(job: dict[str, Any], exc: Exception) -> None:
    _job_manager().fail(job, exc)


def _spawn(job: dict[str, Any], target: Callable[[], None], name: str) -> None:
    _job_manager().launch(job, name, target)


def _jobs() -> list[dict[str, Any]]:
    return _job_manager().recent()


def _backend() -> str:
    configured = str(_state().get("backend") or "auto").lower()
    if configured != "auto":
        return configured
    try:
        result = subprocess.run(["nvidia-smi", "-L"], capture_output=True, check=False,
                                text=True, timeout=5)
        return "cuda" if result.returncode == 0 and result.stdout.strip() else "cpu"
    except OSError:
        return "cpu"


def _official_runtime() -> OfficialRuntimeService:
    """Compose the official-runtime module with this facade's I/O adapters."""
    global _official_runtime_service, _official_runtime_root
    if _official_runtime_service is None or _official_runtime_root != RUNTIME_ROOT:
        _official_runtime_service = OfficialRuntimeService(
            root=RUNTIME_ROOT,
            load_state=_state,
            save_state=_save_state,
            release_loader=lambda: _http_json(
                "https://api.github.com/repos/ggml-org/llama.cpp/releases?per_page=100"
            ),
            backend_detector=_backend,
            platform_name=platform.system,
            architecture=platform.machine,
        )
        _official_runtime_root = RUNTIME_ROOT
    return _official_runtime_service


def _detected_devices() -> list[dict[str, Any]]:
    def run(argv: list[str]) -> tuple[int, str]:
        result = subprocess.run(argv, capture_output=True, check=False, text=True,
                                encoding="utf-8", errors="replace", timeout=10)
        return result.returncode, result.stdout or ""
    return DeviceDiscoveryService(_server_executable, run).devices()


def _runtime_target(force_latest: bool = False, requested_backend: Any = None) -> tuple[str, str]:
    return _official_runtime().resolve_target(force_latest, requested_backend)


def _latest_official_release() -> dict[str, Any]:
    try:
        tag, backend = _official_runtime().latest_target(_backend())
        return {"tag": tag, "backend": backend}
    except Exception as exc:  # noqa: BLE001
        return {"tag": None, "error": str(exc)}


def _latest_prism_release() -> dict[str, Any]:
    try:
        tag, backend = _prism_installer().latest_target(_backend())
        return {"tag": tag, "backend": backend}
    except Exception as exc:  # noqa: BLE001
        return {"tag": None, "error": str(exc)}


def _installed_target() -> tuple[str, str] | None:
    return _official_runtime().installed_target()


def _server_executable_in(root: Path) -> Path | None:
    return _official_runtime().executable_in(root)


def _runtime_kind(state: dict[str, Any] | None = None) -> str:
    current = state or _state()
    raw = current.get("runtime_kind") or current.get("runtime_mode") or "official"
    return get_backend(raw).key


def _resolve_server_executable_from_path(raw_path: Path | str) -> Path:
    """Resolve a user-selected llama-server path through the active adapter."""
    return get_backend(_runtime_kind()).resolve_executable(raw_path)


def _server_executable(tag: str | None = None, backend: str | None = None) -> Path | None:
    if tag and backend:
        target = (tag, backend)
    else:
        state = _state()
        runtime_kind = _runtime_kind(state)
        if runtime_kind != "official":
            try:
                adapter = get_backend(runtime_kind)
                raw_path = state.get("runtime_path") or state.get("custom_runtime_path")
                return adapter.resolve_executable(raw_path or adapter.managed_root(MACHINE_ROOT))
            except RuntimeError:
                return None
        target = _installed_target()
    if target is None:
        return None
    return _server_executable_in(RUNTIME_ROOT / target[0] / target[1])


def _pid_alive(pid: Any) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        import psutil
        return psutil.pid_exists(pid)
    except ImportError:
        try:
            os.kill(pid, 0)
            return True
        except (OSError, ProcessLookupError):
            return False


def _worker_identity_matches(state: dict[str, Any]) -> bool:
    try:
        import psutil
        pid = int(state.get("pid") or 0)
        port = int(state.get("port") or 0)
        identity = state.get("worker_identity")
        if pid <= 0 or port <= 0 or not isinstance(identity, dict):
            return False
        expected_pid = int(identity.get("pid") or 0)
        expected_time = float(identity.get("create_time"))
        expected_executable = os.path.normcase(os.path.abspath(str(identity.get("executable") or "")))
        process = psutil.Process(pid)
        if (
            expected_pid != pid
            or abs(process.create_time() - expected_time) >= 0.01
            or os.path.normcase(os.path.abspath(process.exe())) != expected_executable
        ):
            return False
        return any(
            connection.status == psutil.CONN_LISTEN
            and connection.laddr
            and int(connection.laddr.port) == port
            for connection in process.net_connections(kind="tcp")
        )
    except Exception:  # noqa: BLE001 - identity uncertainty must fail closed
        return False


def _terminate_server(pid: int) -> bool:
    import psutil

    try:
        process = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return True
    state = _state()
    if not _worker_identity_matches(state):
        return False
    if platform.system().lower() == "windows":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, check=False)
    else:
        process.terminate()
    try:
        process.wait(timeout=10)
    except psutil.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)
    return True


def _server_lifecycle(log_path: Path = SERVER_LOG_PATH) -> ServerLifecycleService:
    return ServerLifecycleService(
        load_state=_state,
        save_state=_save_state,
        pid_alive=_pid_alive,
        terminate=_terminate_server,
        unregister_endpoint=_unregister_custom_endpoint,
        log_path=log_path,
        mutate_state=_mutate_state,
    )


def _health(port: int) -> bool:
    # A single health probe with a generous timeout. The startup polling
    # loop re-invokes this on every iteration, so there is no need for an
    # aggressive 1-second deadline that would false-negative while llama.cpp
    # is busy with inference or loading.
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=3) as response:
            return response.status in (200, 201, 204)
    except Exception:  # noqa: BLE001
        return False


def _router_model_ready(port: int, model_id: str) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/models", timeout=3) as response:
            if response.status != 200:
                return False
            payload = json.loads(response.read().decode("utf-8", errors="replace"))
        rows = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            return False
        return any(
            isinstance(row, dict)
            and str(row.get("id") or "") == model_id
            and isinstance(row.get("status"), dict)
            and str(row["status"].get("value") or "") == "loaded"
            for row in rows
        )
    except Exception:  # noqa: BLE001 - readiness polling is fail-closed
        return False


def _endpoint_config_path() -> Path:
    """Return this profile's config for compatibility with older callers."""
    return PROFILE_ROOT / "config.yaml"


def _profile_config_paths() -> list[Path]:
    """Return every local profile config served by this Hermes home."""
    profiles_root = PROFILE_ROOT.parent
    paths = [path / "config.yaml" for path in profiles_root.iterdir()
             if path.is_dir() and (path / "config.yaml").is_file()]
    current = _endpoint_config_path()
    if current.is_file() and current not in paths:
        paths.append(current)
    return sorted(set(paths), key=lambda path: str(path).lower())


def _config_with_managed_endpoint(text: str, base_url: str, model_id: str, context_length: int) -> str:
    return _endpoint_config.upsert(text, base_url, model_id, context_length)


def _config_with_execution_profiles(text: str, base_url: str, default_model: str,
                                    models: dict[str, int]) -> str:
    return _endpoint_config.upsert_models(text, base_url, default_model, models)


def _remove_managed_endpoint(text: str) -> tuple[str, bool]:
    return _endpoint_config.remove(text)


def _write_profile_config(path: Path, text: str) -> None:
    write_text_atomically(path, text)


def _apply_profile_updates(updates: list[tuple[Path, str, str]]) -> None:
    written: list[tuple[Path, str]] = []
    try:
        for path, original, updated in updates:
            _write_profile_config(path, updated)
            written.append((path, original))
    except Exception as update_error:
        rollback_errors: list[Exception] = []
        for path, original in reversed(written):
            try:
                _write_profile_config(path, original)
            except Exception as rollback_error:  # noqa: BLE001 - report every failed recovery write
                rollback_errors.append(rollback_error)
        if rollback_errors:
            raise ExceptionGroup(
                "profile endpoint update failed and rollback failed",
                [update_error, *rollback_errors],
            )
        raise


def _register_custom_endpoint(port: int, model_id: str) -> dict[str, Any]:
    base_url = f"http://127.0.0.1:{COORDINATOR_PORT}/v1"
    worker_base_url = f"http://127.0.0.1:{port}/v1"
    endpoint = _sync_execution_profile_endpoint(fallback_model=model_id)
    endpoint["worker_base_url"] = worker_base_url
    return endpoint


def _profile_provider_models() -> dict[str, int]:
    snapshot = execution_profiles()
    profile_models: dict[str, int] = {}
    options_by_model = _load_options()
    for profile in snapshot["profiles"].values():
        bound_model = str(profile.get("model_id") or "")
        if not bound_model:
            continue
        try:
            profile_models[str(profile["logical_model"])] = int(
                options_by_model.get(bound_model, {}).get("ctx-size") or 65536
            )
        except (TypeError, ValueError):
            profile_models[str(profile["logical_model"])] = 65536
    return profile_models


def _sync_execution_profile_endpoint(fallback_model: str = "") -> dict[str, Any]:
    base_url = f"http://127.0.0.1:{COORDINATOR_PORT}/v1"
    provider_models = _profile_provider_models()
    if not provider_models and fallback_model:
        stored_options = _load_options().get(fallback_model, {})
        try:
            context_length = int(stored_options.get("ctx-size") or 65536)
        except (TypeError, ValueError):
            context_length = 65536
        provider_models = {fallback_model: context_length}
    if not provider_models:
        raise RuntimeError("configure a Main or Compression execution profile first")
    default_model = "main-local" if "main-local" in provider_models else next(iter(provider_models))
    with _custom_endpoint_lock:
        updates: list[tuple[Path, str, str]] = []
        for path in _profile_config_paths():
            text = path.read_text(encoding="utf-8")
            updated = _config_with_execution_profiles(text, base_url, default_model, provider_models)
            if path.parent.name in {"project-manager", "coder", "reviewer"}:
                updated = sync_profile_models(
                    updated,
                    base_url=base_url,
                    main_served="main-local" in provider_models,
                    compression_served="compression-local" in provider_models,
                )
            if updated != text:
                updates.append((path, text, updated))
        _apply_profile_updates(updates)
    return {"key": CUSTOM_ENDPOINT_KEY, "provider": "custom", "base_url": base_url,
            "model": default_model,
            "models": provider_models, "context_length": provider_models[default_model]}


def _unregister_custom_endpoint() -> None:
    with _custom_endpoint_lock:
        updates: list[tuple[Path, str, str]] = []
        for path in _profile_config_paths():
            text = path.read_text(encoding="utf-8")
            updated, changed = _remove_managed_endpoint(text)
            if path.parent.name in {"project-manager", "coder", "reviewer"}:
                routed = sync_profile_models(
                    updated,
                    base_url=f"http://127.0.0.1:{COORDINATOR_PORT}/v1",
                    main_served=False,
                    compression_served=False,
                )
                changed = changed or routed != updated
                updated = routed
            if changed:
                updates.append((path, text, updated))
        _apply_profile_updates(updates)


def _stop_server(*, preserve_log: bool = False, keep_endpoint: bool = False) -> None:
    # The activity log spans coordinator and many sequential model workers.
    _server_lifecycle().stop(True, keep_endpoint)


def shutdown_machine_runtime() -> None:
    """Stop only verified plugin-owned work before the coordinator exits."""
    _server_lifecycle().stop(preserve_log=True, keep_endpoint=True)
    update_execution_queue(0, 0)


def _watch_server_process(process: subprocess.Popen[Any]) -> None:
    _server_lifecycle().watch(process)


def _shutdown_server_on_backend_exit() -> None:
    # llama-server is machine-scoped and must outlive a profile backend restart.
    return


def _load_options() -> dict[str, dict[str, str]]:
    raw_state = _read_json(STATE_PATH, {})
    state = _state()
    persistent = state.get("model_settings")
    if isinstance(raw_state, dict) and "model_settings" in raw_state and isinstance(persistent, dict):
        return {str(model_id): dict(options) for model_id, options in persistent.items()
                if isinstance(options, dict)}
    legacy = _read_json(OPTIONS_PATH, {})
    migrated = legacy if isinstance(legacy, dict) else {}
    state["model_settings"] = migrated
    _save_state(state)
    return {str(model_id): dict(options) for model_id, options in migrated.items()
            if isinstance(options, dict)}


def _save_options(options: dict[str, dict[str, str]]) -> None:
    state = _state()
    state["model_settings"] = options
    _save_state(state)
    # Keep the old runtime file for compatibility with older plugin versions.
    try:
        _write_json(OPTIONS_PATH, options)
    except OSError:
        pass


def _canonical_option_value(metadata: dict[str, Any] | None, value: Any) -> str:
    return ServerOptionCatalog.canonical(metadata, value)


def _option_cli_args(options: dict[str, str]) -> list[str]:
    catalog = {option["key"]: option for option in _option_list()}
    args: list[str] = []
    for key, value in options.items():
        metadata = catalog.get(key)
        canonical = _canonical_option_value(metadata, value)
        if metadata and metadata.get("requires_value") and not canonical.strip():
            continue
        if metadata and metadata.get("toggle"):
            args.append(f"--{key}" if canonical == "on" else f"--no-{key}")
            continue
        args.append(f"--{key}")
        if canonical.strip():
            args.append(canonical)
    return args


def _active_path(model_id: str) -> Path:
    return _models().active_path(model_id)


def _native_router_plan(state: dict[str, Any], executable: Path) -> NativeRouterPlan | None:
    snapshot = execution_profiles()
    profiles = snapshot.get("profiles") if isinstance(snapshot, dict) else None
    if not isinstance(profiles, dict):
        return None
    required = ("main", "compression")
    if any(not isinstance(profiles.get(role), dict) or not profiles[role].get("configured") for role in required):
        return None
    if any(str(profiles[role].get("runtime_kind") or "") != "official" for role in required):
        return None

    all_options = _load_options()
    model_paths: dict[str, Path] = {}
    role_options: dict[str, dict[str, Any]] = {}
    for role in required:
        profile = profiles[role]
        model_id = str(profile.get("model_id") or "")
        if not model_id:
            return None
        model_paths[role] = _active_path(model_id)
        options = dict(all_options.get(model_id, {}))
        preset_id = str(profile.get("preset_id") or "")
        if preset_id:
            preset = _presets_store().get(preset_id)
            if preset is None:
                raise RuntimeError(f"execution profile preset was not found: {preset_id}")
            options.update(dict(preset.get("options") or {}))
        role_options[role] = options

    try:
        port = int(role_options["main"].get("port", state.get("port") or 18434))
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"invalid server port: {role_options['main'].get('port')}") from exc
    return build_native_router_plan(
        executable=executable,
        port=port,
        preset_path=ROUTER_PRESET_PATH,
        profiles=profiles,
        model_paths=model_paths,
        model_options=role_options,
    )


def _server_startup(log_path: Path | None = None) -> ServerStartupService:
    def spawn(command: list[str], log: Any, executable: Path) -> subprocess.Popen[Any]:
        process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                   cwd=str(executable.parent), creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
        bind_child_to_owner_lifetime(process)
        return process
    return ServerStartupService(_state, _save_state, _server_executable, _stop_server, _active_path, _load_options,
                                _runtime_kind, get_backend, _serving_model_name, _option_cli_args, _watch_server_process, _health,
                                _register_custom_endpoint, _server_log_tail, log_path or SERVER_LOG_PATH, spawn, time.time, time.sleep,
                                _mutate_state, _native_router_plan, ROUTER_PRESET_PATH, _router_model_ready)


def _start_server() -> None:
    _server_startup().start()


def _server_log_tail(limit: int = 250, role: str = "activity") -> dict[str, Any]:
    raw = contextual_activity_tail(ACTIVITY_LOG_PATH, REQUEST_CONTEXT_LOG_PATH, limit)
    raw_lines = list(raw.get("lines") or [])
    projected = project_activity_lines(raw_lines)
    return {
        **raw,
        "role": "activity",
        "raw_lines": raw_lines,
        "lines": projected["lines"],
        "events": projected["events"],
    }


def _server_rows() -> list[dict[str, Any]]:
    return _model_lifecycle().server_rows()


def _is_server_running() -> bool:
    state = _state()
    return bool(
        _pid_alive(state.get("pid"))
        and _worker_identity_matches(state)
        and _health(int(state.get("port") or 18434))
    )


def _model_lifecycle() -> ModelLifecycleService:
    return ModelLifecycleService(
        registry=_models(), load_state=_state, save_state=_save_state, runtime_kind=_runtime_kind,
        accepts=lambda kind, repo, paths: _model_policy.accepts(kind, repo, paths),
        server_running=_is_server_running, stop_server=_stop_server, models_root=MODELS_ROOT,
        model_id=_model_id, model_rows=_model_rows,
    )


def _runtime_inspector() -> RuntimeInspector:
    return RuntimeInspector(
        load_state=_state, save_state=_save_state, runtime_kind=_runtime_kind,
        executable=_server_executable, installed_target=_installed_target,
        executable_in=_server_executable_in, backend_detector=_backend, backend=get_backend,
        backend_view=backend_view, machine_root=MACHINE_ROOT, runtime_root=RUNTIME_ROOT,
        prism_root=PRISM_RUNTIME_ROOT, pid_alive=_pid_alive, health=_health,
        unregister_endpoint=_unregister_custom_endpoint, devices=_detected_devices,
        server_rows=_server_rows, models_root=MODELS_ROOT, model_presets=_presets_store().model_preset_map,
        latest_official=_latest_official_release, latest_prism=_latest_prism_release,
    )


def _runtime_info() -> dict[str, Any]:
    return _runtime_inspector().info()


def _runtime_management() -> RuntimeManagementWorkflow:
    return RuntimeManagementWorkflow(
        _state, _save_state, get_backend, MACHINE_ROOT, _runtime_kind, _runtime_info, _pid_alive,
        lambda path: os.startfile(str(path)), _install_prism_runtime, _runtime_target, _job, _spawn,
        _finish, lambda tag, backend, job, download: _official_runtime().install(tag, backend, job, download),
        _download_archive,
    )


def _execution_profile_accepts(runtime_kind: str, model_id: str) -> bool:
    if runtime_kind == "official":
        return True
    state = _state()
    model = state.get("models", {}).get(model_id, {})
    if not isinstance(model, dict):
        return False
    repo_id = str(model.get("hf_repo") or "")
    paths = [str(path) for path in model.get("paths", []) if path]
    if model.get("hf_file"):
        paths.append(str(model["hf_file"]))
    version = str(state.get("prism_release_tag") or "") or None
    return _model_policy.accepts(runtime_kind, repo_id, paths, version)


def _execution_profiles_service() -> ExecutionProfileService:
    return ExecutionProfileService(_state, _save_state, _execution_profile_accepts, _mutate_state)


def execution_profiles() -> dict[str, Any]:
    return _execution_profiles_service().snapshot()


def save_execution_profile(role: str, body: dict[str, Any]) -> dict[str, Any]:
    previous = _state()
    previous_profiles = previous.get("execution_profiles")
    previous_role = (
        dict(previous_profiles.get(role))
        if isinstance(previous_profiles, dict) and isinstance(previous_profiles.get(role), dict)
        else None
    )
    result = _execution_profiles_service().save(role, body)
    written_profile = result.get("profiles", {}).get(role, {})
    written_fields = {
        key: str(written_profile.get(key) or "")
        for key in ("runtime_kind", "model_id", "preset_id")
    }
    try:
        configured = any(
            isinstance(profile, dict) and profile.get("configured")
            for profile in result.get("profiles", {}).values()
        )
        if configured:
            endpoint = _sync_execution_profile_endpoint()
        else:
            _unregister_custom_endpoint()
            endpoint = None
    except Exception:
        def rollback(current: dict[str, Any]) -> None:
            profiles = current.get("execution_profiles")
            next_profiles = dict(profiles) if isinstance(profiles, dict) else {}
            current_role = next_profiles.get(role)
            current_fields = {
                key: str(current_role.get(key) or "")
                for key in ("runtime_kind", "model_id", "preset_id")
            } if isinstance(current_role, dict) else {}
            if current_fields != written_fields:
                return
            if previous_role is None:
                next_profiles.pop(role, None)
            else:
                next_profiles[role] = previous_role
            current["execution_profiles"] = next_profiles

        _mutate_state(rollback)
        raise

    def record(current: dict[str, Any]) -> None:
        current["custom_endpoint"] = endpoint

    _mutate_state(record)
    return result


def _transition_log(event: str, **details: Any) -> None:
    TRANSITION_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    row = {"timestamp": time.time(), "event": event, **details}
    with TRANSITION_LOG_PATH.open("a", encoding="utf-8") as stream:
        stream.write("[transition] " + json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def update_execution_queue(queued_main: int, active_main: int) -> None:
    def update(current: dict[str, Any]) -> None:
        current["queued_main_requests"] = max(0, int(queued_main))
        current["active_main_requests"] = max(0, int(active_main))

    _mutate_state(update)


def ensure_execution_role(role: str) -> dict[str, Any]:
    role = str(role or "").strip().lower()
    profiles = execution_profiles()["profiles"]
    profile = profiles.get(role)
    if not isinstance(profile, dict) or not profile.get("configured"):
        raise RuntimeError(f"{role} execution profile is not configured")
    model_id = str(profile["model_id"])
    runtime_kind = str(profile["runtime_kind"])
    state = _state()
    if state.get("execution_mode") == "native_router" and _is_server_running():
        def route(current: dict[str, Any]) -> None:
            current["active_model_id"] = model_id
            current["active_role"] = role
            current["transition_phase"] = "ROUTER_READY"
            current["transition_error"] = None

        _mutate_state(route)
        _transition_log("router-role-selected", role=role, model_id=model_id, runtime_kind=runtime_kind)
        return {"role": role, "model_id": model_id, "already_running": True, "router_managed": True}
    if (state.get("active_role") == role and state.get("active_model_id") == model_id
            and _runtime_kind(state) == runtime_kind and _is_server_running()):
        return {"role": role, "model_id": model_id, "already_running": True}

    if role == "main":
        while int(state.get("active_main_requests") or 0) > 0:
            time.sleep(0.02)
            state = _state()

    _transition_log("transition-start", role=role, model_id=model_id, runtime_kind=runtime_kind)

    def select(current: dict[str, Any]) -> None:
        current["active_model_id"] = model_id
        current["runtime_kind"] = runtime_kind
        current["active_role"] = role
        current["transition_phase"] = "LOADING_COMPRESSION" if role == "compression" else "RESTORING_MAIN"
        preset_id = str(profile.get("preset_id") or "")
        if preset_id:
            preset = _presets_store().get(preset_id)
            if preset is None:
                raise RuntimeError(f"execution profile preset was not found: {preset_id}")
            settings = current.get("model_settings")
            options = dict(settings) if isinstance(settings, dict) else {}
            options[model_id] = dict(preset.get("options") or {})
            current["model_settings"] = options

    _mutate_state(select)
    log_path = ACTIVITY_LOG_PATH
    try:
        _server_startup(log_path).start()
    except Exception as exc:
        def fail(current: dict[str, Any]) -> None:
            current["transition_phase"] = "FAILED"
            current["transition_error"] = str(exc)
        _mutate_state(fail)
        _transition_log("transition-failed", role=role, model_id=model_id, error=str(exc))
        raise

    def ready(current: dict[str, Any]) -> None:
        current["active_role"] = role
        current["transition_phase"] = "COMPRESSING" if role == "compression" else "MAIN_READY"
        current["transition_error"] = None

    _mutate_state(ready)
    _transition_log("transition-ready", role=role, model_id=model_id, runtime_kind=runtime_kind)
    return {"role": role, "model_id": model_id, "already_running": False}


def execution_role_configured(role: str) -> bool:
    profile = execution_profiles()["profiles"].get(str(role or "").strip().lower())
    return isinstance(profile, dict) and bool(profile.get("configured"))


def abort_execution_transition(role: str, error: str) -> None:
    """Remove a transitional worker when the required Main restoration failed."""
    _server_lifecycle().stop(preserve_log=True, keep_endpoint=True)

    def fail(current: dict[str, Any]) -> None:
        current["active_role"] = None
        current["transition_phase"] = "FAILED"
        current["transition_error"] = str(error)

    _mutate_state(fail)
    _transition_log("transition-aborted", role=role, error=str(error))


def _status() -> dict[str, Any]:
    status = _runtime_inspector().status()
    state = _state()
    execution = execution_profiles()
    status.update(execution)
    visible_models = _model_rows()
    status["models"] = visible_models
    profile_options = [
        {"id": str(model.get("id")), "label": str(model.get("id"))}
        for model in visible_models
        if isinstance(model, dict) and model.get("id")
    ]
    status["main_model_options"] = list(profile_options)
    status["auxiliary_model_options"] = list(profile_options)
    status["profile_model_options"] = list(profile_options)
    status["coordinator"] = {
        "ok": True,
        "pid": os.getpid(),
        "port": COORDINATOR_PORT,
        "singleton": True,
        "inference_base_url": f"http://127.0.0.1:{COORDINATOR_PORT}/v1",
        "cancellation_propagation": True,
    }
    status["execution"] = {
        "active_role": state.get("active_role"),
        "transition_phase": state.get("transition_phase") or "IDLE",
        "transition_error": state.get("transition_error"),
        "queued_main_requests": int(state.get("queued_main_requests") or 0),
        "active_main_requests": int(state.get("active_main_requests") or 0),
    }
    status["logs"] = {"activity": str(ACTIVITY_LOG_PATH)}
    return status


def _hf_download(repo: str, path: str, job: dict[str, Any] | None = None,
                 part_index: int = 0, part_count: int = 1) -> Path:
    executable = shutil.which("hf")
    if not executable:
        raise RuntimeError("hf CLI was not found on PATH")
    if job is not None:
        _download_progress.begin(job, f"다운로드 중: {Path(path).name}")
    def start(argv: list[str]) -> subprocess.Popen[Any]:
        return subprocess.Popen(
            argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
            env=_hf_env(),
        )

    observe = None if job is None else lambda percent: _download_progress.observe_percent(job, percent, part_index, part_count)
    return HuggingFaceDownloadService(start, Path.cwd).download(executable, repo, path, observe)


def _require_compatible_model(kind: str, repo_id: str, paths: list[str]) -> None:
    version = str(_state().get("prism_release_tag") or "") or None
    if not _model_policy.accepts(kind, repo_id, paths, version):
        raise HTTPException(status_code=422, detail="Prism-ML backend에는 catalog에 등록된 Prism Bonsai/Ternary GGUF quant만 등록할 수 있습니다")


def _remove_hf_cache(repo_id: str) -> bool:
    executable = shutil.which("hf")
    if not executable:
        raise RuntimeError("hf CLI was not found on PATH")
    typed_repo_id = repo_id if repo_id.startswith("model/") else f"model/{repo_id}"
    result = subprocess.run(
        [executable, "cache", "rm", typed_repo_id, "--yes", "--format", "quiet"],
        capture_output=True, check=False, text=True, encoding="utf-8", errors="replace",
        env=_hf_env(), timeout=120,
    )
    return result.returncode == 0


def _hf_workflow() -> HuggingFaceModelWorkflow:
    return HuggingFaceModelWorkflow(
        load_state=_state, runtime_kind=_runtime_kind,
        accepts=lambda kind, repo, paths, version: _model_policy.accepts(kind, repo, paths, version),
        cache_models=_hf_downloaded_models, cached_files=_hf_cached_files, cached_paths=_hf_cached_paths,
        http_json=_http_json,
        download=_hf_download, register=_register_model, remove_cache=_remove_hf_cache,
        model_id=_model_id,
        visible_repositories=lambda kind, repositories: _model_policy.visible_repositories(kind, repositories),
        registration_runtime_kind=lambda model_id: "prism_ml" if model_id.startswith("Ternary-Bonsai") else "official",
    )


def _local_hf_models() -> dict[str, Any]:
    return _hf_workflow().local_models()


def _copy_options(options: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return ServerOptionCatalog.copy(options)


def _option_cache_identity(executable: Path) -> tuple[str, str, str, str, int, int]:
    target = _installed_target()
    tag, backend = target if target else ("", "")
    try:
        stat = executable.stat()
        return ("5", str(executable.resolve()), tag, backend, stat.st_mtime_ns, stat.st_size)
    except OSError:
        return ("5", str(executable), tag, backend, 0, 0)


def _option_list() -> list[dict[str, Any]]:
    global _option_catalog_cache
    executable = _server_executable()
    if executable is None:
        return []
    identity = _option_cache_identity(executable)
    with _option_cache_lock:
        if _option_catalog_cache and _option_catalog_cache[0] == identity:
            return _copy_options(_option_catalog_cache[1])

        persisted = _read_json(OPTION_METADATA_CACHE_PATH, {})
        cached_identity = persisted.get("identity") if isinstance(persisted, dict) else None
        cached_options = persisted.get("options") if isinstance(persisted, dict) else None
        if (isinstance(cached_identity, list) and tuple(cached_identity) == identity
                and isinstance(cached_options, list) and all(isinstance(item, dict) for item in cached_options)):
            _option_catalog_cache = (identity, cached_options)
            return _copy_options(cached_options)

        result = subprocess.run([str(executable), "--help"], capture_output=True, check=False,
                                text=True, encoding="utf-8", errors="replace", timeout=30)
        if result.returncode != 0:
            return []
        catalog = ServerOptionCatalog.parse_help(result.stdout)
        _option_catalog_cache = (identity, catalog)
        try:
            _write_json(OPTION_METADATA_CACHE_PATH, {"identity": list(identity), "options": catalog})
        except OSError:
            pass
        return _copy_options(catalog)


router.include_router(create_model_router(ModelRouteContext(
    lifecycle=_model_lifecycle, workflow=_hf_workflow, create_job=_job,
    launch=_spawn, finish=_finish, begin_download=_download_progress.begin,
)))
router.include_router(create_server_router(ServerRouteContext(
    state=_state, stop=_stop_server, start=_start_server, create_job=_job, launch=_spawn,
    finish=_finish, recent_jobs=_jobs, find_job=lambda job_id: _job_manager().find(job_id),
    logs=_server_log_tail,
)))
router.include_router(create_execution_profile_router(ExecutionProfileRouteContext(
    snapshot=execution_profiles, save=save_execution_profile, start=ensure_execution_role,
    create_job=_job, launch=_spawn, finish=_finish,
)))
def status() -> dict[str, Any]:
    return _status()


def registered_models() -> dict[str, Any]:
    """Return registered models and the current selection for agent tools."""
    state = _state()
    return {
        "models": _server_rows(),
        "active_model_id": state.get("active_model_id"),
        "server_running": _is_server_running(),
    }


def activate_model(model_id: str) -> dict[str, Any]:
    """Select a registered model without starting llama-server."""
    return _model_lifecycle().activate(model_id)


def start_server() -> dict[str, Any]:
    """Launch the selected model asynchronously and return its tracked job."""
    if not _state().get("active_model_id"):
        raise RuntimeError("select a model before starting llama-server")
    if _is_server_running():
        return {"ok": True, "server_running": True, "already_running": True}
    job = _job("server-start", "llama-server 시작 중")

    def run() -> None:
        _start_server()
        _finish(job, "llama-server is healthy")

    _spawn(job, run, "llamacpp-server-start")
    return {"ok": True, "server_running": False, "job_id": job["job_id"]}


def stop_server() -> dict[str, Any]:
    """Stop only the plugin-managed llama-server process and its endpoint."""
    _stop_server()
    return {"ok": True, "server_running": False}


def runtime_info() -> dict[str, Any]:
    return _runtime_info()


# --- Runtime/preset public entrypoints ------------------------------------
#
# The public entrypoint keeps these names so external callers
# (``api.save_runtime(...)``, ``api.open_runtime(...)``, ``api.runtime_install(...)``)
# work unchanged. Each one simply forwards to the framework-independent
# ``RuntimeManagementWorkflow``; the FastAPI HTTP mapping lives in
# ``runtime_routes`` (same as the other route groups), so the facade owns no
# transport contract.


def save_runtime(body: dict[str, Any]) -> dict[str, Any]:
    return _runtime_management().select(body)


def hardware() -> dict[str, Any]:
    return {"gpu_name": None, "gpu_util_percent": None, "vram_total_bytes": None,
            "vram_usable_bytes": None, "models_dir": str(MODELS_ROOT)}


def catalog() -> dict[str, Any]:
    return {"models": []}


def _download_archive(url: str, destination: Path, job: dict[str, Any], floor: int, ceiling: int) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(url, timeout=120) as response, destination.open("wb") as output:
        total, done = int(response.headers.get("Content-Length") or 0), 0
        while chunk := response.read(1 << 20):
            output.write(chunk); done += len(chunk)
            if total: job["percent"] = floor + round(done / total * (ceiling - floor))


def _prism_installer() -> PrismRuntimeInstaller:
    global _prism_runtime_service, _prism_runtime_root
    if _prism_runtime_service is None or _prism_runtime_root != PRISM_RUNTIME_ROOT:
        _prism_runtime_service = PrismRuntimeInstaller(
            root=PRISM_RUNTIME_ROOT,
            load_state=_state,
            save_state=_save_state,
            release_loader=lambda: _http_json(
                "https://api.github.com/repos/PrismML-Eng/llama.cpp/releases?per_page=100"
            ),
            backend_detector=_backend,
            platform_name=platform.system,
            architecture=platform.machine,
        )
        _prism_runtime_root = PRISM_RUNTIME_ROOT
        _prism_runtime_service.migrate_legacy_layout()
    return _prism_runtime_service


def _install_prism_runtime() -> dict[str, Any]:
    tag, backend = _prism_installer().latest_target(_backend(), force=True)
    job = _job("prism-runtime-install", f"Prism-ML {tag} ({backend})")

    def run() -> None:
        _prism_installer().install(tag, backend, job, _download_archive)
        _finish(job, f"Prism-ML {tag} ready")

    _spawn(job, run, "prism-runtime-install")
    return {"job_id": job["job_id"], "kind": "prism_ml", "tag": tag,
            "backend": backend, "repository": "PrismML-Eng/llama.cpp"}




def open_runtime(body: dict[str, Any]) -> dict[str, Any]:
    return _runtime_management().open(body or {})


def runtime_install(body: dict[str, Any]) -> dict[str, Any]:
    return _runtime_management().install(body if isinstance(body, dict) else {})


def _parameter_error(detail: str) -> HTTPException:
    return HTTPException(status_code=422, detail=detail)


def _server_requires_restart() -> bool:
    return _is_server_running()


def _parameters() -> ParameterSettingsService:
    return ParameterSettingsService(
        _option_list, _canonical_option_value, _load_options, _save_options, _presets_store(),
        _parameter_error, time.time, lambda: uuid.uuid4().hex[:12],
    )


# --- Parameter/preset public entrypoints ----------------------------------
#
# The public entrypoint keeps these names so external callers
# (``api.save_model_settings(...)``, ``api.create_preset(...)``, ...) work
# unchanged. Each one simply forwards to the framework-independent transport
# handler in ``parameter_routes``, so all the real logic lives in a single
# module instead of being duplicated in the facade.

_PARAMETER_ROUTE_CONTEXT: ParameterRouteContext | None = None


def _parameter_route_context() -> ParameterRouteContext:
    # NOTE: keep the module-level cache so ``patch.object(api, ...)`` in the
    # public-API regression tests is still honoured every call. A stale cached
    # context would otherwise bind an old ``_state``/``_runtime_kind`` and hide
    # per-test patches.
    global _PARAMETER_ROUTE_CONTEXT
    _PARAMETER_ROUTE_CONTEXT = None
    return ParameterRouteContext(
        options=_option_list, executable=_server_executable, parameters=_parameters,
        server_requires_restart=_server_requires_restart, runtime_kind=_runtime_kind, state=_state,
    )


def _load_presets() -> dict[str, dict[str, Any]]:
    return {row["id"]: {
        "name": row["name"], "model_id": row["model_id"], "options": row["options"],
        "created_at": row["created_at"], "updated_at": row["updated_at"],
    } for row in _parameters().presets()}


def _validate_option_values(options: dict[str, str]) -> None:
    _parameters().validate(options)


def _normalize_options(options: Any) -> dict[str, str]:
    return _parameters().normalize(options)


def model_settings(model_id: str) -> dict[str, Any]:
    return _transport_model_settings(_parameter_route_context(), model_id)


def save_model_settings(model_id: str, body: dict[str, Any]) -> dict[str, Any]:
    return _transport_save_model_settings(_parameter_route_context(), model_id, body)


def presets(model_id: str = "") -> dict[str, Any]:
    return _transport_presets(_parameter_route_context(), model_id)


def create_preset(body: dict[str, Any]) -> dict[str, Any]:
    return _transport_create_preset(_parameter_route_context(), body)


def apply_preset(preset_id: str, body: dict[str, Any]) -> dict[str, Any]:
    return _transport_apply_preset(_parameter_route_context(), preset_id, body)


def rename_preset(preset_id: str, body: dict[str, Any]) -> dict[str, Any]:
    return _transport_rename_preset(_parameter_route_context(), preset_id, body)


def delete_preset(preset_id: str) -> dict[str, Any]:
    return _transport_delete_preset(_parameter_route_context(), preset_id)


router.include_router(create_parameter_router(ParameterRouteContext(
    options=_option_list, executable=_server_executable, parameters=_parameters,
    server_requires_restart=_server_requires_restart, runtime_kind=_runtime_kind, state=_state,
)))
router.include_router(create_runtime_router(RuntimeRouteContext(
    status=status, runtime_info=runtime_info, save=save_runtime, hardware=hardware, catalog=catalog,
    open_runtime=open_runtime, install=runtime_install,
)))
