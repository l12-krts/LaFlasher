"""The LaFlasher main window."""

from __future__ import annotations

import html
import os
import subprocess
import sys
from pathlib import Path

from PyQt6.QtCore import QElapsedTimer, QSettings, Qt, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import QAction, QDesktopServices, QFontDatabase, QKeySequence
from PyQt6.QtWidgets import (
    QApplication, QFileDialog, QGroupBox, QHBoxLayout, QInputDialog, QLabel, QListWidget,
    QListWidgetItem, QMainWindow, QMessageBox, QPlainTextEdit, QPushButton, QSplitter,
    QStackedWidget, QStyle, QTabWidget, QVBoxLayout, QWidget,
)

from .. import APP_NAME, __version__, chips, fsimage, standalone
from ..flasher import EspWorker, Job, build_flash_job
from ..icon import app_icon
from ..package import (
    EXTENSION, LaFlashPackage, find_build_dirs, find_fs_source, project_name,
)
from ..util import human_size
from .celebrate import celebrate
from .device_panel import DevicePanel
from .dialogs import CreditsDialog, FormatHelpDialog
from .export_dialog import ExportDialog
from .files_tab import FilesTab
from .firmware_tab import PACKAGE_SUFFIXES, FirmwareTab
from .info_tab import InfoTab
from .partitions_tab import PartitionsTab
from .simple_window import SimpleWindow

MAX_RECENT = 8
LOG_PREFIX = {"info": "", "warn": "⚠ ", "error": "✖ ", "ok": "✔ ", "hint": "→ "}
PACKAGE_FILTER = f"LaFlash packages (*{EXTENSION} *.zip);;All files (*)"


