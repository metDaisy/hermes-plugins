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
import zipfile
from pathlib import Path
from typing import Any, Callable

from fastapi import APIRouter, HTTPException

try:
    from dashboard.runtimes import get_runtime
except ImportError:
    from runtimes import get_runtime

router = APIRouter()

PLUGIN_DIR = Path(__file__).resolve().parents[1]
PROFILE_ROOT = PLUGIN_DIR.parent.parent
MACHINE_ROOT = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")) / "hermes"
RUNTIME_ROOT = MACHINE_ROOT / "runtimes" / "llamacpp"
HF_HOME = Path(os.environ.get("HF_HOME") or (Path.home() / ".cache" / "huggingface"))
MODELS_ROOT = HF_HOME
ASSETS_ROOT = HF_HOME / "assets"
STATE_PATH = PLUGIN_DIR / "state.json"
OPTIONS_PATH = RUNTIME_ROOT / "model-options.json"
OPTION_METADATA_CACHE_PATH = RUNTIME_ROOT / "parameter-metadata-cache.json"
SERVER_LOG_PATH = RUNTIME_ROOT / "logs" / "llama-server.log"
CUSTOM_ENDPOINT_KEY = "llamacpp-manager-local"
CUSTOM_ENDPOINT_BEGIN = "# BEGIN llamacpp-manager endpoint (managed)"
CUSTOM_ENDPOINT_END = "# END llamacpp-manager endpoint (managed)"
_custom_endpoint_lock = threading.RLock()
_SPLIT_RE = re.compile(r"-\d{5}-of-\d{5}$", re.IGNORECASE)
_OPTION_RE = re.compile(r"(?<![-\w])(?:--[A-Za-z0-9][A-Za-z0-9-]*|-[A-Za-z][A-Za-z0-9-]*)")
_KNOWN_OPTION_CHOICES = {"load-mode": ["auto", "mmap", "none"]}
_option_catalog_cache: tuple[tuple[str, str, str, str, int, int], list[dict[str, Any]]] | None = None
_job_store: dict[str, dict[str, Any]] = {}
_jobs_lock = threading.RLock()
_state_lock = threading.RLock()
_option_cache_lock = threading.RLock()
_latest_cache: tuple[float, str] | None = None


def _default_state() -> dict[str, Any]:
    return {"active_model_id": None, "tag": "latest", "backend": "auto", "port": 18434,
            "pid": None, "custom_endpoint": None, "models": {}, "model_settings": {},
            "parameter_presets": {}, "runtime_kind": "llamacpp", "runtime_path": None,
            "runtime_mode": "official", "custom_runtime_path": None}


