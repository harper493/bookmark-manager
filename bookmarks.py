#!/usr/bin/env python3
from __future__ import annotations
import os, sys, json
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple, Set

try:
    from bs4 import BeautifulSoup, Tag
except Exception:
    BeautifulSoup = None  # type: ignore
    Tag = object  # type: ignore

@dataclass
class BmLink:
    title: str
    href: str
    folder_path: str  # e.g., "Foo/Bar"

# -------- Netscape HTML (robust, tolerates missing </DT>) --------

def _make_soup(data: bytes):
    if BeautifulSoup is None:
        raise RuntimeError("BeautifulSoup (bs4) not installed. Use Load Chrome… or install bs4.")
    try:
        return BeautifulSoup(data, "lxml")
    except Exception:
        return BeautifulSoup(data, "html.parser")


def load_bookmarks_html(file_path: str) -> List[BmLink]:
    with open(file_path, "rb") as f:
        data = f.read()
    soup = _make_soup(data)
    dl = soup.find("dl")
    if not dl:
        raise RuntimeError("Could not find <DL> in the bookmarks file.")
    out: List[BmLink] = []
    _walk_dl(dl, [], out)
    return out


def _nearest_prev_header(parent_dl: Tag, child: Tag) -> Optional[str]:
    sib = child.previous_sibling
    while sib is not None:
        if getattr(sib, "name", None):
            nm = sib.name.lower()
            if nm in ("h1", "h2", "h3"):
                return sib.get_text(strip=True)
            if nm in ("dt", "p"):
                h = sib.find(["h3", "h2", "h1"], recursive=True)
                if h:
                    return h.get_text(strip=True)
        sib = getattr(sib, "previous_sibling", None)
    return None


def _anchors_in_current_folder(node: Tag, current_dl: Tag):
    anchors = []
    for a in node.find_all("a", href=True):
        anc = a.find_parent("dl")
        if anc is current_dl:
            anchors.append(a)
    return anchors


def _walk_dl(current_dl: Tag, path_stack: List[str], out: List[BmLink]):
    last_header: Optional[str] = None
    for child in list(current_dl.children):
        nm = getattr(child, "name", "").lower()
        if not nm:
            continue
        if nm in ("dt", "p"):
            h = child.find(["h3", "h2", "h1"], recursive=True)
            if h:
                last_header = h.get_text(strip=True)
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
            if nm in ("h3", "h2", "h1"):
                last_header = child.get_text(strip=True)
            if nm == "a" and child.has_attr("href"):
                href = child["href"]; title = child.get_text(strip=True) or href
                out.append(BmLink(title=title, href=href, folder_path="/".join(path_stack)))

# -------- Folder helpers --------

def gather_folder_paths(links: List[BmLink]) -> List[str]:
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


def select_folder(links: List[BmLink], target_path: str) -> List[BmLink]:
    target = (target_path or "").strip().strip("/")
    if not target:
        return links[:]
    if "/" in target:
        tci = target.lower(); return [b for b in links if b.folder_path.lower() == tci]
    seg = target.lower()
    return [b for b in links if (b.folder_path.lower().split("/")[-1] if b.folder_path else "") == seg]

# -------- Chrome live bookmarks --------

def _chrome_base_dirs() -> List[Path]:
    home = Path.home()
    if os.name == "nt":
        local = os.environ.get("LOCALAPPDATA")
        if local:
            base = Path(local)
            return [base / "Google/Chrome/User Data", base / "Chromium/User Data"]
        return []
    if sys.platform == "darwin":  # type: ignore
        return [home / "Library/Application Support/Google/Chrome",
                home / "Library/Application Support/Chromium"]
    return [home / ".config/google-chrome", home / ".config/chromium"]


def find_chrome_profiles() -> List[Tuple[str, Path]]:
    out: List[Tuple[str, Path]] = []
    for base in _chrome_base_dirs():
        if not base.exists():
            continue
        names = ["Default"]
        try:
            names += [d.name for d in base.iterdir() if d.is_dir() and d.name.startswith("Profile ")]
        except Exception:
            pass
        for prof in sorted(set(names), key=str.lower):
            p = base / prof / "Bookmarks"
            if p.exists() and p.is_file():
                out.append((f"{base.name} / {prof}", p))
    return out


def load_chrome_bookmarks_file(path: Path) -> List[BmLink]:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    roots = (data or {}).get("roots", {})
    out: List[BmLink] = []

    def walk(node: dict, stack: List[str]):
        if not isinstance(node, dict):
            return
        t = node.get("type")
        if t == "url":
            url = node.get("url") or ""; title = node.get("name") or url
            out.append(BmLink(title=title, href=url, folder_path="/".join(stack)))
        elif t == "folder":
            name = node.get("name") or ""
            new_stack = stack + ([name] if name else [])
            for ch in node.get("children", []) or []:
                walk(ch, new_stack)

    mapping = [("Bookmarks Bar", roots.get("bookmark_bar")),
               ("Other Bookmarks", roots.get("other")),
               ("Mobile Bookmarks", roots.get("synced"))]
    for root_name, node in mapping:
        if isinstance(node, dict):
            base_stack = [root_name]
            for ch in node.get("children", []) or []:
                walk(ch, base_stack)
    return out
