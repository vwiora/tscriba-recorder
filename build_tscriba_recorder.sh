#!/usr/bin/env bash
set -euo pipefail

# -----------------------------------------------------------------------------
# Tscriba Recorder Build Script (Python 3.12 pinned + native helper + permissions)
# -----------------------------------------------------------------------------

TRANSCRIBER_PY="/Users/volkerwiora/Projects/Transcriba Transcription Manager/.venv/bin/python"
if [ -x "$TRANSCRIBER_PY" ]; then
  DEFAULT_PY="$TRANSCRIBER_PY"
else
  DEFAULT_PY="python3.12"
fi

PYTHON_BIN="${PYTHON_BIN:-$DEFAULT_PY}"

if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  echo "ERROR: $PYTHON_BIN not found."
  echo "Install Python 3.12 (e.g. brew install python@3.12) or set PYTHON_BIN=/path/to/python3.12"
  exit 1
fi

echo "== Tscriba Recorder Build =="
echo "Using Python: $($PYTHON_BIN -V)"

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_DIR"

APP_NAME="Transcriba Recorder"
BUNDLE_ID="com.local.transcriba.recorder"
REQ_FILE="requirements.txt"
VENV_DIR=".venv"

# -----------------------------------------------------------------------------
# Clean
# -----------------------------------------------------------------------------
echo "Cleaning build artifacts..."
rm -rf build dist native/build "${APP_NAME}.spec" "$VENV_DIR"

# -----------------------------------------------------------------------------
# Python venv + deps
# -----------------------------------------------------------------------------
echo "Creating virtual environment..."
"$PYTHON_BIN" -m venv "$VENV_DIR"
# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"

python -m pip install --upgrade pip setuptools wheel

if [ ! -f "$REQ_FILE" ]; then
  echo "ERROR: $REQ_FILE not found!"
  exit 1
fi

echo "Installing Python dependencies..."
pip install -r "$REQ_FILE"

echo "Verifying faster-whisper install..."
python - <<'EOF'
import platform
print("Python:", platform.python_version(), platform.machine())
import faster_whisper, ctranslate2, tokenizers
print("faster_whisper:", getattr(faster_whisper, "__version__", "unknown"))
print("ctranslate2:", getattr(ctranslate2, "__version__", "unknown"))
print("tokenizers:", getattr(tokenizers, "__version__", "unknown"))
EOF

# -----------------------------------------------------------------------------
# Build native helper (Swift) for system audio
# -----------------------------------------------------------------------------
echo "Building system audio helpers..."
mkdir -p native/build
SWIFT_CACHE_DIR="${TMPDIR:-/tmp}/swift-module-cache"
CLANG_CACHE_DIR="${TMPDIR:-/tmp}/clang-module-cache"
mkdir -p "$SWIFT_CACHE_DIR" "$CLANG_CACHE_DIR"

SWIFT_MODULECACHE_PATH="$SWIFT_CACHE_DIR" CLANG_MODULE_CACHE_PATH="$CLANG_CACHE_DIR" swiftc -O -parse-as-library \
  -framework ScreenCaptureKit \
  -framework AVFoundation \
  -framework CoreMedia \
  -o native/build/system_audio_capture \
  native/system_audio_capture.swift

SWIFT_MODULECACHE_PATH="$SWIFT_CACHE_DIR" CLANG_MODULE_CACHE_PATH="$CLANG_CACHE_DIR" swiftc -O -parse-as-library \
  -framework CoreAudio \
  -framework AudioToolbox \
  -o native/build/system_audio_tap \
  native/system_audio_tap.swift

# -----------------------------------------------------------------------------
# PyInstaller build
# -----------------------------------------------------------------------------
echo "Building app bundle with PyInstaller..."
WEBRTC_PYINSTALLER_ARGS=()
if python -c "import webrtc_audio_processing" >/dev/null 2>&1; then
  echo "Including optional webrtc-audio-processing module..."
  WEBRTC_PYINSTALLER_ARGS+=(--collect-submodules webrtc_audio_processing --collect-binaries webrtc_audio_processing)
else
  echo "webrtc-audio-processing unavailable; building without AEC module support."
fi

EXTRA_PYINSTALLER_ARGS=()
if [[ -f "release_manifest.json" ]]; then
  echo "Including release_manifest.json..."
  EXTRA_PYINSTALLER_ARGS+=(--add-data "release_manifest.json:.")
