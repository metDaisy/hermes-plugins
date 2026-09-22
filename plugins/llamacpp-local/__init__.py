"""Hermes model-provider profile for a local llama.cpp-compatible server.

The profile declares the HTTP transport only. Process lifecycle, runtime
selection, model downloads, and parameter presets remain in llamacpp-manager.
"""
from __future__ import annotations

from typing import Any

from providers import register_provider
from providers.base import ProviderProfile

DEFAULT_BASE_URL = "http://127.0.0.1:18434/v1"
DEFAULT_MODEL = "Ternary-Bonsai-2-27B-gguf"


class LlamaCppLocalProfile(ProviderProfile):
    """OpenAI-compatible local endpoint with conservative reasoning defaults."""

    def default_reasoning_config(self, model: str | None = None) -> dict | None:
        """Do not inject hosted-provider reasoning fields into llama-server."""
        return None

    def supported_reasoning_efforts(self, model: str | None) -> tuple[str, ...]:
        """An empty vocabulary tells Hermes to omit reasoning parameters."""
        return ()

    def build_client_kwargs_extras(self, **context: Any) -> dict[str, Any]:
        """Keep local requests free of provider-specific client overrides."""
        return {}


llamacpp_local = LlamaCppLocalProfile(
    name="llamacpp-local",
    aliases=("local-llamacpp", "prism-ml-local", "bonsai-local"),
    display_name="llama.cpp Local",
    description="Local official or Prism-ML llama.cpp server",
    env_vars=("LLAMACPP_LOCAL_API_KEY",),
    base_url=DEFAULT_BASE_URL,
    auth_type="api_key",
    supports_health_check=True,
    supports_model_listing=True,
    fallback_models=(DEFAULT_MODEL,),
    model_aliases={
        "bonsai-2": DEFAULT_MODEL,
        "ternary-bonsai-2": DEFAULT_MODEL,
    },
)

register_provider(llamacpp_local)