def _read_json(path: Path, fallback: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return fallback


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _state() -> dict[str, Any]:
    with _state_lock:
        value = _read_json(STATE_PATH, {})
        state = _default_state()
        if isinstance(value, dict):
            state.update(value)
        return state


def _save_state(state: dict[str, Any]) -> None:
    with _state_lock:
        _write_json(STATE_PATH, state)


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
    headers = {"Accept": "application/json", "User-Agent": "llamacpp-manager"}
    token = os.environ.get("HF_TOKEN") or os.environ.get("hf_token")
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    if token and (host == "huggingface.co" or host.endswith(".huggingface.co") or host == "hf.co"):
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace").strip()
            last_error = RuntimeError(
                f"HTTP {exc.code} from {urllib.parse.urlsplit(url).netloc}"
                + (f": {body[:240]}" if body else "")
            )
            if exc.code not in (408, 429, 500, 502, 503, 504) or attempt == 2:
                raise last_error from exc
        except (URLError, TimeoutError, json.JSONDecodeError) as exc:
            last_error = RuntimeError(f"request failed for {urllib.parse.urlsplit(url).netloc}: {exc}")
            if attempt == 2:
                raise last_error from exc
        time.sleep(1.0 * (attempt + 1))
    raise last_error or RuntimeError("request failed")


def _hf_downloaded_models() -> tuple[list[dict[str, str]], str | None, str | None]:
    executable = shutil.which("hf")
    if not executable:
        return [], None, "hf CLI was not found on PATH"
    result = subprocess.run(
        [executable, "cache", "ls", "--format", "json"],
        capture_output=True, check=False, text=True, encoding="utf-8", errors="replace",
        env=_hf_env(), timeout=30,
    )
    if result.returncode != 0:
        return [], executable, "hf cache listing failed"
    try:
        payload = json.loads(result.stdout or "[]")
    except json.JSONDecodeError:
        return [], executable, "hf cache listing returned invalid JSON"
    if isinstance(payload, dict):
        payload = payload.get("items", [])
    if not isinstance(payload, list):
        return [], executable, "hf cache listing returned an unexpected JSON shape"
    models = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        repo_id = item.get("repo_id")
        size = item.get("size")
        if isinstance(repo_id, str) and repo_id and isinstance(size, str):
            models.append({"repo_id": repo_id, "size": size})
    return models, executable, None


def _hf_cached_files(repo_id: str) -> tuple[list[dict[str, Any]], str | None]:
    executable = shutil.which("hf")
    if not executable:
        return [], "hf CLI was not found on PATH"
    result = subprocess.run(
        [executable, "cache", "ls", "--revisions", "--format", "json"],
        capture_output=True, check=False, text=True, encoding="utf-8", errors="replace",
        env=_hf_env(), timeout=30,
    )
    if result.returncode != 0:
        return [], "HF cache revision listing failed"
    try:
        payload = json.loads(result.stdout or "[]")
    except json.JSONDecodeError:
        return [], "HF cache revision listing returned invalid JSON"
    revision = next((item for item in payload if isinstance(item, dict)
                     and item.get("repo_id") == repo_id and item.get("snapshot_path")), None)
    if not revision:
        return [], "다운로드가 완료된 cache snapshot을 찾을 수 없습니다."
    snapshot = Path(str(revision["snapshot_path"]))
    if not snapshot.is_dir():
        return [], "다운로드가 완료된 cache snapshot을 찾을 수 없습니다."
    grouped: dict[str, dict[str, Any]] = {}
    for path in snapshot.rglob("*.gguf"):
        if not path.is_file() or "mmproj" in path.name.lower() or "draft" in path.name.lower():
            continue
        relative = path.relative_to(snapshot).as_posix()
        label = _SPLIT_RE.sub("", path.stem)
        group = grouped.setdefault(label, {"label": label, "paths": [], "total_bytes": 0,
                                           "fit": "downloaded"})
        group["paths"].append(relative)
        group["total_bytes"] += path.stat().st_size
    return sorted(grouped.values(), key=lambda item: item["total_bytes"], reverse=True), None


def _model_id(path: Path) -> str:
    return _SPLIT_RE.sub("", path.stem)


def _model_rows() -> list[dict[str, Any]]:
    """Return only models explicitly registered in plugin state.

    Files found in HF_HOME are inventory, not registrations. This distinction
    prevents an ordinary HF download from silently becoming an active candidate.
    """
    state = _state()
    registered = state.get("models", {})
    if not isinstance(registered, dict):
        return []

    rows: list[dict[str, Any]] = []
    state_dirty = False
    active = state.get("active_model_id")
    for model_id, entry in registered.items():
        if not isinstance(entry, dict):
            continue
        paths: list[str] = []
        size_bytes = 0
        for raw_path in entry.get("paths", []):
            path = Path(str(raw_path))
            if path.suffix.lower() != ".gguf" or path.name.lower().startswith("mmproj"):
                continue
            resolved = str(path.resolve())
            if resolved in paths:
                continue
            paths.append(resolved)
            try:
                size_bytes += path.stat().st_size
            except OSError:
                pass
        if not size_bytes:
            try:
                size_bytes = max(0, int(entry.get("size_bytes") or 0))
            except (TypeError, ValueError):
                size_bytes = 0
        if not size_bytes and entry.get("hf_repo") and entry.get("hf_file"):
            cached_groups, _ = _hf_cached_files(str(entry["hf_repo"]))
            cached_group = next((group for group in cached_groups
                                 if str(entry["hf_file"]) in group.get("paths", [])), None)
            if cached_group:
                size_bytes = int(cached_group.get("total_bytes") or 0)
                if size_bytes:
                    entry["size_bytes"] = size_bytes
                    state_dirty = True
        row: dict[str, Any] = {
            "id": str(model_id),
            "model_id": str(model_id),
            "paths": sorted(paths),
            "size_bytes": size_bytes,
            "owned": bool(entry.get("owned", False)),
            "active": str(model_id) == str(active),
        }
        if entry.get("hf_repo"):
            row["hf_repo"] = str(entry["hf_repo"])
        if entry.get("hf_file"):
            row["hf_file"] = str(entry["hf_file"])
        row["path"] = row["paths"][0] if row["paths"] else None
        row["size_label"] = f"{size_bytes / (1 << 30):.1f} GB" if size_bytes else "size unknown"
        rows.append(row)
    if state_dirty:
        _save_state(state)
    return sorted(rows, key=lambda item: item["id"].lower())


def _register_model(model_id: str, paths: list[Path], owned: bool,
                    hf_repo: str | None = None, hf_file: str | None = None,
                    size_bytes: int = 0) -> None:
    state = _state()
    models = state.setdefault("models", {})
    entry: dict[str, Any] = {"paths": [str(path.resolve()) for path in paths], "owned": owned}
    if size_bytes > 0:
        entry["size_bytes"] = size_bytes
    if hf_repo:
        entry["hf_repo"] = hf_repo
    if hf_file:
        entry["hf_file"] = hf_file
    models[model_id] = entry
    _save_state(state)


def _job(kind: str, detail: str) -> dict[str, Any]:
    value = {"job_id": uuid.uuid4().hex[:12], "kind": kind, "detail": detail,
             "status": "running", "phase": "starting", "percent": None,
             "created_at": time.time()}
    with _jobs_lock:
        _job_store[value["job_id"]] = value
    return value


def _finish(job: dict[str, Any], detail: str) -> None:
    job.update({"status": "done", "phase": "done", "percent": 100, "detail": detail})


def _fail(job: dict[str, Any], exc: Exception) -> None:
    job.update({"status": "error", "phase": "error", "detail": "작업 실패", "error": str(exc)})


def _spawn(job: dict[str, Any], target: Callable[[], None], name: str) -> None:
    def run() -> None:
        try:
            target()
        except Exception as exc:  # noqa: BLE001
            _fail(job, exc)
    threading.Thread(target=run, daemon=True, name=name).start()


def _jobs() -> list[dict[str, Any]]:
    with _jobs_lock:
        values = list(_job_store.values())
    values.sort(key=lambda item: (item.get("status") != "running", -item.get("created_at", 0)))
    return values[:20]


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


def _detected_devices() -> list[dict[str, Any]]:
    executable = _server_executable()
    if executable is None:
        return []
    try:
        result = subprocess.run([str(executable), "--list-devices"], capture_output=True,
                                check=False, text=True, encoding="utf-8", errors="replace",
                                timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return []
    if result.returncode != 0:
        return []
    devices: list[dict[str, Any]] = []
    for raw_line in result.stdout.splitlines():
        line = raw_line.strip()
        if not line or line.lower().startswith("available devices"):
            continue
        line = re.sub(r"^[-*]\s*", "", line)
        if ":" not in line:
            continue
        device_id, detail = (part.strip() for part in line.split(":", 1))
        memory_match = re.search(r"\(([^()]*)\)\s*$", detail)
        memory = memory_match.group(1).strip() if memory_match else ""
        name = detail[:memory_match.start()].strip() if memory_match else detail
        if not name:
            continue
        device: dict[str, Any] = {"id": device_id, "name": name, "memory": memory}
        vram_match = re.search(
            r"(?P<total>[\d.]+)\s*(?P<total_unit>MiB|GiB|MB|GB)\s*,\s*"
            r"(?P<free>[\d.]+)\s*(?P<free_unit>MiB|GiB|MB|GB)\s+free",
            memory, re.IGNORECASE)
        if vram_match:
            def to_bytes(value: str, unit: str) -> int:
                factor = 1024 ** 3 if unit.lower() in {"gib", "gb"} else 1024 ** 2
                return int(float(value) * factor)

            device["vram_total_bytes"] = to_bytes(vram_match.group("total"), vram_match.group("total_unit"))
            device["vram_free_bytes"] = to_bytes(vram_match.group("free"), vram_match.group("free_unit"))
        devices.append(device)
    return devices


def _release_number(tag: str) -> int:
    return int(tag[1:]) if tag.startswith("b") and tag[1:].isdigit() else 0


def _asset_names(tag: str, backend: str) -> list[str]:
    if platform.system().lower() != "windows":
        raise RuntimeError("standalone runtime installer currently supports Windows only")
    arch = "arm64" if platform.machine().lower() in ("arm64", "aarch64") else "x64"
    if backend == "cuda":
        version = "13.4" if arch == "arm64" else "13.3"
        return [f"llama-{tag}-bin-win-cuda-{version}-{arch}.zip",
                f"cudart-llama-bin-win-cuda-{version}-{arch}.zip"]
    if backend == "cpu":
        return [f"llama-{tag}-bin-win-cpu-{arch}.zip"]
    if backend == "vulkan":
        return [f"llama-{tag}-bin-win-vulkan-x64.zip"]
    raise RuntimeError(f"unsupported Windows backend: {backend}")


def _latest_build(backend: str, force: bool = False) -> str:
    global _latest_cache
    now = time.monotonic()
    if not force and _latest_cache and now - _latest_cache[0] < 300:
        return _latest_cache[1]
    payload = _http_json("https://api.github.com/repos/ggml-org/llama.cpp/releases?per_page=100")
    candidates = []
    for release in payload if isinstance(payload, list) else []:
        if not isinstance(release, dict) or release.get("draft"):
            continue
        tag = str(release.get("tag_name", ""))
        names = {str(item.get("name", "")) for item in release.get("assets", []) if isinstance(item, dict)}
        if tag.startswith("b") and tag[1:].isdigit() and all(x in names for x in _asset_names(tag, backend)):
            candidates.append(tag)
    if not candidates:
        raise RuntimeError(f"GitHub has no complete llama.cpp build for {backend}")
    tag = max(candidates, key=_release_number)
    _latest_cache = (now, tag)
    return tag


def _runtime_target(force_latest: bool = False, requested_backend: Any = None) -> tuple[str, str]:
    state = _state()
    requested = str(requested_backend or "auto").lower()
    backend = requested if requested in {"auto", "cuda", "cpu", "vulkan"} else "auto"
    if backend == "auto":
        backend = _backend()
    configured = str(state.get("tag") or "latest")
    return (_latest_build(backend, force=force_latest) if configured == "latest" else configured), backend


def _installed_target() -> tuple[str, str] | None:
    state = _state()
    preferred = (str(state.get("installed_tag") or ""), str(state.get("installed_backend") or ""))
    candidates = [preferred] if all(preferred) else []
    if RUNTIME_ROOT.is_dir():
        discovered = []
        for tag_dir in RUNTIME_ROOT.iterdir():
            if not tag_dir.is_dir() or not tag_dir.name.startswith("b"):
                continue
            for backend_dir in tag_dir.iterdir():
                if not backend_dir.is_dir():
                    continue
                manifest = _read_json(backend_dir / "manifest.json", {})
                if isinstance(manifest, dict) and manifest.get("verified_version") and _server_executable_in(backend_dir):
                    discovered.append((tag_dir.name, backend_dir.name))
        candidates += sorted(discovered, key=lambda item: _release_number(item[0]), reverse=True)
    for tag, backend in candidates:
        if tag and backend and _server_executable_in(RUNTIME_ROOT / tag / backend):
            return tag, backend
    return None


def _server_executable_in(root: Path) -> Path | None:
    if not root.is_dir():
        return None
    for candidate in (root / "llama-server.exe", root / "llama-server", *root.rglob("llama-server.exe")):
        if candidate.is_file():
            return candidate
    return None


def _runtime_kind(state: dict[str, Any] | None = None) -> str:
    current = state or _state()
    raw = current.get("runtime_kind") or current.get("runtime_mode") or "llamacpp"
    return get_runtime(raw).key


def _resolve_server_executable_from_path(raw_path: Path | str) -> Path:
    """Resolve a user-selected llama-server path through the active adapter."""
    return get_runtime(_runtime_kind()).resolve_executable(raw_path)


def _server_executable(tag: str | None = None, backend: str | None = None) -> Path | None:
    if tag and backend:
        target = (tag, backend)
    else:
        state = _state()
        if _runtime_kind(state) != "llamacpp":
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


def _config_newline(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


def _managed_endpoint_lines(base_url: str, model_id: str, context_length: int, indent: str) -> list[str]:
    prefix = indent
    child = indent + "  "
    model_child = child + "  "
    quoted_model = json.dumps(model_id, ensure_ascii=False)
    quoted_url = json.dumps(base_url, ensure_ascii=False)
    return [
        f"{prefix}{CUSTOM_ENDPOINT_BEGIN}",
        f"{child}{CUSTOM_ENDPOINT_KEY}:",
        f"{model_child}name: llama.cpp (local)",
        f"{model_child}api: {quoted_url}",
        f"{model_child}transport: chat_completions",
        f"{model_child}default_model: {quoted_model}",
        f"{model_child}models:",
        f"{model_child}  {quoted_model}:",
        f"{model_child}    context_length: {context_length}",
        f"{prefix}{CUSTOM_ENDPOINT_END}",
    ]


def _find_managed_endpoint(lines: list[str]) -> tuple[int, int] | None:
    begin = next((index for index, line in enumerate(lines)
                  if line.strip() == CUSTOM_ENDPOINT_BEGIN), None)
    if begin is None:
        return None
    end = next((index for index in range(begin + 1, len(lines))
                if lines[index].strip() == CUSTOM_ENDPOINT_END), None)
    if end is None:
        raise RuntimeError("llamacpp-manager endpoint block is incomplete")
    return begin, end


def _config_with_managed_endpoint(text: str, base_url: str, model_id: str, context_length: int) -> str:
    newline = _config_newline(text)
    lines = text.splitlines()
    managed = _find_managed_endpoint(lines)
    if managed is not None:
        begin, end = managed
        indent = lines[begin][:len(lines[begin]) - len(lines[begin].lstrip())]
        replacement = _managed_endpoint_lines(base_url, model_id, context_length, indent)
        lines[begin:end + 1] = replacement
        return newline.join(lines) + (newline if text.endswith(("\n", "\r")) else "")

    providers_index = next((index for index, line in enumerate(lines)
                            if line.strip() == "providers:" and not line.startswith((" ", "\	"))), None)
    if providers_index is not None:
        key_prefix = "  "
        if any(line.startswith(key_prefix + CUSTOM_ENDPOINT_KEY + ":")
               for line in lines[providers_index + 1:]):
            raise RuntimeError("a non-plugin provider already owns the llamacpp-manager endpoint key")
        insert_at = providers_index + 1
        while insert_at < len(lines) and (not lines[insert_at].strip() or lines[insert_at].startswith((" ", "\	"))):
            insert_at += 1
        block = _managed_endpoint_lines(base_url, model_id, context_length, key_prefix)
        lines[insert_at:insert_at] = block
    else:
        block = ["providers:"] + _managed_endpoint_lines(base_url, model_id, context_length, "  ")
        if lines and lines[-1].strip():
            lines.append("")
        lines.extend(block)
    return newline.join(lines) + newline


def _remove_managed_endpoint(text: str) -> tuple[str, bool]:
    lines = text.splitlines()
    managed = _find_managed_endpoint(lines)
    if managed is None:
        return text, False
    begin, end = managed
    # A root-level block created by this plugin includes the providers: line immediately before it.
    root_created = begin >= 1 and lines[begin - 1].strip() == "providers:" and not lines[begin - 1].startswith((" ", "\	"))
    start = begin - 1 if root_created else begin
    del lines[start:end + 1]
    while start < len(lines) and not lines[start].strip() and (start == 0 or not lines[start - 1].strip()):
        del lines[start]
    newline = _config_newline(text)
    result = newline.join(lines)
    if text.endswith(("\n", "\r")) and result:
        result += newline
    return result, True


def _write_profile_config(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".llamacpp.tmp")
    tmp.write_text(text, encoding="utf-8", newline="")
    os.replace(tmp, path)


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
    return {"key": CUSTOM_ENDPOINT_KEY, "provider": "custom", "provider_profile": "llamacpp-local",
            "base_url": base_url, "model": model_id, "context_length": context_length}


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


def _stop_server() -> None:
    state = _state()
    pid = state.get("pid")
    if _pid_alive(pid):
        if platform.system().lower() == "windows":
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, check=False)
        else:
            os.kill(pid, 15)
    state["pid"] = None
    state["custom_endpoint"] = None
    _save_state(state)
    _unregister_custom_endpoint()
    SERVER_LOG_PATH.unlink(missing_ok=True)


def _watch_server_process(process: subprocess.Popen[Any]) -> None:
    def wait_for_exit() -> None:
        process.wait()
        state = _state()
        if state.get("pid") != process.pid:
            return
        state["pid"] = None
        state["custom_endpoint"] = None
        _save_state(state)
        _unregister_custom_endpoint()
        SERVER_LOG_PATH.unlink(missing_ok=True)

    threading.Thread(target=wait_for_exit, daemon=True, name="llamacpp-server-watch").start()


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
    text = str(value)
    if metadata and metadata.get("key") == "spec-type" and len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"'}:
        text = text[1:-1]
    if metadata and metadata.get("toggle"):
        lowered = text.strip().lower()
        if lowered in {"true", "enabled", "1", "on"}:
            return "on"
        if lowered in {"false", "disabled", "0", "off"}:
            return "off"
    return text


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
    for row in _model_rows():
        if row["id"] == model_id:
            if not row["paths"]:
                break
            return Path(row["paths"][0])
    raise RuntimeError(f"model is not registered: {model_id}")


def _start_server() -> None:
    state = _state()
    model_id = str(state.get("active_model_id") or "")
    if not model_id:
        raise RuntimeError("select a model before starting llama-server")
    executable = _server_executable()
    if executable is None:
        raise RuntimeError("llama-server runtime is not installed")
    _stop_server()
    entry = state.get("models", {}).get(model_id, {}) if isinstance(state.get("models"), dict) else {}
    stored_options = _load_options().get(model_id, {})
    raw_port = stored_options.get("port", state.get("port") or 18434)
    try:
        port = int(raw_port)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"invalid server port: {raw_port}") from exc
    model_path = None if isinstance(entry, dict) and entry.get("hf_repo") else _active_path(model_id)
    adapter = get_runtime(_runtime_kind(state))
    command = adapter.build_command(executable, port, model_id, entry if isinstance(entry, dict) else {}, stored_options,
                                   model_path=model_path)
    command.extend(_option_cli_args({key: value for key, value in stored_options.items() if key != "port"}))
    SERVER_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    log = SERVER_LOG_PATH.open("wb")
    log.write(f"\n--- llama-server start: model={model_id}, port={port} ---\n".encode("utf-8"))
    log.flush()
    flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    try:
        process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                   cwd=str(executable.parent), creationflags=flags)
    except OSError:
        log.close()
        SERVER_LOG_PATH.unlink(missing_ok=True)
        raise
    log.close()
    state["pid"] = process.pid
    state["port"] = port
    _save_state(state)
    _watch_server_process(process)
    deadline = time.time() + 60
    while time.time() < deadline:
        if process.poll() is not None:
            _stop_server()
            raise RuntimeError("llama-server exited during startup")
        if _health(port):
            try:
                endpoint = _register_custom_endpoint(port, model_id)
            except Exception as exc:
                _stop_server()
                raise RuntimeError(f"custom endpoint registration failed: {exc}") from exc
            state = _state()
            if state.get("pid") == process.pid:
                state["custom_endpoint"] = endpoint
                _save_state(state)
            return
        time.sleep(0.5)
    _stop_server()
    raise RuntimeError("llama-server did not become healthy within 60 seconds")


