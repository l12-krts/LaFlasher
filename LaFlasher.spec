# PyInstaller spec for LaFlasher. Normally run through build.py, which also creates the icon,
# the version resource and the zip. Direct use: pyinstaller LaFlasher.spec
# Set LAFLASHER_ONEFILE=1 for a single-file exe.
import os

from PyInstaller.utils.hooks import collect_all, collect_data_files, collect_submodules

NAME = "LaFlasher"
ONEFILE = os.environ.get("LAFLASHER_ONEFILE") == "1"
ICON = os.path.join(SPECPATH, "build", "laflasher.ico")
VERSION_FILE = os.path.join(SPECPATH, "build", "version_info.txt")

datas, binaries, hiddenimports = [], [], []
# Embedded esptool: chip targets plus the stub flasher binaries (JSON data files).
datas += collect_data_files("esptool")
hiddenimports += collect_submodules("esptool")
# esptool's logging library, and rich which it uses to format messages (loads tables lazily).
for package in ("esp_pylib", "littlefs"):  # littlefs = the embedded "mklittlefs"
    d, b, h = collect_all(package)
    datas, binaries, hiddenimports = datas + d, binaries + b, hiddenimports + h
hiddenimports += collect_submodules("rich")

a = Analysis(
    [os.path.join(SPECPATH, "LaFlasher.py")],
    pathex=[SPECPATH],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["tkinter", "PyQt5", "PySide2", "PySide6", "matplotlib", "numpy", "IPython",
              "pytest", "setuptools", "pkg_resources"],
)
# A widgets-only app needs neither the software OpenGL renderer nor the PDF image plugin.
DROP = {"opengl32sw.dll", "qt6pdf.dll", "qpdf.dll"}
a.binaries = [entry for entry in a.binaries if os.path.basename(entry[0]).lower() not in DROP]

pyz = PYZ(a.pure)
exe_options = dict(
    name=NAME,
    console=False,
    upx=False,
    icon=ICON if os.path.exists(ICON) else None,
    version=VERSION_FILE if os.path.exists(VERSION_FILE) else None,
)
if ONEFILE:
    exe = EXE(pyz, a.scripts, a.binaries, a.datas, [], **exe_options)
else:
    exe = EXE(pyz, a.scripts, [], exclude_binaries=True, **exe_options)
    coll = COLLECT(exe, a.binaries, a.datas, name=NAME, upx=False)
