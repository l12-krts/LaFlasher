"""ESP32 chip facts, taken from the embedded esptool so new chips work automatically."""

from __future__ import annotations

import struct
from functools import lru_cache
from pathlib import Path

_PREFERRED_ORDER = [
    "esp32", "esp32s2", "esp32s3", "esp32c2", "esp32c3", "esp32c5", "esp32c6",
    "esp32c61", "esp32h2", "esp32h21", "esp32h4", "esp32p4", "esp32s31", "esp32e22",
]
FLASH_SIZES = ["1MB", "2MB", "4MB", "8MB", "16MB", "32MB", "64MB", "128MB"]


@lru_cache(maxsize=None)
def _defs() -> dict:
    from esptool.targets import CHIP_DEFS
    return {k: v for k, v in CHIP_DEFS.items() if k != "esp8266"}


def chip_keys() -> list[str]:
    defs = _defs()
    return [k for k in _PREFERRED_ORDER if k in defs] + sorted(k for k in defs if k not in _PREFERRED_ORDER)


def chip_label(key: str | None) -> str:
    if not key:
        return "Any ESP32"
    cls = _defs().get(key)
    return cls.CHIP_NAME if cls else key.upper()


def key_from_chip_name(chip_name: str) -> str | None:
    """'ESP32-S3' -> 'esp32s3'."""
    return next((k for k, cls in _defs().items() if cls.CHIP_NAME == chip_name), None)


def bootloader_offset(key: str) -> int:
    return int(getattr(_defs()[key], "BOOTLOADER_FLASH_OFFSET", 0))


def bootloader_offsets_hint() -> str:
    groups: dict[int, list[str]] = {}
    for key in chip_keys():
        groups.setdefault(bootloader_offset(key), []).append(chip_label(key))
    return "; ".join(f"0x{off:x}: {', '.join(names)}" for off, names in sorted(groups.items()))


def _image_header(path: str | Path) -> tuple[int, bytes] | None:
    try:
        with open(path, "rb") as f:
            data = f.read(0x2000 + 24)
    except OSError:
        return None
    # Merged images for ESP32/S2 start with padding; the header sits at the bootloader offset.
    for base in (0x0, 0x1000, 0x2000):
        if len(data) >= base + 24 and data[base] == 0xE9:
            return base, data[base:base + 24]
    return None


def chip_from_image(path: str | Path) -> str | None:
    """Read the target chip from an app/bootloader image header."""
    found = _image_header(path)
    if not found:
        return None
    chip_id = struct.unpack_from("<H", found[1], 12)[0]
    return next((k for k, cls in _defs().items() if getattr(cls, "IMAGE_CHIP_ID", None) == chip_id), None)


def flash_size_from_image(path: str | Path) -> str | None:
    found = _image_header(path)
    if not found:
        return None
    index = found[1][3] >> 4
    return FLASH_SIZES[index] if index < len(FLASH_SIZES) else None


def is_esp_image(path: str | Path) -> bool:
    try:
        with open(path, "rb") as f:
            return f.read(1) == b"\xe9"
    except OSError:
        return False
