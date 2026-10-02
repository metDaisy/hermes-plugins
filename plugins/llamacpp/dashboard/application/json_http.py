"""Retrying JSON-over-HTTP adapter with scoped Hugging Face auth."""
from __future__ import annotations

import json
import urllib.parse
import urllib.request
from typing import Any, Callable
from urllib.error import HTTPError, URLError


class JsonHttpClient:
    """Fetch JSON while ensuring HF credentials never leave Hugging Face hosts."""

    def __init__(self, token: Callable[[], str | None], open_request: Callable[[urllib.request.Request], Any], sleep: Callable[[float], None]) -> None:
        self._token = token
        self._open_request = open_request
        self._sleep = sleep

    def get(self, url: str) -> Any:
        headers = {"Accept": "application/json", "User-Agent": "llamacpp"}
        host = (urllib.parse.urlsplit(url).hostname or "").lower()
        token = self._token()
        if token and (host == "huggingface.co" or host.endswith(".huggingface.co") or host == "hf.co"):
            headers["Authorization"] = f"Bearer {token}"
        request = urllib.request.Request(url, headers=headers)
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                with self._open_request(request) as response:
                    return json.loads(response.read().decode("utf-8"))
            except HTTPError as exc:
                body = exc.read().decode("utf-8", errors="replace").strip()
                last_error = RuntimeError(f"HTTP {exc.code} from {urllib.parse.urlsplit(url).netloc}" + (f": {body[:240]}" if body else ""))
                if exc.code not in (408, 429, 500, 502, 503, 504) or attempt == 2:
                    raise last_error from exc
            except (URLError, TimeoutError, json.JSONDecodeError) as exc:
                last_error = RuntimeError(f"request failed for {urllib.parse.urlsplit(url).netloc}: {exc}")
                if attempt == 2:
                    raise last_error from exc
            self._sleep(float(attempt + 1))
        raise last_error or RuntimeError("request failed")
