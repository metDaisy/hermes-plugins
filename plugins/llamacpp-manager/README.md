# llamacpp-manager

Hermes general plugin for managing official or external llama.cpp runtimes, Hugging Face GGUF registrations, model parameters and presets, llama-server lifecycle, health-gated custom endpoints, device information, and ephemeral server logs. The separate `llamacpp-local` model-provider plugin declares the Hermes provider profile for the resulting OpenAI-compatible endpoint.

## External llama.cpp runtime

The runtime selector accepts either a directory containing `llama-server.exe` (or `bin/llama-server.exe`) or the executable itself. This allows Prism-ML and other llama.cpp forks to be selected without copying their binaries into the official runtime directory. Switching the selection does not restart a running server; the next explicit start uses the selected runtime.

## Parameter presets

Model settings are persisted per registered model. The parameter editor can save the current validated values as a named preset, apply a preset to the model, and delete it. Applying or editing settings persists configuration only; a running server remains unchanged until the next explicit start.

## Model-provider separation

`llamacpp-manager` is the general/runtime-management layer. Install `llamacpp-local` separately when Hermes should route agent turns through the managed server as a registered `ProviderProfile`:

```text
llamacpp-manager  -> starts Prism-ML or official llama-server
llamacpp-local    -> declares http://127.0.0.1:18434/v1 to Hermes
```

The manager keeps the legacy managed `providers.llamacpp-manager-local` endpoint block for compatibility. Its runtime status additionally reports `provider_profile: llamacpp-local` so clients can migrate without guessing the endpoint contract.

## Layout

```text
llamacpp-manager/
├── plugin.yaml
├── __init__.py
├── dashboard/
│   ├── manifest.json
│   └── plugin_api.py
└── desktop/
    └── plugin.js
```

`state.json`, runtime binaries, model files, logs, and profile materializations are runtime data and are intentionally not stored in this repository.

## Verification

From the repository root:

```bash
python -m py_compile plugins/llamacpp-manager/dashboard/plugin_api.py
python plugins/llamacpp-manager/test_plugin_api.py
node --check plugins/llamacpp-manager/desktop/plugin.js
```

The plugin is materialized into the active Hermes profiles separately; this directory is the monorepo source of truth.
