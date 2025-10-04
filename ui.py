#!/usr/bin/env python3
from __future__ import annotations
import os, threading
from typing import List, Tuple, Optional, Set
from PIL import Image

from PySide6.QtCore import Qt, QSize, Signal, QObject
from PySide6.QtGui import QPixmap, QImage
from PySide6.QtWidgets import (
    QWidget, QMainWindow, QHBoxLayout, QVBoxLayout, QSplitter,
    QListWidget, QListWidgetItem, QLabel, QLineEdit, QPushButton, QComboBox,
    QProgressBar, QMessageBox, QFileDialog, QInputDialog
)

from bookmarks import (
    BmLink,
    load_bookmarks_html,
    gather_folder_paths,
    select_folder,
    find_chrome_profiles,
    load_chrome_bookmarks_file,
)
from preview import take_screenshot, clear_cache
from utils import normalize_url, host_of, fit_image, filter_valid


# ---- Qt signal bridge ----
class Signals(QObject):
    progress = Signal(int)
    status = Signal(str)
    list_filled = Signal(list)            # List[Tuple[str,BmLink]]
    preview_ready = Signal(QPixmap)
    preview_failed = Signal(str)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Bookmark Viewer (Qt)")
        self.resize(1100, 700)

        self.sig = Signals()
        self.sig.progress.connect(self.on_progress)
        self.sig.status.connect(self.on_status)
        self.sig.list_filled.connect(self.on_list_filled)
        self.sig.preview_ready.connect(self.on_preview_ready)
        self.sig.preview_failed.connect(self.on_preview_failed)

        # --- Top controls (Row 1) ---
        top = QWidget(); top_layout = QHBoxLayout(top)
        self.file_edit = QLineEdit(); self.file_edit.setPlaceholderText("Select bookmarks HTML…")
        self.file_edit.returnPressed.connect(self.on_scan)
        browse_btn = QPushButton("Browse…"); browse_btn.clicked.connect(self.on_browse)
        self.folder_combo = QComboBox(); self.folder_combo.setEditable(False)
        self.folder_combo.addItem("All folders", "")
        self.folder_combo.currentIndexChanged.connect(self.on_folder_changed)
        self.check_btn = QPushButton("Check links: OFF"); self.check_btn.setCheckable(True); self.check_btn.setChecked(False)
        self.check_btn.clicked.connect(self.on_toggle_check)
        scan_btn = QPushButton("Scan"); scan_btn.clicked.connect(self.on_scan)

        top_layout.addWidget(self.file_edit, 4)
        top_layout.addWidget(browse_btn)
        top_layout.addWidget(self.folder_combo, 3)
        top_layout.addWidget(self.check_btn)
        top_layout.addWidget(scan_btn)

        # --- Top controls (Row 2) — move Chrome button under file selector ---
        chrome_row = QWidget(); chrome_layout = QHBoxLayout(chrome_row)
        chrome_btn = QPushButton("Load from Chrome"); chrome_btn.clicked.connect(self.on_load_chrome)
        chrome_layout.addWidget(chrome_btn)
        chrome_layout.addStretch(1)

        # Progress + status
        self.progress = QProgressBar(); self.progress.setMaximum(100)
        self.status = QLabel("Ready")
        statw = QWidget(); statl = QHBoxLayout(statw)
        statl.addWidget(self.progress, 3); statl.addWidget(self.status, 1)

        # Split main area
        split = QSplitter(Qt.Horizontal)
        left = QWidget(); left_layout = QVBoxLayout(left)
        self.list = QListWidget(); self.list.itemSelectionChanged.connect(self.on_select)
        left_layout.addWidget(self.list)
        right = QWidget(); right_layout = QVBoxLayout(right)
        self.preview = QLabel("(Click a bookmark to preview)")
        self.preview.setAlignment(Qt.AlignCenter)
        self.preview.setMinimumSize(QSize(300, 300))
        right_layout.addWidget(self.preview)
        split.addWidget(left); split.addWidget(right)
        split.setStretchFactor(0, 1); split.setStretchFactor(1, 2)

        # Central
        central = QWidget(); lay = QVBoxLayout(central)
        lay.addWidget(top)
        lay.addWidget(chrome_row)
        lay.addWidget(statw)
        lay.addWidget(split, 1)

        # Bottom bar
        bottom = QWidget(); bottom_l = QHBoxLayout(bottom)
        bottom_l.addStretch(1)
        clear_btn = QPushButton("Clear preview cache"); clear_btn.clicked.connect(self.on_clear_cache)
        bottom_l.addWidget(clear_btn)
        lay.addWidget(bottom)

        self.setCentralWidget(central)

        # State
        self.items: List[Tuple[str, BmLink]] = []
        self._preview_thread: Optional[threading.Thread] = None
        self._scan_thread: Optional[threading.Thread] = None
        self._links_cache: Optional[List[BmLink]] = None

    # ---- UI actions ----
    def on_toggle_check(self):
        self.check_btn.setText("Check links: ON" if self.check_btn.isChecked() else "Check links: OFF")

    def on_browse(self):
        path, _ = QFileDialog.getOpenFileName(self, "Select bookmarks HTML", "", "HTML files (*.html *.htm);;All files (*)")
        if not path:
            return
        self.file_edit.setText(path)
        self._links_cache = None
        try:
            links = load_bookmarks_html(path)
            folders = gather_folder_paths(links)
            self.folder_combo.blockSignals(True)
            self.folder_combo.clear(); self.folder_combo.addItem("All folders", "")
            for f in folders:
                self.folder_combo.addItem(f, f)
            self.folder_combo.setCurrentIndex(0)
        finally:
            self.folder_combo.blockSignals(False)
        self.on_scan()

    def on_load_chrome(self):
        profiles = find_chrome_profiles()
        if not profiles:
            QMessageBox.information(self, "Chrome", "Couldn't find a Chrome/Chromium profile with a Bookmarks file.")
            return
        if len(profiles) == 1:
            chosen = 0
        else:
            names = [n for (n, _p) in profiles]
            name, ok = QInputDialog.getItem(self, "Choose Chrome profile", "Profile:", names, 0, False)
            if not ok:
                return
            chosen = names.index(name)
        prof_name, bpath = profiles[chosen]
        try:
            links = load_chrome_bookmarks_file(bpath)
        except Exception as e:
            QMessageBox.critical(self, "Chrome", str(e)); return
        self._links_cache = links
        self.file_edit.setText(f"Chrome: {prof_name}")
        folders = gather_folder_paths(links)
        self.folder_combo.blockSignals(True)
        self.folder_combo.clear(); self.folder_combo.addItem("All folders", "")
        for f in folders:
            self.folder_combo.addItem(f, f)
        self.folder_combo.setCurrentIndex(0)
        self.folder_combo.blockSignals(False)
        self.on_scan()

    def on_scan(self):
        if self._scan_thread and self._scan_thread.is_alive():
            self.status.setText("Scan in progress…"); return
        path = self.file_edit.text().strip()
        use_file = bool(path) and os.path.isfile(path) and not path.startswith("Chrome:")
        folder = (self.folder_combo.currentData() or "").strip()
        self.list.clear(); self.preview.setText("(Click a bookmark to preview)"); self.preview.setPixmap(QPixmap())
        self.status.setText("Parsing…"); self.progress.setValue(0)
        self._scan_thread = threading.Thread(target=self._worker_scan, args=(path if use_file else "", folder, self.check_btn.isChecked()), daemon=True)
        self._scan_thread.start()

    def on_folder_changed(self, idx: int):
        path = self.file_edit.text().strip(); has_file = bool(path) and os.path.isfile(path)
        has_chrome = bool(self._links_cache)
        if not (has_file or has_chrome):
            return
        if self._scan_thread and self._scan_thread.is_alive():
            self.status.setText("Scan in progress…"); return
        self.on_scan()

    def _worker_scan(self, file_path: str, folder: str, do_check: bool):
        try:
            if file_path:
                links = load_bookmarks_html(file_path)
            elif self._links_cache is not None:
                links = list(self._links_cache)
            else:
                raise RuntimeError("No bookmarks source loaded.")
            sel = select_folder(links, folder)
            # de-dupe
            seen: Set[str] = set(); deduped: List[Tuple[str, BmLink]] = []
            for b in sel:
                n = normalize_url(b.href)
                if n and n not in seen:
                    seen.add(n); deduped.append((n, b))
            items = filter_valid(deduped) if do_check else deduped
            self.sig.list_filled.emit(items)
            self.sig.progress.emit(100)
            self.sig.status.emit(f"Found: {len(items)}")
        except Exception as e:
            self.sig.status.emit(f"Error: {e}")

    def on_list_filled(self, items: list):
        self.items = items
        for u, b in items:
            it = QListWidgetItem(f"{b.title}   —   {host_of(u)}"); it.setData(Qt.UserRole, u)
            self.list.addItem(it)
        if not items:
            self.status.setText("No links found.")

    def on_select(self):
        if self._preview_thread and self._preview_thread.is_alive():
            self.status.setText("Preview in progress…"); return
        sel = self.list.currentItem();
        if not sel: return
        url = sel.data(Qt.UserRole)
        self.status.setText("Capturing screenshot…")
        self._preview_thread = threading.Thread(target=self._worker_preview, args=(url,), daemon=True)
        self._preview_thread.start()

    def _worker_preview(self, url: str):
        try:
            path = take_screenshot(url)
            im = Image.open(path); im.load()
            max_w = max(320, self.preview.width()-16); max_h = max(280, self.preview.height()-16)
            im = fit_image(im, max_w, max_h)
            if im.mode != "RGBA": im = im.convert("RGBA")
            data = im.tobytes("raw", "RGBA")
            qimg = QImage(data, im.width, im.height, QImage.Format_RGBA8888)
            pm = QPixmap.fromImage(qimg)
            self.sig.preview_ready.emit(pm)
        except Exception as e:
            self.sig.preview_failed.emit(str(e))

    def on_preview_ready(self, pm: QPixmap):
        self.preview.setPixmap(pm); self.preview.setText(""); self.status.setText("")

    def on_preview_failed(self, msg: str):
        self.preview.setPixmap(QPixmap()); self.preview.setText("(Preview unavailable)"); self.status.setText("Screenshot error")
        QMessageBox.warning(self, "Preview error", msg)

    def on_progress(self, v: int):
        self.progress.setValue(v)

    def on_status(self, s: str):
        self.status.setText(s)

    def on_clear_cache(self):
        clear_cache(); QMessageBox.information(self, "Cache", "Preview cache cleared.")
