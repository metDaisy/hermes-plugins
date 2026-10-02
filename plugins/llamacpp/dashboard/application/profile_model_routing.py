"""Synchronize Hermes profile main, compression, and fallback model routing."""
from __future__ import annotations

from typing import Iterable


_LOCAL_PROVIDER = "llamacpp-local"
_REMOTE_PROVIDER = "openai-codex"
_REMOTE_MAIN = "gpt-5.6-luna-high"
_REMOTE_FALLBACK = "gpt-5.6-luna"
_REMOTE_BASE_URL = "https://chatgpt.com/backend-api/codex"


def _root_end(lines: list[str], start: int) -> int:
    for index in range(start + 1, len(lines)):
        if lines[index].strip() and not lines[index].startswith((" ", "\t")):
            return index
    return len(lines)


def _find_root(lines: list[str], name: str) -> int | None:
    target = f"{name}:"
    return next(
        (index for index, line in enumerate(lines)
         if line == target or line.startswith(target + " ")),
        None,
    )


def _set_mapping(lines: list[str], section: str, values: Iterable[tuple[str, str]]) -> None:
    start = _find_root(lines, section)
    if start is None:
        if lines and lines[-1].strip():
            lines.append("")
        start = len(lines)
        lines.append(f"{section}:")
    end = _root_end(lines, start)
    for key, value in values:
        prefix = f"  {key}:"
        found = next((index for index in range(start + 1, end) if lines[index].startswith(prefix)), None)
        replacement = f"  {key}: {value}"
        if found is not None:
            lines[found] = replacement
        else:
            lines.insert(end, replacement)
            end += 1


def _replace_root(lines: list[str], section: str, replacement: list[str]) -> None:
    start = _find_root(lines, section)
    if start is None:
        if lines and lines[-1].strip():
            lines.append("")
        lines.extend(replacement)
        return
    lines[start:_root_end(lines, start)] = replacement


def _set_aux_compression(lines: list[str], provider: str, model: str) -> None:
    aux_start = _find_root(lines, "auxiliary")
    if aux_start is None:
        if lines and lines[-1].strip():
            lines.append("")
        lines.extend(["auxiliary:", "  compression:", f"    provider: {provider}", f"    model: {model}"])
        return
    aux_end = _root_end(lines, aux_start)
    compression = next(
        (index for index in range(aux_start + 1, aux_end) if lines[index] == "  compression:"),
        None,
    )
    if compression is None:
        lines[aux_end:aux_end] = ["  compression:", f"    provider: {provider}", f"    model: {model}"]
        return
    child_end = next(
        (index for index in range(compression + 1, aux_end)
         if lines[index].startswith("  ") and not lines[index].startswith("    ") and lines[index].strip()),
        aux_end,
    )
    for key, value in (("provider", provider), ("model", model)):
        prefix = f"    {key}:"
        found = next((index for index in range(compression + 1, child_end) if lines[index].startswith(prefix)), None)
        replacement = f"    {key}: {value}"
        if found is not None:
            lines[found] = replacement
        else:
            lines.insert(child_end, replacement)
            child_end += 1
            aux_end += 1


def sync_profile_models(text: str, *, base_url: str, main_served: bool,
                        compression_served: bool) -> str:
    """Apply the user's fixed model policy while preserving unrelated settings."""
    newline = "\r\n" if "\r\n" in text else "\n"
    trailing = text.endswith(("\n", "\r"))
    lines = text.splitlines()

    if main_served:
        model_values = (
            ("provider", _LOCAL_PROVIDER), ("default", "main-local"),
            ("base_url", base_url), ("api_mode", "chat_completions"),
        )
    else:
        model_values = (
            ("provider", _REMOTE_PROVIDER), ("default", _REMOTE_MAIN),
            ("base_url", _REMOTE_BASE_URL), ("api_mode", "codex_responses"),
        )
    _set_mapping(lines, "model", model_values)
    _replace_root(lines, "fallback_providers", [
        "fallback_providers:", f"  - provider: {_REMOTE_PROVIDER}", f"    model: {_REMOTE_FALLBACK}",
    ])
    _set_aux_compression(
        lines,
        _LOCAL_PROVIDER if compression_served else _REMOTE_PROVIDER,
        "compression-local" if compression_served else _REMOTE_MAIN,
    )
    result = newline.join(lines)
    return result + newline if trailing else result
