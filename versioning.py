import json
import os
import platform
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

APP_SLUG_AUDIO_RECORDER = "transcriba-audio-recorder"
DEFAULT_PORTAL_BASE_URL = "https://portal.transcriba.ai"
DEFAULT_TIMEOUT_SECONDS = 8.0


@dataclass(frozen=True)
class UpdateCheckResult:
    current_version: str
    latest_version: Optional[str]
    update_available: bool
    download_url: Optional[str]
    error: Optional[str] = None


def normalize_version(version: str) -> str:
    raw = str(version or "").strip()
    if not raw:
        return "0.0.0"
    if raw.lower().startswith("v"):
        raw = raw[1:]
    return raw


def _version_key(version: str) -> Tuple[List[int], str]:
    normalized = normalize_version(version).lower()
    main, sep, suffix = normalized.partition("-")
    nums: List[int] = []
    for part in re.split(r"[._+]", main):
        if not part:
            continue
        try:
            nums.append(int(part))
        except ValueError:
            nums.append(0)
    while len(nums) < 3:
        nums.append(0)
    return nums, suffix if sep else ""


def compare_versions(a: str, b: str) -> int:
    a_nums, a_suffix = _version_key(a)
    b_nums, b_suffix = _version_key(b)
    if a_nums < b_nums:
        return -1
    if a_nums > b_nums:
        return 1
    if not a_suffix and b_suffix:
        return 1
    if a_suffix and not b_suffix:
        return -1
    if a_suffix < b_suffix:
        return -1
    if a_suffix > b_suffix:
        return 1
    return 0


def detect_os_slug() -> str:
    name = platform.system().lower()
    if "darwin" in name:
        return "macos"
    if "windows" in name:
        return "windows"
    return "linux"


def _candidate_manifest_paths(bundle_dir: Optional[str] = None) -> List[Path]:
    paths: List[Path] = []
    if bundle_dir:
        paths.append(Path(bundle_dir) / "release_manifest.json")
    module_dir = Path(__file__).resolve().parent
    paths.append(module_dir / "release_manifest.json")
    paths.append(Path.cwd() / "release_manifest.json")
    return paths


def load_release_manifest(bundle_dir: Optional[str] = None) -> Dict[str, Any]:
    for path in _candidate_manifest_paths(bundle_dir=bundle_dir):
        if path.exists():
            with path.open("r", encoding="utf-8") as f:
                return json.load(f)
    return {}


def resolve_version_from_manifest(
    app_slug: str,
    os_slug: str,
    bundle_dir: Optional[str] = None,
    env_override_var: str = "TRANSCRIBA_APP_VERSION",
) -> str:
    env_override = normalize_version(os.environ.get(env_override_var, ""))
    if env_override != "0.0.0":
        return env_override

    manifest = load_release_manifest(bundle_dir=bundle_dir)
    apps = manifest.get("apps", {})
    app_entry = apps.get(app_slug, {}) if isinstance(apps, dict) else {}

    # Accept either "version" or per-OS shape:
    # {"versions": {"macos": "1.2.3", "default": "1.2.3"}}
    direct_version = normalize_version(str(app_entry.get("version", "")).strip())
    if direct_version != "0.0.0":
        return direct_version

    versions = app_entry.get("versions", {})
    if isinstance(versions, dict):
        os_version = normalize_version(str(versions.get(os_slug, "")).strip())
        if os_version != "0.0.0":
            return os_version
        default_version = normalize_version(str(versions.get("default", "")).strip())
        if default_version != "0.0.0":
            return default_version

    return "0.0.0"


def portal_base_url() -> str:
    return (os.environ.get("TRANSCRIBA_PORTAL_BASE_URL") or DEFAULT_PORTAL_BASE_URL).rstrip("/")


def fallback_download_url() -> str:
    return f"{portal_base_url()}/download"


def check_for_update(
    app_slug: str,
    os_slug: str,
    current_version: str,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    base_url: Optional[str] = None,
) -> UpdateCheckResult:
    base = (base_url or portal_base_url()).rstrip("/")
    params = urllib.parse.urlencode({"app_slug": app_slug, "os_slug": os_slug})
    url = f"{base}/api/releases/latest?{params}"
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:
            body = resp.read().decode("utf-8")
        payload = json.loads(body)
    except Exception as exc:
        return UpdateCheckResult(
            current_version=normalize_version(current_version),
            latest_version=None,
            update_available=False,
            download_url=fallback_download_url(),
            error=str(exc),
        )

    latest = normalize_version(str(payload.get("latest_version", "")).strip())
    download_url = str(payload.get("download_url", "")).strip() or fallback_download_url()
    current = normalize_version(current_version)
    has_update = latest != "0.0.0" and compare_versions(current, latest) < 0
    return UpdateCheckResult(
        current_version=current,
        latest_version=None if latest == "0.0.0" else latest,
        update_available=has_update,
        download_url=download_url,
        error=None,
    )
