"""Embedded esptool: runs esptool in-process on a worker thread and reports to the UI."""

from __future__ import annotations

import contextlib
import copy
import gc
import io
import os
import tempfile
import traceback
from dataclasses import dataclass, field

import serial
from PyQt6.QtCore import QThread, pyqtSignal
from serial.tools import list_ports

import esptool
from esptool.cmds import attach_flash, detect_chip, detect_flash_size, reset_chip, run_stub
from esptool.logger import TemplateLogger, log
from esptool.util import FatalError

from . import chips, fsimage
from .fsimage import FsParams
from .package import BOOTLOADER
from .util import hex_addr, human_size

ROM_BAUD = 115200
BAUD_RATES = [115200, 230400, 460800, 921600, 1500000, 2000000]
DEFAULT_BAUD = 921600
CONNECT_ATTEMPTS = 7
# USB-UART bridges found on ESP32 boards, used to pick the right port automatically.
KNOWN_USB_VIDS = {0x303A: "Espressif", 0x10C4: "CP210x", 0x1A86: "CH340/CH9102", 0x0403: "FTDI",
                  0x067B: "Prolific", 0x2341: "Arduino", 0x239A: "Adafruit"}
_COMM_ERRORS = ("Timed out", "Invalid head of packet", "Packet content transfer stopped",
                "stopped responding", "MD5 of file does not match", "Lost connection",
                "Serial data stream stopped", "Write failed")


class Cancelled(BaseException):
    """BaseException so esptool's own `except Exception` handlers don't swallow it."""


class ChipMismatch(Exception):
    pass


@dataclass
class FsJob:
    source: str
    offset: int
    size: int
    label: str
    params: FsParams


@dataclass
class Job:
    kind: str                                 # flash | detect | erase | merge
    port: str = ""
    baud: int = DEFAULT_BAUD
    images: list[tuple] = field(default_factory=list)  # (offset | BOOTLOADER, path, label)
    fs: FsJob | None = None
    otadata: tuple[int, int] | None = None
    erase_all: bool = False
    reset_after: bool = True
    flash_mode: str = "keep"
    flash_freq: str = "keep"
    flash_size: str = "keep"
    expected_chip: str | None = None
    chip: str | None = None                   # merge only
    output: str = ""                          # merge only


def build_flash_job(pkg, port: str, baud: int, erase_all: bool = False,
                    reset_after: bool = True, kind: str = "flash") -> Job:
    """Everything the worker needs to write `pkg` (a LaFlashPackage) to a board."""
    job = Job(kind, port=port, baud=baud, erase_all=erase_all, reset_after=reset_after)
    job.images = [(img.offset, str(pkg.root / img.file), img.name) for img in pkg.images if img.enabled]
    if pkg.fs_will_flash():
        offset, size, label = pkg.fs_target()
        job.fs = FsJob(str(pkg.fs_dir), offset, size, label, copy.copy(pkg.fs_params))
    job.otadata = pkg.otadata_to_reset()
    job.flash_mode, job.flash_freq, job.flash_size = pkg.flash_mode, pkg.flash_freq, pkg.flash_size
    job.expected_chip = pkg.target_chip()
    return job


def list_serial_ports() -> list[tuple[str, str, bool]]:
    """(device, description, looks_like_esp32_board), likely boards first."""
    ports = []
    for p in list_ports.comports():
        if "bluetooth" in (p.description or "").lower() or (p.hwid or "").upper().startswith("BTHENUM"):
            continue  # never an ESP32 flashing port
        desc = (p.description or "").replace(f"({p.device})", "").strip() or p.device
        if desc == "n/a":
            desc = p.device
        ports.append((p.device, desc, p.vid in KNOWN_USB_VIDS))
    number = lambda dev: int("".join(c for c in dev if c.isdigit()) or 0)
    return sorted(ports, key=lambda p: (not p[2], number(p[0]), p[0]))


def _plain(text: str) -> str:
    """esptool messages are Rich markup; the log panel wants plain text."""
    if "[" not in text:
        return text
    try:
        from rich.text import Text
        return Text.from_markup(text).plain
    except Exception:
        return text


_active: "EspWorker | None" = None


class _GuiLogger(TemplateLogger):
    """Receives all esptool output and forwards it to the running worker."""

    def print(self, *args, **kwargs):
        if _active:
            _active._write(kwargs.get("sep", " ").join(str(a) for a in args) + kwargs.get("end", "\n"))

    def note(self, message):
        if _active:
            _active._line(f"Note: {message}", "info")

    def warning(self, message):
        if _active:
            _active._line(f"Warning: {message}", "warn")

    def error(self, message):
        if _active:
            _active._line(str(message), "error")

    def stage(self, finish=False):
        pass

    def progress_bar(self, cur_iter, total_iters, prefix="", suffix="", bar_length=30):
        if _active:
            _active._on_progress(cur_iter, total_iters, prefix)

    def set_verbosity(self, verbosity):
        pass


