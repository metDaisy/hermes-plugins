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
    from .application.download_progress import DownloadProgressTracker
    from .application.huggingface_cache import HuggingFaceCacheService
    from .application.huggingface_download import HuggingFaceDownloadService
    from .application.huggingface_model_workflow import HuggingFaceModelWorkflow
    from .application.job_manager import JobManager
    from .application.json_http import JsonHttpClient
    from .application.model_lifecycle import ModelLifecycleService
    from .application.model_registry import RegisteredModelService
    from .application.model_policy import ModelPolicyService
    from .application.option_catalog import ServerOptionCatalog
    from .application.official_runtime import OfficialRuntimeService
    from .application.parameter_settings import ParameterSettingsService
    from .application.preset_store import PresetStore
    from .application.profile_endpoint import ManagedEndpointConfig, write_text_atomically
    from .application.prism_runtime import PrismRuntimeInstaller
    from .application.runtime_inspector import RuntimeInspector
    from .application.runtime_management import RuntimeManagementWorkflow
    from .application.server_lifecycle import ServerLifecycleService
    from .application.server_startup import ServerStartupService
    from .application.state_store import StateStore
    from .routes.model_routes import ModelRouteContext, create_router as create_model_router
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
    from application.download_progress import DownloadProgressTracker
    from application.huggingface_cache import HuggingFaceCacheService
    from application.huggingface_download import HuggingFaceDownloadService
    from application.huggingface_model_workflow import HuggingFaceModelWorkflow
    from application.job_manager import JobManager
    from application.json_http import JsonHttpClient
    from application.model_lifecycle import ModelLifecycleService
    from application.model_registry import RegisteredModelService
    from application.model_policy import ModelPolicyService
    from application.option_catalog import ServerOptionCatalog
    from application.official_runtime import OfficialRuntimeService
    from application.parameter_settings import ParameterSettingsService
    from application.preset_store import PresetStore
    from application.profile_endpoint import ManagedEndpointConfig, write_text_atomically
    from application.prism_runtime import PrismRuntimeInstaller
    from application.runtime_inspector import RuntimeInspector
    from application.runtime_management import RuntimeManagementWorkflow
    from application.server_lifecycle import ServerLifecycleService
    from application.server_startup import ServerStartupService
    from application.state_store import StateStore
    from routes.model_routes import ModelRouteContext, create_router as create_model_router
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
SERVER_LOG_PATH = RUNTIME_ROOT / "logs" / "llama-server.log"
CUSTOM_ENDPOINT_KEY = "llamacpp-local"
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
_prism_release_cache: tuple[float, dict[str, Any]] | None = None
_prism_release_cache_lock = threading.RLock()
_endpoint_config = ManagedEndpointConfig(CUSTOM_ENDPOINT_KEY, CUSTOM_ENDPOINT_BEGIN, CUSTOM_ENDPOINT_END)


def _default_state() -> dict[str, Any]:
    return {"active_model_id": None, "tag": "latest", "backend": "auto", "port": 18434,
            "pid": None, "custom_endpoint": None, "models": {}, "model_settings": {},
            "runtime_kind": "official", "runtime_path": None,
            "runtime_mode": "official", "custom_runtime_path": None}


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

    return HuggingFaceCacheService(lambda: shutil.which("hf"), run)


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
                    size_bytes: int = 0) -> None:
    _models().register(model_id, paths, owned, hf_repo, hf_file, size_bytes)


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
    global _prism_release_cache
    now = time.monotonic()
    with _prism_release_cache_lock:
        if _prism_release_cache and now - _prism_release_cache[0] < 300:
            return dict(_prism_release_cache[1])
        try:
            from .application.prism_runtime import PrismRuntimeInstaller
        except ImportError:
            from application.prism_runtime import PrismRuntimeInstaller
        try:
            tag, cuda_tag = PrismRuntimeInstaller.release_metadata_from_text(
                _http_text("https://raw.githubusercontent.com/PrismML-Eng/Bonsai-demo/main/setup.ps1")
            )
            backend = "cuda" if _backend() == "cuda" else "cpu"
            asset = (
                f"llama-{tag}-bin-win-cuda-{cuda_tag}-x64.zip"
                if backend == "cuda" else f"llama-{tag}-bin-win-cpu-x64.zip"
            )
            release = _http_json(f"https://api.github.com/repos/PrismML-Eng/llama.cpp/releases/tags/{tag}")
            assets = {
                str(item.get("name") or "") for item in release.get("assets", [])
                if isinstance(item, dict)
            } if isinstance(release, dict) else set()
            if asset not in assets:
                raise RuntimeError(f"GitHub release {tag} has no compatible {backend} asset")
            result = {"tag": tag, "backend": backend, "cuda_tag": cuda_tag, "asset": asset}
        except Exception as exc:  # noqa: BLE001
            result = {"tag": None, "error": str(exc)}
        _prism_release_cache = (now, result)
        return dict(result)


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
        if _runtime_kind(state) != "official":
            try:
                return _resolve_server_executable_from_path(str(state.get("runtime_path") or state.get("custom_runtime_path") or ""))
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


