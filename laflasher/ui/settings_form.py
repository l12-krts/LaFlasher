"""A settings file (JSON or KEY=value) edited as a plain form."""

from __future__ import annotations

import json
from pathlib import Path

from PyQt6.QtCore import QLocale, QRegularExpression, QTimer, pyqtSignal
from PyQt6.QtGui import QDoubleValidator, QRegularExpressionValidator
from PyQt6.QtWidgets import (
    QCheckBox, QFormLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QSpinBox, QToolButton,
    QToolTip, QVBoxLayout, QWidget,
)

from ..settings_doc import Field, load_settings

_INT32 = 2 ** 31 - 1


class SettingsForm(QWidget):
    """Shows one settings file as labelled fields. Edits are written back to the file."""

    changed = pyqtSignal()  # the file on disk was updated

    def __init__(self, parent=None):
        super().__init__(parent)
        self.path: Path | None = None
        self.doc = None
        self._written: bytes | None = None
        self._dirty = False
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._content: QWidget | None = None
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(400)
        self._save_timer.timeout.connect(self.flush)

    # ------------------------------------------------------------------ loading
    def load(self, path: Path) -> str | None:
        """Show `path` as a form. Returns None on success, else why it can't be shown."""
        self.flush()
        self.path, self.doc = Path(path), None
        self._replace_content(QWidget())
        try:
            doc = load_settings(path)
            written = self.path.read_bytes()
        except (OSError, ValueError) as exc:
            return str(exc)
        if not doc.fields:
            return f"{self.path.name} has no settings to show."
        self.doc, self._written = doc, written
        self._build()
        return None

    def reload_if_changed(self) -> None:
        """Pick up changes made to the file elsewhere (file list, full editor)."""
        if self.path is None:
            return
        try:
            current = self.path.read_bytes()
        except OSError:
            return
        if not self._dirty and current != self._written:
            self.load(self.path)

    def _replace_content(self, widget: QWidget) -> None:
        if self._content is not None:
            self._layout.removeWidget(self._content)
            self._content.deleteLater()
        self._content = widget
        self._layout.addWidget(widget)

    def _build(self) -> None:
        content = QWidget()
        column = QVBoxLayout(content)
        column.setContentsMargins(0, 0, 0, 0)
        sections: dict[str, list[Field]] = {"": []}
        for field in self.doc.fields:
            sections.setdefault(field.section, []).append(field)
        for title, fields in sections.items():
            if not fields:
                continue
            if title:
                box = QGroupBox(title)
                form = QFormLayout(box)
                column.addWidget(box)
            else:
                form = QFormLayout()
                column.addLayout(form)
            form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
            for field in fields:
                label = QLabel(field.label + ":")
                label.setToolTip(f"{field.name} in {self.path.name}")
                form.addRow(label, self._editor(field))
        column.addStretch(1)  # spare height goes below the fields, not into the last group
        self._replace_content(content)

    # ------------------------------------------------------------------ editors
    def _editor(self, field: Field) -> QWidget:
        tip = f"{field.name} in {self.path.name}"
        if field.kind == "bool":
            box = QCheckBox()
            box.setChecked(bool(field.value))
            box.toggled.connect(lambda checked, f=field: self._set(f, checked))
            box.setToolTip(tip)
            return box
        if field.kind == "int" and -_INT32 <= field.value <= _INT32:
            spin = QSpinBox()
            spin.setRange(-_INT32, _INT32)
            spin.setValue(field.value)
            spin.setAccelerated(True)
            spin.valueChanged.connect(lambda value, f=field: self._set(f, value))
            spin.setToolTip(tip)
            return spin

        edit = QLineEdit()
        edit.setToolTip(tip)
        if field.kind == "int":
            edit.setText(str(field.value))
            edit.setValidator(QRegularExpressionValidator(QRegularExpression(r"-?\d+"), edit))
            edit.textEdited.connect(lambda text, f=field: self._set_number(f, text, int))
        elif field.kind == "float":
            edit.setText(repr(field.value))
            validator = QDoubleValidator(edit)
            validator.setLocale(QLocale.c())  # always a dot, like the file
            edit.setValidator(validator)
            edit.textEdited.connect(lambda text, f=field: self._set_number(f, text, float))
        elif field.kind == "json":
            edit.setText(json.dumps(field.value, ensure_ascii=False))
            edit.setToolTip(tip + " (JSON value)")
            edit.editingFinished.connect(lambda e=edit, f=field: self._set_json(f, e))
        else:  # text, or null (empty until something is typed)
            edit.setText("" if field.value is None else str(field.value))
            if field.kind == "null":
                edit.setPlaceholderText("(empty)")
            edit.textEdited.connect(lambda text, f=field: self._set_text(f, text))
        if not field.secret:
            return edit
        edit.setEchoMode(QLineEdit.EchoMode.Password)
        show = QToolButton()
        show.setText("Show")
        show.setCheckable(True)
        show.setToolTip("Show or hide the text")
        show.toggled.connect(lambda on: edit.setEchoMode(
            QLineEdit.EchoMode.Normal if on else QLineEdit.EchoMode.Password))
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(edit, 1)
        layout.addWidget(show)
        return row

    def _set(self, field: Field, value) -> None:
        self.doc.set(field, value)
        self._dirty = True
        self._save_timer.start()

    def _set_text(self, field: Field, text: str) -> None:
        self._set(field, None if field.kind == "null" and text == "" else text)

    def _set_number(self, field: Field, text: str, convert) -> None:
        try:
            self._set(field, convert(text))
        except ValueError:
            pass  # half-typed number such as "-" or "1e"

    def _set_json(self, field: Field, edit: QLineEdit) -> None:
        try:
            value = json.loads(edit.text())
        except ValueError:
            QToolTip.showText(edit.mapToGlobal(edit.rect().bottomLeft()),
                              "That isn't a valid value, so it wasn't changed.", edit)
            edit.setText(json.dumps(field.value, ensure_ascii=False))
            return
        if value != field.value:
            self._set(field, value)

    # ------------------------------------------------------------------- saving
    def flush(self) -> None:
        """Write pending edits to the file now."""
        self._save_timer.stop()
        if not self._dirty or self.doc is None:
            return
        data = self.doc.to_bytes()
        try:
            self.path.write_bytes(data)
        except OSError:
            return  # keep the edits pending; the next change retries
        self._written, self._dirty = data, False
        self.changed.emit()
