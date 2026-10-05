"""Firmware tab: the list of images that get written, with offsets and checkboxes."""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtWidgets import (
    QAbstractItemView, QFileDialog, QHBoxLayout, QHeaderView, QLabel, QMessageBox, QPushButton,
    QStyle, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from .. import chips
from ..package import BOOTLOADER, ROLE_LABELS, ROLE_PARTITIONS, LaFlashPackage
from ..util import hex_addr, human_size

PACKAGE_SUFFIXES = (".laflash", ".zip")


class _DropTable(QTableWidget):
    files_dropped = pyqtSignal(list)

    def __init__(self, rows: int, cols: int, parent=None):
        super().__init__(rows, cols, parent)
        self.setAcceptDrops(True)
        self.viewport().setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DropOnly)

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event):
        paths = [u.toLocalFile() for u in event.mimeData().urls() if u.isLocalFile()]
        event.acceptProposedAction()
        if paths:
            self.files_dropped.emit(paths)


class FirmwareTab(QWidget):
    changed = pyqtSignal()
    open_package_requested = pyqtSignal(str)
    show_files_requested = pyqtSignal()

    COLUMNS = ["Image", "Offset", "Size", "Type", "Partition"]

    def __init__(self, parent=None):
        super().__init__(parent)
        self.pkg: LaFlashPackage | None = None
        self.last_dir = ""
        self._rows: list = []
        self._updating = False

        layout = QVBoxLayout(self)
        intro = QLabel("Ticked images are written to the board when you click Flash. "
                       "Double-click an offset to change it, or drop .bin files here to add them.")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        self.table = _DropTable(0, len(self.COLUMNS))
        self.table.setHorizontalHeaderLabels(self.COLUMNS)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked
                                   | QAbstractItemView.EditTrigger.EditKeyPressed)
        self.table.setAlternatingRowColors(True)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for col in range(1, len(self.COLUMNS)):
            header.setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
        self.table.itemChanged.connect(self._item_changed)
        self.table.itemSelectionChanged.connect(self._update_buttons)
        self.table.cellDoubleClicked.connect(self._cell_double_clicked)
        self.table.files_dropped.connect(self.add_images)
        layout.addWidget(self.table, 1)

        buttons = QHBoxLayout()
        style = self.style()
        self.add_btn = QPushButton(style.standardIcon(QStyle.StandardPixmap.SP_FileIcon), "Add Image...")
        self.add_btn.setToolTip("Add a .bin image (or a partitions.csv) to the package")
        self.add_btn.clicked.connect(lambda: self.add_images())
        self.replace_btn = QPushButton(style.standardIcon(QStyle.StandardPixmap.SP_BrowserReload), "Replace...")
        self.replace_btn.setToolTip("Swap the selected image for a newer build, keeping its offset")
        self.replace_btn.clicked.connect(self.replace_selected)
        self.remove_btn = QPushButton(style.standardIcon(QStyle.StandardPixmap.SP_TrashIcon), "Remove")
        self.remove_btn.clicked.connect(self.remove_selected)
        for b in (self.add_btn, self.replace_btn, self.remove_btn):
            buttons.addWidget(b)
        buttons.addStretch(1)
        layout.addLayout(buttons)

        self.summary = QLabel()
        self.summary.setWordWrap(True)
        self.summary.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.summary)
        self._update_buttons()

    # ------------------------------------------------------------------ view
    def set_package(self, pkg: LaFlashPackage | None) -> None:
        self.pkg = pkg
        self.refresh()

    def refresh(self) -> None:
        self._updating = True
        self.table.setRowCount(0)
        self._rows = []
        pkg = self.pkg
        if pkg is None:
            self.summary.clear()
            self._updating = False
            return
        chip = pkg.target_chip()

        def sort_key(img):
            if img.offset == BOOTLOADER:
                return -1
            return img.offset if isinstance(img.offset, int) else 1 << 40

        for img in sorted(pkg.images, key=sort_key):
            path = pkg.root / img.file
            size = path.stat().st_size if path.exists() else None
            part = pkg.partition_at(img.offset)
            if part:
                used = f", {size * 100 // part.size}% full" if size and part.size else ""
                part_text = f"{part.name} ({human_size(part.size)}{used})"
            elif img.role == ROLE_PARTITIONS:
                part_text = "partition table"
            else:
                part_text = ""
            role = ROLE_LABELS.get(img.role, img.role)
            image_chip = chips.chip_from_image(path) if img.role in ("app", "bootloader", "merged") else None
            if image_chip:
                role += f" ({chips.chip_label(image_chip)})"
            offset_tip = ("Double-click to change. Type 'bootloader' for the chip-dependent "
                          f"bootloader offset ({chips.bootloader_offsets_hint()}).")
            self._add_row(img, img.name, img.enabled, img.offset_text(chip), size, role, part_text,
                          f"{img.file} inside the package", offset_tip, editable_offset=True)

        if pkg.fs_dir.is_dir():
            target = pkg.fs_target()
            count = pkg.fs_file_count()
            self._add_row(None, f"LittleFS files ({count} file{'s' if count != 1 else ''})",
                          pkg.fs_enabled, hex_addr(target[0]) if target else "not set",
                          target[1] if target else None, "LittleFS image, built when flashing",
                          target[2] if target else "choose in the LittleFS tab",
                          "Double-click to open the LittleFS tab",
                          "The LittleFS offset comes from the partition chosen in the LittleFS tab",
                          editable_offset=False)
        self._updating = False
        self._update_summary()
        self._update_buttons()

    def _add_row(self, img, name, enabled, offset, size, role, part, name_tip, offset_tip,
                 editable_offset: bool) -> None:
        row = self.table.rowCount()
        self.table.insertRow(row)
        self._rows.append(img)
        icon = self.style().standardIcon(QStyle.StandardPixmap.SP_DirIcon if img is None
                                         else QStyle.StandardPixmap.SP_FileIcon)
        name_item = QTableWidgetItem(icon, name)
        name_item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
                           | Qt.ItemFlag.ItemIsUserCheckable)
        name_item.setCheckState(Qt.CheckState.Checked if enabled else Qt.CheckState.Unchecked)
        name_item.setToolTip(name_tip)
        offset_item = QTableWidgetItem(offset)
        flags = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        offset_item.setFlags(flags | Qt.ItemFlag.ItemIsEditable if editable_offset else flags)
        offset_item.setToolTip(offset_tip)
        size_item = QTableWidgetItem(human_size(size) if size is not None else "missing")
        size_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        cells = [name_item, offset_item, size_item, QTableWidgetItem(role), QTableWidgetItem(part)]
        for col, item in enumerate(cells):
            if col >= 2:
                item.setFlags(flags)
            self.table.setItem(row, col, item)

    def _update_summary(self) -> None:
        errors, warnings = self.pkg.problems()
        lines = [f"⚠ {e}" for e in errors] + [f"ℹ {w}" for w in warnings]
        if not errors:
            count = sum(1 for i in self.pkg.images if i.enabled) + (1 if self.pkg.fs_will_flash() else 0)
            lines.insert(0, f"✔ Ready to flash {count} image{'s' if count != 1 else ''}.")
        self.summary.setText("\n".join(lines))

    def _selected(self):
        rows = self.table.selectionModel().selectedRows() if self.table.selectionModel() else []
        if not rows:
            return None, False
        index = rows[0].row()
        return self._rows[index], True

    def _update_buttons(self) -> None:
        img, has = self._selected()
        self.add_btn.setEnabled(self.pkg is not None)
        self.replace_btn.setEnabled(has and img is not None)
        self.remove_btn.setEnabled(has and img is not None)

    # --------------------------------------------------------------- editing
    def _item_changed(self, item: QTableWidgetItem) -> None:
        if self._updating or self.pkg is None:
            return
        img = self._rows[item.row()]
        if item.column() == 0:
            checked = item.checkState() == Qt.CheckState.Checked
            if img is None:
                self.pkg.fs_enabled = checked
            elif checked and img.offset is None:
                QMessageBox.information(self, "Offset needed",
                                        f"Set a flash offset for {img.name} first (double-click "
                                        "its Offset cell).")
            else:
                img.enabled = checked
        elif item.column() == 1 and img is not None:
            try:
                self.pkg.set_image_offset(img, item.text())
            except ValueError:
                QMessageBox.warning(self, "Invalid offset",
                                    "Enter a hexadecimal offset such as 0x10000, or 'bootloader'.")
        QTimer.singleShot(0, self.changed.emit)  # rebuilding the table inside its own signal is unsafe

    def _cell_double_clicked(self, row: int, column: int) -> None:
        if self._rows[row] is None:
            self.show_files_requested.emit()

    def add_images(self, paths: list[str] | None = None) -> None:
        if self.pkg is None:
            return
        if paths is None:
            paths, _ = QFileDialog.getOpenFileNames(
                self, "Add Firmware Images", self.last_dir,
                "Firmware images (*.bin);;Partition table (*.csv);;All files (*)")
        if not paths:
            return
        self.last_dir = str(Path(paths[0]).parent)
        unknown = []
        for path in paths:
            if path.lower().endswith(PACKAGE_SUFFIXES):
                self.open_package_requested.emit(path)
                return
            if Path(path).is_dir():
                continue
            existing = self.pkg.image_named(Path(path).name)
            if existing:
                answer = QMessageBox.question(
                    self, "Replace Image?",
                    f"The package already contains {existing.name}. Replace it with the new file?")
                if answer == QMessageBox.StandardButton.Yes:
                    self.pkg.replace_image(existing, path)
                continue
            img = self.pkg.add_image(path)
            if img is not None and img.offset is None:
                unknown.append(img.name)
        self.changed.emit()
        if unknown:
            QMessageBox.information(
                self, "Set the Offset",
                "LaFlasher couldn't tell where to flash:\n\n" + "\n".join(unknown) +
                "\n\nDouble-click the Offset cell and type the address (e.g. 0x10000), then tick it.")

    def replace_selected(self) -> None:
        img, has = self._selected()
        if img is None:
            return
        path, _ = QFileDialog.getOpenFileName(self, f"Replace {img.name}", self.last_dir,
                                              "Firmware images (*.bin);;All files (*)")
        if path:
            self.last_dir = str(Path(path).parent)
            self.pkg.replace_image(img, path)
            self.changed.emit()

    def remove_selected(self) -> None:
        img, has = self._selected()
        if img is None:
            return
        answer = QMessageBox.question(self, "Remove Image",
                                      f"Remove {img.name} from the package?")
        if answer == QMessageBox.StandardButton.Yes:
            self.pkg.remove_image(img)
            self.changed.emit()