class MainWindow(QMainWindow):
    closed = pyqtSignal()

    def __init__(self, open_path: str | None = None, package: LaFlashPackage | None = None,
                 embedded: bool = False):
        """`package` + `embedded`: the full editor opened from a standalone flasher, which
        keeps owning the package (it isn't closed or saved-on-exit here)."""
        super().__init__()
        self.settings = QSettings()
        self.pkg: LaFlashPackage | None = None
        self.worker: EspWorker | None = None
        self._embedded = embedded
        self._borrowed = package if embedded else None
        self._job_clock = QElapsedTimer()
        self._last_device_info: dict = {}
        self.setWindowIcon(app_icon())
        self.setAcceptDrops(True)
        self._build_ui()
        self._build_actions()
        self._restore_settings()
        self._set_package(package)
        if open_path:
            QTimer.singleShot(0, lambda: self.open_package(open_path))

    # ================================================================== layout
    def _build_ui(self) -> None:
        style = self.style()
        sp = QStyle.StandardPixmap
        self.device = DevicePanel()
        self.device.flash_requested.connect(self.flash)
        self.device.detect_requested.connect(self.detect_board)
        self.device.port_appeared.connect(self._port_appeared)

        self.firmware_tab = FirmwareTab()
        self.files_tab = FilesTab()
        self.partitions_tab = PartitionsTab()
        self.info_tab = InfoTab()
        self.firmware_tab.changed.connect(self._firmware_changed)
        self.firmware_tab.open_package_requested.connect(self.open_package)
        self.firmware_tab.show_files_requested.connect(lambda: self.tabs.setCurrentWidget(self.files_tab))
        self.files_tab.changed.connect(self._files_changed)
        self.files_tab.open_package_requested.connect(self.open_package)
        self.info_tab.changed.connect(self._info_changed)

        self.tabs = QTabWidget()
        self.tabs.addTab(self.firmware_tab, style.standardIcon(sp.SP_DriveHDIcon), "Firmware")
        self.tabs.addTab(self.files_tab, style.standardIcon(sp.SP_DirIcon), "LittleFS Files")
        self.tabs.addTab(self.partitions_tab, style.standardIcon(sp.SP_FileDialogListView), "Partition Table")
        self.tabs.addTab(self.info_tab, style.standardIcon(sp.SP_FileDialogInfoView), "Package Info")

        editor = QWidget()
        editor_layout = QVBoxLayout(editor)
        editor_layout.setContentsMargins(0, 0, 0, 0)
        self.header = QLabel()
        self.header.setWordWrap(True)
        self.header.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        editor_layout.addWidget(self.header)
        editor_layout.addWidget(self.tabs, 1)

        self.stack = QStackedWidget()
        self.stack.addWidget(self._build_welcome())
        self.stack.addWidget(editor)

        top = QWidget()
        top_layout = QHBoxLayout(top)
        top_layout.addWidget(self.stack, 1)
        top_layout.addWidget(self.device)

        log_box = QGroupBox("Log")
        log_layout = QVBoxLayout(log_box)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(20000)
        self.log_view.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self.log_view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        log_layout.addWidget(self.log_view, 1)
        log_buttons = QHBoxLayout()
        clear_btn = QPushButton("Clear")
        clear_btn.clicked.connect(self.log_view.clear)
        save_log_btn = QPushButton("Save Log...")
        save_log_btn.clicked.connect(self.save_log)
        log_buttons.addStretch(1)
        log_buttons.addWidget(clear_btn)
        log_buttons.addWidget(save_log_btn)
        log_layout.addLayout(log_buttons)

        self.splitter = QSplitter(Qt.Orientation.Vertical)
        self.splitter.addWidget(top)
        self.splitter.addWidget(log_box)
        self.splitter.setStretchFactor(0, 3)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setCollapsible(0, False)
        self.setCentralWidget(self.splitter)

        self.status_pkg = QLabel()
        self.statusBar().addPermanentWidget(self.status_pkg)

    def _build_welcome(self) -> QWidget:
        page = QWidget()
        outer = QHBoxLayout(page)
        column = QVBoxLayout()
        outer.addStretch(1)
        outer.addLayout(column, 3)
        outer.addStretch(1)

        title = QLabel(APP_NAME)
        font = title.font()
        font.setPointSize(font.pointSize() + 14)
        font.setBold(True)
        title.setFont(font)
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        subtitle = QLabel("Flash ESP32 firmware and LittleFS files in one click.")
        subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)

        sp = QStyle.StandardPixmap
        open_btn = QPushButton(self.style().standardIcon(sp.SP_DialogOpenButton), "Open .laflash Package...")
        open_btn.setMinimumHeight(44)
        big = open_btn.font()
        big.setPointSize(big.pointSize() + 2)
        open_btn.setFont(big)
        open_btn.clicked.connect(lambda: self.open_package())
        import_btn = QPushButton(self.style().standardIcon(sp.SP_DirOpenIcon), "Import from Build Folder...")
        import_btn.setToolTip("Make a package from an Arduino IDE export, a PlatformIO project "
                              "or an ESP-IDF build folder")
        import_btn.clicked.connect(lambda: self.import_folder())
        new_btn = QPushButton(self.style().standardIcon(sp.SP_FileIcon), "New Empty Package")
        new_btn.clicked.connect(self.new_package)
        hint = QLabel("You can also drag a .laflash file, a build folder or .bin files onto this window.")
        hint.setWordWrap(True)
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.recent_list = QListWidget()
        self.recent_list.setMaximumHeight(160)
        self.recent_list.itemActivated.connect(lambda item: self.open_package(item.data(Qt.ItemDataRole.UserRole)))
        credits_link = QLabel('<a href="#credits">Credits</a>')
        credits_link.setAlignment(Qt.AlignmentFlag.AlignCenter)
        credits_link.linkActivated.connect(lambda _: self.show_credits())

        column.addStretch(2)
        for w in (title, subtitle):
            column.addWidget(w)
        column.addSpacing(18)
        for w in (open_btn, import_btn, new_btn):
            column.addWidget(w)
        column.addSpacing(8)
        column.addWidget(hint)
        column.addSpacing(18)
        self.recent_label = QLabel("Recent packages (double-click to open):")
        column.addWidget(self.recent_label)
        column.addWidget(self.recent_list)
        column.addStretch(3)
        column.addWidget(credits_link)
        return page

    def _action(self, text: str, slot, shortcut=None, pixmap=None, tip: str = "") -> QAction:
        action = QAction(text, self)
        if pixmap is not None:
            action.setIcon(self.style().standardIcon(pixmap))
        if shortcut is not None:
            action.setShortcut(QKeySequence(shortcut))
        if tip:
            action.setStatusTip(tip)
            action.setToolTip(tip)
        action.triggered.connect(slot)
        return action

    def _build_actions(self) -> None:
        sp = QStyle.StandardPixmap
        sk = QKeySequence.StandardKey
        self.new_act = self._action("&New Package", self.new_package, sk.New, sp.SP_FileIcon)
        self.open_act = self._action("&Open...", lambda: self.open_package(), sk.Open,
                                     sp.SP_DialogOpenButton, "Open a .laflash package")
        self.import_act = self._action("&Import from Build Folder...", lambda: self.import_folder(),
                                       "Ctrl+I", sp.SP_DirOpenIcon,
                                       "Make a package from Arduino, PlatformIO or ESP-IDF build output")
        self.save_act = self._action("&Save", self.save, sk.Save, sp.SP_DialogSaveButton,
                                     "Save changes to the package")
        self.save_as_act = self._action("Save &As...", self.save_as, "Ctrl+Shift+S")
        self.export_exe_act = self._action(
            "&Export Standalone Flasher (.exe)...", self.export_standalone, "Ctrl+E",
            sp.SP_ComputerIcon, "One .exe that anyone can run to flash this package, "
                                "no installation needed")
        self.export_exe_act.setIconText("Export .exe")
        self.close_act = self._action("&Close Package", self.close_package, "Ctrl+W")
        self.exit_act = self._action("E&xit", self.close, "Ctrl+Q")
        self.flash_act = self._action("&Flash", self.flash, "F5", sp.SP_ArrowForward,
                                      "Write the package to the connected board")
        self.detect_act = self._action("&Detect Board", self.detect_board, "Ctrl+D",
                                       sp.SP_ComputerIcon, "Show the connected chip, flash size and MAC")
        self.erase_act = self._action("&Erase Entire Flash...", self.erase_flash)
        self.refresh_act = self._action("&Refresh Ports", lambda: self.device.refresh_ports(force=True),
                                        None, sp.SP_BrowserReload)
        self.export_fs_act = self._action("Export &LittleFS Image...", self.export_fs_image)
        self.export_merged_act = self._action("Export &Merged Firmware (.bin)...", self.export_merged,
                                              tip="One file to flash at 0x0, e.g. with web flashers")
        self.associate_act = self._action("Open .laflash Files with LaFlasher", self.associate_files,
                                          tip="Double-click .laflash files in Explorer to open them here")
        self.confetti_act = self._action("&Confetti After Flashing", lambda checked: None,
                                         tip="Celebrate a successful flash with confetti")
        self.confetti_act.setCheckable(True)
        self.help_act = self._action("About .laflash &Packages", lambda: FormatHelpDialog(self).exec(),
                                     sk.HelpContents)
        self.credits_act = self._action("&Credits", self.show_credits)
        self.about_act = self._action("&About LaFlasher", self.about)
        self.about_qt_act = self._action("About &Qt", QApplication.aboutQt)

        bar = self.menuBar()
        file_menu = bar.addMenu("&File")
        file_menu.addActions([self.new_act, self.open_act, self.import_act])
        self.recent_menu = file_menu.addMenu("Open &Recent")
        file_menu.addSeparator()
        file_menu.addActions([self.save_act, self.save_as_act])
        file_menu.addSeparator()
        file_menu.addAction(self.export_exe_act)
        file_menu.addSeparator()
        file_menu.addActions([self.close_act, self.exit_act])
        device_menu = bar.addMenu("&Device")
        device_menu.addActions([self.flash_act, self.detect_act, self.erase_act])
        device_menu.addSeparator()
        device_menu.addAction(self.refresh_act)
        tools_menu = bar.addMenu("&Tools")
        tools_menu.addActions([self.export_fs_act, self.export_merged_act])
        tools_menu.addSeparator()
        tools_menu.addAction(self.confetti_act)
        if sys.platform == "win32":
            tools_menu.addAction(self.associate_act)
        help_menu = bar.addMenu("&Help")
        help_menu.addActions([self.help_act])
        help_menu.addSeparator()
        help_menu.addActions([self.credits_act, self.about_act, self.about_qt_act])

        toolbar = self.addToolBar("Main")
        toolbar.setObjectName("MainToolbar")
        toolbar.setMovable(False)
        toolbar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        toolbar.addActions([self.open_act, self.save_act, self.export_exe_act])
        toolbar.addSeparator()
        toolbar.addActions([self.flash_act, self.detect_act])

    # ================================================================ settings
    def _restore_settings(self) -> None:
        geometry = self.settings.value("geometry")
        if geometry is not None:
            self.restoreGeometry(geometry)
        else:
            self.resize(1180, 780)
        splitter = self.settings.value("splitter")
        if splitter is not None:
            self.splitter.restoreState(splitter)
        else:
            self.splitter.setSizes([560, 200])
        self.last_dir = str(self.settings.value("lastDir", str(Path.home())))
        self.firmware_tab.last_dir = self.files_tab.last_dir = self.last_dir
        baud = self.settings.value("baud")
        if baud:
            self.device.set_baud(int(baud))
        self.device.reset_check.setChecked(self.settings.value("resetAfter", True, type=bool))
        self.confetti_act.setChecked(self.settings.value("confetti", True, type=bool))
        port = self.settings.value("port")
        if port:
            self.device.set_port(str(port))
        self._refresh_recent()

    def _save_settings(self) -> None:
        self.settings.setValue("geometry", self.saveGeometry())
        self.settings.setValue("splitter", self.splitter.saveState())
        self.settings.setValue("lastDir", self.last_dir)
        self.settings.setValue("baud", self.device.baud())
        self.settings.setValue("resetAfter", self.device.reset_check.isChecked())
        self.settings.setValue("confetti", self.confetti_act.isChecked())
        if self.device.current_port():
            self.settings.setValue("port", self.device.current_port())

    def _recent(self) -> list[str]:
        value = self.settings.value("recent", [])
        if isinstance(value, str):
            value = [value]
        return [p for p in (value or []) if p]

    def _add_recent(self, path: Path) -> None:
        path_str = str(path)
        recent = [p for p in self._recent() if os.path.normcase(p) != os.path.normcase(path_str)]
        self.settings.setValue("recent", [path_str] + recent[:MAX_RECENT - 1])
        self._refresh_recent()

    def _refresh_recent(self) -> None:
        recent = [p for p in self._recent() if os.path.isfile(p)]
        self.recent_menu.clear()
        self.recent_list.clear()
        for path in recent:
            action = self.recent_menu.addAction(Path(path).name)
            action.setToolTip(path)
            action.triggered.connect(lambda _=False, p=path: self.open_package(p))
            item = QListWidgetItem(self.style().standardIcon(QStyle.StandardPixmap.SP_FileIcon),
                                   f"{Path(path).name}   ({Path(path).parent})")
            item.setData(Qt.ItemDataRole.UserRole, path)
            item.setToolTip(path)
            self.recent_list.addItem(item)
        self.recent_menu.setEnabled(bool(recent))
        self.recent_label.setVisible(bool(recent))
        self.recent_list.setVisible(bool(recent))

    # ================================================================== state
    def log(self, text: str, level: str = "info") -> None:
        lines = str(text).splitlines() or [""]
        self.log_view.appendPlainText(LOG_PREFIX.get(level, "") + lines[0])
        for line in lines[1:]:
            self.log_view.appendPlainText(line)
        bar = self.log_view.verticalScrollBar()
        bar.setValue(bar.maximum())

    def _set_dirty(self, dirty: bool) -> None:
        self.setWindowModified(dirty)
        self._update_title()

    def _update_title(self) -> None:
        if self.pkg is None:
            self.setWindowTitle(f"{APP_NAME} {__version__}")
            return
        if self.pkg.path:
            name = self.pkg.path.name
        elif self.pkg is self._borrowed:  # the package inside a standalone flasher
            name = self.pkg.name or "Firmware"
        else:
            name = f"{self.pkg.name or 'Untitled'} (unsaved)"
        self.setWindowTitle(f"{name}[*] - {APP_NAME}")

    def _update_header(self) -> None:
        pkg = self.pkg
        if pkg is None:
            self.status_pkg.setText("No package")
            return
        chip = pkg.target_chip()
        title = f"<b>{html.escape(pkg.name or 'Untitled')}</b>"
        if pkg.version:
            title += f" {html.escape(pkg.version)}"
        images = sum(1 for i in pkg.images if i.enabled)
        files = pkg.fs_file_count()
        facts = [title, f"for {chips.chip_label(chip)}" if chip else "for any ESP32",
                 f"{images} image{'s' if images != 1 else ''}",
                 f"{files} LittleFS file{'s' if files != 1 else ''}"]
        if pkg.path:
            location = str(pkg.path)
        elif pkg is self._borrowed:
            location = "Built into this flasher - changes apply until you close it"
        else:
            location = "New package - use File > Save to keep it"
        text = " &nbsp;&middot;&nbsp; ".join(facts) + f"<br>{html.escape(location)}"
        if pkg.description:
            text += f"<br><i>{html.escape(pkg.description)}</i>"
        self.header.setText(text)
        self.status_pkg.setText(pkg.path.name if pkg.path else
                                "Built-in package" if pkg is self._borrowed else "Unsaved package")

    def _update_state(self) -> None:
        has_pkg = self.pkg is not None
        busy = self.worker is not None
        for action in (self.new_act, self.open_act, self.import_act):
            action.setEnabled(not busy)
        for action in (self.save_act, self.save_as_act, self.close_act):
            action.setEnabled(has_pkg and not busy)
        for action in (self.export_fs_act, self.export_merged_act, self.export_exe_act):
            action.setEnabled(has_pkg and not busy)
        self.detect_act.setEnabled(not busy)
        self.erase_act.setEnabled(not busy)
        self.flash_act.setText("&Cancel" if busy else "&Flash")
        self.flash_act.setIcon(self.style().standardIcon(
            QStyle.StandardPixmap.SP_DialogCancelButton if busy else QStyle.StandardPixmap.SP_ArrowForward))
        self.stack.setEnabled(not busy)

    def _set_package(self, pkg: LaFlashPackage | None, dirty: bool = False) -> None:
        old, self.pkg = self.pkg, pkg
        for tab in (self.firmware_tab, self.files_tab, self.partitions_tab, self.info_tab):
            tab.set_package(pkg)
        if old is not None and old is not pkg and old is not self._borrowed:
            old.close()
        self.stack.setCurrentIndex(1 if pkg else 0)
        self.tabs.setCurrentIndex(0)
        if pkg is not None:
            for note in pkg.notes:
                self.log(note, "info")
            pkg.notes.clear()
            if pkg.baud:
                self.device.set_baud(pkg.baud)
            self.device.set_status("Ready. Connect your board and click Flash.")
        else:
            self.device.set_status("Open a package to get started.")
        self._set_dirty(dirty)
        self._update_header()
        self._update_state()

    def _firmware_changed(self) -> None:
        self.firmware_tab.refresh()
        self.files_tab.refresh()
        self.partitions_tab.set_package(self.pkg)
        self.info_tab.refresh_detected()
        self._update_header()
        self._set_dirty(True)

    def _files_changed(self) -> None:
        self.firmware_tab.refresh()
        self.partitions_tab.set_package(self.pkg)
        self._update_header()
        self._set_dirty(True)

    def _info_changed(self) -> None:
        self.firmware_tab.refresh()
        self.files_tab._schedule_usage()
        self._update_header()
        self._update_title()
        self._set_dirty(True)

    # ============================================================== packages
    def _maybe_save(self) -> bool:
        """Ask about unsaved changes. Returns False if the user cancelled."""
        if self.pkg is None or self.pkg is self._borrowed or not self.isWindowModified():
            return True  # a standalone flasher's package keeps its changes while it runs
        answer = QMessageBox.warning(
            self, "Unsaved Changes",
            f"Save the changes to {self.pkg.path.name if self.pkg.path else self.pkg.name or 'the package'}?",
            QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard
            | QMessageBox.StandardButton.Cancel)
        if answer == QMessageBox.StandardButton.Save:
            return self.save()
        return answer == QMessageBox.StandardButton.Discard

    def _can_switch(self) -> bool:
        if self.worker is not None:
            QMessageBox.information(self, "Busy", "Wait for the current operation to finish.")
            return False
        return self._maybe_save()

    def new_package(self) -> None:
        if not self._can_switch():
            return
        self._set_package(LaFlashPackage.new("Untitled"), dirty=True)
        self.log("Created a new empty package. Add firmware images, then save it.")

    def open_package(self, path: str | None = None) -> None:
        if not self._can_switch():
            return
        if not path:
            path, _ = QFileDialog.getOpenFileName(self, "Open Package", self.last_dir, PACKAGE_FILTER)
            if not path:
                return
        path = Path(path)
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            pkg = LaFlashPackage.open(path)
        except Exception as exc:
            QApplication.restoreOverrideCursor()
            QMessageBox.critical(self, "Could Not Open Package", f"{path.name}\n\n{exc}")
            return
        QApplication.restoreOverrideCursor()
        self.last_dir = str(path.parent)
        self.log("")
        self.log(f"Opened {path}")
        self._set_package(pkg)
        self._add_recent(path)

    def import_folder(self, folder: str | None = None) -> None:
        if not self._can_switch():
            return
        if not folder:
            folder = QFileDialog.getExistingDirectory(self, "Select Project or Build Folder", self.last_dir)
            if not folder:
                return
        folder_path = Path(folder)
        self.last_dir = str(folder_path)
        builds = find_build_dirs(folder_path)
        if not builds:
            QMessageBox.information(
                self, "No Firmware Found",
                "No compiled firmware (.bin files) was found in this folder.\n\n"
                "• Arduino IDE: use Sketch > Export Compiled Binary, then pick the sketch folder.\n"
                "• PlatformIO: build the project, then pick the project folder.\n"
                "• ESP-IDF: run idf.py build, then pick the project folder.")
            return
        build = builds[0]
        if len(builds) > 1:
            labels = [str(b.relative_to(folder_path)) if b != folder_path else "(this folder)"
                      for b in builds]
            choice, ok = QInputDialog.getItem(self, "Choose Build",
                                              "Several builds were found. Which one should be packaged?",
                                              labels, 0, False)
            if not ok:
                return
            build = builds[labels.index(choice)]
        fs_source = find_fs_source(folder_path, build)
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            pkg = LaFlashPackage.from_build_dir(build, project_name(folder_path), fs_source)
        except Exception as exc:
            QApplication.restoreOverrideCursor()
            QMessageBox.critical(self, "Import Failed", str(exc))
            return
        QApplication.restoreOverrideCursor()
        self.log("")
        self.log(f"Imported {len(pkg.images)} image(s) from {build}")
        if fs_source:
            self.log(f"LittleFS files copied from {fs_source}")
        self._set_package(pkg, dirty=True)
        self.statusBar().showMessage("Imported. Check the images, then use File > Save to create "
                                     "the .laflash package.", 15000)

    def save(self) -> bool:
        if self.pkg is None:
            return False
        if self.pkg.path is None:
            return self.save_as()
        return self._save_to(self.pkg.path)

    def save_as(self) -> bool:
        if self.pkg is None:
            return False
        suggested = self.pkg.path or Path(self.last_dir) / f"{self.pkg.name or 'package'}{EXTENSION}"
        path, _ = QFileDialog.getSaveFileName(self, "Save Package", str(suggested),
                                              f"LaFlash package (*{EXTENSION})")
        if not path:
            return False
        path = Path(path)
        if path.suffix.lower() not in PACKAGE_SUFFIXES:
            path = path.with_name(path.name + EXTENSION)
        return self._save_to(path)

    def _save_to(self, path: Path) -> bool:
        self.files_tab.rescan_if_changed()
        try:
            self.pkg.save(path)
        except OSError as exc:
            QMessageBox.critical(self, "Could Not Save", str(exc))
            return False
        self.last_dir = str(path.parent)
        self.log(f"Saved {path} ({human_size(path.stat().st_size)})", "ok")
        self.statusBar().showMessage(f"Saved {path.name}", 8000)
        self._add_recent(path)
        self._set_dirty(False)
        self._update_header()
        return True

    def close_package(self) -> None:
        if self._can_switch():
            self._set_package(None)

    # =========================================================== board jobs
    def _make_job(self, kind: str, port: str = "") -> Job:
        return build_flash_job(self.pkg, port, self.device.baud(),
                               erase_all=self.device.erase_check.isChecked(),
                               reset_after=self.device.reset_check.isChecked(), kind=kind)

    def _require_port(self) -> str | None:
        port = self.device.current_port()
        if not port:
            QMessageBox.warning(
                self, "No Board Found",
                "Connect your ESP32 board with a USB data cable.\n\nIf it still doesn't appear, "
                "install the USB driver for its USB chip (CP210x or CH340) and click refresh.")
        return port

    def flash(self) -> None:
        if self.worker is not None:
            self.worker.cancel()
            self.device.set_status("Cancelling...")
            return
        if self.pkg is None:
            QMessageBox.information(self, "No Package", "Open a .laflash package first.")
            self.open_package()
            return
        self.files_tab.rescan_if_changed()
        errors, warnings = self.pkg.problems()
        fs_error = self.files_tab.fs_error()
        if fs_error:
            errors.append(f"LittleFS: {fs_error}")
        if errors:
            QMessageBox.warning(self, "Can't Flash Yet",
                                "Please fix these first:\n\n" + "\n".join(f"• {e}" for e in errors))
            return
        port = self._require_port()
        if not port:
            return
        if self.device.erase_check.isChecked():
            answer = QMessageBox.warning(
                self, "Erase Entire Flash?",
                f"Everything on the board at {port} will be erased before flashing, including "
                "saved Wi-Fi settings and other stored data. Continue?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if answer != QMessageBox.StandardButton.Yes:
                return
        self.log("")
        self.log(f"=== Flashing {self.pkg.name or 'package'} to {port} ===")
        for warning in warnings:
            self.log(warning, "warn")
        self._start(self._make_job("flash", port))

    def detect_board(self) -> None:
        if self.worker is not None:
            return
        port = self._require_port()
        if port:
            self.log("")
            self.log(f"=== Detecting board on {port} ===")
            self._start(Job("detect", port=port, baud=self.device.baud()))

    def erase_flash(self) -> None:
        if self.worker is not None:
            return
        port = self._require_port()
        if not port:
            return
        answer = QMessageBox.warning(
            self, "Erase Entire Flash?",
            f"This erases everything on the board at {port}: firmware, files and settings. "
            "The board will do nothing until you flash it again. Continue?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if answer == QMessageBox.StandardButton.Yes:
            self.log("")
            self.log(f"=== Erasing flash on {port} ===")
            self._start(Job("erase", port=port, baud=self.device.baud(),
                            reset_after=self.device.reset_check.isChecked()))

    def _start(self, job: Job) -> None:
        worker = EspWorker(job, self)
        worker.log_line.connect(self.log)
        worker.progress.connect(self.device.set_progress)
        worker.status.connect(self.device.set_status)
        worker.device_info.connect(self._device_info)
        worker.done.connect(self._job_done)
        worker.finished.connect(worker.deleteLater)
        self.worker = worker
        self._last_device_info = {}
        self._job_clock.start()
        self.device.set_busy(True, flashing=True)
        self._update_state()
        worker.start()

    def _job_done(self, ok: bool, message: str, hint: str) -> None:
        worker = self.worker
        kind = worker.job.kind if worker else ""
        self.worker = None
        self.device.set_busy(False)
        self.device.finish_progress(ok)
        self._update_state()
        if ok:
            self.log(message, "ok")
            self.device.set_status(f"✔ {message}")
            self.statusBar().showMessage(message, 15000)
            QApplication.alert(self)
            if kind == "flash" and worker is not None:
                self._celebrate(worker)
            return
        self.log(message, "error")
        if hint:
            self.log(hint, "hint")
        headline = message.splitlines()[0] if message else "Failed."
        self.device.set_status(f"✖ {headline}" + (f"\n\n{hint}" if hint else ""))
        if message != "Cancelled.":
            titles = {"flash": "Flashing Failed", "detect": "Could Not Detect Board",
                      "erase": "Erase Failed", "merge": "Export Failed"}
            box = QMessageBox(QMessageBox.Icon.Critical, titles.get(kind, "Error"), message,
                              QMessageBox.StandardButton.Ok, self)
            if hint:
                box.setInformativeText(hint)
            box.exec()

    def _celebrate(self, worker: EspWorker) -> None:
        """Success popup, with confetti all over the screen."""
        package = self.pkg.name if self.pkg and self.pkg.name else "The firmware"
        if self.pkg and self.pkg.version:
            package += f" {self.pkg.version}"
        if celebrate(self, package, worker, self._job_clock.elapsed() / 1000.0,
                     self._last_device_info, self.confetti_act.isChecked()):
            QTimer.singleShot(0, self.flash)

    def show_credits(self) -> None:
        CreditsDialog(self).exec()

    # ====================================================== standalone flashers
    def export_standalone(self) -> None:
        """Save the package as one .exe that opens the simple flasher window."""
        pkg = self.pkg
        if pkg is None or self.worker is not None:
            return
        runtime = standalone.find_runtime()
        if runtime is None:
            QMessageBox.warning(
                self, "Can't Export",
                "This copy of LaFlasher doesn't include the standalone runtime "
                f"({standalone.RUNTIME_NAME}).\n\nInstall LaFlasher with its installer, or build "
                "it with: python build.py")
            return
        self.files_tab.rescan_if_changed()
        dialog = ExportDialog(pkg, self.last_dir, runtime.stat().st_size, self)
        dialog.preview_requested.connect(lambda options: self._preview_standalone(options, dialog))
        if not dialog.exec():
            return
        options, out = dialog.options(), dialog.output_path()
        errors, _ = pkg.problems()
        if errors:
            answer = QMessageBox.warning(
                self, "Export Anyway?",
                "Flashing this package would fail right now:\n\n"
                + "\n".join(f"• {e}" for e in errors) + "\n\nExport it anyway?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if answer != QMessageBox.StandardButton.Yes:
                return
        if options != pkg.standalone:
            pkg.standalone = options  # remembered in laflash.json for the next export
            self._set_dirty(True)
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            size = standalone.write_standalone(runtime, pkg.archive_bytes(), out)
        except OSError as exc:
            QApplication.restoreOverrideCursor()
            QMessageBox.critical(self, "Export Failed", str(exc))
            return
        QApplication.restoreOverrideCursor()
        self.last_dir = str(out.parent)
        self.log(f"Exported standalone flasher {out} ({human_size(size)})", "ok")
        box = QMessageBox(QMessageBox.Icon.Information, "Standalone Flasher Ready",
                          f"Saved {out.name} ({human_size(size)}).", QMessageBox.StandardButton.Ok,
                          self)
        box.setInformativeText("Send it to anyone: they double-click it, plug in the board and "
                               "click Flash. Nothing needs to be installed.")
        show = box.addButton("Show in Folder", QMessageBox.ButtonRole.ActionRole)
        box.exec()
        if box.clickedButton() is show:
            if sys.platform == "win32":
                subprocess.Popen(["explorer", "/select,", str(out)])
            else:
                QDesktopServices.openUrl(QUrl.fromLocalFile(str(out.parent)))

    def _preview_standalone(self, options: dict, parent: QWidget) -> None:
        preview = SimpleWindow(self.pkg, options=options, owns_package=False, preview=True,
                               parent=parent)
        preview.setWindowModality(Qt.WindowModality.WindowModal)
        preview.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        preview.show()

    def _device_info(self, info: dict) -> None:
        self._last_device_info = info
        self.device.show_device_info(info)
        expected = self.pkg.target_chip() if self.pkg else None
        if expected and info.get("chip") and info["chip"] != expected:
            self.log(f"This board is {info.get('name')}, but the open package is for "
                     f"{chips.chip_label(expected)}.", "warn")

    def _port_appeared(self, device: str, description: str) -> None:
        self.statusBar().showMessage(f"Board connected on {device} ({description})", 8000)
        self.log(f"Board connected on {device} ({description})")

    # ================================================================= tools
    def export_fs_image(self) -> None:
        pkg = self.pkg
        target = pkg.fs_target() if pkg else None
        if target is None:
            QMessageBox.information(self, "No LittleFS Partition",
                                    "Choose a LittleFS partition in the LittleFS Files tab first.")
            return
        path, _ = QFileDialog.getSaveFileName(self, "Export LittleFS Image",
                                              str(Path(self.last_dir) / f"{pkg.name or 'littlefs'}-littlefs.bin"),
                                              "Binary image (*.bin)")
        if not path:
            return
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            result = fsimage.build(pkg.fs_dir, target[1], pkg.fs_params)
            Path(path).write_bytes(result.data)
        except Exception as exc:
            QApplication.restoreOverrideCursor()
            QMessageBox.critical(self, "Export Failed", str(exc))
            return
        QApplication.restoreOverrideCursor()
        self.last_dir = str(Path(path).parent)
        self.log(f"Exported LittleFS image to {path} (flash it at 0x{target[0]:x}; "
                 f"{human_size(result.used)} of {human_size(result.total)} used)", "ok")

    def export_merged(self) -> None:
        pkg = self.pkg
        if pkg is None or self.worker is not None:
            return
        chip = pkg.target_chip()
        if not chip:
            labels = [chips.chip_label(k) for k in chips.chip_keys()]
            choice, ok = QInputDialog.getItem(self, "Choose Chip",
                                              "Which ESP32 model is this firmware for?", labels, 0, False)
            if not ok:
                return
            chip = chips.chip_keys()[labels.index(choice)]
        errors, _ = pkg.problems(chip)
        errors += [f"LittleFS: {e}" for e in [self.files_tab.fs_error()] if e]
        if errors:
            QMessageBox.warning(self, "Can't Export Yet", "\n".join(f"• {e}" for e in errors))
            return
        path, _ = QFileDialog.getSaveFileName(self, "Export Merged Firmware",
                                              str(Path(self.last_dir) / f"{pkg.name or 'firmware'}-merged.bin"),
                                              "Binary image (*.bin)")
        if not path:
            return
        self.last_dir = str(Path(path).parent)
        job = self._make_job("merge")
        job.chip, job.output, job.otadata = chip, path, None
        self.log("")
        self.log(f"=== Exporting merged firmware for {chips.chip_label(chip)} ===")
        self._start(job)

    def associate_files(self) -> None:
        if sys.platform != "win32":
            return
        import ctypes
        import winreg
        if getattr(sys, "frozen", False):
            exe, command = sys.executable, f'"{sys.executable}" "%1"'
        else:
            pythonw = Path(sys.executable).with_name("pythonw.exe")
            exe = str(pythonw if pythonw.exists() else sys.executable)
            command = f'"{exe}" -m laflasher "%1"'
        answer = QMessageBox.question(
            self, "Open .laflash Files with LaFlasher",
            f"Make .laflash files open in LaFlasher when you double-click them?\n\n"
            f"This registers the file type for your Windows user account only:\n{command}")
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            base = r"Software\Classes"
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, rf"{base}\{EXTENSION}") as key:
                winreg.SetValue(key, "", winreg.REG_SZ, "LaFlasher.Package")
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, rf"{base}\LaFlasher.Package") as key:
                winreg.SetValue(key, "", winreg.REG_SZ, "LaFlasher firmware package")
                winreg.SetValue(key, "DefaultIcon", winreg.REG_SZ, f'"{exe}",0')
                winreg.SetValue(key, r"shell\open\command", winreg.REG_SZ, command)
            ctypes.windll.shell32.SHChangeNotify(0x08000000, 0, None, None)
        except OSError as exc:
            QMessageBox.critical(self, "Could Not Register", str(exc))
            return
        self.log(".laflash files now open with LaFlasher.", "ok")

    def save_log(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Save Log", str(Path(self.last_dir) / "laflasher-log.txt"),
                                              "Text files (*.txt)")
        if path:
            Path(path).write_text(self.log_view.toPlainText(), encoding="utf-8")

    def about(self) -> None:
        import esptool
        import littlefs
        from PyQt6.QtCore import QT_VERSION_STR
        QMessageBox.about(
            self, f"About {APP_NAME}",
            f"<h3>{APP_NAME} {__version__}</h3>"
            "<p>One-click flasher for .laflash ESP32 firmware packages.</p>"
            f"<p>Embedded esptool {esptool.__version__}<br>"
            f"Embedded LittleFS image builder (littlefs-python {getattr(littlefs, '__version__', '?')})<br>"
            f"Qt {QT_VERSION_STR}</p>")

    # ================================================================ events
    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls() and self.worker is None:
            event.acceptProposedAction()

    def dropEvent(self, event):
        paths = [u.toLocalFile() for u in event.mimeData().urls() if u.isLocalFile()]
        if not paths:
            return
        event.acceptProposedAction()
        first = paths[0]
        # Queue the work so Explorer isn't left waiting for the drop to finish.
        if first.lower().endswith(PACKAGE_SUFFIXES):
            QTimer.singleShot(0, lambda: self.open_package(first))
        elif os.path.isdir(first):
            QTimer.singleShot(0, lambda: self.import_folder(first))
        elif self.pkg is not None:
            QTimer.singleShot(0, lambda: self.firmware_tab.add_images(paths))
        else:
            QTimer.singleShot(0, lambda: self._new_from_files(paths))

    def _new_from_files(self, paths: list[str]) -> None:
        if not self._can_switch():
            return
        images = [Path(p) for p in paths if p.lower().endswith(".bin")]
        csv = next((Path(p) for p in paths if p.lower().endswith(".csv")), None)
        if not images:
            return
        try:
            pkg = LaFlashPackage.from_files(project_name(images[0].parent), images, csv)
        except Exception as exc:
            QMessageBox.critical(self, "Could Not Create Package", str(exc))
            return
        self._set_package(pkg, dirty=True)

    def changeEvent(self, event):
        super().changeEvent(event)
        if (event.type() == event.Type.ActivationChange and self.isActiveWindow()
                and self.pkg is not None and self.worker is None):
            self.files_tab.rescan_if_changed()

    def closeEvent(self, event):
        if self.worker is not None:
            answer = QMessageBox.warning(
                self, "Operation in Progress",
                "The board is being flashed. Stopping now can leave it unable to start until it is "
                "flashed again. Stop and quit?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self.worker.cancel()
            self.worker.wait(10000)
        if not self._maybe_save():
            event.ignore()
            return
        self._save_settings()
        if self.pkg is not None and self.pkg is not self._borrowed:
            self.pkg.close()
        self.closed.emit()
        event.accept()
