# Backend composition

`backend_impl.py` remains the compatibility facade and FastAPI route assembly point.
New policy concerns are deliberately composed rather than inherited:

- `domain/prism_catalog.py`: immutable Prism model/quant compatibility facts.
- `application/model_policy.py`: backend-aware compatibility decision service.
- `application/download_progress.py`: honest indeterminate-to-observed progress transitions.

The next extractions are HF inventory/download and parameter persistence. Routes keep their existing HTTP contracts while application/infrastructure concerns move behind injected collaborators.
