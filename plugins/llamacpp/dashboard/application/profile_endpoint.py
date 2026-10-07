"""Managed Hermes provider-block composition for the local llama.cpp server."""
from __future__ import annotations

import json
import os
from pathlib import Path


class ManagedEndpointConfig:
    """Owns only the plugin-managed YAML block, preserving all other config."""

    _ROOT_MARKER = "# llamacpp providers root (managed)"

    def __init__(self, key: str, begin: str, end: str) -> None:
        self._key = key
        self._begin = begin
        self._end = end

    def upsert(self, text: str, base_url: str, model_id: str, context_length: int) -> str:
        return self.upsert_models(text, base_url, model_id, {model_id: context_length})

    def upsert_models(self, text: str, base_url: str, default_model: str,
                      models: dict[str, int]) -> str:
        newline = "\r\n" if "\r\n" in text else "\n"
        lines = text.splitlines()
        managed = self._find(lines)
        if managed is not None:
            begin, end = managed
            indent = lines[begin][: len(lines[begin]) - len(lines[begin].lstrip())]
            lines[begin : end + 1] = self._model_lines(base_url, default_model, models, indent)
            return newline.join(lines) + (newline if text.endswith(("\n", "\r")) else "")

        providers = next((index for index, line in enumerate(lines)
                          if line.strip() == "providers:" and not line.startswith((" ", "\t"))), None)
        if providers is None:
            if lines and lines[-1].strip():
                lines.append("")
            lines.extend([
                self._ROOT_MARKER,
                "providers:",
                *self._model_lines(base_url, default_model, models, "  "),
            ])
        else:
            if any(line.startswith(f"  {self._key}:") for line in lines[providers + 1 :]):
                raise RuntimeError("a non-plugin provider already owns the llamacpp endpoint key")
            insert_at = providers + 1
            while insert_at < len(lines) and (not lines[insert_at].strip() or lines[insert_at].startswith((" ", "\t"))):
                insert_at += 1
            lines[insert_at:insert_at] = self._model_lines(base_url, default_model, models, "  ")
        return newline.join(lines) + newline

    def remove(self, text: str) -> tuple[str, bool]:
        lines = text.splitlines()
        managed = self._find(lines)
        if managed is None:
            return text, False
        begin, end = managed
        root_marker = begin - 2
        root_created = (
            root_marker >= 0
            and lines[root_marker].strip() == self._ROOT_MARKER
            and lines[begin - 1].strip() == "providers:"
            and not lines[begin - 1].startswith((" ", "\t"))
        )
        remove_root = False
        if root_created:
            section_end = next(
                (index for index in range(end + 1, len(lines))
                 if lines[index].strip() and not lines[index].startswith((" ", "\t"))),
                len(lines),
            )
            remove_root = not any(line.strip() for line in lines[end + 1:section_end])
            if not remove_root:
                del lines[root_marker]
                begin -= 1
                end -= 1
        start = root_marker if remove_root else begin
        if remove_root and start > 0 and not lines[start - 1].strip():
            start -= 1
        del lines[start : end + 1]
        while start < len(lines) and not lines[start].strip() and (start == 0 or not lines[start - 1].strip()):
            del lines[start]
        newline = "\r\n" if "\r\n" in text else "\n"
        result = newline.join(lines)
        return (result + newline if text.endswith(("\n", "\r")) and result else result), True

    def _lines(self, base_url: str, model_id: str, context_length: int, indent: str) -> list[str]:
        return self._model_lines(base_url, model_id, {model_id: context_length}, indent)

    def _model_lines(self, base_url: str, default_model: str,
                     models: dict[str, int], indent: str) -> list[str]:
        child, model_child = indent + "  ", indent + "    "
        model, url = json.dumps(default_model, ensure_ascii=False), json.dumps(base_url, ensure_ascii=False)
        lines = [
            f"{indent}{self._begin}", f"{child}{self._key}:", f"{model_child}name: llama.cpp (local)",
            f"{model_child}api: {url}", f"{model_child}transport: chat_completions", f"{model_child}default_model: {model}",
            f"{model_child}models:",
        ]
        for model_id, context_length in models.items():
            encoded = json.dumps(model_id, ensure_ascii=False)
            lines.extend([f"{model_child}  {encoded}:", f"{model_child}    context_length: {int(context_length)}"])
        lines.append(f"{indent}{self._end}")
        return lines

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


