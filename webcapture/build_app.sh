#!/usr/bin/env bash
set -euo pipefail

WEBCAPTURE_DIR="$(cd "$(dirname "$0")" && pwd)"
DIST_DIR="$WEBCAPTURE_DIR/dist"
BUILD_DIR="$WEBCAPTURE_DIR/build"
APP_NAME="Core Audio Capture MVP"
APP_DIR="$DIST_DIR/$APP_NAME.app"
CONTENTS_DIR="$APP_DIR/Contents"
MACOS_DIR="$CONTENTS_DIR/MacOS"
RES_DIR="$CONTENTS_DIR/Resources"
LOG_DIR="$HOME/Library/Logs/CoreAudioCaptureMVP"

mkdir -p "$DIST_DIR"
rm -rf "$APP_DIR"
mkdir -p "$MACOS_DIR" "$RES_DIR"
mkdir -p "$BUILD_DIR"

if [[ -x "$WEBCAPTURE_DIR/.venv/bin/pyinstaller" ]]; then
  PYI_CMD=("$WEBCAPTURE_DIR/.venv/bin/pyinstaller")
elif command -v pyinstaller >/dev/null 2>&1; then
  PYI_CMD=("pyinstaller")
elif python3 -c "import PyInstaller" >/dev/null 2>&1; then
  PYI_CMD=("python3" "-m" "PyInstaller")
else
  echo "PyInstaller not found."
  echo "Install it globally or in webcapture/.venv, e.g.:"
  echo "  python3 -m venv \"$WEBCAPTURE_DIR/.venv\""
  echo "  \"$WEBCAPTURE_DIR/.venv/bin/pip\" install pyinstaller"
  exit 1
fi

# Bundle only what the app needs.
mkdir -p "$RES_DIR/webcapture"
cp "$WEBCAPTURE_DIR/open_webcapture.html" "$RES_DIR/webcapture/"
mkdir -p "$RES_DIR/webcapture/bin"

rm -rf "$BUILD_DIR/pyi-dist" "$BUILD_DIR/pyi-work" "$BUILD_DIR/pyi-spec"
"${PYI_CMD[@]}" \
  --clean \
  --noconfirm \
  --onefile \
  --name core_audio_capture_service \
  --distpath "$BUILD_DIR/pyi-dist" \
  --workpath "$BUILD_DIR/pyi-work" \
  --specpath "$BUILD_DIR/pyi-spec" \
  --add-data "$WEBCAPTURE_DIR/system_audio_tap_mvp.html:." \
  "$WEBCAPTURE_DIR/system_audio_tap_web_helper.py"
cp "$BUILD_DIR/pyi-dist/core_audio_capture_service" "$RES_DIR/webcapture/bin/"
chmod +x "$RES_DIR/webcapture/bin/core_audio_capture_service"

# Use dedicated webcapture binary.
if [[ -x "$WEBCAPTURE_DIR/bin/system_audio_tap" ]]; then
  cp "$WEBCAPTURE_DIR/bin/system_audio_tap" "$RES_DIR/webcapture/bin/system_audio_tap"
else
  echo "Tap binary not found. Build it first: ./build_helper.sh"
  exit 1
fi
chmod +x "$RES_DIR/webcapture/bin/system_audio_tap"

cat > "$CONTENTS_DIR/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleDevelopmentRegion</key>
  <string>en</string>
  <key>CFBundleDisplayName</key>
  <string>Core Audio Capture MVP</string>
  <key>CFBundleExecutable</key>
  <string>CoreAudioCaptureMVP</string>
  <key>CFBundleIdentifier</key>
  <string>com.local.coreaudiocapturemvp</string>
  <key>CFBundleInfoDictionaryVersion</key>
  <string>6.0</string>
  <key>CFBundleName</key>
  <string>Core Audio Capture MVP</string>
  <key>CFBundlePackageType</key>
  <string>APPL</string>
  <key>CFBundleShortVersionString</key>
  <string>1.0</string>
  <key>CFBundleVersion</key>
  <string>1</string>
  <key>NSAudioCaptureUsageDescription</key>
  <string>System audio capture for Core Audio Capture MVP.</string>
  <key>LSBackgroundOnly</key>
  <true/>
  <key>LSMinimumSystemVersion</key>
  <string>14.4</string>
</dict>
</plist>
PLIST

cat > "$MACOS_DIR/CoreAudioCaptureMVP" <<'LAUNCHER'
#!/usr/bin/env bash
set -euo pipefail

APP_CONTENTS_DIR="$(cd "$(dirname "$0")/.." && pwd)"
RES_DIR="$APP_CONTENTS_DIR/Resources"
SERVICE_BIN="$RES_DIR/webcapture/bin/core_audio_capture_service"
LAUNCHER_PAGE="$RES_DIR/webcapture/open_webcapture.html"
LOG_DIR="$HOME/Library/Logs/CoreAudioCaptureMVP"
PID_FILE="$LOG_DIR/helper.pid"
LOG_FILE="$LOG_DIR/service.log"

mkdir -p "$LOG_DIR"

is_running() {
  if [[ ! -f "$PID_FILE" ]]; then
    return 1
  fi
  local pid
  pid="$(cat "$PID_FILE" 2>/dev/null || true)"
  if [[ -z "$pid" ]]; then
    return 1
  fi
  kill -0 "$pid" >/dev/null 2>&1
}

if ! is_running; then
  /usr/bin/nohup "$SERVICE_BIN" --host 127.0.0.1 --port 8765 >>"$LOG_FILE" 2>&1 &
  echo "$!" > "$PID_FILE"
fi

# Always open launcher page so user can connect without reading logs.
/usr/bin/open "$LAUNCHER_PAGE"
LAUNCHER

chmod +x "$MACOS_DIR/CoreAudioCaptureMVP"
mkdir -p "$LOG_DIR"

# Ad-hoc sign so macOS privacy attribution is tied to this app bundle as much as possible.
codesign --force --sign - --timestamp=none "$RES_DIR/webcapture/bin/core_audio_capture_service" || true
codesign --force --sign - --timestamp=none "$RES_DIR/webcapture/bin/system_audio_tap" || true
codesign --force --deep --sign - --timestamp=none "$APP_DIR" || true

echo "Built app: $APP_DIR"
echo "Launch by double-clicking: $APP_DIR"
