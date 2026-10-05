"""Standalone flashers: one .exe that is LaFlasher plus a .laflash package.

An exported flasher is a copy of LaFlasher's single-file build (the "runtime") with the
package zip appended and a small trailer at the very end:

    [runtime exe][package zip][payload length, key, reserved, MAGIC]

PyInstaller's loader finds its own archive by searching backwards for its cookie, so the
appended bytes don't disturb it. On start-up LaFlasher reads the trailer of its own exe and,
if a package is there, opens the simple flasher window instead of the full editor.
"""

from __future__ import annotations

import os
import struct
import sys
from pathlib import Path

MAGIC = b"LaFlasherPayload"
_TRAILER = struct.Struct("<QB7s16s")  # payload length, xor key, reserved, MAGIC
# PyInstaller's archive cookie. If the package happened to contain it, the loader would
# find that copy first, so such payloads are stored xor-ed with a key that removes it.
_PYINSTALLER_COOKIE = b"MEI\x0c\x0b\x0a\x0b\x0e"
RUNTIME_NAME = "LaFlasher-standalone-runtime.exe"


def _xor(data: bytes, key: int) -> bytes:
    return data.translate(bytes(i ^ key for i in range(256))) if key else data


def read_payload(exe: str | Path) -> bytes | None:
    """The package appended to `exe`, or None if it has none."""
    try:
        with open(exe, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            if size < _TRAILER.size:
                return None
            f.seek(size - _TRAILER.size)
            length, key, _, magic = _TRAILER.unpack(f.read(_TRAILER.size))
            if magic != MAGIC or not 0 < length <= size - _TRAILER.size:
                return None
            f.seek(size - _TRAILER.size - length)
            return _xor(f.read(length), key)
    except OSError:
        return None


def embedded_payload() -> bytes | None:
    """The package of this exe when it runs as an exported standalone flasher."""
    if not getattr(sys, "frozen", False):
        return None
    return read_payload(sys.executable)


def _without_payload(data: bytes) -> bytes:
    if len(data) >= _TRAILER.size:
        length, _, _, magic = _TRAILER.unpack(data[-_TRAILER.size:])
        if magic == MAGIC and 0 < length <= len(data) - _TRAILER.size:
            return data[:len(data) - _TRAILER.size - length]
    return data


def is_onefile_build() -> bool:
    """True when running as PyInstaller's single-file exe (which can be its own runtime)."""
    meipass = getattr(sys, "_MEIPASS", None)
    if not getattr(sys, "frozen", False) or not meipass:
        return False
    return Path(meipass).resolve() != (Path(sys.executable).parent / "_internal").resolve()


def find_runtime() -> Path | None:
    """LaFlasher's single-file build, used as the base of exported flashers."""
    if getattr(sys, "frozen", False):
        exe = Path(sys.executable)
        candidates = ([exe] if is_onefile_build() else []) + [exe.parent / RUNTIME_NAME]
    else:  # running from source: use what build.py produced
        dist = Path(__file__).resolve().parent.parent / "dist"
        candidates = [dist / "LaFlasher" / RUNTIME_NAME, dist / "LaFlasher.exe"]
    return next((path for path in candidates if path.is_file()), None)


def export_file(package_path: str | Path, out: str | Path) -> int:
    """Command-line export: a saved .laflash (with its standalone options) -> flasher .exe."""
    import zipfile

    runtime = find_runtime()
    if runtime is None:
        raise FileNotFoundError(f"the standalone runtime ({RUNTIME_NAME}) is missing")
    if not zipfile.is_zipfile(package_path):
        raise ValueError(f"{package_path} is not a .laflash package")
    return write_standalone(runtime, Path(package_path).read_bytes(), out)


def write_standalone(runtime: str | Path, package: bytes, out: str | Path) -> int:
    """Write runtime + package to `out`. Returns the size of the new exe."""
    out = Path(out)
    base = _without_payload(Path(runtime).read_bytes())  # the runtime may itself be a flasher
    key, payload = 0, package
    while _PYINSTALLER_COOKIE in payload:
        key += 1
        payload = _xor(package, key)
    partial = out.with_name(out.name + ".part")
    with open(partial, "wb") as f:
        f.write(base)
        f.write(payload)
        f.write(_TRAILER.pack(len(payload), key, b"", MAGIC))
    os.replace(partial, out)
    return out.stat().st_size