else
  echo "release_manifest.json not found; build will rely on TRANSCRIBA_APP_VERSION override."
fi

pyinstaller \
  --clean \
  --noconfirm \
  --windowed \
  --name "$APP_NAME" \
  --icon "assets/transcriba.icns" \
  --osx-bundle-identifier "$BUNDLE_ID" \
  --add-data "transcriba_theme.json:." \
  --add-data "assets:assets" \
  ${EXTRA_PYINSTALLER_ARGS[@]+"${EXTRA_PYINSTALLER_ARGS[@]}"} \
  --collect-binaries ctranslate2 \
  --collect-binaries tokenizers \
  --collect-submodules ctranslate2 \
  --collect-submodules tokenizers \
  --collect-all faster_whisper \
  --collect-submodules pystray \
  --hidden-import pystray._darwin \
  --collect-all PIL \
  --collect-submodules PIL \
  --collect-submodules objc \
  --collect-submodules Foundation \
  --collect-submodules AppKit \
  ${WEBRTC_PYINSTALLER_ARGS[@]+"${WEBRTC_PYINSTALLER_ARGS[@]}"} \
  tscriba_recorder_app.py

# -----------------------------------------------------------------------------
# Embed helper into app bundle
# -----------------------------------------------------------------------------
echo "Embedding helper..."
HELPER_DIR="dist/${APP_NAME}.app/Contents/Helpers"
mkdir -p "$HELPER_DIR"
cp native/build/system_audio_capture "$HELPER_DIR/system_audio_capture"
chmod +x "$HELPER_DIR/system_audio_capture"
cp native/build/system_audio_tap "$HELPER_DIR/system_audio_tap"
chmod +x "$HELPER_DIR/system_audio_tap"

# -----------------------------------------------------------------------------
# Info.plist permissions (required for mic prompt)
# -----------------------------------------------------------------------------
echo "Setting permission strings in Info.plist..."
PLIST="dist/${APP_NAME}.app/Contents/Info.plist"

/usr/libexec/PlistBuddy -c \
  "Add :NSMicrophoneUsageDescription string \"Audioaufnahme (Mikrofon) für ${APP_NAME}.\"" \
  "$PLIST" 2>/dev/null || \
/usr/libexec/PlistBuddy -c \
  "Set :NSMicrophoneUsageDescription \"Audioaufnahme (Mikrofon) für ${APP_NAME}.\"" \
  "$PLIST"

/usr/libexec/PlistBuddy -c \
  "Add :NSAudioCaptureUsageDescription string \"Systemaudioaufnahme mit Core Audio taps für ${APP_NAME}.\"" \
  "$PLIST" 2>/dev/null || \
/usr/libexec/PlistBuddy -c \
  "Set :NSAudioCaptureUsageDescription \"Systemaudioaufnahme mit Core Audio taps für ${APP_NAME}.\"" \
  "$PLIST"

/usr/libexec/PlistBuddy -c \
  "Add :NSScreenCaptureUsageDescription string \"Systemaudioaufnahme über ScreenCaptureKit für ${APP_NAME}.\"" \
  "$PLIST" 2>/dev/null || \
/usr/libexec/PlistBuddy -c \
  "Set :NSScreenCaptureUsageDescription \"Systemaudioaufnahme über ScreenCaptureKit für ${APP_NAME}.\"" \
  "$PLIST"

# -----------------------------------------------------------------------------
# Remove quarantine attributes (helps local runs)
# -----------------------------------------------------------------------------
echo "Removing quarantine attributes..."
xattr -cr "dist/${APP_NAME}.app" || true

# -----------------------------------------------------------------------------
# Codesign (ad-hoc) so macOS permissions behave consistently
# -----------------------------------------------------------------------------
echo "Codesigning (ad-hoc)..."
codesign --force --sign - --timestamp=none \
  --identifier "${BUNDLE_ID}.system_audio_capture" \
  "dist/${APP_NAME}.app/Contents/Helpers/system_audio_capture" || true

codesign --force --sign - --timestamp=none \
  --identifier "${BUNDLE_ID}.system_audio_tap" \
  "dist/${APP_NAME}.app/Contents/Helpers/system_audio_tap" || true

codesign --force --deep --sign - --timestamp=none \
  "dist/${APP_NAME}.app" || true

echo "BUILD SUCCESS ✅"
echo "App: dist/${APP_NAME}.app"
