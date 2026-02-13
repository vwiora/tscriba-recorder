# WebCapture Standalone MVP

Dedicated standalone folder: `/Users/volkerwiora/Projects/Transcriba Recorder/webcapture`

Contents:
- `system_audio_tap_web_helper.py`: localhost HTTP bridge for browser control
- `system_audio_tap_mvp.html`: ON/OFF + live dBFS level bar UI
- `open_webcapture.html`: direct-open browser launcher that discovers the local service
- `system_audio_tap.swift`: local Core Audio tap helper source (copied into this folder)
- `build_helper.sh`: builds a local tap binary into `webcapture/bin/system_audio_tap`
- `run_mvp.sh`: starts the helper API on localhost
- `build_app.sh`: builds a macOS app named `Core Audio Capture MVP.app` (no Terminal needed)
- `setup_python_env.sh`: creates `webcapture/.venv` and installs packaging deps
- `stop_service.sh`: stops the background MVP helper started by the app
- `requirements.txt`: Python build dependency for app packaging (`pyinstaller`)

## Quick Start

1. Build local tap binary (recommended):

```bash
cd "/Users/volkerwiora/Projects/Transcriba Recorder/webcapture"
./build_helper.sh
```

2. Start MVP helper:

```bash
cd "/Users/volkerwiora/Projects/Transcriba Recorder/webcapture"
./run_mvp.sh
```

3. Open the URL printed in Terminal (`Serving on ...`).
   Alternative: open `/Users/volkerwiora/Projects/Transcriba Recorder/webcapture/open_webcapture.html` directly in browser and click `Find Service`.

## Run Without Terminal

Build and run the app:

```bash
cd "/Users/volkerwiora/Projects/Transcriba Recorder/webcapture"
./setup_python_env.sh
./build_app.sh
open "/Users/volkerwiora/Projects/Transcriba Recorder/webcapture/dist/Core Audio Capture MVP.app"
```

What happens:
- The app starts a packaged service binary (`core_audio_capture_service`) in the background (no direct `python3` launch path).
- It opens `open_webcapture.html` so you can click `Find Service`.
- Logs/PID live in `~/Library/Logs/CoreAudioCaptureMVP/`.

To stop service later:

```bash
cd "/Users/volkerwiora/Projects/Transcriba Recorder/webcapture"
./stop_service.sh
```

Notes:
- If port `8765` is busy, the helper auto-selects the first free port in `8765..8815` (then random free port as last fallback).
- Browser JS talks to the local helper API; it does not call Core Audio directly.
- Core helper resolution order:
  1. `webcapture/bin/system_audio_tap`
  2. bundled app resource binary (`.../Resources/webcapture/bin/system_audio_tap`)

Packaging dependency:
- `build_app.sh` needs PyInstaller.
- If missing, create local venv inside this folder:
  - `./setup_python_env.sh`

## API Endpoints

- `POST /start`
- `POST /stop`
- `GET /state`
- `GET /events` (SSE)
- `GET /` (serves `system_audio_tap_mvp.html`)
