"""Export Standalone Flasher: choose what people see, then save the .exe."""

from __future__ import annotations

import re
from pathlib import Path

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QHBoxLayout,
    QLabel, QLineEdit, QMessageBox, QPushButton, QVBoxLayout,
)

from ..settings_doc import load_settings
from ..util import human_size


def _file_name(title: str) -> str:
    safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", title).strip(" .")
    return f"{safe or 'Firmware'} Flasher.exe"


class ExportDialog(QDialog):
    preview_requested = pyqtSignal(dict)

    def __init__(self, pkg, last_dir: str, runtime_size: int, parent=None):
        super().__init__(parent)
        self.pkg = pkg
        self.setWindowTitle("Export Standalone Flasher")
        self.setMinimumWidth(560)
        options = pkg.standalone
        layout = QVBoxLayout(self)
        intro = QLabel("Make one <b>.exe</b> that anyone can run to flash this package. LaFlasher is "
                       "built into it, so nothing needs to be installed: they get a small window "
                       "with the settings you choose and a big Flash button.")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        form = QFormLayout()
        self.title_edit = QLineEdit(options.get("title") or pkg.name or "")
        self.title_edit.setPlaceholderText("Shown at the top of the window")
        form.addRow("Title:", self.title_edit)

        self.settings_combo = QComboBox()
        self.settings_combo.addItem("Don't show settings", "")
        for rel in pkg.settings_candidates():
            self.settings_combo.addItem(rel, rel)
        current = pkg.settings_file()
        index = self.settings_combo.findData(current) if current else 0
        self.settings_combo.setCurrentIndex(max(index, 0))
        self.settings_combo.currentIndexChanged.connect(self._check_settings)
        self.settings_note = QLabel()
        self.settings_note.setWordWrap(True)
        form.addRow("Settings form:", self.settings_combo)
        form.addRow("", self.settings_note)

        self.files_check = QCheckBox("People can add, replace and remove the files on the board")
        self.files_check.setChecked(options.get("allow_files", True))
        self.editor_check = QCheckBox('Show an "Open Full Editor" button')
        self.editor_check.setChecked(options.get("allow_editor", True))
        self.editor_check.setToolTip("Lets people switch to the complete LaFlasher window")
        form.addRow("Allow:", self.files_check)
        form.addRow("", self.editor_check)

        path_row = QHBoxLayout()
        default_name = _file_name(self.title_edit.text())
        self.path_edit = QLineEdit(str(Path(last_dir or Path.home()) / default_name))
        browse = QPushButton("Browse...")
        browse.clicked.connect(self._browse)
        path_row.addWidget(self.path_edit, 1)
        path_row.addWidget(browse)
        form.addRow("Save as:", path_row)
        layout.addLayout(form)

        size = QLabel(f"The .exe will be about {human_size(runtime_size + self._package_size())}. "
                      "Windows may ask people to confirm before running a downloaded .exe "
                      "(More info > Run anyway).")
        size.setWordWrap(True)
        layout.addWidget(size)

        buttons = QDialogButtonBox()
        preview = buttons.addButton("Preview...", QDialogButtonBox.ButtonRole.ActionRole)
        preview.setToolTip("Open the window people will see, using this package")
        preview.clicked.connect(lambda: self.preview_requested.emit(self.options()))
        export = buttons.addButton("Export", QDialogButtonBox.ButtonRole.AcceptRole)
        export.setDefault(True)
        buttons.addButton(QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.title_edit.textEdited.connect(self._title_edited)
        self._check_settings()

    def _package_size(self) -> int:
        return sum(p.stat().st_size for p in self.pkg.root.rglob("*") if p.is_file())

    def _title_edited(self, title: str) -> None:
        path = Path(self.path_edit.text())
        if path.name.endswith(" Flasher.exe"):  # still the suggested name: keep it in step
            self.path_edit.setText(str(path.with_name(_file_name(title))))

    def _check_settings(self) -> None:
        rel = self.settings_combo.currentData()
        if not rel:
            self.settings_note.setText("The window shows no settings, only Connect and Flash.")
            return
        try:
            fields = load_settings(self.pkg.fs_dir / rel).fields
        except (OSError, ValueError) as exc:
            self.settings_note.setText(f"⚠ Can't be shown as a form: {exc}")
            return
        names = ", ".join(f.label for f in fields[:4]) + ("..." if len(fields) > 4 else "")
        self.settings_note.setText(f"✔ Shows {len(fields)} setting"
                                   f"{'s' if len(fields) != 1 else ''}: {names}")

    def _browse(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Save Standalone Flasher", self.path_edit.text(),
                                              "Programs (*.exe)")
        if path:
            self.path_edit.setText(path)

    def options(self) -> dict:
        return {
            "title": self.title_edit.text().strip(),
            "settings_file": self.settings_combo.currentData() or "",
            "allow_files": self.files_check.isChecked(),
            "allow_editor": self.editor_check.isChecked(),
        }

    def output_path(self) -> Path:
        path = Path(self.path_edit.text().strip())
        return path if path.suffix.lower() == ".exe" else path.with_name(path.name + ".exe")

    def accept(self) -> None:
        if not self.path_edit.text().strip():
            QMessageBox.warning(self, "Export", "Choose where to save the .exe.")
            return
        path = self.output_path()
        if not path.parent.is_dir():
            QMessageBox.warning(self, "Export", f"The folder {path.parent} doesn't exist.")
            return
        if path.exists():
            answer = QMessageBox.question(self, "Replace File?",
                                          f"{path.name} already exists. Replace it?")
            if answer != QMessageBox.StandardButton.Yes:
                return
        super().accept()
