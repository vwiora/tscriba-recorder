#!/usr/bin/env python3
import os
import subprocess
import sys
import platform
from datetime import datetime
import struct
import wave
import threading
import queue
import time
from typing import Optional, Callable
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox
import customtkinter as ctk
# Optional tray/menu bar controls (Windows tray / macOS menu bar)
try:
    import pystray  # type: ignore
    from PIL import Image, ImageDraw  # type: ignore
    TRAY_AVAILABLE = True
except Exception:
    pystray = None  # type: ignore
    Image = None  # type: ignore
    ImageDraw = None  # type: ignore
    TRAY_AVAILABLE = False

# For IPC pipe non-blocking (macOS/Linux). On Windows, we just read best-effort.
try:
    import fcntl  # type: ignore
except Exception:
    fcntl = None  # type: ignore

from audio_recorder import (
    TscribaRecorder,
    TscribaRecorderConfig,
    list_input_devices,
    get_device_max_input_channels,
)

def default_recordings_dir():
    return os.path.join(os.path.expanduser("~"), "Documents", "Tscriba Recorder Recordings")


def default_out_path():
    base = default_recordings_dir()
    os.makedirs(base, exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    return os.path.join(base, f"rec_{ts}.wav")


def _unique_session_dir(base_dir: str, session_name: str) -> str:
    """Return a unique session directory path inside base_dir.

    If base_dir/session_name already exists, a numeric suffix is appended.
    """
    base_dir = base_dir or default_recordings_dir()
    os.makedirs(base_dir, exist_ok=True)

    # sanitize a bit
    session_name = (session_name or "session").strip() or "session"
    session_name = session_name.replace(os.sep, "_")

    candidate = os.path.join(base_dir, session_name)
    if not os.path.exists(candidate):
        return candidate

    i = 1
    while True:
        cand = f"{candidate}_{i}"
        if not os.path.exists(cand):
            return cand
        i += 1


# ------------------------------------------------------------------
# ScreenCaptureKit System Audio Helper (Swift CLI embedded in .app)
# ------------------------------------------------------------------

_HDR = struct.Struct("<IHHI")  # frames(u32), channels(u16), fmt(u16), nbytes(u32); little-endian

def _bundle_root():
    """Return Path to *.app bundle root if running from a bundled app, else None."""
    exe = os.path.realpath(sys.executable)
    marker = ".app/Contents/MacOS/"
    if marker in exe:
        # .../Tscriba Recorder.app/Contents/MacOS/Tscriba Recorder -> .../Tscriba Recorder.app
        return Path(exe).parents[2]
    return None

def _system_audio_helper_path():
    """Locate embedded helper first; fall back to native/build for dev runs."""
    br = _bundle_root()
    if br is not None:
        p = br / "Contents" / "Helpers" / "system_audio_capture"
        if p.exists():
            return p
    here = Path(__file__).resolve().parent
    return here / "native" / "build" / "system_audio_capture"

class SystemAudioHelper:
    """Start the Swift ScreenCaptureKit helper and write received audio to a WAV file.

    Reads framed float32 interleaved PCM from helper stdout (see Swift code),
    converts to int16 WAV for compatibility.
    """

    def __init__(self, wav_path: str, sample_rate: int = 48000, status_cb=None, level_cb=None):
        self.wav_path = wav_path
        self.sample_rate = int(sample_rate)
        self._status_cb = status_cb
        self._level_cb = level_cb
        self._proc = None
        self._stop_evt = threading.Event()
        self._thread = None
        self._stderr_thread = None
        self._bytes_written = 0
        self._started_writing = False
        self._audio_tap = None  # optional callback for live transcription (receives float32 frames)

    @property
    def is_running(self) -> bool:
        return self._proc is not None

    def start(self):
        if self.is_running:
            return
        helper = _system_audio_helper_path()
        if not helper.exists():
            raise FileNotFoundError(f"Systemaudio helper not found: {helper}")

        # Ensure output directory exists
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
            p.terminate()
        except Exception:
            pass

        # Best-effort: wait a moment so WAV header can be finalized by writer thread.
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

    def set_audio_tap(self, cb: Optional[Callable]):
        """Set a callback that receives raw float32 PCM frames.

        cb(audio: np.ndarray, samplerate: int, channels: int) -> None
        Audio is interleaved float32 from the helper, shaped (nframes, nch).
        """
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
                    # Surface errors and important state messages.
                    if ("Failed" in s) or ("error" in s.lower()) or ("started" in s.lower()):
                        self._emit_status(f"Systemaudio: {s}")
        except Exception:
            pass

    def _reader_loop(self):
        p = self._proc
        if p is None or p.stdout is None:
            return

        try:
            # Open file handle explicitly so the file exists immediately.
            with open(self.wav_path, "wb") as raw_f:
                with wave.open(raw_f, "wb") as w:
                    w.setsampwidth(2)  # int16
                    w.setframerate(self.sample_rate)
                    first = True

                    while not self._stop_evt.is_set():
                        h = p.stdout.read(_HDR.size)
                        if not h or len(h) != _HDR.size:
                            break
                        nframes, nch, fmt, nbytes = _HDR.unpack(h)
                        payload = p.stdout.read(nbytes)
                        if len(payload) != nbytes:
                            break

                        if fmt != 1:
                            self._emit_status(f"Systemaudio: unexpected format code {fmt}")
                            break

                        if first:
                            w.setnchannels(int(nch))
                            first = False

                        # Optional: forward raw float32 frames to a live-transcription tap.
                        if self._audio_tap is not None:
                            try:
                                import numpy as np  # type: ignore
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

                        # Compute a rough level meter for system audio (dBFS) and surface it via callback.
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

                        # Flush periodically so file grows on disk during recording.
                        try:
                            raw_f.flush()
                        except Exception:
                            pass
        except Exception as e:
            self._emit_status(f"Systemaudio: writer error: {e}")

def _create_tray_image():
    # Simple red dot icon; replace later with a real PNG if you want.
    if not TRAY_AVAILABLE:
        return None
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((12, 12, 52, 52), fill=(220, 0, 0, 255))
    return img


class TrayController:
    """IMPORTANT (macOS): Tray callbacks must NOT call Tk directly.
    They only write single-letter commands into an OS pipe.
    The Tk main thread polls the pipe and executes actions safely.
    """
    def __init__(self, ipc_write_fd: int):
        self._wfd = ipc_write_fd
        self.icon = None

        if not TRAY_AVAILABLE:
            return

        menu = pystray.Menu(
            pystray.MenuItem("Show", self._cmd("W")),
            pystray.MenuItem("Record", self._cmd("R")),
            pystray.MenuItem("Pause/Resume", self._cmd("P")),
            pystray.MenuItem("Stop", self._cmd("S")),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", self._cmd("Q")),
        )
        self.icon = pystray.Icon("Tscriba Recorder", _create_tray_image(), "Tscriba Recorder", menu)

    def _cmd(self, ch: str):
        def _handler(icon=None, item=None):
            try:
                os.write(self._wfd, ch.encode("utf-8"))
            except Exception:
                pass
        return _handler

    def start(self):
        if not self.icon:
            return

        # macOS: run in a detached way (Cocoa requirement)
        try:
            self.icon.run_detached()
            return
        except Exception:
            pass

        # Other OS: run in a daemon thread
        import threading
        import queue
        import time
        from typing import Optional, Callable
        t = threading.Thread(target=self.icon.run, daemon=True)
        t.start()

    def stop(self):
        try:
            if self.icon is not None:
                self.icon.stop()
        except Exception:
            pass

class FasterWhisperMicTranscriber:
    """
    Mic-only live transcription using faster-whisper.
    Starts its own sounddevice InputStream (read-only tap) to avoid touching the recorder logic.
    UI updates must be performed by the caller (typically via Tk.after).
    """

    def __init__(
        self,
        device: Optional[int],
        samplerate: int,
        channels: int,
        language: Optional[str] = None,
        model_size: str = "small",
        chunk_seconds: float = 2.5,
        overlap_seconds: float = 0.7,
        beam_size: int = 3,
        vad_filter: bool = True,
        status_cb: Optional[Callable[[str], None]] = None,
        text_cb: Optional[Callable[[str], None]] = None,
        language_detected_cb: Optional[Callable[[str, Optional[float]], None]] = None,
        external_audio: bool = False,
    ):
        self.external_audio = bool(external_audio)
        self.device = device
        self.samplerate = int(samplerate)
        self.channels = int(channels) if int(channels) > 0 else 1
        self.language = (language or None)
        self.model_size = str(model_size or "small").strip().lower()
        if self.model_size not in ("small", "medium", "large"):
            self.model_size = "small"
        self.status_cb = status_cb
        self.text_cb = text_cb
        self.language_detected_cb = language_detected_cb
        self._detected_language = None
        self._detected_language_prob = None
        self.icon = None  # optional UI/tray icon handle; may be unused

        self._stop = threading.Event()
        self._q: "queue.Queue[bytes]" = queue.Queue(maxsize=50)
        self._worker: Optional[threading.Thread] = None
        self._stream = None

        self._model = None
        self._last_emitted_t = 0.0  # seconds (absolute in our running timeline)
        self._timeline_t = 0.0      # seconds processed in-order

        # tuning
        self.chunk_seconds = float(chunk_seconds)
        self.overlap_seconds = float(overlap_seconds)
        self.beam_size = int(beam_size)
        self.vad_filter = bool(vad_filter)
        
        # live buffering (for very small incoming chunks like 512 frames)
        self._buf = None  # ring buffer (numpy float32 mono) at 16kHz
        self._buf_sr = 16000

        # --- realtime streaming policy state (text stabilization / de-dup) ---
        self._committed_text = ""
        self._pending_text = ""
        self._pending_count = 0

    @staticmethod
    def _dedup_append(committed: str, decoded: str, max_overlap_words: int = 50) -> str:
        """Return only the newly-added tail of `decoded` compared to `committed`.

        We do word-level suffix/prefix matching to avoid repeated text caused by overlap windows.
        """
        committed = (committed or "").strip()
        decoded = (decoded or "").strip()
        if not decoded:
            return ""
        if not committed:
            return decoded

        a = committed.split()
        b = decoded.split()
        if not b:
            return ""

        # Compare case-insensitively for overlap detection, but return original casing.
        a_l = [w.lower() for w in a]
        b_l = [w.lower() for w in b]
        max_k = min(len(a), len(b), int(max_overlap_words))
        for k in range(max_k, 0, -1):
            if a_l[-k:] == b_l[:k]:
                return " ".join(b[k:]).strip()
        return decoded

    class _RingBufferF32:
        """Simple single-producer/single-consumer ring buffer for float32 audio."""

        def __init__(self, capacity: int):
            import numpy as _np  # type: ignore
            self.cap = int(max(1, capacity))
            self.buf = _np.zeros((self.cap,), dtype=_np.float32)
            self.r = 0
            self.w = 0
            self.count = 0

        def available(self) -> int:
            return int(self.count)

        def write(self, x):
            import numpy as _np  # type: ignore
            x = _np.asarray(x, dtype=_np.float32).reshape(-1)
            n = int(x.size)
            if n <= 0:
                return

            # If chunk larger than capacity, keep only the newest tail.
            if n >= self.cap:
                x = x[-self.cap :]
                n = int(x.size)
                self.r = 0
                self.w = 0
                self.count = 0

            # Backpressure policy: if not enough space, drop oldest audio to stay realtime.
            overflow = (self.count + n) - self.cap
            if overflow > 0:
                self.r = (self.r + overflow) % self.cap
                self.count -= overflow

            end = self.w + n
            if end <= self.cap:
                self.buf[self.w : end] = x
            else:
                first = self.cap - self.w
                self.buf[self.w :] = x[:first]
                self.buf[: end % self.cap] = x[first:]
            self.w = end % self.cap
            self.count += n

        def read(self, n: int):
            import numpy as _np  # type: ignore
            n = int(n)
            if n <= 0 or n > self.count:
                return _np.zeros((0,), dtype=_np.float32)
            end = self.r + n
            if end <= self.cap:
                return self.buf[self.r : end].copy()
            first = self.cap - self.r
            return _np.concatenate([self.buf[self.r :].copy(), self.buf[: end % self.cap].copy()])

        def advance(self, n: int):
            n = int(n)
            if n <= 0:
                return
            n = min(n, self.count)
            self.r = (self.r + n) % self.cap
            self.count -= n

    def start(self):
        try:
            import numpy as np  # type: ignore
            import sounddevice as sd  # type: ignore
            from faster_whisper import WhisperModel  # type: ignore
        except Exception as e:
            raise RuntimeError(
                "faster-whisper / numpy / sounddevice nicht verfügbar. "
                "Bitte Dependencies installieren und erneut starten.\n\n"
                f"Import-Fehler: {e}"
            )

        # Use device default samplerate if possible (reduces silent streams / resampling issues)
        if not getattr(self, 'external_audio', False):
            try:
                import sounddevice as sd  # type: ignore
                if self.device is None:
                    dev_info = sd.query_devices(None, 'input')
                else:
                    dev_info = sd.query_devices(self.device, 'input')
                dsr = int(float(dev_info.get('default_samplerate', self.samplerate)))
                if dsr and dsr != int(self.samplerate):
                    self.samplerate = dsr
                    if self.status_cb:
                        self.status_cb(f"Live-Transkription: verwende Device-Samplerate {dsr} Hz")
            except Exception:
                pass

        # audio stream -> queue (float32)
        blocksize = int(self.samplerate * 0.05)  # 50 ms

        if getattr(self, "external_audio", False):
            self._stop.clear()
            self._worker = threading.Thread(target=self._run, name="fw_transcriber", daemon=True)
            self._worker.start()
            if self.status_cb:
                self.status_cb("Live-Transkription: externes Mic-Audio aktiv.")
            return

        self._rms_last_t = 0.0
        self._rms_silence_warned = False

        def _cb(indata, frames, _time, status):
            if status and self.status_cb:
                self.status_cb(f"Transcription Mic: {status}")
            if self._stop.is_set():
                return
            try:
                import numpy as np  # type: ignore
                tnow = time.time()
                if tnow - getattr(self, '_rms_last_t', 0.0) > 1.0:
                    self._rms_last_t = tnow
                    rms = float(np.sqrt(np.mean(indata.astype('float32') ** 2))) if frames else 0.0
                    if rms < 0.002 and not getattr(self, '_rms_silence_warned', False):
                        self._rms_silence_warned = True
                        if self.status_cb:
                            self.status_cb('Live-Transkription: Mic scheint stumm/zu leise…')
                    if rms >= 0.004:
                        self._rms_silence_warned = False
            except Exception:
                pass
            try:
                # indata is float32; store as bytes to keep queue light
                self._q.put_nowait(indata.copy().tobytes())
            except queue.Full:
                # drop if UI/ASR can't keep up
                if self.status_cb:
                    self.status_cb("Live-Transkription: zu langsam (Audio-Drops).")

        self._stream = sd.InputStream(
            samplerate=self.samplerate,
            channels=self.channels,
            dtype="float32",
            device=self.device,
            callback=_cb,
            blocksize=blocksize,
        )
        self._stream.start()

        self._worker = threading.Thread(target=self._run, name="fw_transcriber", daemon=True)
        self._worker.start()

        if self.status_cb:
            self.status_cb("Live-Transkription (Mic) läuft…")

    def stop(self):
        self._stop.set()
        try:
            if self._stream is not None:
                self._stream.stop()
                self._stream.close()
        except Exception:
            pass
        self._stream = None
        try:
            if self._worker is not None:
                self._worker.join(timeout=1.5)
        except Exception:
            pass
        self._worker = None
        if self.status_cb:
            self.status_cb("Live-Transkription (Mic) gestoppt.")

    def push_audio(self, chunk, samplerate=None):
        """Receive external mic audio from AudioRecorder (chunk, samplerate)."""
        try:
            import numpy as np
        except Exception:
            return
        if self._stop.is_set():
            return
        try:
            a = np.asarray(chunk)
            if a.size == 0:
                return
            
            
            # VWIORA: alle 500 Chunks einmal RMS ausgeben (zeigt ob wirklich Audio ankommt)
            try:
                if not hasattr(self, "_push_n"):
                    self._push_n = 0
                self._push_n += 1
                if self._push_n % 500 == 0 and self.status_cb:
                    x = a
                    if x.ndim == 2 and x.shape[1] > 0:
                        x = x[:, 0]
                    rms = float(np.sqrt(np.mean(x.astype("float32") ** 2)))
                    # self.status_cb(f"Audio rein: n={self._push_n} rms={rms:.4f}")
            except Exception:
                pass
                    
            if a.dtype != np.float32:
                if a.dtype == np.int16:
                    a = a.astype(np.float32) / 32768.0
                elif a.dtype == np.int32:
                    a = a.astype(np.float32) / 2147483648.0
                else:
                    a = a.astype(np.float32)
            b = a.reshape(-1).tobytes()
            try:
                self._q.put_nowait(b)
            except Exception:
                pass
                
        except Exception:
            pass

    def _run(self):
        import numpy as np  # type: ignore

        # VWIORA
        if self.status_cb:
            self.status_cb("Transcriber-Thread gestartet.")

        try:
            from faster_whisper import WhisperModel  # type: ignore
        except Exception as e:
            if self.status_cb:
                self.status_cb(f"Model-Import fehlgeschlagen: {e}")
            return

        if self._model is None:
            if self.status_cb:
                self.status_cb(f"Lade faster-whisper Model ({self.model_size})…")
            try:
                self._model = WhisperModel(self.model_size, device="cpu", compute_type="int8")
            except Exception as e:
                if self.status_cb:
                    self.status_cb(f"Model-Laden fehlgeschlagen: {e}")
                return

        sr_in = int(self.samplerate)  # input device rate (e.g. 44100 / 48000)
        sr = 16000                  
        # sr = float(self.samplerate)
        chunk_n = int(self.chunk_seconds * sr)
        overlap_n = int(self.overlap_seconds * sr)

        # Ring buffer avoids repeated np.concatenate() copies in tight loops.
        # Capacity is generous to handle short stalls, but we drop oldest on overflow to keep realtime.
        rb_capacity = int(max(10.0, self.chunk_seconds * 8.0) * sr)  # seconds -> samples @16k
        rb = self._RingBufferF32(rb_capacity)
        
        # VWIORA optional: resampler (scipy if available, else linear)
        try:
            from scipy.signal import resample_poly  # type: ignore
            def _resample_to_16k(x: np.ndarray) -> np.ndarray:
                if sr_in == sr:
                    return x.astype(np.float32, copy=False)
                g = np.gcd(sr_in, sr)
                up = sr // g
                down = sr_in // g
                return resample_poly(x, up, down).astype(np.float32, copy=False)
        except Exception:
            def _resample_to_16k(x: np.ndarray) -> np.ndarray:
                if sr_in == sr:
                    return x.astype(np.float32, copy=False)
                # linear fallback
                n_in = x.shape[0]
                n_out = int(n_in * (sr / float(sr_in)))
                if n_out <= 1:
                    return np.zeros((0,), dtype=np.float32)
                idx = np.linspace(0.0, n_in - 1, num=n_out)
                return np.interp(idx, np.arange(n_in), x).astype(np.float32, copy=False)

        while not self._stop.is_set():
            try:
                b = self._q.get(timeout=0.2)
            except queue.Empty:
                continue

            # bytes -> float32 array
            a = np.frombuffer(b, dtype=np.float32)
            if self.channels > 1:
                a = a.reshape(-1, self.channels)
                mono = a.mean(axis=1)
            else:
                mono = a

            # keep mono float32
            # buf = np.concatenate([buf, mono.astype(np.float32, copy=False).reshape(-1)], axis=0)
            
            mono16 = _resample_to_16k(mono.astype(np.float32, copy=False).reshape(-1))
            
            # append to 16k ring buffer
            if mono16.size:
                rb.write(mono16)

            # process in chunks
            while rb.available() >= chunk_n:
                # include overlap for better word-boundaries
                step_n = max(1, chunk_n - overlap_n)
                chunk = rb.read(chunk_n)
                rb.advance(step_n)

                # normalize: whisper expects float32 -1..1; ours already is
                audio = chunk
                
                #if self.status_cb:
                    # VWIORA self.status_cb(f"[DEBUG] transcribe: samples={audio.shape[0]} sr=16000")

                try:
                    # vad_filter improves realtime stability
                    segments, info = self._model.transcribe(
                        audio,
                        language=self.language,
                        task="transcribe",
                        vad_filter=self.vad_filter,
                        beam_size=self.beam_size,
                        temperature=0.0,
                        condition_on_previous_text=False,
                    )
                except Exception as e:
                    if self.status_cb:
                        self.status_cb(f"Live-Transkription Fehler: {e}")
                    continue

                # If language is set to Auto (None), surface detected language once it becomes available
                if self.language is None:
                    try:
                        det_lang = getattr(info, "language", None)
                        det_prob = getattr(info, "language_probability", None)
                        if det_lang and det_lang != getattr(self, "_detected_language", None):
                            self._detected_language = det_lang
                            self._detected_language_prob = det_prob
                            if self.language_detected_cb:
                                self.language_detected_cb(det_lang, det_prob)
                    except Exception:
                        pass

                base_t = self._timeline_t
                # chunk duration in seconds (note: we advance read pointer by step_n)
                self._timeline_t += step_n / sr

                # --- streaming policy: join segments, de-dup against committed, then commit only stable additions ---
                out_lines = []
                for seg in segments:
                    txt = (getattr(seg, 'text', '') or '').strip()
                    if txt:
                        out_lines.append(txt)

                decoded = " ".join(out_lines).strip()
                if not decoded:
                    continue

                # Deduplicate overlap repeats at word level.
                candidate_add = self._dedup_append(self._committed_text, decoded)
                if not candidate_add:
                    continue

                # Stabilize: only emit when the same addition appears twice (or looks like sentence end).
                if candidate_add == self._pending_text:
                    self._pending_count += 1
                else:
                    self._pending_text = candidate_add
                    self._pending_count = 1

                ends_sentence = candidate_add.endswith((".", "!", "?"))
                stable = ends_sentence or (self._pending_count >= 2)
                if not stable:
                    continue

                # Commit and emit only the newly-stable addition.
                if self._committed_text:
                    self._committed_text = (self._committed_text + " " + candidate_add).strip()
                else:
                    self._committed_text = candidate_add.strip()
                self._pending_text = ""
                self._pending_count = 0

                if self.text_cb:
                    self.text_cb(candidate_add + "\n")


class TscribaRecorderApp(ctk.CTk):
    def __init__(self):
        super().__init__()

        # ---- Window basics ----
        ctk.set_appearance_mode("System")
        self.title("Tscriba Recorder")
        self.geometry("860x420")

        # IMPORTANT: set scaling on THIS root (no second Tk window!)
        try:
            self.tk.call("tk", "scaling", 1.0)
        except Exception:
            pass

        self._level_db = -120.0
        self._sys_level_db = -120.0

        # --- Live transcription UI (Mic only: faster-whisper, Schritt 1) ---
        self.live_transcription_var = tk.BooleanVar(value=False)
        self.transcription_language_var = tk.StringVar(value="Auto")
        # Live transcription tuning (UI)
        self.transcription_chunk_seconds_var = tk.DoubleVar(value=2.5)
        self.transcription_overlap_seconds_var = tk.DoubleVar(value=0.7)
        self.transcription_beam_size_var = tk.IntVar(value=3)
        self.transcription_vad_filter_var = tk.BooleanVar(value=True)
        self.transcription_model_size_var = tk.StringVar(value="small")
        self._transcript_win = None
        self._transcript_close_btn = None
        self._transcript_text = None
        self._transcript_text = None
        self._mic_transcriber = None
        self._sys_transcriber = None

        # IPC pipe for tray -> Tk thread (avoid calling Tk from tray callbacks, esp. macOS)
        self._ipc_rfd = None
        self._ipc_wfd = None
        try:
            rfd, wfd = os.pipe()
            if fcntl is not None:
                flags = fcntl.fcntl(rfd, fcntl.F_GETFL)
                fcntl.fcntl(rfd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
            self._ipc_rfd, self._ipc_wfd = rfd, wfd
        except Exception:
            self._ipc_rfd = self._ipc_wfd = None

        self.rec = TscribaRecorder(on_level=self.on_level, on_status=self.on_status)

        # ScreenCaptureKit system audio helper (separate WAV for now)
        self.sys_helper = None
        self._sys_only_running = False
        # Show the macOS permission explanation only once per app run.
        self._systemaudio_permission_hint_shown = False
        self._systemaudio_permission_denied_shown = False
        # Remember last started recording paths so system-only output is discoverable
        self._last_out_path = None
        self._last_mic_enabled = False
        self._last_sys_enabled = False
        # device cache
        self.input_devices = []
        self._build_device_cache()

        # ---- UI ----
        frm = ctk.CTkFrame(self)
        frm.pack(fill="both", expand=True, padx=12, pady=12)
        # Aufnahme-Quelle(n)
        r0 = ctk.CTkFrame(frm)
        r0.pack(fill="x", pady=(0, 8))
        ctk.CTkLabel(r0, text="Aufnahme:").pack(side="left")

        self.rec_mic_var = tk.BooleanVar(value=True)
        self.rec_sys_var = tk.BooleanVar(value=False)

        self.rec_mic_chk = ctk.CTkCheckBox(
            r0, text="Mikrofon aufnehmen", variable=self.rec_mic_var, command=self.on_mode_change
        )
        self.rec_mic_chk.pack(side="left", padx=(8, 0))

        self.rec_sys_chk = ctk.CTkCheckBox(
            r0, text="Systemaudio aufnehmen", variable=self.rec_sys_var, command=self.on_mode_change
        )
        self.rec_sys_chk.pack(side="left", padx=(12, 0))

        # Mic device
        r1 = ctk.CTkFrame(frm)
        r1.pack(fill="x", pady=(0, 8))
        ctk.CTkLabel(r1, text="Mikrofon").pack(anchor="w", padx=8, pady=(6, 0))
        self.mic_var = tk.StringVar(value="Default Input")
        self.mic_cb = ctk.CTkComboBox(
            r1,
            variable=self.mic_var,
            values=[],
            command=lambda _val: self.on_device_change(),
        )
        self.mic_cb.pack(side="left", padx=8, pady=(6, 8), fill="x", expand=True)
        # Systemaudio (UI only – capture via ScreenCaptureKit will be implemented later)
        r2 = ctk.CTkFrame(frm)
        r2.pack(fill="x", pady=(0, 8))
        ctk.CTkLabel(r2, text="Systemaudio").pack(anchor="w", padx=8, pady=(6, 0))
        self.sys_var = tk.StringVar(value="ScreenCaptureKit (keine Auswahl)")
        self.sys_label = ctk.CTkLabel(r2, textvariable=self.sys_var)
        self.sys_label.pack(side="left", padx=8, pady=(6, 8), fill="x", expand=True)

        # Settings
        r3 = ctk.CTkFrame(frm)
        r3.pack(fill="x", pady=(0, 8))

        ctk.CTkLabel(r3, text="Sample rate:").pack(side="left")
        self.sr_var = tk.IntVar(value=48000)
        tk.Spinbox(r3, from_=8000, to=192000, textvariable=self.sr_var, width=10).pack(
            side="left", padx=(6, 14)
        )

        ctk.CTkLabel(r3, text="Channels:").pack(side="left")
        self.ch_var = tk.IntVar(value=1)
        self.ch_spin = tk.Spinbox(r3, from_=1, to=2, textvariable=self.ch_var, width=5)
        self.ch_spin.pack(side="left", padx=(6, 0))

        ctk.CTkButton(r3, text="Refresh Devices", command=self.refresh_devices).pack(side="right")


        # Gain (dB)
        r3b = ctk.CTkFrame(frm)
        r3b.pack(fill="x", pady=(0, 8))

        ctk.CTkLabel(r3b, text="Mic gain (dB):").pack(side="left")
        self.mic_gain_var = tk.DoubleVar(value=0.0)
        self.mic_gain_spin = tk.Spinbox(
            r3b, from_=-24, to=24, increment=1, textvariable=self.mic_gain_var, width=6
        )
        self.mic_gain_spin.pack(side="left", padx=(6, 14))

        ctk.CTkLabel(r3b, text="System gain (dB):").pack(side="left")
        self.sys_gain_var = tk.DoubleVar(value=9.0)
        self.sys_gain_spin = tk.Spinbox(
            r3b, from_=-24, to=24, increment=1, textvariable=self.sys_gain_var, width=6
        )
        self.sys_gain_spin.pack(side="left", padx=(6, 0))

        # Output
        r4 = ctk.CTkFrame(frm)
        r4.pack(fill="x", pady=(0, 8))
        ctk.CTkLabel(r4, text="Output file:").pack(side="left")
        self.out_var = tk.StringVar(value=default_out_path())
        ctk.CTkEntry(r4, textvariable=self.out_var).pack(side="left", padx=8, fill="x", expand=True)
        ctk.CTkButton(r4, text="Browse…", command=self.browse_out).pack(side="left")
        ctk.CTkButton(r4, text="Ordner öffnen", command=self.open_recordings_folder).pack(
            side="left", padx=(8, 0)
        )

        # Controls
        r5 = ctk.CTkFrame(frm)
        r5.pack(fill="x", pady=(0, 8))
        self.btn_record = ctk.CTkButton(r5, text="Record", command=self.start_recording)
        self.btn_record.pack(side="left")
        self.btn_pause = ctk.CTkButton(r5, text="Pause", command=self.toggle_pause, state="disabled")
        self.btn_pause.pack(side="left", padx=8)
        self.btn_stop = ctk.CTkButton(r5, text="Stop", command=self.stop_recording, state="disabled")
        self.btn_stop.pack(side="left")

        # Live transcription (UI only for now)
        r5b = ctk.CTkFrame(frm)
        r5b.pack(fill="x", pady=(0, 8))
        ctk.CTkCheckBox(
            r5b,
            text="Live-Transkription",
            variable=self.live_transcription_var,
            command=self.on_live_transcription_toggle,
        ).pack(side="left")

        # Language selector for live transcription (Auto / Deutsch / English)
        ctk.CTkLabel(r5b, text="Sprache:").pack(side="left", padx=(10, 6))
        ctk.CTkOptionMenu(
            r5b,
            variable=self.transcription_language_var,
            values=["Auto", "Deutsch", "English"],
        ).pack(side="left")

        ctk.CTkButton(
            r5b,
            text="Transcript…",
            command=self.open_transcript_window,
        ).pack(side="left", padx=(10, 0))

        # Live transcription tuning row (below checkbox)
        r5c = ctk.CTkFrame(frm)
        r5c.pack(fill="x", pady=(0, 8))

        ctk.CTkLabel(r5c, text="Chunk (s):").pack(side="left")
        tk.Spinbox(
            r5c, from_=0.5, to=15.0, increment=0.1,
            textvariable=self.transcription_chunk_seconds_var, width=6
        ).pack(side="left", padx=(6, 12))

        ctk.CTkLabel(r5c, text="Overlap (s):").pack(side="left")
        tk.Spinbox(
            r5c, from_=0.0, to=10.0, increment=0.1,
            textvariable=self.transcription_overlap_seconds_var, width=6
        ).pack(side="left", padx=(6, 12))

        ctk.CTkLabel(r5c, text="Beam:").pack(side="left")
        tk.Spinbox(
            r5c, from_=1, to=10, increment=1,
            textvariable=self.transcription_beam_size_var, width=4
        ).pack(side="left", padx=(6, 12))

        ctk.CTkCheckBox(
            r5c,
            text="VAD",
            variable=self.transcription_vad_filter_var,
        ).pack(side="left")
        ctk.CTkLabel(r5c, text="Model:").pack(side="left", padx=(10, 6))
        ctk.CTkOptionMenu(
            r5c,
            variable=self.transcription_model_size_var,
            values=["small", "medium", "large"],
        ).pack(side="left")
        # Level (separat: Mic / System)
        r6 = ctk.CTkFrame(frm)
        r6.pack(fill="x", pady=(8, 0))

        r6a = ctk.CTkFrame(r6)
        r6a.pack(fill="x")
        ctk.CTkLabel(r6a, text="Mic:").pack(side="left")
        self.level_mic = ctk.CTkProgressBar(r6a, width=520)
        self.level_mic.set(0)
        self.level_mic.pack(side="left", padx=8, fill="x", expand=True)

        r6b = ctk.CTkFrame(r6)
        r6b.pack(fill="x", pady=(4, 0))
        ctk.CTkLabel(r6b, text="System:").pack(side="left")
        self.level_sys = ctk.CTkProgressBar(r6b, width=520)
        self.level_sys.set(0)
        self.level_sys.pack(side="left", padx=8, fill="x", expand=True)

        # Status / hint
        self.status_var = tk.StringVar(value="Ready.")
        ctk.CTkLabel(frm, textvariable=self.status_var, wraplength=820).pack(fill="x", pady=(10, 0))

        # Shortcut to open transcript window
        # macOS Command key
        self.hint_var = tk.StringVar(value="")
        ctk.CTkLabel(frm, textvariable=self.hint_var, wraplength=820).pack(fill="x", pady=(6, 0))

        # Init
        self.refresh_devices()
        self.on_mode_change()

        # Unlock transcript window close when recording stops
        if self._transcript_win is not None:
            self._update_transcript_close_state()

        # Tray/menu bar controls (delayed & safe start for macOS)
        self.tray = None
        if TRAY_AVAILABLE and self._ipc_wfd is not None:
            # pystray on macOS can fail if started too early; delay startup
            self.after(600, self._start_tray_safe)
        else:
            if not TRAY_AVAILABLE:
                self.on_status("Tray nicht verfügbar (pystray/PIL Import fehlgeschlagen).")


        # Poll tray IPC
        self.after(100, self._process_ipc)

        self.after(50, self._tick_ui)
        self.protocol("WM_DELETE_WINDOW", self.on_close)

    # ------------------------------------------------------------------
    # Start Tray safe
    # ------------------------------------------------------------------

    def _start_tray_safe(self):
        try:
            self.tray = TrayController(self._ipc_wfd)
            self.tray.start()
            self.on_status("Tray gestartet.")
        except Exception as e:
            self.tray = None
            msg = f"Tray-Start fehlgeschlagen: {type(e).__name__}: {e}"
            self.on_status(msg)
            try:
                print(msg)
            except Exception:
                pass
    
    # ------------------------------------------------------------------
    # Device handling
    # ------------------------------------------------------------------

    def _build_device_cache(self):
        self.input_devices = []

        try:
            default_max = get_device_max_input_channels(None)
        except Exception:
            default_max = 0

        self.input_devices.append(
            {
                "label": "Default Input",
                "id": None,
                "max_in": default_max
            }
        )

        for idx, name, max_in in list_input_devices():
            self.input_devices.append(
                {
                    "label": f"{name} (id={idx}, in={max_in})",
                    "id": idx,
                    "max_in": max_in
                }
            )

    def refresh_devices(self):
        self._build_device_cache()

        mic_list = sorted(
            self.input_devices,
            key=lambda d: (0 if d["label"] == "Default Input" else 1, d["label"].lower()),
        )
        values = [d["label"] for d in mic_list]
        self.mic_cb.configure(values=values)
        if self.mic_var.get() not in values and values:
            self.mic_var.set(values[0])
        # Systemaudio: no input device selection in UI (ScreenCaptureKit comes later)
        self.sys_var.set("ScreenCaptureKit (keine Auswahl)")

        self.on_device_change()

    def _find_entry(self, label: str):
        for d in self.input_devices:
            if d["label"] == label:
                return d
        return self.input_devices[0]

    def selected_mic_id(self):
        return self._find_entry(self.mic_var.get())["id"]

    def selected_sys_id(self):
        # Systemaudio will be captured via ScreenCaptureKit later (no input device selection)
        return None


    # ------------------------------------------------------------------
    # UI logic
    # ------------------------------------------------------------------

    def _has_systemaudio_permission(self) -> bool:
        """Best-effort check for macOS "Bildschirm- & Systemaudioaufnahme" permission.

        We use CoreGraphics' CGPreflightScreenCaptureAccess(), which returns whether
        Screen Recording permission is granted without showing any prompt.
        """

        if platform.system() != "Darwin":
            return True

        try:
            import ctypes
            import ctypes.util

            lib_path = ctypes.util.find_library("CoreGraphics")
            if not lib_path:
                return False

            cg = ctypes.cdll.LoadLibrary(lib_path)
            cg.CGPreflightScreenCaptureAccess.restype = ctypes.c_bool
            return bool(cg.CGPreflightScreenCaptureAccess())
        except Exception:
            return False

    def _current_mode(self):
        mic = bool(self.rec_mic_var.get())
        sys = bool(self.rec_sys_var.get())
        if mic and sys:
            return "both"
        if sys and not mic:
            return "system"
        if mic and not sys:
            return "mic"
        # If nothing selected, fall back to mic (and UI will disable Record)
        return "mic"

    def on_mode_change(self):
        mode = self._current_mode()
        self.hint_var.set("")

        mic_enabled = bool(self.rec_mic_var.get())
        sys_enabled = bool(self.rec_sys_var.get())

        # Enable/disable mic controls
        self.mic_cb.configure(state="normal" if mic_enabled else "disabled")
        self.mic_gain_spin.configure(state="normal" if mic_enabled else "disabled")

        # Systemaudio: UI only (no device selection)
        # We just enable/disable the gain control to reflect the choice.
        self.sys_gain_spin.configure(state="normal" if sys_enabled else "disabled")

        # Channels behavior: if both sources selected, force 2ch mixdown mode in UI (like before)
        if mode == "both":
            self.ch_var.set(2)
            self.ch_spin.configure(state="disabled")

        # Lock transcript window close while recording
        if self._transcript_win is not None:
            self._update_transcript_close_state()
        else:
            self.ch_spin.configure(state="normal")

        # Prevent recording if nothing is selected
        if (not mic_enabled) and (not sys_enabled):
            self.btn_record.configure(state="disabled")
            self.hint_var.set("Bitte mindestens eine Quelle auswählen (Mikrofon und/oder Systemaudio).")
        else:
            if not self.rec.is_running:
                self.btn_record.configure(state="normal")

        # One-time hint: macOS requires the "Screen & System Audio Recording" permission for system audio
        # via ScreenCaptureKit (even though we do NOT record any video).
        if (
            sys_enabled
            and (not self._systemaudio_permission_hint_shown)
            and platform.system() == "Darwin"
            and (not self._has_systemaudio_permission())
        ):
            self._systemaudio_permission_hint_shown = True
            try:
                messagebox.showinfo(
                    "Systemaudio (macOS Berechtigung)",
                    "macOS koppelt die Aufnahme von Systemaudio an die Berechtigung\n"
                    "\"Bildschirm- & Systemaudioaufnahme\".\n\n"
                    "Der Tscriba Recorder speichert KEIN Bildschirmvideo – nur Systemaudio.\n\n"
                    "Bitte erlaube den Zugriff unter:\n"
                    "Systemeinstellungen → Datenschutz & Sicherheit → Bildschirm- & Systemaudioaufnahme",
                )
            except Exception:
                pass

        self.on_device_change()

    def on_device_change(self):
        # Channel limits only depend on the selected mic device in the current implementation.
        # (Systemaudio capture will be switched to ScreenCaptureKit later.)
        mode = self._current_mode()
        mic_enabled = bool(self.rec_mic_var.get())

        if mode == "both":
            return

        if mic_enabled:
            entry = self._find_entry(self.mic_var.get())
            max_in = int(entry.get("max_in", 1) or 1)
            max_allowed = max(1, min(2, max_in))
            self.ch_spin.configure(to=max_allowed)

            if self.ch_var.get() > max_allowed:
                self.ch_var.set(max_allowed)
            if self.ch_var.get() < 1:
                self.ch_var.set(1)

    # ------------------------------------------------------------------
    # Folder / file helpers

    # ------------------------------------------------------------------

    def open_recordings_folder(self):
        # Prefer folder of currently selected output file; fallback to default recordings directory
        folder = os.path.dirname((self.out_var.get() or "").strip())
        if not folder:
            folder = default_recordings_dir()
        os.makedirs(folder, exist_ok=True)

        try:
            system = platform.system()
            if system == "Darwin":
                subprocess.run(["open", folder], check=False)
            elif system == "Windows":
                subprocess.run(["explorer", folder], check=False)
            else:
                subprocess.run(["xdg-open", folder], check=False)
        except Exception as e:
            messagebox.showerror("Fehler", f"Ordner konnte nicht geöffnet werden:\n\n{e}")

    def browse_out(self):
        path = filedialog.asksaveasfilename(
            title="Choose output WAV file",
            defaultextension=".wav",
            filetypes=[("WAV audio", "*.wav")],
            initialfile=os.path.basename(self.out_var.get()),
            initialdir=os.path.dirname(self.out_var.get()) or default_recordings_dir(),
        )
        if path:
            if not path.lower().endswith(".wav"):
                path += ".wav"
            self.out_var.set(path)

    # ------------------------------------------------------------------
    # Recording
    # ------------------------------------------------------------------

    def start_recording(self):
        # Allow recording if either mic and/or system is selected.
        mic_enabled = bool(self.rec_mic_var.get())
        sys_enabled = bool(self.rec_sys_var.get())
        if (not mic_enabled) and (not sys_enabled):
            return

        # Prevent double-start: mic recorder or system-only helper already running
        if self.rec.is_running or self._sys_only_running:
            return

        # The UI still lets the user pick a "base" WAV filename.
        # For consistent output handling, we always create a session folder
        # next to that base name and write:
        #   - mic.wav
        #   - system.wav
        base_out = (self.out_var.get() or "").strip()
        if not base_out:
            base_out = default_out_path()
            self.out_var.set(base_out)

        base_dir = os.path.dirname(base_out) or default_recordings_dir()
        base_stem = os.path.splitext(os.path.basename(base_out))[0] or "session"
        session_dir = _unique_session_dir(base_dir, base_stem)
        os.makedirs(session_dir, exist_ok=True)

        mic_out_path = os.path.join(session_dir, "mic.wav")
        sys_out_path = os.path.join(session_dir, "system.wav")

        # Remember what the user chose for this run
        self._last_out_path = session_dir
        self._last_mic_enabled = mic_enabled
        self._last_sys_enabled = sys_enabled

        # Build config from UI (TscribaRecorder still handles mic capture).
        cfg = TscribaRecorderConfig(
            samplerate=int(self.sr_var.get() or 48000),
            channels=int(self.ch_var.get() or 1),
            mic_gain_db=float(self.mic_gain_var.get() or 0.0),
            system_gain_db=float(self.sys_gain_var.get() or 9.0),
            mic_device=self.selected_mic_id(),
            system_device=None,  # system capture handled via ScreenCaptureKit helper
        )

        # For consistent naming we always use fixed filenames in the session folder.
        # If a source is disabled, we simply don't create its file.

        # Start Systemaudio helper first (so permission errors show early).
        if sys_enabled:
            try:
                self.sys_helper = SystemAudioHelper(
                    sys_out_path,
                    sample_rate=int(self.sr_var.get() or 48000),
                    status_cb=lambda s: self.after(0, lambda: self.status_var.set(s)),
                    level_cb=self.on_sys_level,
                )
                self.sys_helper.start()
            except Exception as e:
                self.sys_helper = None
                messagebox.showerror("Systemaudio error", str(e))
                return

        # Start mic recorder if needed.
        if mic_enabled:
            try:
                # Use TscribaRecorder in mic-only mode for now.
                self.rec.start(mic_out_path, "mic", cfg)
            except Exception as e:
                # If mic start fails, stop system helper as well.
                try:
                    if self.sys_helper is not None:
                        self.sys_helper.stop()
                        self.sys_helper = None
                except Exception:
                    pass
                messagebox.showerror("Recording error", str(e))
                # restore UI state
                self.btn_record.configure(state="normal")
                self.btn_pause.configure(state="disabled", text="Pause")
                self.btn_stop.configure(state="disabled")
                self.rec_mic_chk.configure(state="normal")
                self.rec_sys_chk.configure(state="normal")
                self.on_mode_change()
                return
        else:
            # System-only recording running
            self._sys_only_running = True
            self.status_var.set(f"Recording system audio → {sys_out_path}")

        # Start live transcription (Mic + System) if enabled
        if bool(self.live_transcription_var.get()):
            try:
                if mic_enabled:
                    self._start_mic_transcription(cfg)
                if sys_enabled:
                    self._start_sys_transcription(cfg)
            except Exception as e:
                # Don't fail recording if transcription fails
                try:
                    self.live_transcription_var.set(False)
                    self._update_transcript_status()
                except Exception:
                    pass
                try:
                    messagebox.showerror("Live-Transkription", str(e))
                except Exception:
                    pass

        # lock UI during recording (mic and/or system)
        self.btn_record.configure(state="disabled")
        # Pause only supported for mic (for now)
        if mic_enabled:
            self.btn_pause.configure(state="normal", text="Pause")
        else:
            self.btn_pause.configure(state="disabled", text="Pause")
        self.btn_stop.configure(state="normal")
        self.rec_mic_chk.configure(state="disabled")
        self.rec_sys_chk.configure(state="disabled")
        self.mic_cb.configure(state="disabled")
        self.ch_spin.configure(state="disabled")

    def toggle_pause(self):
        # Pause/Resume is currently only implemented for mic recording.
        if not self.rec.is_running:
            return
        if self.rec.is_paused:
            self.rec.resume()
            self.btn_pause.configure(text="Pause")
        else:
            self.rec.pause()
            self.btn_pause.configure(text="Resume")

    def stop_recording(self):
        if (not self.rec.is_running) and (not self._sys_only_running) and (self.sys_helper is None or not self.sys_helper.is_running):
            return

        # Stop mic first
        try:
            if self.rec.is_running:
                self.rec.stop()
        except Exception:
            pass

        # Stop live transcription
        try:
            self._stop_mic_transcription()
        except Exception:
            pass
        try:
            self._stop_sys_transcription()
        except Exception:
            pass

        # If live transcription was enabled, automatically save a timestamped transcript snapshot.
        try:
            if self.live_transcription_var.get():
                self._save_transcript_txt()
        except Exception:
            pass

        # Stop system helper
        try:
            if self.sys_helper is not None:
                self.sys_helper.stop()
                self.sys_helper = None
        except Exception:
            pass

        self._sys_only_running = False

        self.btn_record.configure(state="normal")
        self.btn_pause.configure(state="disabled", text="Pause")
        self.btn_stop.configure(state="disabled")
        self.rec_mic_chk.configure(state="normal")
        self.rec_sys_chk.configure(state="normal")
        self.on_mode_change()

        # Tell the user where the files were written (consistent names).
        try:
            session_dir = getattr(self, "_last_out_path", "") or ""
            mic_path = os.path.join(session_dir, "mic.wav")
            sys_path = os.path.join(session_dir, "system.wav")

            merged_path = os.path.join(session_dir, "combined.wav")
            # If both mic and system were recorded, also create a single combined WAV (stereo: mic=left, system=right).
            if getattr(self, "_last_sys_enabled", False) and getattr(self, "_last_mic_enabled", False):
                try:
                    self._combine_wavs_to_stereo(mic_path, sys_path, merged_path)
                except Exception:
                    pass

            if getattr(self, "_last_sys_enabled", False) and not getattr(self, "_last_mic_enabled", False):
                self.status_var.set(f"Saved system audio to: {sys_path}")
            elif getattr(self, "_last_sys_enabled", False) and getattr(self, "_last_mic_enabled", False):
                self.status_var.set(f"Saved mic+system audio to: {mic_path}  and  {sys_path}  (combined: {os.path.join(session_dir, 'combined.wav')})")
            elif getattr(self, "_last_mic_enabled", False) and not getattr(self, "_last_sys_enabled", False):
                self.status_var.set(f"Saved microphone audio to: {mic_path}")
        except Exception:
            pass

        # Pre-fill next recording base name
        try:
            self.out_var.set(default_out_path())
        except Exception:
            pass

    # ------------------------------------------------------------------
    # WAV post-processing
    # ------------------------------------------------------------------

    
    def _combine_wavs_to_stereo(self, mic_path: str, sys_path: str, out_path: str) -> None:
        """Create a stereo WAV from mic (L) + system (R).

        Uses only Python stdlib (wave/struct). If the inputs differ in sample rate,
        system audio is resampled (nearest-neighbor) to match mic. If lengths differ,
        the shorter stream is padded with silence.
        """
        import os, wave, struct

        if (not mic_path) or (not sys_path) or (not out_path):
            return
        if (not os.path.exists(mic_path)) or (not os.path.exists(sys_path)):
            return
        if os.path.getsize(mic_path) < 44 or os.path.getsize(sys_path) < 44:
            return

        def _read_int16_mono(path: str):
            with wave.open(path, "rb") as w:
                nch = w.getnchannels()
                sw = w.getsampwidth()
                fr = w.getframerate()
                nframes = w.getnframes()
                data = w.readframes(nframes)

            if sw != 2:
                # Unsupported: keep behavior safe (no crash).
                return None, None

            # Convert to mono if needed (simple average for stereo; first channel for >2).
            if nch == 1:
                samples = struct.unpack("<%dh" % (len(data)//2), data)
                return fr, list(samples)
            elif nch == 2:
                samples = struct.unpack("<%dh" % (len(data)//2), data)
                mono = []
                for i in range(0, len(samples), 2):
                    mono.append(int((samples[i] + samples[i+1]) / 2))
                return fr, mono
            else:
                samples = struct.unpack("<%dh" % (len(data)//2), data)
                mono = []
                step = nch
                for i in range(0, len(samples), step):
                    mono.append(int(samples[i]))
                return fr, mono

        mic_rate, mic = _read_int16_mono(mic_path)
        sys_rate, sys = _read_int16_mono(sys_path)
        if mic is None or sys is None or mic_rate is None or sys_rate is None:
            return

        target_rate = mic_rate

        # Resample system to mic rate if needed (nearest-neighbor).
        if sys_rate != target_rate:
            if sys_rate <= 0 or target_rate <= 0:
                return
            ratio = sys_rate / float(target_rate)
            new_len = int(len(sys) / ratio)
            if new_len <= 0:
                return
            res = []
            for i in range(new_len):
                src_i = int(i * ratio)
                if src_i >= len(sys):
                    break
                res.append(sys[src_i])
            sys = res

        frames = max(len(mic), len(sys))
        if len(mic) < frames:
            mic.extend([0] * (frames - len(mic)))
        if len(sys) < frames:
            sys.extend([0] * (frames - len(sys)))

        stereo = []
        for m, s in zip(mic, sys):
            stereo.append(int(m))
            stereo.append(int(s))

        stereo_bytes = struct.pack("<%dh" % len(stereo), *stereo)

        os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
        with wave.open(out_path, "wb") as wo:
            wo.setnchannels(2)
            wo.setsampwidth(2)
            wo.setframerate(target_rate)
            wo.writeframes(stereo_bytes)
    def on_level(self, db: float):
        # Mic level (dBFS)
        self._level_db = db

    def on_sys_level(self, db: float):
        # Systemaudio level (dBFS). Called from helper reader thread; do not touch Tk here.
        self._sys_level_db = db

    def on_status(self, text: str):
        def _set():
            self.status_var.set(text)

            # Provide a helpful, explicit message if macOS denied ScreenCaptureKit permission.
            # Typical helper stderr: "Failed: ... SCStreamErrorDomain Code=-3801 ... TCC ... abgelehnt"
            if (
                (not self._systemaudio_permission_denied_shown)
                and platform.system() == "Darwin"
                and ("SCStreamErrorDomain" in text or "TCC" in text)
                and ("-3801" in text or "abgelehnt" in text.lower() or "denied" in text.lower())
            ):
                self._systemaudio_permission_denied_shown = True
                try:
                    messagebox.showerror(
                        "Systemaudio: Zugriff verweigert",
                        "macOS hat die Berechtigung für \"Bildschirm- & Systemaudioaufnahme\" verweigert.\n\n"
                        "Der Tscriba Recorder speichert KEIN Bildschirmvideo – nur Systemaudio.\n\n"
                        "Bitte aktiviere den Zugriff unter:\n"
                        "Systemeinstellungen → Datenschutz & Sicherheit → Bildschirm- & Systemaudioaufnahme\n\n"
                        "Danach Tscriba Recorder neu starten.",
                    )
                except Exception:
                    pass

        self.after(0, _set)

    def _tick_ui(self):
        mic_val = int(max(0.0, min(1.0, (self._level_db + 60.0) / 60.0)) * 100)
        sys_val = int(max(0.0, min(1.0, (self._sys_level_db + 60.0) / 60.0)) * 100)

        try:
            self.level_mic.set(mic_val / 100.0)
        except Exception:
            pass
        try:
            self.level_sys.set(sys_val / 100.0)
        except Exception:
            pass

        self.after(50, self._tick_ui)

    def _process_ipc(self):
        # Read and execute tray commands in Tk main thread
        if self._ipc_rfd is not None:
            try:
                while True:
                    data = os.read(self._ipc_rfd, 1024)
                    if not data:
                        break
                    for b in data:
                        ch = chr(b)
                        if ch == "W":
                            self.show_window()
                        elif ch == "R":
                            self.tray_record()
                        elif ch == "P":
                            self.tray_pause_resume()
                        elif ch == "S":
                            self.tray_stop()
                        elif ch == "Q":
                            self.quit_from_tray()
            except BlockingIOError:
                pass
            except OSError:
                pass
            except Exception:
                pass

        self.after(100, self._process_ipc)

    def show_window(self):
        try:
            self.deiconify()
            self.lift()
            self.focus_force()
        except Exception:
            pass

    def tray_record(self):
        if self.rec.is_running:
            self.show_window()
            return
        self.start_recording()

    def tray_pause_resume(self):
        if not self.rec.is_running:
            return
        self.toggle_pause()

    def tray_stop(self):
        if not self.rec.is_running:
            return
        self.stop_recording()

    def quit_from_tray(self):
        try:
            if self.rec.is_running:
                self.rec.stop()
        except Exception:
            pass
        self.on_close()

    def on_close(self):
        try:
            if self.rec.is_running:
                self.rec.stop()
        except Exception:
            pass

        # stop tray
        try:
            if self.tray is not None:
                self.tray.stop()
        except Exception:
            pass

        # close ipc pipe fds
        try:
            if self._ipc_rfd is not None:
                os.close(self._ipc_rfd)
        except Exception:
            pass
        try:
            if self._ipc_wfd is not None:
                os.close(self._ipc_wfd)
        except Exception:
            pass

        self.destroy()


# ----------------------------
# Live transcript window (UI only)
# ----------------------------

    # ----------------------------
    # Live transcript window (UI only)
    # ----------------------------


    # ------------------------------------------------------------------
    # Live transcription (Mic only – faster-whisper)
    # ------------------------------------------------------------------


    def _get_whisper_language(self) -> Optional[str]:
        """Map UI language selection to faster-whisper 'language' parameter.

        - Auto: None (auto-detect)
        - Deutsch: "de"
        - English: "en"
        """
        try:
            v = (self.transcription_language_var.get() or "").strip()
        except Exception:
            v = "Auto"

        if v.lower().startswith("de"):
            return "de"
        if v.lower().startswith("en"):
            return "en"
        return None

    def _get_whisper_tuning(self):
        """Read live transcription tuning settings from UI with safe fallbacks."""
        # Defaults (match previous hard-coded values)
        chunk_s = 2.5
        overlap_s = 0.7
        beam = 3
        vad = True

        try:
            chunk_s = float(self.transcription_chunk_seconds_var.get())
        except Exception:
            pass
        try:
            overlap_s = float(self.transcription_overlap_seconds_var.get())
        except Exception:
            pass
        try:
            beam = int(self.transcription_beam_size_var.get())
        except Exception:
            pass
        try:
            vad = bool(self.transcription_vad_filter_var.get())
        except Exception:
            pass

        # Basic sanity clamps (avoid crashes / weird negatives)
        if not (chunk_s > 0.1):
            chunk_s = 2.5
        if overlap_s < 0.0:
            overlap_s = 0.0
        # overlap should never be >= chunk (would stall)
        if overlap_s >= chunk_s:
            overlap_s = max(0.0, chunk_s * 0.25)

        if beam < 1:
            beam = 1
        if beam > 50:
            beam = 50

        return {
            "chunk_seconds": chunk_s,
            "overlap_seconds": overlap_s,
            "beam_size": beam,
            "vad_filter": vad,
        }


    def on_live_transcription_toggle(self):
        self._update_transcript_status()
        # If recording is already running, start/stop transcription immediately
        try:
            # If recording is already running, start/stop transcription immediately
            mic_enabled = bool(self.rec_mic_var.get())
            sys_enabled = bool(self.rec_sys_var.get())

            recording_active = bool(self.rec.is_running or self._sys_only_running or (self.sys_helper is not None and self.sys_helper.is_running))
            if not recording_active:
                return

            if bool(self.live_transcription_var.get()):
                cfg = TscribaRecorderConfig(
                    samplerate=int(self.sr_var.get() or 48000),
                    channels=int(self.ch_var.get() or 1),
                    mic_gain_db=float(self.mic_gain_var.get() or 0.0),
                    system_gain_db=float(self.sys_gain_var.get() or 9.0),
                    mic_device=self.selected_mic_id(),
                    system_device=None,
                )
                if mic_enabled and self.rec.is_running:
                    self._start_mic_transcription(cfg)
                if sys_enabled and (self.sys_helper is not None):
                    self._start_sys_transcription(cfg)
            else:
                self._stop_mic_transcription()
                self._stop_sys_transcription()
        except Exception as e:
            try:
                messagebox.showerror("Live-Transkription", str(e))
            except Exception:
                pass

    def _update_transcript_status(self):
        if getattr(self, "_transcript_status_var", None) is None:
            return
        enabled = "AN" if self.live_transcription_var.get() else "AUS"
        try:
            self._transcript_status_var.set(f"Live-Transkription: {enabled}")
        except Exception:
            pass

        # Also update language display (especially relevant when set to Auto)
        try:
            if getattr(self, "_transcript_lang_var", None) is None:
                return
            ui_sel = (self.transcription_language_var.get() or "").strip()
            if ui_sel.lower().startswith("de"):
                self._transcript_lang_var.set("Sprache: Deutsch")
            elif ui_sel.lower().startswith("en"):
                self._transcript_lang_var.set("Sprache: English")
            else:
                det = getattr(self, "_detected_language", None)
                prob = getattr(self, "_detected_language_prob", None)
                if det:
                    if prob is None:
                        self._transcript_lang_var.set(f"Sprache: Auto (erkannt: {det})")
                    else:
                        try:
                            self._transcript_lang_var.set(f"Sprache: Auto (erkannt: {det}, {float(prob)*100:.0f}%)")
                        except Exception:
                            self._transcript_lang_var.set(f"Sprache: Auto (erkannt: {det})")
                else:
                    self._transcript_lang_var.set("Sprache: Auto")
        except Exception:
            pass


    def _on_detected_language(self, det_lang: str, prob: Optional[float] = None):
        # Called from transcriber thread; must route to Tk safely
        self._detected_language = det_lang
        self._detected_language_prob = prob

        def _do():
            # update footer label
            try:
                self._update_transcript_status()
            except Exception:
                pass
            # also print a one-time notice into transcript (only for Auto)
            try:
                ui_sel = (self.transcription_language_var.get() or "").strip().lower()
                if not ui_sel.startswith("de") and not ui_sel.startswith("en"):
                    if prob is None:
                        self._append_transcript(f"[LANG] erkannt: {det_lang}\n")
                    else:
                        self._append_transcript(f"[LANG] erkannt: {det_lang} ({float(prob)*100:.0f}%)\n")
            except Exception:
                pass

        try:
            self.after(0, _do)
        except Exception:
            _do()

    def _append_transcript(self, text_line: str):
        if not text_line:
            return

        def _do():
            if self._transcript_text is None:
                return
            try:
                self._transcript_text.configure(state="normal")
                self._transcript_text.insert("end", text_line)
                self._transcript_text.see("end")
                self._transcript_text.configure(state="disabled")
            except Exception:
                pass

        # Tk-safe
        try:
            self.after(0, _do)
        except Exception:
            _do()

    def _start_mic_transcription(self, cfg: "TscribaRecorderConfig"):
        # avoid duplicates
        if self._mic_transcriber is not None:
            return

        # Ensure transcript window exists (optional, but helpful)
        try:
            if self._transcript_win is None:
                self.open_transcript_window()
        except Exception:
            pass

        device_id = getattr(cfg, "mic_device", None)
        sr = int(getattr(cfg, "samplerate", 48000))
        ch = 1  # transcription tap runs mono for robustness

        tuning = self._get_whisper_tuning()

        try:
            model_size = str(self.transcription_model_size_var.get() or "small").strip().lower()
        except Exception:
            model_size = "small"
        if model_size not in ("small", "medium", "large"):
            model_size = "small"

        def status_cb(msg: str):
            try:
                self.after(0, lambda: self.status_var.set(msg))
            except Exception:
                pass
            # Also mirror into transcript window for debugging
            try:
                self._append_transcript(f"[STATUS] {msg}\n")
            except Exception:
                pass

        self._mic_transcriber = FasterWhisperMicTranscriber(
            device=device_id,
            samplerate=sr,
            channels=ch,
            language=self._get_whisper_language(),
            chunk_seconds=tuning["chunk_seconds"],
            overlap_seconds=tuning["overlap_seconds"],
            beam_size=tuning["beam_size"],
            vad_filter=tuning["vad_filter"],
            status_cb=status_cb,
            text_cb=lambda t: self._append_transcript(f"[MIC] {t}"),
            language_detected_cb=self._on_detected_language,
            model_size=model_size,
            external_audio=True,
        )
        
        # Feed mic audio from the recorder into the transcriber (external_audio=True means: no own InputStream)
        try:
            self.rec.set_mic_tap(self._mic_transcriber.push_audio)
        except Exception:
            pass

        try:
            self._mic_transcriber.start()
        except Exception:
            self._mic_transcriber = None
            raise
        self._update_transcript_status()

    def _stop_mic_transcription(self):
        if self._mic_transcriber is None:
            return
        try:
            self._mic_transcriber.stop()
        finally:
            # Stop feeding audio into the transcriber
            try:
                self.rec.set_mic_tap(None)
            except Exception:
                pass
            self._mic_transcriber = None
        self._update_transcript_status()

    def _start_sys_transcription(self, cfg: "TscribaRecorderConfig"):
        # avoid duplicates
        if self._sys_transcriber is not None:
            return
        if self.sys_helper is None:
            return

        # Ensure transcript window exists (optional, but helpful)
        try:
            if self._transcript_win is None:
                self.open_transcript_window()
        except Exception:
            pass

        sr = int(getattr(cfg, "samplerate", 48000))  # helper uses this value (typically 48000)
        ch = 2  # helper outputs stereo float32 by default

        tuning = self._get_whisper_tuning()

        try:
            model_size = str(self.transcription_model_size_var.get() or "small").strip().lower()
        except Exception:
            model_size = "small"
        if model_size not in ("small", "medium", "large"):
            model_size = "small"

        def status_cb(msg: str):
            try:
                self.after(0, lambda: self.status_var.set(msg))
            except Exception:
                pass
            try:
                self._append_transcript(f"[STATUS] {msg}\n")
            except Exception:
                pass

        self._sys_transcriber = FasterWhisperMicTranscriber(
            device=None,
            samplerate=sr,
            channels=ch,
            language=self._get_whisper_language(),
            chunk_seconds=tuning["chunk_seconds"],
            overlap_seconds=tuning["overlap_seconds"],
            beam_size=tuning["beam_size"],
            vad_filter=tuning["vad_filter"],
            status_cb=status_cb,
            text_cb=lambda t: self._append_transcript(f"[SYS] {t}"),
            language_detected_cb=self._on_detected_language,
            model_size=model_size,
            external_audio=True,
        )

        # Feed system audio from ScreenCaptureKit helper into the transcriber
        try:
            self.sys_helper.set_audio_tap(lambda a, _sr, _ch: self._sys_transcriber.push_audio(a))
        except Exception:
            pass

        try:
            self._sys_transcriber.start()
        except Exception:
            self._sys_transcriber = None
            # also remove tap
            try:
                if self.sys_helper is not None:
                    self.sys_helper.set_audio_tap(None)
            except Exception:
                pass
            raise

        self._update_transcript_status()

    def _stop_sys_transcription(self):
        if self._sys_transcriber is None:
            return
        try:
            self._sys_transcriber.stop()
        finally:
            try:
                if self.sys_helper is not None:
                    self.sys_helper.set_audio_tap(None)
            except Exception:
                pass
            self._sys_transcriber = None
        self._update_transcript_status()


    def open_transcript_window(self):
        """Open (or focus) the transcript window. UI only; no transcription yet."""
        if self._transcript_win is not None:
            try:
                self._transcript_win.deiconify()
                self._transcript_win.lift()
                self._transcript_win.focus_force()
                try:
                    self._update_transcript_close_state()
                except Exception:
                    pass
                return
            except Exception:
                self._transcript_win = None
        self._transcript_text = None

        win = ctk.CTkToplevel(self)
        win.title("Tscriba Recorder – Live Transcript")
        win.geometry("720x500")
        win.protocol("WM_DELETE_WINDOW", self._close_transcript_window)
        self._transcript_win = win

        top = ctk.CTkFrame(win)
        top.pack(fill="both", expand=True, padx=10, pady=10)

        hdr = ctk.CTkLabel(
            top,
            text="Live-Transkription – faster-whisper.\n"
                 "Aktiviere die Checkbox im Hauptfenster und starte eine Aufnahme (Mic und/oder System).",
            justify="left",
        )
        hdr.pack(anchor="w", pady=(0, 10))

        txt = ctk.CTkTextbox(top, wrap="word")
        txt.insert("1.0", "")
        txt.configure(state="disabled")
        txt.pack(fill="both", expand=True)
        self._transcript_text = txt

        footer = ctk.CTkFrame(top)
        footer.pack(fill="x", pady=(10, 0))

        enabled = "AN" if self.live_transcription_var.get() else "AUS"
        self._transcript_status_var = tk.StringVar(value=f"Live-Transkription: {enabled}")
        ctk.CTkLabel(footer, textvariable=self._transcript_status_var).pack(side="left")

        # Language display (relevant when UI is set to Auto)
        self._transcript_lang_var = tk.StringVar(value="Sprache: Auto")
        ctk.CTkLabel(footer, textvariable=self._transcript_lang_var).pack(side="left", padx=(12, 0))

                # initialize with current selection / detection
        try:
            self._update_transcript_status()
        except Exception:
            pass

        self._transcript_close_btn = ctk.CTkButton(footer, text="Schließen", command=self._close_transcript_window)
        self._transcript_close_btn.pack(side="right")
        ctk.CTkButton(footer, text="Save", command=self._save_transcript_txt).pack(side="right", padx=(0, 8))

        # Disable transcript window close controls while recording (also during pause)
        self._update_transcript_close_state()




    def _save_transcript_txt(self):
        """Save current transcript text as a timestamped TXT next to the WAV session folder."""
        try:
            txtw = self._transcript_text
            if txtw is None:
                return

            # Determine target folder: prefer current session folder; fallback to selected output folder.
            folder = (getattr(self, "_last_out_path", None) or "").strip()
            if not folder:
                folder = os.path.dirname((self.out_var.get() or "").strip())
            if not folder:
                folder = default_recordings_dir()
            os.makedirs(folder, exist_ok=True)

            # Read current transcript content
            prev_state = str(txtw.cget("state"))
            try:
                if prev_state != "normal":
                    txtw.configure(state="normal")
                content = txtw.get("1.0", "end-1c")
            finally:
                try:
                    if prev_state != "normal":
                        txtw.configure(state=prev_state)
                except Exception:
                    pass

            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            out_path = os.path.join(folder, f"transcript{ts}.txt")
            with open(out_path, "w", encoding="utf-8") as f:
                f.write(content)

            try:
                self.status_var.set(f"Transcript gespeichert: {os.path.basename(out_path)}")
            except Exception:
                pass
        except Exception as e:
            try:
                messagebox.showerror("Fehler", f"Transcript konnte nicht gespeichert werden:\n\n{e}")
            except Exception:
                pass


    def _is_recording_or_paused(self) -> bool:
        """Return True if any recording is active (including pause)."""
        try:
            if getattr(self.rec, "is_running", False):
                return True
        except Exception:
            pass
        try:
            if getattr(self, "_sys_only_running", False):
                return True
        except Exception:
            pass
        try:
            if self.sys_helper is not None and getattr(self.sys_helper, "is_running", False):
                return True
        except Exception:
            pass
        return False

    def _update_transcript_close_state(self):
        """Grey/lock transcript close controls while recording (also during pause)."""
        if self._transcript_win is None:
            return
        locked = self._is_recording_or_paused()

        # Window "X"
        if locked:
            self._transcript_win.protocol("WM_DELETE_WINDOW", lambda: None)
        else:
            self._transcript_win.protocol("WM_DELETE_WINDOW", self._close_transcript_window)

        # Close button (if present)
        try:
            if self._transcript_close_btn is not None:
                self._transcript_close_btn.configure(state=("disabled" if locked else "normal"))
        except Exception:
            pass

    def _close_transcript_window(self):
        """Actually close the transcript window (only allowed when not recording)."""
        if self._transcript_win is None:
            return
        try:
            self._transcript_win.destroy()
        except Exception:
            pass
        self._transcript_win = None
        self._transcript_text = None
        self._transcript_close_btn = None

    # Backward-compatible alias (in case any old callback still references it)
    def _on_transcript_close(self):
        if self._is_recording_or_paused():
            return
        self._close_transcript_window()

if __name__ == "__main__":
    # Required for PyInstaller-frozen apps that (directly or indirectly) use multiprocessing.
    # Prevents child-process relaunch from executing the GUI entrypoint (2nd window) on macOS.
    import multiprocessing as _mp
    _mp.freeze_support()

    app = TscribaRecorderApp()
    app.mainloop()
