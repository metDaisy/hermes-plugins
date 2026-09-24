"""Parse and validate the serving binary's llama-server option catalog."""
from __future__ import annotations

import re
from typing import Any, Iterator

_OPTION_RE = re.compile(r"(?<![-\w])(?:--[A-Za-z0-9][A-Za-z0-9-]*|-[A-Za-z][A-Za-z0-9-]*)")
_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.+\-]*$")
_CACHE_TYPES = ["f32", "f16", "bf16", "q8_0", "q4_0", "q4_1", "iq4_nl", "q5_0", "q5_1"]
_KNOWN_CHOICES = {
    "load-mode": ["auto", "none", "mmap", "mlock", "mmap+mlock", "dio"],
    "numa": ["distribute", "isolate", "numactl"],
    "cache-type-k": _CACHE_TYPES,
    "cache-type-v": _CACHE_TYPES,
    "spec-draft-type-k": _CACHE_TYPES,
    "spec-draft-type-v": _CACHE_TYPES,
    # ``default`` means omit the flag and let the chat template decide. It is
    # intentionally not a selectable value: the UI represents it by omission.
    "reasoning-effort": ["minimal", "low", "medium", "high", "xhigh", "max"],
}


class ServerOptionCatalog:
    """Parse typed option metadata from the active ``llama-server --help``."""

    @staticmethod
    def parse_help(help_text: str) -> list[dict[str, Any]]:
        options: dict[str, dict[str, Any]] = {}
        for header, continuation in ServerOptionCatalog._blocks(help_text):
            separator = re.search(r"\s{2,}(?=[A-Za-z(])", header)
            option_part = header[:separator.start()] if separator else header
            names = _OPTION_RE.findall(option_part)
            if not names:
                continue
            primary = next((name for name in names if name.startswith("--") and not name.startswith("--no-")), names[0])
            key = primary.lstrip("-")
            inline_description = header[separator.end():].strip() if separator else ""
            description = " ".join(part for part in [inline_description, *continuation] if part).strip()
            value_hint = _OPTION_RE.sub("", option_part).strip(" ,")
            choices = ServerOptionCatalog._choices(key, value_hint, description)
            toggle = any(name == f"--no-{key}" for name in names) and bool(value_hint)

            # Preserve the separate flag representation used by mmproj-auto and
            # other valueless aliases. A real value-bearing enum is handled below.
            if not value_hint and not choices and any(name.startswith("--no-") for name in names):
                for name in names:
                    flag_key = name.lstrip("-")
                    options.setdefault(flag_key, ServerOptionCatalog._option(
                        flag_key, name, [name], "", description, False, None, [], "string", False,
                    ))
                continue

            if key in _KNOWN_CHOICES:
                choices = list(_KNOWN_CHOICES[key])
            if toggle:
                value_hint, choices = "on|off", ["on", "off"]

            default_match = re.search(r"default:\s*(?:'([^']+)'|\"([^\"]+)\"|([^,)\s]+))", f"{header} {description}", re.IGNORECASE)
            raw_default_value = next((value for value in (default_match.groups() if default_match else ()) if value is not None), None)
            default_value = raw_default_value
            if default_value not in choices:
                default_value = None if choices else default_value

            value_upper = value_hint.upper()
            value_kind = "choice" if choices else "string"
            if toggle:
                value_kind = "choice"
                default_value = "on" if str(raw_default_value or "").lower() in {"enabled", "on", "true", "1"} else "off"
            elif not choices and (
                re.fullmatch(r"[+-]?\d+\.\d+", str(default_value or ""))
                or re.search(r"\b(?:FLOAT|PROB|PROBABILITY)\b", value_upper)
            ):
                value_kind = "number"
            elif not choices and re.search(r"\b(?:N|NUM|NUMBER|COUNT|SIZE|PORT|LAYERS?)\b", value_upper):
                value_kind = "integer"

            requires_value = bool(value_hint) or bool(choices) or toggle
            options.setdefault(key, ServerOptionCatalog._option(
                key, primary, names, "|".join(choices) if choices else value_hint or header,
                description, requires_value, default_value, choices, value_kind, toggle,
            ))
        return list(options.values())

    @staticmethod
    def _blocks(help_text: str) -> Iterator[tuple[str, list[str]]]:
        header: str | None = None
        continuation: list[str] = []
        for raw in help_text.splitlines():
            line = raw.strip()
            is_header = line.startswith("-") and "--" in line and not line.startswith("---")
            if is_header:
                if header is not None:
                    yield header, continuation
                header, continuation = line, []
            elif header is not None and line:
                continuation.append(line)
        if header is not None:
            yield header, continuation

    @staticmethod
    def _choices(key: str, value_hint: str, description: str) -> list[str]:
        choices_match = re.search(r"(?:\[([^\]]+)\]|\{([^}]+)\})", value_hint)
        if choices_match:
            choices = ServerOptionCatalog._split_choices(next(value for value in choices_match.groups() if value))
            if choices:
                return choices

        allowed_match = re.search(r"allowed values:\s*([^\n<(]+)", description, re.IGNORECASE)
        if allowed_match:
            choices = ServerOptionCatalog._split_choices(allowed_match.group(1).rstrip("."))
            if choices:
                return choices

        # llama.cpp documents restricted values as bullet lists after "one of"
        # or "Values:". Keep this generic so new flags become typed automatically.
        if re.search(r"\b(?:one of|values):", description, re.IGNORECASE):
            bullets = re.findall(r"(?:^|\s)-\s*([A-Za-z0-9][A-Za-z0-9_.+\-]*)\s*:", description)
            if bullets:
                return list(dict.fromkeys(bullets))

        # reasoning-effort is documented as quoted levels rather than a bracket
        # expression in llama-server --help.
        if re.search(r"\blevel such as\b", description, re.IGNORECASE):
            quoted = [match[0] or match[1] for match in re.findall(r"'([^']+)'|\"([^\"]+)\"", description)]
            choices = [value for value in quoted if value in _KNOWN_CHOICES.get(key, [])]
            if choices:
                return list(dict.fromkeys(choices))

        # Some options put a closed enum directly in the value position, e.g.
        # --spec-type none,draft-simple,... . Do not treat placeholders such as
        # MiB0,MiB1 or comma-separated paths as enums.
        if "," in value_hint:
            candidates = [value.strip().strip("'\"") for value in value_hint.split(",")]
            if len(candidates) > 1 and all(_TOKEN_RE.fullmatch(value) for value in candidates):
                return candidates
        return []

    @staticmethod
    def _split_choices(text: str) -> list[str]:
        values = [value.strip().strip("'\"") for value in re.split(r"[|,]", text)]
        return list(dict.fromkeys(value for value in values if value and _TOKEN_RE.fullmatch(value)))

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
