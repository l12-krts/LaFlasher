"""Build LaFlasher.exe with PyInstaller, then the zips and the installer.

    python build.py                 everything:
                                      dist/LaFlasher/ (folder build, starts fast) and its zip,
                                      dist/LaFlasher.exe (single file) and its zip,
                                      dist/LaFlasher-<version>-setup.exe (needs Inno Setup 6)
    python build.py --onefile       only the single-file LaFlasher.exe and its zip
    python build.py --no-installer  skip the Inno Setup installer
    python build.py --no-venv       use the current Python instead of .venv

esptool and the LittleFS image builder are embedded, so a zip or the installer is all a user
needs. The single-file build doubles as the runtime of exported standalone flashers
(File > Export Standalone Flasher), so it is also copied into the folder build.

The script creates .venv with requirements.txt + PyInstaller on first run, generates the icon
and version resource, runs PyInstaller on LaFlasher.spec, smoke-tests each exe (including a
freshly exported standalone flasher), zips the results and compiles installer/LaFlasher.iss.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import venv
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV = ROOT / ".venv"
BUILD = ROOT / "build"
DIST = ROOT / "dist"
APP = "LaFlasher"
PUBLISHER = "lab.krts"
PYINSTALLER_REQ = "pyinstaller>=6.10"
INNO_SETUP_URL = "https://jrsoftware.org/isdl.php"


def venv_python() -> Path:
    return VENV / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def reexec_in_venv() -> None:
    py = venv_python()
    if not py.exists():
        print(f"Creating virtual environment in {VENV} ...")
        venv.create(VENV, with_pip=True)
    print("Installing requirements ...")
    subprocess.check_call([str(py), "-m", "pip", "install", "--disable-pip-version-check", "-q",
                           "-r", str(ROOT / "requirements.txt"), PYINSTALLER_REQ])
    sys.exit(subprocess.call([str(py), str(Path(__file__).resolve()), *sys.argv[1:], "--no-venv"]))


def write_version_file(path: Path, version: str) -> None:
    parts = [int(p) if p.isdigit() else 0 for p in version.split(".")[:4]]
    nums = tuple(parts + [0] * (4 - len(parts)))
    strings = {
        "CompanyName": PUBLISHER, "FileDescription": "LaFlasher - ESP32 package flasher",
        "FileVersion": version, "InternalName": APP, "OriginalFilename": f"{APP}.exe",
        "ProductName": APP, "ProductVersion": version,
    }
    entries = ",\n".join(f"          StringStruct('{k}', '{v}')" for k, v in strings.items())
    path.write_text(f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers={nums}, prodvers={nums}, mask=0x3f, flags=0x0, OS=0x40004,
                    fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('040904B0', [
{entries}
    ])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
""", encoding="utf-8")


def smoke_test(exe: Path, expect_standalone: bool = False) -> bool:
    print(f"Smoke test: starting {exe.name} ...")
    report = Path(tempfile.gettempdir()) / "laflasher_smoke.json"
    report.unlink(missing_ok=True)
    try:
        subprocess.run([str(exe), "--smoke-test", str(report)], timeout=120)
    except subprocess.TimeoutExpired:
        print("  FAILED: the app did not exit (an error dialog may be open).")
        return False
    if not report.exists():
        print("  FAILED: the app exited without writing its report.")
        return False
    result = json.loads(report.read_text(encoding="utf-8"))
    report.unlink(missing_ok=True)
    Path(str(report) + ".png").unlink(missing_ok=True)
    print("  " + ", ".join(f"{k}={v}" for k, v in result.items()))
    if result.get("standalone") != expect_standalone or (expect_standalone and not result.get("package_loaded")):
        print(f"  FAILED: expected the {'simple flasher' if expect_standalone else 'full editor'} window.")
        return False
    return bool(result.get("ok"))


def standalone_smoke_test(runtime: Path) -> bool:
    """Export a tiny package with `runtime` and check the result opens the simple window."""
    from laflasher.package import LaFlashPackage
    from laflasher.standalone import write_standalone

    pkg = LaFlashPackage.new("Smoke Test")
    try:
        pkg.fs_dir.mkdir()
        (pkg.fs_dir / "config.json").write_text('{"wifi_ssid": "test"}', encoding="utf-8")
        exe = BUILD / "standalone-smoke-test.exe"
        write_standalone(runtime, pkg.archive_bytes(), exe)
    finally:
        pkg.close()
    try:
        return smoke_test(exe, expect_standalone=True)
    finally:
        exe.unlink(missing_ok=True)