def _server_log_tail(limit: int = 250) -> dict[str, Any]:
    bounded = max(1, min(int(limit), 500))
    try:
        size_bytes = SERVER_LOG_PATH.stat().st_size
        with SERVER_LOG_PATH.open("rb") as stream:
            stream.seek(max(0, size_bytes - 256 * 1024))
            text = stream.read().decode("utf-8", errors="replace")
    except OSError:
        return {"path": str(SERVER_LOG_PATH), "lines": [], "size_bytes": 0}
    lines = text.splitlines()
    return {"path": str(SERVER_LOG_PATH), "lines": lines[-bounded:], "size_bytes": size_bytes}


def _server_rows() -> list[dict[str, Any]]:
    return [{"id": row["id"], "size_bytes": row["size_bytes"], "size_label": row["size_label"]} for row in _model_rows()]


def _runtime_info() -> dict[str, Any]:
    state = _state()
    kind = _runtime_kind(state)
    mode = "official" if kind == "llamacpp" else "custom"
    raw_path = state.get("runtime_path") or state.get("custom_runtime_path")
    path = str(Path(str(raw_path)).expanduser().resolve()) if raw_path else None
    executable = _server_executable()
    adapter = get_runtime(kind)
    return {"kind": kind, "mode": mode, "label": adapter.label, "description": adapter.description,
            "repository": getattr(adapter, "repository", None), "path": path,
            "executable": str(executable) if executable else None,
            "installed": executable is not None, "official": kind == "llamacpp"}


