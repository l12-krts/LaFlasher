"""Right-hand panel: serial port, baud rate, options and the Flash button."""

from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QIntValidator
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QFormLayout, QGroupBox, QLabel, QProgressBar, QPushButton, QSizePolicy,
    QStyle, QVBoxLayout, QWidget,
)

from ..flasher import BAUD_RATES, DEFAULT_BAUD
from .port_picker import PortPicker


class DevicePanel(QWidget):
    flash_requested = pyqtSignal()
    detect_requested = pyqtSignal()
    port_appeared = pyqtSignal(str, str)  # device, description

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Preferred)
        self.setMinimumWidth(290)
        self.setMaximumWidth(340)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        board = QGroupBox("Board")
        form = QFormLayout(board)
        self.ports = PortPicker()
        self.ports.port_appeared.connect(self.port_appeared)
        self.port_combo = self.ports.combo
        self.refresh_btn = self.ports.refresh_btn
        form.addRow("Port:", self.ports)

        self.baud_combo = QComboBox()
        self.baud_combo.setEditable(True)
        self.baud_combo.setValidator(QIntValidator(9600, 5_000_000, self))
        for baud in BAUD_RATES:
            self.baud_combo.addItem(str(baud))
        self.set_baud(DEFAULT_BAUD)
        self.baud_combo.setToolTip("Transfer speed. If flashing fails, LaFlasher retries slower "
                                   "automatically.")
        form.addRow("Speed:", self.baud_combo)

        self.detect_btn = QPushButton("Detect Board")
        self.detect_btn.setToolTip("Connect to the board and show its chip, flash size and MAC "
                                   "address. Nothing is written.")
        self.detect_btn.clicked.connect(self.detect_requested)
        form.addRow(self.detect_btn)
        self.info_label = QLabel("Chip is detected automatically when you flash.")
        self.info_label.setWordWrap(True)
        self.info_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        form.addRow(self.info_label)
        layout.addWidget(board)

        options = QGroupBox("Options")
        opt_layout = QVBoxLayout(options)
        self.erase_check = QCheckBox("Erase entire flash first")
        self.erase_check.setToolTip("Wipes the whole flash chip before writing, including saved "
                                    "Wi-Fi settings and other NVS data. Slower.")
        self.reset_check = QCheckBox("Restart board when done")
        self.reset_check.setChecked(True)
        opt_layout.addWidget(self.erase_check)
        opt_layout.addWidget(self.reset_check)
        layout.addWidget(options)

        self.flash_btn = QPushButton("Flash")
        self.flash_btn.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_ArrowForward))
        self.flash_btn.setMinimumHeight(44)
        font = self.flash_btn.font()
        font.setPointSize(font.pointSize() + 3)
        font.setBold(True)
        self.flash_btn.setFont(font)
        self.flash_btn.setToolTip("Write the selected images and LittleFS files to the board (F5)")
        self.flash_btn.clicked.connect(self.flash_requested)
        layout.addWidget(self.flash_btn)

        self.progress = QProgressBar()
        self.progress.setRange(0, 1000)
        self.progress.setValue(0)
        self.progress.setTextVisible(True)
        self.progress.setFormat("%p%")
        layout.addWidget(self.progress)

        self.status_label = QLabel("Open a package to get started.")
        self.status_label.setWordWrap(True)
        self.status_label.setMinimumHeight(self.fontMetrics().height() * 3)
        layout.addWidget(self.status_label)
        layout.addStretch(1)

    # ------------------------------------------------------------------ ports
    def refresh_ports(self, force: bool = False) -> None:
        self.ports.refresh(force)

    def current_port(self) -> str | None:
        return self.ports.current_port()

    def set_port(self, device: str) -> None:
        self.ports.set_port(device)

    def baud(self) -> int:
        try:
            return max(9600, int(self.baud_combo.currentText()))
        except ValueError:
            return DEFAULT_BAUD

    def set_baud(self, baud: int) -> None:
        index = self.baud_combo.findText(str(baud))
        if index >= 0:
            self.baud_combo.setCurrentIndex(index)
        else:
            self.baud_combo.setEditText(str(baud))

    # ----------------------------------------------------------------- state
    def set_busy(self, busy: bool, flashing: bool = False) -> None:
        self.ports.set_busy(busy)
        for w in (self.baud_combo, self.detect_btn, self.erase_check, self.reset_check):
            w.setEnabled(not busy)
        self.flash_btn.setText("Cancel" if busy else "Flash")
        self.flash_btn.setIcon(self.style().standardIcon(
            QStyle.StandardPixmap.SP_DialogCancelButton if busy else QStyle.StandardPixmap.SP_ArrowForward))
        self.flash_btn.setEnabled(flashing or not busy)
        if busy:
            self.progress.setRange(0, 1000)
            self.progress.setValue(0)

    def set_progress(self, value: int, maximum: int) -> None:
        self.progress.setRange(0, maximum)
        if maximum:
            self.progress.setValue(value)

    def set_status(self, text: str) -> None:
        self.status_label.setText(text)

    def finish_progress(self, success: bool) -> None:
        self.progress.setRange(0, 1000)
        self.progress.setValue(1000 if success else 0)

    def show_device_info(self, info: dict) -> None:
        lines = [f"<b>{info.get('description') or info.get('name')}</b>"]
        if info.get("flash_size"):
            lines.append(f"Flash: {info['flash_size']}")
        if info.get("features"):
            lines.append(info["features"])
        if info.get("crystal"):
            lines.append(f"Crystal: {info['crystal']}")
        if info.get("mac"):
            lines.append(f"MAC: {info['mac']}")
        self.info_label.setText("<br>".join(lines))
