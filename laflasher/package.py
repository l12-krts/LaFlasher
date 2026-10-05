"""The .laflash package format.

A .laflash file is a renamed ZIP archive:

    laflash.json      optional metadata (name, chip, offsets, LittleFS settings)
    *.bin             bootloader, partition table, app, OTA data, other images
    partitions.csv    optional partition table source (used if no partitions.bin)
    littlefs/         files that become the LittleFS partition

The package is extracted into a private working folder; every edit happens there and
"Save" zips that folder back up.
"""

from __future__ import annotations

import io
import json
import os
import re
import shutil
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from . import __version__, chips, fsimage, partitions
from .fsimage import FsParams
from .partitions import Partition
from .util import hex_addr, human_size, parse_int

EXTENSION = ".laflash"
MANIFEST = "laflash.json"
MANIFEST_ALIASES = (MANIFEST, "manifest.json")
FS_FOLDER = "littlefs"
BOOTLOADER = "bootloader"  # offset placeholder: 0x0 / 0x1000 / 0x2000 depending on chip
DEFAULT_OTADATA_OFFSET = 0xE000
DEFAULT_APP_OFFSET = 0x10000

ROLE_BOOTLOADER = "bootloader"
ROLE_PARTITIONS = "partition-table"
ROLE_OTADATA = "otadata"
ROLE_APP = "app"
ROLE_MERGED = "merged"
ROLE_FS = "filesystem"
ROLE_DATA = "data"
ROLE_LABELS = {
    ROLE_BOOTLOADER: "Bootloader",
    ROLE_PARTITIONS: "Partition table",
    ROLE_OTADATA: "OTA data",
    ROLE_APP: "Application",
    ROLE_MERGED: "Merged image",
    ROLE_FS: "Filesystem image",
    ROLE_DATA: "Data",
}
_KNOWN_KEYS = {"format", "name", "version", "description", "chip", "flash", "images",
               "partition_table", "littlefs", "standalone", "created_with"}
# File names that are probably the device's settings (shown as a form in standalone flashers).
_SETTINGS_NAME = re.compile(r"config|setting|setup|wifi|wlan|secret|credential|device|param|pref|option",
                            re.I)
_OFFSET_IN_NAME = re.compile(r"(?<![0-9a-z])0x([0-9a-f]{1,8})(?![0-9a-z])", re.I)


@dataclass
class FlashImage:
    file: str                    # posix path inside the package
    offset: int | str | None     # int, BOOTLOADER, or None when unknown
    role: str = ROLE_APP
    enabled: bool = True
    auto: bool = False           # offset was derived from the partition table

    @property
    def name(self) -> str:
        return PurePosixPath(self.file).name

    def offset_text(self, chip: str | None = None) -> str:
        if self.offset == BOOTLOADER:
            if chip:
                return f"{hex_addr(chips.bootloader_offset(chip))} ({chips.chip_label(chip)})"
            return "auto (depends on chip)"
        return hex_addr(self.offset) if isinstance(self.offset, int) else "not set"


def parse_offset(value) -> int | str | None:
    if value is None or str(value).strip() == "":
        return None
    text = str(value).strip().lower()
    if text == BOOTLOADER:
        return BOOTLOADER
    if text in ("auto", "?", "not set"):
        return None
    return parse_int(text)


def guess_image(filename: str) -> tuple[str, int | str | None]:
    """Work out an image's role (and maybe offset) from its file name."""
    n = PurePosixPath(filename).name.lower()
    if "bootloader" in n:
        role, offset = ROLE_BOOTLOADER, BOOTLOADER
    elif "partition" in n:
        role, offset = ROLE_PARTITIONS, partitions.TABLE_OFFSET
    elif any(k in n for k in ("boot_app0", "otadata", "ota_data")):
        role, offset = ROLE_OTADATA, None
    elif "merged" in n or re.search(r"[._-]factory\.bin$", n):
        role, offset = ROLE_MERGED, 0
    elif any(k in n for k in ("littlefs", "spiffs")) or n in ("fs.bin", "filesystem.bin"):
        role, offset = ROLE_FS, None
    elif "nvs" in n:
        role, offset = ROLE_DATA, None
    else:
        role, offset = ROLE_APP, None
    explicit = _OFFSET_IN_NAME.search(n)
    if explicit:
        offset = int(explicit.group(1), 16)
    return role, offset