def _status() -> dict[str, Any]:
    state = _state()
    runtime = _runtime_info()
    target = _installed_target() if runtime["kind"] == "llamacpp" else None
    tag, backend = target or (str(state.get("installed_tag") or ""), str(state.get("installed_backend") or ""))
    process_alive = _pid_alive(state.get("pid"))
    running = process_alive and _health(int(state.get("port") or 18434))
    if not process_alive:
        if state.get("pid"):
            state["pid"] = None
            state["custom_endpoint"] = None
            _save_state(state)
        _unregister_custom_endpoint()
        SERVER_LOG_PATH.unlink(missing_ok=True)
    elif not running:
        state["custom_endpoint"] = None
        _save_state(state)
        _unregister_custom_endpoint()
        SERVER_LOG_PATH.unlink(missing_ok=True)
    return {"enabled": True, "tag": tag, "configured_tag": str(state.get("tag") or "latest"),
            "latest_tag": None, "update_available": False, "runtime_installed": bool(_server_executable()),
            "runtime_backend": backend or (_backend() if tag else None), "backend": backend or (_backend() if tag else None),
            "runtime_mode": runtime["mode"], "runtime_path": runtime["path"],
            "runtime_executable": runtime["executable"], "runtime_kind": runtime["kind"],
            "runtime_label": runtime["label"], "runtime_repository": runtime["repository"],
            "devices": _detected_devices(), "server_running": running,
            "server_base_url": f"http://127.0.0.1:{int(state.get('port') or 18434)}" if running else None,
            "custom_endpoint": state.get("custom_endpoint") if running else None,
            "active_model_id": state.get("active_model_id"), "loaded_models": {},
            "models": _server_rows(), "models_dir": str(MODELS_ROOT), "loading": {}, "placement": {}}


