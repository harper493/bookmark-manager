#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Bookmark Viewer (Qt + Playwright) — viewport-only screenshots

Changes:
- Screenshots now capture only the visible viewport ("above the fold"), not the full page.
- Uses a configurable viewport size and device scale factor for crisp images.
- Retains persistent profile, global serialization, human-check headful hold, and lock cleanup.

Setup (once):
  python3 -m venv .venv
  source .venv/bin/activate
  python -m pip install -U pip
  python -m pip install beautifulsoup4 httpx pillow lxml playwright PySide6
  export PLAYWRIGHT_BROWSERS_PATH="$(pwd)/.pw-browsers"
  python -m playwright install chromium

Run:
  source .venv/bin/activate
  export PLAYWRIGHT_BROWSERS_PATH="$(pwd)/.pw-browsers"
  python bookmark_viewer_qt.py
"""

import os, sys, io, time, threading, hashlib, glob, html, shutil, urllib.parse as urlparse
from dataclasses import dataclass
from typing import List, Tuple, Dict, Optional, Set

import httpx
from bs4 import BeautifulSoup, Tag, NavigableString
from PIL import Image

# Qt
from PySide6.QtCore import Qt, QSize, Signal, QObject
from PySide6.QtGui import QPixmap, QImage
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QFileDialog, QPushButton, QLineEdit, QLabel,
    QListWidget, QListWidgetItem, QHBoxLayout, QVBoxLayout, QSplitter,
    QProgressBar, QCheckBox, QMessageBox, QWidget
)
from PySide6.QtWidgets import QComboBox

# ----------------------------------------------------------------------
# Playwright browser storage inside project to avoid path/perm issues
PW_DIR = os.path.join(os.path.dirname(__file__), ".pw-browsers")
os.makedirs(PW_DIR, exist_ok=True)
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", PW_DIR)
# ----------------------------------------------------------------------

# ---- Screenshot configuration (viewport-only) ----
VIEWPORT_WIDTH = 1280
VIEWPORT_HEIGHT = 800
DEVICE_SCALE_FACTOR = 2.0   # 2.0 looks crisp on HiDPI/Retina; set 1.0 if you prefer
FULL_PAGE = False           # <-- viewport-only; set to True if you want full-page again

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/120.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Connection": "keep-alive",
}

# ---- Download/preview helpers ----

def sniff_resource(url: str, timeout: float = 10.0) -> Tuple[str, str]:
    """Return (content_type, content_disposition) via a light HEAD (fallback to range GET)."""
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True, headers=HEADERS) as c:
            r = c.head(url)
            if r.status_code >= 400 or not r.headers.get("content-type"):
                # Some servers reject HEAD: do a tiny ranged GET
                r = c.get(url, headers={"Range": "bytes=0-0"})
            ct = (r.headers.get("content-type") or "").lower()
            cd = (r.headers.get("content-disposition") or "").lower()
            return ct, cd
    except Exception:
        return "", ""

def is_html_like(ct: str) -> bool:
    ct = ct or ""
    return ("text/html" in ct) or ("application/xhtml+xml" in ct) or (ct.startswith("text/") and "xml" in ct)

def is_image_content(ct: str) -> bool:
    return (ct or "").startswith("image/")

def is_pdf(ct: str) -> bool:
    return "application/pdf" in (ct or "")

def is_definitely_download(ct: str, cd: str) -> bool:
    # If server says attachment OR content-type is clearly non-HTML/non-image and not PDF inline
    if "attachment" in (cd or ""):
        return True
    if not ct:
        return False
    if is_html_like(ct) or is_image_content(ct) or is_pdf(ct):
        return False
    return True

def fetch_image_bytes_direct(url: str, timeout: float = 12.0, max_bytes: int = 6_000_000) -> Optional[bytes]:
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True, headers=HEADERS) as c:
            r = c.get(url)
            if r.status_code >= 400:
                return None
            ct = (r.headers.get("content-type") or "").lower()
            if not is_image_content(ct):
                return None
            data = r.content
            if not data or len(data) > max_bytes:
                return None
            return data
    except Exception:
        return None

# ---------------- Utilities ----------------

def normalize_url(raw: str) -> str:
    if not raw: return ""
    raw = html.unescape(raw.strip())
    try:
        u = urlparse.urlsplit(raw)
    except Exception:
        return raw
    scheme = (u.scheme or "http").lower()
    netloc = u.netloc.lower()
    if netloc.endswith(":80") and scheme == "http":  netloc = netloc[:-3]
    if netloc.endswith(":443") and scheme == "https": netloc = netloc[:-4]
    path = u.path or "/"
    if path != "/" and path.endswith("/"): path = path[:-1]
    clean = urlparse.urlunsplit((scheme, netloc, path, "", ""))  # drop query/fragment
    if not scheme.startswith("http"): clean = "https://" + clean
    return clean

def host_of(url: str) -> str:
    try:
        return urlparse.urlsplit(url).netloc.lower()
    except Exception:
        return ""

def fit_pil(im: Image.Image, max_w: int, max_h: int) -> Image.Image:
    w, h = im.size
    scale = min(max_w / float(w), max_h / float(h), 1.0)
    new_size = (max(1, int(w*scale)), max(1, int(h*scale)))
    return im.resize(new_size, Image.LANCZOS)

def pil_to_qpixmap(im: Image.Image) -> QPixmap:
    if im.mode != "RGBA":
        im = im.convert("RGBA")
    data = im.tobytes("raw", "RGBA")
    qimg = QImage(data, im.width, im.height, QImage.Format_RGBA8888)
    return QPixmap.fromImage(qimg)

# ---------------- Robust bookmarks parser (handles missing </DT>) ----------------

@dataclass
class BmLink:
    title: str
    href: str
    folder_path: str  # "A/B/C"

def make_soup(data: bytes) -> "BeautifulSoup":
    try:
        return BeautifulSoup(data, "lxml")
    except Exception:
        return BeautifulSoup(data, "html.parser")

def _nearest_prev_header(parent_dl: Tag, child: Tag) -> Optional[str]:
    sib = child.previous_sibling
    while sib is not None:
        if isinstance(sib, Tag):
            nm = sib.name.lower()
            if nm in ("h1", "h2", "h3"):
                return sib.get_text(strip=True)
            if nm in ("dt", "p"):
                h = sib.find(["h3", "h2", "h1"], recursive=True)
                if h: return h.get_text(strip=True)
        sib = sib.previous_sibling
    return None

def _anchors_in_current_folder(node: Tag, current_dl: Tag) -> List[Tag]:
    anchors: List[Tag] = []
    for a in node.find_all("a", href=True):
        anc = a.find_parent("dl")
        if anc is current_dl:
            anchors.append(a)
    return anchors

def _walk_dl(current_dl: Tag, path_stack: List[str], out: List[BmLink]):
    last_header: Optional[str] = None
    for child in list(current_dl.children):
        if not isinstance(child, Tag): continue
        nm = child.name.lower()
        if nm in ("dt", "p"):
            h = child.find(["h3", "h2", "h1"], recursive=True)
            if h: last_header = h.get_text(strip=True)
            for a in _anchors_in_current_folder(child, current_dl):
                href = a["href"]; title = a.get_text(strip=True) or href
                out.append(BmLink(title=title, href=href, folder_path="/".join(path_stack)))
            for sub_dl in child.find_all("dl", recursive=False):
                folder_name = last_header or _nearest_prev_header(current_dl, sub_dl)
                if folder_name:
                    path_stack.append(folder_name); _walk_dl(sub_dl, path_stack, out); path_stack.pop()
                else:
                    _walk_dl(sub_dl, path_stack, out)
        elif nm == "dl":
            folder_name = last_header or _nearest_prev_header(current_dl, child)
            if folder_name:
                path_stack.append(folder_name); _walk_dl(child, path_stack, out); path_stack.pop()
            else:
                _walk_dl(child, path_stack, out)
        else:
            if nm in ("h3", "h2", "h1"): last_header = child.get_text(strip=True)
            if nm == "a" and child.has_attr("href"):
                href = child["href"]; title = child.get_text(strip=True) or href
                out.append(BmLink(title=title, href=href, folder_path="/".join(path_stack)))

def load_bookmarks(file_path: str) -> List[BmLink]:
    with open(file_path, "rb") as f: data = f.read()
    soup = make_soup(data)
    dl = soup.find("dl")
    if not dl:
        raise RuntimeError("Could not find <DL> in the bookmarks file. Is it a Netscape export?")
    out: List[BmLink] = []
    _walk_dl(dl, [], out)
    return out

def select_folder(links: List[BmLink], target_path: str) -> List[BmLink]:
    target = (target_path or "").strip().strip("/")
    if not target: return links[:]
    if "/" in target:
        tci = target.lower(); return [b for b in links if b.folder_path.lower() == tci]
    seg = target.lower()
    return [b for b in links if b.folder_path.lower().split("/")[-1] == seg]

# ---------------- Folder listing helpers ----------------

def gather_folder_paths(links: List[BmLink]) -> List[str]:
    """Return all unique folder paths (including intermediate prefixes), sorted."""
    s: Set[str] = set()
    for b in links:
        p = (b.folder_path or "").strip("/")
        if not p:
            continue
        parts = p.split("/")
        accum: List[str] = []
        for part in parts:
            accum.append(part)
            s.add("/".join(accum))
    return sorted(s, key=str.lower)


# ---------------- Link checking (fast) ----------------

def filter_valid(deduped: List[Tuple[str,BmLink]]) -> List[Tuple[str,BmLink]]:
    out: List[Tuple[str,BmLink]] = []
    with httpx.Client(timeout=10.0, follow_redirects=True, headers=HEADERS) as c:
        for u,b in deduped:
            try:
                r = c.head(u)
                if r.status_code >= 400:
                    r = c.get(u)
                ok = 200 <= r.status_code < 400
            except Exception:
                ok = False
            if ok:
                out.append((u,b))
    return out

# ---------------- Playwright screenshot (serialized + cleanup + headful hold) ----------------

PROFILE_DIR  = os.path.join(os.path.dirname(__file__), ".pw-profile")
os.makedirs(PROFILE_DIR, exist_ok=True)

# Global lock to ensure only ONE Playwright session uses the profile at a time
_PW_PROFILE_LOCK = threading.Lock()

def screenshot_cache_dir() -> str:
    base = os.path.join(os.path.expanduser("~"), ".bookmark_viewer_cache_qt")
    os.makedirs(base, exist_ok=True); return base

def _url_hash(u: str) -> str:
    return hashlib.sha256(u.encode("utf-8", errors="ignore")).hexdigest()[:24]

def _cleanup_profile_locks(profile_dir: str):
    """Remove stale Chromium ProcessSingleton lock files if they exist."""
    patterns = [
        "Singleton*", "singleton*", "LOCK", "Lockfile", "lockfile",
        "Crashpad", "Crashpad/completed", "Crashpad/pending/*"
    ]
    for pat in patterns:
        for p in glob.glob(os.path.join(profile_dir, pat)):
            try:
                if os.path.isdir(p):
                    if "Crashpad/pending" in p:
                        for f in glob.glob(os.path.join(p, "*")):
                            try: os.remove(f)
                            except Exception: pass
                else:
                    os.remove(p)
            except Exception:
                pass

def _looks_like_challenge(page) -> bool:
    """Return True only for clear, known bot-check patterns to avoid false positives."""
    try:
        title = (page.title() or "").strip().lower()
    except Exception:
        title = ""
    try:
        url = (page.url or "")
    except Exception:
        url = ""

    # Strict markers: Cloudflare and similar
    if "/cdn-cgi/challenge" in url or "challenges.cloudflare.com" in url:
        return True
    if ("checking your browser" in title) or ("just a moment" in title):
        return True
    if ("attention required" in title and "cloudflare" in title):
        return True

    # Do NOT scan full page content for 'captcha' — too many false positives.
    return False

def take_screenshot(url: str, width: int = VIEWPORT_WIDTH, height: int = VIEWPORT_HEIGHT, timeout_ms: int = 25_000) -> str:
    """Capture screenshot with a persistent profile; serialize to avoid ProcessSingleton conflicts.
       Viewport-only capture by default (full_page=False). On challenge: keep visible window open to solve.
    """
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    except Exception:
        raise RuntimeError(
            "Playwright not ready.\nUse this venv:\n"
            "  python -m pip install playwright\n"
            f"  export PLAYWRIGHT_BROWSERS_PATH={PW_DIR}\n"
            "  python -m playwright install chromium"
        )

    img_path = os.path.join(screenshot_cache_dir(), f"{_url_hash(url)}.png")
    if os.path.exists(img_path) and os.path.getsize(img_path) > 0:
        return img_path

    # Pre-check the resource type to avoid auto-download popups and non-previewable targets
    ct, cd = sniff_resource(url)
    # Direct image? Fetch and cache without launching a browser
    if is_image_content(ct):
        data = fetch_image_bytes_direct(url)
        if data:
            with open(img_path, "wb") as f:
                f.write(data)
            return img_path
    # If it's a guaranteed download (zip, exe, etc.), don't try to preview
    if is_definitely_download(ct, cd):
        raise RuntimeError("This link triggers a file download (not a web page), so a preview isn't available.")

    args = []
    try:
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            args.append("--no-sandbox")
    except Exception:
        pass

    with _PW_PROFILE_LOCK:  # serialize access to the persistent profile
        _cleanup_profile_locks(PROFILE_DIR)

        with sync_playwright() as p:
            # 1) Try headless first with persistent profile (viewport-only)
            ctx = p.chromium.launch_persistent_context(
                user_data_dir=PROFILE_DIR,
                headless=True,
                accept_downloads=False,
                args=args,
                viewport={"width": width, "height": height},
                user_agent=HEADERS["User-Agent"],
                timezone_id="Europe/Paris",
                locale="en-US",
                ignore_https_errors=True,
                device_scale_factor=DEVICE_SCALE_FACTOR,
            )
            page = ctx.new_page()
            try:
                try:
                    page.goto(url, wait_until="networkidle", timeout=timeout_ms)
                except PWTimeout:
                    page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)

                # Ensure we capture the top of the page
                try:
                    page.evaluate("window.scrollTo(0, 0)")
                except Exception:
                    pass

                if not _looks_like_challenge(page):
                    page.screenshot(path=img_path, full_page=FULL_PAGE)
                    return img_path
            finally:
                try: ctx.close()
                except Exception: pass

            # 2) Challenge detected -> open visible window and HOLD
            _cleanup_profile_locks(PROFILE_DIR)  # clean again just in case
            ctx2 = p.chromium.launch_persistent_context(
                user_data_dir=PROFILE_DIR,
                headless=False,
                accept_downloads=False,
                args=args,
                viewport={"width": width, "height": height},
                user_agent=HEADERS["User-Agent"],
                timezone_id="Europe/Paris",
                locale="en-US",
                ignore_https_errors=True,
                device_scale_factor=DEVICE_SCALE_FACTOR,
            )
            page2 = ctx2.new_page()
            page2.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            page2.bring_to_front()

            MIN_VISIBLE_MS = 5_000
            MAX_WAIT_MS = 120_000
            POLL_MS = 1_500

            page2.wait_for_timeout(MIN_VISIBLE_MS)

            waited = 0
            while waited < MAX_WAIT_MS:
                # Keep viewport at the top
                try:
                    page2.evaluate("window.scrollTo(0, 0)")
                except Exception:
                    pass
                if not _looks_like_challenge(page2):
                    break
                page2.wait_for_timeout(POLL_MS)
                waited += POLL_MS

            if _looks_like_challenge(page2):
                try: ctx2.close()
                except Exception: pass
                raise RuntimeError("Human-verification still active after 2 minutes. Finish it in the window and try again.")

            # Screenshot only the viewport area (no full page)
            page2.screenshot(path=img_path, full_page=FULL_PAGE)
            try: ctx2.close()
            except Exception: pass

    return img_path

# ---------------- Qt App ----------------

class Signals(QObject):
    progress = Signal(int)
    status = Signal(str)
    list_filled = Signal(list)              # List[Tuple[str,BmLink]]
    preview_ready = Signal(QPixmap)
    preview_failed = Signal(str)

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Bookmark Viewer (Qt + Playwright)")
        self.resize(1100, 700)

        self.sig = Signals()
        self.sig.progress.connect(self.on_progress)
        self.sig.status.connect(self.on_status)
        self.sig.list_filled.connect(self.on_list_filled)
        self.sig.preview_ready.connect(self.on_preview_ready)
        self.sig.preview_failed.connect(self.on_preview_failed)

        # Top controls
        top = QWidget(); top_layout = QHBoxLayout(top)
        self.file_edit = QLineEdit(); self.file_edit.setPlaceholderText("Select bookmarks HTML…")
        browse_btn = QPushButton("Browse…"); browse_btn.clicked.connect(self.on_browse)
        self.folder_combo = QComboBox(); self.folder_combo.setEditable(False)
        self.folder_combo.addItem("All folders", "")
        self.check_box = QCheckBox("Check links before listing"); self.check_box.setChecked(True)
        scan_btn = QPushButton("Scan"); scan_btn.clicked.connect(self.on_scan)
        clear_btn = QPushButton("Clear cache"); clear_btn.clicked.connect(self.on_clear_cache)
        # Pressing Return in the file path field triggers Scan
        try:
            self.file_edit.returnPressed.connect(self.on_scan)
        except Exception:
            pass

        top_layout.addWidget(self.file_edit, 4)
        top_layout.addWidget(browse_btn, 0)
        top_layout.addWidget(self.folder_combo, 3)
        top_layout.addWidget(self.check_box, 0)
        top_layout.addWidget(clear_btn, 0)
        top_layout.addWidget(scan_btn, 0)

        # Progress + status
        self.progress = QProgressBar(); self.progress.setMaximum(100)
        self.status = QLabel("Ready"); self.status.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        pbar = QWidget(); pbar_l = QHBoxLayout(pbar)
        pbar_l.addWidget(self.progress, 3); pbar_l.addWidget(self.status, 1)

        # Split main area
        split = QSplitter(Qt.Horizontal)
        # Left list
        left = QWidget(); left_layout = QVBoxLayout(left)
        self.list = QListWidget(); self.list.itemSelectionChanged.connect(self.on_select)
        left_layout.addWidget(self.list)
        # Right preview
        right = QWidget(); right_layout = QVBoxLayout(right)
        self.preview = QLabel("(Click a bookmark to preview)")
        self.preview.setAlignment(Qt.AlignCenter)
        self.preview.setMinimumSize(QSize(300, 300))
        right_layout.addWidget(self.preview)

        split.addWidget(left); split.addWidget(right)
        split.setStretchFactor(0, 1); split.setStretchFactor(1, 2)

        # Central layout
        central = QWidget(); lay = QVBoxLayout(central)
        lay.addWidget(top)
        lay.addWidget(pbar)
        lay.addWidget(split, 1)
        self.setCentralWidget(central)

        self.items: List[Tuple[str,BmLink]] = []  # (url, BmLink)
        self._preview_thread: Optional[threading.Thread] = None  # prevent overlap

    # --- UI handlers ---

    def on_browse(self):
        path, _ = QFileDialog.getOpenFileName(self, "Select bookmarks HTML", "", "HTML files (*.html *.htm);;All files (*)")
        if path:
            self.file_edit.setText(path)
            try:
                links = load_bookmarks(path)
                folders = gather_folder_paths(links)
                self.folder_combo.clear()
                self.folder_combo.addItem("All folders", "")
                for f in folders:
                    self.folder_combo.addItem(f, f)
            except Exception as e:
                QMessageBox.warning(self, "Folder list", f"Couldn't parse folders: {e}")

    def on_scan(self):
        path = self.file_edit.text().strip()
        if not path or not os.path.isfile(path):
            QMessageBox.critical(self, "Missing file", "Please choose a bookmarks HTML file.")
            return
        folder = (self.folder_combo.currentData() or "").strip()
        self.list.clear()
        self.preview.setText("(Click a bookmark to preview)")
        self.preview.setPixmap(QPixmap())
        self.status.setText("Parsing…"); self.progress.setValue(0)

        t = threading.Thread(target=self._worker_scan, args=(path, folder, self.check_box.isChecked()), daemon=True)
        t.start()
    def on_clear_cache(self):
        """Delete cached preview images and notify the user."""
        cache_dir = screenshot_cache_dir()
        try:
            if os.path.isdir(cache_dir):
                shutil.rmtree(cache_dir)
            # Recreate empty cache dir so future writes succeed
            os.makedirs(cache_dir, exist_ok=True)
            QMessageBox.information(self, "Cache cleared", "Preview cache was cleared.")
        except Exception as e:
            QMessageBox.warning(self, "Cache", f"Couldn't clear cache: {e}")

    def _worker_scan(self, file_path: str, folder: str, do_check: bool):
        try:
            links = load_bookmarks(file_path)
            sel = select_folder(links, folder)
            # de-dupe
            seen: Set[str] = set()
            deduped: List[Tuple[str,BmLink]] = []
            for b in sel:
                n = normalize_url(b.href)
                if not n or n in seen: continue
                seen.add(n); deduped.append((n, b))

            items = deduped
            total = len(items)
            if do_check:
                self.sig.status.emit(f"Checking {total} link(s)…")
                items = filter_valid(items)
                self.sig.progress.emit(100)
                self.sig.status.emit(f"Valid: {len(items)}/{total}")
            else:
                self.sig.progress.emit(100)
                self.sig.status.emit(f"Found: {len(items)}")

            self.sig.list_filled.emit(items)
        except Exception as e:
            self.sig.status.emit(f"Error: {e}")
            QMessageBox.critical(self, "Error", str(e))

    def on_list_filled(self, items: list):
        self.items = items
        for u, b in items:
            it = QListWidgetItem(f"{b.title}   —   {host_of(u)}")
            it.setData(Qt.UserRole, u)
            self.list.addItem(it)
        if not items:
            self.status.setText("No links found for that folder.")

    def on_select(self):
        # Prevent overlapping previews (which would fight for the profile)
        if self._preview_thread and self._preview_thread.is_alive():
            self.status.setText("Preview in progress…")
            return
        sel = self.list.currentItem()
        if not sel: return
        url = sel.data(Qt.UserRole)
        self.status.setText("Capturing screenshot…")
        self._preview_thread = threading.Thread(target=self._worker_preview, args=(url,), daemon=True)
        self._preview_thread.start()

    def _worker_preview(self, url: str):
        try:
            # measure current label size
            max_w = max(320, self.preview.width() - 16)
            max_h = max(280, self.preview.height() - 16)
            path = take_screenshot(url)
            im = Image.open(path); im.load()
            im = fit_pil(im, max_w, max_h)
            pm = pil_to_qpixmap(im)
            self.sig.preview_ready.emit(pm)
        except Exception as e:
            self.sig.preview_failed.emit(str(e))

    def on_preview_ready(self, pm: QPixmap):
        self.preview.setPixmap(pm)
        self.preview.setText("")
        self.status.setText("")

    def on_preview_failed(self, msg: str):
        self.preview.setPixmap(QPixmap())
        self.preview.setText("(Preview unavailable)")
        self.status.setText("Screenshot error")
        QMessageBox.warning(self, "Preview error", msg)

    def on_progress(self, v: int):
        self.progress.setValue(v)

    def on_status(self, s: str):
        self.status.setText(s)


def main():
    app = QApplication(sys.argv)
    w = MainWindow()
    w.show()
    sys.exit(app.exec())

if __name__ == "__main__":
    main()
