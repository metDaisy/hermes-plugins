"""llama-server device discovery and VRAM parsing."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Callable


class DeviceDiscoveryService:
    """Hide binary output parsing behind a stable device projection."""

    _memory = re.compile(
        r"(?P<total>[\d.]+)\s*(?P<total_unit>MiB|GiB|MB|GB)\s*,\s*"
        r"(?P<free>[\d.]+)\s*(?P<free_unit>MiB|GiB|MB|GB)\s+free",
        re.IGNORECASE,
    )

    def __init__(self, executable: Callable[[], Path | None], run: Callable[[list[str]], tuple[int, str]]) -> None:
        self._executable = executable
        self._run = run

    def devices(self) -> list[dict[str, Any]]:
        executable = self._executable()
        if executable is None:
            return []
        try:
            returncode, output = self._run([str(executable), "--list-devices"])
        except OSError:
            return []
        if returncode != 0:
            return []
        devices: list[dict[str, Any]] = []
        for raw_line in output.splitlines():
            line = re.sub(r"^[-*]\s*", "", raw_line.strip())
            if not line or line.lower().startswith("available devices") or ":" not in line:
                continue
            device_id, detail = (part.strip() for part in line.split(":", 1))
            memory_match = re.search(r"\(([^()]*)\)\s*$", detail)
            memory = memory_match.group(1).strip() if memory_match else ""
            name = detail[:memory_match.start()].strip() if memory_match else detail
            if not name:
                continue
            device: dict[str, Any] = {"id": device_id, "name": name, "memory": memory}
            vram = self._memory.search(memory)
            if vram:
                device["vram_total_bytes"] = self._bytes(vram.group("total"), vram.group("total_unit"))
                device["vram_free_bytes"] = self._bytes(vram.group("free"), vram.group("free_unit"))
            devices.append(device)
        return devices

    @staticmethod
    def _bytes(value: str, unit: str) -> int:
        factor = 1024 ** 3 if unit.lower() in {"gib", "gb"} else 1024 ** 2
        return int(float(value) * factor)
