"""Managed Hermes provider-block composition for the local llama.cpp server."""
from __future__ import annotations

import json
import os
from pathlib import Path


class ManagedEndpointConfig:
    """Owns only the plugin-managed YAML block, preserving all other config."""

    def __init__(self, key: str, begin: str, end: str) -> None:
        self._key = key
        self._begin = begin
        self._end = end

    def upsert(self, text: str, base_url: str, model_id: str, context_length: int) -> str:
        newline = "\r\n" if "\r\n" in text else "\n"
        lines = text.splitlines()
        managed = self._find(lines)
        if managed is not None:
            begin, end = managed
            indent = lines[begin][: len(lines[begin]) - len(lines[begin].lstrip())]
            lines[begin : end + 1] = self._lines(base_url, model_id, context_length, indent)
            return newline.join(lines) + (newline if text.endswith(("\n", "\r")) else "")

        providers = next((index for index, line in enumerate(lines)
                          if line.strip() == "providers:" and not line.startswith((" ", "\t"))), None)
        if providers is None:
            if lines and lines[-1].strip():
                lines.append("")
            lines.extend(["providers:", *self._lines(base_url, model_id, context_length, "  ")])
        else:
            if any(line.startswith(f"  {self._key}:") for line in lines[providers + 1 :]):
                raise RuntimeError("a non-plugin provider already owns the llamacpp endpoint key")
            insert_at = providers + 1
            while insert_at < len(lines) and (not lines[insert_at].strip() or lines[insert_at].startswith((" ", "\t"))):
                insert_at += 1
            lines[insert_at:insert_at] = self._lines(base_url, model_id, context_length, "  ")
        return newline.join(lines) + newline

    def remove(self, text: str) -> tuple[str, bool]:
        lines = text.splitlines()
        managed = self._find(lines)
        if managed is None:
            return text, False
        begin, end = managed
        root_created = begin >= 1 and lines[begin - 1].strip() == "providers:" and not lines[begin - 1].startswith((" ", "\t"))
        start = begin - 1 if root_created else begin
        del lines[start : end + 1]
        while start < len(lines) and not lines[start].strip() and (start == 0 or not lines[start - 1].strip()):
            del lines[start]
        newline = "\r\n" if "\r\n" in text else "\n"
        result = newline.join(lines)
        return (result + newline if text.endswith(("\n", "\r")) and result else result), True

    def _lines(self, base_url: str, model_id: str, context_length: int, indent: str) -> list[str]:
        child, model_child = indent + "  ", indent + "    "
        model, url = json.dumps(model_id, ensure_ascii=False), json.dumps(base_url, ensure_ascii=False)
        return [
            f"{indent}{self._begin}", f"{child}{self._key}:", f"{model_child}name: llama.cpp (local)",
            f"{model_child}api: {url}", f"{model_child}transport: chat_completions", f"{model_child}default_model: {model}",
            f"{model_child}models:", f"{model_child}  {model}:", f"{model_child}    context_length: {context_length}", f"{indent}{self._end}",
        ]

    def _find(self, lines: list[str]) -> tuple[int, int] | None:
        begin = next((index for index, line in enumerate(lines) if line.strip() == self._begin), None)
        if begin is None:
            return None
        end = next((index for index in range(begin + 1, len(lines)) if lines[index].strip() == self._end), None)
        if end is None:
            # Recover from an interrupted atomic update. The next root-level
            # section is the boundary of the orphaned managed block.
            next_root = next(
                (index for index in range(begin + 1, len(lines))
                 if lines[index].strip() and not lines[index].startswith((" ", "\t"))),
                len(lines),
            )
            end = next_root - 1
        return begin, end


def write_text_atomically(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".llamacpp.tmp")
    temporary.write_text(text, encoding="utf-8", newline="")
    os.replace(temporary, path)
