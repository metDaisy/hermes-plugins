"""Shared primitives for build-task-graph contract validators."""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urlsplit


WHITESPACE = re.compile(r"\s")


def serialize_task_body(body: Any) -> str:
    """Serialize a structured native task body once, with readable JSON."""
    if isinstance(body, (dict, list)):
        return json.dumps(body, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    if body is None:
        return ""
    if isinstance(body, str):
        return body
    raise TypeError("task body must be an object, array, string, or null")


def is_canonical_https_url(value: Any) -> bool:
    """Return whether value is a canonical absolute HTTPS URL with a real path."""
    if not isinstance(value, str) or not value or value != value.strip() or WHITESPACE.search(value):
        return False
    try:
        parsed = urlsplit(value)
        _ = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and parsed.hostname is not None
        and parsed.username is None
        and parsed.password is None
        and bool(parsed.netloc)
        and parsed.path.startswith("/")
        and parsed.path != "/"
        and not parsed.query
        and not parsed.fragment
    )
