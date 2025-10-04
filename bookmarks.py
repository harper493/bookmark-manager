#!/usr/bin/env python3
"""
Open bookmarks from a selected folder (Chrome/Firefox Netscape export),
deduplicate (ignoring query/fragment), optionally check links, and (optionally)
open them in your browser with polite, per-host delays that only trigger on
repeat visits.

Examples:
  python open_bookmarks.py -f ~/Downloads/bookmarks.html -p "Bookmarks Bar/My Folder" -l 20 -t 1.5
  python open_bookmarks.py -f bookmarks.html -p "Reading List" --no-open  # just report
"""

import argparse
import time
import sys
import html
import urllib.parse as urlparse
from dataclasses import dataclass
from typing import Iterable, List, Tuple, Dict, Optional, Set

# Third-party:
#   pip install beautifulsoup4 httpx
#   (optional) pip install lxml "httpx[http2]"
from bs4 import BeautifulSoup, Tag, NavigableString
import httpx
import webbrowser


# ---------- small argparse helper for --flag / --no-flag (portable) ----------
def add_bool_arg(parser: argparse.ArgumentParser, name: str, default: bool, help_on: str, help_off: str):
    dest = name.replace("-", "_")
    group = parser.add_mutually_exclusive_group()
    group.add_argument(f"--{name}", dest=dest, action="store_true", help=help_on)
    group.add_argument(f"--no-{name}", dest=dest, action="store_false", help=help_off)
    parser.set_defaults(**{dest: default})
    return dest


# ---------- URL utilities ----------

def normalize_url(raw: str) -> str:
    """Normalize for de-dupe:
    - strip whitespace, decode HTML entities
    - lowercase scheme+host
    - drop default ports
    - drop query & fragment
    - trim trailing slash (but keep root '/')
    """
    if not raw:
        return ""
    raw = html.unescape(raw.strip())
    try:
        u = urlparse.urlsplit(raw)
    except Exception:
        return raw

    scheme = (u.scheme or "http").lower()
    netloc = u.netloc.lower()

    if netloc.endswith(":80") and scheme == "http":
        netloc = netloc[:-3]
    if netloc.endswith(":443") and scheme == "https":
        netloc = netloc[:-4]

    path = u.path or "/"
    if path != "/" and path.endswith("/"):
        path = path[:-1]

    # Drop params, query, fragment
    clean = urlparse.urlunsplit((scheme, netloc, path, "", ""))

    if not scheme.startswith("http"):
        clean = "https://" + clean
    return clean


def host_of(url: str) -> str:
    try:
        return urlparse.urlsplit(url).netloc.lower()
    except Exception:
        return ""


# ---------- Robust Netscape walker (no </DT> required) ----------

@dataclass
class BmLink:
    title: str
    href: str
    folder_path: str  # e.g., "Bookmarks Bar/My Folder"

def make_soup(data: bytes) -> "BeautifulSoup":
    # Prefer lxml for robustness, but fall back to built-in html.parser
    try:
        return BeautifulSoup(data, "lxml")
    except Exception:
        return BeautifulSoup(data, "html.parser")

def _nearest_prev_header(parent: Tag, child: Tag) -> Optional[str]:
    """In parent <DL>, find the nearest preceding folder header (<H3>/<H2>/<H1>),
    possibly inside a preceding <DT> or <p>."""
    sib = child.previous_sibling
    while sib is not None:
        if isinstance(sib, Tag):
            nm = sib.name.lower()
            if nm in ("h1", "h2", "h3"):
                return sib.get_text(strip=True)
            if nm in ("dt", "p"):
                h = sib.find(["h3", "h2", "h1"], recursive=True)
                if h:
                    return h.get_text(strip=True)
        sib = sib.previous_sibling
    return None

def _anchors_in_current_folder(node: Tag, current_dl: Tag) -> List[Tag]:
    """Return all <a href> under 'node' that belong to the *current* folder level:
    i.e., their nearest ancestor <dl> is current_dl (not a nested one)."""
    anchors: List[Tag] = []
    for a in node.find_all("a", href=True):
        anc = a.find_parent("dl")
        if anc is current_dl:
            anchors.append(a)
    return anchors

