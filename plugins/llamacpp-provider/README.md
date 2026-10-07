# llama.cpp Local Provider

Thin Hermes Model Provider adapter for the machine-local coordinator managed by the sibling `llamacpp` plugin.

- Provider: `llamacpp-local`
- Endpoint: `http://127.0.0.1:18380/v1`
- Authentication: none; Core restricts this mode to explicit loopback endpoints.
- Catalog: discovers the user-selectable `main-local` alias and its effective context window from `/v1/models`.
- Auxiliary: `compression-local` remains an explicit auxiliary model and is not shown in the Main model picker.
- Offline fallback: only `main-local`; provider discovery remains available while coordinator health/catalog probing fails honestly.
