# llamacpp-local

Hermes `model-provider` plugin for a local OpenAI-compatible `llama-server`.

## Responsibilities

This plugin only declares the provider profile:

- provider id: `llamacpp-local`
- endpoint: `http://127.0.0.1:18434/v1` by default
- aliases: `local-llamacpp`, `prism-ml-local`, `bonsai-local`
- live model discovery through `/v1/models`
- conservative omission of hosted-provider reasoning fields

`llamacpp-manager` remains responsible for runtime selection, Prism-ML/official
adapter execution, HF GGUF registration, presets, server lifecycle, and logs.

## Setup

1. Install and configure `llamacpp-manager`.
2. Select `llama.cpp official` or `Prism-ML llama.cpp` in its runtime card.
3. Register/activate a GGUF model and start the server.
4. Add a harmless local API-key placeholder to the active profile's `.env`:

   ```dotenv
   LLAMACPP_LOCAL_API_KEY=local
   ```

   `llama-server` normally ignores this value; the OpenAI client requires a
   non-empty key even for a no-auth local endpoint.
5. Select the provider in Hermes:

   ```yaml
   model:
     provider: llamacpp-local
     default: Ternary-Bonsai-2-27B-PQ2_0
   ```

For the Prism Bonsai 2 repository, select `Ternary-Bonsai-2-27B-PQ2_0.gguf`
for an RTX 5080-class Blackwell GPU. If the registered model appears under a
different `/v1/models` id, use that id as `model.default`. The provider
profile's fallback id is only a bootstrap value; live model discovery is
authoritative once the server is running.

To use another port, set `model.base_url` to the server's `/v1` endpoint. The
manager's default port is `18434`.
