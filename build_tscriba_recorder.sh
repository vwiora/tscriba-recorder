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
echo "Building system audio helper..."
mkdir -p native/build

swiftc -O -parse-as-library \
  -framework ScreenCaptureKit \
  -framework AVFoundation \
  -framework CoreMedia \
  -o native/build/system_audio_capture \
  native/system_audio_capture.swift

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

pyinstaller \
  --clean \
  --noconfirm \
  --windowed \
  --name "$APP_NAME" \
  --icon "assets/transcriba.icns" \
  --osx-bundle-identifier "$BUNDLE_ID" \
  --add-data "transcriba_theme.json:." \
  --add-data "assets:assets" \
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

# -----------------------------------------------------------------------------
# Info.plist permissions (required for mic prompt)
# -----------------------------------------------------------------------------
echo "Setting microphone permission string in Info.plist..."
PLIST="dist/${APP_NAME}.app/Contents/Info.plist"

/usr/libexec/PlistBuddy -c \
  "Add :NSMicrophoneUsageDescription string \"Audioaufnahme (Mikrofon) für ${APP_NAME}.\"" \
  "$PLIST" 2>/dev/null || \
/usr/libexec/PlistBuddy -c \
  "Set :NSMicrophoneUsageDescription \"Audioaufnahme (Mikrofon) für ${APP_NAME}.\"" \
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

codesign --force --deep --sign - --timestamp=none \
  "dist/${APP_NAME}.app" || true

echo "BUILD SUCCESS ✅"
echo "App: dist/${APP_NAME}.app"
