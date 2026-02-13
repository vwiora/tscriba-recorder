#!/usr/bin/env bash
set -euo pipefail

PID_FILE="$HOME/Library/Logs/CoreAudioCaptureMVP/helper.pid"

if [[ ! -f "$PID_FILE" ]]; then
  echo "Core Audio Capture MVP: no PID file found."
  exit 0
fi

PID="$(cat "$PID_FILE" 2>/dev/null || true)"
if [[ -z "$PID" ]]; then
  echo "Core Audio Capture MVP: empty PID file."
  rm -f "$PID_FILE"
  exit 0
fi

if kill -0 "$PID" >/dev/null 2>&1; then
  kill "$PID" >/dev/null 2>&1 || true
  sleep 0.3
  if kill -0 "$PID" >/dev/null 2>&1; then
    kill -9 "$PID" >/dev/null 2>&1 || true
  fi
fi

rm -f "$PID_FILE"
echo "Core Audio Capture MVP: service stopped."
