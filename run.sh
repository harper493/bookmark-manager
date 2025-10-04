#!/usr/bin/env bash
set -euo pipefail

# Resolve project root (where this script lives)
DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"

# 1) Virtualenv (create if missing)
if [ ! -d "$DIR/.venv" ]; then
  python3 -m venv "$DIR/.venv"
fi
# shellcheck disable=SC1091
source "$DIR/.venv/bin/activate"

# 2) Make pip quiet (no "Requirement already satisfied")
export PIP_DISABLE_PIP_VERSION_CHECK=1
export PIP_NO_WARN_SCRIPT_LOCATION=1

# Upgrade pip quietly (stdout hidden, errors still show)
python -m pip install -U pip --progress-bar off -q 1>/dev/null

# Install deps quietly
if [ -f "$DIR/requirements.txt" ]; then
  python -m pip install -r "$DIR/requirements.txt" --progress-bar off -q 1>/dev/null
else
  python -m pip install beautifulsoup4 httpx pillow lxml playwright PySide6 \
    --progress-bar off -q 1>/dev/null
fi

# 3) Keep Playwright browsers local to the project
export PLAYWRIGHT_BROWSERS_PATH="$DIR/.pw-browsers"

# 4) Ensure Chromium is downloaded (leave Playwright output visible)
if [ ! -d "$PLAYWRIGHT_BROWSERS_PATH" ] || \
   ! find "$PLAYWRIGHT_BROWSERS_PATH" -maxdepth 1 -type d -name "chromium-*" -print -quit 2>/dev/null | grep -q . ; then
  python -m playwright install chromium
fi

# 5) Run the app
exec python "$DIR/bookmark_viewer_qt.py"