_HF_PERCENT_RE = re.compile(r"(?<!\d)(\d{1,3})\s*%")


def _hf_download(repo: str, path: str, job: dict[str, Any] | None = None,
                 part_index: int = 0, part_count: int = 1) -> Path:
    executable = shutil.which("hf")
    if not executable:
        raise RuntimeError("hf CLI was not found on PATH")
    uri = f"hf://{repo}/{path}"
    if job is not None:
        job.update({"phase": "downloading", "detail": f"다운로드 중: {Path(path).name}", "percent":
                    round(part_index / max(1, part_count) * 100)})
    process = subprocess.Popen(
        [executable, "download", uri, "--format", "quiet"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
        env=_hf_env(),
    )
    stdout_chunks: list[str] = []
    stderr_tail = ""

    def consume_stdout() -> None:
        if process.stdout is None:
            return
        while chunk := process.stdout.read(4096):
            stdout_chunks.append(chunk.decode("utf-8", errors="replace")
                                 if isinstance(chunk, bytes) else str(chunk))

    def consume_stderr() -> None:
        nonlocal stderr_tail
        if process.stderr is None:
            return
        while chunk := process.stderr.read(4096):
            text = chunk.decode("utf-8", errors="replace") if isinstance(chunk, bytes) else str(chunk)
            stderr_tail = (stderr_tail + text)[-4096:]
            if job is None:
                continue
            matches = _HF_PERCENT_RE.findall(stderr_tail)
            if not matches:
                continue
            file_percent = min(100, max(0, int(matches[-1])))
            overall = round((part_index + file_percent / 100) / max(1, part_count) * 100)
            job["percent"] = min(100, max(0, overall))

    stdout_thread = threading.Thread(target=consume_stdout, daemon=True, name="llamacpp-hf-stdout")
    stderr_thread = threading.Thread(target=consume_stderr, daemon=True, name="llamacpp-hf-progress")
    stdout_thread.start()
    stderr_thread.start()
    try:
        returncode = process.wait(timeout=6 * 60 * 60)
    except subprocess.TimeoutExpired as exc:
        process.kill()
        process.wait()
        raise RuntimeError(f"hf download timed out for {uri}") from exc
    stdout_thread.join()
    stderr_thread.join()
    if returncode != 0:
        raise RuntimeError(f"hf download failed for {uri} (exit {returncode})")
    output_lines = [line.strip() for line in "".join(stdout_chunks).splitlines() if line.strip()]
    if not output_lines:
        raise RuntimeError(f"hf download returned no local path for {uri}")
    reported = Path(output_lines[-1])
    if not reported.is_absolute():
        reported = (Path.cwd() / reported).resolve()
    if not reported.is_file():
        raise RuntimeError(f"hf download returned a missing local path for {uri}")
    return reported.resolve()


def _local_hf_models() -> dict[str, Any]:
    models, executable, warning = _hf_downloaded_models()
    return {
        "models": models,
        "warning": warning,
    }


def _copy_options(options: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [dict(option, aliases=list(option.get("aliases", [])), choices=list(option.get("choices", [])))
            for option in options]


def _option_cache_identity(executable: Path) -> tuple[str, str, str, str, int, int]:
    target = _installed_target()
    tag, backend = target if target else ("", "")
    try:
        stat = executable.stat()
        return ("2", str(executable.resolve()), tag, backend, stat.st_mtime_ns, stat.st_size)
    except OSError:
        return ("2", str(executable), tag, backend, 0, 0)


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
        options: dict[str, dict[str, Any]] = {}
        for raw in result.stdout.splitlines():
            line = raw.strip()
            if not line.startswith("-") or "--" not in line:
                continue
            separator = re.search(r"\s{2,}(?=[A-Za-z(])", line)
            option_part = line[:separator.start()] if separator else line
            names = _OPTION_RE.findall(option_part)
            if not names:
                continue
            primary = next((name for name in names if name.startswith("--") and not name.startswith("--no-")), names[0])
            key = primary.lstrip("-")
            description = line[separator.end():].strip() if separator else ""
            value_hint = _OPTION_RE.sub("", option_part).strip(" ,")
            choices_match = re.search(r"(?:\[([^\]]+)\]|\{([^}]+)\})", value_hint)
            choices_text = next((value for value in choices_match.groups() if value), "") if choices_match else ""
            choices = [value.strip().strip("'\"") for value in re.split(r"[|,]", choices_text) if value.strip()] if choices_text else []
            toggle = any(name == f"--no-{key}" for name in names)
            if key in _KNOWN_OPTION_CHOICES:
                choices = list(_KNOWN_OPTION_CHOICES[key])
                value_hint = "|".join(choices)
            default_match = re.search(r"default:\s*([^,)]+)", line, re.IGNORECASE)
            default_value = default_match.group(1).strip().strip("'\"") if default_match else None
            value_upper = value_hint.upper()
            value_kind = "choice" if choices else "string"
            if toggle:
                value_hint = "on|off"
                choices = ["on", "off"]
                value_kind = "choice"
                default_value = "on" if str(default_value or "").lower() in {"enabled", "on", "true", "1"} else "off"
            if re.search(r"\b(?:N|NUM|NUMBER|COUNT|SIZE|PORT|LAYERS?)\b", value_upper):
                value_kind = "integer"
            elif re.search(r"\b(?:FLOAT|PROB|PROBABILITY)\b", value_upper):
                value_kind = "number"
            options.setdefault(key, {"key": key, "name": primary, "aliases": names,
                                     "value_hint": value_hint or line, "description": description,
                                     "requires_value": bool(value_hint) or toggle, "default_value": default_value,
                                     "choices": choices, "value_kind": value_kind, "toggle": toggle})
        catalog = list(options.values())
        _option_catalog_cache = (identity, catalog)
        try:
            _write_json(OPTION_METADATA_CACHE_PATH, {"identity": list(identity), "options": catalog})
        except OSError:
            pass
        return _copy_options(catalog)


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
        kind = get_runtime(requested).key
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    mode = "official" if kind == "llamacpp" else "custom"
    custom_path = None
    if kind != "llamacpp":
        raw_path = str(body.get("path") or "").strip()
        if not raw_path:
            raise HTTPException(status_code=422, detail="custom llama.cpp path is required")
        try:
            _resolve_server_executable_from_path(raw_path)
        except RuntimeError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
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


@router.get("/hf-models")
def hf_models() -> dict[str, Any]:
    return _local_hf_models()


@router.get("/hf-models/files")
def hf_model_files(repo_id: str) -> dict[str, Any]:
    files, warning = _hf_cached_files(repo_id)
    return {"repo_id": repo_id, "files": files, "warning": warning}


@router.post("/hf-models/delete")
def delete_hf_model(body: dict[str, Any]) -> dict[str, Any]:
    """Delete one repository from the user's HF cache via the HF CLI."""
    repo_id = str(body.get("repo_id") or "")
    if not repo_id or repo_id != repo_id.strip():
        raise HTTPException(status_code=422, detail="repo_id is required")

    state = _state()
    registered = [
        model_id
        for model_id, entry in (state.get("models") or {}).items()
        if isinstance(entry, dict) and entry.get("hf_repo") == repo_id
    ]
    if registered:
        raise HTTPException(
            status_code=409,
            detail="등록된 모델을 먼저 삭제해야 cache를 삭제할 수 있습니다: " + ", ".join(registered),
        )

    executable = shutil.which("hf")
    if not executable:
        raise HTTPException(status_code=503, detail="hf CLI was not found on PATH")
    result = subprocess.run(
        [executable, "cache", "rm", repo_id, "--yes", "--format", "quiet"],
        capture_output=True,
        check=False,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=_hf_env(),
        timeout=120,
    )
    if result.returncode != 0:
        raise HTTPException(status_code=502, detail="HF cache deletion failed")

    remaining, _, warning = _hf_downloaded_models()
    if warning:
        raise HTTPException(status_code=502, detail="HF cache deletion completed but verification failed")
    if any(item.get("repo_id") == repo_id for item in remaining):
        raise HTTPException(status_code=502, detail="HF cache deletion could not be verified")
    return {"ok": True, "repo_id": repo_id, "deleted": True}


@router.post("/runtime/install")
def runtime_install(body: dict[str, Any]) -> dict[str, Any]:
    try:
        tag, backend = _runtime_target(
            force_latest=True,
            requested_backend=body.get("backend") if isinstance(body, dict) else None,
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"runtime target resolution failed: {exc}") from exc
    job = _job("runtime-install", f"llama.cpp {tag} ({backend})")

    def run() -> None:
        assets = _asset_names(tag, backend)
        download_root = RUNTIME_ROOT / "downloads"
        extract_root = RUNTIME_ROOT / f".{tag}-{backend}-{job['job_id']}"
        install_root = RUNTIME_ROOT / tag / backend
        extract_root.mkdir(parents=True, exist_ok=True)
        try:
            for index, asset in enumerate(assets):
                url = f"https://github.com/ggml-org/llama.cpp/releases/download/{tag}/{asset}"
                archive = download_root / asset
                download_root.mkdir(parents=True, exist_ok=True)
                job["phase"] = "downloading"
                job["detail"] = f"Downloading {asset}"
                with urllib.request.urlopen(url, timeout=120) as response, archive.open("wb") as output:
                    total = int(response.headers.get("Content-Length") or 0)
                    done = 0
                    while chunk := response.read(1 << 20):
                        output.write(chunk)
                        done += len(chunk)
                        job["percent"] = min(90, round((index + (done / total if total else 0)) / len(assets) * 80))
                job["phase"] = "extracting"
                with zipfile.ZipFile(archive) as package:
                    package.extractall(extract_root)
            executable = next(extract_root.rglob("llama-server.exe"), None)
            if executable is None:
                raise RuntimeError("runtime archive did not contain llama-server.exe")
            install_root.parent.mkdir(parents=True, exist_ok=True)
            if install_root.exists():
                shutil.rmtree(install_root)
            shutil.move(str(extract_root), str(install_root))
            _write_json(install_root / "manifest.json", {"tag": tag, "backend": backend, "verified_version": tag})
            state = _state()
            state.update({"installed_tag": tag, "installed_backend": backend})
            _save_state(state)
            job["percent"] = 100
            _finish(job, f"llama.cpp {tag} ready")
        finally:
            shutil.rmtree(extract_root, ignore_errors=True)

    _spawn(job, run, "llamacpp-runtime-install")
    return {"job_id": job["job_id"], "tag": tag, "backend": backend}


@router.post("/server")
def server(body: dict[str, Any]) -> dict[str, Any]:
    action = str(body.get("action") or "")
    if action == "stop":
        _stop_server()
        return {"ok": True, "server_running": False}
    if action == "start":
        state = _state()
        if not state.get("active_model_id"):
            raise HTTPException(status_code=400, detail="select a model before starting llama-server")
        job = _job("server-start", "llama-server 시작 중")

        def run() -> None:
            _start_server()
            _finish(job, "llama-server is healthy")

        _spawn(job, run, "llamacpp-server-start")
        return {"ok": True, "server_running": False, "job_id": job["job_id"]}
    raise HTTPException(status_code=400, detail="action must be start or stop")


@router.post("/activate")
def activate(body: dict[str, Any]) -> dict[str, Any]:
    model_id = str(body.get("model_id") or "")
    state = _state()
    entry = state.get("models", {}).get(model_id) if isinstance(state.get("models"), dict) else None
    if not isinstance(entry, dict):
        raise HTTPException(status_code=404, detail="model is not registered")
    if not entry.get("hf_repo"):
        _active_path(model_id)
    server_running = bool(_pid_alive(state.get("pid")) and _health(int(state.get("port") or 18434)))
    if server_running and state.get("active_model_id") != model_id:
        raise HTTPException(status_code=409, detail="stop llama-server before selecting another model")
    state["active_model_id"] = model_id
    _save_state(state)
    return {"ok": True, "model_id": model_id, "server_running": server_running}


@router.post("/eject")
def eject(body: dict[str, Any]) -> dict[str, Any]:
    model_id = str(body.get("model_id") or "")
    state = _state()
    if state.get("active_model_id") == model_id:
        _stop_server()
        state["active_model_id"] = None
        _save_state(state)
    return {"ok": True, "model_id": model_id}


@router.delete("/models/{model_id}")
def delete(model_id: str) -> dict[str, Any]:
    if _state().get("active_model_id") == model_id:
        _stop_server()
        state = _state()
        state["active_model_id"] = None
        _save_state(state)
    state = _state()
    entry = state.get("models", {}).get(model_id) if isinstance(state.get("models"), dict) else None
    rows = [row for row in _model_rows() if row["id"] == model_id]
    if not entry and not rows:
        raise HTTPException(status_code=404, detail="model not found")
    if isinstance(entry, dict) and entry.get("owned"):
        for raw_path in entry.get("paths", []):
            Path(str(raw_path)).unlink(missing_ok=True)
    elif not entry:
        for path in rows[0]["paths"] if rows else []:
            candidate = Path(path)
            if MODELS_ROOT in candidate.parents:
                candidate.unlink(missing_ok=True)
    if isinstance(state.get("models"), dict):
        state["models"].pop(model_id, None)
    _save_state(state)
    return {"ok": True, "model_id": model_id}


@router.get("/search")
def search(q: str = "", limit: int = 20) -> dict[str, Any]:
    if not q.strip():
        return {"hits": []}
    url = "https://huggingface.co/api/models?" + urllib.parse.urlencode(
        {"search": q, "filter": "gguf", "sort": "downloads", "direction": "-1", "limit": max(1, min(limit, 50))})
    try:
        payload = _http_json(url)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Hugging Face search failed: {exc}") from exc
    return {"hits": [{"repo": str(item.get("id", "")), "downloads": int(item.get("downloads") or 0)}
                    for item in payload if isinstance(item, dict) and item.get("id")]}


@router.get("/repo")
def repo(repo_id: str) -> dict[str, Any]:
    url = f"https://huggingface.co/api/models/{urllib.parse.quote(repo_id, safe='/')}/tree/main?recursive=true&expand=true"
    try:
        payload = _http_json(url)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Could not list {repo_id}: {exc}") from exc
    groups: dict[str, dict[str, Any]] = {}
    for item in payload if isinstance(payload, list) else []:
        path = str(item.get("path", ""))
        if not path.lower().endswith(".gguf") or "mmproj" in path.lower() or "draft" in path.lower():
            continue
        name = Path(path).name
        label = _SPLIT_RE.sub("", Path(name).stem)
        row = groups.setdefault(label, {"label": label, "paths": [], "total_bytes": 0, "fit": "available"})
        row["paths"].append(path)
        row["total_bytes"] += int(item.get("size") or 0)
    return {"files": sorted(groups.values(), key=lambda item: item["total_bytes"], reverse=True)}


@router.post("/download-browsed")
def download_browsed(body: dict[str, Any]) -> dict[str, Any]:
    repo_id = str(body.get("repo") or "")
    paths = [str(path) for path in body.get("paths") or [] if str(path).lower().endswith(".gguf")]
    if not repo_id or not paths:
        raise HTTPException(status_code=422, detail="repo and .gguf paths are required")
    model_id = _model_id(Path(paths[0]))
    job = _job("model-download", model_id)
    job.update({"phase": "downloading", "percent": 0})

    def run() -> None:
        downloaded: list[Path] = []
        for path in paths:
            downloaded.append(_hf_download(repo_id, path, job=job,
                                           part_index=len(downloaded), part_count=len(paths)))
        _finish(job, f"{model_id} downloaded; not registered")

    _spawn(job, run, "llamacpp-hf-download")
    return {"job_id": job["job_id"], "model_id": model_id}


@router.post("/register")
def register(body: dict[str, Any]) -> dict[str, Any]:
    """Explicitly register an HF repo/file without downloading it."""
    repo_id = str(body.get("repo") or "")
    paths = [str(path) for path in body.get("paths") or [] if str(path).lower().endswith(".gguf")]
    if not repo_id or not paths:
        raise HTTPException(status_code=422, detail="repo and .gguf paths are required")
    model_id = _model_id(Path(paths[0]))
    cached_groups, warning = _hf_cached_files(repo_id)
    selected_group = next((group for group in cached_groups if set(group["paths"]) == set(paths)), None)
    if selected_group is None:
        raise HTTPException(status_code=422, detail=warning or "선택한 GGUF가 HF cache에 없습니다")
    size_bytes = int(selected_group.get("total_bytes") or 0)
    _register_model(model_id, [], False, hf_repo=repo_id, hf_file=paths[0], size_bytes=size_bytes)
    return {"ok": True, "model_id": model_id, "registered": True, "downloaded": False, "size_bytes": size_bytes}


@router.get("/jobs")
def jobs() -> dict[str, Any]:
    return {"jobs": _jobs()}


@router.get("/jobs/{job_id}")
def job(job_id: str) -> dict[str, Any]:
    with _jobs_lock:
        value = _job_store.get(job_id)
    if value is None:
        raise HTTPException(status_code=404, detail="job not found")
    return value


@router.get("/logs")
def logs(limit: int = 250) -> dict[str, Any]:
    return _server_log_tail(limit)


@router.get("/settings")
def settings(q: str = "", limit: int = 50) -> dict[str, Any]:
    query = q.strip().lower()
    options = [option for option in _option_list() if query and (query in option["key"].lower() or query in option["name"].lower()
                                                               or query in option["description"].lower())]
    return {"editable": True, "query": q, "options": options[:max(1, min(limit, 100))],
            "runtime": str(_server_executable() or ""), "message": "plugin이 관리하는 llama-server --help에서 검색합니다."}


def _validate_option_values(options: dict[str, str]) -> None:
    catalog = {option["key"]: option for option in _option_list()}
    unknown = sorted(key for key in options if key not in catalog)
    if unknown:
        raise HTTPException(status_code=422, detail=f"unsupported llama-server parameter(s): {', '.join(unknown)}")
    for key, value in options.items():
        metadata = catalog[key]
        text = str(value)
        if metadata["requires_value"] and not text.strip():
            raise HTTPException(status_code=422, detail=f"parameter '{key}' requires a value")
        if not metadata["requires_value"] and text.strip():
            raise HTTPException(status_code=422, detail=f"parameter '{key}' does not accept a value")
        if metadata["choices"] and text not in metadata["choices"]:
            allowed = ", ".join(metadata["choices"])
            raise HTTPException(status_code=422, detail=f"parameter '{key}' must be one of: {allowed}")
        if metadata["value_kind"] == "integer" and not re.fullmatch(r"[+-]?\d+", text):
            raise HTTPException(status_code=422, detail=f"parameter '{key}' requires an integer")
        if metadata["value_kind"] == "number":
            try:
                float(text)
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=f"parameter '{key}' requires a number") from exc


def _normalize_options(options: Any) -> dict[str, str]:
    if not isinstance(options, dict):
        raise HTTPException(status_code=422, detail="options must be an object")
    catalog = {option["key"]: option for option in _option_list()}
    normalized = {str(key).lstrip("-"): _canonical_option_value(catalog.get(str(key).lstrip("-")), value)
                  for key, value in options.items()}
    _validate_option_values(normalized)
    return normalized


def _load_presets() -> dict[str, dict[str, Any]]:
    state = _state()
    raw = state.get("parameter_presets")
    if isinstance(raw, dict):
        return {str(key): dict(value) for key, value in raw.items() if isinstance(value, dict)}
    state["parameter_presets"] = {}
    _save_state(state)
    return {}


def _preset_row(preset_id: str, value: dict[str, Any]) -> dict[str, Any]:
    return {"id": preset_id, "name": str(value.get("name") or preset_id),
            "model_id": value.get("model_id"), "options": dict(value.get("options") or {}),
            "created_at": value.get("created_at"), "updated_at": value.get("updated_at")}


@router.get("/settings/{model_id}")
def model_settings(model_id: str) -> dict[str, Any]:
    stored = _load_options().get(model_id, {})
    catalog = _option_list()
    ordered = {option["key"]: _canonical_option_value(option, stored[option["key"]])
               for option in catalog if option["key"] in stored}
    metadata = {option["key"]: option for option in catalog if option["key"] in ordered}
    return {"model_id": model_id, "options": ordered, "order": [option["key"] for option in catalog], "metadata": metadata}


@router.put("/settings/{model_id}")
def save_model_settings(model_id: str, body: dict[str, Any]) -> dict[str, Any]:
    normalized = _normalize_options(body.get("options") or {})
    all_options = _load_options()
    all_options[model_id] = normalized
    _save_options(all_options)
    state = _state()
    server_running = bool(_pid_alive(state.get("pid")) and _health(int(state.get("port") or 18434)))
    return {"model_id": model_id, "options": normalized, "applied": False,
            "requires_restart": server_running, "job_id": None}


@router.get("/presets")
def presets(model_id: str = "") -> dict[str, Any]:
    wanted = model_id.strip()
    rows = []
    for preset_id, value in _load_presets().items():
        owner = str(value.get("model_id") or "")
        if wanted and owner not in {"", wanted}:
            continue
        rows.append(_preset_row(preset_id, value))
    rows.sort(key=lambda item: (item["name"].lower(), item["id"]))
    return {"presets": rows}


@router.post("/presets")
def create_preset(body: dict[str, Any]) -> dict[str, Any]:
    name = str(body.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=422, detail="preset name is required")
    if len(name) > 100:
        raise HTTPException(status_code=422, detail="preset name must be at most 100 characters")
    model_id = str(body.get("model_id") or "").strip() or None
    options = body.get("options")
    if options is None:
        options = _load_options().get(model_id or "", {})
    normalized = _normalize_options(options)
    now = time.time()
    preset_id = uuid.uuid4().hex[:12]
    state = _state()
    stored = state.setdefault("parameter_presets", {})
    stored[preset_id] = {"name": name, "model_id": model_id, "options": normalized,
                         "created_at": now, "updated_at": now}
    _save_state(state)
    return {"preset": _preset_row(preset_id, stored[preset_id])}


@router.post("/presets/{preset_id}/apply")
def apply_preset(preset_id: str, body: dict[str, Any]) -> dict[str, Any]:
    stored = _load_presets()
    value = stored.get(preset_id)
    if value is None:
        raise HTTPException(status_code=404, detail="preset not found")
    model_id = str(body.get("model_id") or value.get("model_id") or "").strip()
    if not model_id:
        raise HTTPException(status_code=422, detail="model_id is required to apply a preset")
    normalized = _normalize_options(value.get("options") or {})
    all_options = _load_options()
    all_options[model_id] = normalized
    _save_options(all_options)
    state = _state()
    server_running = bool(_pid_alive(state.get("pid")) and _health(int(state.get("port") or 18434)))
    return {"preset_id": preset_id, "model_id": model_id, "options": normalized,
            "applied": False, "requires_restart": server_running}


@router.delete("/presets/{preset_id}")
def delete_preset(preset_id: str) -> dict[str, Any]:
    state = _state()
    stored = state.get("parameter_presets")
    if not isinstance(stored, dict) or preset_id not in stored:
        raise HTTPException(status_code=404, detail="preset not found")
    stored.pop(preset_id, None)
    _save_state(state)
    return {"ok": True, "preset_id": preset_id}


@router.post("/sideload")
def sideload(body: dict[str, Any]) -> dict[str, Any]:
    source = Path(str(body.get("path") or ""))
    if not source.is_file() or source.suffix.lower() != ".gguf":
        raise HTTPException(status_code=422, detail="Pick a .gguf model file")
    model_id = _model_id(source)
    _register_model(model_id, [source], False)
    return {"ok": True, "model_id": model_id, "link_mode": "direct-path", "path": str(source.resolve())}