def _safe_rel(path: str) -> str:
    rel = PurePosixPath(str(path).replace("\\", "/"))
    if rel.is_absolute() or ".." in rel.parts or not rel.parts:
        raise ValueError(f"invalid path in package: {path}")
    return rel.as_posix()


def _safe_extract(zf: zipfile.ZipFile, dest: Path) -> None:
    for info in zf.infolist():
        name = info.filename.replace("\\", "/")
        if name.startswith("__MACOSX/") or name.endswith("/.DS_Store"):
            continue
        rel = _safe_rel(name.rstrip("/")) if name.rstrip("/") else None
        if rel is None:
            continue
        target = dest / rel
        if info.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        with zf.open(info) as src, open(target, "wb") as out:
            shutil.copyfileobj(src, out)


def _content_root(folder: Path) -> Path:
    """Packages zipped from a parent folder have everything inside one directory."""
    while True:
        entries = [p for p in folder.iterdir()]
        if (len(entries) == 1 and entries[0].is_dir()
                and entries[0].name.lower() != FS_FOLDER):
            folder = entries[0]
        else:
            return folder


def find_build_dirs(folder: Path) -> list[Path]:
    """Folders with compiled firmware: the folder itself, Arduino IDE exports (build/<board>),
    PlatformIO (.pio/build/<env>) or ESP-IDF (build/ with flasher_args.json)."""
    def usable(d: Path) -> bool:
        return d.is_dir() and ((d / "flasher_args.json").is_file() or any(d.glob("*.bin")))

    found: list[Path] = []
    for d in [folder, folder / "build", *sorted(folder.glob("build/*")),
              *sorted(folder.glob(".pio/build/*"))]:
        if usable(d) and d not in found:
            found.append(d)
    return found


def find_fs_source(project: Path, build_dir: Path) -> Path | None:
    """The folder Arduino/PlatformIO upload as the filesystem: data/ (or littlefs/)."""
    for base in [project, *list(build_dir.parents)[:3]]:
        for name in (FS_FOLDER, "data"):
            if (base / name).is_dir():
                return base / name
    return None


def project_name(folder: Path) -> str:
    parts = folder.parts
    for marker in (".pio", "build"):
        if marker in parts and parts.index(marker) > 0:
            return parts[parts.index(marker) - 1]
    return folder.name or "Untitled"


