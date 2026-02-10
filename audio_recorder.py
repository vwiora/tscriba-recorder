import os
import queue
import threading
from dataclasses import dataclass
from typing import Callable, Optional, List, Tuple, Literal

import numpy as np
import sounddevice as sd
import soundfile as sf

Mode = Literal["mic", "system", "both"]


@dataclass
class RecorderConfig:
    samplerate: int = 48000
    # For single-source modes, channels is used as requested output channels (clamped).
    # For "both" mode, output will be forced to 2 channels (stereo mix).
    channels: int = 1
    dtype: str = "float32"
    subtype: str = "PCM_16"
    # Gain in dB applied before writing (float audio is clipped to [-1,1]).
    mic_gain_db: float = 0.0
    system_gain_db: float = 9.0
    mic_device: Optional[int] = None      # None = default input
    system_device: Optional[int] = None   # None = default input (usually not desired)
    blocksize: int = 0                    # 0=auto; for "both" we will override to a safe fixed size


def list_input_devices() -> List[Tuple[int, str, int]]:
    devs = sd.query_devices()
    items: List[Tuple[int, str, int]] = []
    for idx, d in enumerate(devs):
        max_in = int(d.get("max_input_channels", 0) or 0)
        if max_in > 0:
            items.append((idx, str(d.get("name", f"Device {idx}")), max_in))
    return items


def get_device_max_input_channels(device_id: Optional[int]) -> int:
    info = sd.query_devices(device_id, "input") if device_id is not None else sd.query_devices(None, "input")
    return int(info.get("max_input_channels", 0) or 0)


def get_device_default_samplerate(device_id: Optional[int]) -> int:
    """Return the *default* input sample-rate for the given device.

    Rationale: On macOS, especially when using Multi-Output Devices + virtual drivers
    like virtual loopback drivers, forcing a non-default samplerate can cause CoreAudio to
    reconfigure the graph and sometimes mute/disable the audible output.
    Using each device's default rate is the most stable option.
    """
    info = sd.query_devices(device_id, "input") if device_id is not None else sd.query_devices(None, "input")
    sr = info.get("default_samplerate", None)
    try:
        return int(sr)
    except Exception:
        # Safe fallback
        return 48000


def _clamp_channels_for_device(requested: int, device_id: Optional[int]) -> int:
    max_in = get_device_max_input_channels(device_id)
    if max_in <= 0:
        raise RuntimeError("Selected input device has no input channels.")
    if requested < 1:
        requested = 1
    if requested > max_in:
        requested = max_in
    return requested




def _db_to_lin(db: float) -> float:
    """Convert decibels to linear gain."""
    try:
        return float(10.0 ** (float(db) / 20.0))
    except Exception:
        return 1.0


def _apply_gain_and_clip(x: np.ndarray, gain: float) -> np.ndarray:
    """Apply gain to float audio and hard-clip to [-1, 1] to avoid PCM overflow."""
    if gain == 1.0:
        return x
    y = x * gain
    return np.clip(y, -1.0, 1.0)

def _to_stereo(x: np.ndarray) -> np.ndarray:
    """
    Convert any NxC float array to Nx2.
    - mono -> duplicate
    - >2ch -> take first 2
    """
    if x.ndim != 2:
        x = np.atleast_2d(x)
    c = x.shape[1]
    if c == 1:
        return np.repeat(x, 2, axis=1)
    if c == 2:
        return x
    return x[:, :2]


def _to_mono(x: np.ndarray) -> np.ndarray:
    """
    Convert any NxC float array to Nx1 by averaging channels.
    """
    if x.ndim != 2:
        x = np.atleast_2d(x)
    if x.shape[1] == 1:
        return x
    return np.mean(x, axis=1, keepdims=True)


