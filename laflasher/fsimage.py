"""Embedded "mklittlefs": build and unpack LittleFS partition images.

Uses littlefs-python, which wraps the same littlefs C library that mklittlefs and the
ESP32 esp_littlefs driver are built from, so no external tool is needed.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from littlefs import LittleFS, LittleFSError, UserContext

DEFAULT_BLOCK_SIZE = 4096
# esp_littlefs (Arduino-ESP32 / ESP-IDF) opens paths through a 64 byte buffer incl. NUL.
ESP_VFS_PATH_MAX = 63
DISK_VERSIONS = {"2.0": 0x00020000, "2.1": 0x00020001}
JUNK_FILES = {"thumbs.db", "desktop.ini", ".ds_store"}


@dataclass
class FsParams:
    block_size: int = DEFAULT_BLOCK_SIZE
    name_max: int = 255
    # 2.0 mounts on every esp_littlefs release, old Arduino cores included.
    disk_version: str = "2.0"

    def to_dict(self) -> dict:
        return {"block_size": self.block_size, "name_max": self.name_max,
                "disk_version": self.disk_version}

    @classmethod
    def from_dict(cls, data: dict | None) -> "FsParams":
        params = cls()
        data = data or {}
        params.block_size = int(data.get("block_size", params.block_size))
        params.name_max = int(data.get("name_max", params.name_max))
        version = str(data.get("disk_version", params.disk_version))
        params.disk_version = version if version in DISK_VERSIONS else params.disk_version
        return params


@dataclass
class FsResult:
    data: bytes | None
    used: int
    total: int
    files: int
    dirs: int
    warnings: list[str] = field(default_factory=list)


class FsBuildError(Exception):
    pass


class FsTooSmallError(FsBuildError):
    def __init__(self, needed: int, available: int):
        self.needed, self.available = needed, available
        super().__init__(f"The files need about {needed // 1024} KB but the LittleFS "
                         f"partition only has {available // 1024} KB")


def iter_source(src: Path):
    """Yield (relative posix path, absolute path, is_dir) in a stable order."""
    if not src.is_dir():
        return
    for dirpath, dirnames, filenames in os.walk(src):
        dirnames.sort(key=str.lower)
        base = Path(dirpath)
        for name in dirnames:
            yield (base / name).relative_to(src).as_posix(), base / name, True
        for name in sorted(filenames, key=str.lower):
            if name.lower() in JUNK_FILES:
                continue
            yield (base / name).relative_to(src).as_posix(), base / name, False


def _new_fs(block_count: int, params: FsParams) -> LittleFS:
    fs = LittleFS(
        mount=False,
        block_size=params.block_size,
        block_count=block_count,
        read_size=128,
        prog_size=128,
        cache_size=512,
        lookahead_size=32,
        name_max=params.name_max,
        disk_version=DISK_VERSIONS.get(params.disk_version, 0),
    )
    fs.format()
    fs.mount()
    return fs


def _populate(entries, block_count: int, params: FsParams) -> LittleFS:
    fs = _new_fs(block_count, params)
    for rel, path, is_dir in entries:
        try:
            if is_dir:
                fs.makedirs(rel, exist_ok=True)
            else:
                with open(path, "rb") as src, fs.open(rel, "wb") as dst:
                    shutil.copyfileobj(src, dst, 64 * 1024)
        except LittleFSError as exc:
            if exc.code == LittleFSError.Error.LFS_ERR_NOSPC:
                raise
            if exc.code == LittleFSError.Error.LFS_ERR_NAMETOOLONG:
                raise FsBuildError(f"Name too long for LittleFS (max {params.name_max} bytes): {rel}") from None
            raise FsBuildError(f"Could not add '{rel}': {exc}") from None
    return fs


def build(src: str | Path, size: int, params: FsParams | None = None, want_data: bool = True) -> FsResult:
    """Build an image of exactly `size` bytes from the files in `src`."""
    params = params or FsParams()
    src = Path(src)
    if size <= 0 or size % params.block_size:
        raise FsBuildError(f"LittleFS partition size {size} is not a multiple of the "
                           f"{params.block_size} byte block size")
    block_count = size // params.block_size
    entries = list(iter_source(src))
    try:
        fs = _populate(entries, block_count, params)
    except LittleFSError as exc:
        if exc.code == LittleFSError.Error.LFS_ERR_NOSPC:
            raise FsTooSmallError(estimate_needed(entries, params), size) from None
        raise FsBuildError(str(exc)) from None

    warnings = []
    for rel, _, _ in entries:
        if len(("/" + rel).encode("utf-8")) > ESP_VFS_PATH_MAX:
            warnings.append(f"Path longer than {ESP_VFS_PATH_MAX} characters may not open on the "
                            f"ESP32: /{rel}")
    used = fs.used_block_count * params.block_size
    data = bytes(fs.context.buffer) if want_data else None
    fs.unmount()
    return FsResult(data, used, size, sum(1 for e in entries if not e[2]),
                    sum(1 for e in entries if e[2]), warnings)


def estimate_needed(entries, params: FsParams) -> int:
    total = sum(os.path.getsize(p) for _, p, is_dir in entries if not is_dir)
    blocks = total // params.block_size * 2 + len(entries) * 2 + 16
    fs = _populate(entries, blocks, params)
    return fs.used_block_count * params.block_size


def measure(src: str | Path, size: int, params: FsParams | None = None) -> FsResult:
    return build(src, size, params, want_data=False)


def extract(data: bytes, dest: str | Path, block_size: int = DEFAULT_BLOCK_SIZE) -> int:
    """Unpack a LittleFS image into `dest`. Returns the number of files written."""
    dest = Path(dest)
    # block_count=0 lets littlefs read the real size from the superblock.
    fs = LittleFS(context=UserContext(buffer=bytearray(data)), mount=False,
                  block_size=block_size, block_count=0)
    fs.mount()
    count = 0
    dest_resolved = dest.resolve()

    def inside_dest(target: Path) -> bool:
        return target.resolve().is_relative_to(dest_resolved)

    for root, dirs, files in fs.walk("/"):
        rel_root = root.strip("/")
        for name in dirs:
            if inside_dest(dest / rel_root / name):
                (dest / rel_root / name).mkdir(parents=True, exist_ok=True)
        for name in files:
            target = dest / rel_root / name
            if not inside_dest(target):
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with fs.open(f"{rel_root}/{name}" if rel_root else name, "rb") as src:
                target.write_bytes(src.read())
            count += 1
    fs.unmount()
    return count
