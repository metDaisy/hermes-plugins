"""Safe version detection and explicit updates for an external Strata checkout."""
from __future__ import annotations

from pathlib import Path
import re
from typing import Any, Callable


class StrataRuntimeService:
    """Own Strata checkout discovery, version projection, and updater execution."""

    def __init__(
        self,
        load_state: Callable[[], dict[str, Any]],
        save_state: Callable[[dict[str, Any]], None],
        run: Callable[[list[str], Path, int], tuple[int, str]],
        platform_name: Callable[[], str],
    ) -> None:
        self._load_state = load_state
        self._save_state = save_state
        self._run = run
        self._platform_name = platform_name

    @staticmethod
    def _root_from_state(state: dict[str, Any]) -> Path | None:
        roots = {
            str(entry.get("runtime_root") or "").strip()
            for entry in (state.get("models") or {}).values()
            if isinstance(entry, dict)
            and str(entry.get("runtime_kind") or "").casefold() == "strata"
            and str(entry.get("runtime_root") or "").strip()
        }
        if not roots and str(state.get("runtime_kind") or "").casefold() == "strata":
            raw_root = str(state.get("runtime_path") or state.get("custom_runtime_path") or "").strip()
            if raw_root:
                roots.add(raw_root)
        if len(roots) > 1:
            raise RuntimeError("multiple Strata runtime roots are registered")
        return Path(next(iter(roots))).expanduser().resolve() if roots else None

    def current(self) -> dict[str, Any]:
        state = self._load_state()
        root = self._root_from_state(state)
        if root is None or not root.is_dir():
            return {"version": None, "installed": False}
        code, output = self._run(
            ["git", "-C", str(root), "describe", "--tags", "--always"], root, 10,
        )
        version = output.strip().splitlines()[-1] if code == 0 and output.strip() else None
        if not version:
            version = str(state.get("strata_version") or "").strip() or None
        return {"version": version, "installed": True}

    def latest(self) -> dict[str, Any]:
        root = self._root_from_state(self._load_state())
        if root is None or not root.is_dir():
            return {"tag": None, "error": "Strata checkout is not registered"}
        code, output = self._run(
            ["git", "-C", str(root), "ls-remote", "--tags", "--refs", "origin"], root, 30,
        )
        if code != 0:
            return {"tag": None, "error": "unable to query Strata tags"}
        tags = []
        for line in output.splitlines():
            ref = line.rsplit("refs/tags/", 1)[-1].strip() if "refs/tags/" in line else ""
            match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", ref)
            if match:
                tags.append((tuple(int(part) for part in match.groups()), ref))
        if not tags:
            return {"tag": None, "error": "no semantic Strata release tags found"}
        return {"tag": max(tags)[1]}

    def update(self) -> str:
        state = self._load_state()
        root = self._root_from_state(state)
        if root is None or not root.is_dir():
            raise RuntimeError("Strata checkout is not registered")

        code, output = self._run(
            ["git", "-C", str(root), "status", "--porcelain", "--untracked-files=no"],
            root,
            10,
        )
        if code != 0:
            raise RuntimeError("unable to inspect the Strata checkout")
        if output.strip():
            raise RuntimeError("Strata checkout has tracked changes; update refused")

        windows = self._platform_name().casefold() == "windows"
        updater = root / ("UPDATE.bat" if windows else "update.sh")
        if not updater.is_file():
            raise RuntimeError(f"Strata updater is missing: {updater.name}")
        command = (
            ["cmd.exe", "/d", "/s", "/c", f'call "{updater}" --yes <nul']
            if windows
            else ["bash", str(updater), "--yes"]
        )
        code, _output = self._run(command, root, 1800)
        if code != 0:
            raise RuntimeError(f"Strata updater exited with code {code}")

        current = self.current()
        version = str(current.get("version") or "").strip()
        if not version:
            raise RuntimeError("Strata update completed but its version could not be detected")
        state["strata_version"] = version
        for entry in (state.get("models") or {}).values():
            if isinstance(entry, dict) and str(entry.get("runtime_kind") or "").casefold() == "strata":
                entry["strata_version"] = version
        self._save_state(state)
        return version
