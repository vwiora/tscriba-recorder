#!/usr/bin/env bash
set -euo pipefail

WEBCAPTURE_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="$WEBCAPTURE_DIR/.venv"

python3 -m venv "$VENV_DIR"
"$VENV_DIR/bin/python" -m pip install --upgrade pip
"$VENV_DIR/bin/pip" install -r "$WEBCAPTURE_DIR/requirements.txt"

echo "Python env ready: $VENV_DIR"
echo "PyInstaller: $VENV_DIR/bin/pyinstaller"
