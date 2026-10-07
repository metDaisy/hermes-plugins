from __future__ import annotations

import json
from urllib.request import Request, urlopen

from providers import register_provider
from providers.base import ProviderProfile, is_loopback_no_auth_profile


class LlamaCppLocalProfile(ProviderProfile):
    """Stable Hermes provider identity backed by the machine-local coordinator."""

    def build_api_kwargs_extras(self, *, reasoning_config=None, **context):
        del context
        if not isinstance(reasoning_config, dict) or not reasoning_config.get("enabled"):
            return {}, {}
        effort = str(reasoning_config.get("effort") or "").strip()
        if not effort:
            return {}, {}
        return {}, {"reasoning_effort": effort}

    def fetch_models(self, *, api_key=None, base_url=None, timeout=8.0):
        del api_key
        root = str(base_url or self.base_url).rstrip("/")
        request = Request(root + "/models", headers={"Accept": "application/json"})
        with urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
        rows = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            return None
        models = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            model = str(row.get("id") or "").strip()
            if not model:
                continue
            models.append(model)
            try:
                context_length = int(row.get("context_length") or 0)
            except (TypeError, ValueError):
                context_length = 0
            if context_length > 0:
                self.model_capabilities[model] = {"context_window": context_length}
        return models


profile = LlamaCppLocalProfile(
    name="llamacpp-local",
    aliases=("llamacpp-manager",),
    display_name="llama.cpp Local",
    description="Machine-local llama.cpp coordinator managed by the llama.cpp Manager plugin",
    auth_type="none",
    base_url="http://127.0.0.1:18380/v1",
    default_aux_model="compression-local",
    fallback_models=("main-local",),
)

register_provider(profile)