def _walk_dl(current_dl: Tag, path_stack: List[str], out: List[BmLink]):
    """Walk a <DL> in document order:
       - Collect all links whose nearest ancestor <DL> is this one (they belong to this folder).
       - Track the most recent header (H3) seen; when a child <DL> appears, recurse into it using that header.
       - Works even when exports omit </DT> and wrap things in <p>.
    """
    last_header: Optional[str] = None

    for child in list(current_dl.children):
        if isinstance(child, NavigableString):
            continue
        if not isinstance(child, Tag):
            continue

        nm = child.name.lower()

        if nm in ("dt", "p"):
            # Update last seen header (folder name) if present
            h = child.find(["h3", "h2", "h1"], recursive=True)
            if h:
                last_header = h.get_text(strip=True)

            # Collect all anchors at *this* folder level from this subtree
            for a in _anchors_in_current_folder(child, current_dl):
                href = a["href"]
                title = a.get_text(strip=True) or href
                out.append(BmLink(title=title, href=href, folder_path="/".join(path_stack)))

            # Recurse into any nested DLs inside this block using the nearest relevant header
            for sub_dl in child.find_all("dl", recursive=False):
                folder_name = last_header or _nearest_prev_header(current_dl, sub_dl)
                if folder_name:
                    path_stack.append(folder_name)
                    _walk_dl(sub_dl, path_stack, out)
                    path_stack.pop()
                else:
                    # No header found; still walk to avoid losing links
                    _walk_dl(sub_dl, path_stack, out)

        elif nm == "dl":
            # A bare nested DL at this level — associate it to the nearest previous header
            folder_name = last_header or _nearest_prev_header(current_dl, child)
            if folder_name:
                path_stack.append(folder_name)
                _walk_dl(child, path_stack, out)
                path_stack.pop()
            else:
                _walk_dl(child, path_stack, out)

        else:
            # Handle headers directly under DL (rare but seen)
            if nm in ("h3", "h2", "h1"):
                last_header = child.get_text(strip=True)
            # Handle anchors placed directly under DL (very rare)
            if nm == "a" and child.has_attr("href"):
                href = child["href"]
                title = child.get_text(strip=True) or href
                out.append(BmLink(title=title, href=href, folder_path="/".join(path_stack)))

def load_bookmarks(file_path: str) -> List[BmLink]:
    with open(file_path, "rb") as f:
        data = f.read()
    soup = make_soup(data)
    dl = soup.find("dl")
    if not dl:
        raise RuntimeError("Could not find <DL> in the bookmarks file. Is it a Netscape export?")
    out: List[BmLink] = []
    _walk_dl(dl, [], out)
    return out

def select_folder(links: List[BmLink], target_path: str) -> List[BmLink]:
    """
    Match either exact path or last-segment folder name.
      - "Bookmarks Bar/My Folder"  -> exact match only
      - "My Folder"                -> match any folder whose last segment equals it
    """
    target_path = target_path.strip().strip("/")
    if "/" in target_path:
        tci = target_path.lower()
        return [b for b in links if b.folder_path.lower() == tci]
    else:
        seg = target_path.lower()
        return [b for b in links if b.folder_path.lower().split("/")[-1] == seg]


# ---------- Link checking with per-host pacing ----------

class LinkChecker:
    def __init__(self, timeout: float = 10.0, per_host_delay: float = 1.0):
        self.timeout = timeout
        self.per_host_delay = per_host_delay
        self.last_touch: Dict[str, float] = {}

        def make_client(http2: bool) -> httpx.Client:
            return httpx.Client(
                http2=http2,
                timeout=httpx.Timeout(timeout, connect=timeout),
                headers={
                    # Browser-like headers to dodge some 403s
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/120.0.0.0 Safari/537.36"
                    ),
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Accept-Language": "en-US,en;q=0.9",
                    "Connection": "keep-alive",
                    "Upgrade-Insecure-Requests": "1",
                },
                follow_redirects=True,  # client-level redirect following
                verify=True,
            )

        # Prefer HTTP/2 but fall back silently if h2 isn't installed
        try:
            self.session = make_client(http2=True)
            _ = self.session.http2  # trigger AttributeError if unsupported
        except Exception:
            self.session = make_client(http2=False)

    def _pace(self, url: str):
        host = host_of(url)
        if not host:
            return
        now = time.time()
        last = self.last_touch.get(host)
        if last is not None:
            sleep_for = max(0.0, self.per_host_delay - (now - last))
            if sleep_for > 0:
                time.sleep(sleep_for)
        self.last_touch[host] = time.time()

    def check(self, url: str) -> Tuple[bool, int, str]:
        """
        Return (is_ok, status_code, reason)
          1) HEAD
          2) If 4xx/5xx or suspicious, GET
          3) If 403, retry GET with Safari-like headers
        """
        try:
            self._pace(url)
            r = self.session.head(url)  # NOTE: no allow_redirects kw in httpx
            sc = r.status_code

            # Some servers lie on HEAD; fall back to GET
            if sc >= 400 or sc in (400, 401, 403, 405) or "content-length" not in r.headers:
                self._pace(url)
                r = self.session.get(url)
                sc = r.status_code

            if sc == 403:
                # Soft retry: tweak headers
                self._pace(url)
                try:
                    s2 = httpx.Client(http2=True, timeout=self.timeout, follow_redirects=True, headers={
                        "User-Agent": (
                            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                            "AppleWebKit/605.1.15 (KHTML, like Gecko) "
                            "Version/17.0 Safari/605.1.15"
                        ),
                        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                        "Accept-Language": "en-US,en;q=0.9",
                        "Connection": "keep-alive",
                    })
                    _ = s2.http2
                except Exception:
                    s2 = httpx.Client(http2=False, timeout=self.timeout, follow_redirects=True, headers={
                        "User-Agent": (
                            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                            "AppleWebKit/605.1.15 (KHTML, like Gecko) "
                            "Version/17.0 Safari/605.1.15"
                        ),
                        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                        "Accept-Language": "en-US,en;q=0.9",
                        "Connection": "keep-alive",
                    })
                with s2 as s2c:
                    r = s2c.get(url)
                    sc = r.status_code

            ok = 200 <= sc < 400
            return ok, sc, r.reason_phrase
        except httpx.ReadTimeout:
            return False, 408, "Timeout"
        except httpx.ConnectTimeout:
            return False, 408, "Connect Timeout"
        except httpx.ConnectError as e:
            return False, 503, f"Connect Error: {e}"
        except Exception as e:
            return False, 520, f"Error: {e}"


