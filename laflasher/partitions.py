"""ESP-IDF partition tables: read the binary and CSV formats, write the binary one."""

from __future__ import annotations

import csv
import hashlib
import io
import struct
from dataclasses import dataclass
from pathlib import Path

from .util import align_up, parse_int

TABLE_OFFSET = 0x8000
MAX_TABLE_LEN = 0xC00
_ENTRY = struct.Struct("<2sBBLL16sL")
_MAGIC = b"\xaa\x50"
_MD5_MAGIC = b"\xeb\xeb"

TYPES = {"app": 0x00, "data": 0x01, "bootloader": 0x02, "partition_table": 0x03}
SUBTYPES = {
    0x00: {"factory": 0x00, "test": 0x20, **{f"ota_{i}": 0x10 + i for i in range(16)}},
    0x01: {
        "ota": 0x00, "phy": 0x01, "nvs": 0x02, "coredump": 0x03, "nvs_keys": 0x04,
        "efuse": 0x05, "undefined": 0x06, "esphttpd": 0x80, "fat": 0x81,
        "spiffs": 0x82, "littlefs": 0x83,
    },
    0x02: {"primary": 0x00, "ota": 0x01, "recovery": 0x02},
    0x03: {"primary": 0x00, "ota": 0x01},
}
FLAG_BITS = {"encrypted": 0, "readonly": 1}

# Arduino/PlatformIO put LittleFS into "spiffs" subtype partitions, ESP-IDF into "littlefs".
FS_SUBTYPES = (SUBTYPES[1]["spiffs"], SUBTYPES[1]["littlefs"])


@dataclass
class Partition:
    name: str
    type: int
    subtype: int
    offset: int
    size: int
    flags: int = 0

    @property
    def end(self) -> int:
        return self.offset + self.size

    @property
    def type_name(self) -> str:
        return next((k for k, v in TYPES.items() if v == self.type), f"0x{self.type:02x}")

    @property
    def subtype_name(self) -> str:
        names = SUBTYPES.get(self.type, {})
        return next((k for k, v in names.items() if v == self.subtype), f"0x{self.subtype:02x}")

    @property
    def flags_text(self) -> str:
        return ":".join(name for name, bit in FLAG_BITS.items() if self.flags & (1 << bit))

    @property
    def is_app(self) -> bool:
        return self.type == TYPES["app"]

    @property
    def is_filesystem(self) -> bool:
        return self.type == TYPES["data"] and self.subtype in FS_SUBTYPES

    @property
    def is_otadata(self) -> bool:
        return self.type == TYPES["data"] and self.subtype == SUBTYPES[1]["ota"]


def parse_binary(data: bytes) -> list[Partition]:
    parts: list[Partition] = []
    for pos in range(0, min(len(data), MAX_TABLE_LEN), _ENTRY.size):
        chunk = data[pos:pos + _ENTRY.size]
        if len(chunk) < _ENTRY.size or chunk == b"\xff" * _ENTRY.size:
            break
        if chunk[:2] == _MD5_MAGIC:
            if hashlib.md5(data[:pos]).digest() != chunk[16:]:
                raise ValueError("partition table checksum (MD5) does not match")
            break
        if chunk[:2] != _MAGIC:
            raise ValueError(f"not a partition table (bad entry at byte {pos})")
        _, ptype, subtype, offset, size, label, flags = _ENTRY.unpack(chunk)
        name = label.split(b"\0", 1)[0].decode("utf-8", "replace")
        parts.append(Partition(name, ptype, subtype, offset, size, flags))
    if not parts:
        raise ValueError("partition table is empty")
    return parts


def parse_csv(text: str, table_offset: int = TABLE_OFFSET) -> list[Partition]:
    """Parse an ESP-IDF style partitions.csv, filling in blank offsets like gen_esp32part."""
    parts: list[Partition] = []
    next_offset = table_offset + 0x1000
    lines = [line.split("#", 1)[0] for line in text.splitlines()]
    for lineno, row in enumerate(csv.reader(io.StringIO("\n".join(lines))), 1):
        row = [cell.strip() for cell in row]
        if not any(row):
            continue
        row += [""] * (6 - len(row))
        name, ptype_s, subtype_s, offset_s, size_s, flags_s = row[:6]
        try:
            ptype = TYPES[ptype_s.lower()] if ptype_s.lower() in TYPES else parse_int(ptype_s)
            names = SUBTYPES.get(ptype, {})
            if not subtype_s:
                subtype = 0
            elif subtype_s.lower() in names:
                subtype = names[subtype_s.lower()]
            else:
                subtype = parse_int(subtype_s)
            alignment = 0x10000 if ptype == TYPES["app"] else 0x1000
            offset = parse_int(offset_s) if offset_s else align_up(next_offset, alignment)
            size = parse_int(size_s)
        except (ValueError, KeyError) as exc:
            raise ValueError(f"partitions.csv line {lineno}: {exc}") from None
        flags = 0
        for flag in filter(None, (f.strip().lower() for f in flags_s.split(":"))):
            if flag in FLAG_BITS:
                flags |= 1 << FLAG_BITS[flag]
        parts.append(Partition(name, ptype, subtype, offset, size, flags))
        next_offset = offset + size
    if not parts:
        raise ValueError("partitions.csv has no partitions")
    return parts


def to_binary(parts: list[Partition]) -> bytes:
    """Generate the flashable binary table (same layout as gen_esp32part.py, with MD5)."""
    out = b""
    for p in parts:
        label = p.name.encode("utf-8")[:16].ljust(16, b"\0")
        out += _ENTRY.pack(_MAGIC, p.type, p.subtype, p.offset, p.size, label, p.flags)
    out += _MD5_MAGIC + b"\xff" * 14 + hashlib.md5(out).digest()
    if len(out) > MAX_TABLE_LEN:
        raise ValueError("too many partitions")
    return out + b"\xff" * (MAX_TABLE_LEN - len(out))


def load(path: str | Path) -> list[Partition]:
    path = Path(path)
    if path.suffix.lower() == ".csv":
        return parse_csv(path.read_text(encoding="utf-8-sig"))
    return parse_binary(path.read_bytes())