def pyinstaller(onefile: bool) -> Path:
    import PyInstaller.__main__

    os.environ["LAFLASHER_ONEFILE"] = "1" if onefile else "0"
    print(f"Building the {'single-file' if onefile else 'folder'} version ...")
    work = BUILD / ("pyinstaller-onefile" if onefile else "pyinstaller")
    PyInstaller.__main__.run([str(ROOT / f"{APP}.spec"), "--noconfirm", "--clean",
                              "--distpath", str(DIST), "--workpath", str(work)])
    return DIST / f"{APP}.exe" if onefile else DIST / APP / f"{APP}.exe"


def write_zip(zip_path: Path, files: list[tuple[Path, str]]) -> None:
    zip_path.unlink(missing_ok=True)
    print(f"Zipping {zip_path.name} ...")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for path, name in files:
            zf.write(path, name)
        zf.write(ROOT / "README.md", f"{APP}/README.md")
    print(f"Done: {zip_path} ({zip_path.stat().st_size / 1024 / 1024:.1f} MB)")


def find_iscc() -> Path | None:
    """Inno Setup 6's command-line compiler: $ISCC, PATH, or the default install folders."""
    candidates = [os.environ.get("ISCC", ""), shutil.which("iscc") or ""]
    for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles"),
                 str(Path(os.environ.get("LOCALAPPDATA", "")) / "Programs")):
        if base:
            candidates.append(str(Path(base) / "Inno Setup 6" / "ISCC.exe"))
    return next((Path(c) for c in candidates if c and Path(c).is_file()), None)


def build_installer(version: str) -> Path | None:
    iscc = find_iscc()
    if iscc is None:
        print(f"Inno Setup 6 was not found, so no installer was made. Install it from "
              f"{INNO_SETUP_URL} (or: winget install JRSoftware.InnoSetup), then run build.py again.")
        return None
    from laflasher.icon import write_installer_images

    images = write_installer_images(BUILD)
    print(f"Building the installer with {iscc} ...")
    subprocess.run([str(iscc), "/Q",
                    f"/DAppVersion={version}",
                    f"/DSourceDir={DIST / APP}",
                    f"/DBuildDir={BUILD}",
                    f"/DOutputDir={DIST}",
                    f"/DWizardLarge={images['large']}",
                    f"/DWizardSmall={images['small']}",
                    str(ROOT / "installer" / f"{APP}.iss")], check=True)
    setup = DIST / f"{APP}-{version}-setup.exe"
    return setup if setup.exists() else None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--onefile", action="store_true", help="only build the single-file LaFlasher.exe")
    parser.add_argument("--no-venv", action="store_true", help="use the current Python environment")
    parser.add_argument("--skip-test", action="store_true", help="don't launch the built exes")
    parser.add_argument("--no-installer", action="store_true", help="don't build the Inno Setup installer")
    args = parser.parse_args()
    if not args.no_venv:
        reexec_in_venv()

    sys.path.insert(0, str(ROOT))
    from laflasher import __version__
    from laflasher.icon import write_ico
    from laflasher.standalone import RUNTIME_NAME

    BUILD.mkdir(exist_ok=True)
    write_ico(str(BUILD / "laflasher.ico"))
    write_version_file(BUILD / "version_info.txt", __version__)
    print(f"Building {APP} {__version__} ...")

    # 1. Single-file exe: a portable LaFlasher, and the base of exported standalone flashers.
    runtime = pyinstaller(onefile=True)
    if not runtime.exists():
        print("Build failed: no single-file exe produced.")
        return 1
    if not args.skip_test and not (smoke_test(runtime) and standalone_smoke_test(runtime)):
        print("The single-file build failed its smoke test; stopping.")
        return 1
    write_zip(DIST / f"{APP}-{__version__}-windows-x64-onefile.zip", [(runtime, f"{APP}/{APP}.exe")])
    if args.onefile:
        return 0

    # 2. Folder build (starts faster), carrying the single-file exe so it can export flashers.
    exe = pyinstaller(onefile=False)
    if not exe.exists():
        print("Build failed: no exe produced.")
        return 1
    shutil.copy2(runtime, exe.parent / RUNTIME_NAME)
    if not args.skip_test and not smoke_test(exe):
        print("The built app failed its smoke test; not creating the zip.")
        return 1
    write_zip(DIST / f"{APP}-{__version__}-windows-x64.zip",
              [(item, f"{APP}/{item.relative_to(DIST / APP).as_posix()}")
               for item in sorted((DIST / APP).rglob("*")) if item.is_file()])

    if not args.no_installer:
        setup = build_installer(__version__)
        if setup:
            print(f"Done: {setup} ({setup.stat().st_size / 1024 / 1024:.1f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
