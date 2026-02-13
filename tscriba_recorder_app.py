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
import numpy as np  # type: ignore
import sounddevice as sd  # type: ignore
from system_audio_backend import (
    SYSTEM_AUDIO_BACKEND_SCK,
    SYSTEM_AUDIO_BACKEND_COREAUDIO,
    SYSTEM_AUDIO_BACKEND_LABELS,
    normalize_system_audio_backend as _normalize_system_audio_backend,
    system_audio_permission_hint_message,
    system_audio_permission_denied_message,
    SystemAudioHelper,
)
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
try:
    from webrtc_audio_processing import AudioProcessing  # type: ignore
except Exception:
    AudioProcessing = None  # type: ignore

from audio_recorder import (
    TscribaRecorder,
    TscribaRecorderConfig,
    list_input_devices,
    get_device_max_input_channels,
)

def _theme_path():
    try:
        if getattr(sys, "_MEIPASS", None):
            return os.path.join(sys._MEIPASS, "transcriba_theme.json")
    except Exception:
        pass
    try:
        br_fn = globals().get("_bundle_root")
        if br_fn:
            br = br_fn()
            if br is not None:
                return os.path.join(str(br / "Contents" / "Resources"), "transcriba_theme.json")
    except Exception:
        pass
    return os.path.join(os.path.dirname(__file__), "transcriba_theme.json")


def _runtime_state_dir():
    """Writable runtime state directory (never inside the app bundle)."""
    try:
        if sys.platform == "darwin":
            base = os.path.join(os.path.expanduser("~"), "Library", "Application Support", "Transcriba")
        elif os.name == "nt":
            base = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "Transcriba")
        else:
            base = os.path.join(os.path.expanduser("~"), ".config", "Transcriba")
        os.makedirs(base, exist_ok=True)
        return base
    except Exception:
        return os.path.expanduser("~")

# CustomTkinter theme setup (shared with Transcriba Transcription Manager)
ctk.set_appearance_mode("System")
_THEME_PATH = _theme_path()
if os.path.exists(_THEME_PATH):
    ctk.set_default_color_theme(_THEME_PATH)
try:
    _ws = ctk.get_widget_scaling()
    _wns = ctk.get_window_scaling()
    with open(os.path.join(_runtime_state_dir(), "ctk_scaling.txt"), "w", encoding="utf-8") as _f:
        _f.write(f"widget_scaling={_ws}\nwindow_scaling={_wns}\n")
except Exception:
    pass

FONT_BASE = 16
FONT_TITLE = 20
PAD = 5
CONTROL_HEIGHT = 36
BG_MAIN = ("#ffffff", "#1f1f1f")
SIDEBAR_BG = ("#f7f7f7", "#1a1a1a")
ENTRY_BG = ("#f9f9f9", "#2a2a2a")
ENTRY_BORDER = ("#d0d0d0", "#3a3a3a")
ENTRY_TEXT = ("#000000", "#e6e6e6")
READONLY_BG = ("#f2f2f2", "#242424")
TAB_SELECTED_BG = ("#ffffff", "#262626")
TAB_HOVER_BG = ("#cfe8c9", "#2a2a2a")
TAB_TEXT_ACTIVE = ("#000000", "#f5f5f5")
TAB_TEXT_INACTIVE = ("#2e7d32", "#c8c8c8")
BUTTON_BG = ("#cfe8c9", "#2f3a33")
BUTTON_HOVER_BG = ("#2e7d32", "#3f8f5a")
BUTTON_TEXT = ("#2e7d32", "#cfe8c9")
BUTTON_TEXT_HOVER = ("#ffffff", "#ffffff")
LOGO_MAX_SIZE = (180, 60)


def _font(size, weight=None):
    weight = "normal" if weight is None else weight
    return ctk.CTkFont(family="Manrope", size=size, weight=weight)


def _resolve_color(color):
    if isinstance(color, (list, tuple)) and len(color) == 2:
        try:
            mode = ctk.get_appearance_mode()
            if mode == "System":
                try:
                    mode = ctk.get_system_appearance_mode()
                except Exception:
                    mode = "Light"
        except Exception:
            mode = "Light"
        return color[1] if mode == "Dark" else color[0]
    return color


def _style_button(btn):
    if btn is None:
        return
    try:
        btn.configure(
            fg_color=_resolve_color(BUTTON_BG),
            hover_color=_resolve_color(BUTTON_HOVER_BG),
            text_color=_resolve_color(BUTTON_TEXT),
            anchor="center",
            height=CONTROL_HEIGHT,
        )
    except Exception:
        pass
    if not getattr(btn, "_style_button_bound", False):
        try:
            btn.bind("<Enter>", lambda _e, b=btn: b.configure(text_color=_resolve_color(BUTTON_TEXT_HOVER)), add="+")
            btn.bind("<Leave>", lambda _e, b=btn: b.configure(text_color=_resolve_color(BUTTON_TEXT)), add="+")
        except Exception:
            pass
        try:
            btn.bind("<Enter>", lambda _e, b=btn: b.configure(fg_color=_resolve_color(BUTTON_HOVER_BG)), add="+")
            btn.bind("<Leave>", lambda _e, b=btn: b.configure(fg_color=_resolve_color(BUTTON_BG)), add="+")
        except Exception:
            pass
        btn._style_button_bound = True


def _style_option_menu(menu):
    if menu is None:
        return
    try:
        menu.configure(height=CONTROL_HEIGHT, font=_font(size=FONT_BASE))
    except Exception:
        pass
    try:
        for attr in ("_text_label", "_label"):
            lbl = getattr(menu, attr, None)
            if lbl is not None:
                try:
                    lbl.configure(anchor="w", padx=(8, 52))
                except Exception:
                    pass
                try:
                    lbl.grid_configure(padx=(8, 52))
                except Exception:
                    pass
    except Exception:
        pass


def _style_entry(entry):
    if entry is None:
        return
    try:
        entry.configure(
            fg_color=ENTRY_BG,
            border_color=ENTRY_BORDER,
            border_width=1,
            text_color=ENTRY_TEXT,
        )
    except Exception:
        pass


def _style_textbox(textbox):
    if textbox is None:
        return
    try:
        textbox.configure(
            fg_color=ENTRY_BG,
            border_color=ENTRY_BORDER,
            border_width=1,
            text_color=ENTRY_TEXT,
        )
    except Exception:
        pass


def _style_textbox_readonly(textbox):
    if textbox is None:
        return
    try:
        textbox.configure(
            fg_color=READONLY_BG,
            border_width=0,
            text_color=ENTRY_TEXT,
        )
    except Exception:
        pass

RECORDINGS_ROOT_OVERRIDE = None


def default_recordings_dir():
    override = RECORDINGS_ROOT_OVERRIDE
    if override:
        return os.path.expanduser(str(override))
    return os.path.join(os.path.expanduser("~"), "Documents", "Tscriba Recorder Recordings")


