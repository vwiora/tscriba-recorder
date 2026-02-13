#!/usr/bin/env bash
set -euo pipefail

WEBCAPTURE_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$WEBCAPTURE_DIR"

python3 "$WEBCAPTURE_DIR/system_audio_tap_web_helper.py" --host 127.0.0.1 --port 8765
