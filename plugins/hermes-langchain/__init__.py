"""Required Hermes Agent plugin entrypoint.

The implementation lives under ``src/hermes_langchain``. This adapter only
bootstraps that package for repository and installed-profile layouts.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
from types import ModuleType

_PACKAGE_NAME = "_hermes_langchain_source"
_SOURCE_PACKAGE = Path(__file__).resolve().parent / "src" / "hermes_langchain"


def _load_source_package() -> ModuleType:
    module = sys.modules.get(_PACKAGE_NAME)
    if module is not None:
        if Path(module.__file__).resolve() != (_SOURCE_PACKAGE / "__init__.py").resolve():
            raise ImportError(f"{_PACKAGE_NAME} is bound to another path")
        return module
    spec = importlib.util.spec_from_file_location(
        _PACKAGE_NAME,
        _SOURCE_PACKAGE / "__init__.py",
        submodule_search_locations=[str(_SOURCE_PACKAGE)],
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load Hermes LangChain source package from {_SOURCE_PACKAGE}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[_PACKAGE_NAME] = module
    spec.loader.exec_module(module)
    return module


register = _load_source_package().register

__all__ = ["register"]