log.set_logger(_GuiLogger())


class _Stream(io.TextIOBase):
    """Catches anything printed directly (e.g. click usage errors)."""

    def __init__(self, worker: "EspWorker"):
        self.worker = worker

    def write(self, text):
        self.worker._write(text)
        return len(text)


def explain_error(exc: BaseException) -> tuple[str, str]:
    """(message, what the user can do about it)."""
    text = str(exc) or exc.__class__.__name__
    lower = text.lower()
    if isinstance(exc, StopIteration):
        text, lower = "The chip stopped responding.", "stopped responding"
    if isinstance(exc, SystemExit):
        return "esptool rejected the command (see the log).", ""
    if isinstance(exc, ChipMismatch):
        return text, "Connect the board this package was made for, or open the matching package."
    if isinstance(exc, fsimage.FsTooSmallError):
        return text, "Remove some files in the LittleFS tab or choose a bigger partition."
    if "filenotfounderror" in lower or "cannot find the file" in lower:
        return text, "The board was not found. Check the USB cable and pick the right port."
    if "access is denied" in lower or "permissionerror" in lower:
        return text, ("The serial port is in use by another program. Close the Arduino Serial "
                      "Monitor, PlatformIO monitor, PuTTY or any other app using it and try again.")
    if "could not open port" in lower or "cannot configure port" in lower:
        return text, ("Could not open the serial port. Check that the board is plugged in and that "
                      "no other program is using the port.")
    if "failed to connect" in lower or "no serial data received" in lower or "wrong boot mode" in lower:
        return text, ("The board did not enter download mode. Hold the BOOT button, tap RESET (EN), "
                      "release BOOT and click Flash again. Charge-only USB cables also cause this.")
    if "unexpected chip id in image" in lower or "different chip" in lower:
        return text, "This firmware was built for a different ESP32 model than the connected board."
    if "will not fit" in lower or "exceeds" in lower and "flash" in lower:
        return text, "The firmware is bigger than the board's flash chip."
    if any(s.lower() in lower for s in _COMM_ERRORS):
        return text, "The connection was interrupted. Try a lower baud rate or another USB cable/port."
    if "secure boot" in lower or "encryption" in lower:
        return text, "The chip has security features enabled that block normal flashing."
    return text, ""


def _is_comm_error(exc: BaseException) -> bool:
    if isinstance(exc, StopIteration):
        return True
    if isinstance(exc, serial.SerialException):
        return "could not open port" not in str(exc).lower()
    return isinstance(exc, FatalError) and any(s in str(exc) for s in _COMM_ERRORS)


def _close(esp) -> None:
    with contextlib.suppress(Exception):
        esp._port.close()


