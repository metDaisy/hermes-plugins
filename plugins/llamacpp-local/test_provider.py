"""Behavior tests for the standalone llama.cpp model-provider profile."""
from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

PLUGIN_PATH = Path(__file__).with_name("__init__.py")


class RecordingProfile:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)

    def default_reasoning_config(self, model=None):
        return None

    def supported_reasoning_efforts(self, model=None):
        return None


class LlamaCppLocalProviderTests(unittest.TestCase):
    def _load(self):
        registered = []
        providers = types.ModuleType("providers")
        providers.register_provider = registered.append
        providers_base = types.ModuleType("providers.base")
        providers_base.ProviderProfile = RecordingProfile
        with patch.dict(sys.modules, {"providers": providers, "providers.base": providers_base}):
            spec = importlib.util.spec_from_file_location("llamacpp_local_test", PLUGIN_PATH)
            assert spec and spec.loader
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        return module, registered

    def test_profile_is_registered_with_local_endpoint(self):
        module, registered = self._load()
        self.assertEqual(len(registered), 1)
        profile = registered[0]
        self.assertIs(profile, module.llamacpp_local)
        self.assertEqual(profile.name, "llamacpp-local")
        self.assertEqual(profile.base_url, "http://127.0.0.1:18434/v1")
        self.assertIn("LLAMACPP_LOCAL_API_KEY", profile.env_vars)

    def test_profile_omits_reasoning_parameters(self):
        module, _ = self._load()
        profile = module.llamacpp_local
        self.assertIsNone(profile.default_reasoning_config("bonsai"))
        self.assertEqual(profile.supported_reasoning_efforts("bonsai"), ())

    def test_bonsai_bootstrap_alias_resolves_to_fallback_model(self):
        module, _ = self._load()
        self.assertEqual(module.llamacpp_local.model_aliases["bonsai-2"], module.DEFAULT_MODEL)
        self.assertIn(module.DEFAULT_MODEL, module.llamacpp_local.fallback_models)


if __name__ == "__main__":
    unittest.main()
