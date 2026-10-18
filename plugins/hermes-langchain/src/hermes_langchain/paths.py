"""Stable paths for the repository and installed plugin layouts."""

from __future__ import annotations

from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parent
SOURCE_ROOT = PACKAGE_ROOT.parent
PLUGIN_ROOT = SOURCE_ROOT.parent


def source_file(*parts: str) -> Path:
    """Return one implementation file below ``src/hermes_langchain``."""

    return PACKAGE_ROOT.joinpath(*parts)