class LaFlashPackage:
    def __init__(self, root: Path, workdir: Path):
        self.root = root              # package contents
        self._workdir = workdir       # temp folder deleted on close()
        self.path: Path | None = None
        self.name = ""
        self.version = ""
        self.description = ""
        self.chip = ""                # "" = any chip
        self.flash_mode = "keep"
        self.flash_freq = "keep"
        self.flash_size = "keep"
        self.baud: int | None = None
        self.images: list[FlashImage] = []
        self.partition_file: str | None = None
        self.partitions: list[Partition] = []
        self.partition_error = ""
        self.fs_folder = FS_FOLDER
        self.fs_enabled = True
        self.fs_partition: str | None = None          # partition label, None = automatic
        self.fs_custom: tuple[int, int] | None = None  # manual (offset, size)
        self.fs_params = FsParams()
        # Options for exported standalone flashers: title, settings_file, allow_files, allow_editor
        self.standalone: dict = {}
        self.notes: list[str] = []
        self._extra: dict = {}

    # ---------------------------------------------------------------- creation
    @classmethod
    def new(cls, name: str = "Untitled") -> "LaFlashPackage":
        workdir = Path(tempfile.mkdtemp(prefix="laflash_"))
        pkg = cls(workdir, workdir)
        pkg.name = name
        return pkg

    @classmethod
    def _from_zip(cls, source, label: str, default_name: str) -> "LaFlashPackage":
        workdir = Path(tempfile.mkdtemp(prefix="laflash_"))
        try:
            try:
                with zipfile.ZipFile(source) as zf:
                    _safe_extract(zf, workdir)
            except zipfile.BadZipFile:
                raise ValueError(f"'{label}' is not a valid .laflash package "
                                 "(it must be a ZIP archive).") from None
            pkg = cls(_content_root(workdir), workdir)
            pkg._load(default_name=default_name)
            return pkg
        except Exception:
            shutil.rmtree(workdir, ignore_errors=True)
            raise

    @classmethod
    def open(cls, path: str | Path) -> "LaFlashPackage":
        path = Path(path)
        pkg = cls._from_zip(path, path.name, path.stem)
        pkg.path = path
        return pkg

    @classmethod
    def from_bytes(cls, data: bytes, name: str = "Firmware") -> "LaFlashPackage":
        """Open a package held in memory, e.g. the one inside a standalone flasher."""
        return cls._from_zip(io.BytesIO(data), name, name)

    @classmethod
    def from_files(cls, name: str, images: list[Path], partition_csv: Path | None = None,
                   fs_source: Path | None = None) -> "LaFlashPackage":
        """Build a new package from compiler output (Arduino / PlatformIO / ESP-IDF)."""
        pkg = cls.new(name)
        try:
            for src in images:
                shutil.copy2(src, pkg.root / pkg._unique_name(src.name))
            if partition_csv:
                shutil.copy2(partition_csv, pkg.root / pkg._unique_name(partition_csv.name))
            if fs_source and fs_source.is_dir():
                shutil.copytree(fs_source, pkg.fs_dir, ignore=shutil.ignore_patterns(".DS_Store"))
            pkg._load(default_name=name)
        except Exception:
            pkg.close()
            raise
        return pkg

    @classmethod
    def from_build_dir(cls, build_dir: Path, name: str, fs_source: Path | None) -> "LaFlashPackage":
        """Package an Arduino IDE export, a PlatformIO .pio/build/<env> or an ESP-IDF build/."""
        args_file = build_dir / "flasher_args.json"
        if not args_file.is_file():
            return cls.from_files(name, sorted(build_dir.glob("*.bin")), None, fs_source)
        # ESP-IDF writes the exact offsets and settings it flashes with.
        args = json.loads(args_file.read_text(encoding="utf-8"))
        pkg = cls.new(name)
        try:
            images = []
            for offset, rel in (args.get("flash_files") or {}).items():
                src = build_dir / rel
                if src.is_file():
                    dest = pkg._unique_name(src.name)
                    shutil.copy2(src, pkg.root / dest)
                    images.append({"file": dest, "offset": offset})
            settings = args.get("flash_settings") or {}
            manifest = {
                "name": name,
                "chip": (args.get("extra_esptool_args") or {}).get("chip", ""),
                "flash": {"mode": settings.get("flash_mode", "keep"),
                          "freq": settings.get("flash_freq", "keep"),
                          "size": settings.get("flash_size", "keep")},
                "images": images,
            }
            (pkg.root / MANIFEST).write_text(json.dumps(manifest, indent=2), encoding="utf-8")
            if fs_source and fs_source.is_dir():
                shutil.copytree(fs_source, pkg.fs_dir, ignore=shutil.ignore_patterns(".DS_Store"))
            pkg._load(default_name=name)
        except Exception:
            pkg.close()
            raise
        return pkg

    def close(self) -> None:
        shutil.rmtree(self._workdir, ignore_errors=True)

    # ------------------------------------------------------------------ loading
    def _read_manifest(self) -> dict:
        for alias in MANIFEST_ALIASES:
            path = self.root / alias
            if path.is_file():
                try:
                    data = json.loads(path.read_text(encoding="utf-8-sig"))
                except ValueError as exc:
                    raise ValueError(f"{alias} is not valid JSON: {exc}") from None
                if not isinstance(data, dict):
                    raise ValueError(f"{alias} must contain a JSON object")
                return data
        return {}

    def _load(self, default_name: str) -> None:
        manifest = self._read_manifest()
        self._extra = {k: v for k, v in manifest.items() if k not in _KNOWN_KEYS}
        self.name = str(manifest.get("name") or default_name)
        self.version = str(manifest.get("version") or "")
        self.description = str(manifest.get("description") or "")
        chip = str(manifest.get("chip") or "").strip().lower().replace("-", "")
        self.chip = chip if chip in chips.chip_keys() else ""
        standalone = manifest.get("standalone")
        self.standalone = dict(standalone) if isinstance(standalone, dict) else {}

        flash = manifest.get("flash") or {}
        self.flash_mode = str(flash.get("mode", "keep"))
        self.flash_freq = str(flash.get("freq", "keep"))
        self.flash_size = str(flash.get("size", "keep"))
        self.baud = int(flash["baud"]) if str(flash.get("baud", "")).isdigit() else None

        fs = manifest.get("littlefs") or {}
        self.fs_folder = _safe_rel(fs.get("folder") or FS_FOLDER)
        self.fs_enabled = bool(fs.get("enabled", True))
        self.fs_partition = fs.get("partition") or None
        if fs.get("offset") not in (None, "") and fs.get("size") not in (None, ""):
            self.fs_custom = (parse_int(fs["offset"]), parse_int(fs["size"]))
        self.fs_params = FsParams.from_dict(fs)

        entries = manifest.get("images")
        if isinstance(entries, list):
            for entry in entries:
                entry = {"file": entry} if isinstance(entry, str) else dict(entry)
                rel = _safe_rel(entry.get("file", ""))
                if not (self.root / rel).is_file():
                    self.notes.append(f"Missing image listed in {MANIFEST}: {rel}")
                    continue
                role, guessed = guess_image(rel)
                offset = parse_offset(entry["offset"]) if "offset" in entry else guessed
                self.images.append(FlashImage(rel, offset, entry.get("role") or role,
                                              bool(entry.get("enabled", True))))
        else:
            for path in sorted(self.root.rglob("*.bin"), key=lambda p: str(p).lower()):
                rel = path.relative_to(self.root).as_posix()
                if rel.lower().startswith(self.fs_folder.lower() + "/"):
                    continue
                role, offset = guess_image(rel)
                self.images.append(FlashImage(rel, offset, role))
            separate = any(i.role in (ROLE_APP, ROLE_BOOTLOADER) for i in self.images)
            for img in self.images:
                if img.role == ROLE_MERGED and separate:
                    img.enabled = False
                    self.notes.append(f"{img.name} is a merged image; the separate images "
                                      "will be flashed instead (tick it to use it).")

        table = manifest.get("partition_table")
        if table and (self.root / _safe_rel(table)).is_file():
            self.partition_file = _safe_rel(table)
        else:
            self.partition_file = self._find_partition_source()
        self.reload_partitions()
        for img in self.images:  # e.g. ESP-IDF's storage.bin written to a littlefs partition
            part = self.partition_at(img.offset)
            if part and part.is_filesystem and img.role in (ROLE_APP, ROLE_DATA):
                img.role = ROLE_FS
        self._unpack_fs_image()
        if not self.fs_dir.is_dir():
            # No LittleFS content: never wipe the board's files with an empty image by default.
            self.fs_enabled = False
        self.resolve_offsets()

    def _find_partition_source(self) -> str | None:
        img = next((i for i in self.images if i.role == ROLE_PARTITIONS), None)
        if img:
            return img.file
        csvs = sorted(self.root.glob("*.csv"), key=lambda p: ("partition" not in p.name.lower(), p.name))
        return csvs[0].relative_to(self.root).as_posix() if csvs else None

    def reload_partitions(self) -> None:
        self.partitions, self.partition_error = [], ""
        if not self.partition_file:
            return
        path = self.root / self.partition_file
        try:
            self.partitions = partitions.load(path)
        except (OSError, ValueError) as exc:
            self.partition_error = f"{self.partition_file}: {exc}"
            return
        has_binary = any(i.role == ROLE_PARTITIONS and not i.file.lower().endswith(".csv")
                         for i in self.images)
        if path.suffix.lower() == ".csv" and not has_binary:
            rel = self._unique_name("partitions.bin")
            (self.root / rel).write_bytes(partitions.to_binary(self.partitions))
            self.images.append(FlashImage(rel, partitions.TABLE_OFFSET, ROLE_PARTITIONS))
            self.notes.append(f"Generated {rel} from {self.partition_file}.")

    def _unpack_fs_image(self) -> None:
        """Turn a prebuilt littlefs.bin into editable files when there is no littlefs/ folder."""
        img = next((i for i in self.images if i.role == ROLE_FS), None)
        if img is None or self.fs_dir.exists():
            return
        data = (self.root / img.file).read_bytes()
        staging = Path(tempfile.mkdtemp(prefix="lfs_", dir=self._workdir))
        try:
            count = fsimage.extract(data, staging, self.fs_params.block_size)
        except Exception as exc:  # not LittleFS (e.g. SPIFFS) - keep it as a raw image
            shutil.rmtree(staging, ignore_errors=True)
            self.notes.append(f"{img.name} could not be opened as LittleFS ({exc}); "
                              "it will be flashed as-is.")
            return
        shutil.move(str(staging), str(self.fs_dir))
        part = self.partition_at(img.offset)
        if part and part.is_filesystem:
            self.fs_partition = part.name
        elif self.fs_target() is None and isinstance(img.offset, int):
            self.fs_custom = (img.offset, len(data))
        self.images.remove(img)
        (self.root / img.file).unlink(missing_ok=True)
        self.notes.append(f"Unpacked {count} files from {img.name} into the LittleFS editor.")

    # ----------------------------------------------------------------- offsets
    def resolve_offsets(self) -> None:
        app_taken = any(i.role == ROLE_APP and isinstance(i.offset, int) and not i.auto
                        for i in self.images)
        for img in self.images:
            if img.offset is not None and not img.auto:
                continue
            offset = self._infer_offset(img, app_taken)
            if img.role == ROLE_APP and offset is not None and not self._label_match(img):
                app_taken = True
            img.offset, img.auto = offset, offset is not None
            if offset is None and img.enabled:
                img.enabled = False
                self.notes.append(f"Don't know where to flash {img.name} - set its offset "
                                  "in the Firmware tab to include it.")

    def _label_match(self, img: FlashImage) -> Partition | None:
        stem = img.name.lower().split(".")[0]
        return next((p for p in self.partitions if p.name.lower() == stem), None)

    def _infer_offset(self, img: FlashImage, app_taken: bool) -> int | str | None:
        if img.role == ROLE_BOOTLOADER:
            return BOOTLOADER
        if img.role == ROLE_PARTITIONS:
            return partitions.TABLE_OFFSET
        if img.role == ROLE_MERGED:
            return 0
        if img.role == ROLE_OTADATA:
            p = next((p for p in self.partitions if p.is_otadata), None)
            return p.offset if p else (DEFAULT_OTADATA_OFFSET if not self.partitions else None)
        if img.role == ROLE_FS:
            target = self.fs_target()
            return target[0] if target else None
        match = self._label_match(img)
        if match:
            return match.offset
        if img.role == ROLE_DATA and "nvs" in img.name.lower():
            p = next((p for p in self.partitions if p.type == 1 and p.subtype == 2), None)
            return p.offset if p else None
        if img.role == ROLE_APP and not app_taken:
            app = self.app_partition()
            return app.offset if app else (DEFAULT_APP_OFFSET if not self.partitions else None)
        return None

    def app_partition(self) -> Partition | None:
        apps = [p for p in self.partitions if p.is_app]
        factory = next((p for p in apps if p.subtype == 0), None)
        return factory or min(apps, key=lambda p: p.offset, default=None)

    def partition_at(self, offset) -> Partition | None:
        if not isinstance(offset, int):
            return None
        return next((p for p in self.partitions if p.offset == offset), None)

    # -------------------------------------------------------------- filesystem
    @property
    def fs_dir(self) -> Path:
        return self.root / self.fs_folder

    def fs_candidates(self) -> list[Partition]:
        return [p for p in self.partitions if p.is_filesystem]

    def fs_target(self) -> tuple[int, int, str] | None:
        """(offset, size, label) of the partition the LittleFS image goes to."""
        if self.fs_custom:
            return self.fs_custom[0], self.fs_custom[1], "custom"
        candidates = self.fs_candidates()
        chosen = next((p for p in candidates if p.name == self.fs_partition), None)
        if chosen is None:
            def rank(p: Partition):
                return (p.name.lower() != "littlefs", p.subtype != 0x83,
                        p.name.lower() != "spiffs", p.offset)
            chosen = min(candidates, key=rank, default=None)
        return (chosen.offset, chosen.size, chosen.name) if chosen else None

    def fs_file_count(self) -> int:
        return sum(1 for _, _, is_dir in fsimage.iter_source(self.fs_dir) if not is_dir)

    def fs_has_content(self) -> bool:
        return self.fs_dir.is_dir() and any(self.fs_dir.iterdir())

    def fs_will_flash(self) -> bool:
        return self.fs_enabled and self.fs_dir.is_dir() and self.fs_target() is not None

    def fs_total_size(self) -> int:
        return sum(path.stat().st_size for _, path, is_dir in fsimage.iter_source(self.fs_dir)
                   if not is_dir)

    # --------------------------------------------------------- device settings
    def settings_candidates(self) -> list[str]:
        """LittleFS files that could be shown as a settings form, most likely first."""
        from .settings_doc import MAX_SETTINGS_SIZE, SETTINGS_EXTENSIONS
        found = [rel for rel, path, is_dir in fsimage.iter_source(self.fs_dir)
                 if not is_dir and path.suffix.lower() in SETTINGS_EXTENSIONS
                 and path.stat().st_size <= MAX_SETTINGS_SIZE]

        def rank(rel: str):
            name = PurePosixPath(rel).name
            return (not _SETTINGS_NAME.search(name), rel.count("/"),
                    PurePosixPath(rel).suffix.lower() != ".json", rel.lower())
        return sorted(found, key=rank)

    def settings_file(self, options: dict | None = None) -> str | None:
        """The settings file a standalone flasher shows: the chosen one, else a likely one.
        `options` defaults to the package's own standalone options."""
        options = self.standalone if options is None else options
        if "settings_file" in options:
            rel = options.get("settings_file") or None  # "" / null = show none
            return rel if rel and (self.fs_dir / rel).is_file() else None
        likely = [rel for rel in self.settings_candidates()
                  if _SETTINGS_NAME.search(PurePosixPath(rel).name)]
        return likely[0] if likely else None

    # ------------------------------------------------------------ image edits
    def _unique_name(self, name: str) -> str:
        candidate, stem, suffix, n = name, Path(name).stem, Path(name).suffix, 2
        while (self.root / candidate).exists():
            candidate, n = f"{stem} ({n}){suffix}", n + 1
        return candidate

    def image_named(self, name: str) -> FlashImage | None:
        return next((i for i in self.images if i.name.lower() == name.lower()), None)

    def add_image(self, src: str | Path) -> FlashImage | None:
        """Copy a file into the package. A .csv becomes the partition table source."""
        src = Path(src)
        rel = self._unique_name(src.name)
        shutil.copy2(src, self.root / rel)
        if src.suffix.lower() == ".csv":
            for old in [i for i in self.images if i.role == ROLE_PARTITIONS]:
                self.images.remove(old)
                (self.root / old.file).unlink(missing_ok=True)
            self.partition_file = rel
            self._partitions_changed()
            return None
        role, offset = guess_image(src.name)
        img = FlashImage(rel, offset, role)
        self.images.append(img)
        if role == ROLE_PARTITIONS:
            self.partition_file = rel
            self._partitions_changed()
        else:
            self.resolve_offsets()
        return img

    def replace_image(self, img: FlashImage, src: str | Path) -> None:
        shutil.copyfile(src, self.root / img.file)
        if img.role == ROLE_PARTITIONS:
            self.partition_file = img.file
            self._partitions_changed()

    def remove_image(self, img: FlashImage) -> None:
        self.images.remove(img)
        (self.root / img.file).unlink(missing_ok=True)
        if img.file == self.partition_file:
            self.partition_file = self._find_partition_source()
            self._partitions_changed()

    def set_image_offset(self, img: FlashImage, text: str) -> None:
        img.offset = parse_offset(text)
        img.auto = False
        if img.offset is None:
            img.enabled = False

    def _partitions_changed(self) -> None:
        self.reload_partitions()
        for img in self.images:
            if img.auto:
                img.offset = None
        if self.fs_partition and not any(p.name == self.fs_partition for p in self.fs_candidates()):
            self.fs_partition = None
        self.resolve_offsets()

    # ------------------------------------------------------------------ chips
    def detected_chip(self) -> str | None:
        """Chip the firmware was compiled for, read from the image headers."""
        order = {ROLE_APP: 0, ROLE_BOOTLOADER: 1, ROLE_MERGED: 2}
        for img in sorted(self.images, key=lambda i: order.get(i.role, 9)):
            if img.role in order:
                chip = chips.chip_from_image(self.root / img.file)
                if chip:
                    return chip
        return None

    def target_chip(self) -> str | None:
        return self.chip or self.detected_chip()

    def bootloader_flash_size(self) -> str | None:
        img = next((i for i in self.images if i.role in (ROLE_BOOTLOADER, ROLE_MERGED)), None)
        return chips.flash_size_from_image(self.root / img.file) if img else None

    # ----------------------------------------------------------------- checks
    def resolved_plan(self, chip: str | None) -> list[tuple[int | None, int, str]]:
        """(offset, size, label) of everything that will be written, for overlap checks."""
        plan = []
        for img in self.images:
            if not img.enabled:
                continue
            size = (self.root / img.file).stat().st_size if (self.root / img.file).exists() else 0
            offset = img.offset
            if offset == BOOTLOADER:
                offset = chips.bootloader_offset(chip) if chip else None
            plan.append((offset, size, img.name))
        if self.fs_will_flash():
            offset, size, label = self.fs_target()
            plan.append((offset, size, f"LittleFS ({label})"))
        return plan

    def problems(self, chip: str | None = None) -> tuple[list[str], list[str]]:
        """Blocking errors and warnings for flashing."""
        errors, warnings = [], []
        chip = chip or self.target_chip()
        for img in self.images:
            if not img.enabled:
                continue
            if not (self.root / img.file).is_file():
                errors.append(f"{img.name} is missing from the package.")
            elif img.offset is None:
                errors.append(f"{img.name} has no flash offset.")
            elif isinstance(img.offset, int):
                size = (self.root / img.file).stat().st_size
                part = self.partition_at(img.offset)
                if part and size > part.size:
                    errors.append(f"{img.name} ({human_size(size)}) is larger than partition "
                                  f"'{part.name}' ({human_size(part.size)}).")
                if img.offset % 0x1000:
                    warnings.append(f"{img.name} offset {hex_addr(img.offset)} is not aligned to "
                                    "4 KB; esptool will pad it.")
        if self.fs_enabled and self.fs_has_content() and self.fs_target() is None:
            errors.append("The package has LittleFS files but no LittleFS partition was found. "
                          "Pick a partition or enter an offset and size in the LittleFS tab.")
        plan = sorted((p for p in self.resolved_plan(chip) if p[0] is not None), key=lambda p: p[0])
        for (off_a, size_a, name_a), (off_b, _, name_b) in zip(plan, plan[1:]):
            if off_a + size_a > off_b:
                errors.append(f"{name_a} ({hex_addr(off_a)}-{hex_addr(off_a + size_a)}) overlaps "
                              f"{name_b} at {hex_addr(off_b)}.")
        if not self.resolved_plan(chip):
            errors.append("Nothing is selected to flash.")
        return errors, warnings

    def otadata_to_reset(self) -> tuple[int, int] | None:
        """When a new app is written but no OTA data image is, blank otadata so the
        bootloader starts the new app instead of an older OTA slot (like idf.py flash)."""
        if not any(i.enabled and i.role in (ROLE_APP, ROLE_MERGED) for i in self.images):
            return None
        if any(i.enabled and i.role == ROLE_OTADATA for i in self.images):
            return None
        part = next((p for p in self.partitions if p.is_otadata), None)
        return (part.offset, part.size) if part else None

    # ------------------------------------------------------------------ saving
    def to_manifest(self) -> dict:
        def offset_value(img: FlashImage):
            if img.offset == BOOTLOADER:
                return BOOTLOADER
            if img.auto or img.offset is None:
                return "auto"
            return hex_addr(img.offset)

        fs = {"folder": self.fs_folder, "enabled": self.fs_enabled, "partition": self.fs_partition}
        if self.fs_custom:
            fs.update(offset=hex_addr(self.fs_custom[0]), size=hex_addr(self.fs_custom[1]))
        fs.update(self.fs_params.to_dict())
        flash = {"mode": self.flash_mode, "freq": self.flash_freq, "size": self.flash_size}
        if self.baud:
            flash["baud"] = self.baud
        manifest = {
            "format": 1,
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "chip": self.chip or "auto",
            "flash": flash,
            "partition_table": self.partition_file,
            "images": [{"file": i.file, "offset": offset_value(i), "role": i.role,
                        "enabled": i.enabled} for i in self.images],
            "littlefs": fs,
        }
        if self.standalone:
            manifest["standalone"] = self.standalone
        manifest["created_with"] = f"LaFlasher {__version__}"
        return {**manifest, **self._extra}

    def _write_archive(self, target) -> None:
        """Write laflash.json, then zip the package into `target` (a path or a file object)."""
        for alias in MANIFEST_ALIASES[1:]:
            (self.root / alias).unlink(missing_ok=True)
        (self.root / MANIFEST).write_text(json.dumps(self.to_manifest(), indent=2), encoding="utf-8")
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            for item in sorted(self.root.rglob("*")):
                rel = item.relative_to(self.root).as_posix()
                if item.is_dir():
                    if not any(item.iterdir()):
                        zf.writestr(rel + "/", b"")
                else:
                    zf.write(item, rel)

    def save(self, path: str | Path) -> None:
        path = Path(path)
        partial = path.with_name(path.name + ".part")
        self._write_archive(partial)
        os.replace(partial, path)
        self.path = path

    def archive_bytes(self) -> bytes:
        """The package as .laflash bytes, without changing where it is saved."""
        buffer = io.BytesIO()
        self._write_archive(buffer)
        return buffer.getvalue()
