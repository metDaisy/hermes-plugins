"""Non-destructive live proof for the machine-scoped llama.cpp coordinator."""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

HERMES_ROOT = Path.home() / "AppData" / "Local" / "hermes"
HEALTH = "http://127.0.0.1:18380/__llamacpp_backend_health"
EXPECTED_SERVICE = "hermes-llamacpp-coordinator"
EXPECTED_PROTOCOL = 1
EXPECTED_BUILD = "0.2.45"


def load_proxy(profile: str):
    path = HERMES_ROOT / "profiles" / profile / "plugins" / "llamacpp" / "dashboard" / "plugin_api.py"
    spec = importlib.util.spec_from_file_location(f"llamacpp_proxy_{profile}", path)
    if not spec or not spec.loader:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def health() -> dict:
    with urllib.request.urlopen(HEALTH, timeout=3) as response:
        payload = json.loads(response.read())
    expected = {
        "ok": True,
        "service": EXPECTED_SERVICE,
        "protocol": EXPECTED_PROTOCOL,
        "build": EXPECTED_BUILD,
    }
    if any(payload.get(key) != value for key, value in expected.items()):
        raise RuntimeError(f"unexpected coordinator identity: {payload}")
    return payload


def listener_pids() -> set[int]:
    result = subprocess.run(["netstat", "-ano"], capture_output=True, text=False, check=True)
    pids: set[int] = set()
    for line in result.stdout.decode("ascii", errors="ignore").splitlines():
        fields = line.split()
        if len(fields) >= 5 and fields[0].upper() == "TCP" and fields[1].endswith(":18380") and fields[3].upper() == "LISTENING":
            pids.add(int(fields[4]))
    return pids


def main() -> None:
    profiles = [name for name in ("main", "coder") if (HERMES_ROOT / "profiles" / name / "plugins" / "llamacpp").is_dir()]
    if len(profiles) < 2:
        raise SystemExit("two installed profile copies are required")
    seen = []
    for profile in profiles:
        proxy = load_proxy(profile)
        proxy._ensure_coordinator()
        seen.append(int(health()["pid"]))
    if len(set(seen)) != 1:
        raise SystemExit(f"profile proxies reached different coordinators: {seen}")
    owner = seen[0]
    pids = listener_pids()
    if pids != {owner}:
        raise SystemExit(f"expected one :18380 listener owned by {owner}, found {sorted(pids)}")

    script = HERMES_ROOT / "profiles" / profiles[0] / "plugins" / "llamacpp" / "dashboard" / "coordinator_server.py"
    duplicate = subprocess.run([sys.executable, str(script)], capture_output=True, text=False, timeout=10, check=False)
    if duplicate.returncode != 0:
        raise SystemExit(f"duplicate coordinator did not exit cleanly: {duplicate.returncode}")
    time.sleep(0.2)
    after = health()
    if int(after["pid"]) != owner or listener_pids() != {owner}:
        raise SystemExit("duplicate launch disturbed the healthy coordinator")
    print(f"shared coordinator live verification passed: pid={owner}, profiles={','.join(profiles)}")


if __name__ == "__main__":
    main()
