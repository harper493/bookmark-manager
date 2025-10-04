#!/usr/bin/env python3
from __future__ import annotations
import sys, os, argparse, shutil
from typing import List, Tuple, Set

# Lazy imports so CLI can run without PySide6
from bookmarks import (
    BmLink,
    load_bookmarks_html,
    select_folder,
    find_chrome_profiles,
    load_chrome_bookmarks_file,
)
from utils import normalize_url, url_hash, filter_valid
from preview import take_screenshot


def run_cli(argv: List[str]) -> int:
    p = argparse.ArgumentParser(description="Bookmark Viewer CLI (no GUI)")
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--chrome", action="store_true", help="Use live Chrome/Chromium bookmarks")
    src.add_argument("--html", metavar="FILE", help="Path to bookmarks HTML export")
    p.add_argument("--profile", help="Chrome profile name contains this text (when multiple found)")
    p.add_argument("--folder", default="", help="Folder path to filter (e.g. 'Foo/Bar'). Default: all")
    p.add_argument("--check", action="store_true", help="HEAD/GET check links before preview")
    p.add_argument("--limit", type=int, default=0, help="Limit number of links (0 = no limit)")
    p.add_argument("--out", "--shots", dest="out", default="shots", help="Output directory for PNG previews")

    args = p.parse_args(argv)

    # Load links
    if args.chrome:
        profiles = find_chrome_profiles()
        if not profiles:
            print("No Chrome/Chromium profiles with Bookmarks file found.", file=sys.stderr)
            return 2
        chosen = 0
        if args.profile:
            name_l = args.profile.lower()
            for i, (name, _path) in enumerate(profiles):
                if name_l in name.lower():
                    chosen = i
                    break
        prof_name, path = profiles[chosen]
        print(f"Using Chrome profile: {prof_name}")
        links = load_chrome_bookmarks_file(path)
    else:
        if not os.path.isfile(args.html):
            print(f"HTML file not found: {args.html}", file=sys.stderr)
            return 2
        links = load_bookmarks_html(args.html)
        print(f"Loaded {len(links)} links from HTML.")

    # Folder filter
    folder = (args.folder or "").strip()
    if folder:
        links = select_folder(links, folder)
        print(f"Folder '{folder}': {len(links)} links")
    else:
        print(f"All folders: {len(links)} links")

    # De-duplicate
    seen: Set[str] = set()
    items: List[Tuple[str, BmLink]] = []
    for b in links:
        n = normalize_url(b.href)
        if n and n not in seen:
            seen.add(n)
            items.append((n, b))
    if args.limit and args.limit > 0:
        items = items[: args.limit]
    print(f"After de-dup: {len(items)} unique URLs")

    # Optional validity check
    if args.check:
        items = filter_valid(items)
        print(f"Valid after check: {len(items)}")

    # Ensure output dir
    out_dir = os.path.abspath(args.out)
    os.makedirs(out_dir, exist_ok=True)

    # Generate previews (sequential)
    total = len(items)
    for i, (url, b) in enumerate(items, 1):
        print(f"[{i}/{total}] {url}")
        try:
            src_path = take_screenshot(url)
        except Exception as e:
            print(f"  ! preview error: {e}")
            continue
        dest = os.path.join(out_dir, f"{i:04d}_{url_hash(url)}.png")
        try:
            shutil.copyfile(src_path, dest)
        except Exception as e:
            print(f"  ! save error: {e}")
            continue

    print(f"Done. Previews saved to: {out_dir}")
    return 0


def run_gui() -> int:
    try:
        from PySide6.QtWidgets import QApplication
        from ui import MainWindow
    except Exception:
        print("PySide6 is not available; run CLI mode instead (see --help).", file=sys.stderr)
        return 3
    app = QApplication(sys.argv)
    w = MainWindow()
    w.show()
    return app.exec()


def main():
    # Force CLI if --cli is present
    if "--cli" in sys.argv[1:]:
        argv = [a for a in sys.argv[1:] if a != "--cli"]
        sys.exit(run_cli(argv))

    # Otherwise: if any known CLI flags are present, run CLI; else run GUI
    cli_flags = {"--chrome", "--html", "--shots", "--out", "--folder", "--check", "--limit", "--profile"}
    if any(f in sys.argv[1:] for f in cli_flags):
        sys.exit(run_cli(sys.argv[1:]))

    sys.exit(run_gui())


if __name__ == "__main__":
    main()
