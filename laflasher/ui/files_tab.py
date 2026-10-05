"""LittleFS tab: edit the files that go into the LittleFS partition."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from PyQt6.QtCore import QFileInfo, QMimeData, QPoint, Qt, QThread, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import QDesktopServices, QDrag
from PyQt6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QFileDialog, QFileIconProvider, QGridLayout,
    QGroupBox, QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit, QMenu, QMessageBox,
    QProgressBar, QPushButton, QStyle, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from .. import fsimage
from ..package import LaFlashPackage
from ..util import hex_addr, human_size, parse_int
from .dialogs import TextEditorDialog, is_text_file

PATH_ROLE = Qt.ItemDataRole.UserRole
CUSTOM = "__custom__"
_BAD_NAME_CHARS = set('<>:"/\\|?*')


class _UsageWorker(QThread):
    """Builds the image in the background to measure exactly how much space it needs."""
    result = pyqtSignal(object, int)  # FsResult or exception, generation it was started for

    def __init__(self, src: Path, size: int, params, generation: int, parent=None):
        super().__init__(parent)
        self.src, self.size, self.params, self.generation = src, size, params, generation

    def run(self):
        try:
            self.result.emit(fsimage.measure(self.src, self.size, self.params), self.generation)
        except Exception as exc:  # reported in the usage label
            self.result.emit(exc, self.generation)


class FileTree(QTreeWidget):
    paths_dropped = pyqtSignal(list, str)   # files from outside, destination folder
    items_moved = pyqtSignal(list, str)     # files moved inside the tree
    delete_pressed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.root_dir = ""
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.viewport().setAcceptDrops(True)
        self.setDropIndicatorShown(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DragDrop)
        self.setEditTriggers(QAbstractItemView.EditTrigger.EditKeyPressed)

    def startDrag(self, actions):
        items = self.selectedItems()
        if not items:
            return
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(i.data(0, PATH_ROLE)) for i in items])
        drag = QDrag(self)
        drag.setMimeData(mime)
        # Copy by default so dragging out to Explorer never deletes the package's files.
        drag.exec(Qt.DropAction.CopyAction | Qt.DropAction.MoveAction, Qt.DropAction.CopyAction)

    def _target_dir(self, pos: QPoint) -> str:
        item = self.itemAt(pos)
        if item is None:
            return self.root_dir
        path = item.data(0, PATH_ROLE)
        return path if os.path.isdir(path) else os.path.dirname(path)

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        if not event.mimeData().hasUrls():
            event.ignore()
            return
        event.setDropAction(Qt.DropAction.MoveAction if event.source() is self
                            else Qt.DropAction.CopyAction)
        event.accept()

    def dropEvent(self, event):
        paths = [u.toLocalFile() for u in event.mimeData().urls() if u.isLocalFile()]
        target = self._target_dir(event.position().toPoint())
        internal = event.source() is self
        event.setDropAction(Qt.DropAction.CopyAction)
        event.accept()
        if paths:
            (self.items_moved if internal else self.paths_dropped).emit(paths, target)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Delete and self.state() != QAbstractItemView.State.EditingState:
            self.delete_pressed.emit()
            return
        super().keyPressEvent(event)


class FilesTab(QWidget):
    changed = pyqtSignal()
    open_package_requested = pyqtSignal(str)

    def __init__(self, parent=None, simple: bool = False):
        """`simple` is the cut-down version used by standalone flashers: no partition
        settings, just the files with Add / Edit / Replace / Remove."""
        super().__init__(parent)
        self.simple = simple
        self.pkg: LaFlashPackage | None = None
        self.last_dir = ""
        self.last_usage = None
        self._populating = False
        self._snapshot: dict = {}
        self._usage_worker: _UsageWorker | None = None
        # Bumped on every change; a size check started before the latest change is outdated.
        self._usage_generation = 0
        self._icons = QFileIconProvider()
        self._usage_timer = QTimer(self)
        self._usage_timer.setSingleShot(True)
        self._usage_timer.setInterval(300)
        self._usage_timer.timeout.connect(self._start_usage)

        layout = QVBoxLayout(self)

        target_box = QGroupBox("LittleFS partition")
        grid = QGridLayout(target_box)
        self.partition_combo = QComboBox()
        self.partition_combo.setToolTip("Where the LittleFS image is written. Taken from the "
                                        "package's partition table.")
        self.partition_combo.activated.connect(self._partition_chosen)
        self.offset_edit = QLineEdit()
        self.offset_edit.setPlaceholderText("0x290000")
        self.size_edit = QLineEdit()
        self.size_edit.setPlaceholderText("0x160000")
        for edit in (self.offset_edit, self.size_edit):
            edit.setMaximumWidth(110)
            edit.editingFinished.connect(self._custom_edited)
        self.enabled_check = QCheckBox("Write LittleFS when flashing")
        self.enabled_check.setToolTip("Untick to flash only the firmware and keep the files "
                                      "already on the board.")
        self.enabled_check.toggled.connect(self._enabled_toggled)
        grid.addWidget(QLabel("Partition:"), 0, 0)
        grid.addWidget(self.partition_combo, 0, 1)
        grid.addWidget(QLabel("Offset:"), 0, 2)
        grid.addWidget(self.offset_edit, 0, 3)
        grid.addWidget(QLabel("Size:"), 0, 4)
        grid.addWidget(self.size_edit, 0, 5)
        if not simple:
            grid.addWidget(self.enabled_check, 0, 6)
        grid.setColumnStretch(1, 1)
        layout.addWidget(target_box)
        target_box.setVisible(not simple)

        body = QVBoxLayout() if simple else QHBoxLayout()
        self.tree = FileTree()
        self.tree.setHeaderLabels(["Name", "Size"])
        self.tree.header().setStretchLastSection(False)
        self.tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._context_menu)
        self.tree.itemActivated.connect(self._item_activated)
        self.tree.itemChanged.connect(self._item_renamed)
        self.tree.itemSelectionChanged.connect(self._update_buttons)
        self.tree.paths_dropped.connect(self.add_paths)
        self.tree.items_moved.connect(self.move_paths)
        self.tree.delete_pressed.connect(self.remove_selected)
        body.addWidget(self.tree, 1)

        sp = QStyle.StandardPixmap
        buttons = QHBoxLayout() if simple else QVBoxLayout()
        self.add_files_btn = self._button("Add Files...", sp.SP_FileIcon, self.add_files_dialog,
                                          "Copy files from your computer into the selected folder")
        self.add_folder_btn = self._button("Add Folder...", sp.SP_DirIcon, self.add_folder_dialog,
                                           "Copy a whole folder into the selected folder")
        self.new_folder_btn = self._button("New Folder", sp.SP_FileDialogNewFolder, self.new_folder)
        self.new_file_btn = self._button("New Text File...", sp.SP_FileDialogDetailedView,
                                         self.new_text_file)
        self.edit_btn = self._button("Edit", sp.SP_FileDialogContentsView, self.edit_selected,
                                     "Edit a text file (JSON, HTML, config...) or open it in its app")
        self.rename_btn = self._button("Rename", sp.SP_FileLinkIcon, self.rename_selected, "F2")
        self.replace_btn = self._button("Replace...", sp.SP_BrowserReload, self.replace_selected,
                                        "Overwrite the selected file with another file, keeping its name")
        self.remove_btn = self._button("Remove", sp.SP_TrashIcon, self.remove_selected, "Delete key")
        self.extract_btn = self._button("Extract...", sp.SP_DialogSaveButton, self.extract_selected,
                                        "Save the selected files (or everything) to your computer")
        self.explorer_btn = self._button("Show in Explorer", sp.SP_DirOpenIcon, self.open_in_explorer,
                                         "Open the working folder. Changes made there are picked up "
                                         "when you come back to LaFlasher.")
        if simple:  # the rest stays reachable from the right-click menu
            shown = (self.add_files_btn, self.edit_btn, self.replace_btn, self.remove_btn)
        else:
            shown = (self.add_files_btn, self.add_folder_btn, self.new_folder_btn, self.new_file_btn,
                     None, self.edit_btn, self.rename_btn, self.replace_btn, self.remove_btn, None,
                     self.extract_btn, self.explorer_btn)
        for b in shown:
            if b is None:
                buttons.addSpacing(10)
            else:
                buttons.addWidget(b)
        buttons.addStretch(1)
        body.addLayout(buttons)
        layout.addLayout(body, 1)

        usage_row = QHBoxLayout()
        usage_row.addWidget(QLabel("Space used:"))
        self.usage_bar = QProgressBar()
        self.usage_bar.setRange(0, 1)
        self.usage_bar.setValue(0)
        usage_row.addWidget(self.usage_bar, 1)
        layout.addLayout(usage_row)
        self.usage_label = QLabel()
        self.usage_label.setWordWrap(True)
        layout.addWidget(self.usage_label)
        if simple:
            self.enabled_check.setText("Write these files to the board")
            self.enabled_check.setToolTip("Untick to keep the files that are already on the board "
                                          "and only update the firmware.")
            layout.addWidget(self.enabled_check)
            tip = QLabel("Drag files here to add them. Double-click a file to change it.")
        else:
            tip = QLabel("Tip: drag files and folders here from Explorer. Double-click a text file "
                         "to edit it. Changes are flashed right away; use File > Save to keep them "
                         "in the package.")
        tip.setWordWrap(True)
        layout.addWidget(tip)
        self._update_buttons()

    def _button(self, text, pixmap, slot, tip: str = "") -> QPushButton:
        button = QPushButton(self.style().standardIcon(pixmap), text)
        button.clicked.connect(slot)
        if tip:
            button.setToolTip(tip)
        return button

    # -------------------------------------------------------------- package
    def set_package(self, pkg: LaFlashPackage | None) -> None:
        if self._usage_worker is not None:
            # Let a size check of the previous package finish before its files are deleted.
            self._usage_worker.wait(5000)
        self.pkg = pkg
        self.last_usage = None
        self._snapshot = {}
        self.refresh(expand_all=True)

    def refresh(self, expand_all: bool = False) -> None:
        self._refresh_target()
        self._populate(expand_all)
        self._snapshot = self._take_snapshot()
        self._update_buttons()
        self._schedule_usage()

    def _refresh_target(self) -> None:
        pkg = self.pkg
        self.partition_combo.blockSignals(True)
        self.enabled_check.blockSignals(True)
        self.partition_combo.clear()
        if pkg is None:
            self.partition_combo.blockSignals(False)
            self.enabled_check.blockSignals(False)
            return
        for p in pkg.fs_candidates():
            self.partition_combo.addItem(f"{p.name}  ({p.subtype_name}, {hex_addr(p.offset)}, "
                                         f"{human_size(p.size)})", p.name)
        self.partition_combo.addItem("Custom offset and size...", CUSTOM)
        target = pkg.fs_target()
        if pkg.fs_custom or target is None:
            index = self.partition_combo.findData(CUSTOM)
        else:
            index = self.partition_combo.findData(target[2])
        self.partition_combo.setCurrentIndex(max(index, 0))
        custom = self.partition_combo.currentData() == CUSTOM
        self.offset_edit.setEnabled(custom)
        self.size_edit.setEnabled(custom)
        self.offset_edit.setText(hex_addr(target[0]) if target else "")
        self.size_edit.setText(hex_addr(target[1]) if target else "")
        self.enabled_check.setChecked(pkg.fs_enabled and pkg.fs_dir.is_dir())
        self.partition_combo.blockSignals(False)
        self.enabled_check.blockSignals(False)

    def _partition_chosen(self, index: int) -> None:
        data = self.partition_combo.itemData(index)
        if data == CUSTOM:
            target = self.pkg.fs_target()
            if target:
                self.pkg.fs_custom = (target[0], target[1])
            self.offset_edit.setEnabled(True)
            self.size_edit.setEnabled(True)
            self.offset_edit.setFocus()
        else:
            self.pkg.fs_custom = None
            self.pkg.fs_partition = data
        self._target_changed()

    def _custom_edited(self) -> None:
        if self.pkg is None or self.partition_combo.currentData() != CUSTOM:
            return
        try:
            offset = parse_int(self.offset_edit.text())
            size = parse_int(self.size_edit.text())
        except ValueError:
            return
        if (offset, size) != self.pkg.fs_custom:
            self.pkg.fs_custom = (offset, size)
            self._target_changed()

    def _enabled_toggled(self, checked: bool) -> None:
        if self.pkg is None:
            return
        if checked:
            self.pkg.fs_dir.mkdir(parents=True, exist_ok=True)
        self.pkg.fs_enabled = checked
        self._files_changed()

    def _target_changed(self) -> None:
        for img in self.pkg.images:
            if img.role == "filesystem" and img.auto:
                img.offset = None
        self.pkg.resolve_offsets()
        self._schedule_usage()
        self.changed.emit()

    # ----------------------------------------------------------------- tree
    def _expanded_paths(self) -> set[str]:
        result = set()

        def walk(item):
            for i in range(item.childCount()):
                child = item.child(i)
                if child.isExpanded():
                    result.add(child.data(0, PATH_ROLE))
                walk(child)
        walk(self.tree.invisibleRootItem())
        return result

    def _populate(self, expand_all: bool = False) -> None:
        expanded = self._expanded_paths()
        selected = {i.data(0, PATH_ROLE) for i in self.tree.selectedItems()}
        self._populating = True
        self.tree.clear()
        root = self.pkg.fs_dir if self.pkg else None
        self.tree.root_dir = str(root) if root else ""
        if root and root.is_dir():
            self._add_children(self.tree.invisibleRootItem(), root, expanded, selected, expand_all)
        self._populating = False
        if self.tree.topLevelItemCount() == 0 and self.pkg:
            placeholder = QTreeWidgetItem(["(empty) - add files or drag them here", ""])
            placeholder.setFlags(Qt.ItemFlag.NoItemFlags)
            placeholder.setData(0, PATH_ROLE, str(root))
            self._populating = True
            self.tree.addTopLevelItem(placeholder)
            self._populating = False

    def _add_children(self, parent, folder: Path, expanded, selected, expand_all) -> int:
        try:
            entries = sorted(folder.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        except OSError:
            return 0
        count = 0
        for path in entries:
            if path.name.lower() in fsimage.JUNK_FILES:
                continue
            item = QTreeWidgetItem([path.name, ""])
            item.setIcon(0, self._icons.icon(QFileInfo(str(path))))
            item.setData(0, PATH_ROLE, str(path))
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEditable)
            item.setTextAlignment(1, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            parent.addChild(item)
            if path.is_dir():
                n = self._add_children(item, path, expanded, selected, expand_all)
                item.setText(1, f"{n} item{'s' if n != 1 else ''}")
                item.setExpanded(expand_all or str(path) in expanded)
            else:
                item.setText(1, human_size(path.stat().st_size))
            item.setSelected(str(path) in selected)
            count += 1
        return count

    def _take_snapshot(self) -> dict:
        snap = {}
        if self.pkg and self.pkg.fs_dir.is_dir():
            for rel, path, is_dir in fsimage.iter_source(self.pkg.fs_dir):
                try:
                    st = path.stat()
                    snap[rel] = (is_dir, st.st_size, st.st_mtime_ns)
                except OSError:
                    pass
        return snap

    def rescan_if_changed(self) -> None:
        """Pick up edits made outside LaFlasher (Explorer, external editors)."""
        if self.pkg is None:
            return
        snapshot = self._take_snapshot()
        if snapshot != self._snapshot:
            if snapshot and not self._snapshot:
                self.pkg.fs_enabled = True  # first files were added from Explorer
            self._files_changed()

    def _files_changed(self) -> None:
        self.refresh()
        self.changed.emit()

    def _selected_paths(self) -> list[Path]:
        return [Path(i.data(0, PATH_ROLE)) for i in self.tree.selectedItems()
                if i.flags() & Qt.ItemFlag.ItemIsSelectable]

    def _target_folder(self) -> Path:
        items = self.tree.selectedItems()
        if items and items[0].flags() & Qt.ItemFlag.ItemIsSelectable:
            path = Path(items[0].data(0, PATH_ROLE))
            return path if path.is_dir() else path.parent
        return self.pkg.fs_dir

    def _update_buttons(self) -> None:
        has_pkg = self.pkg is not None
        selected = self._selected_paths() if has_pkg else []
        one_file = len(selected) == 1 and selected[0].is_file()
        for b in (self.add_files_btn, self.add_folder_btn, self.new_folder_btn, self.new_file_btn,
                  self.extract_btn, self.explorer_btn, self.partition_combo, self.enabled_check):
            b.setEnabled(has_pkg)
        self.edit_btn.setEnabled(one_file)
        self.replace_btn.setEnabled(one_file)
        self.rename_btn.setEnabled(len(selected) == 1)
        self.remove_btn.setEnabled(bool(selected))

    # ------------------------------------------------------------- usage
    def _schedule_usage(self) -> None:
        self._usage_generation += 1
        self.last_usage = None  # the previous measurement no longer applies
        self._usage_timer.start()

    def _start_usage(self) -> None:
        pkg = self.pkg
        target = pkg.fs_target() if pkg else None
        if pkg is None or not pkg.fs_dir.is_dir():
            self._show_usage(None, "No LittleFS files in this package. Add files to include a "
                                   "LittleFS image when flashing." if pkg else "")
            return
        if target is None:
            self._show_usage(None, "⚠ No LittleFS partition found. Choose \"Custom offset and "
                                   "size\" and enter where the LittleFS partition is.")
            return
        if self._usage_worker is not None:
            return  # still measuring older contents; _usage_done starts a fresh check
        self.usage_label.setText("Calculating...")
        worker = _UsageWorker(pkg.fs_dir, target[1], pkg.fs_params, self._usage_generation, self)
        worker.result.connect(self._usage_done)
        worker.finished.connect(worker.deleteLater)
        self._usage_worker = worker
        worker.start()

    def _usage_done(self, result, generation: int) -> None:
        self._usage_worker = None
        if generation != self._usage_generation:
            # Something changed while measuring: this result is outdated, measure again.
            if not self._usage_timer.isActive():
                self._start_usage()
            return
        self.last_usage = result
        if isinstance(result, fsimage.FsTooSmallError):
            self.usage_bar.setRange(0, 100)
            self.usage_bar.setValue(100)
            self.usage_bar.setFormat("Too full")
            self.usage_label.setText(f"⚠ {result}. Remove files or pick a bigger partition.")
        elif isinstance(result, Exception):
            self._show_usage(None, f"⚠ {result}")
        else:
            self.usage_bar.setRange(0, max(result.total, 1))
            self.usage_bar.setValue(result.used)
            self.usage_bar.setFormat(f"{human_size(result.used)} of {human_size(result.total)} (%p%)")
            text = (f"{result.files} file{'s' if result.files != 1 else ''} in {result.dirs} "
                    f"folder{'s' if result.dirs != 1 else ''}, "
                    f"{human_size(result.total - result.used)} free.")
            if not self.pkg.fs_enabled:
                text += " LittleFS will not be written (option unticked)."
            self.usage_label.setText("\n".join([text] + [f"⚠ {w}" for w in result.warnings]))

    def _show_usage(self, _result, text: str) -> None:
        self.last_usage = None
        self.usage_bar.setRange(0, 1)
        self.usage_bar.setValue(0)
        self.usage_bar.setFormat("")
        self.usage_label.setText(text)

    def fs_error(self) -> str | None:
        """A blocking LittleFS problem found by the last size check, if any."""
        if self.pkg and self.pkg.fs_will_flash() and isinstance(self.last_usage, Exception):
            return str(self.last_usage)
        return None

    # ------------------------------------------------------------ file ops
    def _ask_overwrite(self, name: str, state: dict) -> bool | None:
        """True = overwrite, False = skip, None = cancel everything."""
        if "all" in state:
            return state["all"]
        buttons = (QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.YesToAll
                   | QMessageBox.StandardButton.No | QMessageBox.StandardButton.NoToAll
                   | QMessageBox.StandardButton.Cancel)
        answer = QMessageBox.question(self, "File Already Exists",
                                      f"'{name}' already exists. Replace it?", buttons)
        if answer == QMessageBox.StandardButton.Cancel:
            return None
        if answer in (QMessageBox.StandardButton.YesToAll, QMessageBox.StandardButton.NoToAll):
            state["all"] = answer == QMessageBox.StandardButton.YesToAll
        return answer in (QMessageBox.StandardButton.Yes, QMessageBox.StandardButton.YesToAll)

    def _copy_into(self, src: Path, dest_dir: Path, state: dict, move: bool = False) -> bool:
        """Copy (or move) src into dest_dir. Returns False if the user cancelled."""
        target = dest_dir / src.name
        if src.is_dir() and target.is_dir():
            for child in sorted(src.iterdir()):
                if not self._copy_into(child, target, state, move):
                    return False
            if move:
                shutil.rmtree(src, ignore_errors=True)
            return True
        if target.exists():
            answer = self._ask_overwrite(target.name, state)
            if answer is None:
                return False
            if not answer:
                return True
            shutil.rmtree(target) if target.is_dir() else target.unlink()
        if move:
            shutil.move(str(src), str(target))
        elif src.is_dir():
            shutil.copytree(src, target, ignore=shutil.ignore_patterns(*fsimage.JUNK_FILES))
        else:
            shutil.copy2(src, target)
        return True

    def add_paths(self, paths: list[str], dest: str | None = None) -> None:
        if self.pkg is None:
            return
        if len(paths) == 1 and paths[0].lower().endswith(".laflash"):
            self.open_package_requested.emit(paths[0])
            return
        self.pkg.fs_dir.mkdir(parents=True, exist_ok=True)
        dest_dir = Path(dest) if dest else self._target_folder()
        fs_root = self.pkg.fs_dir.resolve()
        state: dict = {}
        try:
            for p in paths:
                src = Path(p)
                if src.resolve() == fs_root or fs_root in src.resolve().parents:
                    continue  # already inside the package
                if not self._copy_into(src, dest_dir, state):
                    break
        except OSError as exc:
            QMessageBox.warning(self, "Could Not Add Files", str(exc))
        if not self.pkg.fs_enabled:
            self.pkg.fs_enabled = True
        self._files_changed()

    def move_paths(self, paths: list[str], dest: str) -> None:
        dest_dir = Path(dest)
        state: dict = {}
        try:
            for p in paths:
                src = Path(p)
                if src.parent == dest_dir or src == dest_dir or src in dest_dir.parents:
                    continue
                if not self._copy_into(src, dest_dir, state, move=True):
                    break
        except OSError as exc:
            QMessageBox.warning(self, "Could Not Move", str(exc))
        self._files_changed()

    def add_files_dialog(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(self, "Add Files to LittleFS", self.last_dir)
        if paths:
            self.last_dir = str(Path(paths[0]).parent)
            self.add_paths(paths)

    def add_folder_dialog(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Add Folder to LittleFS", self.last_dir)
        if path:
            self.last_dir = str(Path(path).parent)
            self.add_paths([path])

    def _unique(self, folder: Path, name: str) -> Path:
        candidate, stem, suffix, n = folder / name, Path(name).stem, Path(name).suffix, 2
        while candidate.exists():
            candidate, n = folder / f"{stem} ({n}){suffix}", n + 1
        return candidate

    def _select_path(self, path: Path, edit: bool = False) -> None:
        def find(item):
            for i in range(item.childCount()):
                child = item.child(i)
                if child.data(0, PATH_ROLE) == str(path):
                    return child
                found = find(child)
                if found:
                    return found
            return None
        item = find(self.tree.invisibleRootItem())
        if item:
            self.tree.clearSelection()
            self.tree.setCurrentItem(item)
            self.tree.scrollToItem(item)
            if edit:
                self.tree.editItem(item, 0)

    def new_folder(self) -> None:
        if self.pkg is None:
            return
        self.pkg.fs_dir.mkdir(parents=True, exist_ok=True)
        folder = self._unique(self._target_folder(), "New folder")
        folder.mkdir()
        self.pkg.fs_enabled = True
        self._files_changed()
        self._select_path(folder, edit=True)

    def new_text_file(self) -> None:
        if self.pkg is None:
            return
        name, ok = QInputDialog.getText(self, "New Text File", "File name:", text="config.json")
        name = name.strip()
        if not ok or not name:
            return
        if _BAD_NAME_CHARS & set(name):
            QMessageBox.warning(self, "Invalid Name", 'Names cannot contain < > : " / \\ | ? *')
            return
        self.pkg.fs_dir.mkdir(parents=True, exist_ok=True)
        path = self._target_folder() / name
        if path.exists():
            QMessageBox.warning(self, "File Exists", f"'{name}' already exists.")
            return
        path.write_text("", encoding="utf-8")
        self.pkg.fs_enabled = True
        self._files_changed()
        self._select_path(path)
        self._edit_file(path)

    def _edit_file(self, path: Path) -> None:
        if is_text_file(path):
            dialog = TextEditorDialog(path, self)
            if dialog.exec():
                self._files_changed()
        else:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def edit_selected(self) -> None:
        paths = self._selected_paths()
        if len(paths) == 1 and paths[0].is_file():
            self._edit_file(paths[0])

    def _item_activated(self, item: QTreeWidgetItem, column: int) -> None:
        path = Path(item.data(0, PATH_ROLE))
        if path.is_file():
            self._edit_file(path)

    def rename_selected(self) -> None:
        items = [i for i in self.tree.selectedItems() if i.flags() & Qt.ItemFlag.ItemIsEditable]
        if len(items) == 1:
            self.tree.editItem(items[0], 0)

    def _item_renamed(self, item: QTreeWidgetItem, column: int) -> None:
        if self._populating or column != 0:
            return
        old = Path(item.data(0, PATH_ROLE))
        new_name = item.text(0).strip()
        if new_name == old.name:
            return
        error = None
        if not new_name or _BAD_NAME_CHARS & set(new_name) or new_name in (".", ".."):
            error = 'Names cannot be empty or contain < > : " / \\ | ? *'
        elif len(new_name.encode("utf-8")) > self.pkg.fs_params.name_max:
            error = f"LittleFS names can be at most {self.pkg.fs_params.name_max} bytes long."
        elif (old.parent / new_name).exists() and new_name.lower() != old.name.lower():
            error = f"'{new_name}' already exists."
        if error is None:
            try:
                if new_name.lower() == old.name.lower():  # case-only rename on Windows
                    temp = old.with_name(old.name + ".renaming")
                    old.rename(temp)
                    temp.rename(old.with_name(new_name))
                else:
                    old.rename(old.with_name(new_name))
            except OSError as exc:
                error = str(exc)
        if error:
            QMessageBox.warning(self, "Cannot Rename", error)
            QTimer.singleShot(0, self._populate)
            return
        QTimer.singleShot(0, self._files_changed)
        QTimer.singleShot(0, lambda: self._select_path(old.with_name(new_name)))

    def replace_selected(self) -> None:
        paths = self._selected_paths()
        if len(paths) != 1 or not paths[0].is_file():
            return
        src, _ = QFileDialog.getOpenFileName(self, f"Replace {paths[0].name}", self.last_dir)
        if src:
            self.last_dir = str(Path(src).parent)
            shutil.copyfile(src, paths[0])
            self._files_changed()

    def remove_selected(self) -> None:
        paths = self._selected_paths()
        if not paths:
            return
        what = f"'{paths[0].name}'" if len(paths) == 1 else f"{len(paths)} items"
        answer = QMessageBox.question(self, "Remove", f"Remove {what} from the LittleFS files?")
        if answer != QMessageBox.StandardButton.Yes:
            return
        for path in paths:
            try:
                shutil.rmtree(path) if path.is_dir() else path.unlink(missing_ok=True)
            except OSError as exc:
                QMessageBox.warning(self, "Could Not Remove", str(exc))
                break
        self._files_changed()

    def extract_selected(self) -> None:
        if self.pkg is None:
            return
        paths = self._selected_paths() or ([p for p in self.pkg.fs_dir.iterdir()]
                                           if self.pkg.fs_dir.is_dir() else [])
        if not paths:
            QMessageBox.information(self, "Extract", "There are no LittleFS files to extract.")
            return
        dest = QFileDialog.getExistingDirectory(self, "Extract To Folder", self.last_dir)
        if not dest:
            return
        state: dict = {}
        try:
            for path in paths:
                if not self._copy_into(path, Path(dest), state):
                    break
        except OSError as exc:
            QMessageBox.warning(self, "Could Not Extract", str(exc))
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(dest))

    def open_in_explorer(self) -> None:
        if self.pkg is None:
            return
        self.pkg.fs_dir.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.pkg.fs_dir)))

    def _context_menu(self, pos: QPoint) -> None:
        if self.pkg is None:
            return
        menu = QMenu(self)
        selected = self._selected_paths()
        one_file = len(selected) == 1 and selected[0].is_file()
        entries = [
            ("Edit", self.edit_selected, one_file),
            ("Open With Default App", lambda: QDesktopServices.openUrl(
                QUrl.fromLocalFile(str(selected[0]))), one_file),
            None,
            ("Add Files...", self.add_files_dialog, True),
            ("Add Folder...", self.add_folder_dialog, True),
            ("New Folder", self.new_folder, True),
            ("New Text File...", self.new_text_file, True),
            None,
            ("Rename", self.rename_selected, len(selected) == 1),
            ("Replace...", self.replace_selected, one_file),
            ("Extract...", self.extract_selected, True),
            ("Remove", self.remove_selected, bool(selected)),
        ]
        for entry in entries:
            if entry is None:
                menu.addSeparator()
                continue
            text, slot, enabled = entry
            action = menu.addAction(text)
            action.setEnabled(enabled)
            action.triggered.connect(slot)
        menu.exec(self.tree.viewport().mapToGlobal(pos))
