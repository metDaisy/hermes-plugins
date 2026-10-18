"""Shared import helper for repository-layout tests."""

from __future__ import annotations

import importlib
from pathlib import Path
import sys
from types import ModuleType


ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))


def load_module(relative_name: str) -> ModuleType:
    return importlib.import_module(f"hermes_langchain.{relative_name}")
