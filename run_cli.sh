#!/usr/bin/env bash
set -euo pipefail
DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"

# venv
if [ ! -d "$DIR/.venv" ]; then
  python3 -m venv "$DIR/.venv"
fi
# shellcheck disable=SC1091
source "$DIR/.venv/bin/activate"

# quiet pip
export PIP_DISABLE_PIP_VERSION_CHECK=1
export PIP_NO_WARN_SCRIPT_LOCATION=1
python -m pip install -U pip --progress-bar off -q 1>/dev/null

if [ -f "$DIR/requirements.txt" ]; then
  python -m pip install -r "$DIR/requirements.txt" --progress-bar off -q 1>/dev/null
else
  python -m pip install beautifulsoup4 httpx lxml Pillow playwright PySide6 --progress-bar off -q 1>/dev/null
fi

# local Playwright browsers (optional for CLI but helpful)
export PLAYWRIGHT_BROWSERS_PATH="$DIR/.pw-browsers"
if [ ! -d "$PLAYWRIGHT_BROWSERS_PATH" ] || \
   ! find "$PLAYWRIGHT_BROWSERS_PATH" -maxdepth 1 -type d -name "chromium-*" -print -quit 2>/dev/null | grep -q . ; then
  python -m playwright install chromium || true
fi

# Pass all args to CLI
exec python "$DIR/main.py" --cli "$@"
