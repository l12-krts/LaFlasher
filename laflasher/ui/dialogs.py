"""Small dialogs: text editor for LittleFS files, format help and credits."""

from __future__ import annotations

import html
from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFontDatabase, QKeySequence, QPixmap, QShortcut
from PyQt6.QtWidgets import (
    QDialog, QDialogButtonBox, QGridLayout, QGroupBox, QHBoxLayout, QLabel, QMessageBox,
    QPlainTextEdit, QTextBrowser, QVBoxLayout,
)

from .. import APP_NAME, __version__
from ..icon import render

MAX_TEXT_SIZE = 2 * 1024 * 1024

CREDITS = [
    ("lab.krts", "https://krts.dev/laflasher"),
    ("DrBust", "https://drbust.dev/projects/#laflasher"),
]
BUILT_WITH = [
    ("esptool", "GPL-2.0-or-later", "https://github.com/espressif/esptool"),
    ("littlefs-python", "BSD-3-Clause", "https://github.com/jrast/littlefs-python"),
    ("PyQt6 / Qt", "GPL-3.0", "https://www.riverbankcomputing.com/software/pyqt/"),
    ("pySerial", "BSD-3-Clause", "https://github.com/pyserial/pyserial"),
]


def _link(url: str) -> QLabel:
    shown = url.split("://", 1)[-1].rstrip("/")
    label = QLabel(f'<a href="{html.escape(url)}">{html.escape(shown)}</a>')
    label.setOpenExternalLinks(True)
    label.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
    label.setToolTip(url)
    return label


def is_text_file(path: Path) -> bool:
    try:
        if path.stat().st_size > MAX_TEXT_SIZE:
            return False
        data = path.read_bytes()
    except OSError:
        return False
    if b"\0" in data[:8192]:
        return False
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


