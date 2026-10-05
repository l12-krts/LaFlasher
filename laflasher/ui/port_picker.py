"""Serial port chooser that keeps itself up to date and picks newly plugged-in boards."""

from __future__ import annotations

from PyQt6.QtCore import QTimer, pyqtSignal
from PyQt6.QtWidgets import QComboBox, QHBoxLayout, QSizePolicy, QStyle, QToolButton, QWidget

from ..flasher import list_serial_ports

NO_BOARD_TEXT = "No ESP32 board found - plug one in"


class PortPicker(QWidget):
    port_appeared = pyqtSignal(str, str)  # device, description
    changed = pyqtSignal()                # the selected port or the list changed

    def __init__(self, parent=None, poll_ms: int = 1500):
        super().__init__(parent)
        self._known: list[tuple[str, str, bool]] | None = None
        self._busy = False
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        self.combo = QComboBox()
        self.combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.combo.setToolTip("Serial port of the ESP32 board. Plug the board in and it "
                              "is selected automatically.")
        self.combo.currentIndexChanged.connect(lambda _: self.changed.emit())
        self.refresh_btn = QToolButton()
        self.refresh_btn.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_BrowserReload))
        self.refresh_btn.setToolTip("Refresh the port list")
        self.refresh_btn.clicked.connect(lambda: self.refresh(force=True))
        row.addWidget(self.combo)
        row.addWidget(self.refresh_btn)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self.refresh)
        self._timer.start(poll_ms)
        self.refresh()

    def refresh(self, force: bool = False) -> None:
        if self._busy and not force:
            return
        ports = list_serial_ports()
        if ports == self._known and not force:
            return
        previous = self.current_port()
        new = [p for p in ports if self._known is not None
               and p[0] not in {k[0] for k in self._known}]
        self._known = ports
        self.combo.blockSignals(True)
        self.combo.clear()
        if not self.has_board():
            # Don't pre-select some unrelated serial port; make the user choose it.
            self.combo.addItem(NO_BOARD_TEXT, None)
        for device, desc, _ in ports:
            self.combo.addItem(f"{device}  -  {desc}" if desc != device else device, device)
        target = new[0][0] if new else previous
        index = self.combo.findData(target) if target else -1
        self.combo.setCurrentIndex(max(index, 0))  # likely boards are sorted first
        self.combo.setEnabled(not self._busy)
        self.combo.blockSignals(False)
        self.changed.emit()
        if new:
            self.port_appeared.emit(new[0][0], new[0][1])

    def has_board(self) -> bool:
        """True if a port that looks like an ESP32 board's USB chip is connected."""
        return any(likely for *_, likely in (self._known or []))

    def current_port(self) -> str | None:
        return self.combo.currentData()

    def current_description(self) -> str:
        device = self.current_port()
        return next((desc for dev, desc, _ in (self._known or []) if dev == device), "")

    def set_port(self, device: str) -> None:
        index = self.combo.findData(device)
        if index >= 0:
            self.combo.setCurrentIndex(index)

    def set_busy(self, busy: bool) -> None:
        self._busy = busy
        self.combo.setEnabled(not busy)
        self.refresh_btn.setEnabled(not busy)