# ---------- Opening tabs with per-host pacing ----------

def open_tabs(urls: Iterable[str], per_host_delay: float, dry_run: bool = False):
    last_touch: Dict[str, float] = {}
    for url in urls:
        host = host_of(url)
        now = time.time()
        if host in last_touch:
            sleep_for = max(0.0, per_host_delay - (now - last_touch[host]))
            if sleep_for > 0:
                time.sleep(sleep_for)
        last_touch[host] = time.time()
        if not dry_run:
            webbrowser.open_new_tab(url)


# ---------- Main ----------

def main():
    ap = argparse.ArgumentParser(description="Open/verify bookmarks from a specific folder with per-host pacing.")
    ap.add_argument("-f", "--file", required=True, help="Path to bookmarks HTML export (Netscape format).")
    ap.add_argument("-p", "--path", required=True, help="Folder path or folder name (e.g., 'Bookmarks Bar/My Stuff' or just 'My Stuff').")
    ap.add_argument("-l", "--limit", type=int, default=0, help="Max number of links to process (0 = no limit).")
    ap.add_argument("-t", "--delay", type=float, default=1.0, help="Per-host delay (seconds) applied only on repeat visits).")
    add_bool_arg(ap, "check", True, "Check links before opening (default).", "Skip link checking.")
    add_bool_arg(ap, "open", True, "Open tabs after filtering (default).", "Do not open tabs; only report.")
    add_bool_arg(ap, "no-dedupe", False, "Disable de-duplication.", "Enable de-duplication (default).")
    ap.add_argument("-v", "--verbose", action="store_true", help="Verbose output.")
    args = ap.parse_args()

    # Load + select
    links = load_bookmarks(args.file)
    selected = select_folder(links, args.path)
    if not selected:
        print(f"No links found under folder '{args.path}'.", file=sys.stderr)
        sample_paths = sorted({b.folder_path for b in links})
        hint = "\n".join(sample_paths[:25])
        print(f"\nSome available paths:\n{hint}", file=sys.stderr)
        sys.exit(2)

    # De-duplicate by normalized URL (unless disabled)
    if args.no_dedupe:
        deduped: List[Tuple[str, BmLink]] = []
        for b in selected:
            n = normalize_url(b.href)
            if n:
                deduped.append((n, b))
    else:
        seen: Set[str] = set()
        deduped = []
        for b in selected:
            n = normalize_url(b.href)
            if not n or n in seen:
                continue
            seen.add(n)
            deduped.append((n, b))

    if args.limit and args.limit > 0:
        deduped = deduped[:args.limit]

    if args.verbose:
        print(f"Found {len(selected)} links in folder; {len(deduped)} after normalization/dedup.")

    # Optionally check links
    ok_urls: List[str] = []
    if args.check:
        checker = LinkChecker(timeout=12.0, per_host_delay=args.delay)
        for i, (url, b) in enumerate(deduped, 1):
            ok, sc, reason = checker.check(url)
            status = f"{sc} {reason}"
            if ok:
                ok_urls.append(url)
            if args.verbose:
                mark = "OK " if ok else "BAD"
                print(f"[{i:3d}] {mark} {status:>12}  {url}")
    else:
        ok_urls = [u for u, _ in deduped]

    if args.verbose:
        skipped = len(deduped) - len(ok_urls)
        print(f"\nReady to open {len(ok_urls)} links ({skipped} skipped as broken).")

    # Open in browser with per-host pacing (only delay on repeat host)
    if args.open and ok_urls:
        open_tabs(ok_urls, per_host_delay=args.delay, dry_run=False)

    # Summary
    print(f"Processed {len(deduped)} unique links. Opening {len(ok_urls)} link(s).")
    if len(ok_urls) < len(deduped):
        print(f"Skipped {len(deduped) - len(ok_urls)} broken/unreachable link(s).")


if __name__ == "__main__":
    main()
