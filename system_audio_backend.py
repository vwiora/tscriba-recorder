#!/usr/bin/env python3
import os
import platform
import queue
import struct
import subprocess
import sys
import threading
import wave
from pathlib import Path
from typing import Callable, Optional

import numpy as np  # type: ignore
import sounddevice as sd  # type: ignore

_HDR = struct.Struct("<IHHI")  # frames(u32), channels(u16), fmt(u16), nbytes(u32); little-endian

SYSTEM_AUDIO_BACKEND_SCK = "screencapturekit"
SYSTEM_AUDIO_BACKEND_COREAUDIO = "coreaudio_taps"
SYSTEM_AUDIO_BACKEND_WASAPI = "wasapi_loopback"
SYSTEM_AUDIO_BACKEND_MONITOR = "monitor_device"
SYSTEM_AUDIO_BACKEND_LABELS = {
    SYSTEM_AUDIO_BACKEND_SCK: "ScreenCaptureKit",
    SYSTEM_AUDIO_BACKEND_COREAUDIO: "Core Audio taps",
    SYSTEM_AUDIO_BACKEND_WASAPI: "WASAPI Loopback",
    SYSTEM_AUDIO_BACKEND_MONITOR: "Monitor Device (PulseAudio/PipeWire)",
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
    if "wasapi" in v:
        return SYSTEM_AUDIO_BACKEND_WASAPI
    if "monitor" in v:
        return SYSTEM_AUDIO_BACKEND_MONITOR
    if v in (SYSTEM_AUDIO_BACKEND_WASAPI, "wasapi", "loopback", "wasapi loopback"):
        return SYSTEM_AUDIO_BACKEND_WASAPI
    if v in (SYSTEM_AUDIO_BACKEND_MONITOR, "monitor", "monitor device", "pulse", "pipewire"):
        return SYSTEM_AUDIO_BACKEND_MONITOR
    return default_system_audio_backend()


def _hostapi_name(hostapi_idx: Optional[int]) -> str:
    try:
        if hostapi_idx is None:
            return ""
        info = sd.query_hostapis(int(hostapi_idx))
        return str(info.get("name", "") or "")
    except Exception:
        return ""


def _find_default_wasapi_output_device() -> Optional[int]:
    try:
        default_out = sd.default.device[1]  # (input, output)
    except Exception:
        default_out = None
    if isinstance(default_out, int) and default_out >= 0:
        try:
            d = sd.query_devices(default_out)
            host = _hostapi_name(d.get("hostapi"))
            if "wasapi" in host.lower() and int(d.get("max_output_channels", 0) or 0) > 0:
                return int(default_out)
        except Exception:
            pass

    try:
        for idx, d in enumerate(sd.query_devices()):
            host = _hostapi_name(d.get("hostapi"))
            if "wasapi" not in host.lower():
                continue
            if int(d.get("max_output_channels", 0) or 0) > 0:
                return int(idx)
    except Exception:
        pass
    return None


def _find_monitor_input_device() -> Optional[int]:
    try:
        devices = sd.query_devices()
    except Exception:
        return None
    ranked = []
    for idx, d in enumerate(devices):
        max_in = int(d.get("max_input_channels", 0) or 0)
        if max_in <= 0:
            continue
        name = str(d.get("name", "") or "").lower()
        score = 0
        if "monitor" in name:
            score += 3
        if "loopback" in name:
            score += 2
        if "pulse" in name or "pipewire" in name:
            score += 1
        if score > 0:
            ranked.append((score, idx))
    if not ranked:
        return None
    ranked.sort(reverse=True)
    return int(ranked[0][1])


def default_system_audio_backend() -> str:
    s = platform.system()
    if s == "Darwin":
        return SYSTEM_AUDIO_BACKEND_SCK
    if s == "Windows":
        return SYSTEM_AUDIO_BACKEND_WASAPI
    return SYSTEM_AUDIO_BACKEND_MONITOR


def system_audio_backend_support() -> dict[str, tuple[bool, str]]:
    """Return support map: backend -> (is_supported, reason_if_unsupported)."""
    s = platform.system()
    support = {
        SYSTEM_AUDIO_BACKEND_SCK: (False, "ScreenCaptureKit ist nur auf macOS verfügbar."),
        SYSTEM_AUDIO_BACKEND_COREAUDIO: (False, "Core Audio taps ist nur auf macOS verfügbar."),
        SYSTEM_AUDIO_BACKEND_WASAPI: (False, "WASAPI Loopback ist nur auf Windows verfügbar."),
        SYSTEM_AUDIO_BACKEND_MONITOR: (False, "Monitor-Device Capture ist primär für Linux vorgesehen."),
    }

    if s == "Darwin":
        sck = _system_audio_helper_path(SYSTEM_AUDIO_BACKEND_SCK)
        tap = _system_audio_helper_path(SYSTEM_AUDIO_BACKEND_COREAUDIO)
        support[SYSTEM_AUDIO_BACKEND_SCK] = (sck.exists(), f"Helper fehlt: {sck}")
        support[SYSTEM_AUDIO_BACKEND_COREAUDIO] = (tap.exists(), f"Helper fehlt: {tap}")
        return support

    if s == "Windows":
        if not hasattr(sd, "WasapiSettings"):
            support[SYSTEM_AUDIO_BACKEND_WASAPI] = (False, "PortAudio/WASAPI nicht verfügbar in dieser sounddevice-Build.")
            return support
        dev = _find_default_wasapi_output_device()
        if dev is None:
            support[SYSTEM_AUDIO_BACKEND_WASAPI] = (False, "Kein WASAPI-Ausgabegerät für Loopback gefunden.")
        else:
            support[SYSTEM_AUDIO_BACKEND_WASAPI] = (True, "")
        return support

    # Linux / other Unix: look for monitor-style input.
    mon = _find_monitor_input_device()
    if mon is None:
        support[SYSTEM_AUDIO_BACKEND_MONITOR] = (
            False,
            "Kein Monitor-Eingang gefunden. Unter PipeWire/PulseAudio bitte ein *.monitor-Gerät aktivieren.",
        )
    else:
        support[SYSTEM_AUDIO_BACKEND_MONITOR] = (True, "")
    return support


def list_available_system_audio_backends() -> list[str]:
    support = system_audio_backend_support()
    preferred = default_system_audio_backend()
    ordered = [
        preferred,
        SYSTEM_AUDIO_BACKEND_SCK,
        SYSTEM_AUDIO_BACKEND_COREAUDIO,
        SYSTEM_AUDIO_BACKEND_WASAPI,
        SYSTEM_AUDIO_BACKEND_MONITOR,
    ]
    seen = set()
    out = []
    for b in ordered:
        if b in seen:
            continue
        seen.add(b)
        ok, _reason = support.get(b, (False, ""))
        if ok:
            out.append(b)
    return out


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


def _resolve_stream_capture_params(backend: str, requested_sr: int) -> tuple[int, int, int, object, str]:
    """Resolve device/samplerate/channels/settings for stream-based backends."""
    b = normalize_system_audio_backend(backend)
    if b == SYSTEM_AUDIO_BACKEND_WASAPI:
        dev = _find_default_wasapi_output_device()
        if dev is None:
            raise RuntimeError("Kein WASAPI-Ausgabegerät für Loopback gefunden.")
        info = sd.query_devices(dev)
        ch = int(info.get("max_output_channels", 0) or 0)
        ch = 2 if ch >= 2 else 1
        dev_sr = int(float(info.get("default_samplerate", 48000) or 48000))
        sr = int(requested_sr or dev_sr or 48000)
        extra = sd.WasapiSettings(loopback=True)
        label = f"WASAPI device={dev}"
        return int(dev), int(sr), int(ch), extra, label

    if b == SYSTEM_AUDIO_BACKEND_MONITOR:
        dev = _find_monitor_input_device()
        if dev is None:
            raise RuntimeError(
                "Kein Monitor-Eingang gefunden (z.B. '*.monitor'). Bitte PipeWire/PulseAudio Monitor aktivieren."
            )
        info = sd.query_devices(dev, "input")
        ch = int(info.get("max_input_channels", 0) or 0)
        ch = 2 if ch >= 2 else 1
        dev_sr = int(float(info.get("default_samplerate", 48000) or 48000))
        sr = int(requested_sr or dev_sr or 48000)
        label = f"monitor device={dev}"
        return int(dev), int(sr), int(ch), None, label

    raise RuntimeError(f"Backend '{backend}' unterstützt keinen streambasierten Capture-Pfad.")


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
        self._stream = None
        self._stream_channels = 2
        self._q_stream: "queue.Queue[np.ndarray]" = queue.Queue(maxsize=400)

    @property
    def is_running(self) -> bool:
        return (self._proc is not None) or (self._stream is not None)

    def start(self):
        if self.is_running:
            return
        if self.backend in (SYSTEM_AUDIO_BACKEND_WASAPI, SYSTEM_AUDIO_BACKEND_MONITOR):
            self._start_stream_backend()
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
        if self._stream is not None:
            self._stop_stream_backend()
            return
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

    def _start_stream_backend(self):
        if (not self.no_write) and self.wav_path:
            out_dir = os.path.dirname(self.wav_path)
            if out_dir:
                os.makedirs(out_dir, exist_ok=True)
        self._stop_evt.clear()
        self._bytes_written = 0
        self._started_writing = False

        dev, stream_sr, channels, extra_settings, dev_label = _resolve_stream_capture_params(self.backend, self.sample_rate)
        self.sample_rate = int(stream_sr)

        def _cb(indata, _frames, _time, status):
            if status:
                self._emit_status(f"Systemaudio[{self.backend}]: {status}")
            if self._stop_evt.is_set():
                return
            try:
                self._q_stream.put_nowait(indata.copy())
            except queue.Full:
                self._emit_status("Systemaudio: queue full, dropping frames.")

        self._stream = sd.InputStream(
            device=int(dev),
            samplerate=int(stream_sr),
            channels=int(channels),
            dtype="float32",
            callback=_cb,
            extra_settings=extra_settings,
            blocksize=0,
        )
        self._stream_channels = int(channels)
        self._stream.start()
        self._emit_status(
            f"Systemaudio: starting backend='{self.backend}' source='{dev_label}' sr={stream_sr} ch={channels}"
        )
        self._thread = threading.Thread(target=self._stream_writer_loop, daemon=True)
        self._thread.start()

    def _stop_stream_backend(self):
        self._stop_evt.set()
        s = self._stream
        self._stream = None
        if s is not None:
            try:
                s.stop()
            except Exception:
                pass
            try:
                s.close()
            except Exception:
                pass
        if self._thread is not None:
            try:
                self._thread.join(timeout=1.5)
            except Exception:
                pass
            self._thread = None
        # Drain stale queue frames so a future start begins cleanly.
        while not self._q_stream.empty():
            try:
                self._q_stream.get_nowait()
            except Exception:
                break

    def _stream_writer_loop(self):
        mode_txt = "test mode" if self.no_write else "record mode"
        self._emit_status(f"Systemaudio[{self.backend}]: reader started ({mode_txt})")

        wf = None
        raw_f = None
        try:
            if not self.no_write:
                raw_f = open(self.wav_path, "wb")
                wf = wave.open(raw_f, "wb")
                wf.setnchannels(int(self._stream_channels or 2))
                wf.setsampwidth(2)
                wf.setframerate(int(self.sample_rate))

            while (not self._stop_evt.is_set()) or (not self._q_stream.empty()):
                try:
                    chunk = self._q_stream.get(timeout=0.2)
                except queue.Empty:
                    continue

                if chunk is None or getattr(chunk, "size", 0) == 0:
                    continue

                if self._audio_tap is not None:
                    try:
                        self._audio_tap(chunk, int(self.sample_rate), int(chunk.shape[1] if chunk.ndim > 1 else 1))
                    except Exception:
                        pass

                try:
                    floats = np.asarray(chunk, dtype=np.float32)
                    rms = float(np.sqrt(np.mean(np.square(floats)))) if floats.size > 0 else 0.0
                    db = 20.0 * np.log10(max(rms, 1e-12))
                    if self._level_cb is not None:
                        self._level_cb(db)
                except Exception:
                    pass

                if wf is None:
                    continue
                clipped = np.clip(chunk, -1.0, 1.0)
                pcm16 = (clipped * 32767.0).astype(np.int16)
                wf.writeframesraw(pcm16.tobytes())
                self._bytes_written += int(pcm16.size * 2)
                if not self._started_writing:
                    self._started_writing = True
                    self._emit_status("Systemaudio: receiving audio …")
                try:
                    raw_f.flush()
                except Exception:
                    pass
        except Exception as e:
            self._emit_status(f"Systemaudio: writer error: {e}")
        finally:
            try:
                if wf is not None:
                    wf.close()
            except Exception:
                pass
            try:
                if raw_f is not None:
                    raw_f.close()
            except Exception:
                pass
            if (not self.no_write) and (not self._started_writing):
                self._emit_status(f"Systemaudio[{self.backend}]: no audio frames received.")
            self._emit_status(f"Systemaudio[{self.backend}]: reader exited ({mode_txt})")

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