class AudioRecorder:
    """
    Modes:
      - mic: record from mic_device only
      - system: record from system_device only
      - both: record mic + system simultaneously and mix to stereo
    """

    def __init__(
        self,
        on_level: Optional[Callable[[float], None]] = None,
        on_status: Optional[Callable[[str], None]] = None,
    ):
        self._on_level = on_level
        self._on_status = on_status

        self._stop_evt = threading.Event()
        self._pause_evt = threading.Event()
        self._running = False

        self._writer_thread: Optional[threading.Thread] = None
        self._sf: Optional[sf.SoundFile] = None

        # streams
        self._stream_mic: Optional[sd.InputStream] = None
        self._stream_sys: Optional[sd.InputStream] = None

        # queues
        self._q_single: "queue.Queue[np.ndarray]" = queue.Queue(maxsize=400)
        self._q_mic: "queue.Queue[np.ndarray]" = queue.Queue(maxsize=400)
        self._q_sys: "queue.Queue[np.ndarray]" = queue.Queue(maxsize=400)

        # current mode
        self._mode: Optional[Mode] = None

        # gains (linear)
        self._mic_gain = 1.0
        self._sys_gain = 1.0

        # optional tap: forward mic chunks to another consumer (e.g., live transcription)
        # signature: cb(chunk: np.ndarray, samplerate: int) -> None
        self._mic_tap = None
        # optional processor: transform mic chunks before gain/write
        # signature: cb(chunk: np.ndarray, samplerate: int) -> np.ndarray
        self._mic_processor = None

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def is_paused(self) -> bool:
        return self._pause_evt.is_set()


    def set_mic_tap(self, cb):
        """Set/clear mic tap callback.

        The callback (if set) is called from the sounddevice callback thread.
        It must be fast and non-blocking.
        """
        self._mic_tap = cb

    def set_mic_processor(self, cb):
        """Set/clear mic processing callback.

        The callback (if set) is called from the sounddevice callback thread.
        It must be fast and non-blocking.
        """
        self._mic_processor = cb

    def start(self, out_path: str, mode: Mode, cfg: RecorderConfig):
        if self._running:
            return

        out_dir = os.path.dirname(out_path) or "."
        os.makedirs(out_dir, exist_ok=True)

        self._stop_evt.clear()
        self._pause_evt.clear()
        self._mode = mode

        # Gains (applied before writing)
        self._mic_gain = _db_to_lin(getattr(cfg, 'mic_gain_db', 0.0) or 0.0)
        self._sys_gain = _db_to_lin(getattr(cfg, 'system_gain_db', 0.0) or 0.0)

        # Choose blocksize: for "both" we use fixed blocksize for better sync
        blocksize = int(cfg.blocksize) if cfg.blocksize and cfg.blocksize > 0 else 0
        if mode == "both" and blocksize == 0:
            blocksize = 1024  # safe default

        # Resolve stable samplerate(s)
        # IMPORTANT on macOS: prefer device default samplerate to avoid CoreAudio graph reconfig
        # (which can mute a Multi-Output audible device when a recording stream starts).
        if mode == "mic":
            stream_sr = get_device_default_samplerate(cfg.mic_device)
        elif mode == "system":
            stream_sr = get_device_default_samplerate(cfg.system_device)
        else:
            # Prefer system device rate as the master.
            stream_sr = get_device_default_samplerate(cfg.system_device)

        requested_sr = int(cfg.samplerate)
        if requested_sr and abs(requested_sr - stream_sr) > 1:
            if self._on_status:
                self._on_status(
                    f"Hinweis: Gewünschte Sample-Rate {requested_sr} Hz weicht von der Device-Default {stream_sr} Hz ab. "
                    f"Für stabile Wiedergabe/Monitoring verwende ich {stream_sr} Hz."
                )

        # Resolve channel counts for input streams
        if mode == "mic":
            in_ch = _clamp_channels_for_device(int(cfg.channels), cfg.mic_device)
            out_ch = in_ch
        elif mode == "system":
            in_ch = _clamp_channels_for_device(int(cfg.channels), cfg.system_device)
            out_ch = in_ch
        else:  # both
            # mic: prefer mono if possible
            mic_max = get_device_max_input_channels(cfg.mic_device)
            mic_in_ch = 1 if mic_max >= 1 else mic_max
            mic_in_ch = _clamp_channels_for_device(mic_in_ch, cfg.mic_device)

            # system: prefer stereo if possible
            sys_max = get_device_max_input_channels(cfg.system_device)
            sys_in_ch = 2 if sys_max >= 2 else 1
            sys_in_ch = _clamp_channels_for_device(sys_in_ch, cfg.system_device)

            out_ch = 2  # stereo mix output

        # Open output file
        self._sf = sf.SoundFile(
            out_path,
            mode="w",
            samplerate=int(stream_sr),
            channels=int(out_ch),
            subtype=str(cfg.subtype),
            format="WAV" if out_path.lower().endswith(".wav") else None,
        )

        # Writer thread
        self._writer_thread = threading.Thread(target=self._writer_loop, daemon=True)
        self._writer_thread.start()

        def level_from_chunk(chunk: np.ndarray):
            if self._on_level is None:
                return
            rms = float(np.sqrt(np.mean(np.square(chunk))))
            db = 20.0 * np.log10(max(rms, 1e-9))
            self._on_level(db)

        if mode in ("mic", "system"):
            device_id = cfg.mic_device if mode == "mic" else cfg.system_device
            channels = in_ch

            def cb(indata, frames, time, status):
                if status and self._on_status:
                    self._on_status(str(status))
                if self._stop_evt.is_set() or self._pause_evt.is_set():
                    return
                chunk = indata.copy()
                proc = getattr(self, '_mic_processor', None)
                if proc is not None:
                    try:
                        chunk = proc(chunk, int(stream_sr))
                    except Exception:
                        pass
                tap = getattr(self, '_mic_tap', None)
                if tap is not None:
                    try:
                        tap(chunk, int(stream_sr))
                    except Exception:
                        pass
                # Apply gain for the selected source
                gain = self._mic_gain if mode == "mic" else self._sys_gain
                chunk = _apply_gain_and_clip(chunk, gain)
                level_from_chunk(chunk)
                try:
                    self._q_single.put_nowait(chunk)
                except queue.Full:
                    if self._on_status:
                        self._on_status("Disk writer too slow: dropping audio frames.")

            self._stream_mic = sd.InputStream(
                samplerate=int(stream_sr),
                channels=int(channels),
                dtype=str(cfg.dtype),
                device=device_id,
                callback=cb,
                blocksize=blocksize,
            )
            self._stream_mic.start()

            if self._on_status:
                src = "Mic" if mode == "mic" else "System"
                dev_txt = f"device={device_id}" if device_id is not None else "default input"
                self._on_status(f"Recording started ({src}, {dev_txt}, sr={stream_sr}, ch={channels}) → {out_path}")

        else:
            # BOTH: two streams and two queues, writer mixes to stereo
            def cb_mic(indata, frames, time, status):
                if status and self._on_status:
                    self._on_status(f"Mic: {status}")
                if self._stop_evt.is_set() or self._pause_evt.is_set():
                    return
                chunk = indata.copy()
                proc = getattr(self, '_mic_processor', None)
                if proc is not None:
                    try:
                        chunk = proc(chunk, int(stream_sr))
                    except Exception:
                        pass
                tap = getattr(self, '_mic_tap', None)
                if tap is not None:
                    try:
                        tap(chunk, int(stream_sr))
                    except Exception:
                        pass
                try:
                    self._q_mic.put_nowait(chunk)
                except queue.Full:
                    if self._on_status:
                        self._on_status("Mic queue full: dropping mic frames.")

            def cb_sys(indata, frames, time, status):
                if status and self._on_status:
                    self._on_status(f"System: {status}")
                if self._stop_evt.is_set() or self._pause_evt.is_set():
                    return
                chunk = indata.copy()
                tap = getattr(self, '_mic_tap', None)
                if tap is not None:
                    try:
                        tap(chunk, int(stream_sr))
                    except Exception:
                        pass
                try:
                    self._q_sys.put_nowait(chunk)
                except queue.Full:
                    if self._on_status:
                        self._on_status("System queue full: dropping system frames.")

            self._stream_mic = sd.InputStream(
                samplerate=int(stream_sr),
                channels=int(mic_in_ch),
                dtype=str(cfg.dtype),
                device=cfg.mic_device,
                callback=cb_mic,
                blocksize=blocksize,
            )
            self._stream_sys = sd.InputStream(
                samplerate=int(stream_sr),
                channels=int(sys_in_ch),
                dtype=str(cfg.dtype),
                device=cfg.system_device,
                callback=cb_sys,
                blocksize=blocksize,
            )
            self._stream_mic.start()
            self._stream_sys.start()

            if self._on_status:
                mic_txt = f"mic_device={cfg.mic_device}" if cfg.mic_device is not None else "mic=default"
                sys_txt = f"sys_device={cfg.system_device}" if cfg.system_device is not None else "sys=default"
                # Also report mic default rate for debugging
                mic_sr = get_device_default_samplerate(cfg.mic_device)
                self._on_status(
                    f"Recording started (Both: {mic_txt} (default_sr={mic_sr}), {sys_txt} (default_sr={stream_sr}), out=stereo mix, sr={stream_sr}, mic_gain={cfg.mic_gain_db:.1f}dB, sys_gain={cfg.system_gain_db:.1f}dB) → {out_path}"
                )

        self._running = True

    def _writer_loop(self):
        assert self._sf is not None

        while not self._stop_evt.is_set() or self._has_pending_audio():
            try:
                if self._mode in ("mic", "system"):
                    chunk = self._q_single.get(timeout=0.2)
                    # write as-is
                    self._sf.write(chunk)
                else:
                    # both: need aligned chunks from both
                    mic = self._q_mic.get(timeout=0.2)
                    sysa = self._q_sys.get(timeout=0.2)

                    # Convert mic to mono -> stereo (duplicate)
                    mic_mono = _to_mono(mic)
                    mic_st = _to_stereo(mic_mono)

                    # Convert system to stereo
                    sys_st = _to_stereo(sysa)

                    # Apply gains
                    mic_st = _apply_gain_and_clip(mic_st, self._mic_gain)
                    sys_st = _apply_gain_and_clip(sys_st, self._sys_gain)

                    # Align length by truncation to shortest
                    n = min(mic_st.shape[0], sys_st.shape[0])
                    if n <= 0:
                        continue
                    mix = sys_st[:n, :] + mic_st[:n, :]

                    # simple clipping safeguard for float32 -> file subtype
                    mix = np.clip(mix, -1.0, 1.0)

                    # level meter on mixed signal
                    if self._on_level is not None:
                        rms = float(np.sqrt(np.mean(np.square(mix))))
                        db = 20.0 * np.log10(max(rms, 1e-9))
                        self._on_level(db)

                    self._sf.write(mix)

            except queue.Empty:
                continue
            except Exception as e:
                if self._on_status:
                    self._on_status(f"Write error: {e}")

        try:
            self._sf.flush()
        except Exception:
            pass

    def _has_pending_audio(self) -> bool:
        if self._mode in ("mic", "system"):
            return not self._q_single.empty()
        return (not self._q_mic.empty()) or (not self._q_sys.empty())

    def pause(self):
        if not self._running:
            return
        self._pause_evt.set()
        if self._on_status:
            self._on_status("Paused")

    def resume(self):
        if not self._running:
            return
        self._pause_evt.clear()
        if self._on_status:
            self._on_status("Resumed")

    def stop(self):
        if not self._running:
            return

        self._stop_evt.set()

        # stop streams
        for s in (self._stream_mic, self._stream_sys):
            try:
                if s is not None:
                    s.stop()
                    s.close()
            except Exception:
                pass

        self._stream_mic = None
        self._stream_sys = None

        # wait writer
        if self._writer_thread is not None:
            self._writer_thread.join(timeout=2.0)
            self._writer_thread = None

        # close file
        try:
            if self._sf is not None:
                self._sf.close()
        except Exception:
            pass
        self._sf = None

        # drain queues
        for q in (self._q_single, self._q_mic, self._q_sys):
            while not q.empty():
                try:
                    q.get_nowait()
                except queue.Empty:
                    break

        self._running = False
        self._mode = None
        if self._on_status:
            self._on_status("Stopped")

    def set_mic_gain_db(self, db: float):
        """Update mic gain while running."""
        try:
            self._mic_gain = _db_to_lin(db or 0.0)
        except Exception:
            pass

    def set_sys_gain_db(self, db: float):
        """Update system gain while running."""
        try:
            self._sys_gain = _db_to_lin(db or 0.0)
        except Exception:
            pass


# Preferred aliases
TscribaRecorder = AudioRecorder
TscribaRecorderConfig = RecorderConfig
