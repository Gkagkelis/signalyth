#!/bin/bash
set -e
cd "$(dirname "$0")"

if ! command -v python3 >/dev/null 2>&1; then
  echo "Python 3 is required. Install Python 3, then run this file again."
  read -r -p "Press Return to close..."
  exit 1
fi

VENV_PY=".venv/bin/python3"

# A virtual environment can be moved with the app folder. Never rely on
# 'activate' because its VIRTUAL_ENV path is absolute and becomes stale.
if [ ! -x "$VENV_PY" ]; then
  echo "Preparing SIGNALYTH local environment (first launch only)..."
  rm -rf .venv
  python3 -m venv .venv
fi

# Verify the venv interpreter itself is usable; recreate only if necessary.
if ! "$VENV_PY" -c 'import sys; assert sys.version_info >= (3, 10)' >/dev/null 2>&1; then
  echo "Refreshing SIGNALYTH local environment..."
  rm -rf .venv
  python3 -m venv .venv
fi

STAMP=".venv/.signalyth_requirements_ready"
if [ ! -f "$STAMP" ] || [ requirements.txt -nt "$STAMP" ]; then
  echo "Installing/updating SIGNALYTH requirements..."
  "$VENV_PY" -m pip install -q --upgrade pip
  "$VENV_PY" -m pip install -q -r requirements.txt
  touch "$STAMP"
fi

URL="http://127.0.0.1:8000"
echo "Starting SIGNALYTH v1.8.4.3 (credential-safe local hotfix) at $URL"
(sleep 2; open "$URL") >/dev/null 2>&1 &
exec "$VENV_PY" -m uvicorn app.main:app --host 127.0.0.1 --port 8000
