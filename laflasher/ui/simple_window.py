"""The small window of a standalone flasher: connect, check the settings, flash."""

from __future__ import annotations

import html

from PyQt6.QtCore import QElapsedTimer, QSettings, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QFontDatabase, QIcon, QPixmap
from PyQt6.QtWidgets import (
    QAbstractScrollArea, QApplication, QCheckBox, QDialog, QDialogButtonBox, QFrame, QGroupBox,
    QHBoxLayout, QLabel, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QScrollArea,
    QSizePolicy, QToolButton, QVBoxLayout, QWidget,
)

from .. import chips
from ..flasher import DEFAULT_BAUD, EspWorker, build_flash_job
from ..icon import app_icon, render
from ..util import human_size
from .celebrate import celebrate
from .files_tab import FilesTab
from .port_picker import PortPicker
from .settings_form import SettingsForm

PLUG_IN_HELP = ("Connect your board to this computer with a USB cable. If it doesn't show up, "
                "try another cable (some only charge) or install the driver for the board's "
                "USB chip (CP210x or CH340).")


class FilesDialog(QDialog):
    """'Change Files...': the files that go onto the board, without the expert options."""

    def __init__(self, pkg, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Files on the Board")
        layout = QVBoxLayout(self)
        intro = QLabel("These files are written to the board's storage when you flash.")
        intro.setWordWrap(True)
        layout.addWidget(intro)
        self.files = FilesTab(simple=True)
        self.files.set_package(pkg)
        layout.addWidget(self.files, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.resize(560, 500)

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() == event.Type.ActivationChange and self.isActiveWindow():
            self.files.rescan_if_changed()  # edits made in other apps


class SimpleWindow(QWidget):
    """What people see when they run a standalone flasher exported from LaFlasher."""

    closed = pyqtSignal()

    def __init__(self, pkg, options: dict | None = None, owns_package: bool = True,
                 preview: bool = False, parent=None):
        super().__init__(parent, Qt.WindowType.Window)
        self.pkg = pkg
        self.options = dict(pkg.standalone if options is None else options)
        self._owns_package = owns_package
        self._preview = preview
        self.worker: EspWorker | None = None
        self.editor = None
        self._clock = QElapsedTimer()
        self._device_info: dict = {}
        self._settings_shown = False
        self.settings = QSettings()
        self.setWindowIcon(app_icon())
        self.setMinimumWidth(480)

        layout = QVBoxLayout(self)
        if preview:
            note = QLabel("<b>Preview:</b> this is what people see when they run the exported .exe. "
                          "Changes made here also change your package.")
            note.setWordWrap(True)
            layout.addWidget(note)
            line = QFrame()
            line.setFrameShape(QFrame.Shape.HLine)
            layout.addWidget(line)

        # Header: what this flasher installs
        header = QHBoxLayout()
        icon = QLabel()
        ratio = self.devicePixelRatioF()
        pixmap = QPixmap.fromImage(render(int(56 * ratio)))
        pixmap.setDevicePixelRatio(ratio)
        icon.setPixmap(pixmap)
        icon.setAlignment(Qt.AlignmentFlag.AlignTop)
        header.addWidget(icon)
        text = QVBoxLayout()
        self.title_label = QLabel()
        font = self.title_label.font()
        font.setPointSize(font.pointSize() + 6)
        font.setBold(True)
        self.title_label.setFont(font)
        self.title_label.setWordWrap(True)
        self.subtitle_label = QLabel()
        self.description_label = QLabel()
        self.description_label.setWordWrap(True)
        for label in (self.title_label, self.subtitle_label, self.description_label):
            text.addWidget(label)
        header.addLayout(text, 1)
        layout.addLayout(header)

        # Step: connect the board
        self.board_box = QGroupBox()
        board = QVBoxLayout(self.board_box)
        self.ports = PortPicker()
        self.ports.changed.connect(self._update_board_hint)
        board.addWidget(self.ports)
        self.board_hint = QLabel()
        self.board_hint.setWordWrap(True)
        board.addWidget(self.board_hint)
        layout.addWidget(self.board_box)

        # Step: settings (only when the package has a settings file)
        self.settings_box = QGroupBox()
        settings_layout = QVBoxLayout(self.settings_box)
        self.form = SettingsForm()
        self.form.changed.connect(self._update_files_summary)
        self.form_scroll = QScrollArea()
        self.form_scroll.setWidget(self.form)
        self.form_scroll.setWidgetResizable(True)
        self.form_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.form_scroll.setSizeAdjustPolicy(QAbstractScrollArea.SizeAdjustPolicy.AdjustToContents)
        self.form_scroll.setMaximumHeight(320)
        # As tall as the fields need (scrolling past 320 px), never stretched by spare height.
        self.form_scroll.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        self.form_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        settings_layout.addWidget(self.form_scroll)
        self.settings_note = QLabel()
        self.settings_note.setWordWrap(True)
        settings_layout.addWidget(self.settings_note)
        layout.addWidget(self.settings_box)

        # Step: flash
        self.flash_box = QGroupBox()
        flash = QVBoxLayout(self.flash_box)
        files_row = QHBoxLayout()
        self.files_label = QLabel()
        self.files_label.setWordWrap(True)
        files_row.addWidget(self.files_label, 1)
        self.files_btn = QPushButton("Change Files...")
        self.files_btn.setToolTip("Add, replace or remove the files stored on the board")
        self.files_btn.clicked.connect(self.change_files)
        files_row.addWidget(self.files_btn)
        flash.addLayout(files_row)
        self.erase_check = QCheckBox("Erase everything on the board first")
        self.erase_check.setToolTip("Also deletes saved Wi-Fi settings and anything else stored on "
                                    "the board. Use this if the board misbehaves after flashing.")
        flash.addWidget(self.erase_check)
        self.flash_btn = QPushButton("Flash")
        self.flash_btn.setIcon(QIcon(QPixmap.fromImage(render(64))))
        self.flash_btn.setMinimumHeight(50)
        big = self.flash_btn.font()
        big.setPointSize(big.pointSize() + 5)
        big.setBold(True)
        self.flash_btn.setFont(big)
        self.flash_btn.setDefault(True)
        self.flash_btn.clicked.connect(self.flash)
        flash.addWidget(self.flash_btn)
        self.progress = QProgressBar()
        self.progress.setRange(0, 1000)
        self.progress.setVisible(False)
        flash.addWidget(self.progress)
        self.status_label = QLabel()
        self.status_label.setWordWrap(True)
        flash.addWidget(self.status_label)
        layout.addWidget(self.flash_box)
        layout.addStretch(0)  # spare height when the details are closed

        # Footer: details for troubleshooting, and the way into the full editor
        footer = QHBoxLayout()
        self.details_btn = QToolButton()
        self.details_btn.setText("Details")
        self.details_btn.setCheckable(True)
        self.details_btn.setArrowType(Qt.ArrowType.RightArrow)
        self.details_btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.details_btn.setAutoRaise(True)
        self.details_btn.toggled.connect(self._toggle_details)
        footer.addWidget(self.details_btn)
        footer.addStretch(1)
        self.editor_btn = QPushButton("Open Full Editor...")
        self.editor_btn.setToolTip("All of LaFlasher's options: firmware images, partitions, "
                                   "storage settings and more")
        self.editor_btn.clicked.connect(self.open_editor)
        footer.addWidget(self.editor_btn)
        layout.addLayout(footer)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self.log.setMaximumBlockCount(5000)
        self.log.setMinimumHeight(150)
        self.log.setVisible(False)
        layout.addWidget(self.log, 1)

        self.refresh()
        self.resize(540, self.sizeHint().height())

    def fit_height(self) -> None:
        """Shrink or grow to exactly fit the content at the current width."""
        layout = self.layout()
        if layout.hasHeightForWidth():  # wrapped labels: height depends on width
            height = layout.totalHeightForWidth(self.width())
        else:
            height = layout.totalSizeHint().height()
        self.resize(self.width(), max(height, self.minimumSizeHint().height()))

    def showEvent(self, event):
        super().showEvent(event)
        QTimer.singleShot(0, self.fit_height)  # wrapped text has its real height only once shown

    # ----------------------------------------------------------------- display
    def refresh(self) -> None:
        """Show the package as it is now (also after the full editor changed it)."""
        pkg = self.pkg
        title = self.options.get("title") or pkg.name or "Firmware"
        self.setWindowTitle(("Preview - " if self._preview else "") + title)
        self.title_label.setText(html.escape(title))
        chip = pkg.target_chip()
        facts = [f"Version {pkg.version}" if pkg.version else "",
                 f"For {chips.chip_label(chip)}" if chip else ""]
        self.subtitle_label.setText(" · ".join(f for f in facts if f))
        self.subtitle_label.setVisible(any(facts))
        self.description_label.setText(pkg.description)
        self.description_label.setVisible(bool(pkg.description))

        rel = pkg.settings_file(self.options)
        explicit = bool(self.options.get("settings_file"))
        if rel:
            problem = self.form.load(pkg.fs_dir / rel)
        else:
            problem = f"{self.options.get('settings_file')} is missing." if explicit else "none"
        self._settings_shown = problem is None or explicit
        self.form_scroll.setVisible(problem is None)
        self.settings_note.setText("" if problem is None else
                                   f"⚠ The settings can't be shown: {problem}")
        self.settings_note.setVisible(problem is not None and explicit)
        self.settings_box.setVisible(self._settings_shown)

        self.editor_btn.setVisible(self.options.get("allow_editor", True) and not self._preview)
        self._update_files_summary()
        self._number_steps()
        self._update_board_hint()

    def _number_steps(self) -> None:
        steps = [(self.board_box, "Connect your board")]
        if self._settings_shown:
            steps.append((self.settings_box, "Check the settings"))
        steps.append((self.flash_box, "Flash"))
        for number, (box, title) in enumerate(steps, 1):
            box.setTitle(f"{number}.  {title}")

    def _update_board_hint(self) -> None:
        port = self.ports.current_port()
        if port and self.ports.has_board():
            text = f"✔ Board found: {self.ports.current_description() or port} on {port}."
        elif port:
            text = f"Using {port}."
        else:
            text = PLUG_IN_HELP
        self.board_hint.setText(text)

    def _update_files_summary(self) -> None:
        pkg = self.pkg
        allow_changes = self.options.get("allow_files", True)
        has_files = pkg.fs_dir.is_dir()
        can_write = pkg.fs_target() is not None
        visible = has_files or (allow_changes and can_write)
        self.files_label.setVisible(visible)
        self.files_btn.setVisible(visible and allow_changes and can_write)
        if not visible:
            return
        count = pkg.fs_file_count() if has_files else 0
        if pkg.fs_will_flash():
            text = (f"Files: {count} file{'s' if count != 1 else ''} "
                    f"({human_size(pkg.fs_total_size())}) are written to the board.")
        elif has_files and not can_write:
            text = "Files: this firmware has no storage partition for files."
        else:
            text = "Files: the files already on the board are kept."
        self.files_label.setText(text)

    def _toggle_details(self, shown: bool) -> None:
        self.details_btn.setArrowType(Qt.ArrowType.DownArrow if shown else Qt.ArrowType.RightArrow)
        self.log.setVisible(shown)
        if not shown:
            QTimer.singleShot(0, self.fit_height)

    def _log(self, text: str, level: str = "info") -> None:
        prefix = {"warn": "⚠ ", "error": "✖ ", "ok": "✔ "}.get(level, "")
        self.log.appendPlainText(prefix + text)

    # ----------------------------------------------------------------- actions
    def change_files(self) -> None:
        self.form.flush()
        FilesDialog(self.pkg, self).exec()
        self.form.reload_if_changed()
        self._update_files_summary()

    def open_editor(self) -> None:
        self.form.flush()
        from .main_window import MainWindow
        self.editor = MainWindow(package=self.pkg, embedded=True)
        self.editor.closed.connect(self._editor_closed)
        self.editor.show()
        self.hide()

    def _editor_closed(self) -> None:
        # Runs while the editor is closing, so this window is visible again before it's gone
        # (otherwise Qt would see no windows left and quit).
        self.editor = None
        self.refresh()
        self.show()
        self.raise_()
        self.activateWindow()

    def _set_busy(self, busy: bool) -> None:
        self.ports.set_busy(busy)
        for widget in (self.settings_box, self.files_btn, self.erase_check, self.editor_btn):
            widget.setEnabled(not busy)
        self.flash_btn.setText("Cancel" if busy else "Flash")
        if busy:
            self.progress.setRange(0, 1000)
            self.progress.setValue(0)
            self.progress.setVisible(True)

    def flash(self) -> None:
        if self.worker is not None:
            self.worker.cancel()
            self.status_label.setText("Cancelling...")
            return
        self.form.flush()
        errors, warnings = self.pkg.problems()
        if errors:
            QMessageBox.warning(self, "Can't Flash",
                                "This firmware package has a problem:\n\n"
                                + "\n".join(f"• {e}" for e in errors)
                                + "\n\nPlease contact whoever gave you this flasher.")
            return
        port = self.ports.current_port()
        if not port:
            QMessageBox.information(self, "Plug In Your Board", PLUG_IN_HELP)
            return
        if self.erase_check.isChecked():
            answer = QMessageBox.warning(
                self, "Erase Everything?",
                "Everything on the board will be erased first, including saved Wi-Fi settings "
                "and other stored data. Continue?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if answer != QMessageBox.StandardButton.Yes:
                return
        job = build_flash_job(self.pkg, port, self.pkg.baud or DEFAULT_BAUD,
                              erase_all=self.erase_check.isChecked(), reset_after=True)
        self._log("")
        self._log(f"=== Flashing to {port} ===")
        for warning in warnings:
            self._log(warning, "warn")
        worker = EspWorker(job, self)
        worker.log_line.connect(self._log)
        worker.progress.connect(self._progress)
        worker.status.connect(self.status_label.setText)
        worker.device_info.connect(self._got_device_info)
        worker.done.connect(self._done)
        worker.finished.connect(worker.deleteLater)
        self.worker = worker
        self._device_info = {}
        self._clock.start()
        self._set_busy(True)
        worker.start()

    def _progress(self, value: int, maximum: int) -> None:
        self.progress.setRange(0, maximum)
        if maximum:
            self.progress.setValue(value)

    def _got_device_info(self, info: dict) -> None:
        self._device_info = info

    def _done(self, ok: bool, message: str, hint: str) -> None:
        worker, self.worker = self.worker, None
        self._set_busy(False)
        if ok:
            self.progress.setRange(0, 1000)
            self.progress.setValue(1000)
            self.status_label.setText(f"✔ {message}")
            self._log(message, "ok")
            QApplication.alert(self)
            name = self.options.get("title") or self.pkg.name or "The firmware"
            confetti = self.settings.value("confetti", True, type=bool)
            if worker is not None and celebrate(self, name, worker, self._clock.elapsed() / 1000.0,
                                                self._device_info, confetti):
                QTimer.singleShot(0, self.flash)
            return
        self.progress.setVisible(False)
        headline = message.splitlines()[0] if message else "Failed."
        self.status_label.setText(f"✖ {headline}" + (f"\n{hint}" if hint else ""))
        self._log(message, "error")
        if hint:
            self._log(hint)
        if message != "Cancelled.":
            box = QMessageBox(QMessageBox.Icon.Critical, "Flashing Failed", headline,
                              QMessageBox.StandardButton.Ok, self)
            box.setInformativeText(hint or "Open Details below for more information.")
            box.exec()

    # ------------------------------------------------------------------ events
    def changeEvent(self, event):
        super().changeEvent(event)
        if (event.type() == event.Type.ActivationChange and self.isActiveWindow()
                and self.worker is None):
            self.form.reload_if_changed()
            self._update_files_summary()

    def closeEvent(self, event):
        if self.worker is not None:
            answer = QMessageBox.warning(
                self, "Flashing in Progress",
                "The board is being flashed. If you stop now it may not start until it is "
                "flashed again. Stop and close?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self.worker.cancel()
            self.worker.wait(10000)
        self.form.flush()
        if self._owns_package:
            self.pkg.close()
        self.closed.emit()
        event.accept()
