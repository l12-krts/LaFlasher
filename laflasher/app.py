"""Application entry point."""

from __future__ import annotations

import os
import sys


def _smoke_test(app, window, report_path: str) -> None:
    """Used by build.py to check that a frozen build starts and has everything bundled."""
    import json
    import tempfile
    from pathlib import Path

    import esptool

    from . import flasher, fsimage  # noqa: F401  (flasher installs the GUI logger)

    report = {"esptool": esptool.__version__, "style": app.style().name(),
              "package_loaded": window.pkg is not None,
              "standalone": type(window).__name__ == "SimpleWindow",
              "title": window.windowTitle()}
    window.grab().save(report_path + ".png")  # what the window looked like
    stub_dir = Path(esptool.__file__).parent / "targets" / "stub_flasher"
    report["stub_files"] = len(list(stub_dir.rglob("*.json")))
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        try:
            (tmp_path / "files").mkdir()
            (tmp_path / "files" / "hello.txt").write_text("hello")
            image = fsimage.build(tmp_path / "files", 0x10000).data
            (tmp_path / "fs.bin").write_bytes(image)
            report["littlefs"] = "ok"
        except Exception as exc:
            report["littlefs"] = repr(exc)
        try:  # run a real esptool command through its CLI layer
            import esptool as _esptool
            merged = tmp_path / "merged.bin"
            _esptool.main(["--chip", "esp32", "merge-bin", "-o", str(merged),
                           "0x10000", str(tmp_path / "fs.bin")])
            report["esptool_cli"] = "ok" if merged.stat().st_size == 0x20000 else "bad size"
        except BaseException as exc:
            report["esptool_cli"] = repr(exc)
    report["ok"] = (report["stub_files"] > 0 and report["littlefs"] == "ok"
                    and report["esptool_cli"] == "ok")
    Path(report_path).write_text(json.dumps(report, indent=2), encoding="utf-8")
    window.setWindowModified(False)
    window.close()
    app.quit()


def _take_option(argv: list[str], name: str) -> str | None:
    """Remove `name VALUE` from argv and return VALUE."""
    if name not in argv:
        return None
    index = argv.index(name)
    value = argv[index + 1] if index + 1 < len(argv) else ""
    del argv[index:index + 2]
    return value


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv if argv is None else argv)
    smoke_report = _take_option(argv, "--smoke-test")
    # --standalone PACKAGE: open the simple flasher window as an exported .exe would.
    standalone_file = _take_option(argv, "--standalone")
    # --export-standalone PACKAGE --output FLASHER.exe: export without opening a window.
    export_package = _take_option(argv, "--export-standalone")
    export_output = _take_option(argv, "--output")
    # A windowed exe has no console: give print() somewhere harmless to write.
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")
    if export_package:
        from .standalone import export_file
        try:
            size = export_file(export_package, export_output or
                               os.path.splitext(export_package)[0] + " Flasher.exe")
        except (OSError, ValueError) as exc:
            print(f"Export failed: {exc}", file=sys.stderr)
            return 2
        print(f"Exported standalone flasher ({size} bytes)")
        return 0

    from pathlib import Path

    from .standalone import embedded_payload
    payload = Path(standalone_file).read_bytes() if standalone_file else embedded_payload()
    if sys.platform == "win32":
        try:  # own taskbar button/icon instead of python.exe's
            import ctypes
            app_id = "L12.LaFlasher" + (".Standalone" if payload is not None else "")
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(app_id)
        except Exception:
            pass

    from PyQt6.QtWidgets import QApplication, QMessageBox

    from . import APP_NAME, ORG_NAME, __version__
    from .icon import app_icon

    QApplication.setOrganizationName(ORG_NAME)
    # Standalone flashers keep their own settings, apart from the full LaFlasher app, and smoke
    # tests (run by build.py) use throw-away settings so they never touch the real ones.
    QApplication.setApplicationName(f"{APP_NAME} Smoke Test" if smoke_report else
                                    APP_NAME if payload is None else f"{APP_NAME} Standalone")
    QApplication.setApplicationVersion(__version__)
    app = QApplication(argv)
    app.setWindowIcon(app_icon())

    if payload is not None:
        from .package import LaFlashPackage
        from .ui.simple_window import SimpleWindow  # imports esptool, so after the app exists
        try:
            package = LaFlashPackage.from_bytes(payload, "Firmware")
        except Exception as exc:
            QMessageBox.critical(None, APP_NAME, f"The firmware inside this flasher is damaged:\n{exc}")
            return 1
        window = SimpleWindow(package)
    else:
        from .ui.main_window import MainWindow  # imports esptool, so after the app exists
        open_path = next((a for a in argv[1:] if not a.startswith("-") and os.path.isfile(a)), None)
        window = MainWindow(open_path)
    window.show()
    if not smoke_report:
        return app.exec()
    from PyQt6.QtCore import QSettings, QTimer
    QTimer.singleShot(2500, lambda: _smoke_test(app, window, smoke_report))
    code = app.exec()
    QSettings().clear()  # drop the throw-away settings the test run wrote
    return code