class TextEditorDialog(QDialog):
    def __init__(self, path: Path, parent=None):
        super().__init__(parent)
        self.path = path
        raw = path.read_bytes()
        self._crlf = b"\r\n" in raw
        self._bom = raw.startswith(b"\xef\xbb\xbf")
        text = raw.decode("utf-8-sig")
        self.setWindowTitle(f"Edit {path.name}")
        self.resize(820, 600)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(f"Editing <b>{path.name}</b> in the LittleFS files. "
                                "Save writes it into the package."))
        self.editor = QPlainTextEdit()
        self.editor.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self.editor.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.editor.setPlainText(text.replace("\r\n", "\n"))
        self.editor.document().setModified(False)
        layout.addWidget(self.editor, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save
                                   | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        QShortcut(QKeySequence.StandardKey.Save, self, activated=self.accept)

    def accept(self) -> None:
        text = self.editor.toPlainText()
        if self._crlf:
            text = text.replace("\n", "\r\n")
        data = text.encode("utf-8")
        if self._bom:
            data = b"\xef\xbb\xbf" + data
        if self.path.suffix.lower() == ".json":
            import json
            try:
                json.loads(data.decode("utf-8-sig") or "null")
            except ValueError as exc:
                answer = QMessageBox.question(
                    self, "Invalid JSON",
                    f"This doesn't look like valid JSON:\n{exc}\n\nSave anyway?")
                if answer != QMessageBox.StandardButton.Yes:
                    return
        try:
            self.path.write_bytes(data)
        except OSError as exc:
            QMessageBox.warning(self, "Could Not Save", str(exc))
            return
        super().accept()

    def reject(self) -> None:
        if self.editor.document().isModified():
            answer = QMessageBox.question(self, "Discard Changes?",
                                          f"Discard your changes to {self.path.name}?")
            if answer != QMessageBox.StandardButton.Yes:
                return
        super().reject()


FORMAT_HELP = """
<h3>What is a .laflash package?</h3>
<p>A <b>.laflash</b> file is a normal ZIP archive with a different extension. You can make one
with LaFlasher (<i>File &gt; Import from Build Folder</i>, then <i>File &gt; Save</i>) or by hand
with any ZIP tool.</p>
<pre>
my-project.laflash
 +-- laflash.json        optional: name, chip, offsets, settings
 +-- bootloader.bin
 +-- partitions.bin      (or partitions.csv)
 +-- boot_app0.bin       optional OTA data
 +-- firmware.bin        your application
 +-- littlefs/           files for the LittleFS partition
      +-- index.html
      +-- config.json
</pre>
<h3>Where images are flashed</h3>
<ul>
<li><b>bootloader*.bin</b> goes to 0x0, 0x1000 or 0x2000 depending on the chip that is connected.</li>
<li><b>partition*.bin</b> goes to 0x8000.</li>
<li><b>boot_app0.bin / ota_data*.bin</b> go to the <i>otadata</i> partition.</li>
<li>The application goes to the first app partition (factory or ota_0).</li>
<li>A file whose name matches a partition label (e.g. <i>nvs.bin</i>) goes to that partition.</li>
<li>A hex address in the name (<i>0x10000_app.bin</i>) is always used as-is.</li>
<li><b>laflash.json</b> can list every image with an exact offset instead.</li>
</ul>
<h3>LittleFS</h3>
<p>Everything in the <b>littlefs/</b> folder is packed into a LittleFS image (4 KB blocks, like
<i>mklittlefs</i>) sized to the <i>spiffs</i> or <i>littlefs</i> partition from the partition table,
and written together with the firmware. A prebuilt <i>littlefs.bin</i> is unpacked automatically so
its files can be edited.</p>
<h3>laflash.json example</h3>
<pre>
{
  "name": "Weather Station",
  "version": "1.2.0",
  "chip": "esp32s3",
  "flash": {"mode": "keep", "freq": "keep", "size": "keep", "baud": 921600},
  "images": [
    {"file": "bootloader.bin", "offset": "bootloader"},
    {"file": "partitions.bin", "offset": "0x8000"},
    {"file": "boot_app0.bin",  "offset": "0xe000"},
    {"file": "firmware.bin",   "offset": "0x10000"}
  ],
  "littlefs": {"folder": "littlefs", "partition": "spiffs"}
}
</pre>
"""


class CreditsDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Credits")
        layout = QVBoxLayout(self)

        header = QHBoxLayout()
        icon = QLabel()
        ratio = self.devicePixelRatioF()
        pixmap = QPixmap.fromImage(render(int(64 * ratio)))
        pixmap.setDevicePixelRatio(ratio)
        icon.setPixmap(pixmap)
        header.addWidget(icon)
        title = QLabel(f"<b>{APP_NAME}</b> {__version__}<br>One-click flasher for ESP32 .laflash packages")
        font = title.font()
        font.setPointSize(font.pointSize() + 2)
        title.setFont(font)
        header.addWidget(title, 1)
        layout.addLayout(header)

        credits = QGroupBox("Credits")
        grid = QGridLayout(credits)
        grid.setHorizontalSpacing(24)
        for row, (name, url) in enumerate(CREDITS):
            name_label = QLabel(f"<b>{html.escape(name)}</b>")
            grid.addWidget(name_label, row, 0)
            grid.addWidget(_link(url), row, 1)
        grid.setColumnStretch(1, 1)
        layout.addWidget(credits)

        built = QGroupBox("Built with")
        grid = QGridLayout(built)
        grid.setHorizontalSpacing(24)
        for row, (name, license_name, url) in enumerate(BUILT_WITH):
            grid.addWidget(QLabel(name), row, 0)
            grid.addWidget(QLabel(license_name), row, 1)
            grid.addWidget(_link(url), row, 2)
        grid.setColumnStretch(2, 1)
        layout.addWidget(built)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.setMinimumWidth(520)


class FormatHelpDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("About .laflash Packages")
        self.resize(640, 640)
        layout = QVBoxLayout(self)
        browser = QTextBrowser()
        browser.setHtml(FORMAT_HELP)
        layout.addWidget(browser)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
