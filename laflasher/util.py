"""Small helpers shared across the app."""

from __future__ import annotations

import re

_SUFFIXES = {"k": 1024, "kb": 1024, "m": 1024 * 1024, "mb": 1024 * 1024}


def parse_int(value) -> int:
    """Parse numbers the way ESP tooling writes them: 0x9000, 36864, 64K, 1M, 4MB."""
    if isinstance(value, int):
        return value
    text = str(value).strip().lower().replace("_", "")
    if not text:
        raise ValueError("empty number")
    match = re.fullmatch(r"(.+?)\s*(kb|mb|k|m)", text)
    multiplier = 1
    if match and not text.startswith("0x"):
        text, multiplier = match.group(1), _SUFFIXES[match.group(2)]
    return int(text, 0) * multiplier


def hex_addr(value: int) -> str:
    return f"0x{value:x}"


def human_size(n: int | None) -> str:
    if n is None:
        return "?"
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB".replace(".0 KB", " KB")
    return f"{n / (1024 * 1024):.2f} MB".replace(".00 MB", " MB")


def align_up(value: int, alignment: int) -> int:
    return (value + alignment - 1) // alignment * alignment