def _terminate_server(pid: int) -> None:
    if platform.system().lower() == "windows":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, check=False)
    else:
        os.kill(pid, 15)


def _server_lifecycle() -> ServerLifecycleService:
    return ServerLifecycleService(
        load_state=_state,
        save_state=_save_state,
        pid_alive=_pid_alive,
        terminate=_terminate_server,
        unregister_endpoint=_unregister_custom_endpoint,
        log_path=SERVER_LOG_PATH,
    )


def _health(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1) as response:
            return response.status in (200, 201, 204)
    except Exception:  # noqa: BLE001
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


def _remove_managed_endpoint(text: str) -> tuple[str, bool]:
    return _endpoint_config.remove(text)


def _write_profile_config(path: Path, text: str) -> None:
    write_text_atomically(path, text)


def _register_custom_endpoint(port: int, model_id: str) -> dict[str, Any]:
    base_url = f"http://127.0.0.1:{port}/v1"
    stored_options = _load_options().get(model_id, {})
    try:
        context_length = int(stored_options.get("ctx-size") or 65536)
    except (TypeError, ValueError):
        context_length = 65536
    with _custom_endpoint_lock:
        updates: list[tuple[Path, str]] = []
        for path in _profile_config_paths():
            text = path.read_text(encoding="utf-8")
            updated = _config_with_managed_endpoint(text, base_url, model_id, context_length)
            if updated != text:
                updates.append((path, updated))
        for path, updated in updates:
            _write_profile_config(path, updated)
    return {"key": CUSTOM_ENDPOINT_KEY, "provider": "custom", "base_url": base_url, "model": model_id, "context_length": context_length}


def _unregister_custom_endpoint() -> None:
    with _custom_endpoint_lock:
        updates: list[tuple[Path, str]] = []
        for path in _profile_config_paths():
            text = path.read_text(encoding="utf-8")
            updated, changed = _remove_managed_endpoint(text)
            if changed:
                updates.append((path, updated))
        for path, updated in updates:
            _write_profile_config(path, updated)


def _stop_server(*, preserve_log: bool = False) -> None:
    _server_lifecycle().stop(preserve_log)


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


def _server_startup() -> ServerStartupService:
    def spawn(command: list[str], log: Any, executable: Path) -> subprocess.Popen[Any]:
        return subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                cwd=str(executable.parent), creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    return ServerStartupService(_state, _save_state, _server_executable, _stop_server, _active_path, _load_options,
                                _runtime_kind, get_backend, _serving_model_name, _option_cli_args, _watch_server_process, _health,
                                _register_custom_endpoint, _server_log_tail, SERVER_LOG_PATH, spawn, time.time, time.sleep)


def _start_server() -> None:
    _server_startup().start()


def _server_log_tail(limit: int = 250) -> dict[str, Any]:
    return _server_lifecycle().log_tail(limit)


def _server_rows() -> list[dict[str, Any]]:
    return _model_lifecycle().server_rows()


def _is_server_running() -> bool:
    state = _state()
    return bool(_pid_alive(state.get("pid")) and _health(int(state.get("port") or 18434)))


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


def _status() -> dict[str, Any]:
    return _runtime_inspector().status()


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
    result = subprocess.run(
        [executable, "cache", "rm", repo_id, "--yes", "--format", "quiet"],
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
    def clone(git: str, url: str, destination: Path) -> int:
        result = subprocess.run(
            [git, "clone", "--depth", "1", url, str(destination)], capture_output=True,
            check=False, text=True, encoding="utf-8", errors="replace", timeout=180,
        )
        return result.returncode

    return PrismRuntimeInstaller(
        PRISM_RUNTIME_ROOT, _state, _save_state, lambda: shutil.which("git"), clone, _backend,
        _download_archive, OfficialRuntimeService.extract_archive,
        lambda root: get_backend("prism_ml").resolve_executable(root),
    )


def _install_prism_runtime() -> dict[str, Any]:
    job = _job("prism-runtime-install", "Prism-ML GitHub runtime 준비")

    def run() -> None:
        tag, _ = _prism_installer().install(job)
        _finish(job, f"Prism-ML {tag} ready")

    _spawn(job, run, "prism-runtime-install")
    return {"job_id":job["job_id"],"kind":"prism_ml","repository":"PrismML-Eng/Bonsai-demo"}




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
