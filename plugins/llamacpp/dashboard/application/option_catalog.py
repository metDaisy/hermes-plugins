"""Parse and validate the serving binary's llama-server option catalog."""
from __future__ import annotations

import re
from typing import Any

_OPTION_RE = re.compile(r"(?<![-\w])(?:--[A-Za-z0-9][A-Za-z0-9-]*|-[A-Za-z][A-Za-z0-9-]*)")
_CACHE_TYPES = ["f32", "f16", "bf16", "q8_0", "q4_0", "q4_1", "iq4_nl", "q5_0", "q5_1"]
_KNOWN_CHOICES = {
    "load-mode": ["auto", "none", "mmap", "mlock", "mmap+mlock", "dio"],
    "cache-type-k": _CACHE_TYPES,
    "cache-type-v": _CACHE_TYPES,
}


class ServerOptionCatalog:
    """A small value-object module around parsed ``llama-server --help`` data."""

    @staticmethod
    def parse_help(help_text: str) -> list[dict[str, Any]]:
        options: dict[str, dict[str, Any]] = {}
        for raw in help_text.splitlines():
            line = raw.strip()
            if not line.startswith("-") or "--" not in line:
                continue
            separator = re.search(r"\s{2,}(?=[A-Za-z(])", line)
            option_part = line[:separator.start()] if separator else line
            names = _OPTION_RE.findall(option_part)
            if not names:
                continue
            primary = next((name for name in names if name.startswith("--") and not name.startswith("--no-")), names[0])
            key = primary.lstrip("-")
            description = line[separator.end():].strip() if separator else ""
            value_hint = _OPTION_RE.sub("", option_part).strip(" ,")
            if not value_hint and any(name.startswith("--no-") for name in names):
                for name in names:
                    flag_key = name.lstrip("-")
                    options.setdefault(flag_key, ServerOptionCatalog._option(flag_key, name, [name], "", description, False, None, [], "string", False))
                continue
            choices_match = re.search(r"(?:\[([^\]]+)\]|\{([^}]+)\})", value_hint)
            choices_text = next((value for value in choices_match.groups() if value), "") if choices_match else ""
            allowed_match = re.search(r"allowed values:\s*([^\n<(]+)", description, re.IGNORECASE)
            if allowed_match:
                choices_text = allowed_match.group(1).strip().rstrip(".")
            choices = [value.strip().strip("'\"") for value in re.split(r"[|,]", choices_text) if value.strip()] if choices_text else []
            toggle = any(name == f"--no-{key}" for name in names) and bool(value_hint)
            if key in _KNOWN_CHOICES:
                choices, value_hint = list(_KNOWN_CHOICES[key]), "|".join(_KNOWN_CHOICES[key])
            default_match = re.search(r"default:\s*([^,)]+)", line, re.IGNORECASE)
            default_value = default_match.group(1).strip().strip("'\"") if default_match else None
            value_upper = value_hint.upper()
            value_kind = "choice" if choices else "string"
            if toggle:
                value_hint, choices, value_kind = "on|off", ["on", "off"], "choice"
                default_value = "on" if str(default_value or "").lower() in {"enabled", "on", "true", "1"} else "off"
            if re.fullmatch(r"[+-]?\d+\.\d+", str(default_value or "")) or re.search(r"\b(?:FLOAT|PROB|PROBABILITY)\b", value_upper):
                value_kind = "number"
            elif re.search(r"\b(?:N|NUM|NUMBER|COUNT|SIZE|PORT|LAYERS?)\b", value_upper):
                value_kind = "integer"
            options.setdefault(key, ServerOptionCatalog._option(key, primary, names, value_hint or line, description, bool(value_hint) or toggle, default_value, choices, value_kind, toggle))
        return list(options.values())

    @staticmethod
    def copy(options: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [dict(option, aliases=list(option.get("aliases", [])), choices=list(option.get("choices", []))) for option in options]

    @staticmethod
    def canonical(metadata: dict[str, Any] | None, value: Any) -> str:
        text = str(value)
        if metadata and metadata.get("key") == "spec-type" and len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"'}:
            text = text[1:-1]
        if metadata and metadata.get("toggle"):
            lowered = text.strip().lower()
            if lowered in {"true", "enabled", "1", "on"}:
                return "on"
            if lowered in {"false", "disabled", "0", "off"}:
                return "off"
        return text

    @staticmethod
    def _option(key: str, name: str, aliases: list[str], value_hint: str, description: str, requires_value: bool, default_value: str | None, choices: list[str], value_kind: str, toggle: bool) -> dict[str, Any]:
        return {"key": key, "name": name, "aliases": aliases, "value_hint": value_hint, "description": description,
                "requires_value": requires_value, "default_value": default_value, "choices": choices,
                "value_kind": value_kind, "toggle": toggle}
