from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).parent


def _load_provider():
    captured = []
    spec = importlib.util.spec_from_file_location("llamacpp_provider_test", ROOT / "__init__.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    with patch("providers.register_provider", captured.append):
        spec.loader.exec_module(module)
    assert len(captured) == 1
    return module, captured[0]


def test_manifest_and_profile_declare_loopback_no_auth_provider():
    _module, profile = _load_provider()
    manifest = (ROOT / "plugin.yaml").read_text(encoding="utf-8")

    assert "kind: model-provider" in manifest
    assert profile.name == "llamacpp-local"
    assert profile.auth_type == "none"
    assert profile.base_url == "http://127.0.0.1:18380/v1"
    assert profile.default_aux_model == "compression-local"
    assert profile.fallback_models == ("main-local",)
    assert _module.is_loopback_no_auth_profile(profile) is True


def test_profile_fetches_dynamic_catalog_without_authorization_header():
    module, profile = _load_provider()
    seen = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps({"data": [{"id": "main-local", "context_length": 49152}]}).encode()

    def open_request(request, timeout):
        seen["request"] = request
        seen["timeout"] = timeout
        return Response()

    with patch.object(module, "urlopen", side_effect=open_request):
        models = profile.fetch_models()

    assert models == ["main-local"]
    assert profile.model_capabilities["main-local"]["context_window"] == 49152
    assert seen["request"].full_url == "http://127.0.0.1:18380/v1/models"
    assert seen["request"].get_header("Authorization") is None


def test_profile_forwards_explicit_reasoning_effort_as_top_level_kwarg():
    _module, profile = _load_provider()

    extra_body, top_level = profile.build_api_kwargs_extras(
        reasoning_config={"enabled": True, "effort": "high"},
    )

    assert extra_body == {}
    assert top_level == {"reasoning_effort": "high"}


def test_profile_omits_reasoning_effort_when_not_explicitly_configured():
    _module, profile = _load_provider()

    assert profile.build_api_kwargs_extras(reasoning_config=None) == ({}, {})
    assert profile.build_api_kwargs_extras(reasoning_config={"enabled": False}) == ({}, {})
