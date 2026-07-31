from __future__ import annotations

import hashlib
import re


WINDOWS_INVALID_COMPONENT = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')
WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    "CLOCK$",
    "CONIN$",
    "CONOUT$",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
    "COM¹",
    "COM²",
    "COM³",
    "LPT¹",
    "LPT²",
    "LPT³",
}


def windows_utf16_units(value: str) -> int:
    return len(value.encode("utf-16-le")) // 2


def is_windows_reserved_name(value: str) -> bool:
    base_name = value.split(".", 1)[0].rstrip(" .").upper()
    return base_name in WINDOWS_RESERVED_NAMES


def validate_windows_component(value: str, *, label: str, max_units: int) -> str:
    if not value:
        raise ValueError(f"{label} is required")
    if value != value.strip():
        raise ValueError(f"{label} must not have leading or trailing whitespace")
    if value in {".", ".."}:
        raise ValueError(f"{label} must be a single path segment")
    if value.endswith((" ", ".")):
        raise ValueError(f"{label} must not end with a space or dot")
    if WINDOWS_INVALID_COMPONENT.search(value):
        raise ValueError(f"{label} contains characters unsafe on Windows")
    if is_windows_reserved_name(value):
        raise ValueError(f"{label} uses a reserved Windows device name")
    if windows_utf16_units(value) > max_units:
        raise ValueError(f"{label} exceeds the Windows component length budget")
    return value


def encode_windows_component(
    value: str,
    *,
    max_units: int,
    fallback: str,
) -> str:
    if max_units < 18:
        raise ValueError("Windows component budget is too small for collision-safe encoding")
    raw_value = str(value)
    encoded = WINDOWS_INVALID_COMPONENT.sub("_", raw_value).strip(" .")
    changed = encoded != raw_value
    if not encoded or encoded in {".", ".."}:
        encoded = fallback
        changed = True
    if is_windows_reserved_name(encoded):
        encoded = f"_{encoded}"
        changed = True
    if windows_utf16_units(encoded) > max_units:
        changed = True
    if changed:
        digest = hashlib.sha256(raw_value.encode("utf-8")).hexdigest()[:16]
        suffix = f"-{digest}"
        prefix = _truncate_utf16(encoded, max_units - windows_utf16_units(suffix)).rstrip(" .")
        if not prefix:
            prefix = _truncate_utf16(fallback, max_units - windows_utf16_units(suffix)).rstrip(" .")
        encoded = f"{prefix}{suffix}"
    validate_windows_component(encoded, label="encoded path component", max_units=max_units)
    return encoded


def _truncate_utf16(value: str, max_units: int) -> str:
    output: list[str] = []
    used = 0
    for character in value:
        units = windows_utf16_units(character)
        if used + units > max_units:
            break
        output.append(character)
        used += units
    return "".join(output)
