#!/usr/bin/env python3
import os
import struct
import subprocess
import sys
import threading
import wave
from pathlib import Path
from typing import Callable, Optional

import numpy as np  # type: ignore

_HDR = struct.Struct("<IHHI")  # frames(u32), channels(u16), fmt(u16), nbytes(u32); little-endian

SYSTEM_AUDIO_BACKEND_SCK = "screencapturekit"
SYSTEM_AUDIO_BACKEND_COREAUDIO = "coreaudio_taps"
SYSTEM_AUDIO_BACKEND_LABELS = {
    SYSTEM_AUDIO_BACKEND_SCK: "ScreenCaptureKit",
    SYSTEM_AUDIO_BACKEND_COREAUDIO: "Core Audio taps",
}


def _bundle_root():
    """Return Path to *.app bundle root if running from a bundled app, else None."""
    exe = os.path.realpath(sys.executable)
    marker = ".app/Contents/MacOS/"
    if marker in exe:
        return Path(exe).parents[2]
    return None


def normalize_system_audio_backend(raw: Optional[str]) -> str:
    v = str(raw or "").strip().lower()
    if v in (SYSTEM_AUDIO_BACKEND_SCK, "screencapturekit", "screen_capturekit", "screen"):
        return SYSTEM_AUDIO_BACKEND_SCK
    if v in (SYSTEM_AUDIO_BACKEND_COREAUDIO, "coreaudio", "core audio", "core audio taps", "taps"):
        return SYSTEM_AUDIO_BACKEND_COREAUDIO
    return SYSTEM_AUDIO_BACKEND_SCK


def system_audio_permission_hint_message(backend: str) -> str:
    b = normalize_system_audio_backend(backend)
    if b == SYSTEM_AUDIO_BACKEND_COREAUDIO:
        return (
            "Für 'Core Audio taps' benötigt macOS die Berechtigung\n"
            "\"Nur Aufnahme von Systemaudio\".\n\n"
            "Bitte erlaube den Zugriff unter:\n"
            "Systemeinstellungen → Datenschutz & Sicherheit → Aufnahme von Bildschirm & Systemaudio.\n"
            "Aktiviere dort den Bereich 'Nur Aufnahme von Systemaudio'."
        )
    return (
        "Für 'ScreenCaptureKit' benötigt macOS die Berechtigung\n"
        "\"Bildschirm & Systemaudio\".\n\n"
        "Der Tscriba Recorder speichert KEIN Bildschirmvideo – nur Systemaudio.\n\n"
        "Bitte erlaube den Zugriff unter:\n"
        "Systemeinstellungen → Datenschutz & Sicherheit → Aufnahme von Bildschirm & Systemaudio."
    )


def system_audio_permission_denied_message(backend: str) -> str:
    b = normalize_system_audio_backend(backend)
    if b == SYSTEM_AUDIO_BACKEND_COREAUDIO:
        return (
            "macOS hat die Berechtigung für \"Nur Aufnahme von Systemaudio\" verweigert.\n\n"
            "Bitte aktiviere den Zugriff unter:\n"
            "Systemeinstellungen → Datenschutz & Sicherheit → Aufnahme von Bildschirm & Systemaudio\n"
            "im Abschnitt \"Nur Aufnahme von Systemaudio\".\n\n"
            "Danach Tscriba Recorder neu starten."
        )
    return (
        "macOS hat die Berechtigung für \"Bildschirm & Systemaudio\" verweigert.\n\n"
        "Der Tscriba Recorder speichert KEIN Bildschirmvideo – nur Systemaudio.\n\n"
        "Bitte aktiviere den Zugriff unter:\n"
        "Systemeinstellungen → Datenschutz & Sicherheit → Aufnahme von Bildschirm & Systemaudio.\n\n"
        "Danach Tscriba Recorder neu starten."
    )


def _system_audio_helper_path(backend: str = SYSTEM_AUDIO_BACKEND_SCK):
    """Locate embedded helper for selected backend; fall back to native/build for dev runs."""
    backend = normalize_system_audio_backend(backend)
    helper_name = "system_audio_capture" if backend == SYSTEM_AUDIO_BACKEND_SCK else "system_audio_tap"
    br = _bundle_root()
    if br is not None:
        p = br / "Contents" / "Helpers" / helper_name
        if p.exists():
            return p
    here = Path(__file__).resolve().parent
    return here / "native" / "build" / helper_name