def default_out_path():
    base = default_recordings_dir()
    os.makedirs(base, exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    return _unique_session_dir(base, f"rec_{ts}")


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

def _bundle_root():
    """Return Path to *.app bundle root if running from a bundled app, else None."""
    exe = os.path.realpath(sys.executable)
    marker = ".app/Contents/MacOS/"
    if marker in exe:
        # .../Tscriba Recorder.app/Contents/MacOS/Tscriba Recorder -> .../Tscriba Recorder.app
        return Path(exe).parents[2]
    return None

class _EchoCanceller:
    def __init__(self, sample_rate: int):
        self.sample_rate = int(sample_rate)
        self.frame_size = int(self.sample_rate // 100)
        self._ref_buf = np.zeros((0,), dtype=np.float32)
        self._lock = threading.Lock()
        self._ap = None
        if AudioProcessing is not None and self.frame_size > 0:
            try:
                self._ap = AudioProcessing(
                    enable_aec=True,
                    enable_ns=False,
                    enable_agc=False,
                    enable_vad=False,
                    enable_high_pass_filter=True,
                )
            except Exception:
                self._ap = None

    def available(self) -> bool:
        return self._ap is not None

    def _to_mono(self, chunk: np.ndarray) -> np.ndarray:
        if chunk.ndim == 1:
            return chunk.astype(np.float32, copy=False)
        if chunk.shape[1] == 1:
            return chunk[:, 0].astype(np.float32, copy=False)
        return np.mean(chunk, axis=1, dtype=np.float32)

    def feed_reference(self, chunk: np.ndarray, sr: int, _ch: int):
        if self._ap is None or int(sr) != self.sample_rate:
            return
        mono = self._to_mono(chunk)
        with self._lock:
            self._ref_buf = np.concatenate([self._ref_buf, mono])
            max_len = self.sample_rate * 5  # keep up to 5s of reference
            if self._ref_buf.size > max_len:
                self._ref_buf = self._ref_buf[-max_len:]

    def process_mic(self, chunk: np.ndarray, sr: int):
        if self._ap is None or int(sr) != self.sample_rate:
            return chunk
        mono = self._to_mono(chunk)
        out_frames = []
        idx = 0
        with self._lock:
            while idx + self.frame_size <= mono.size:
                mic_frame = mono[idx : idx + self.frame_size]
                if self._ref_buf.size >= self.frame_size:
                    ref_frame = self._ref_buf[: self.frame_size]
                    self._ref_buf = self._ref_buf[self.frame_size :]
                    try:
                        self._ap.process_reverse_stream(ref_frame)
                        proc = self._ap.process_stream(mic_frame)
                    except Exception:
                        proc = mic_frame
                else:
                    proc = mic_frame
                out_frames.append(proc)
                idx += self.frame_size
        if idx < mono.size:
            out_frames.append(mono[idx:])
        if not out_frames:
            return chunk
        out_mono = np.concatenate(out_frames)
        if chunk.ndim == 2 and chunk.shape[1] > 1:
            return np.repeat(out_mono[:, None], chunk.shape[1], axis=1)
        return out_mono

def _create_tray_image(recording: bool = False, phase: int = 0):
    # Simple status icon:
    # - idle: gray dot
    # - recording: pulsing red ring + red center
    if not TRAY_AVAILABLE:
        return None
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    if recording:
        ring = (255, 80, 80, 220) if (phase % 2 == 0) else (255, 80, 80, 120)
        center = (220, 0, 0, 255) if (phase % 2 == 0) else (220, 0, 0, 210)
        d.ellipse((8, 8, 56, 56), fill=ring)
        d.ellipse((16, 16, 48, 48), fill=center)
    else:
        d.ellipse((12, 12, 52, 52), fill=(130, 130, 130, 255))
    return img


class TrayController:
    """IMPORTANT (macOS): Tray callbacks must NOT call Tk directly.
    They only write single-letter commands into an OS pipe.
    The Tk main thread polls the pipe and executes actions safely.
    """
    def __init__(self, ipc_write_fd: int):
        self._wfd = ipc_write_fd
        self.icon = None
        self._last_title = None
        self._last_recording_state = None
        self._last_recording_phase = None

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
        self.icon = pystray.Icon("Tscriba Recorder", _create_tray_image(recording=False), "Tscriba Recorder", menu)

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

    def set_title(self, title: str):
        try:
            if self.icon is None:
                return
            t = str(title or "").strip()
            if not t:
                return
            if t == self._last_title:
                return
            self.icon.title = t
            self._last_title = t
        except Exception:
            pass

    def set_recording_effect(self, active: bool, phase: int = 0):
        try:
            if self.icon is None:
                return
            if self._last_recording_state == bool(active) and self._last_recording_phase == int(phase):
                return
            self.icon.icon = _create_tray_image(recording=bool(active), phase=int(phase))
            self._last_recording_state = bool(active)
            self._last_recording_phase = int(phase)
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
        self.title("Transcriba")
        self.geometry("1024x720")
        try:
            self.configure(fg_color=BG_MAIN)
        except Exception:
            pass
        self._center_window(1024, 720)

        # IMPORTANT: set scaling on THIS root (no second Tk window!)
        try:
            self.tk.call("tk", "scaling", 1.0)
        except Exception:
            pass

        self._level_db = -120.0
        self._sys_level_db = -120.0
        self._echo_active = False
        self._mic_gain_db_base = 0.0
        self._record_elapsed_total = 0.0
        self._record_started_at = None
        self._record_timer_var = tk.StringVar(value="00:00:00")
        self._last_tray_timer_text = None
        self._last_tray_recording_effect = None
        self._test_running = False
        self._test_mic_stream = None
        self._test_sys_helper = None

        # --- Live transcription UI (Mic only: faster-whisper, Schritt 1) ---
        self.live_transcription_var = tk.BooleanVar(value=False)
        self.transcription_language_var = tk.StringVar(value="Auto")
        # Live transcription tuning (UI)
        self.transcription_chunk_seconds_var = tk.DoubleVar(value=2.5)
        self.transcription_overlap_seconds_var = tk.DoubleVar(value=0.7)
        self.transcription_beam_size_var = tk.IntVar(value=3)
        self.transcription_vad_filter_var = tk.BooleanVar(value=True)
        self.transcription_model_size_var = tk.StringVar(value="small")
        self.appearance_mode = tk.StringVar(value="System")
        self.recordings_root_var = tk.StringVar(value=default_recordings_dir())
        self.system_audio_backend_var = tk.StringVar(value=SYSTEM_AUDIO_BACKEND_LABELS[SYSTEM_AUDIO_BACKEND_SCK])
        self.auto_small_on_recording_var = tk.BooleanVar(value=False)
        self.auto_stop_on_silence_var = tk.BooleanVar(value=False)
        self.silence_threshold_db_var = tk.DoubleVar(value=-55.0)
        self.silence_duration_seconds_var = tk.DoubleVar(value=8.0)
        self.auto_duck_var = tk.BooleanVar(value=False)
        self.auto_duck_strength_var = tk.DoubleVar(value=18.0)
        self.aec_enabled_var = tk.BooleanVar(value=True)
        self._silence_started_at = None
        self._silence_grace_until = 0.0
        self._silence_autostop_triggered = False
        # Recorder settings vars (init early for settings load)
        self.sr_var = tk.IntVar(value=48000)
        self.ch_var = tk.IntVar(value=1)
        self.mic_gain_var = tk.DoubleVar(value=0.0)
        self.sys_gain_var = tk.DoubleVar(value=9.0)
        self._settings_loaded = False
        self._load_settings()
        self._transcript_win = None
        self._transcript_close_btn = None
        self._transcript_text = None
        self._transcript_text = None
        self._mic_transcriber = None
        self._sys_transcriber = None
        self._aec = None
        self._sys_tap_transcriber = None
        self._on_sys_audio_tap = self._update_sys_level_from_tap

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
        self._build_ui()

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
    # UI
    # ------------------------------------------------------------------

    def _bundle_dir(self):
        br = _bundle_root()
        if br is not None:
            return str(br / "Contents" / "Resources")
        return os.path.dirname(os.path.abspath(__file__))

    def _settings_dir(self):
        try:
            if sys.platform == "darwin":
                base = os.path.join(os.path.expanduser("~"), "Library", "Application Support", "Transcriba")
            elif os.name == "nt":
                base = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "Transcriba")
            else:
                base = os.path.join(os.path.expanduser("~"), ".config", "Transcriba")
            os.makedirs(base, exist_ok=True)
            return base
        except Exception:
            return self._bundle_dir()

    def _settings_path(self):
        return os.path.join(self._settings_dir(), "recorder_settings.json")

    def _legacy_settings_path(self):
        return os.path.join(self._bundle_dir(), "recorder_settings.json")

    def _center_window(self, w: int, h: int):
        try:
            self.update_idletasks()
            sw = self.winfo_screenwidth()
            sh = self.winfo_screenheight()
            x = max(0, (sw - w) // 2)
            y = max(0, (sh - h) // 2)
            self.geometry(f"{w}x{h}+{x}+{y}")
        except Exception:
            pass

    def _browse_recordings_root(self):
        try:
            initial = self.recordings_root_var.get() or default_recordings_dir()
        except Exception:
            initial = default_recordings_dir()
        try:
            path = filedialog.askdirectory(initialdir=initial, title="Aufnahmeordner auswählen")
        except Exception:
            path = None
        if path:
            self.recordings_root_var.set(path)
            try:
                global RECORDINGS_ROOT_OVERRIDE
                RECORDINGS_ROOT_OVERRIDE = path
            except Exception:
                pass
            try:
                self.out_var.set(default_out_path())
            except Exception:
                pass

    def _load_settings(self):
        path = self._settings_path()
        if not os.path.exists(path):
            legacy = self._legacy_settings_path()
            if not os.path.exists(legacy):
                return
            path = legacy
        try:
            import json as _json
            with open(path, "r", encoding="utf-8") as f:
                data = _json.load(f)
            self.sr_var.set(int(data.get("sample_rate", self.sr_var.get() or 48000)))
            self.ch_var.set(int(data.get("channels", self.ch_var.get() or 1)))
            self.mic_gain_var.set(float(data.get("mic_gain_db", self.mic_gain_var.get() or 0.0)))
            self.sys_gain_var.set(float(data.get("system_gain_db", self.sys_gain_var.get() or 9.0)))
            self.live_transcription_var.set(bool(data.get("live_transcription", self.live_transcription_var.get())))
            self.transcription_language_var.set(str(data.get("language", self.transcription_language_var.get() or "Auto")))
            self.transcription_chunk_seconds_var.set(float(data.get("chunk_seconds", self.transcription_chunk_seconds_var.get() or 2.5)))
            self.transcription_overlap_seconds_var.set(float(data.get("overlap_seconds", self.transcription_overlap_seconds_var.get() or 0.7)))
            self.transcription_beam_size_var.set(int(data.get("beam_size", self.transcription_beam_size_var.get() or 3)))
            self.transcription_vad_filter_var.set(bool(data.get("vad_filter", self.transcription_vad_filter_var.get())))
            self.transcription_model_size_var.set(str(data.get("model_size", self.transcription_model_size_var.get() or "small")))
            self.auto_duck_var.set(bool(data.get("auto_duck_mic", self.auto_duck_var.get())))
            self.auto_duck_strength_var.set(float(data.get("auto_duck_strength_db", self.auto_duck_strength_var.get() or 18.0)))
            self.aec_enabled_var.set(bool(data.get("aec_enabled", self.aec_enabled_var.get())))
            self.recordings_root_var.set(str(data.get("recordings_root", self.recordings_root_var.get() or default_recordings_dir())))
            backend = _normalize_system_audio_backend(data.get("system_audio_backend", SYSTEM_AUDIO_BACKEND_SCK))
            self.system_audio_backend_var.set(SYSTEM_AUDIO_BACKEND_LABELS.get(backend, SYSTEM_AUDIO_BACKEND_LABELS[SYSTEM_AUDIO_BACKEND_SCK]))
            self.auto_small_on_recording_var.set(bool(data.get("auto_small_on_recording", self.auto_small_on_recording_var.get())))
            self.auto_stop_on_silence_var.set(bool(data.get("auto_stop_on_silence", self.auto_stop_on_silence_var.get())))
            self.silence_threshold_db_var.set(float(data.get("silence_threshold_db", self.silence_threshold_db_var.get() or -55.0)))
            self.silence_duration_seconds_var.set(float(data.get("silence_duration_seconds", self.silence_duration_seconds_var.get() or 8.0)))
            try:
                global RECORDINGS_ROOT_OVERRIDE
                RECORDINGS_ROOT_OVERRIDE = self.recordings_root_var.get()
            except Exception:
                pass
            self._settings_loaded = True
            # Migrate legacy settings to new location if needed
            if path != self._settings_path():
                try:
                    with open(self._settings_path(), "w", encoding="utf-8") as f:
                        _json.dump(data, f, ensure_ascii=False, indent=2)
                except Exception:
                    pass
        except Exception:
            pass

    def _save_settings(self):
        path = self._settings_path()
        sr = self._spin_read_value(self.sr_spin, self.sr_var.get() or 48000, cast=int)
        ch = self._spin_read_value(self.ch_spin, self.ch_var.get() or 1, cast=int)
        mic_gain = self._spin_read_value(self.mic_gain_spin, self.mic_gain_var.get() or 0.0, cast=float)
        sys_gain = self._spin_read_value(self.sys_gain_spin, self.sys_gain_var.get() or 9.0, cast=float)
        silence_threshold = self._spin_read_value(
            getattr(self, "silence_threshold_spin", None),
            self.silence_threshold_db_var.get() or -55.0,
            cast=float,
        )
        silence_duration = self._spin_read_value(
            getattr(self, "silence_duration_spin", None),
            self.silence_duration_seconds_var.get() or 8.0,
            cast=float,
        )
        silence_threshold = max(-120.0, min(0.0, float(silence_threshold)))
        silence_duration = max(1.0, min(3600.0, float(silence_duration)))
        data = {
            "sample_rate": int(sr),
            "channels": int(ch),
            "mic_gain_db": float(mic_gain),
            "system_gain_db": float(sys_gain),
            "live_transcription": bool(self.live_transcription_var.get()),
            "language": str(self.transcription_language_var.get() or "Auto"),
            "chunk_seconds": float(self.transcription_chunk_seconds_var.get() or 2.5),
            "overlap_seconds": float(self.transcription_overlap_seconds_var.get() or 0.7),
            "beam_size": int(self.transcription_beam_size_var.get() or 3),
            "vad_filter": bool(self.transcription_vad_filter_var.get()),
            "model_size": str(self.transcription_model_size_var.get() or "small"),
            "auto_duck_mic": bool(self.auto_duck_var.get()),
            "auto_duck_strength_db": float(self.auto_duck_strength_var.get() or 18.0),
            "aec_enabled": bool(self.aec_enabled_var.get()),
            "recordings_root": str(self.recordings_root_var.get() or ""),
            "system_audio_backend": _normalize_system_audio_backend(self.system_audio_backend_var.get()),
            "auto_small_on_recording": bool(self.auto_small_on_recording_var.get()),
            "auto_stop_on_silence": bool(self.auto_stop_on_silence_var.get()),
            "silence_threshold_db": float(silence_threshold),
            "silence_duration_seconds": float(silence_duration),
        }
        try:
            self.sr_var.set(int(sr))
            self.ch_var.set(int(ch))
            self.mic_gain_var.set(float(mic_gain))
            self.sys_gain_var.set(float(sys_gain))
            self.silence_threshold_db_var.set(float(silence_threshold))
            self.silence_duration_seconds_var.set(float(silence_duration))
        except Exception:
            pass
        try:
            import json as _json
            with open(path, "w", encoding="utf-8") as f:
                _json.dump(data, f, ensure_ascii=False, indent=2)
            self._settings_loaded = True
            try:
                self._btn_save_settings.configure(text="Aktualisieren")
            except Exception:
                pass
            try:
                global RECORDINGS_ROOT_OVERRIDE
                RECORDINGS_ROOT_OVERRIDE = self.recordings_root_var.get()
            except Exception:
                pass
        except Exception:
            pass
    def _load_logo_image(self):
        logo_path = os.path.join(self._bundle_dir(), "assets", "transcriba.png")
        if not os.path.exists(logo_path):
            return None
        try:
            if Image is not None:
                img = Image.open(logo_path)
                img.thumbnail(LOGO_MAX_SIZE, Image.LANCZOS)
                return ctk.CTkImage(light_image=img, dark_image=img, size=img.size)
            return None
        except Exception:
            return None

    def _build_ui(self):
        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(1, weight=1)

        self._sidebar_width = 230
        sidebar = ctk.CTkFrame(self, width=self._sidebar_width, corner_radius=0, fg_color=SIDEBAR_BG)
        sidebar.grid(row=0, column=0, sticky="nsw")
        sidebar.grid_propagate(False)
        sidebar.grid_rowconfigure(90, weight=1)
        sidebar.grid_columnconfigure(0, weight=1)
        self.sidebar = sidebar

        logo_container = ctk.CTkFrame(sidebar, fg_color=SIDEBAR_BG, height=75)
        logo_container.grid(row=0, column=0, sticky="ew", padx=0, pady=0)
        logo_container.grid_propagate(False)
        logo_container.grid_columnconfigure(0, weight=1)
        logo_container.grid_rowconfigure(0, weight=1)
        self._logo_image = self._load_logo_image()
        self.logo_label = ctk.CTkLabel(
            logo_container,
            text="",
            image=self._logo_image,
            anchor="center",
            height=LOGO_MAX_SIZE[1],
        )
        self.logo_label.grid(row=0, column=0, sticky="nsew", padx=0, pady=0)

        title = ctk.CTkLabel(
            sidebar,
            text="Audio Recorder",
            font=_font(size=FONT_TITLE),
            justify="left",
            anchor="w",
        )
        title.grid(row=1, column=0, sticky="w", padx=PAD, pady=(0, PAD))

        # Sidebar tabs
        self._tab_buttons = {}
        self._tab_button_defaults = {}
        tab_names = ["Recorder", "Live Transcript", "Settings"]
        row = 2
        for name in tab_names:
            btn = ctk.CTkButton(
                sidebar,
                text=name,
                font=_font(size=FONT_BASE),
                anchor="w",
                corner_radius=0,
                border_width=0,
                fg_color="transparent",
                hover_color=_resolve_color(TAB_HOVER_BG),
                text_color=_resolve_color(TAB_TEXT_INACTIVE),
                width=230,
                height=36,
                command=lambda n=name: self._select_tab(n),
            )
            try:
                btn.configure(border_spacing=10)
            except Exception:
                pass
            btn.grid(row=row, column=0, sticky="ew", padx=0, pady=5)
            self._tab_buttons[name] = btn
            btn.bind("<Enter>", lambda _e, n=name: self._on_tab_hover(n, True))
            btn.bind("<Leave>", lambda _e, n=name: self._on_tab_hover(n, False))
            btn.bind("<ButtonPress-1>", lambda _e, n=name: self._on_tab_press(n))
            btn.bind("<ButtonRelease-1>", lambda _e, n=name: self._on_tab_release(n))
            try:
                self._tab_button_defaults[name] = {
                    "fg_color": btn.cget("fg_color"),
                    "text_color": btn.cget("text_color"),
                }
            except Exception:
                self._tab_button_defaults[name] = {"fg_color": None, "text_color": None}
            row += 1

        self.compact_panel = ctk.CTkFrame(sidebar, fg_color=SIDEBAR_BG, corner_radius=8)
        self.compact_panel.grid(row=90, column=0, sticky="nsew", padx=PAD, pady=(PAD, 0))
        self.compact_panel.grid_remove()
        self.compact_panel.grid_columnconfigure(0, weight=1)
        self.compact_panel.grid_rowconfigure(2, weight=1)

        compact_btn_row = ctk.CTkFrame(self.compact_panel, fg_color="transparent", corner_radius=0)
        compact_btn_row.grid(row=0, column=0, sticky="ew")
        compact_btn_row.grid_columnconfigure((0, 1, 2), weight=1)

        compact_h = max(28, CONTROL_HEIGHT - 8)
        compact_font = _font(max(12, FONT_BASE - 2))
        self.btn_record_compact = ctk.CTkButton(
            compact_btn_row, text="Play", command=self.start_recording, font=compact_font, height=compact_h
        )
        _style_button(self.btn_record_compact)
        self.btn_record_compact.grid(row=0, column=0, sticky="ew", padx=(0, 4))

        self.btn_pause_compact = ctk.CTkButton(
            compact_btn_row, text="Pause", command=self.toggle_pause, font=compact_font, height=compact_h
        )
        _style_button(self.btn_pause_compact)
        self.btn_pause_compact.grid(row=0, column=1, sticky="ew", padx=2)

        self.btn_stop_compact = ctk.CTkButton(
            compact_btn_row, text="Stop", command=self.stop_recording, font=compact_font, height=compact_h
        )
        _style_button(self.btn_stop_compact)
        self.btn_stop_compact.grid(row=0, column=2, sticky="ew", padx=(4, 0))

        compact_levels = ctk.CTkFrame(self.compact_panel, fg_color="transparent", corner_radius=0)
        compact_levels.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        compact_levels.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(compact_levels, text="Mic", font=_font(FONT_BASE)).grid(row=0, column=0, sticky="w")
        self.level_mic_compact = ctk.CTkProgressBar(compact_levels)
        self.level_mic_compact.set(0)
        self.level_mic_compact.grid(row=1, column=0, sticky="ew")
        ctk.CTkLabel(compact_levels, text="System", font=_font(FONT_BASE)).grid(row=2, column=0, sticky="w", pady=(4, 0))
        self.level_sys_compact = ctk.CTkProgressBar(compact_levels)
        self.level_sys_compact.set(0)
        self.level_sys_compact.grid(row=3, column=0, sticky="ew")

        self._compact_transcript_text = ctk.CTkTextbox(self.compact_panel, wrap="word", font=_font(FONT_BASE))
        _style_textbox(self._compact_transcript_text)
        self._compact_transcript_text.grid(row=2, column=0, sticky="nsew", pady=(8, 0))
        self._compact_transcript_text.configure(state="disabled")
        _style_textbox_readonly(self._compact_transcript_text)

        self._compact_controls = {
            "record": self.btn_record_compact,
            "pause": self.btn_pause_compact,
            "stop": self.btn_stop_compact,
        }

        self._compact_view = False
        self._full_geometry_before_compact = None
        self._compact_fixed_width = None
        self._enforcing_compact_window = False
        self._compact_fullscreen_exit_in_progress = False
        self._normal_window_minsize = None
        self._normal_window_maxsize = None
        self._normal_window_resizable = (True, True)
        self.btn_toggle_view = ctk.CTkButton(
            sidebar,
            text="Kleine Ansicht",
            width=180,
            font=_font(size=FONT_BASE),
            command=self._toggle_compact_view,
        )
        _style_button(self.btn_toggle_view)
        self.btn_toggle_view.grid(row=95, column=0, sticky="ew", padx=PAD, pady=(PAD, 0))

        self.btn_toggle_log = ctk.CTkButton(
            sidebar,
            text="Log anzeigen",
            width=180,
            font=_font(size=FONT_BASE),
            command=self._toggle_log_panel,
        )
        _style_button(self.btn_toggle_log)
        self.btn_toggle_log.grid(row=96, column=0, sticky="ew", padx=PAD, pady=PAD)


        self.main_pane = tk.PanedWindow(
            self,
            orient=tk.HORIZONTAL,
            sashrelief="raised",
            sashwidth=6,
            bd=0,
            bg=_resolve_color(BG_MAIN),
        )
        self.main_pane.grid(row=0, column=1, sticky="nsew", pady=0)

        self.content_root = ctk.CTkFrame(self, corner_radius=0, fg_color=BG_MAIN)
        self.log_root = ctk.CTkFrame(self, corner_radius=0, fg_color=BG_MAIN)
        self.main_pane.add(self.content_root, minsize=480, padx=1)
        self.main_pane.add(self.log_root, minsize=240)
        self._log_collapsed = True
        self._log_last_width = 400
        self.main_pane.bind("<Configure>", lambda _e: self._ensure_log_width())
        try:
            self.main_pane.forget(self.log_root)
        except Exception:
            pass

        self.content_root.grid_rowconfigure(0, weight=1)
        self.content_root.grid_columnconfigure(0, weight=1)
        self._tab_frames = {}
        self._tab_bodies = {}
        self._tab_headlines = {
            "Recorder": "Recorder",
            "Live Transcript": "Live Transcript",
            "Settings": "Settings",
        }
        for name in tab_names:
            tab_frame = ctk.CTkFrame(self.content_root, fg_color=BG_MAIN)
            tab_frame.grid(row=0, column=0, sticky="nsew")
            tab_frame.grid_rowconfigure(0, weight=1)
            tab_frame.grid_columnconfigure(0, weight=1)

            body = ctk.CTkFrame(tab_frame, fg_color=BG_MAIN)
            body.grid(row=0, column=0, sticky="nsew")
            body.grid_rowconfigure(0, weight=0)
            body.grid_rowconfigure(2, weight=1)
            body.grid_columnconfigure(0, weight=1)

            header_spacer = ctk.CTkFrame(body, height=75, fg_color="transparent")
            header_spacer.grid(row=0, column=0, sticky="ew")
            header_spacer.grid_propagate(False)

            tab_header = ctk.CTkLabel(
                body,
                text=self._tab_headlines.get(name, name),
                font=_font(size=FONT_TITLE),
                justify="left",
                anchor="w",
            )
            tab_header.grid(row=1, column=0, sticky="w", padx=PAD, pady=(0, PAD))

            self._tab_frames[name] = tab_frame
            self._tab_bodies[name] = body

        self._build_recorder_tab(self._tab_bodies["Recorder"])
        self._build_transcript_tab(self._tab_bodies["Live Transcript"])
        self._build_settings_tab(self._tab_bodies["Settings"])

        self._build_log_panel()

        self._tab_disabled = set()
        self._set_tab_enabled("Live Transcript", bool(self.live_transcription_var.get()))

        self._select_tab("Recorder")
        self.refresh_devices()
        self.on_mode_change()

        self.after(0, self._ensure_log_width)
        self.after(0, self._sync_main_layout)
        self.after(0, self._capture_default_window_constraints)
        self.bind("<Configure>", self._on_window_configure, add="+")

    def _spin_set_state(self, spin, state):
        try:
            entry = getattr(spin, "_spin_entry", None)
            btn_up = getattr(spin, "_spin_btn_up", None)
            btn_down = getattr(spin, "_spin_btn_down", None)
            if entry is not None:
                entry.configure(state=state)
            if btn_up is not None:
                btn_up.configure(state=state)
            if btn_down is not None:
                btn_down.configure(state=state)
        except Exception:
            pass

    def _spin_set_limits(self, spin, min_v, max_v):
        try:
            spin._spin_min = min_v
            spin._spin_max = max_v
        except Exception:
            pass

    def _spin_read_value(self, spin, default, cast=float):
        try:
            entry = getattr(spin, "_spin_entry", None)
            if entry is None:
                return cast(default)
            raw = entry.get()
            if raw is None:
                return cast(default)
            raw = str(raw).strip()
            if raw == "":
                return cast(default)
            return cast(raw)
        except Exception:
            try:
                return cast(default)
            except Exception:
                return default

    def _safe_set_state(self, widget, state, label: str):
        if widget is None:
            return
        try:
            widget.configure(state=state)
        except Exception as e:
            try:
                self._log_line(f"[ui] state failed {label}: {type(widget).__name__} ({e})")
            except Exception:
                pass

    def _make_spinbox(self, parent, var, from_, to, increment=1, width=80, expand=False):
        container = ctk.CTkFrame(parent, fg_color="transparent", corner_radius=0)
        if expand:
            container.grid_columnconfigure(0, weight=1)

        entry_width = 0 if expand else width
        entry = ctk.CTkEntry(container, textvariable=var, width=entry_width, height=CONTROL_HEIGHT, font=_font(FONT_BASE))
        _style_entry(entry)
        entry.grid(row=0, column=0, sticky=("ew" if expand else "w"))

        btns = ctk.CTkFrame(container, fg_color="transparent", corner_radius=0)
        btns.grid(row=0, column=1, sticky="ns", padx=(4, 0))

        def _parse_value():
            try:
                return float(var.get())
            except Exception:
                try:
                    return float(from_)
                except Exception:
                    return 0.0

        def _format_value(v):
            try:
                if float(increment).is_integer():
                    return str(int(round(v)))
            except Exception:
                pass
            try:
                s = str(increment)
                decimals = len(s.split(".")[1]) if "." in s else 0
            except Exception:
                decimals = 1
            return f"{v:.{decimals}f}"

        def _apply(delta):
            v = _parse_value() + delta
            min_v = getattr(container, "_spin_min", from_)
            max_v = getattr(container, "_spin_max", to)
            if min_v is not None:
                v = max(float(min_v), v)
            if max_v is not None:
                v = min(float(max_v), v)
            var.set(_format_value(v))

        btn_up = ctk.CTkButton(
            btns, text="▲", width=12, height=12, font=_font(8),
            fg_color=_resolve_color(BUTTON_BG), hover_color=_resolve_color(BUTTON_HOVER_BG),
            text_color=_resolve_color(BUTTON_TEXT), corner_radius=4, border_width=0
        )
        btn_down = ctk.CTkButton(
            btns, text="▼", width=12, height=12, font=_font(8),
            fg_color=_resolve_color(BUTTON_BG), hover_color=_resolve_color(BUTTON_HOVER_BG),
            text_color=_resolve_color(BUTTON_TEXT), corner_radius=4, border_width=0
        )
        btn_up.configure(command=lambda: _apply(float(increment)))
        btn_down.configure(command=lambda: _apply(-float(increment)))
        btn_up.grid(row=0, column=0, padx=0, pady=(0, 2))
        btn_down.grid(row=1, column=0, padx=0, pady=(2, 0))

        container._spin_entry = entry
        container._spin_btn_up = btn_up
        container._spin_btn_down = btn_down
        container._spin_min = from_
        container._spin_max = to
        container._spin_inc = increment
        return container

    def _build_recorder_tab(self, parent):
        frm = ctk.CTkFrame(parent, fg_color=BG_MAIN, corner_radius=0)
        frm.grid(row=2, column=0, sticky="nsew", padx=12, pady=(0, 12))

        r0 = ctk.CTkFrame(frm, fg_color="transparent", corner_radius=0)
        r0.pack(fill="x", pady=(0, 8))
        ctk.CTkLabel(r0, text="Aufnahme", font=_font(FONT_BASE)).pack(anchor="w")

        self.rec_mic_var = tk.BooleanVar(value=True)
        self.rec_sys_var = tk.BooleanVar(value=False)
        r1 = ctk.CTkFrame(frm, fg_color="transparent", corner_radius=0)
        r1.pack(fill="x", pady=(0, 8))
        r1_top = ctk.CTkFrame(r1, fg_color="transparent", corner_radius=0)
        r1_top.pack(fill="x", pady=(0, 4))
        self.rec_mic_chk = ctk.CTkCheckBox(
            r1_top,
            text="",
            variable=self.rec_mic_var,
            command=self.on_mode_change,
            font=_font(FONT_BASE),
            width=22,
            height=CONTROL_HEIGHT,
            checkbox_width=22,
            checkbox_height=22,
        )
        self.rec_mic_chk.pack(side="left", padx=(0, 0))
        self.mic_var = tk.StringVar(value="Default Input")
        self.mic_cb = ctk.CTkComboBox(
            r1_top,
            variable=self.mic_var,
            values=[],
            command=lambda _val: self.on_device_change(),
            font=_font(FONT_BASE),
            height=CONTROL_HEIGHT,
        )
        _style_entry(self.mic_cb)
        self.mic_cb.pack(side="left", fill="x", expand=True)
        btn_refresh = ctk.CTkButton(
            r1_top, text="Refresh", command=self.refresh_devices, font=_font(FONT_BASE), height=CONTROL_HEIGHT
        )
        _style_button(btn_refresh)
        btn_refresh.pack(side="right", padx=(4, 0))

        r2 = ctk.CTkFrame(frm, fg_color="transparent", corner_radius=0)
        r2.pack(fill="x", pady=(0, 8))
        r2_top = ctk.CTkFrame(r2, fg_color="transparent", corner_radius=0)
        r2_top.pack(fill="x", pady=(0, 4))
        self.rec_sys_chk = ctk.CTkCheckBox(
            r2_top,
            text="",
            variable=self.rec_sys_var,
            command=self.on_mode_change,
            font=_font(FONT_BASE),
            width=22,
            height=CONTROL_HEIGHT,
            checkbox_width=22,
            checkbox_height=22,
        )
        self.rec_sys_chk.pack(side="left", padx=(0, 0))
        self.sys_cb = ctk.CTkComboBox(
            r2_top,
            variable=self.system_audio_backend_var,
            values=list(SYSTEM_AUDIO_BACKEND_LABELS.values()),
            command=lambda _val: self.on_mode_change(),
            font=_font(FONT_BASE),
            height=CONTROL_HEIGHT,
        )
        _style_entry(self.sys_cb)
        self.sys_cb.pack(side="left", fill="x", expand=True)
        self.sys_refresh_btn = ctk.CTkButton(
            r2_top, text="Refresh", command=lambda: None, font=_font(FONT_BASE), height=CONTROL_HEIGHT, state="disabled"
        )
        _style_button(self.sys_refresh_btn)
        self.sys_refresh_btn.pack(side="right", padx=(4, 0))

        r2b = ctk.CTkFrame(frm, fg_color="transparent", corner_radius=0)
        r2b.pack(fill="x", pady=(0, 8))
        self.live_transcription_chk = ctk.CTkCheckBox(
            r2b,
            text="Live-Transkription aktivieren",
            variable=self.live_transcription_var,
            command=self.on_live_transcription_toggle,
            font=_font(FONT_BASE),
        )
        self.live_transcription_chk.pack(side="left")

        r4 = ctk.CTkFrame(frm, fg_color="transparent", corner_radius=0)
        r4.pack(fill="x", pady=(12, 12))
        ctk.CTkLabel(r4, text="Ausgabe", font=_font(FONT_BASE)).pack(anchor="w")
        self.out_var = tk.StringVar(value=default_out_path())
        r4a = ctk.CTkFrame(r4, fg_color="transparent", corner_radius=0)
        r4a.pack(fill="x")
        out_entry = ctk.CTkEntry(r4a, textvariable=self.out_var, height=CONTROL_HEIGHT, font=_font(FONT_BASE))
        _style_entry(out_entry)
        out_entry.pack(side="left", fill="x", expand=True)
        btn_browse = ctk.CTkButton(
            r4a, text="Browse…", command=self.browse_out, font=_font(FONT_BASE), height=CONTROL_HEIGHT
        )
        _style_button(btn_browse)
        btn_browse.pack(side="left", padx=(8, 0))

        r4b = ctk.CTkFrame(r4, fg_color="transparent", corner_radius=0)
        r4b.pack(fill="x", pady=(6, 0))
        btn_open_folder = ctk.CTkButton(
            r4b, text="Ordner öffnen", command=self.open_recordings_folder, font=_font(FONT_BASE), height=CONTROL_HEIGHT
        )
        _style_button(btn_open_folder)
        btn_open_folder.pack(side="right")

        r5 = ctk.CTkFrame(frm, fg_color="transparent", corner_radius=0)
        r5.pack(fill="x", pady=(16, 0))
        r5c = ctk.CTkFrame(r5, fg_color="transparent", corner_radius=0)
        r5c.pack(anchor="center")
        self.btn_test = ctk.CTkButton(
            r5c, text="Start Test", command=self.toggle_test_mode, font=_font(FONT_BASE), height=CONTROL_HEIGHT
        )
        _style_button(self.btn_test)
        self.btn_test.pack(side="left", padx=(0, 8))
        self.btn_record = ctk.CTkButton(
            r5c, text="Record", command=self.start_recording, font=_font(FONT_BASE), height=CONTROL_HEIGHT
        )
        _style_button(self.btn_record)
        self.btn_record.pack(side="left")
        self.btn_pause = ctk.CTkButton(
            r5c, text="Pause", command=self.toggle_pause, state="disabled", font=_font(FONT_BASE), height=CONTROL_HEIGHT
        )
        _style_button(self.btn_pause)
        self.btn_pause.pack(side="left", padx=8)
        self.btn_stop = ctk.CTkButton(
            r5c, text="Stop", command=self.stop_recording, state="disabled", font=_font(FONT_BASE), height=CONTROL_HEIGHT
        )
        _style_button(self.btn_stop)
        self.btn_stop.pack(side="left")
        ctk.CTkLabel(r5, textvariable=self._record_timer_var, font=_font(FONT_BASE)).pack(anchor="center", pady=(6, 0))

        r6 = ctk.CTkFrame(frm, fg_color="transparent", corner_radius=0)
        self.levels_frame = r6
        self._levels_pack_opts = {"fill": "x", "pady": (8, 0)}
        self._levels_visible = False

        r6a = ctk.CTkFrame(r6, fg_color="transparent", corner_radius=0)
        r6a.pack(fill="x")
        ctk.CTkLabel(r6a, text="Mic", font=_font(FONT_BASE)).pack(anchor="w")
        self.level_mic = ctk.CTkProgressBar(r6a, width=520)
        self.level_mic.set(0)
        self.level_mic.pack(fill="x", expand=True)

        r6b = ctk.CTkFrame(r6, fg_color="transparent", corner_radius=0)
        r6b.pack(fill="x", pady=(4, 0))
        ctk.CTkLabel(r6b, text="System", font=_font(FONT_BASE)).pack(anchor="w")
        self.level_sys = ctk.CTkProgressBar(r6b, width=520)
        self.level_sys.set(0)
        self.level_sys.pack(fill="x", expand=True)

        r6c = ctk.CTkFrame(r6, fg_color="transparent", corner_radius=0)
        r6c.pack(fill="x", pady=(8, 0))
        self.echo_var = tk.StringVar(value="")
        self.echo_label = ctk.CTkLabel(r6c, textvariable=self.echo_var, font=_font(FONT_BASE), text_color="#b00020")
        self.echo_label.pack(anchor="w")

        self.hint_var = tk.StringVar(value="")
        ctk.CTkLabel(frm, textvariable=self.hint_var, wraplength=820, font=_font(FONT_BASE)).pack(
            fill="x", pady=(6, 0)
        )

        self._set_levels_visible(self._is_recording_or_paused())

    def _build_transcript_tab(self, parent):
        top = ctk.CTkFrame(parent, fg_color=BG_MAIN, corner_radius=0)
        top.grid(row=2, column=0, sticky="nsew", padx=12, pady=(0, 12))

        self._transcript_controls = self._add_record_controls(top, label="", pady=(0, 8))

        txt = ctk.CTkTextbox(top, wrap="word", font=_font(FONT_BASE), fg_color=ENTRY_BG)
        _style_textbox(txt)
        txt.insert("1.0", "")
        txt.configure(state="disabled")
        _style_textbox_readonly(txt)
        txt.pack(fill="both", expand=True)
        self._transcript_text = txt

        footer = ctk.CTkFrame(top, fg_color="transparent", corner_radius=0)
        footer.pack(fill="x", pady=(10, 0))
        ctk.CTkButton(footer, text="Save", command=self._save_transcript_txt, font=_font(FONT_BASE)).pack(
            side="right", padx=(0, 8)
        )

        self._sync_record_buttons()

    def _build_settings_tab(self, parent):
        body = ctk.CTkFrame(parent, fg_color=BG_MAIN, corner_radius=0)
        body.grid(row=2, column=0, sticky="nsew", padx=12, pady=(0, 12))
        body.grid_columnconfigure(0, weight=1)
        body.grid_rowconfigure(2, weight=1)

        settings_tabs = ctk.CTkTabview(body)
        settings_tabs.grid(row=2, column=0, sticky="nsew", pady=(6, 0))
        try:
            settings_tabs.configure(
                fg_color=ENTRY_BG,
                border_width=1,
                border_color=ENTRY_BORDER,
                segmented_button_fg_color=ENTRY_BG,
                segmented_button_font=_font(size=FONT_BASE),
                segmented_button_height=42,
                segmented_button_padding=5,
            )
        except Exception:
            pass
        self._style_tabview_buttons(settings_tabs)

        # Recordings tab (3-column layout)
        rec_tab = settings_tabs.add("Recordings")
        rec_tab.grid_rowconfigure(0, weight=1)
        rec_tab.grid_columnconfigure(0, weight=1, uniform="settings_rec")
        rec_tab.grid_columnconfigure(1, weight=1, uniform="settings_rec")
        rec_tab.grid_columnconfigure(2, weight=1, uniform="settings_rec")

        rec_col1 = ctk.CTkFrame(rec_tab, fg_color="transparent", corner_radius=0)
        rec_col1.grid(row=0, column=0, sticky="nsew", padx=(0, 8), pady=(0, 8))
        r3 = ctk.CTkFrame(rec_col1, fg_color="transparent", corner_radius=0)
        r3.pack(fill="x", padx=8, pady=(8, 8))
        ctk.CTkLabel(r3, text="Sample rate", font=_font(FONT_BASE)).pack(anchor="w")
        self.sr_spin = self._make_spinbox(r3, self.sr_var, 8000, 192000, increment=1000, width=90, expand=True)
        self.sr_spin.pack(fill="x", pady=(4, 0))

        r3b = ctk.CTkFrame(rec_col1, fg_color="transparent", corner_radius=0)
        r3b.pack(fill="x", padx=8, pady=(0, 8))
        ctk.CTkLabel(r3b, text="Channels", font=_font(FONT_BASE)).pack(anchor="w")
        self.ch_spin = self._make_spinbox(r3b, self.ch_var, 1, 2, increment=1, width=60, expand=True)
        self.ch_spin.pack(fill="x", pady=(4, 0))

        rec_col2 = ctk.CTkFrame(rec_tab, fg_color="transparent", corner_radius=0)
        rec_col2.grid(row=0, column=1, sticky="nsew", padx=8, pady=(0, 8))

        r3c = ctk.CTkFrame(rec_col2, fg_color="transparent", corner_radius=0)
        r3c.pack(fill="x", padx=8, pady=(8, 8))
        ctk.CTkLabel(r3c, text="Mic gain (dB)", font=_font(FONT_BASE)).pack(anchor="w")
        self.mic_gain_spin = self._make_spinbox(r3c, self.mic_gain_var, -24, 24, increment=1, width=70, expand=True)
        self.mic_gain_spin.pack(fill="x", pady=(4, 0))

        r3d = ctk.CTkFrame(rec_col2, fg_color="transparent", corner_radius=0)
        r3d.pack(fill="x", padx=8, pady=(0, 8))
        ctk.CTkLabel(r3d, text="System gain (dB)", font=_font(FONT_BASE)).pack(anchor="w")
        self.sys_gain_spin = self._make_spinbox(r3d, self.sys_gain_var, -24, 24, increment=1, width=70, expand=True)
        self.sys_gain_spin.pack(fill="x", pady=(4, 0))

        rec_col3 = ctk.CTkFrame(rec_tab, fg_color="transparent", corner_radius=0)
        rec_col3.grid(row=0, column=2, sticky="nsew", padx=(8, 0), pady=(0, 8))

        r3e = ctk.CTkFrame(rec_col3, fg_color="transparent", corner_radius=0)
        r3e.pack(fill="x", padx=8, pady=(8, 8))
        self.auto_duck_chk = ctk.CTkCheckBox(
            r3e,
            text="Auto-duck Microphone",
            variable=self.auto_duck_var,
            font=_font(FONT_BASE),
            command=self._on_auto_duck_toggle,
        )
        self.auto_duck_chk.pack(anchor="w")

        r3g = ctk.CTkFrame(rec_col3, fg_color="transparent", corner_radius=0)
        r3g.pack(fill="x", padx=8, pady=(0, 8))
        ctk.CTkLabel(r3g, text="Auto-duck preset", font=_font(FONT_BASE)).pack(anchor="w")
        self.auto_duck_preset = ctk.CTkComboBox(
            r3g,
            values=["12 dB", "18 dB", "24 dB"],
            font=_font(FONT_BASE),
            height=CONTROL_HEIGHT,
            command=self._on_auto_duck_preset,
        )
        _style_entry(self.auto_duck_preset)
        try:
            self.auto_duck_preset.set(self._auto_duck_preset_label())
        except Exception:
            pass
        self.auto_duck_preset.pack(fill="x", pady=(4, 0))

        r3e2 = ctk.CTkFrame(rec_col3, fg_color="transparent", corner_radius=0)
        r3e2.pack(fill="x", padx=8, pady=(0, 8))
        self.aec_chk = ctk.CTkCheckBox(
            r3e2,
            text="Echo Cancellation (AEC)",
            variable=self.aec_enabled_var,
            font=_font(FONT_BASE),
            command=self._on_aec_toggle,
        )
        self.aec_chk.pack(anchor="w")

        r3h = ctk.CTkFrame(rec_col3, fg_color="transparent", corner_radius=0)
        r3h.pack(fill="x", padx=8, pady=(0, 8))
        self.auto_stop_silence_chk = ctk.CTkCheckBox(
            r3h,
            text="Auto-stop on Silence",
            variable=self.auto_stop_on_silence_var,
            font=_font(FONT_BASE),
            command=self._on_silence_autostop_toggle,
        )
        self.auto_stop_silence_chk.pack(anchor="w")

        r3h2 = ctk.CTkFrame(rec_col3, fg_color="transparent", corner_radius=0)
        r3h2.pack(fill="x", padx=8, pady=(0, 8))
        ctk.CTkLabel(r3h2, text="Silence threshold (max dBFS)", font=_font(FONT_BASE)).pack(anchor="w")
        self.silence_threshold_spin = self._make_spinbox(
            r3h2,
            self.silence_threshold_db_var,
            -120.0,
            0.0,
            increment=1.0,
            width=90,
            expand=True,
        )
        self.silence_threshold_spin.pack(fill="x", pady=(4, 0))

        r3h3 = ctk.CTkFrame(rec_col3, fg_color="transparent", corner_radius=0)
        r3h3.pack(fill="x", padx=8, pady=(0, 8))
        ctk.CTkLabel(r3h3, text="Silence duration (s)", font=_font(FONT_BASE)).pack(anchor="w")
        self.silence_duration_spin = self._make_spinbox(
            r3h3,
            self.silence_duration_seconds_var,
            1.0,
            3600.0,
            increment=1.0,
            width=90,
            expand=True,
        )
        self.silence_duration_spin.pack(fill="x", pady=(4, 0))

        # Live-Transcripts tab (3-column layout)
        live_tab = settings_tabs.add("Live-Transcripts")
        live_tab.grid_rowconfigure(0, weight=1)
        live_tab.grid_columnconfigure(0, weight=1, uniform="settings_live")
        live_tab.grid_columnconfigure(1, weight=1, uniform="settings_live")
        live_tab.grid_columnconfigure(2, weight=1, uniform="settings_live")
        self._transcription_controls = []

        live_col1 = ctk.CTkFrame(live_tab, fg_color="transparent", corner_radius=0)
        live_col1.grid(row=0, column=0, sticky="nsew", padx=(0, 8), pady=(0, 8))
        r5b = ctk.CTkFrame(live_col1, fg_color="transparent", corner_radius=0)
        r5b.pack(fill="x", padx=8, pady=(8, 8))
        ctk.CTkLabel(r5b, text="Sprache", font=_font(FONT_BASE)).pack(anchor="w")
        menu_lang = ctk.CTkComboBox(
            r5b,
            variable=self.transcription_language_var,
            values=["Auto", "Deutsch", "English", "French"],
            font=_font(FONT_BASE),
            height=CONTROL_HEIGHT,
        )
        _style_entry(menu_lang)
        menu_lang.pack(fill="x", pady=(4, 0))
        self._transcription_controls.append(menu_lang)

        r5g = ctk.CTkFrame(live_col1, fg_color="transparent", corner_radius=0)
        r5g.pack(fill="x", padx=8, pady=(0, 8))
        ctk.CTkLabel(r5g, text="Model", font=_font(FONT_BASE)).pack(anchor="w")
        menu_model = ctk.CTkComboBox(
            r5g,
            variable=self.transcription_model_size_var,
            values=["small", "medium", "large"],
            font=_font(FONT_BASE),
            height=CONTROL_HEIGHT,
        )
        _style_entry(menu_model)
        menu_model.pack(fill="x", pady=(4, 0))
        self._transcription_controls.append(menu_model)

        live_col2 = ctk.CTkFrame(live_tab, fg_color="transparent", corner_radius=0)
        live_col2.grid(row=0, column=1, sticky="nsew", padx=8, pady=(0, 8))
        r5c = ctk.CTkFrame(live_col2, fg_color="transparent", corner_radius=0)
        r5c.pack(fill="x", padx=8, pady=(8, 8))
        ctk.CTkLabel(r5c, text="Chunk (s)", font=_font(FONT_BASE)).pack(anchor="w")
        chunk_spin = self._make_spinbox(
            r5c, self.transcription_chunk_seconds_var, 0.5, 15.0, increment=0.1, width=70, expand=True
        )
        self._transcription_controls.append(chunk_spin)
        chunk_spin.pack(fill="x", pady=(4, 0))

        r5d = ctk.CTkFrame(live_col2, fg_color="transparent", corner_radius=0)
        r5d.pack(fill="x", padx=8, pady=(0, 8))
        ctk.CTkLabel(r5d, text="Overlap (s)", font=_font(FONT_BASE)).pack(anchor="w")
        overlap_spin = self._make_spinbox(
            r5d, self.transcription_overlap_seconds_var, 0.0, 10.0, increment=0.1, width=70, expand=True
        )
        self._transcription_controls.append(overlap_spin)
        overlap_spin.pack(fill="x", pady=(4, 0))

        live_col3 = ctk.CTkFrame(live_tab, fg_color="transparent", corner_radius=0)
        live_col3.grid(row=0, column=2, sticky="nsew", padx=(8, 0), pady=(0, 8))
        r5e = ctk.CTkFrame(live_col3, fg_color="transparent", corner_radius=0)
        r5e.pack(fill="x", padx=8, pady=(8, 8))
        ctk.CTkLabel(r5e, text="Beam", font=_font(FONT_BASE)).pack(anchor="w")
        beam_spin = self._make_spinbox(
            r5e, self.transcription_beam_size_var, 1, 10, increment=1, width=60, expand=True
        )
        self._transcription_controls.append(beam_spin)
        beam_spin.pack(fill="x", pady=(4, 0))

        r5f = ctk.CTkFrame(live_col3, fg_color="transparent", corner_radius=0)
        r5f.pack(fill="x", padx=8, pady=(0, 8))
        vad_chk = ctk.CTkCheckBox(
            r5f,
            text="Voice Activity Detection",
            variable=self.transcription_vad_filter_var,
            font=_font(FONT_BASE),
        )
        vad_chk.pack(fill="x", pady=(0, 0))
        self._transcription_controls.append(vad_chk)

        # General tab (single-column layout)
        general_tab = settings_tabs.add("General")
        general_tab.grid_rowconfigure(0, weight=1)
        general_tab.grid_columnconfigure(0, weight=1)

        sec3 = ctk.CTkFrame(general_tab, fg_color="transparent", corner_radius=0)
        sec3.grid(row=0, column=0, sticky="new", pady=(0, 8))

        r7 = ctk.CTkFrame(sec3, fg_color="transparent", corner_radius=0)
        r7.pack(fill="x", padx=8, pady=(8, 8))
        ctk.CTkLabel(r7, text="Anzeige", font=_font(FONT_BASE)).pack(anchor="w")
        self.appearance_menu = ctk.CTkComboBox(
            r7,
            values=["System", "Light", "Dark"],
            variable=self.appearance_mode,
            command=self._on_appearance_change,
            font=_font(FONT_BASE),
            height=CONTROL_HEIGHT,
        )
        _style_entry(self.appearance_menu)
        self.appearance_menu.pack(fill="x", pady=(4, 0))

        r7b = ctk.CTkFrame(sec3, fg_color="transparent", corner_radius=0)
        r7b.pack(fill="x", padx=8, pady=(0, 8))
        ctk.CTkLabel(r7b, text="Aufnahmeordner", font=_font(FONT_BASE)).pack(anchor="w")
        entry_root = ctk.CTkEntry(
            r7b, textvariable=self.recordings_root_var, height=CONTROL_HEIGHT, font=_font(FONT_BASE)
        )
        _style_entry(entry_root)
        entry_root.pack(fill="x", pady=(4, 0))

        r7c = ctk.CTkFrame(sec3, fg_color="transparent", corner_radius=0)
        r7c.pack(fill="x", padx=8, pady=(0, 8))
        btn_root = ctk.CTkButton(
            r7c, text="Auswählen…", command=self._browse_recordings_root, font=_font(FONT_BASE), height=CONTROL_HEIGHT
        )
        _style_button(btn_root)
        btn_root.pack(side="right")

        r7d = ctk.CTkFrame(sec3, fg_color="transparent", corner_radius=0)
        r7d.pack(fill="x", padx=8, pady=(0, 8))
        auto_small_chk = ctk.CTkCheckBox(
            r7d,
            text="Kleine Anzeige während Recordings einschalten",
            variable=self.auto_small_on_recording_var,
            font=_font(FONT_BASE),
        )
        auto_small_chk.pack(anchor="w")

        r7e = ctk.CTkFrame(sec3, fg_color="transparent", corner_radius=0)
        r7e.pack(fill="x", padx=8, pady=(0, 8))
        ctk.CTkLabel(r7e, text="System Audio Backend", font=_font(FONT_BASE)).pack(anchor="w")
        backend_menu = ctk.CTkComboBox(
            r7e,
            variable=self.system_audio_backend_var,
            values=list(SYSTEM_AUDIO_BACKEND_LABELS.values()),
            font=_font(FONT_BASE),
            height=CONTROL_HEIGHT,
            command=lambda _v: self.on_mode_change(),
        )
        _style_entry(backend_menu)
        backend_menu.pack(fill="x", pady=(4, 0))

        btn_label = "Aktualisieren" if self._settings_loaded else "Speichern"
        self._btn_save_settings = ctk.CTkButton(
            body, text=btn_label, font=_font(FONT_BASE), height=CONTROL_HEIGHT, command=self._save_settings
        )
        _style_button(self._btn_save_settings)
        self._btn_save_settings.grid(row=3, column=0, sticky="e", pady=(6, 0))

        try:
            settings_tabs.set("Recordings")
        except Exception:
            pass
        self._on_silence_autostop_toggle()

        self._set_transcription_settings_enabled(
            bool(self.live_transcription_var.get()) and (bool(self.rec_mic_var.get()) or bool(self.rec_sys_var.get()))
        )

    def _style_tabview_buttons(self, tabs):
        try:
            sb = getattr(tabs, "_segmented_button", None)
            if sb is None:
                return
            buttons = getattr(sb, "_buttons_dict", {}) or {}
            for btn in buttons.values():
                try:
                    btn.configure(border_spacing=5, font=_font(size=FONT_BASE))
                except Exception:
                    pass
        except Exception:
            pass

    def _add_record_controls(self, parent, label: str = "Recorder", pady=(10, 0)):
        row = ctk.CTkFrame(parent, fg_color="transparent", corner_radius=0)
        row.pack(fill="x", pady=pady)
        ctk.CTkLabel(row, text=label, font=_font(FONT_BASE)).pack(anchor="w")

        rowc = ctk.CTkFrame(row, fg_color="transparent", corner_radius=0)
        rowc.pack(anchor="center")

        btn_record = ctk.CTkButton(
            rowc, text="Record", command=self.start_recording, font=_font(FONT_BASE), height=CONTROL_HEIGHT
        )
        _style_button(btn_record)
        btn_record.pack(side="left")

        btn_pause = ctk.CTkButton(
            rowc, text="Pause", command=self.toggle_pause, font=_font(FONT_BASE), height=CONTROL_HEIGHT
        )
        _style_button(btn_pause)
        btn_pause.pack(side="left", padx=8)

        btn_stop = ctk.CTkButton(
            rowc, text="Stop", command=self.stop_recording, font=_font(FONT_BASE), height=CONTROL_HEIGHT
        )
        _style_button(btn_stop)
        btn_stop.pack(side="left")
        ctk.CTkLabel(row, textvariable=self._record_timer_var, font=_font(FONT_BASE)).pack(anchor="center", pady=(6, 0))

        return {"record": btn_record, "pause": btn_pause, "stop": btn_stop}

    def _sync_record_buttons(self):
        ctrls = getattr(self, "_transcript_controls", None)
        targets = []
        if ctrls:
            targets.append(ctrls)
        compact_ctrls = getattr(self, "_compact_controls", None)
        if compact_ctrls:
            targets.append(compact_ctrls)
        if not targets:
            return
        for t in targets:
            try:
                t["record"].configure(state=self.btn_record.cget("state"))
                t["stop"].configure(state=self.btn_stop.cget("state"))
                t["pause"].configure(state=self.btn_pause.cget("state"), text=self.btn_pause.cget("text"))
            except Exception:
                pass

    def _build_log_panel(self):
        self.status_var = tk.StringVar(value="Ready.")

        self.log_root.grid_rowconfigure(1, weight=1)
        self.log_root.grid_rowconfigure(2, weight=0)
        self.log_root.grid_columnconfigure(0, weight=1)

        log_header = ctk.CTkFrame(self.log_root, corner_radius=12, fg_color=BG_MAIN)
        log_header.grid(row=0, column=0, sticky="ew", pady=PAD)
        log_header.grid_columnconfigure(1, weight=1)

        self.status_label = ctk.CTkLabel(
            log_header,
            textvariable=self.status_var,
            text_color="#666666",
            font=_font(size=FONT_BASE),
            anchor="w",
        )
        self.status_label.grid(row=0, column=0, sticky="w", padx=PAD, pady=PAD)

        self.progress = ctk.CTkProgressBar(log_header)
        self.progress.set(0)
        self.progress.grid(row=0, column=1, sticky="ew", padx=PAD, pady=PAD)

        card_log = ctk.CTkFrame(self.log_root, corner_radius=16, fg_color=BG_MAIN)
        card_log.grid(row=1, column=0, sticky="nsew")
        card_log.grid_rowconfigure(1, weight=1)
        card_log.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            card_log,
            text="Log",
            font=_font(size=FONT_BASE),
            anchor="w",
        ).grid(row=0, column=0, sticky="w", padx=PAD, pady=PAD)

        self.log = ctk.CTkTextbox(card_log, wrap="word")
        _style_textbox(self.log)
        self.log.grid(row=1, column=0, sticky="nsew", padx=PAD, pady=PAD)
        self.log.configure(state="disabled")
        _style_textbox_readonly(self.log)

        bottom_bar = ctk.CTkFrame(self.log_root, corner_radius=0, fg_color=BG_MAIN)
        bottom_bar.grid(row=2, column=0, sticky="ew", padx=PAD, pady=PAD)
        bottom_bar.grid_columnconfigure(0, weight=1)
        bottom_bar.grid_columnconfigure(1, weight=0)
        bottom_bar.grid_columnconfigure(2, weight=0)
        bottom_bar.grid_columnconfigure(3, weight=0)
        self.btn_copy_log = ctk.CTkButton(
            bottom_bar,
            text="Kopieren",
            font=_font(size=FONT_BASE),
            anchor="w",
            width=94,
            command=self.copy_log,
        )
        _style_button(self.btn_copy_log)
        self.btn_copy_log.grid(row=0, column=1, sticky="e", padx=(0, PAD))
        self.btn_save_log = ctk.CTkButton(
            bottom_bar,
            text="Speichern",
            font=_font(size=FONT_BASE),
            anchor="w",
            width=94,
            command=self.save_log,
        )
        _style_button(self.btn_save_log)
        self.btn_save_log.grid(row=0, column=2, sticky="e", padx=(0, PAD))
        self.btn_clear = ctk.CTkButton(
            bottom_bar,
            text="Löschen",
            font=_font(size=FONT_BASE),
            anchor="w",
            width=94,
            command=self.clear_log,
        )
        _style_button(self.btn_clear)
        self.btn_clear.grid(row=0, column=3, sticky="e")

    def _select_tab(self, name: str):
        frame = self._tab_frames.get(name)
        if frame is None:
            return
        if name in getattr(self, "_tab_disabled", set()):
            return
        frame.tkraise()
        self._current_tab = name
        for tab_name, btn in self._tab_buttons.items():
            try:
                is_active = tab_name == name
                defaults = (getattr(self, "_tab_button_defaults", {}) or {}).get(tab_name, {})
                btn.configure(
                    font=_font(size=FONT_BASE),
                    fg_color=_resolve_color(TAB_SELECTED_BG) if is_active else "transparent",
                    text_color=_resolve_color(TAB_TEXT_ACTIVE) if is_active else _resolve_color(TAB_TEXT_INACTIVE),
                )
            except Exception:
                pass
        for tab_name in self._tab_buttons.keys():
            self._on_tab_hover(tab_name, False)

    def _set_tab_enabled(self, name: str, enabled: bool):
        if not hasattr(self, "_tab_disabled"):
            self._tab_disabled = set()
        btn = self._tab_buttons.get(name)
        if btn is None:
            return
        if enabled:
            self._tab_disabled.discard(name)
            try:
                btn.configure(
                    state="normal",
                    text_color=_resolve_color(TAB_TEXT_INACTIVE),
                    hover_color=_resolve_color(TAB_HOVER_BG),
                )
            except Exception:
                pass
        else:
            self._tab_disabled.add(name)
            try:
                btn.configure(
                    state="disabled",
                    text_color=_resolve_color(READONLY_BG),
                    hover_color=_resolve_color(READONLY_BG),
                )
            except Exception:
                pass

    def _on_tab_hover(self, name, is_hover):
        btn = self._tab_buttons.get(name)
        if btn is None:
            return
        if name in getattr(self, "_tab_disabled", set()):
            try:
                btn.configure(text_color=_resolve_color(READONLY_BG))
            except Exception:
                pass
            return
        is_active = name == getattr(self, "_current_tab", None)
        try:
            if is_active:
                btn.configure(
                    fg_color=_resolve_color(TAB_HOVER_BG) if is_hover else _resolve_color(TAB_SELECTED_BG),
                    text_color=_resolve_color(TAB_TEXT_INACTIVE) if is_hover else _resolve_color(TAB_TEXT_ACTIVE),
                )
            else:
                btn.configure(
                    fg_color=_resolve_color(TAB_HOVER_BG) if is_hover else "transparent",
                    text_color=_resolve_color(TAB_TEXT_INACTIVE),
                )
        except Exception:
            pass

    def _on_tab_press(self, name):
        btn = self._tab_buttons.get(name)
        if btn is None:
            return
        if name in getattr(self, "_tab_disabled", set()):
            return
        try:
            btn.configure(
                fg_color=_resolve_color(TAB_HOVER_BG),
                text_color=_resolve_color(TAB_TEXT_INACTIVE),
            )
        except Exception:
            pass

    def _on_tab_release(self, name):
        try:
            self._select_tab(name if name == getattr(self, "_current_tab", None) else self._current_tab)
        except Exception:
            pass

    def _toggle_log_panel(self):
        if self._log_collapsed:
            self._log_collapsed = False
        else:
            try:
                w = int(self.log_root.winfo_width() or 0)
                if w > 0:
                    self._log_last_width = w
            except Exception:
                pass
            self._log_collapsed = True
        self._refresh_log_toggle_label()
        self._sync_main_layout()
        if bool(getattr(self, "_compact_view", False)):
            self._apply_compact_window_size(log_visible=(not self._log_collapsed))
            self._apply_window_constraints_for_mode()

    def _toggle_compact_view(self):
        turning_on = not bool(getattr(self, "_compact_view", False))
        if turning_on:
            self._full_geometry_before_compact = self._current_window_geometry()
        self._compact_view = turning_on
        try:
            self.btn_toggle_view.configure(text=("Große Anzeige" if self._compact_view else "Kleine Ansicht"))
        except Exception:
            pass
        self._refresh_log_toggle_label()
        self._sync_main_layout()
        if self._compact_view:
            self._apply_compact_window_size(log_visible=(not self._log_collapsed))
            self._apply_window_constraints_for_mode()
        else:
            self._restore_full_window_geometry()
            self._apply_window_constraints_for_mode()

    def _pane_has(self, pane) -> bool:
        try:
            return str(pane) in set(self.main_pane.panes())
        except Exception:
            return False

    def _set_tab_buttons_visible(self, visible: bool):
        for btn in (self._tab_buttons or {}).values():
            try:
                if visible:
                    btn.grid()
                else:
                    btn.grid_remove()
            except Exception:
                pass

    def _set_compact_panel_visible(self, visible: bool):
        panel = getattr(self, "compact_panel", None)
        if panel is None:
            return
        try:
            if visible:
                panel.grid()
                self._update_compact_transcript_visibility()
            else:
                panel.grid_remove()
        except Exception:
            pass

    def _update_compact_transcript_visibility(self):
        txt = getattr(self, "_compact_transcript_text", None)
        if txt is None:
            return
        show = bool(self.live_transcription_var.get())
        try:
            if show:
                txt.grid()
            else:
                txt.grid_remove()
        except Exception:
            pass

    def _refresh_log_toggle_label(self):
        try:
            self.btn_toggle_log.configure(text=("Log anzeigen" if self._log_collapsed else "Log ausblenden"))
        except Exception:
            pass

    def _sync_main_layout(self):
        compact = bool(getattr(self, "_compact_view", False))
        log_hidden = bool(getattr(self, "_log_collapsed", True))
        self._refresh_log_toggle_label()
        self._set_tab_buttons_visible(not compact)
        self._set_compact_panel_visible(compact)

        if compact and log_hidden:
            try:
                self.grid_columnconfigure(1, weight=0)
            except Exception:
                pass
            try:
                self.main_pane.grid_remove()
            except Exception:
                pass
            return

        try:
            self.main_pane.grid(row=0, column=1, sticky="nsew", pady=0)
        except Exception:
            pass

        # Rebuild pane content deterministically to avoid stale splitbars/panes.
        # Forget panes unconditionally; tk.PanedWindow can report pane lists inconsistently across transitions.
        for pane in (self.content_root, self.log_root):
            try:
                self.main_pane.forget(pane)
            except Exception:
                pass

        if compact:
            try:
                self.grid_columnconfigure(1, weight=1)
            except Exception:
                pass
            if not log_hidden:
                try:
                    self.main_pane.add(self.log_root, minsize=240)
                except Exception:
                    pass
        else:
            try:
                self.grid_columnconfigure(1, weight=1)
            except Exception:
                pass
            try:
                self.main_pane.add(self.content_root, minsize=480, padx=1)
            except Exception:
                pass
            if not log_hidden:
                try:
                    self.main_pane.add(self.log_root, minsize=240)
                except Exception:
                    pass

        if not log_hidden:
            self._ensure_log_width()

    def _current_window_geometry(self):
        try:
            self.update_idletasks()
            return (
                int(self.winfo_width()),
                int(self.winfo_height()),
                int(self.winfo_x()),
                int(self.winfo_y()),
            )
        except Exception:
            return None

    def _set_window_geometry(self, w: int, h: int, x: int, y: int):
        try:
            self.geometry(f"{int(w)}x{int(h)}+{int(x)}+{int(y)}")
        except Exception:
            pass

    def _capture_default_window_constraints(self):
        try:
            self._normal_window_minsize = tuple(map(int, self.minsize()))
        except Exception:
            self._normal_window_minsize = (1, 1)
        try:
            self._normal_window_maxsize = tuple(map(int, self.maxsize()))
        except Exception:
            self._normal_window_maxsize = (10_000, 10_000)
        try:
            rs = self.resizable()
            if isinstance(rs, tuple) and len(rs) == 2:
                self._normal_window_resizable = (bool(rs[0]), bool(rs[1]))
        except Exception:
            self._normal_window_resizable = (True, True)

    def _force_exit_fullscreen_if_compact(self):
        if not bool(getattr(self, "_compact_view", False)):
            return
        if bool(getattr(self, "_compact_fullscreen_exit_in_progress", False)):
            return
        fullscreen_active = False
        zoomed_active = False
        try:
            fullscreen_active = bool(self.attributes("-fullscreen"))
        except Exception:
            fullscreen_active = False
        try:
            zoomed_active = (str(self.state()).lower() == "zoomed")
        except Exception:
            zoomed_active = False
        if (not fullscreen_active) and (not zoomed_active):
            return
        self._compact_fullscreen_exit_in_progress = True
        # New behavior: fullscreen request in Small View switches app back to Large View.
        try:
            self._compact_view = False
            try:
                self.btn_toggle_view.configure(text="Kleine Ansicht")
            except Exception:
                pass
            self._refresh_log_toggle_label()
            self._sync_main_layout()
            self._restore_full_window_geometry()
            self._apply_window_constraints_for_mode()
        except Exception:
            pass
        try:
            self.after(250, lambda: setattr(self, "_compact_fullscreen_exit_in_progress", False))
        except Exception:
            self._compact_fullscreen_exit_in_progress = False

    def _enforce_compact_width(self):
        if not bool(getattr(self, "_compact_view", False)):
            return
        target_w = int(getattr(self, "_compact_fixed_width", 0) or 0)
        if target_w <= 0 or bool(getattr(self, "_enforcing_compact_window", False)):
            return
        g = self._current_window_geometry()
        if g is None:
            return
        w, h, x, y = g
        if abs(int(w) - target_w) <= 1:
            return
        self._enforcing_compact_window = True
        try:
            self._set_window_geometry(target_w, h, x, y)
        finally:
            self._enforcing_compact_window = False

    def _apply_window_constraints_for_mode(self):
        compact = bool(getattr(self, "_compact_view", False))
        if compact:
            target_w = int(self._compact_target_width(log_visible=(not self._log_collapsed)))
            self._compact_fixed_width = target_w
            try:
                self.resizable(False, True)
            except Exception:
                pass
            try:
                min_h = max(320, int(self.winfo_reqheight() or 320))
                self.minsize(target_w, min_h)
            except Exception:
                pass
            try:
                max_h = 10_000
                if isinstance(self._normal_window_maxsize, tuple) and len(self._normal_window_maxsize) == 2:
                    max_h = max(int(self._normal_window_maxsize[1]), 320)
                self.maxsize(target_w, max_h)
            except Exception:
                pass
            self._force_exit_fullscreen_if_compact()
            self._enforce_compact_width()
            return

        self._compact_fixed_width = None
        try:
            rw, rh = self._normal_window_resizable if isinstance(self._normal_window_resizable, tuple) else (True, True)
            self.resizable(bool(rw), bool(rh))
        except Exception:
            pass
        try:
            mw, mh = self._normal_window_minsize if isinstance(self._normal_window_minsize, tuple) else (1, 1)
            self.minsize(int(mw), int(mh))
        except Exception:
            pass
        try:
            xw, xh = self._normal_window_maxsize if isinstance(self._normal_window_maxsize, tuple) else (10_000, 10_000)
            self.maxsize(int(xw), int(xh))
        except Exception:
            pass

    def _on_window_configure(self, _event=None):
        if not bool(getattr(self, "_compact_view", False)):
            return
        self._force_exit_fullscreen_if_compact()
        self._enforce_compact_width()

    def _compact_target_width(self, log_visible: bool) -> int:
        sidebar_w = int(getattr(self, "_sidebar_width", 230) or 230)
        if not log_visible:
            return sidebar_w
        log_w = int(self._log_last_width if self._log_last_width else 400)
        return sidebar_w + log_w + 16

    def _apply_compact_window_size(self, log_visible: bool):
        g = self._current_window_geometry()
        if g is None:
            return
        _w, h, x, y = g
        target_w = self._compact_target_width(log_visible=bool(log_visible))
        self._compact_fixed_width = int(target_w)
        self._set_window_geometry(target_w, h, x, y)

    def _restore_full_window_geometry(self):
        g = getattr(self, "_full_geometry_before_compact", None)
        if g is not None:
            w, h, x, y = g
            self._set_window_geometry(w, h, x, y)
        self._full_geometry_before_compact = None

    def _ensure_log_width(self):
        if self._log_collapsed:
            return
        # In compact mode (log-only pane), let window resizing directly resize log.
        try:
            if bool(getattr(self, "_compact_view", False)) and self._pane_has(self.log_root) and (not self._pane_has(self.content_root)):
                w = int(self.log_root.winfo_width() or 0)
                if w > 0:
                    self._log_last_width = w
                return
        except Exception:
            pass
        try:
            w = self._log_last_width if self._log_last_width else 400
            self.main_pane.paneconfigure(self.log_root, minsize=240, width=w)
        except Exception:
            pass

    def _log_line(self, text: str):
        try:
            self.log.configure(state="normal")
            self.log.insert("end", text.strip() + "\n")
            self.log.see("end")
            self.log.configure(state="disabled")
        except Exception:
            pass

    def copy_log(self):
        try:
            text = self.log.get("1.0", "end-1c")
            self.clipboard_clear()
            self.clipboard_append(text)
        except Exception:
            pass

    def save_log(self):
        try:
            path = filedialog.asksaveasfilename(
                title="Log speichern",
                defaultextension=".txt",
                filetypes=[("Text", "*.txt")],
            )
            if not path:
                return
            text = self.log.get("1.0", "end-1c")
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
        except Exception:
            pass

    def clear_log(self):
        try:
            self.log.configure(state="normal")
            self.log.delete("1.0", "end")
            self.log.configure(state="disabled")
        except Exception:
            pass

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
        # Systemaudio backend selection.
        current_backend = _normalize_system_audio_backend(self.system_audio_backend_var.get())
        self.system_audio_backend_var.set(
            SYSTEM_AUDIO_BACKEND_LABELS.get(current_backend, SYSTEM_AUDIO_BACKEND_LABELS[SYSTEM_AUDIO_BACKEND_SCK])
        )

        self.on_device_change()

    def _find_entry(self, label: str):
        for d in self.input_devices:
            if d["label"] == label:
                return d
        return self.input_devices[0]

    def selected_mic_id(self):
        return self._find_entry(self.mic_var.get())["id"]

    def selected_sys_id(self):
        # Systemaudio is captured by selected helper backend (no input device selection).
        return None


    # ------------------------------------------------------------------
    # UI logic
    # ------------------------------------------------------------------

    def _current_system_audio_backend(self) -> str:
        return _normalize_system_audio_backend(self.system_audio_backend_var.get())

    def _coreaudio_backend_supported_here(self) -> tuple[bool, str]:
        if platform.system() != "Darwin":
            return False, "Core Audio taps ist nur auf macOS verfügbar."
        if _bundle_root() is None:
            return (
                False,
                "Core Audio taps benötigt den Start aus der gebauten .app (dist/Transcriba Recorder.app), "
                "nicht aus dem Python-Quelllauf.",
            )
        return True, ""

    def _has_systemaudio_permission(self) -> bool:
        """Best-effort check for macOS system audio permission.

        ScreenCaptureKit backend uses CoreGraphics preflight for screen/audio permission.
        Core Audio taps permission does not currently provide a preflight API here, so
        we defer to runtime prompt/error handling in the helper.
        """

        if platform.system() != "Darwin":
            return True

        if self._current_system_audio_backend() != SYSTEM_AUDIO_BACKEND_SCK:
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

        # Live transcript checkbox only if any source is enabled
        if mic_enabled or sys_enabled:
            self._safe_set_state(self.live_transcription_chk, "normal", "live_transcription_chk")
        else:
            self._safe_set_state(self.live_transcription_chk, "disabled", "live_transcription_chk")
        # Live Transcript tab only if checkbox is on AND any source is enabled
        live_tab_enabled = bool(self.live_transcription_var.get()) and (mic_enabled or sys_enabled)
        self._set_tab_enabled("Live Transcript", live_tab_enabled)
        if (not live_tab_enabled) and getattr(self, "_current_tab", "") == "Live Transcript":
            self._select_tab("Recorder")
        self._set_transcription_settings_enabled(live_tab_enabled)

        # Enable/disable mic controls
        self.mic_cb.configure(state="normal" if mic_enabled else "disabled")
        self._spin_set_state(self.mic_gain_spin, "normal" if mic_enabled else "disabled")

        # Systemaudio: UI only (no device selection)
        # We enable/disable the dropdown + gain control to reflect the choice.
        self.sys_cb.configure(state="normal" if sys_enabled else "disabled")
        self._spin_set_state(self.sys_gain_spin, "normal" if sys_enabled else "disabled")

        # Channels behavior: if both sources selected, force 2ch mixdown mode in UI (like before)
        if mode == "both":
            self.ch_var.set(2)
            self._spin_set_state(self.ch_spin, "disabled")

        # Lock transcript close while recording
        self._update_transcript_close_state()
        if mode != "both":
            self._spin_set_state(self.ch_spin, "normal")

        # Prevent recording if nothing is selected
        if (not mic_enabled) and (not sys_enabled):
            self.btn_record.configure(state="disabled")
            try:
                self.btn_test.configure(state="disabled")
            except Exception:
                pass
            self.hint_var.set("Bitte mindestens eine Quelle auswählen (Mikrofon und/oder Systemaudio).")
        else:
            if (not self.rec.is_running) and (not self._test_running):
                self.btn_record.configure(state="normal")
                try:
                    self.btn_test.configure(state="normal")
                except Exception:
                    pass
            elif self._test_running:
                self.btn_record.configure(state="disabled")
                try:
                    self.btn_test.configure(state="normal")
                except Exception:
                    pass

        # One-time hint for macOS system audio permission (backend-specific).
        if sys_enabled and (not self._systemaudio_permission_hint_shown) and platform.system() == "Darwin":
            backend = self._current_system_audio_backend()
            needs_hint = (backend == SYSTEM_AUDIO_BACKEND_COREAUDIO) or (not self._has_systemaudio_permission())
            if needs_hint:
                self._systemaudio_permission_hint_shown = True
                try:
                    messagebox.showinfo(
                        "Systemaudio (macOS Berechtigung)",
                        system_audio_permission_hint_message(backend),
                    )
                except Exception:
                    pass

        self.on_device_change()
        self._sync_record_buttons()
        self._set_levels_visible(self._is_recording_or_paused())
        self._update_level_states()

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
            self._spin_set_limits(self.ch_spin, 1, max_allowed)

            if self.ch_var.get() > max_allowed:
                self.ch_var.set(max_allowed)
            if self.ch_var.get() < 1:
                self.ch_var.set(1)

    # ------------------------------------------------------------------
    # Folder / file helpers

    # ------------------------------------------------------------------

    def open_recordings_folder(self):
        # Prefer current session folder; fallback to selected output folder or default recordings directory
        folder = (getattr(self, "_last_out_path", "") or "").strip()
        if not folder:
            folder = (self.out_var.get() or "").strip()
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
        path = filedialog.askdirectory(
            title="Ausgabeordner wählen",
            initialdir=(self.out_var.get() or default_recordings_dir()),
        )
        if path:
            self.out_var.set(path)

    def toggle_test_mode(self):
        if self._test_running:
            self.stop_test_mode()
        else:
            self.start_test_mode()

    def start_test_mode(self):
        if self.rec.is_running or self._sys_only_running:
            return
        self._reset_silence_autostop_state()

        mic_enabled = bool(self.rec_mic_var.get())
        sys_enabled = bool(self.rec_sys_var.get())
        try:
            self._log_line(
                f"[debug] Start Test requested: mic={mic_enabled} sys={sys_enabled} backend={self._current_system_audio_backend()}"
            )
        except Exception:
            pass
        if (not mic_enabled) and (not sys_enabled):
            return
        if sys_enabled and self._current_system_audio_backend() == SYSTEM_AUDIO_BACKEND_COREAUDIO:
            ok, reason = self._coreaudio_backend_supported_here()
            if not ok:
                try:
                    self._log_line(f"[debug] Core Audio taps blocked: {reason}")
                    self.status_var.set(reason)
                    messagebox.showerror("Core Audio taps", reason)
                except Exception:
                    pass
                return

        self._level_db = -120.0
        self._sys_level_db = -120.0

        if mic_enabled:
            try:
                def _mic_cb(indata, frames, _time, status):
                    if status:
                        return
                    if not self._test_running:
                        return
                    try:
                        a = np.asarray(indata, dtype=np.float32)
                        if a.size == 0:
                            return
                        rms = float(np.sqrt(np.mean(np.square(a))))
                        self._level_db = 20.0 * np.log10(max(rms, 1e-12))
                    except Exception:
                        pass

                self._test_mic_stream = sd.InputStream(
                    samplerate=int(self.sr_var.get() or 48000),
                    channels=1,
                    dtype="float32",
                    device=self.selected_mic_id(),
                    callback=_mic_cb,
                )
                self._test_mic_stream.start()
            except Exception as e:
                self._test_mic_stream = None
                messagebox.showerror("Test error", str(e))
                return

        if sys_enabled:
            try:
                self._test_sys_helper = SystemAudioHelper(
                    wav_path=None,
                    sample_rate=int(self.sr_var.get() or 48000),
                    status_cb=self.on_status,
                    level_cb=self.on_sys_level,
                    no_write=True,
                    backend=self._current_system_audio_backend(),
                )
                self._test_sys_helper.start()
            except Exception as e:
                if self._test_mic_stream is not None:
                    try:
                        self._test_mic_stream.stop()
                        self._test_mic_stream.close()
                    except Exception:
                        pass
                    self._test_mic_stream = None
                messagebox.showerror("Systemaudio test error", str(e))
                return

        self._test_running = True
        try:
            self.btn_test.configure(text="Stop Test")
        except Exception:
            pass
        self.btn_record.configure(state="disabled")
        self.btn_pause.configure(state="disabled", text="Pause")
        self.btn_stop.configure(state="disabled")
        self.rec_mic_chk.configure(state="disabled")
        self.rec_sys_chk.configure(state="disabled")
        self._safe_set_state(self.live_transcription_chk, "disabled", "live_transcription_chk")
        self.mic_cb.configure(state="disabled")
        self.sys_cb.configure(state="disabled")
        self.status_var.set("Input-Test läuft (ohne WAV-Aufnahme).")
        self._set_levels_visible(True)
        self._update_level_states(recording_active=True)
        self._sync_record_buttons()

    def stop_test_mode(self):
        if not self._test_running:
            return
        self._reset_silence_autostop_state()

        if self._test_mic_stream is not None:
            try:
                self._test_mic_stream.stop()
                self._test_mic_stream.close()
            except Exception:
                pass
            self._test_mic_stream = None

        if self._test_sys_helper is not None:
            try:
                self._test_sys_helper.stop()
            except Exception:
                pass
            self._test_sys_helper = None

        self._test_running = False
        self._level_db = -120.0
        self._sys_level_db = -120.0
        try:
            self.btn_test.configure(text="Start Test")
        except Exception:
            pass
        self._safe_set_state(self.rec_mic_chk, "normal", "rec_mic_chk")
        self._safe_set_state(self.rec_sys_chk, "normal", "rec_sys_chk")
        self.status_var.set("Input-Test beendet.")
        self.on_mode_change()
        self._set_levels_visible(False)
        self._update_level_states(recording_active=False)
        self._sync_record_buttons()

    # ------------------------------------------------------------------
    # Recording
    # ------------------------------------------------------------------

    def start_recording(self):
        if self._test_running:
            return
        self._reset_silence_autostop_state()
        # Allow recording if either mic and/or system is selected.
        mic_enabled = bool(self.rec_mic_var.get())
        sys_enabled = bool(self.rec_sys_var.get())
        try:
            self._log_line(
                f"[debug] Record requested: mic={mic_enabled} sys={sys_enabled} backend={self._current_system_audio_backend()}"
            )
        except Exception:
            pass
        if (not mic_enabled) and (not sys_enabled):
            return
        if sys_enabled and self._current_system_audio_backend() == SYSTEM_AUDIO_BACKEND_COREAUDIO:
            ok, reason = self._coreaudio_backend_supported_here()
            if not ok:
                try:
                    self._log_line(f"[debug] Core Audio taps blocked: {reason}")
                    self.status_var.set(reason)
                    messagebox.showerror("Core Audio taps", reason)
                except Exception:
                    pass
                return

        # Prevent double-start: mic recorder or system-only helper already running
        if self.rec.is_running or self._sys_only_running:
            return

        # The UI lets the user choose a session folder for the next recording.
        # We write:
        #   - mic.wav
        #   - system.wav
        base_out = (self.out_var.get() or "").strip()
        if not base_out or base_out.lower().endswith(".wav"):
            base_out = default_out_path()
            self.out_var.set(base_out)

        session_dir = base_out
        os.makedirs(session_dir, exist_ok=True)

        mic_out_path = os.path.join(session_dir, "mic.wav")
        sys_out_path = os.path.join(session_dir, "system.wav")

        # Remember what the user chose for this run
        self._last_out_path = session_dir
        self._last_mic_enabled = mic_enabled
        self._last_sys_enabled = sys_enabled

        # Build config from UI (TscribaRecorder still handles mic capture).
        try:
            self._mic_gain_db_base = float(self.mic_gain_var.get() or 0.0)
        except Exception:
            self._mic_gain_db_base = 0.0
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

        # Prepare AEC (mic recording only, uses system audio as reference)
        if bool(self.aec_enabled_var.get()) and mic_enabled and sys_enabled:
            try:
                self._enable_aec(int(self.sr_var.get() or 48000))
            except Exception:
                pass

        # Start Systemaudio helper first (so permission errors show early).
        if sys_enabled:
            try:
                self.sys_helper = SystemAudioHelper(
                    sys_out_path,
                    sample_rate=int(self.sr_var.get() or 48000),
                    status_cb=self.on_status,
                    level_cb=self.on_sys_level,
                    backend=self._current_system_audio_backend(),
                )
                self.sys_helper.start()
                self._set_sys_audio_tap()
            except Exception as e:
                self.sys_helper = None
                self._disable_aec()
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
                self._sync_record_buttons()
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
        self._record_elapsed_total = 0.0
        self._record_started_at = time.monotonic()
        self._silence_grace_until = time.monotonic() + 2.0
        self._record_timer_var.set("00:00:00")
        self.btn_record.configure(state="disabled")
        # Pause only supported for mic (for now)
        if mic_enabled:
            self.btn_pause.configure(state="normal", text="Pause")
        else:
            self.btn_pause.configure(state="disabled", text="Pause")
        self.btn_stop.configure(state="normal")
        try:
            self.btn_test.configure(state="disabled")
        except Exception:
            pass
        self.rec_mic_chk.configure(state="disabled")
        self.rec_sys_chk.configure(state="disabled")
        self._safe_set_state(self.live_transcription_chk, "disabled", "live_transcription_chk")
        self.mic_cb.configure(state="disabled")
        self._spin_set_state(self.ch_spin, "disabled")
        self._sync_record_buttons()
        self._set_levels_visible(True)
        self._update_level_states(recording_active=True)
        self._apply_mic_gain_db(self._mic_gain_db_base)
        if bool(self.auto_small_on_recording_var.get()) and (not bool(getattr(self, "_compact_view", False))):
            self._full_geometry_before_compact = self._current_window_geometry()
            self._compact_view = True
            try:
                self.btn_toggle_view.configure(text="Große Anzeige")
            except Exception:
                pass
            self._sync_main_layout()
            self._apply_compact_window_size(log_visible=(not self._log_collapsed))
            self._apply_window_constraints_for_mode()

    def toggle_pause(self):
        # Pause/Resume is currently only implemented for mic recording.
        if not self.rec.is_running:
            return
        if self.rec.is_paused:
            self.rec.resume()
            self._record_started_at = time.monotonic()
            self._reset_silence_autostop_state()
            self._silence_grace_until = time.monotonic() + 1.0
            self.btn_pause.configure(text="Pause")
        else:
            self.rec.pause()
            if self._record_started_at is not None:
                self._record_elapsed_total += max(0.0, time.monotonic() - self._record_started_at)
            self._record_started_at = None
            self._reset_silence_autostop_state()
            self.btn_pause.configure(text="Resume")
        self._sync_record_buttons()
        self._set_levels_visible(self._is_recording_or_paused())
        self._update_level_states()

    def stop_recording(self):
        if self._test_running:
            return
        if (not self.rec.is_running) and (not self._sys_only_running) and (self.sys_helper is None or not self.sys_helper.is_running):
            return
        self._reset_silence_autostop_state()

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
        self._disable_aec()

        self._sys_only_running = False

        self._safe_set_state(self.btn_record, "normal", "btn_record")
        try:
            self.btn_pause.configure(state="disabled", text="Pause")
        except Exception as e:
            self._safe_set_state(self.btn_pause, "disabled", "btn_pause")
            try:
                self.btn_pause.configure(text="Pause")
            except Exception:
                pass
        self._safe_set_state(self.btn_stop, "disabled", "btn_stop")
        self._safe_set_state(self.rec_mic_chk, "normal", "rec_mic_chk")
        self._safe_set_state(self.rec_sys_chk, "normal", "rec_sys_chk")
        if self._record_started_at is not None:
            self._record_elapsed_total += max(0.0, time.monotonic() - self._record_started_at)
        self._record_started_at = None
        self._update_record_timer()
        try:
            if self.tray is not None:
                self.tray.set_title("Transcriba Recorder")
                self.tray.set_recording_effect(False, 0)
            self._last_tray_timer_text = None
            self._last_tray_recording_effect = None
        except Exception:
            pass
        self.on_mode_change()
        self._sync_record_buttons()
        self._set_levels_visible(False)
        self._update_level_states(recording_active=False)
        self._echo_active = False
        try:
            self.echo_var.set("")
        except Exception:
            pass
        self._apply_mic_gain_db(self._mic_gain_db_base)

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

        # Pre-fill next recording session folder
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
            try:
                self._log_line(text)
            except Exception:
                pass

            # Provide a helpful, explicit message if macOS denied system-audio permission.
            if (
                (not self._systemaudio_permission_denied_shown)
                and platform.system() == "Darwin"
                and (
                    ("SCStreamErrorDomain" in text)
                    or ("TCC" in text)
                    or ("permission" in text.lower())
                    or ("not permitted" in text.lower())
                )
                and (
                    ("-3801" in text)
                    or ("abgelehnt" in text.lower())
                    or ("denied" in text.lower())
                    or ("not permitted" in text.lower())
                )
            ):
                self._systemaudio_permission_denied_shown = True
                try:
                    messagebox.showerror(
                        "Systemaudio: Zugriff verweigert",
                        system_audio_permission_denied_message(self._current_system_audio_backend()),
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
        try:
            self.level_mic_compact.set(mic_val / 100.0)
        except Exception:
            pass
        try:
            self.level_sys_compact.set(sys_val / 100.0)
        except Exception:
            pass

        self._update_record_timer()
        self._update_echo_state()
        self._evaluate_silence_autostop()

        self.after(50, self._tick_ui)

    def _update_record_timer(self):
        elapsed = float(self._record_elapsed_total)
        if self._record_started_at is not None:
            elapsed += max(0.0, time.monotonic() - self._record_started_at)
        sec = int(elapsed)
        h = sec // 3600
        m = (sec % 3600) // 60
        s = sec % 60
        timer_text = f"{h:02d}:{m:02d}:{s:02d}"
        self._record_timer_var.set(timer_text)

        # Update tray hover text while recording/paused (throttled to 1s by value change).
        try:
            if self._is_recording_or_paused():
                if timer_text != self._last_tray_timer_text:
                    if self.tray is not None:
                        self.tray.set_title(f"Transcriba Recorder {timer_text}")
                    self._last_tray_timer_text = timer_text
                phase = sec % 2
                effect_key = (True, phase)
                if effect_key != self._last_tray_recording_effect:
                    if self.tray is not None:
                        self.tray.set_recording_effect(True, phase)
                    self._last_tray_recording_effect = effect_key
            else:
                if self._last_tray_timer_text is not None:
                    if self.tray is not None:
                        self.tray.set_title("Transcriba Recorder")
                    self._last_tray_timer_text = None
                if self._last_tray_recording_effect is not None:
                    if self.tray is not None:
                        self.tray.set_recording_effect(False, 0)
                    self._last_tray_recording_effect = None
        except Exception:
            pass

    def _update_sys_level_from_tap(self, audio: np.ndarray, sr: int, ch: int):
        try:
            if audio is None or audio.size == 0:
                return
            if audio.ndim > 1:
                mono = np.mean(audio, axis=1)
            else:
                mono = audio
            rms = float(np.sqrt(np.mean(np.square(mono))))
            db = 20.0 * np.log10(max(rms, 1e-9))
            self._sys_level_db = db
        except Exception:
            pass

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
        try:
            if self._test_running:
                self.stop_test_mode()
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
        mic_enabled = bool(self.rec_mic_var.get())
        sys_enabled = bool(self.rec_sys_var.get())
        live_tab_enabled = bool(self.live_transcription_var.get()) and (mic_enabled or sys_enabled)
        self._set_tab_enabled("Live Transcript", live_tab_enabled)
        if (not live_tab_enabled) and getattr(self, "_current_tab", "") == "Live Transcript":
            self._select_tab("Recorder")
        self._set_transcription_settings_enabled(live_tab_enabled)
        self._update_compact_transcript_visibility()
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
            # also print a one-time notice into the app log (only for Auto)
            try:
                ui_sel = (self.transcription_language_var.get() or "").strip().lower()
                if not ui_sel.startswith("de") and not ui_sel.startswith("en"):
                    if prob is None:
                        self._log_line(f"[LANG] erkannt: {det_lang}")
                    else:
                        self._log_line(f"[LANG] erkannt: {det_lang} ({float(prob)*100:.0f}%)")
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
                pass
            else:
                try:
                    self._transcript_text.configure(state="normal")
                    self._transcript_text.insert("end", text_line)
                    self._transcript_text.see("end")
                    self._transcript_text.configure(state="disabled")
                except Exception:
                    pass
            compact_txt = getattr(self, "_compact_transcript_text", None)
            if compact_txt is not None:
                try:
                    compact_txt.configure(state="normal")
                    compact_txt.insert("end", text_line)
                    compact_txt.see("end")
                    compact_txt.configure(state="disabled")
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

        # Ensure transcript tab is visible (optional, but helpful)
        try:
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
            # Log status messages instead of transcript
            try:
                self._log_line(f"[STATUS] {msg}")
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
            text_cb=lambda t: self._append_transcript(f"[Microphone] {t}"),
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

        # Ensure transcript tab is visible (optional, but helpful)
        try:
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
                self._log_line(f"[STATUS] {msg}")
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
            text_cb=lambda t: self._append_transcript(f"[Speaker] {t}"),
            language_detected_cb=self._on_detected_language,
            model_size=model_size,
            external_audio=True,
        )

        # Feed system audio from ScreenCaptureKit helper into the transcriber
        try:
            self._sys_tap_transcriber = lambda a, _sr, _ch: self._sys_transcriber.push_audio(a)
            self._set_sys_audio_tap()
        except Exception:
            pass

        try:
            self._sys_transcriber.start()
        except Exception:
            self._sys_transcriber = None
            self._sys_tap_transcriber = None
            try:
                self._set_sys_audio_tap()
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
            self._sys_tap_transcriber = None
            try:
                self._set_sys_audio_tap()
            except Exception:
                pass
            self._sys_transcriber = None
        self._update_transcript_status()


    def open_transcript_window(self):
        """Open (or focus) the transcript tab."""
        try:
            self._select_tab("Live Transcript")
        except Exception:
            pass




    def _save_transcript_txt(self):
        """Save current transcript text as a timestamped TXT next to the WAV session folder."""
        try:
            txtw = self._transcript_text
            if txtw is None:
                return

            # Determine target folder: prefer current session folder; fallback to selected output folder.
            folder = (getattr(self, "_last_out_path", None) or "").strip()
            if not folder:
                folder = (self.out_var.get() or "").strip()
            if not folder:
                folder = default_recordings_dir()
            os.makedirs(folder, exist_ok=True)

            # Read current transcript content
            content = ""
            try:
                # CTkTextbox doesn't support configure(state=...)
                inner = getattr(txtw, "_textbox", None)
                if inner is not None:
                    prev_state = str(inner.cget("state"))
                    try:
                        if prev_state != "normal":
                            inner.configure(state="normal")
                        content = inner.get("1.0", "end-1c")
                    finally:
                        if prev_state != "normal":
                            inner.configure(state=prev_state)
                else:
                    content = txtw.get("1.0", "end-1c")
            except Exception:
                try:
                    content = txtw.get("1.0", "end-1c")
                except Exception:
                    content = ""

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
            if getattr(self.rec, "is_paused", False):
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
        locked = self._is_recording_or_paused()
        try:
            if self._transcript_close_btn is not None:
                self._transcript_close_btn.configure(state=("disabled" if locked else "normal"))
        except Exception:
            pass

    def _on_appearance_change(self, mode: str):
        try:
            ctk.set_appearance_mode(mode)
        except Exception:
            return
        try:
            self.after(0, lambda: self.main_pane.configure(bg=_resolve_color(BG_MAIN)))
        except Exception:
            pass
        try:
            for name, btn in (self._tab_buttons or {}).items():
                if name in getattr(self, "_tab_disabled", set()):
                    btn.configure(hover_color=_resolve_color(READONLY_BG))
                else:
                    btn.configure(hover_color=_resolve_color(TAB_HOVER_BG))
        except Exception:
            pass
        try:
            if getattr(self, "_current_tab", None):
                self._select_tab(self._current_tab)
        except Exception:
            pass

    def _set_levels_visible(self, visible: bool):
        if getattr(self, "levels_frame", None) is None:
            return
        if not hasattr(self, "_levels_visible"):
            self._levels_visible = False
        try:
            if visible and not self._levels_visible:
                self.levels_frame.pack(**self._levels_pack_opts)
                self._levels_visible = True
            elif (not visible) and self._levels_visible:
                self.levels_frame.pack_forget()
                self._levels_visible = False
        except Exception:
            pass

    def _set_transcription_settings_enabled(self, enabled: bool):
        state = "normal" if enabled else "disabled"
        for w in getattr(self, "_transcription_controls", []) or []:
            try:
                if hasattr(w, "_spin_entry"):
                    self._spin_set_state(w, state)
                else:
                    w.configure(state=state)
            except Exception:
                pass

    def _update_level_states(self, recording_active: bool | None = None):
        if recording_active is None:
            recording_active = self._is_recording_or_paused()
        if not recording_active:
            return
        mic_enabled = bool(self.rec_mic_var.get())
        sys_enabled = bool(self.rec_sys_var.get())
        try:
            self.level_mic.configure(state=("normal" if mic_enabled else "disabled"))
        except Exception:
            pass
        try:
            self.level_sys.configure(state=("normal" if sys_enabled else "disabled"))
        except Exception:
            pass
        if not mic_enabled:
            try:
                self.level_mic.set(0)
            except Exception:
                pass
        if not sys_enabled:
            try:
                self.level_sys.set(0)
            except Exception:
                pass

    def _apply_mic_gain_db(self, db: float):
        try:
            if hasattr(self.rec, "set_mic_gain_db"):
                self.rec.set_mic_gain_db(db)
        except Exception:
            pass

    def _set_sys_audio_tap(self):
        if self.sys_helper is None:
            return
        callbacks = []
        if self._on_sys_audio_tap is not None:
            callbacks.append(self._on_sys_audio_tap)
        if self._aec is not None:
            callbacks.append(self._aec.feed_reference)
        if self._sys_tap_transcriber is not None:
            callbacks.append(self._sys_tap_transcriber)
        if not callbacks:
            self.sys_helper.set_audio_tap(None)
            return

        def _tap(a, sr, ch):
            for cb in callbacks:
                try:
                    cb(a, sr, ch)
                except Exception:
                    pass

        self.sys_helper.set_audio_tap(_tap)

    def _enable_aec(self, sample_rate: int):
        if not self.aec_enabled_var.get():
            return
        if int(sample_rate) not in (8000, 16000, 32000, 48000):
            try:
                self._log_line(f"AEC disabled: unsupported sample rate {sample_rate} Hz.")
            except Exception:
                pass
            return
        if AudioProcessing is None:
            try:
                self._log_line("AEC unavailable: webrtc-audio-processing not installed.")
            except Exception:
                pass
            return
        self._aec = _EchoCanceller(sample_rate)
        if not self._aec.available():
            self._aec = None
            try:
                self._log_line("AEC unavailable: init failed.")
            except Exception:
                pass
            return
        try:
            self.rec.set_mic_processor(lambda chunk, sr: self._aec.process_mic(chunk, sr))
        except Exception:
            pass
        self._set_sys_audio_tap()
        try:
            self._log_line("AEC enabled.")
        except Exception:
            pass

    def _disable_aec(self):
        self._aec = None
        try:
            self.rec.set_mic_processor(None)
        except Exception:
            pass
        try:
            self._set_sys_audio_tap()
        except Exception:
            pass

    def _on_auto_duck_toggle(self):
        if not self.auto_duck_var.get():
            self._apply_mic_gain_db(self._mic_gain_db_base)

    def _on_auto_duck_preset(self, value: str):
        try:
            v = str(value).strip().split()[0]
            strength = float(v)
        except Exception:
            return
        try:
            self.auto_duck_strength_var.set(strength)
        except Exception:
            pass

    def _auto_duck_preset_label(self) -> str:
        try:
            s = float(self.auto_duck_strength_var.get() or 18.0)
        except Exception:
            s = 18.0
        if s <= 15.0:
            return "12 dB"
        if s <= 21.0:
            return "18 dB"
        return "24 dB"

    def _on_aec_toggle(self):
        if not self.aec_enabled_var.get():
            self._disable_aec()
        else:
            if self._is_recording_or_paused() and bool(self.rec_sys_var.get()) and bool(self.rec_mic_var.get()):
                try:
                    self._enable_aec(int(self.sr_var.get() or 48000))
                except Exception:
                    pass

    def _on_silence_autostop_toggle(self):
        enabled = bool(self.auto_stop_on_silence_var.get())
        try:
            self._spin_set_state(self.silence_threshold_spin, "normal" if enabled else "disabled")
            self._spin_set_state(self.silence_duration_spin, "normal" if enabled else "disabled")
        except Exception:
            pass
        if not enabled:
            self._reset_silence_autostop_state()

    def _reset_silence_autostop_state(self):
        self._silence_started_at = None
        self._silence_autostop_triggered = False
        self._silence_grace_until = 0.0

    def _evaluate_silence_autostop(self):
        if self._test_running:
            return
        if not self._is_recording_or_paused():
            self._reset_silence_autostop_state()
            return
        if bool(getattr(self.rec, "is_paused", False)):
            self._reset_silence_autostop_state()
            return
        if not bool(self.auto_stop_on_silence_var.get()):
            self._reset_silence_autostop_state()
            return
        if self._silence_autostop_triggered:
            return

        now = time.monotonic()
        if now < float(getattr(self, "_silence_grace_until", 0.0) or 0.0):
            return

        active_levels = []
        if bool(self.rec_mic_var.get()):
            active_levels.append(float(self._level_db))
        if bool(self.rec_sys_var.get()):
            active_levels.append(float(self._sys_level_db))
        if not active_levels:
            self._reset_silence_autostop_state()
            return

        try:
            threshold_db = float(self.silence_threshold_db_var.get() or -55.0)
        except Exception:
            threshold_db = -55.0
        try:
            silence_duration = float(self.silence_duration_seconds_var.get() or 8.0)
        except Exception:
            silence_duration = 8.0
        threshold_db = max(-120.0, min(0.0, threshold_db))
        silence_duration = max(1.0, min(3600.0, silence_duration))

        all_silent = all(level <= threshold_db for level in active_levels)
        if not all_silent:
            self._silence_started_at = None
            return

        if self._silence_started_at is None:
            self._silence_started_at = now
            return
        if (now - float(self._silence_started_at)) < silence_duration:
            return

        self._silence_autostop_triggered = True
        try:
            self._log_line(
                f"Auto-stop triggered: silence for {silence_duration:.1f}s at <= {threshold_db:.1f} dBFS on active inputs."
            )
            self.status_var.set("Auto-stop: keine Audiosignale erkannt.")
        except Exception:
            pass
        try:
            self.stop_recording()
        except Exception:
            pass

    def _update_echo_state(self):
        if not self._is_recording_or_paused():
            return
        if not bool(self.rec_mic_var.get()):
            return
        if not bool(self.rec_sys_var.get()):
            return
        sys_db = float(self._sys_level_db)
        mic_db = float(self._level_db)
        # Simple heuristic: system audio is loud and mic is close in level -> likely echo bleed
        echo_detected = (sys_db > -45.0) and (mic_db > (sys_db - 12.0)) and (mic_db > -55.0)
        if echo_detected and not self._echo_active:
            self._echo_active = True
            try:
                self.echo_var.set("Echo erkannt – bitte Kopfhörer verwenden oder Systemaudio leiser stellen.")
            except Exception:
                pass
            try:
                self._log_line("Echo erkannt: Mikrofon nimmt Systemaudio auf.")
            except Exception:
                pass
            if self.auto_duck_var.get():
                try:
                    strength = float(self.auto_duck_strength_var.get() or 18.0)
                except Exception:
                    strength = 18.0
                duck_db = max(-30.0, self._mic_gain_db_base - max(0.0, strength))
                self._apply_mic_gain_db(duck_db)
        elif (not echo_detected) and self._echo_active:
            self._echo_active = False
            try:
                self.echo_var.set("")
            except Exception:
                pass
            try:
                self._log_line("Echo nicht mehr erkannt.")
            except Exception:
                pass
            if self.auto_duck_var.get():
                self._apply_mic_gain_db(self._mic_gain_db_base)

    def _close_transcript_window(self):
        """Switch back to Recorder tab (only allowed when not recording)."""
        if self._is_recording_or_paused():
            return
        try:
            self._select_tab("Recorder")
        except Exception:
            pass

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
