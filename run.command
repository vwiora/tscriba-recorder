#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

TRANSCRIBER_PY="/Users/volkerwiora/Projects/Transcriba Transcription Manager/.venv/bin/python"
if [ -x "$TRANSCRIBER_PY" ]; then
  DEFAULT_PY="$TRANSCRIBER_PY"
else
  DEFAULT_PY="python3.12"
fi

PYTHON_BIN="${PYTHON_BIN:-$DEFAULT_PY}"

if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  echo "ERROR: $PYTHON_BIN not found."
  echo "Install Python 3.12 with Tk support (e.g. brew install python@3.12)"
  echo "or run with: PYTHON_BIN=/opt/homebrew/bin/python3.12 ./run.command"
  exit 1
fi

if [ -d ".venv" ]; then
  VENV_PY=".venv/bin/python"
  VENV_VER="$("$VENV_PY" -V 2>/dev/null || true)"
  if [[ "$VENV_VER" != *"$("$PYTHON_BIN" -V 2>/dev/null | awk '{print $2}')"* ]]; then
    echo "Recreating .venv with $PYTHON_BIN (current: ${VENV_VER:-unknown})..."
    rm -rf .venv
  fi
fi

if [ ! -d ".venv" ]; then
  echo "Creating virtual environment in .venv..."
  "$PYTHON_BIN" -m venv .venv
fi

# shellcheck disable=SC1091
source ".venv/bin/activate"

VENV_PY=".venv/bin/python"

echo "Using Python: $("$VENV_PY" -V) at $("$VENV_PY" -c 'import sys; print(sys.executable)')"

if ! "$VENV_PY" - >/dev/null 2>&1 <<'PY'; then
import _tkinter  # noqa: F401
PY
  echo "ERROR: Tk (_tkinter) is not available in this Python."
  echo "Try: PYTHON_BIN=/opt/homebrew/bin/python3.12 ./run.command"
  exit 1
fi

if ! "$VENV_PY" - >/dev/null 2>&1 <<'PY'; then
import faster_whisper  # noqa: F401
PY
  echo "Missing dependency: faster-whisper"
  echo "Installing dependencies from requirements.txt..."
  if [ ! -f "requirements.txt" ]; then
    echo "ERROR: requirements.txt not found."
    exit 1
  fi
  "$VENV_PY" -m pip install -r requirements.txt
fi

"$VENV_PY" tscriba_recorder_app.py