class SystemAudioHelper:
    """Start selected Swift system-audio helper and write received audio to a WAV file."""

    def __init__(
        self,
        wav_path: Optional[str],
        sample_rate: int = 48000,
        status_cb=None,
        level_cb=None,
        no_write: bool = False,
        backend: str = SYSTEM_AUDIO_BACKEND_SCK,
    ):
        self.wav_path = wav_path
        self.sample_rate = int(sample_rate)
        self._status_cb = status_cb
        self._level_cb = level_cb
        self.no_write = bool(no_write)
        self.backend = normalize_system_audio_backend(backend)
        self._proc = None
        self._stop_evt = threading.Event()
        self._thread = None
        self._stderr_thread = None
        self._bytes_written = 0
        self._started_writing = False
        self._audio_tap = None

    @property
    def is_running(self) -> bool:
        return self._proc is not None

    def start(self):
        if self.is_running:
            return
        helper = _system_audio_helper_path(self.backend)
        if not helper.exists():
            raise FileNotFoundError(f"Systemaudio helper not found for backend '{self.backend}': {helper}")
        self._emit_status(f"Systemaudio: starting backend='{self.backend}' helper='{helper}'")

        if (not self.no_write) and self.wav_path:
            out_dir = os.path.dirname(self.wav_path)
            if out_dir:
                os.makedirs(out_dir, exist_ok=True)

        self._stop_evt.clear()
        self._proc = subprocess.Popen(
            [str(helper)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            bufsize=0,
        )
        try:
            self._emit_status(f"Systemaudio: helper pid={int(self._proc.pid)}")
        except Exception:
            pass

        self._thread = threading.Thread(target=self._reader_loop, daemon=True)
        self._thread.start()
        self._stderr_thread = threading.Thread(target=self._drain_stderr, daemon=True)
        self._stderr_thread.start()

    def stop(self):
        self._stop_evt.set()
        p = self._proc
        self._proc = None
        if p is None:
            return
        try:
            self._emit_status(f"Systemaudio: stopping helper pid={int(p.pid)}")
        except Exception:
            pass
        try:
            p.terminate()
        except Exception:
            pass
        try:
            if self._thread is not None:
                self._thread.join(timeout=1.5)
        except Exception:
            pass
        try:
            if self._stderr_thread is not None:
                self._stderr_thread.join(timeout=0.5)
        except Exception:
            pass
        try:
            rc = p.poll()
            self._emit_status(f"Systemaudio: helper stopped rc={rc}")
        except Exception:
            pass

    def set_audio_tap(self, cb: Optional[Callable]):
        self._audio_tap = cb

    def _emit_status(self, msg: str):
        if callable(self._status_cb):
            try:
                self._status_cb(msg)
            except Exception:
                pass

    def _drain_stderr(self):
        p = self._proc
        if p is None or p.stderr is None:
            return
        try:
            while not self._stop_evt.is_set():
                line = p.stderr.readline()
                if not line:
                    break
                s = line.decode(errors="replace").strip()
                if s:
                    self._emit_status(f"Systemaudio[{self.backend}]: {s}")
        except Exception:
            pass
        try:
            rc = p.poll()
            self._emit_status(f"Systemaudio[{self.backend}]: stderr closed rc={rc}")
        except Exception:
            pass

    def _reader_loop(self):
        p = self._proc
        if p is None or p.stdout is None:
            return

        if self.no_write:
            self._emit_status(f"Systemaudio[{self.backend}]: reader started (test mode)")
            try:
                while not self._stop_evt.is_set():
                    h = p.stdout.read(_HDR.size)
                    if not h or len(h) != _HDR.size:
                        self._emit_status(
                            f"Systemaudio[{self.backend}]: reader header EOF/short ({0 if not h else len(h)} bytes)"
                        )
                        break
                    nframes, nch, fmt, nbytes = _HDR.unpack(h)
                    payload = p.stdout.read(nbytes)
                    if len(payload) != nbytes:
                        self._emit_status(
                            f"Systemaudio[{self.backend}]: reader payload short ({len(payload)}/{nbytes})"
                        )
                        break
                    if fmt != 1:
                        self._emit_status(f"Systemaudio: unexpected format code {fmt}")
                        break
                    if self._audio_tap is not None:
                        try:
                            a = np.frombuffer(payload, dtype=np.float32)
                            if a.size == int(nframes) * int(nch):
                                a = a.reshape(int(nframes), int(nch))
                                self._audio_tap(a, int(self.sample_rate), int(nch))
                        except Exception:
                            pass
                    if self._level_cb is not None:
                        try:
                            floats = np.frombuffer(payload, dtype=np.float32)
                            if floats.size > 0:
                                rms = float(np.sqrt(np.mean(np.square(floats))))
                                db = 20.0 * np.log10(max(rms, 1e-12))
                                self._level_cb(db)
                        except Exception:
                            pass
            except Exception as e:
                self._emit_status(f"Systemaudio: reader error: {e}")
            try:
                rc = p.poll()
                self._emit_status(f"Systemaudio[{self.backend}]: reader exited (test mode) rc={rc}")
            except Exception:
                pass
            return

        self._emit_status(f"Systemaudio[{self.backend}]: reader started (record mode)")
        try:
            with open(self.wav_path, "wb") as raw_f:
                with wave.open(raw_f, "wb") as w:
                    w.setnchannels(2)
                    w.setsampwidth(2)
                    w.setframerate(self.sample_rate)
                    first = True

                    while not self._stop_evt.is_set():
                        h = p.stdout.read(_HDR.size)
                        if not h or len(h) != _HDR.size:
                            self._emit_status(
                                f"Systemaudio[{self.backend}]: reader header EOF/short ({0 if not h else len(h)} bytes)"
                            )
                            break
                        nframes, nch, fmt, nbytes = _HDR.unpack(h)
                        payload = p.stdout.read(nbytes)
                        if len(payload) != nbytes:
                            self._emit_status(
                                f"Systemaudio[{self.backend}]: reader payload short ({len(payload)}/{nbytes})"
                            )
                            break

                        if fmt != 1:
                            self._emit_status(f"Systemaudio: unexpected format code {fmt}")
                            break

                        if first:
                            w.setnchannels(int(nch))
                            first = False

                        if self._audio_tap is not None:
                            try:
                                a = np.frombuffer(payload, dtype=np.float32)
                                if a.size == int(nframes) * int(nch):
                                    a = a.reshape(int(nframes), int(nch))
                                    self._audio_tap(a, int(self.sample_rate), int(nch))
                            except Exception:
                                pass

                        floats = struct.unpack("<" + "f" * (nframes * nch), payload)
                        out = bytearray()
                        sum_sq = 0.0
                        cnt = 0
                        for x in floats:
                            if x != x:
                                x = 0.0
                            if x > 1.0:
                                x = 1.0
                            if x < -1.0:
                                x = -1.0
                            sum_sq += float(x) * float(x)
                            cnt += 1
                            out += struct.pack("<h", int(x * 32767.0))

                        if self._level_cb is not None and cnt > 0:
                            try:
                                import math
                                rms = math.sqrt(sum_sq / float(cnt))
                                db = 20.0 * math.log10(max(rms, 1e-12))
                                self._level_cb(db)
                            except Exception:
                                pass
                        w.writeframesraw(out)
                        self._bytes_written += len(out)
                        if not self._started_writing:
                            self._started_writing = True
                            self._emit_status("Systemaudio: receiving audio …")
                        try:
                            raw_f.flush()
                        except Exception:
                            pass
            if not self._started_writing:
                try:
                    rc = p.poll()
                    self._emit_status(f"Systemaudio[{self.backend}]: no audio frames received (helper rc={rc}).")
                except Exception:
                    pass
        except Exception as e:
            self._emit_status(f"Systemaudio: writer error: {e}")
        try:
            rc = p.poll()
            self._emit_status(f"Systemaudio[{self.backend}]: reader exited (record mode) rc={rc}")
        except Exception:
            pass
