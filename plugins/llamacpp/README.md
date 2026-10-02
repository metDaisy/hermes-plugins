# llamacpp

Core-native control panel for Hermes Local Models.

- Hermes Core owns the model registry, runtime installation, `llama-server`
  supervisor, jobs, autoload/LRU behavior, and provider assignments.
- The plugin reads those public Core HTTP APIs and exposes Main/Compression role
  assignment, status, hardware, activate/eject, and server controls.
- The plugin does not start a coordinator, install a second runtime, register a
  duplicate provider endpoint, or own another `llama-server` lifecycle.
- A `models_max=1` Core configuration is the expected policy for a 16 GB VRAM
  machine so Main and Compression models are resident one at a time.

The built-in **Settings → Providers → Local Models** UI remains unchanged.