def apply_worker_model_policy(text: str) -> str:
    """Assign managed local Main/Aux routes for worker Profiles only."""
    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines()

    def section(key: str, indent: int, start: int = 0,
                stop: int | None = None) -> tuple[int, int] | None:
        prefix = " " * indent + key + ":"
        upper = len(lines) if stop is None else stop
        begin = next((index for index in range(start, upper) if lines[index] == prefix), None)
        if begin is None:
            return None
        end = next(
            (index for index in range(begin + 1, upper)
             if lines[index].strip() and len(lines[index]) - len(lines[index].lstrip()) <= indent),
            upper,
        )
        return begin, end

    def update_scalars(bounds: tuple[int, int], indent: int,
                       values: dict[str, str], remove: set[str]) -> None:
        begin, end = bounds
        child_prefix = " " * (indent + 2)
        deleted = 0
        for index in range(end - 1, begin, -1):
            line = lines[index]
            if not line.startswith(child_prefix) or line.startswith(child_prefix + " ") or ":" not in line:
                continue
            key = line[len(child_prefix):].split(":", 1)[0].strip()
            if key in remove:
                del lines[index]
                deleted += 1
        end -= deleted
        while end > begin + 1 and not lines[end - 1].strip():
            end -= 1
        found: dict[str, int] = {}
        for index in range(begin + 1, end):
            line = lines[index]
            if not line.startswith(child_prefix) or line.startswith(child_prefix + " ") or ":" not in line:
                continue
            key = line[len(child_prefix):].split(":", 1)[0].strip()
            if key in values:
                found[key] = index
        for key, value in values.items():
            encoded = json.dumps(value, ensure_ascii=False)
            if key in found:
                lines[found[key]] = f"{child_prefix}{key}: {encoded}"
            else:
                lines.insert(end, f"{child_prefix}{key}: {encoded}")
                end += 1

    def update_plugin_list(key: str, *, include: bool) -> None:
        plugins = section("plugins", 0)
        if plugins is None:
            if lines and lines[-1].strip():
                lines.append("")
            lines.extend([
                "plugins:",
                "  enabled:",
                "    - llamacpp" if include else "    - agent-audit",
                "  disabled: []" if include else "  disabled:",
            ])
            return

        begin, end = plugins
        prefix = "  " + key + ":"
        item_line = "    - llamacpp"
        key_index = next(
            (index for index in range(begin + 1, end) if lines[index].startswith(prefix)),
            None,
        )
        if key_index is None:
            if include:
                lines[end:end] = [prefix, item_line]
            return

        inline_value = lines[key_index][len(prefix):].strip()
        if inline_value:
            values = []
            if inline_value.startswith("[") and inline_value.endswith("]"):
                values = [
                    value.strip().strip("\"'")
                    for value in inline_value[1:-1].split(",")
                    if value.strip()
                ]
            if include and "llamacpp" not in values:
                values.append("llamacpp")
            if not include:
                values = [value for value in values if value != "llamacpp"]
            if values:
                lines[key_index] = prefix + " [" + ", ".join(values) + "]"
            else:
                lines[key_index] = prefix + " []"
            return

        list_end = next(
            (index for index in range(key_index + 1, end)
             if lines[index].strip() and len(lines[index]) - len(lines[index].lstrip()) <= 2),
            end,
        )
        existing = [lines[index].strip()[2:].strip().strip("\"'")
                    for index in range(key_index + 1, list_end)
                    if lines[index].strip().startswith("- ")]
        if include and "llamacpp" not in existing:
            lines.insert(list_end, item_line)
        elif not include:
            for index in range(list_end - 1, key_index, -1):
                if lines[index].strip().startswith("- ") and \
                        lines[index].strip()[2:].strip().strip("\"'") == "llamacpp":
                    del lines[index]

    model = section("model", 0)
    if model is None:
        lines[0:0] = [
            "model:", '  provider: "llamacpp-local"', '  default: "main-local"',
            '  reasoning_effort: "high"',
        ]
    else:
        update_scalars(
            model, 0,
            {"provider": "llamacpp-local", "default": "main-local", "reasoning_effort": "high"},
            {"base_url", "api_base", "api_mode"},
        )

    auxiliary = section("auxiliary", 0)
    if auxiliary is None:
        if lines and lines[-1].strip():
            lines.append("")
        lines.extend([
            "auxiliary:", "  compression:", '    provider: "llamacpp-local"',
            '    model: "compression-local"', '    reasoning_effort: "none"',
        ])
    else:
        compression = section("compression", 2, auxiliary[0] + 1, auxiliary[1])
        if compression is None:
            lines[auxiliary[1]:auxiliary[1]] = [
                "  compression:", '    provider: "llamacpp-local"',
                '    model: "compression-local"', '    reasoning_effort: "none"',
            ]
        else:
            update_scalars(
                compression, 2,
                {"provider": "llamacpp-local", "model": "compression-local", "reasoning_effort": "none"},
                {"base_url", "api_base", "api_mode"},
            )

    update_plugin_list("enabled", include=True)
    update_plugin_list("disabled", include=False)

    return newline.join(lines) + (newline if text.endswith((chr(10), chr(13))) else "")


def write_text_atomically(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".llamacpp.tmp")
    temporary.write_text(text, encoding="utf-8", newline="")
    os.replace(temporary, path)
