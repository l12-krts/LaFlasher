"""Partition table tab (read-only overview)."""

from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QAbstractItemView, QHeaderView, QLabel, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from ..package import BOOTLOADER, LaFlashPackage
from ..util import hex_addr, human_size


class PartitionsTab(QWidget):
    COLUMNS = ["Name", "Type", "Subtype", "Offset", "Size", "Flags", "Written by this package"]

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        self.source_label = QLabel()
        self.source_label.setWordWrap(True)
        layout.addWidget(self.source_label)
        self.table = QTableWidget(0, len(self.COLUMNS))
        self.table.setHorizontalHeaderLabels(self.COLUMNS)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setAlternatingRowColors(True)
        header = self.table.horizontalHeader()
        for col in range(len(self.COLUMNS) - 1):
            header.setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(len(self.COLUMNS) - 1, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.table, 1)
        note = QLabel("To use a different partition layout, replace the partition table image in "
                      "the Firmware tab (a partitions.csv works too).")
        note.setWordWrap(True)
        layout.addWidget(note)

    def set_package(self, pkg: LaFlashPackage | None) -> None:
        self.table.setRowCount(0)
        if pkg is None:
            self.source_label.clear()
            return
        if pkg.partition_error:
            self.source_label.setText(f"⚠ Could not read the partition table: {pkg.partition_error}")
        elif not pkg.partitions:
            self.source_label.setText("This package has no partition table. Offsets must be set "
                                      "by hand in the Firmware tab.")
        else:
            self.source_label.setText(f"Partition table from <b>{pkg.partition_file}</b>, "
                                      f"written at {hex_addr(0x8000)}.")

        writers: dict[int, list[str]] = {}
        for img in pkg.images:
            if img.enabled and isinstance(img.offset, int) and img.offset != BOOTLOADER:
                writers.setdefault(img.offset, []).append(img.name)
        if pkg.fs_will_flash():
            writers.setdefault(pkg.fs_target()[0], []).append("LittleFS files")
        otadata = pkg.otadata_to_reset()
        if otadata:
            writers.setdefault(otadata[0], []).append("cleared, so the new app boots")

        for p in pkg.partitions:
            row = self.table.rowCount()
            self.table.insertRow(row)
            values = [p.name, p.type_name, p.subtype_name, hex_addr(p.offset),
                      f"{human_size(p.size)} ({hex_addr(p.size)})", p.flags_text,
                      ", ".join(writers.get(p.offset, [])) or "-"]
            for col, value in enumerate(values):
                item = QTableWidgetItem(value)
                if col in (3, 4):
                    item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                self.table.setItem(row, col, item)
