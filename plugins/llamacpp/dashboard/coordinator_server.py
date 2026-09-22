"""Machine-scoped llama.cpp plugin backend process."""
from __future__ import annotations

import sys
from pathlib import Path

from fastapi import FastAPI

DASHBOARD_DIR = Path(__file__).resolve().parent
if str(DASHBOARD_DIR) not in sys.path:
    sys.path.insert(0, str(DASHBOARD_DIR))

from backend_impl import router  # noqa: E402

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


@app.get("/__llamacpp_backend_health")
def health() -> dict[str, bool]:
    return {"ok": True}


app.include_router(router)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=18380, log_level="warning")
