#!/usr/bin/env python3
from __future__ import annotations
import os, html, hashlib, urllib.parse as urlparse
from typing import Tuple
from PIL import Image

# Shared HTTP headers for network ops
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Connection": "keep-alive",
}

# Screenshot cache location
_DEF_CACHE = os.path.join(os.path.expanduser("~"), ".bookmark_viewer_cache_qt")

def screenshot_cache_dir() -> str:
    os.makedirs(_DEF_CACHE, exist_ok=True)
    return _DEF_CACHE


def url_hash(u: str) -> str:
    return hashlib.sha256(u.encode("utf-8", errors="ignore")).hexdigest()[:24]


def normalize_url(raw: str) -> str:
    if not raw:
        return ""
    raw = html.unescape(raw.strip())
    try:
        u = urlparse.urlsplit(raw)
    except Exception:
        return raw
    scheme = (u.scheme or "http").lower()
    netloc = (u.netloc or "").lower()
    if netloc.endswith(":80") and scheme == "http":
        netloc = netloc[:-3]
    if netloc.endswith(":443") and scheme == "https":
        netloc = netloc[:-4]
    path = u.path or "/"
    if path != "/" and path.endswith("/"):
        path = path[:-1]
    clean = urlparse.urlunsplit((scheme, netloc, path, "", ""))
    if not scheme.startswith("http"):
        clean = "https://" + clean
    return clean


def host_of(url: str) -> str:
    try:
        return urlparse.urlsplit(url).netloc.lower()
    except Exception:
        return ""


def fit_image(im: Image.Image, max_w: int, max_h: int) -> Image.Image:
    w, h = im.size
    scale = min(max_w / float(w), max_h / float(h), 1.0)
    new_size = (max(1, int(w * scale)), max(1, int(h * scale)))
    return im.resize(new_size, Image.LANCZOS)

# Optional link check (used by UI)
try:
    import httpx
except Exception:
    httpx = None  # type: ignore

def filter_valid(items):
    if httpx is None:
        return items
    out = []
    with httpx.Client(timeout=10.0, follow_redirects=True, headers=HEADERS) as c:
        for u, b in items:
            try:
                r = c.head(u)
                if r.status_code >= 400:
                    r = c.get(u)
                if 200 <= r.status_code < 400:
                    out.append((u, b))
            except Exception:
                pass
    return out
