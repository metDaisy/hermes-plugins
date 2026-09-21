# llamacpp-manager

Hermes Desktop/Agent plugin for managing llama.cpp runtimes, Hugging Face GGUF registrations, model parameters, llama-server lifecycle, health-gated custom endpoints, device information, and ephemeral server logs.

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
node --check plugins/llamacpp-manager/desktop/plugin.js
```

The plugin is materialized into the active Hermes profiles separately; this directory is the monorepo source of truth.
