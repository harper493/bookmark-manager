#!/usr/bin/env python3
from __future__ import annotations
import os, glob, threading
from typing import Optional, Tuple
from PIL import Image, ImageDraw

from utils import screenshot_cache_dir, url_hash, HEADERS

# Optional deps
try:
    import httpx
except Exception:
    httpx = None  # type: ignore

# Playwright is optional; if missing we generate a placeholder
try:
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
except Exception:
    sync_playwright = None  # type: ignore
    PWTimeout = Exception  # type: ignore

# Project-local browser + profile
PW_DIR = os.path.join(os.path.dirname(__file__), ".pw-browsers")
os.makedirs(PW_DIR, exist_ok=True)
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", PW_DIR)

PROFILE_DIR = os.path.join(os.path.dirname(__file__), ".pw-profile")
os.makedirs(PROFILE_DIR, exist_ok=True)

# Serialize access to the persistent Chromium profile
_PW_LOCK = threading.Lock()

VIEWPORT = (1280, 800)
DSF = 2.0
FULL_PAGE = False  # viewport-only

# ---- light resource sniffing to avoid download-only URLs ----

def _sniff(url: str) -> Tuple[str, str]:
    if httpx is None:
        return "", ""
    try:
        with httpx.Client(timeout=8.0, follow_redirects=True, headers=HEADERS) as c:
            r = c.head(url)
            if r.status_code >= 400 or not r.headers.get("content-type"):
                r = c.get(url, headers={"Range": "bytes=0-0"})
            return (r.headers.get("content-type", "").lower(),
                    r.headers.get("content-disposition", "").lower())
    except Exception:
        return "", ""


def _is_image(ct: str) -> bool:
    return (ct or "").startswith("image/")


def _is_htmlish(ct: str) -> bool:
    ct = ct or ""
    return ("text/html" in ct) or ("application/xhtml+xml" in ct) or (ct.startswith("text/") and "xml" in ct)


def _is_pdf(ct: str) -> bool:
    return "application/pdf" in (ct or "")


def _is_download_only(ct: str, cd: str) -> bool:
    if "attachment" in (cd or ""):
        return True
    if not ct:
        return False
    if _is_htmlish(ct) or _is_image(ct) or _is_pdf(ct):
        return False
    return True

# ---- challenge detection (strict to avoid false positives) ----

def _looks_like_challenge(page) -> bool:
    try:
        title = (page.title() or "").strip().lower()
    except Exception:
        title = ""
    try:
        url = (page.url or "")
    except Exception:
        url = ""
    if "/cdn-cgi/challenge" in url or "challenges.cloudflare.com" in url:
        return True
    if ("checking your browser" in title) or ("just a moment" in title):
        return True
    if ("attention required" in title and "cloudflare" in title):
        return True
    return False

# ---- maintenance ----

def _cleanup_profile_locks(profile_dir: str):
    pats = ["Singleton*", "singleton*", "LOCK", "Lockfile", "lockfile",
            "Crashpad", "Crashpad/completed", "Crashpad/pending/*"]
    for pat in pats:
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

# ---- public API ----

def clear_cache():
    cache = screenshot_cache_dir()
    try:
        for f in os.listdir(cache):
            try: os.remove(os.path.join(cache, f))
            except Exception: pass
    except Exception:
        pass


def take_screenshot(url: str) -> str:
    img_path = os.path.join(screenshot_cache_dir(), f"{url_hash(url)}.png")
    if os.path.exists(img_path) and os.path.getsize(img_path) > 0:
        return img_path

    # Pre-sniff
    ct, cd = _sniff(url)
    if _is_download_only(ct, cd):
        # create a small placeholder explaining why
        im = Image.new("RGB", VIEWPORT, (245, 245, 245))
        d = ImageDraw.Draw(im)
        d.text((20, 20), "This link triggers a download; no preview.", fill=(80, 80, 80))
        im.save(img_path)
        return img_path

    if sync_playwright is None:
        # No Playwright here
        im = Image.new("RGB", VIEWPORT, (245, 245, 245))
        d = ImageDraw.Draw(im)
        d.text((20, 20), "Preview requires Playwright.", fill=(80, 80, 80))
        im.save(img_path)
        return img_path

    args = []
    try:
        import os as _os
        if hasattr(_os, "geteuid") and _os.geteuid() == 0:
            args.append("--no-sandbox")
    except Exception:
        pass

    with _PW_LOCK:
        _cleanup_profile_locks(PROFILE_DIR)
        os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", PW_DIR)
        with sync_playwright() as p:
            # 1) headless try
            ctx = p.chromium.launch_persistent_context(
                user_data_dir=PROFILE_DIR,
                headless=True,
                accept_downloads=False,
                args=args,
                viewport={"width": VIEWPORT[0], "height": VIEWPORT[1]},
                device_scale_factor=DSF,
                user_agent=HEADERS["User-Agent"],
                ignore_https_errors=True,
                locale="en-US",
            )
            page = ctx.new_page()
            try:
                try:
                    page.goto(url, wait_until="networkidle", timeout=25000)
                except PWTimeout:
                    page.goto(url, wait_until="domcontentloaded", timeout=25000)
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

            # 2) headful hold for human check
            _cleanup_profile_locks(PROFILE_DIR)
            ctx2 = p.chromium.launch_persistent_context(
                user_data_dir=PROFILE_DIR,
                headless=False,
                accept_downloads=False,
                args=args,
                viewport={"width": VIEWPORT[0], "height": VIEWPORT[1]},
                device_scale_factor=DSF,
                user_agent=HEADERS["User-Agent"],
                ignore_https_errors=True,
                locale="en-US",
            )
            page2 = ctx2.new_page()
            page2.goto(url, wait_until="domcontentloaded", timeout=25000)
            page2.bring_to_front()

            MIN_VISIBLE_MS = 5000
            MAX_WAIT_MS = 120000
            POLL_MS = 1500
            page2.wait_for_timeout(MIN_VISIBLE_MS)
            waited = 0
            while waited < MAX_WAIT_MS:
                try:
                    page2.evaluate("window.scrollTo(0, 0)")
                except Exception:
                    pass
                if not _looks_like_challenge(page2):
                    break
                page2.wait_for_timeout(POLL_MS)
                waited += POLL_MS
            # screenshot regardless; if still challenged, you'll see that page
            page2.screenshot(path=img_path, full_page=FULL_PAGE)
            try: ctx2.close()
            except Exception: pass
            return img_path
