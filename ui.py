#!/usr/bin/env python3
from __future__ import annotations
import os, threading, json, shutil, html
from typing import List, Tuple, Optional, Set, Dict, Any
from datetime import datetime, timezone
from pathlib import Path
from PIL import Image

from PySide6.QtCore import Qt, QSize, Signal, QObject
from PySide6.QtGui import QPixmap, QImage
from PySide6.QtWidgets import (
    QWidget, QMainWindow, QHBoxLayout, QVBoxLayout, QSplitter,
    QListWidget, QListWidgetItem, QLabel, QLineEdit, QPushButton, QComboBox,
    QProgressBar, QMessageBox, QFileDialog, QInputDialog, QAbstractItemView
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
    # Include a sequence id so we can ignore stale previews completing late
    preview_ready = Signal(int, QPixmap)
    preview_failed = Signal(int, str)


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
        self.folder_combo.setMinimumWidth(260)
        top_layout.addWidget(self.folder_combo, 0)
        top_layout.addWidget(self.check_btn)
        top_layout.addWidget(scan_btn)

        # --- Top controls (Row 2) — Chrome button under file selector ---
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
        self.list.setSelectionMode(QAbstractItemView.ExtendedSelection)
        left_layout.addWidget(self.list)
        # Row of actions below list
        list_row = QWidget(); list_row_l = QHBoxLayout(list_row)
        del_btn = QPushButton("Delete selected"); del_btn.clicked.connect(self.on_delete_selected)
        move_btn = QPushButton("Move to folder…"); move_btn.clicked.connect(self.on_move_selected)
        list_row_l.addWidget(del_btn)
        list_row_l.addWidget(move_btn)
        list_row_l.addStretch(1)
        left_layout.addWidget(list_row)

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
        export_btn = QPushButton("Export to HTML…"); export_btn.clicked.connect(self.on_export_html)
        save_json_btn = QPushButton("Save JSON…"); save_json_btn.clicked.connect(self.on_save_json)
        write_btn = QPushButton("Write back to Chrome…"); write_btn.clicked.connect(self.on_write_back)
        clear_btn = QPushButton("Clear preview cache"); clear_btn.clicked.connect(self.on_clear_cache)
        for b in (export_btn, save_json_btn, write_btn, clear_btn):
            bottom_l.addWidget(b)
        lay.addWidget(bottom)

        self.setCentralWidget(central)

        # State
        self.items: List[Tuple[str, BmLink]] = []
        self._preview_thread: Optional[threading.Thread] = None
        self._scan_thread: Optional[threading.Thread] = None
        self._links_cache: Optional[List[BmLink]] = None
        self._edit_links: Optional[List[BmLink]] = None  # editable working set
        self._ignore_selection: bool = False            # suppress preview during refreshes
        self._preview_seq: int = 0                      # cancels stale previews
        self._chrome_profile_path: Optional[Path] = None
        self._chrome_profile_name: Optional[str] = None

    # ---- UI actions ----
    def on_toggle_check(self):
        self.check_btn.setText("Check links: ON" if self.check_btn.isChecked() else "Check links: OFF")

    def on_browse(self):
        path, _ = QFileDialog.getOpenFileName(self, "Select bookmarks HTML", "", "HTML files (*.html *.htm);;All files (*)")
        if not path:
            return
        self.file_edit.setText(path)
        self._links_cache = None
        self._chrome_profile_path = None
        try:
            links = load_bookmarks_html(path)
            self._edit_links = list(links)
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
        self._links_cache = list(links)
        self._edit_links = list(links)
        self._chrome_profile_path = Path(bpath)
        self._chrome_profile_name = prof_name
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
        # suppress selection-driven previews while we rebuild the list
        self._ignore_selection = True
        # cancel any in-flight preview by bumping seq
        self._preview_seq += 1
        path = self.file_edit.text().strip()
        use_file = bool(path) and os.path.isfile(path) and not path.startswith("Chrome:")
        folder = (self.folder_combo.currentData() or "").strip()
        self.list.clear(); self.list.clearSelection()
        self.preview.setText("(Click a bookmark to preview)"); self.preview.setPixmap(QPixmap())
        self.status.setText("Parsing…"); self.progress.setValue(0)
        self._scan_thread = threading.Thread(target=self._worker_scan, args=(path if use_file else "", folder, self.check_btn.isChecked()), daemon=True)
        self._scan_thread.start()

    def on_folder_changed(self, idx: int):
        path = self.file_edit.text().strip(); has_file = bool(path) and os.path.isfile(path)
        has_chrome = bool(self._links_cache)
        if not (has_file or has_chrome or self._edit_links):
            return
        if self._scan_thread and self._scan_thread.is_alive():
            self.status.setText("Scan in progress…"); return
        self.on_scan()

    def _worker_scan(self, file_path: str, folder: str, do_check: bool):
        try:
            if self._edit_links is not None:
                links = list(self._edit_links)
            elif file_path:
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
        # Avoid triggering selection events while we fill
        self.list.blockSignals(True)
        self.list.clear()
        for u, b in items:
            it = QListWidgetItem(f"{b.title}   —   {host_of(u)}"); it.setData(Qt.UserRole, u)
            self.list.addItem(it)
        self.list.blockSignals(False)
        # Ensure nothing is selected and no current item
        self.list.clearSelection()
        try:
            self.list.setCurrentRow(-1)
        except Exception:
            pass
        if not items:
            self.status.setText("No links found.")
        # allow selection again
        self._ignore_selection = False

    def on_select(self):
        if self._ignore_selection:
            return
        if self._preview_thread and self._preview_thread.is_alive():
            self.status.setText("Preview in progress…"); return
        sel = self.list.currentItem();
        if not sel:
            return
        url = sel.data(Qt.UserRole)
        self.status.setText("Capturing screenshot…")
        # start a new preview sequence; this cancels any late arrivals
        self._preview_seq += 1
        seq = self._preview_seq
        self._preview_thread = threading.Thread(target=self._worker_preview, args=(seq, url,), daemon=True)
        self._preview_thread.start()

    def _worker_preview(self, seq: int, url: str):
        try:
            path = take_screenshot(url)
            im = Image.open(path); im.load()
            max_w = max(320, self.preview.width()-16); max_h = max(280, self.preview.height()-16)
            im = fit_image(im, max_w, max_h)
            if im.mode != "RGBA": im = im.convert("RGBA")
            data = im.tobytes("raw", "RGBA")
            qimg = QImage(data, im.width, im.height, QImage.Format_RGBA8888)
            pm = QPixmap.fromImage(qimg)
            self.sig.preview_ready.emit(seq, pm)
        except Exception as e:
            self.sig.preview_failed.emit(seq, str(e))

    def on_preview_ready(self, seq: int, pm: QPixmap):
        # Ignore stale previews
        if seq != self._preview_seq:
            return
        self.preview.setPixmap(pm); self.preview.setText(""); self.status.setText("")

    def on_preview_failed(self, seq: int, msg: str):
        if seq != self._preview_seq:
            return
        self.preview.setPixmap(QPixmap()); self.preview.setText("(Preview unavailable)"); self.status.setText("Screenshot error")
        QMessageBox.warning(self, "Preview error", msg)

    def on_progress(self, v: int):
        self.progress.setValue(v)

    def on_status(self, s: str):
        self.status.setText(s)

    # ---- Edit operations ----
    def _refresh_folders(self):
        links = self._edit_links or []
        folders = gather_folder_paths(links)
        current = (self.folder_combo.currentData() or "")
        self.folder_combo.blockSignals(True)
        self.folder_combo.clear(); self.folder_combo.addItem("All folders", "")
        for f in folders:
            self.folder_combo.addItem(f, f)
        # restore if still present
        idx = self.folder_combo.findData(current)
        if idx >= 0:
            self.folder_combo.setCurrentIndex(idx)
        else:
            self.folder_combo.setCurrentIndex(0)
        self.folder_combo.blockSignals(False)

    def on_delete_selected(self):
        sel_items = self.list.selectedIndexes()
        if not sel_items:
            return
        if self._scan_thread and self._scan_thread.is_alive():
            self.status.setText("Wait for scan to finish…"); return
        if not self._edit_links:
            return
        # Delete ALL bookmarks whose normalized URL matches any selected row
        selected_urls: Set[str] = set()
        for idx in sel_items:
            row = idx.row()
            if 0 <= row < len(self.items):
                url, _b = self.items[row]
                selected_urls.add(url)
        before = len(self._edit_links)
        self._edit_links = [b for b in self._edit_links if normalize_url(b.href) not in selected_urls]
        removed = before - len(self._edit_links)
        self.status.setText(f"Deleted {removed} bookmark(s)")
        self._refresh_folders()
        # Clear preview + selection and rescan; also cancel any in-flight preview
        self._preview_seq += 1
        self.preview.setPixmap(QPixmap()); self.preview.setText("(Click a bookmark to preview)")
        self.list.clearSelection()
        self.on_scan()

    def on_move_selected(self):
        sel_items = self.list.selectedIndexes()
        if not sel_items:
            return
        if self._scan_thread and self._scan_thread.is_alive():
            self.status.setText("Wait for scan to finish…"); return
        folders = gather_folder_paths(self._edit_links or [])
        # Allow typing a new path too
        dest, ok = QInputDialog.getItem(self, "Move to folder", "Destination folder:", folders, 0, True)
        if not ok:
            return
        dest = (dest or "").strip().strip("/")
        if not dest:
            return
        # Move ALL bookmarks whose normalized URL matches any selected row
        selected_urls: Set[str] = set()
        for idx in sel_items:
            row = idx.row()
            if 0 <= row < len(self.items):
                url, _b = self.items[row]
                selected_urls.add(url)
        changed = 0
        if self._edit_links:
            for b in self._edit_links:
                if normalize_url(b.href) in selected_urls:
                    b.folder_path = dest
                    changed += 1
        self.status.setText(f"Moved {changed} bookmark(s)")
        self._refresh_folders()
        # cancel any in-flight preview and rescan
        self._preview_seq += 1
        self.on_scan()

    # ---- Write back to Chrome ----
    def _chrome_time_now_str(self) -> str:
        # Chrome stores microseconds since 1601-01-01 UTC as a string
        epoch = datetime(1601, 1, 1, tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        micros = int((now - epoch).total_seconds() * 1_000_000)
        return str(micros)

    def _scan_max_id(self, node: Dict[str, Any]) -> int:
        m = 0
        def rec(n):
            nonlocal m
            if isinstance(n, dict):
                i = n.get("id")
                if isinstance(i, str) and i.isdigit():
                    m = max(m, int(i))
                for ch in n.get("children", []) or []:
                    rec(ch)
        rec(node)
        return m

    def on_write_back(self):
        if not self._edit_links:
            QMessageBox.information(self, "Write", "Nothing to write — no bookmarks loaded.")
            return
        # Choose target profile/bookmarks file
        target_path: Optional[Path] = self._chrome_profile_path
        target_name: Optional[str] = self._chrome_profile_name
        if target_path is None:
            profiles = find_chrome_profiles()
            if not profiles:
                QMessageBox.information(self, "Chrome", "Couldn't find a Chrome/Chromium profile with a Bookmarks file.")
                return
            if len(profiles) == 1:
                target_name, p = profiles[0]
                target_path = Path(p)
            else:
                names = [n for (n, _p) in profiles]
                name, ok = QInputDialog.getItem(self, "Choose Chrome profile", "Write to:", names, 0, False)
                if not ok:
                    return
                idx = names.index(name)
                target_name, p = profiles[idx]
                target_path = Path(p)
        # Final confirmation
        if QMessageBox.question(
            self, "Write back to Chrome",
            f"This will overwrite\n\n{target_name}\n{target_path}\n\nClose Chrome first. Continue?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No
        ) != QMessageBox.Yes:
            return
        try:
            # Load existing skeleton if present
            data: Dict[str, Any]
            if target_path.exists():
                with open(target_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            else:
                data = {}
            roots = data.get("roots") or {}
            # Ensure roots exist
            def ensure_root(key: str, name: str, fallback_id: str) -> Dict[str, Any]:
                node = roots.get(key)
                if not isinstance(node, dict):
                    node = {"type": "folder", "name": name, "children": [], "id": fallback_id}
                    roots[key] = node
                if "children" not in node or not isinstance(node["children"], list):
                    node["children"] = []
                return node
            bar = ensure_root("bookmark_bar", "Bookmarks bar", "1")
            oth = ensure_root("other", "Other bookmarks", "2")
            syn = ensure_root("synced", "Mobile bookmarks", "3")

            # Fresh children we will build
            new_children: Dict[str, List[Dict[str, Any]]] = {
                "bookmark_bar": [],
                "other": [],
                "synced": [],
            }

            # ID generator (continue from current max id)
            max_id = 0
            for k in ("bookmark_bar", "other", "synced"):
                max_id = max(max_id, self._scan_max_id(roots.get(k) or {}))
            def next_id() -> str:
                nonlocal max_id
                max_id += 1
                return str(max_id)

            now_s = self._chrome_time_now_str()

            # Helpers to build nested folders under a root
            def ensure_folder(children: List[Dict[str, Any]], name: str) -> Dict[str, Any]:
                for ch in children:
                    if ch.get("type") == "folder" and ch.get("name") == name:
                        return ch
                node = {"type": "folder", "name": name, "children": [], "id": next_id(), "date_added": now_s, "date_modified": now_s}
                children.append(node)
                return node

            ROOT_NAME_TO_KEY = {
                "bookmarks bar": "bookmark_bar",
                "other bookmarks": "other",
                "mobile bookmarks": "synced",
            }

            # Build trees from edited links
            for b in list(self._edit_links):
                url = normalize_url(b.href)
                if not url:
                    continue
                # Determine root and subpath
                parts = (b.folder_path or "").strip("/").split("/") if b.folder_path else []
                root_key = None
                if parts:
                    first = parts[0].strip().lower()
                    root_key = ROOT_NAME_TO_KEY.get(first)
                    if root_key:
                        parts = parts[1:]
                if not root_key:
                    root_key = "other"
                # Walk/construct folders
                cur_children = new_children[root_key]
                for seg in parts:
                    if not seg:
                        continue
                    folder = ensure_folder(cur_children, seg)
                    cur_children = folder["children"]
                # Add URL node
                node = {
                    "type": "url",
                    "name": b.title or url,
                    "url": url,
                    "id": next_id(),
                    "date_added": now_s,
                }
                cur_children.append(node)

            # Swap children
            bar["children"] = new_children["bookmark_bar"]
            oth["children"] = new_children["other"]
            syn["children"] = new_children["synced"]
            data["roots"] = roots
            data.pop("checksum", None)  # let Chrome recompute
            if "version" not in data:
                data["version"] = 1

            # Backup & write
            ts = datetime.now().strftime("%Y%m%d-%H%M%S")
            backup = target_path.with_name(target_path.name + f".backup-{ts}.json")
            try:
                if target_path.exists():
                    shutil.copyfile(target_path, backup)
            except Exception:
                pass
            with open(target_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)

            QMessageBox.information(self, "Write", f"Bookmarks written to:\n{target_name}\n{target_path}\n\nA backup was saved as:\n{backup.name}")
        except Exception as e:
            QMessageBox.critical(self, "Write error", str(e))

    # ---- Export / Save ----
    def _build_folder_tree(self) -> Dict[str, Any]:
        """Return a nested folder tree: {name, children:[folders|links]} from self._edit_links.
        Links look like {type:'url', title, url}. Folders: {type:'folder', name, children}.
        Root is an anonymous folder.
        """
        root = {"type": "folder", "name": "ROOT", "children": []}
        def ensure_path(parts: List[str]) -> List[Dict[str, Any]]:
            cur = root["children"]
            for seg in parts:
                seg = seg.strip()
                if not seg:
                    continue
                found = None
                for ch in cur:
                    if ch.get("type") == "folder" and ch.get("name") == seg:
                        found = ch; break
                if not found:
                    found = {"type": "folder", "name": seg, "children": []}
                    cur.append(found)
                cur = found["children"]
            return cur
        for b in list(self._edit_links or []):
            url = normalize_url(b.href)
            if not url:
                continue
            parts = (b.folder_path or "").strip("/")
            parts_list = [p for p in parts.split("/") if p] if parts else []
            cur_children = ensure_path(parts_list)
            cur_children.append({
                "type": "url",
                "title": b.title or url,
                "url": url,
            })
        return root

    def _export_tree_to_html(self, node: Dict[str, Any], out, level: int = 0):
        IND = "    " * level
        now_unix = str(int(datetime.now(timezone.utc).timestamp()))
        if node.get("type") == "folder":
            name = html.escape(node.get("name", ""))
            if level > 0:  # skip writing a heading for the anonymous root
                out.write(f"{IND}<DT><H3 ADD_DATE=\"{now_unix}\">{name}</H3>\n")
            if level == 0:
                out.write(f"<DL><p>\n")
            else:
                out.write(f"{IND}<DL><p>\n")
            for ch in node.get("children", []):
                self._export_tree_to_html(ch, out, level + 1)
            if level == 0:
                out.write(f"</DL><p>\n")
            else:
                out.write(f"{IND}</DL><p>\n")
        else:  # url
            title = html.escape(node.get("title", ""))
            url = html.escape(node.get("url", ""))
            out.write(f"{IND}<DT><A HREF=\"{url}\" ADD_DATE=\"{now_unix}\">{title}</A>\n")

    def on_export_html(self):
        if not self._edit_links:
            QMessageBox.information(self, "Export", "Nothing to export — no bookmarks loaded.")
            return
        path, _ = QFileDialog.getSaveFileName(self, "Export to HTML", "Bookmarks.html", "HTML files (*.html *.htm)")
        if not path:
            return
        tree = self._build_folder_tree()
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write("<!DOCTYPE NETSCAPE-Bookmark-file-1>\n")
                f.write("<!-- This is an automatically generated file. -->\n")
                f.write("<META HTTP-EQUIV=\"Content-Type\" CONTENT=\"text/html; charset=UTF-8\">\n")
                f.write("<TITLE>Bookmarks</TITLE>\n")
                f.write("<H1>Bookmarks</H1>\n")
                self._export_tree_to_html(tree, f, 0)
            QMessageBox.information(self, "Export", f"Exported to: {path}")
        except Exception as e:
            QMessageBox.critical(self, "Export error", str(e))

    def on_save_json(self):
        if not self._edit_links:
            QMessageBox.information(self, "Save", "Nothing to save — no bookmarks loaded.")
            return
        path, _ = QFileDialog.getSaveFileName(self, "Save JSON", "bookmarks.json", "JSON files (*.json)")
        if not path:
            return
        payload = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "count": len(self._edit_links),
            "bookmarks": [
                {
                    "title": (b.title or normalize_url(b.href) or ""),
                    "url": normalize_url(b.href) or "",
                    "folder_path": (b.folder_path or ""),
                }
                for b in (self._edit_links or [])
                if normalize_url(b.href)
            ],
        }
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            QMessageBox.information(self, "Save", f"Saved JSON to: {path}")
        except Exception as e:
            QMessageBox.critical(self, "Save error", str(e))

    def on_clear_cache(self):
        clear_cache(); QMessageBox.information(self, "Cache", "Preview cache cleared.")
