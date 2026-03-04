# Transcriba Audio Recorder

Desktop app for microphone/system audio recording, optional live transcription, and packaged macOS builds.

## Version Management

### Source of truth
- Versions are managed in [`release_manifest.json`](/Users/volkerwiora/Projects/Transcriba%20Recorder/release_manifest.json) with `app_slug` entries for:
- `transcriba-transcription-manager`
- `transcriba-audio-recorder`
- App runtime slug is `transcriba-audio-recorder`.

### Runtime resolution
- Version/update logic is implemented in [`versioning.py`](/Users/volkerwiora/Projects/Transcriba%20Recorder/versioning.py).
- On startup, the app resolves:
- `os_slug` from platform (`macos`, `windows`, `linux`)
- current app version from `release_manifest.json` for `transcriba-audio-recorder`
- UI shows `Version: X.Y.Z` in Settings -> General.

### Update check flow
- API endpoint:
- `GET /api/releases/latest?app_slug=<slug>&os_slug=<macos|windows|linux>`
- Default portal base URL:
- `https://portal.transcriba.ai` (override via env var below)
- UI actions:
- `Nach Updates suchen`: manual check with dialogs
- silent background check runs shortly after startup (no popup)
- `Download-Seite öffnen`: opens API `download_url` or fallback `<portal>/download`

### Environment overrides
- `TRANSCRIBA_APP_VERSION`: overrides version shown/used for comparisons.
- `TRANSCRIBA_PORTAL_BASE_URL`: overrides portal/API base URL.

### Basic release workflow
1. Update `release_manifest.json` for both app slugs.
2. Build via `./build_tscriba_recorder.sh`.
3. Packaging includes `release_manifest.json` in the app bundle when present.
4. In-app update checks compare installed version against portal `latest_version`.
