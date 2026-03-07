#!/usr/bin/env bash
set -euo pipefail

WEBCAPTURE_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$WEBCAPTURE_DIR"

if [[ -x "$WEBCAPTURE_DIR/.venv/bin/python" ]]; then
  PY_BIN="$WEBCAPTURE_DIR/.venv/bin/python"
else
  PY_BIN="python3"
fi

echo "Using Python: $PY_BIN"
"$PY_BIN" "$WEBCAPTURE_DIR/system_audio_tap_web_helper.py" --host 127.0.0.1 --port 8765
