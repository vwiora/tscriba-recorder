#!/usr/bin/env bash
set -euo pipefail

WEBCAPTURE_DIR="$(cd "$(dirname "$0")" && pwd)"
OUT_DIR="$WEBCAPTURE_DIR/bin"
SRC="$WEBCAPTURE_DIR/system_audio_tap.swift"
OUT="$OUT_DIR/system_audio_tap"
SWIFT_CACHE_DIR="${TMPDIR:-/tmp}/swift-module-cache"
CLANG_CACHE_DIR="${TMPDIR:-/tmp}/clang-module-cache"

mkdir -p "$OUT_DIR"
mkdir -p "$SWIFT_CACHE_DIR" "$CLANG_CACHE_DIR"

SWIFT_MODULECACHE_PATH="$SWIFT_CACHE_DIR" CLANG_MODULE_CACHE_PATH="$CLANG_CACHE_DIR" xcrun swiftc -O -parse-as-library \
  -framework CoreAudio \
  -framework AudioToolbox \
  -o "$OUT" \
  "$SRC"

chmod +x "$OUT"
echo "Built: $OUT"
