"""Standalone llama.cpp manager backend.

This plugin deliberately does not import Hermes core. It owns its API, state,
HF CLI downloads, runtime discovery, model registration, and llama-server
lifecycle while keeping machine-scoped assets in the conventional Hermes paths.
"""
from __future__ import annotations

import atexit
import json
import os
import platform
import re
import shutil
import subprocess
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
    from dashboard.backends import backend_view, get_backend
    from dashboard.application.device_discovery import DeviceDiscoveryService
    from dashboard.application.download_progress import DownloadProgressTracker
    from dashboard.application.huggingface_cache import HuggingFaceCacheService
    from dashboard.application.huggingface_download import HuggingFaceDownloadService
    from dashboard.application.huggingface_model_workflow import HuggingFaceModelWorkflow
    from dashboard.application.job_manager import JobManager
    from dashboard.application.json_http import JsonHttpClient
    from dashboard.application.model_lifecycle import ModelLifecycleService
    from dashboard.application.model_registry import RegisteredModelService
    from dashboard.application.model_policy import ModelPolicyService
    from dashboard.application.option_catalog import ServerOptionCatalog
    from dashboard.application.official_runtime import OfficialRuntimeService
    from dashboard.application.parameter_settings import ParameterSettingsService
    from dashboard.application.profile_endpoint import ManagedEndpointConfig, write_text_atomically
    from dashboard.application.prism_runtime import PrismRuntimeInstaller
    from dashboard.application.runtime_inspector import RuntimeInspector
    from dashboard.application.server_lifecycle import ServerLifecycleService
    from dashboard.application.server_startup import ServerStartupService
    from dashboard.application.state_store import StateStore
    from dashboard.routes.model_routes import ModelRouteContext, create_router as create_model_router
    from dashboard.routes.parameter_routes import ParameterRouteContext, create_router as create_parameter_router
    from dashboard.routes.server_routes import ServerRouteContext, create_router as create_server_router
except ImportError:
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
    from application.profile_endpoint import ManagedEndpointConfig, write_text_atomically
    from application.prism_runtime import PrismRuntimeInstaller
    from application.runtime_inspector import RuntimeInspector
    from application.server_lifecycle import ServerLifecycleService
    from application.server_startup import ServerStartupService
    from application.state_store import StateStore
    from routes.model_routes import ModelRouteContext, create_router as create_model_router
    from routes.parameter_routes import ParameterRouteContext, create_router as create_parameter_router
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
OPTION_METADATA_CACHE_PATH = RUNTIME_ROOT / "parameter-metadata-cache.json"
SERVER_LOG_PATH = RUNTIME_ROOT / "logs" / "llama-server.log"
CUSTOM_ENDPOINT_KEY = "llamacpp-local"
CUSTOM_ENDPOINT_BEGIN = "# BEGIN llamacpp endpoint (managed)"
CUSTOM_ENDPOINT_END = "# END llamacpp endpoint (managed)"
_custom_endpoint_lock = threading.RLock()
_SPLIT_RE = re.compile(r"-\d{5}-of-\d{5}$", re.IGNORECASE)
_option_catalog_cache: tuple[tuple[str, str, str, str, int, int], list[dict[str, Any]]] | None = None
_job_store: dict[str, dict[str, Any]] = {}
_jobs_lock = threading.RLock()
_state_lock = threading.RLock()
_option_cache_lock = threading.RLock()
_model_policy = ModelPolicyService()
_download_progress = DownloadProgressTracker()
_state_store: StateStore | None = None
_state_store_path: Path | None = None
_official_runtime_service: OfficialRuntimeService | None = None
_official_runtime_root: Path | None = None
_endpoint_config = ManagedEndpointConfig(CUSTOM_ENDPOINT_KEY, CUSTOM_ENDPOINT_BEGIN, CUSTOM_ENDPOINT_END)


def _default_state() -> dict[str, Any]:
    return {"active_model_id": None, "tag": "latest", "backend": "auto", "port": 18434,
            "pid": None, "custom_endpoint": None, "models": {}, "model_settings": {},
            "parameter_presets": {}, "runtime_kind": "official", "runtime_path": None,
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


def _model_id(path: Path) -> str:
    return _SPLIT_RE.sub("", path.stem)


def _models() -> RegisteredModelService:
    return RegisteredModelService(_state, _save_state, _hf_cached_files)


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
    try:
        _stop_server()
    except OSError:
        return


atexit.register(_shutdown_server_on_backend_exit)


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
                                _runtime_kind, get_backend, _option_cli_args, _watch_server_process, _health,
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
        server_rows=_server_rows, models_root=MODELS_ROOT,
    )


def _runtime_info() -> dict[str, Any]:
    return _runtime_inspector().info()


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
        cache_models=_hf_downloaded_models, cached_files=_hf_cached_files, http_json=_http_json,
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
        return ("3", str(executable.resolve()), tag, backend, stat.st_mtime_ns, stat.st_size)
    except OSError:
        return ("3", str(executable), tag, backend, 0, 0)


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
@router.get("/status")
def status() -> dict[str, Any]:
    return _status()


@router.get("/runtime")
def runtime_info() -> dict[str, Any]:
    return _runtime_info()


