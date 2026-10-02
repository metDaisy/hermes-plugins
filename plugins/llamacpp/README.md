# llamacpp

Unified standalone llama.cpp plugin.

- **Manager frontend:** `desktop/plugin.js` manages registered GGUF models, parameters, server lifecycle, logs, and backend selection.
- **Official backend:** `dashboard/backends/official.py` installs and runs official ggml-org releases.
- **Prism-ML backend:** `dashboard/backends/prism_ml.py` resolves Bonsai-compatible commands. Runtime download/update discovers the latest complete Windows release directly from `PrismML-Eng/llama.cpp`, installs verified versioned assets like the official backend, and selects it. It never downloads a model.

The plugin owns a standard Hermes `custom` provider endpoint only after a healthy server starts; no separate model-provider plugin is required.
