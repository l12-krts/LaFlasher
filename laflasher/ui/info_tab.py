"""Package info tab: metadata, target chip and flash settings stored in laflash.json."""

from __future__ import annotations

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import (
    QComboBox, QFormLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit, QSpinBox,
    QVBoxLayout, QWidget,
)

from .. import chips
from ..flasher import BAUD_RATES
from ..fsimage import DISK_VERSIONS
from ..package import LaFlashPackage

FLASH_MODES = ["keep", "qio", "qout", "dio", "dout"]
FLASH_FREQS = ["keep", "80m", "60m", "48m", "40m", "30m", "26m", "24m", "20m", "16m", "15m", "12m"]
FLASH_SIZES = ["keep", "detect"] + chips.FLASH_SIZES


class InfoTab(QWidget):
    changed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.pkg: LaFlashPackage | None = None
        self._loading = False
        layout = QVBoxLayout(self)

        about = QGroupBox("Package")
        form = QFormLayout(about)
        self.name_edit = QLineEdit()
        self.version_edit = QLineEdit()
        self.version_edit.setPlaceholderText("e.g. 1.0.0")
        self.desc_edit = QPlainTextEdit()
        self.desc_edit.setMaximumHeight(self.fontMetrics().height() * 5)
        self.desc_edit.setPlaceholderText("Shown to whoever flashes this package")
        form.addRow("Name:", self.name_edit)
        form.addRow("Version:", self.version_edit)
        form.addRow("Description:", self.desc_edit)
        layout.addWidget(about)

        target = QGroupBox("Target")
        tform = QFormLayout(target)
        self.chip_combo = QComboBox()
        self.chip_combo.addItem("Any ESP32 (don't check)", "")
        for key in chips.chip_keys():
            self.chip_combo.addItem(chips.chip_label(key), key)
        self.chip_combo.setToolTip("Flashing stops if the connected board is a different model.")
        self.detected_label = QLabel()
        chip_row = QHBoxLayout()
        chip_row.addWidget(self.chip_combo)
        chip_row.addWidget(self.detected_label, 1)
        tform.addRow("Chip:", chip_row)
        self.baud_combo = QComboBox()
        self.baud_combo.addItem("Use the app setting", 0)
        for baud in BAUD_RATES:
            self.baud_combo.addItem(str(baud), baud)
        tform.addRow("Flash speed:", self.baud_combo)
        layout.addWidget(target)

        flash = QGroupBox("Flash chip settings (written into the bootloader header)")
        fform = QFormLayout(flash)
        self.mode_combo = self._combo(FLASH_MODES)
        self.freq_combo = self._combo(FLASH_FREQS)
        self.size_combo = self._combo(FLASH_SIZES)
        self.bootloader_size_label = QLabel()
        size_row = QHBoxLayout()
        size_row.addWidget(self.size_combo)
        size_row.addWidget(self.bootloader_size_label, 1)
        fform.addRow("Mode:", self.mode_combo)
        fform.addRow("Frequency:", self.freq_combo)
        fform.addRow("Size:", size_row)
        fform.addRow(QLabel("Leave these on \"keep\" unless you know the board needs something "
                            "else; \"detect\" sets the size to the real flash chip."))
        layout.addWidget(flash)

        lfs = QGroupBox("LittleFS image format")
        lform = QFormLayout(lfs)
        self.block_spin = QSpinBox()
        self.block_spin.setRange(512, 65536)
        self.block_spin.setSingleStep(512)
        self.block_spin.setSuffix(" bytes")
        self.disk_combo = QComboBox()
        for version in DISK_VERSIONS:
            label = f"{version} (works with every ESP32 core)" if version == "2.0" else version
            self.disk_combo.addItem(label, version)
        self.name_spin = QSpinBox()
        self.name_spin.setRange(16, 1022)
        lform.addRow("Block size:", self.block_spin)
        lform.addRow("Disk version:", self.disk_combo)
        lform.addRow("Max name length:", self.name_spin)
        layout.addWidget(lfs)
        layout.addStretch(1)

        self.name_edit.textEdited.connect(self._store)
        self.version_edit.textEdited.connect(self._store)
        self.desc_edit.textChanged.connect(self._store)
        for combo in (self.chip_combo, self.baud_combo, self.mode_combo, self.freq_combo,
                      self.size_combo, self.disk_combo):
            combo.currentIndexChanged.connect(self._store)
        self.block_spin.valueChanged.connect(self._store)
        self.name_spin.valueChanged.connect(self._store)
        self.setEnabled(False)

    @staticmethod
    def _combo(values: list[str]) -> QComboBox:
        combo = QComboBox()
        for value in values:
            combo.addItem(value, value)
        return combo

    @staticmethod
    def _select(combo: QComboBox, value) -> None:
        index = combo.findData(value)
        if index < 0:
            combo.addItem(str(value), value)
            index = combo.count() - 1
        combo.setCurrentIndex(index)

    def set_package(self, pkg: LaFlashPackage | None) -> None:
        self.pkg = pkg
        self.setEnabled(pkg is not None)
        if pkg is None:
            return
        self._loading = True
        self.name_edit.setText(pkg.name)
        self.version_edit.setText(pkg.version)
        self.desc_edit.setPlainText(pkg.description)
        self._select(self.chip_combo, pkg.chip)
        self._select(self.baud_combo, pkg.baud or 0)
        self._select(self.mode_combo, pkg.flash_mode)
        self._select(self.freq_combo, pkg.flash_freq)
        self._select(self.size_combo, pkg.flash_size)
        self.block_spin.setValue(pkg.fs_params.block_size)
        self._select(self.disk_combo, pkg.fs_params.disk_version)
        self.name_spin.setValue(pkg.fs_params.name_max)
        self._loading = False
        self.refresh_detected()

    def refresh_detected(self) -> None:
        if self.pkg is None:
            return
        detected = self.pkg.detected_chip()
        self.detected_label.setText(f"Firmware was built for {chips.chip_label(detected)}."
                                    if detected else "")
        size = self.pkg.bootloader_flash_size()
        self.bootloader_size_label.setText(f"Bootloader is set to {size}." if size else "")

    def _store(self, *_):
        if self._loading or self.pkg is None:
            return
        pkg = self.pkg
        pkg.name = self.name_edit.text().strip()
        pkg.version = self.version_edit.text().strip()
        pkg.description = self.desc_edit.toPlainText().strip()
        pkg.chip = self.chip_combo.currentData() or ""
        pkg.baud = self.baud_combo.currentData() or None
        pkg.flash_mode = self.mode_combo.currentData()
        pkg.flash_freq = self.freq_combo.currentData()
        pkg.flash_size = self.size_combo.currentData()
        pkg.fs_params.block_size = self.block_spin.value()
        pkg.fs_params.disk_version = self.disk_combo.currentData()
        pkg.fs_params.name_max = self.name_spin.value()
        self.changed.emit()