@router.put("/runtime")
def save_runtime(body: dict[str, Any]) -> dict[str, Any]:
    requested = body.get("kind") or body.get("mode") or "llamacpp"
    try:
        kind = get_backend(requested).key
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    mode = "official" if kind == "official" else "custom"
    custom_path = None
    if kind != "official":
        adapter = get_backend(kind)
        raw_path = str(body.get("path") or adapter.managed_root(MACHINE_ROOT)).strip()
        try:
            adapter.resolve_executable(raw_path)
        except RuntimeError as exc:
            raise HTTPException(status_code=422, detail="Prism-ML runtime이 아직 준비되지 않았습니다. 먼저 다운로드를 완료하세요.") from exc
        custom_path = str(Path(raw_path).expanduser().resolve())
    state = _state()
    state["runtime_kind"] = kind
    state["runtime_path"] = custom_path
    state["runtime_mode"] = mode
    state["custom_runtime_path"] = custom_path
    _save_state(state)
    selected = _runtime_info()
    selected["installed"] = bool(selected["executable"])
    selected["requires_restart"] = bool(_pid_alive(state.get("pid")))
    return selected


@router.get("/hardware")
def hardware() -> dict[str, Any]:
    return {"gpu_name": None, "gpu_util_percent": None, "vram_total_bytes": None,
            "vram_usable_bytes": None, "models_dir": str(MODELS_ROOT)}


@router.get("/catalog")
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




@router.post("/runtime/open")
def open_runtime(body: dict[str, Any]) -> dict[str, Any]:
    kind = get_backend((body or {}).get("kind") or _runtime_kind(_state())).key
    target = get_backend(kind).managed_root(MACHINE_ROOT)
    target.mkdir(parents=True, exist_ok=True)
    try:
        os.startfile(str(target))  # type: ignore[attr-defined]
    except OSError as exc:
        raise HTTPException(status_code=503, detail="runtime folder could not be opened") from exc
    return {"ok": True, "kind": kind}


@router.post("/runtime/install")
def runtime_install(body: dict[str, Any]) -> dict[str, Any]:
    requested = body.get("kind") if isinstance(body, dict) else None
    try:
        kind = get_backend(requested or "official").key
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if kind == "prism_ml":
        return _install_prism_runtime()
    try:
        tag, backend = _runtime_target(
            force_latest=True,
            requested_backend=body.get("backend") if isinstance(body, dict) else None,
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"runtime target resolution failed: {exc}") from exc
    job = _job("runtime-install", f"llama.cpp {tag} ({backend})")

    def run() -> None:
        _official_runtime().install(tag, backend, job, _download_archive)
        _finish(job, f"llama.cpp {tag} ready")

    _spawn(job, run, "llamacpp-runtime-install")
    return {"job_id": job["job_id"], "tag": tag, "backend": backend}


def _parameter_error(detail: str) -> HTTPException:
    return HTTPException(status_code=422, detail=detail)


def _server_requires_restart() -> bool:
    return _is_server_running()


def _parameters() -> ParameterSettingsService:
    return ParameterSettingsService(
        _option_list, _canonical_option_value, _load_options, _save_options, _state, _save_state,
        _parameter_error, time.time, lambda: uuid.uuid4().hex[:12],
    )


def _validate_option_values(options: dict[str, str]) -> None:
    _parameters().validate(options)


def _normalize_options(options: Any) -> dict[str, str]:
    return _parameters().normalize(options)


def _load_presets() -> dict[str, dict[str, Any]]:
    return {row["id"]: {"name": row["name"], "model_id": row["model_id"], "options": row["options"],
                        "created_at": row["created_at"], "updated_at": row["updated_at"]}
            for row in _parameters().presets()}


def _preset_row(preset_id: str, value: dict[str, Any]) -> dict[str, Any]:
    return {"id": preset_id, "name": str(value.get("name") or preset_id),
            "model_id": value.get("model_id"), "options": dict(value.get("options") or {}),
            "created_at": value.get("created_at"), "updated_at": value.get("updated_at")}


def model_settings(model_id: str) -> dict[str, Any]:
    return _parameters().model_settings(model_id)


def save_model_settings(model_id: str, body: dict[str, Any]) -> dict[str, Any]:
    return _parameters().save_model(model_id, body.get("options") or {}, _server_requires_restart())


def presets(model_id: str = "") -> dict[str, Any]:
    return {"presets": _parameters().presets()}


def create_preset(body: dict[str, Any]) -> dict[str, Any]:
    return {"preset": _parameters().create_preset(
        body.get("name"), body.get("options"), str(body.get("model_id") or "").strip(),
    )}


def apply_preset(preset_id: str, body: dict[str, Any]) -> dict[str, Any]:
    stored = _load_presets().get(preset_id)
    model_id = str(body.get("model_id") or (stored or {}).get("model_id") or "").strip()
    return _parameters().apply_preset(preset_id, model_id, _runtime_kind(_state()), _server_requires_restart())


def rename_preset(preset_id: str, body: dict[str, Any]) -> dict[str, Any]:
    return {"preset": _parameters().rename_preset(preset_id, body.get("name"))}


def delete_preset(preset_id: str) -> dict[str, Any]:
    _parameters().delete_preset(preset_id)
    return {"ok": True, "preset_id": preset_id}


router.include_router(create_parameter_router(ParameterRouteContext(
    options=_option_list, executable=_server_executable, parameters=_parameters,
    server_requires_restart=_server_requires_restart, runtime_kind=_runtime_kind, state=_state,
)))
