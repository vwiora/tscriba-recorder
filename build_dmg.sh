#!/usr/bin/env bash
set -euo pipefail

# -----------------------------------------------------------------------------
# Build a shareable DMG for Transcriba Recorder
# -----------------------------------------------------------------------------

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_DIR"

APP_NAME="${APP_NAME:-Transcriba Recorder}"
APP_PATH="${APP_PATH:-dist/${APP_NAME}.app}"
OUT_DIR="${OUT_DIR:-dist}"
VERSION="${VERSION:-$(date +%Y%m%d-%H%M)}"
DMG_NAME="${DMG_NAME:-${APP_NAME// /-}-${VERSION}}"
DMG_PATH="${OUT_DIR}/${DMG_NAME}.dmg"
VOL_NAME="${VOL_NAME:-${APP_NAME} ${VERSION}}"

AUTO_BUILD=1
if [[ "${1:-}" == "--no-build" ]]; then
  AUTO_BUILD=0
fi

if [[ "$AUTO_BUILD" -eq 1 && ! -d "$APP_PATH" ]]; then
  echo "App bundle not found at: $APP_PATH"
  echo "Running build_tscriba_recorder.sh first..."
  ./build_tscriba_recorder.sh
fi

if [[ ! -d "$APP_PATH" ]]; then
  echo "ERROR: App bundle not found at: $APP_PATH"
  echo "Build the app first or set APP_PATH."
  exit 1
fi

mkdir -p "$OUT_DIR"
rm -f "$DMG_PATH"

STAGE_DIR="$(mktemp -d "${TMPDIR:-/tmp}/transcriba-dmg-stage.XXXXXX")"
cleanup() {
  rm -rf "$STAGE_DIR"
}
trap cleanup EXIT

cp -R "$APP_PATH" "$STAGE_DIR/"
ln -s /Applications "$STAGE_DIR/Applications"

echo "Creating DMG..."
hdiutil create \
  -volname "$VOL_NAME" \
  -srcfolder "$STAGE_DIR" \
  -ov \
  -format UDZO \
  "$DMG_PATH"

echo "DMG created: $DMG_PATH"