class EspWorker(QThread):
    log_line = pyqtSignal(str, str)      # text, level: info | warn | error | ok
    progress = pyqtSignal(int, int)      # value, maximum (maximum 0 = busy)
    status = pyqtSignal(str)
    device_info = pyqtSignal(dict)
    done = pyqtSignal(bool, str, str)    # success, message, hint

    def __init__(self, job: Job, parent=None):
        super().__init__(parent)
        self.job = job
        self._cancel = False
        self._cancel_raised = False
        self._partial = ""
        self._temp_files: list[str] = []
        self._writing: dict | None = None
        self._result = "Done."
        self.written_images = 0   # filled in by a successful flash, for the success popup
        self.written_bytes = 0

    def cancel(self) -> None:
        self._cancel = True

    # ---------------------------------------------------------------- plumbing
    def _check_cancel(self) -> None:
        if self._cancel and not self._cancel_raised:
            self._cancel_raised = True
            raise Cancelled()

    def _write(self, text: str) -> None:
        self._check_cancel()
        self._partial += _plain(text).replace("\r", "")
        while "\n" in self._partial:
            line, self._partial = self._partial.split("\n", 1)
            self._line(line)
        if self._partial.strip() and not self._writing:
            self.status.emit(self._partial.strip())

    def _line(self, line: str, level: str | None = None) -> None:
        line = _plain(line).rstrip()
        if not line.strip():
            return
        if level is None:
            lowered = line.lower()
            level = ("error" if lowered.startswith(("a fatal error", "error")) else
                     "warn" if lowered.startswith("warning") else "info")
        self.log_line.emit(line, level)
        if not self._writing and len(line) < 120:
            self.status.emit(line.strip())

    def _flush(self) -> None:
        if self._partial.strip():
            self.log_line.emit(self._partial.strip(), "info")
        self._partial = ""

    def _temp_file(self, data: bytes, prefix: str) -> str:
        fd, path = tempfile.mkstemp(prefix=prefix, suffix=".bin")
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        self._temp_files.append(path)
        return path

    def run(self) -> None:
        global _active
        _active = self
        stream = _Stream(self)
        try:
            with contextlib.redirect_stdout(stream), contextlib.redirect_stderr(stream):
                getattr(self, f"_run_{self.job.kind}")()
            self._flush()
            self.done.emit(True, self._result, "")
        except Cancelled:
            self._flush()
            self.done.emit(False, "Cancelled.", "")
        except BaseException as exc:  # the UI must always get an answer
            self._flush()
            message, hint = explain_error(exc)
            expected = (FatalError, serial.SerialException, ChipMismatch, fsimage.FsBuildError,
                        OSError, StopIteration, SystemExit, ValueError)
            if not isinstance(exc, expected):
                self.log_line.emit(traceback.format_exc().rstrip(), "error")
            self.done.emit(False, message, hint)
        finally:
            _active = None
            for path in self._temp_files:
                with contextlib.suppress(OSError):
                    os.unlink(path)
            gc.collect()  # releases serial handles held by failed connection attempts

    # -------------------------------------------------------------- operations
    def _connect(self):
        self.status.emit(f"Connecting to {self.job.port}...")
        self.progress.emit(0, 0)
        esp = detect_chip(self.job.port, baud=ROM_BAUD, connect_mode="default-reset",
                          connect_attempts=CONNECT_ATTEMPTS)
        return esp, chips.key_from_chip_name(esp.CHIP_NAME)

    def _base_args(self, baud: int) -> list[str]:
        return ["--port", self.job.port, "--baud", str(baud),
                "--after", "hard-reset" if self.job.reset_after else "no-reset"]

    def _flash_args(self) -> list[str]:
        job = self.job
        return ["--flash-mode", job.flash_mode, "--flash-freq", job.flash_freq,
                "--flash-size", job.flash_size]

    def _items_with_fs(self) -> list[tuple]:
        items = list(self.job.images)
        fs = self.job.fs
        if fs:
            self.status.emit("Building LittleFS image...")
            result = fsimage.build(fs.source, fs.size, fs.params)
            path = self._temp_file(result.data, "littlefs_")
            self._line(f"Built LittleFS image for partition '{fs.label}': {result.files} files, "
                       f"{human_size(result.used)} of {human_size(result.total)} used.", "info")
            for warning in result.warnings:
                self._line(f"Warning: {warning}", "warn")
            items.append((fs.offset, path, f"LittleFS ({fs.label})"))
        return items

    def _resolve(self, items: list[tuple], bootloader_offset: int) -> list[tuple[int, str, str]]:
        plan = [(bootloader_offset if off == BOOTLOADER else off, path, label)
                for off, path, label in items]
        if self.job.otadata:
            off, size = self.job.otadata
            if not any(o < off + size and off < o + os.path.getsize(p) for o, p, _ in plan):
                plan.append((off, self._temp_file(b"\xff" * size, "otadata_"), "OTA data (reset)"))
                self._line("No OTA data image in the package: clearing the otadata partition so "
                           "the new app starts.", "info")
        plan.sort(key=lambda item: item[0])
        for (a, pa, na), (b, _, nb) in zip(plan, plan[1:]):
            if a + os.path.getsize(pa) > b:
                raise ValueError(f"{na} at {hex_addr(a)} overlaps {nb} at {hex_addr(b)}.")
        return plan

    def _start_progress(self, plan) -> None:
        sizes = [max(os.path.getsize(p), 1) for _, p, _ in plan]
        self._writing = {"labels": [label for *_, label in plan], "sizes": sizes,
                         "total": sum(sizes), "index": 0, "before": 0, "file_done": False}
        for off, path, label in plan:
            self._line(f"  {hex_addr(off):>10}  {label}  ({human_size(os.path.getsize(path))})", "info")

    def _on_progress(self, cur: int, total: int, prefix: str) -> None:
        self._check_cancel()
        w = self._writing
        if not w or not prefix.startswith("Writing"):
            if total:
                self.progress.emit(int(cur * 1000 / total), 1000)
            return
        if w["file_done"] and cur < total:  # esptool moved on to the next file
            w["before"] += w["sizes"][w["index"]]
            w["index"] = min(w["index"] + 1, len(w["sizes"]) - 1)
            w["file_done"] = False
        i = w["index"]
        fraction = cur / total if total else 1.0
        self.progress.emit(int((w["before"] + fraction * w["sizes"][i]) * 1000 / w["total"]), 1000)
        self.status.emit(f"Writing {w['labels'][i]} ({i + 1}/{len(w['sizes'])}) - {fraction:.0%}")
        if cur >= total:
            w["file_done"] = True

    def _run_flash(self) -> None:
        job = self.job
        items = self._items_with_fs()
        bauds = [job.baud] + [b for b in (460800, ROM_BAUD) if b < job.baud]
        for attempt, baud in enumerate(bauds):
            esp, chip = self._connect()
            handed_over = False
            try:
                if job.expected_chip and chip and job.expected_chip != chip:
                    raise ChipMismatch(f"This package is for {chips.chip_label(job.expected_chip)}, "
                                       f"but the connected board is {esp.CHIP_NAME}.")
                self.device_info.emit(self._chip_info(esp, chip))
                plan = self._resolve(items, int(getattr(esp, "BOOTLOADER_FLASH_OFFSET", 0)))
                argv = self._base_args(baud) + ["write-flash"] + self._flash_args()
                if job.erase_all:
                    argv.append("--erase-all")
                for off, path, _ in plan:
                    argv += [hex_addr(off), path]
                self._line(f"Writing {len(plan)} image(s) to {esp.CHIP_NAME} at {baud} baud:", "info")
                self._start_progress(plan)
                handed_over = True  # esptool resets the chip itself when it finishes
                esptool.main(argv, esp=esp)
                self.progress.emit(1000, 1000)
                self.written_images = len(plan)
                self.written_bytes = sum(os.path.getsize(path) for _, path, _ in plan)
                self._result = (f"Flashed {esp.CHIP_NAME} successfully." +
                                (" The board has been restarted." if job.reset_after else ""))
                return
            except Exception as exc:
                if attempt + 1 < len(bauds) and _is_comm_error(exc) and not self._cancel:
                    self._line(f"Transfer failed at {baud} baud ({exc}). "
                               f"Retrying at {bauds[attempt + 1]} baud...", "warn")
                    continue
                raise
            finally:
                self._writing = None
                if not handed_over:
                    with contextlib.suppress(BaseException):
                        reset_chip(esp, "hard-reset")
                _close(esp)

    @staticmethod
    def _chip_info(esp, chip: str | None) -> dict:
        """Chip description, features, crystal and MAC (each best-effort)."""
        info = {"chip": chip, "name": esp.CHIP_NAME}
        for key, getter in (("description", esp.get_chip_description),
                            ("features", lambda: ", ".join(esp.get_chip_features())),
                            ("crystal", lambda: f"{esp.get_crystal_freq()} MHz"),
                            ("mac", lambda: ":".join(f"{b:02x}" for b in esp.read_mac()))):
            with contextlib.suppress(Exception):
                info[key] = getter()
        return info

    def _run_detect(self) -> None:
        esp, chip = self._connect()
        info = self._chip_info(esp, chip)
        try:
            if not esp.secure_download_mode:
                esp = run_stub(esp)
                attach_flash(esp)
                with contextlib.suppress(Exception):
                    info["flash_size"] = detect_flash_size(esp)
            self.device_info.emit(info)
            self._result = (f"Found {info.get('description', esp.CHIP_NAME)}"
                            + (f" with {info['flash_size']} flash" if info.get("flash_size") else "") + ".")
        finally:
            with contextlib.suppress(BaseException):
                reset_chip(esp, "hard-reset")
            _close(esp)

    def _run_erase(self) -> None:
        esp, _ = self._connect()
        try:
            esptool.main(self._base_args(self.job.baud) + ["erase-flash"], esp=esp)
        finally:
            _close(esp)
        self._result = f"Erased the flash of {esp.CHIP_NAME}."

    def _run_merge(self) -> None:
        job = self.job
        self.progress.emit(0, 0)
        plan = self._resolve(self._items_with_fs(), chips.bootloader_offset(job.chip))
        argv = ["--chip", job.chip, "merge-bin", "-o", job.output] + self._flash_args()
        for off, path, _ in plan:
            argv += [hex_addr(off), path]
        esptool.main(argv)
        self._result = f"Saved merged image ({human_size(os.path.getsize(job.output))}) - flash it at 0x0."
