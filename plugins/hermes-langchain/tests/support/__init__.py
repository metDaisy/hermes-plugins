"""Shared import helper for repository-layout tests."""

from __future__ import annotations

import importlib
from pathlib import Path
import sys
from types import ModuleType


ROOT = Path(__file__).resolve().parents[2]
SOURCE_ROOT = ROOT / "src"
CONTROLLER_WORKERS_ROOT = ROOT / "tests" / "controller" / "workers"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))


def load_module(relative_name: str) -> ModuleType:
    return importlib.import_module(f"hermes_langchain.{relative_name}")


def controller_worker(filename: str) -> Path:
    """Return one test-only controller subprocess worker path."""

    path = CONTROLLER_WORKERS_ROOT / filename
    if not path.is_file():
        raise FileNotFoundError(f"controller test worker not found: {filename}")
    return path
