#!/usr/bin/env python3
import argparse
import errno
import json
import math
import os
import queue
import signal
import struct
import subprocess
import sys
import threading
import time
from array import array
from collections import deque
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

try:
    import numpy as np
except Exception:  # pragma: no cover - runtime fallback
    np = None  # type: ignore

try:
    import webrtcvad
except Exception:  # pragma: no cover - runtime fallback
    webrtcvad = None  # type: ignore

try:
    from faster_whisper import WhisperModel
except Exception:  # pragma: no cover - runtime fallback
    WhisperModel = None  # type: ignore

try:
    from rapidfuzz import fuzz
except Exception:  # pragma: no cover - runtime fallback
    fuzz = None  # type: ignore

try:
    from sentence_transformers import SentenceTransformer
except Exception:  # pragma: no cover - runtime fallback
    SentenceTransformer = None  # type: ignore

try:
    import torch
except Exception:  # pragma: no cover - runtime fallback
    torch = None  # type: ignore

# Compatibility shim for newer torchaudio builds where list_audio_backends was removed.
try:
    import torchaudio  # type: ignore
    if not hasattr(torchaudio, "list_audio_backends"):
        def _list_audio_backends() -> list:
            # SpeechBrain only checks for non-empty availability here.
            return ["shim"]
        torchaudio.list_audio_backends = _list_audio_backends  # type: ignore[attr-defined]
    if not hasattr(torchaudio, "set_audio_backend"):
        def _set_audio_backend(_name: str) -> None:
            return None
        torchaudio.set_audio_backend = _set_audio_backend  # type: ignore[attr-defined]
except Exception:
    torchaudio = None  # type: ignore

# Compatibility shim for older callers passing use_auth_token to huggingface_hub.
try:
    import huggingface_hub  # type: ignore
    _hf_hub_download_orig = getattr(huggingface_hub, "hf_hub_download", None)
    if callable(_hf_hub_download_orig):
        def _hf_hub_download_compat(*args, **kwargs):
            if "use_auth_token" in kwargs:
                if "token" not in kwargs:
                    kwargs["token"] = kwargs.get("use_auth_token")
                kwargs.pop("use_auth_token", None)
            return _hf_hub_download_orig(*args, **kwargs)
        huggingface_hub.hf_hub_download = _hf_hub_download_compat  # type: ignore[attr-defined]
except Exception:
    huggingface_hub = None  # type: ignore

# Compatibility shim for SpeechBrain expecting deprecated transformers symbols.
try:
    import transformers  # type: ignore
    if not hasattr(transformers, "AutoModelWithLMHead"):
        try:
            from transformers import AutoModelForCausalLM  # type: ignore
            transformers.AutoModelWithLMHead = AutoModelForCausalLM  # type: ignore[attr-defined]
        except Exception:
            pass
except Exception:
    transformers = None  # type: ignore

_speechbrain_import_error = ""
try:
    from speechbrain.inference.interfaces import foreign_class
except Exception as _e:  # pragma: no cover - runtime fallback
    foreign_class = None  # type: ignore
    _speechbrain_import_error = str(_e)

HDR = struct.Struct("<IHHI")


def _runtime_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            return Path(meipass).resolve()
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


class LocalObjectionDetector:
    TARGET_SR = 16000
    VAD_FRAME_MS = 20
    CHUNK_SECONDS = 1.5
    STEP_SECONDS = 0.35
    MAX_BUFFER_SECONDS = 4.0
    TRIGGER_THRESHOLD = 0.55
    COOLDOWN_SECONDS = 8.0
    SCORE_IMPROVEMENT_RETRIGGER = 0.08
    LEXICAL_WEIGHT = 0.45
    SEMANTIC_WEIGHT = 0.55
    USE_VAD = True
    ASR_MODEL_SIZE = os.environ.get("WEBCAPTURE_ASR_MODEL", "small").strip() or "small"
    ASR_BEAM_SIZE = 3
    ASR_BEST_OF = 3
    ENABLE_SEMANTIC = os.environ.get("WEBCAPTURE_ENABLE_SEMANTIC", "").strip().lower() in ("1", "true", "yes", "on")

    def __init__(
        self,
        sample_rate: int,
        on_detection: Callable[[dict], None],
        on_text: Optional[Callable[[dict], None]] = None,
        on_status: Optional[Callable[[str], None]] = None,
    ):
        self.sample_rate = int(sample_rate)
        self.on_detection = on_detection
        self.on_text = on_text
        self.on_status = on_status
        self._stop_evt = threading.Event()
        self._q: "queue.Queue[np.ndarray]" = queue.Queue(maxsize=64)  # type: ignore[type-arg]
        self._worker: Optional[threading.Thread] = None
        self._vad = None
        self._asr = None
        self._embedder = None
        self._semantic_enabled = False
        self._intent_embeddings: Dict[str, np.ndarray] = {} if np is not None else {}
        self._last_fire_by_intent: Dict[str, float] = {}
        self._last_score_by_intent: Dict[str, float] = {}
        self._last_error = ""
        self._ready = False
        self._last_text = ""
        self._last_text_ts = 0.0

        self.intents = self._build_intents()
        self._phrase_hint = (
            "Deutsch. Relevante Einwaende: keine zeit, kein geld, andere loesung, "
            "kein budget, zu teuer, wir nutzen bereits etwas anderes."
        )

    @staticmethod
    def _build_intents() -> Dict[str, dict]:
        return {
            "Keine Zeit": {
                "canonical": "ich habe keine zeit",
                "phrases": [
                    "ich habe keine zeit",
                    "keine zeit",
                    "dafuer habe ich keine zeit",
                    "das passt gerade zeitlich nicht",
                    "ich bin gerade zu busy",
                    "ich habe im moment keine kapazitaet",
                    "keine kapazitaet gerade",
                    "im moment geht das nicht",
                ],
                "suggestion": "Verstanden. Darf ich Ihnen in 20 Sekunden den Kernnutzen zeigen?",
            },
            "Kein Geld": {
                "canonical": "ich habe kein geld",
                "phrases": [
                    "ich habe kein geld",
                    "kein geld",
                    "das ist zu teuer",
                    "dafuer ist kein budget da",
                    "kein budget",
                    "wir koennen uns das nicht leisten",
                    "passt nicht ins budget",
                    "ist finanziell gerade nicht drin",
                ],
                "suggestion": "Verstanden. Soll ich kurz die kleinste sinnvolle Startoption erklaeren?",
            },
            "Andere Loesung": {
                "canonical": "wir haben bereits eine andere loesung",
                "phrases": [
                    "wir haben bereits eine andere loesung",
                    "andere loesung",
                    "wir nutzen schon etwas anderes",
                    "wir haben schon einen anbieter",
                    "wir sind bereits versorgt",
                    "wir haben schon ein tool dafuer",
                    "wir haben das schon geloest",
                ],
                "suggestion": "Verstanden. Was soll Ihre aktuelle Loesung unbedingt besser machen?",
            },
        }

    def start(self):
        if self._worker is not None:
            return
        self._stop_evt.clear()
        self._init_models()
        if not self._ready:
            return
        self._worker = threading.Thread(target=self._run, name="objection_detector", daemon=True)
        self._worker.start()
        self._emit_status("Objection detector: active")

    def stop(self):
        self._stop_evt.set()
        if self._worker is not None:
            try:
                self._worker.join(timeout=1.5)
            except Exception:
                pass
        self._worker = None

    def reset_memory(self):
        self._last_fire_by_intent.clear()
        self._last_score_by_intent.clear()
        self._last_text = ""
        self._last_text_ts = 0.0

    def status(self) -> str:
        if self._ready:
            if self._semantic_enabled:
                return "active (lexical+semantic)"
            return "active (lexical-only)"
        if self._last_error:
            return f"disabled ({self._last_error})"
        return "disabled"

    def push_audio(self, samples: array, nch: int):
        if not self._ready or np is None:
            return
        try:
            a = np.asarray(samples, dtype=np.float32)
            if a.size == 0:
                return
            if int(nch) > 1:
                a = a.reshape(-1, int(nch)).mean(axis=1)
            else:
                a = a.reshape(-1)
            x = self._resample_to_target(a)
            x = self._vad_filter(x)
            if x.size == 0:
                return
            try:
                self._q.put_nowait(x)
            except queue.Full:
                try:
                    _ = self._q.get_nowait()
                except Exception:
                    pass
                try:
                    self._q.put_nowait(x)
                except Exception:
                    pass
        except Exception:
            return

    def _emit_status(self, msg: str):
        if callable(self.on_status):
            try:
                self.on_status(msg)
            except Exception:
                pass

    def _set_error(self, msg: str):
        self._last_error = str(msg or "unknown")
        self._emit_status(f"Objection detector: {self._last_error}")

    def _init_models(self):
        if np is None:
            self._set_error("numpy missing")
            return
        if WhisperModel is None:
            self._set_error("faster-whisper missing")
            return
        if fuzz is None:
            self._set_error("rapidfuzz missing")
            return
        try:
            self._asr = WhisperModel(self.ASR_MODEL_SIZE, device="cpu", compute_type="int8")
        except Exception as e:
            self._set_error(f"ASR load failed: {e}")
            return

        if webrtcvad is not None:
            try:
                self._vad = webrtcvad.Vad(2)
            except Exception:
                self._vad = None

        self._semantic_enabled = False
        if self.ENABLE_SEMANTIC and SentenceTransformer is not None:
            try:
                self._embedder = SentenceTransformer("sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2")
                self._prepare_embeddings()
                self._semantic_enabled = True
            except Exception as e:
                self._embedder = None
                self._emit_status(f"Objection detector: semantic disabled ({e})")
        elif self.ENABLE_SEMANTIC and SentenceTransformer is None:
            self._emit_status("Objection detector: semantic disabled (sentence-transformers missing)")

        self._last_error = ""
        self._ready = True
        mode = "lexical+semantic" if self._semantic_enabled else "lexical-only"
        self._emit_status(f"Objection detector: ASR model={self.ASR_MODEL_SIZE}, mode={mode}")

    def _prepare_embeddings(self):
        if np is None or self._embedder is None:
            return
        for intent_name, cfg in self.intents.items():
            texts = [cfg["canonical"]] + list(cfg["phrases"])
            emb = self._embedder.encode(texts, normalize_embeddings=True)
            self._intent_embeddings[intent_name] = np.asarray(emb, dtype=np.float32)

    def _resample_to_target(self, x: np.ndarray) -> np.ndarray:
        sr_in = int(max(1, self.sample_rate))
        if sr_in == self.TARGET_SR:
            return x.astype(np.float32, copy=False)
        n_in = int(x.shape[0])
        if n_in <= 1:
            return np.zeros((0,), dtype=np.float32)
        n_out = int(n_in * (self.TARGET_SR / float(sr_in)))
        if n_out <= 1:
            return np.zeros((0,), dtype=np.float32)
        idx = np.linspace(0.0, n_in - 1, num=n_out)
        return np.interp(idx, np.arange(n_in), x).astype(np.float32, copy=False)

    def _vad_filter(self, x: np.ndarray) -> np.ndarray:
        if (not self.USE_VAD) or self._vad is None or x.size == 0 or np is None:
            return x
        frame_len = int(self.TARGET_SR * self.VAD_FRAME_MS / 1000)
        if frame_len <= 0:
            return x
        out: List[np.ndarray] = []
        n = int(x.size)
        step = frame_len
        for i in range(0, n - frame_len + 1, step):
            frm = x[i : i + frame_len]
            pcm16 = np.clip(frm * 32768.0, -32768.0, 32767.0).astype(np.int16).tobytes()
            is_speech = False
            try:
                is_speech = bool(self._vad.is_speech(pcm16, self.TARGET_SR))
            except Exception:
                is_speech = False
            if is_speech:
                out.append(frm)
        if not out:
            # Keep a fallback tail to avoid starving ASR on strict VAD decisions.
            tail = min(x.size, int(0.3 * self.TARGET_SR))
            return x[-tail:] if tail > 0 else np.zeros((0,), dtype=np.float32)
        return np.concatenate(out, axis=0)

    @staticmethod
    def _normalize_text(text: str) -> str:
        t = (text or "").lower().strip()
        while "  " in t:
            t = t.replace("  ", " ")
        return t

    def _run(self):
        if np is None:
            return
        buf = np.zeros((0,), dtype=np.float32)
        chunk_n = int(self.CHUNK_SECONDS * self.TARGET_SR)
        step_n = int(self.STEP_SECONDS * self.TARGET_SR)
        max_n = int(self.MAX_BUFFER_SECONDS * self.TARGET_SR)

        while not self._stop_evt.is_set():
            try:
                block = self._q.get(timeout=0.2)
            except queue.Empty:
                continue
            if block.size == 0:
                continue

            buf = np.concatenate([buf, block], axis=0)
            if buf.size > max_n:
                buf = buf[-max_n:]
            if buf.size < chunk_n:
                continue

            window = buf[-chunk_n:]
            try:
                segments, _info = self._asr.transcribe(
                    window,
                    language="de",
                    task="transcribe",
                    initial_prompt=self._phrase_hint,
                    beam_size=self.ASR_BEAM_SIZE,
                    best_of=self.ASR_BEST_OF,
                    vad_filter=False,
                    temperature=0.0,
                    condition_on_previous_text=False,
                )
            except Exception as e:
                self._set_error(f"ASR runtime error: {e}")
                continue

            text = " ".join((getattr(seg, "text", "") or "").strip() for seg in segments).strip()
            text = self._normalize_text(text)
            if not text:
                continue

            now = time.time()
            if text == self._last_text and (now - self._last_text_ts) < 1.0:
                buf = buf[-step_n:] if buf.size > step_n else buf
                continue
            self._last_text = text
            self._last_text_ts = now
            if callable(self.on_text):
                try:
                    self.on_text({"text": text, "at": now})
                except Exception:
                    pass

            match = self._match_intent(text)
            if match is not None:
                intent_name, score = match
                last_ts = self._last_fire_by_intent.get(intent_name, 0.0)
                last_score = self._last_score_by_intent.get(intent_name, 0.0)
                cooldown_done = (now - last_ts) >= self.COOLDOWN_SECONDS
                score_retrigger = score >= (last_score + self.SCORE_IMPROVEMENT_RETRIGGER)
                if cooldown_done or score_retrigger:
                    cfg = self.intents[intent_name]
                    self._last_fire_by_intent[intent_name] = now
                    self._last_score_by_intent[intent_name] = score
                    payload = {
                        "intent": intent_name,
                        "score": float(score),
                        "suggestion": cfg["suggestion"],
                        "text": text,
                        "at": now,
                    }
                    try:
                        self.on_detection(payload)
                    except Exception:
                        pass

            buf = buf[-step_n:] if buf.size > step_n else buf

    def _match_intent(self, text: str) -> Optional[Tuple[str, float]]:
        if fuzz is None:
            return None
        best_intent = None
        best_score = 0.0
        keyword_boosts = {
            "Keine Zeit": ("keine", "zeit"),
            "Kein Geld": ("kein", "geld"),
            "Andere Loesung": ("andere", "loesung"),
        }
        semantic_vector = None
        if self._semantic_enabled and self._embedder is not None and np is not None:
            try:
                semantic_vector = self._embedder.encode([text], normalize_embeddings=True)[0]
            except Exception:
                semantic_vector = None

        for intent_name, cfg in self.intents.items():
            variants = [cfg["canonical"]] + list(cfg["phrases"])
            lexical = 0.0
            for phrase in variants:
                s = float(fuzz.partial_ratio(text, phrase)) / 100.0
                if s > lexical:
                    lexical = s

            semantic = lexical
            if semantic_vector is not None and np is not None:
                emb = self._intent_embeddings.get(intent_name)
                if emb is not None and emb.size > 0:
                    try:
                        sims = np.matmul(emb, semantic_vector)
                        semantic = max(float(np.max(sims)), lexical)
                    except Exception:
                        semantic = lexical

            score = (self.LEXICAL_WEIGHT * lexical) + (self.SEMANTIC_WEIGHT * semantic)
            kws = keyword_boosts.get(intent_name)
            if kws and all(k in text for k in kws):
                score = max(score, 0.92)
            if score > best_score:
                best_score = score
                best_intent = intent_name

        if best_intent is None or best_score < self.TRIGGER_THRESHOLD:
            return None
        return best_intent, best_score


class LocalToneDetector:
    TARGET_SR = 16000
    WINDOW_SECONDS = 1.5
    HOP_SECONDS = 0.25
    MAX_BUFFER_SECONDS = 4.0
    SMOOTH_WINDOW = 5
    MIN_MAJORITY = 3
    SWITCH_COOLDOWN_SECONDS = 1.2
    ENABLE_TONE = os.environ.get("WEBCAPTURE_ENABLE_TONE", "").strip().lower() in ("1", "true", "yes", "on")
    MODEL_SOURCE = os.environ.get(
        "WEBCAPTURE_TONE_MODEL",
        "speechbrain/emotion-recognition-wav2vec2-IEMOCAP",
    ).strip() or "speechbrain/emotion-recognition-wav2vec2-IEMOCAP"

    def __init__(
        self,
        sample_rate: int,
        on_tone: Callable[[dict], None],
        on_status: Optional[Callable[[str], None]] = None,
    ):
        self.sample_rate = int(sample_rate)
        self.on_tone = on_tone
        self.on_status = on_status
        self._stop_evt = threading.Event()
        self._q: "queue.Queue[np.ndarray]" = queue.Queue(maxsize=64)  # type: ignore[type-arg]
        self._worker: Optional[threading.Thread] = None
        self._classifier = None
        self._ready = False
        self._last_error = ""
        self._recent_labels: "deque[str]" = deque(maxlen=self.SMOOTH_WINDOW)
        self._enabled = bool(self.ENABLE_TONE)
        self._display_label = "Calm"
        self._last_switch_ts = 0.0

    def start(self):
        if not self._enabled:
            return
        if self._worker is not None:
            return
        self._stop_evt.clear()
        self._init_model()
        if not self._ready:
            return
        self._worker = threading.Thread(target=self._run, name="tone_detector", daemon=True)
        self._worker.start()
        self._emit_status("Tone detector: active")

    def stop(self):
        self._stop_evt.set()
        if self._worker is not None:
            try:
                self._worker.join(timeout=1.5)
            except Exception:
                pass
        self._worker = None

    def set_enabled(self, enabled: bool):
        self._enabled = bool(enabled)
        if self._enabled:
            self.start()
        else:
            self.stop()

    def is_enabled(self) -> bool:
        return bool(self._enabled)

    def status(self) -> str:
        if not self._enabled:
            return "disabled (off)"
        if self._ready:
            return "active"
        if self._last_error:
            return f"disabled ({self._last_error})"
        return "disabled"

    def reset_memory(self):
        self._recent_labels.clear()
        self._display_label = "Calm"
        self._last_switch_ts = 0.0

    def push_audio(self, samples: array, nch: int):
        if not self._ready or np is None:
            return
        try:
            a = np.asarray(samples, dtype=np.float32)
            if a.size == 0:
                return
            if int(nch) > 1:
                a = a.reshape(-1, int(nch)).mean(axis=1)
            else:
                a = a.reshape(-1)
            x = self._resample_to_target(a)
            if x.size == 0:
                return
            try:
                self._q.put_nowait(x)
            except queue.Full:
                try:
                    _ = self._q.get_nowait()
                except Exception:
                    pass
                try:
                    self._q.put_nowait(x)
                except Exception:
                    pass
        except Exception:
            return

    def _emit_status(self, msg: str):
        if callable(self.on_status):
            try:
                self.on_status(msg)
            except Exception:
                pass

    def _set_error(self, msg: str):
        self._last_error = str(msg or "unknown")
        self._emit_status(f"Tone detector: {self._last_error}")

    def _init_model(self):
        if not self._enabled:
            return
        if np is None:
            self._set_error("numpy missing")
            return
        if torch is None:
            self._set_error("torch missing")
            return
        if foreign_class is None:
            reason = _speechbrain_import_error or "speechbrain missing"
            self._set_error(f"speechbrain unavailable: {reason}")
            return
        try:
            self._classifier = foreign_class(
                source=self.MODEL_SOURCE,
                pymodule_file="custom_interface.py",
                classname="CustomEncoderWav2vec2Classifier",
            )
        except Exception as e:
            self._set_error(f"model load failed: {e}")
            return
        self._last_error = ""
        self._ready = True
        self._emit_status(f"Tone detector: model={self.MODEL_SOURCE}")

    def _resample_to_target(self, x: np.ndarray) -> np.ndarray:
        sr_in = int(max(1, self.sample_rate))
        if sr_in == self.TARGET_SR:
            return x.astype(np.float32, copy=False)
        n_in = int(x.shape[0])
        if n_in <= 1:
            return np.zeros((0,), dtype=np.float32)
        n_out = int(n_in * (self.TARGET_SR / float(sr_in)))
        if n_out <= 1:
            return np.zeros((0,), dtype=np.float32)
        idx = np.linspace(0.0, n_in - 1, num=n_out)
        return np.interp(idx, np.arange(n_in), x).astype(np.float32, copy=False)

    @staticmethod
    def _map_label(label: str) -> str:
        l = str(label or "").strip().lower()
        # Business-facing 4-term mapping for objection handling.
        if l in ("ang", "angry"):
            return "Frustrated"
        if l in ("sad",):
            return "Tense"
        if l in ("hap", "exc", "happy", "excited"):
            return "Positive"
        if l in ("neu", "neutral"):
            return "Calm"
        return "Calm"

    @staticmethod
    def _majority_vote(items: "deque[str]") -> str:
        if not items:
            return "unknown"
        counts: Dict[str, int] = {}
        for it in items:
            counts[it] = counts.get(it, 0) + 1
        best = max(counts.items(), key=lambda kv: kv[1])[0]
        return best

    @staticmethod
    def _majority_count(items: "deque[str]", label: str) -> int:
        c = 0
        for it in items:
            if it == label:
                c += 1
        return c

    def _classify(self, window: np.ndarray) -> Optional[dict]:
        if self._classifier is None or torch is None:
            return None
        try:
            wav = torch.from_numpy(window.astype(np.float32, copy=False)).unsqueeze(0)
            out_prob, score, _index, text_lab = self._classifier.classify_batch(wav)
            raw_label = ""
            if isinstance(text_lab, (list, tuple)) and text_lab:
                raw_label = str(text_lab[0])
            elif text_lab is not None:
                raw_label = str(text_lab)
            raw_label = raw_label.strip("[]'\" ")
            mapped = self._map_label(raw_label)

            conf = 0.0
            try:
                if score is not None:
                    if hasattr(score, "detach"):
                        conf = float(score.detach().cpu().reshape(-1)[0].item())
                    else:
                        conf = float(score)
            except Exception:
                conf = 0.0
            if conf <= 0.0 and out_prob is not None:
                try:
                    if hasattr(out_prob, "detach"):
                        conf = float(out_prob.detach().cpu().reshape(-1).max().item())
                except Exception:
                    conf = 0.0
            conf = max(0.0, min(1.0, conf))
            scores: Dict[str, float] = {}
            try:
                if out_prob is not None and hasattr(out_prob, "detach"):
                    vals = out_prob.detach().cpu().reshape(-1).tolist()
                    label_names = []
                    try:
                        enc = getattr(getattr(self._classifier, "hparams", None), "label_encoder", None)
                        ind2lab = getattr(enc, "ind2lab", None)
                        if isinstance(ind2lab, dict):
                            for i in range(len(vals)):
                                label_names.append(str(ind2lab.get(i, f"class_{i}")))
                        else:
                            label_names = [f"class_{i}" for i in range(len(vals))]
                    except Exception:
                        label_names = [f"class_{i}" for i in range(len(vals))]
                    scores = {str(k): float(v) for k, v in zip(label_names, vals)}
            except Exception:
                scores = {}
            return {"label": mapped, "score": conf, "scores": scores}
        except Exception:
            return None

    def _run(self):
        if np is None:
            return
        buf = np.zeros((0,), dtype=np.float32)
        win_n = int(self.WINDOW_SECONDS * self.TARGET_SR)
        hop_n = int(self.HOP_SECONDS * self.TARGET_SR)
        max_n = int(self.MAX_BUFFER_SECONDS * self.TARGET_SR)

        while not self._stop_evt.is_set():
            try:
                block = self._q.get(timeout=0.2)
            except queue.Empty:
                continue
            if block.size == 0:
                continue

            buf = np.concatenate([buf, block], axis=0)
            if buf.size > max_n:
                buf = buf[-max_n:]
            if buf.size < win_n:
                continue

            window = buf[-win_n:]
            out = self._classify(window)
            if out is not None:
                label = str(out.get("label") or "unknown")
                score = float(out.get("score") or 0.0)
                scores = dict(out.get("scores") or {})
                self._recent_labels.append(label)
                candidate = self._majority_vote(self._recent_labels)
                candidate_count = self._majority_count(self._recent_labels, candidate)
                now = time.time()
                if candidate == self._display_label:
                    pass
                else:
                    # Hysteresis: only switch on stable majority and small cooldown.
                    if candidate_count >= self.MIN_MAJORITY and (now - self._last_switch_ts) >= self.SWITCH_COOLDOWN_SECONDS:
                        self._display_label = candidate
                        self._last_switch_ts = now
                try:
                    self.on_tone(
                        {
                            "label": self._display_label,
                            "rawLabel": label,
                            "score": score,
                            "scores": scores,
                            "at": now,
                        }
                    )
                except Exception:
                    pass

            buf = buf[-hop_n:] if buf.size > hop_n else buf


class TapBridge:
    def __init__(self, helper_path: Optional[str], sample_rate: int = 48000):
        self.sample_rate = int(sample_rate)
        self._helper_path_override = helper_path

        self._lock = threading.RLock()
        self._proc: Optional[subprocess.Popen] = None
        self._stdout_thread: Optional[threading.Thread] = None
        self._stderr_thread: Optional[threading.Thread] = None
        self._stop_evt = threading.Event()

        self._running = False
        self._last_db = -120.0
        self._last_update_ts = 0.0
        self._error = ""
        self._last_intent = ""
        self._last_intent_score = 0.0
        self._last_intent_at = 0.0
        self._last_suggestion = ""
        self._last_text = ""
        self._last_text_at = 0.0
        self._detector_status = "disabled"
        self._tone_label = ""
        self._tone_raw_label = ""
        self._tone_score = 0.0
        self._tone_at = 0.0
        self._tone_scores: Dict[str, float] = {}
        self._tone_status = "disabled"
        self._detector = LocalObjectionDetector(
            sample_rate=self.sample_rate,
            on_detection=self._on_intent_detected,
            on_text=self._on_asr_text,
            on_status=self._on_detector_status,
        )
        self._tone_detector = LocalToneDetector(
            sample_rate=self.sample_rate,
            on_tone=self._on_tone_update,
            on_status=self._on_tone_status,
        )

    def _resolve_helper_path(self) -> Path:
        if self._helper_path_override:
            p = Path(self._helper_path_override).expanduser().resolve()
            if not p.exists():
                raise FileNotFoundError(f"helper binary not found: {p}")
            return p

        here = _runtime_base_dir()
        exe_dir = Path(sys.executable).resolve().parent
        candidates = [
            here / "bin" / "system_audio_tap",
            exe_dir / "system_audio_tap",
        ]
        for c in candidates:
            if c.exists():
                return c.resolve()

        raise FileNotFoundError("system_audio_tap binary not found. Build it first (./build_helper.sh).")

    def state(self) -> dict:
        with self._lock:
            return {
                "running": self._running,
                "db": float(self._last_db),
                "sampleRate": self.sample_rate,
                "lastUpdate": self._last_update_ts,
                "error": self._error,
                "lastIntent": self._last_intent,
                "lastIntentScore": float(self._last_intent_score),
                "lastIntentAt": float(self._last_intent_at),
                "lastSuggestion": self._last_suggestion,
                "lastText": self._last_text,
                "lastTextAt": float(self._last_text_at),
                "detectorStatus": self._detector_status,
                "toneLabel": self._tone_label,
                "toneRawLabel": self._tone_raw_label,
                "toneScore": float(self._tone_score),
                "toneAt": float(self._tone_at),
                "toneScores": dict(self._tone_scores),
                "toneStatus": self._tone_status,
                "toneEnabled": bool(self._tone_detector.is_enabled()),
            }

    def start(self) -> dict:
        with self._lock:
            if self._running and self._proc is not None and self._proc.poll() is None:
                return self.state()

            helper_path = self._resolve_helper_path()
            self._stop_evt.clear()
            self._error = ""
            self._last_db = -120.0
            self._last_update_ts = time.time()
            self._last_intent = ""
            self._last_intent_score = 0.0
            self._last_intent_at = 0.0
            self._last_suggestion = ""
            self._last_text = ""
            self._last_text_at = 0.0
            self._tone_label = ""
            self._tone_raw_label = ""
            self._tone_score = 0.0
            self._tone_at = 0.0
            self._tone_scores = {}

            self._proc = subprocess.Popen(
                [str(helper_path)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
            )
            self._running = True

            self._stdout_thread = threading.Thread(target=self._stdout_loop, daemon=True)
            self._stderr_thread = threading.Thread(target=self._stderr_loop, daemon=True)
            self._stdout_thread.start()
            self._stderr_thread.start()
            self._detector.start()
            self._detector_status = self._detector.status()
            self._tone_detector.start()
            self._tone_status = self._tone_detector.status()

            return self.state()

    def stop(self) -> dict:
        with self._lock:
            self._stop_evt.set()
            proc = self._proc
            self._proc = None
            self._running = False

        try:
            self._detector.stop()
            with self._lock:
                self._detector_status = self._detector.status()
        except Exception:
            pass
        try:
            self._tone_detector.stop()
            with self._lock:
                self._tone_status = self._tone_detector.status()
        except Exception:
            pass

        if proc is not None:
            try:
                proc.terminate()
            except Exception:
                pass
            try:
                proc.wait(timeout=2.0)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

        with self._lock:
            return self.state()

    def shutdown(self):
        self.stop()

    def _clear_tone_state(self):
        with self._lock:
            self._tone_label = ""
            self._tone_raw_label = ""
            self._tone_score = 0.0
            self._tone_at = 0.0
            self._tone_scores = {}

    def tone_on(self) -> dict:
        try:
            self._tone_detector.set_enabled(True)
            self._tone_detector.start()
        except Exception:
            pass
        with self._lock:
            self._tone_status = self._tone_detector.status()
        return self.state()

    def tone_off(self) -> dict:
        try:
            self._tone_detector.set_enabled(False)
            self._tone_detector.reset_memory()
        except Exception:
            pass
        self._clear_tone_state()
        with self._lock:
            self._tone_status = self._tone_detector.status()
        return self.state()

    def reset_objection_state(self) -> dict:
        with self._lock:
            self._last_intent = ""
            self._last_intent_score = 0.0
            self._last_intent_at = 0.0
            self._last_suggestion = ""
            self._last_text = ""
            self._last_text_at = 0.0
            self._tone_label = ""
            self._tone_raw_label = ""
            self._tone_score = 0.0
            self._tone_at = 0.0
            self._tone_scores = {}
            self._last_update_ts = time.time()
        try:
            self._detector.reset_memory()
        except Exception:
            pass
        try:
            self._tone_detector.reset_memory()
        except Exception:
            pass
        return self.state()

    def _set_error(self, msg: str):
        with self._lock:
            self._error = msg

    def _on_detector_status(self, msg: str):
        with self._lock:
            self._detector_status = self._detector.status()
        if "disabled" in self._detector_status:
            self._set_error(msg)

    def _on_intent_detected(self, payload: dict):
        with self._lock:
            self._last_intent = str(payload.get("intent") or "")
            self._last_intent_score = float(payload.get("score") or 0.0)
            self._last_intent_at = float(payload.get("at") or time.time())
            self._last_suggestion = str(payload.get("suggestion") or "")
            self._last_text = str(payload.get("text") or "")
            self._last_text_at = float(payload.get("at") or time.time())
            self._last_update_ts = time.time()

    def _on_asr_text(self, payload: dict):
        with self._lock:
            self._last_text = str(payload.get("text") or "")
            self._last_text_at = float(payload.get("at") or time.time())

    def _on_tone_status(self, _msg: str):
        with self._lock:
            self._tone_status = self._tone_detector.status()

    def _on_tone_update(self, payload: dict):
        with self._lock:
            self._tone_label = str(payload.get("label") or "")
            self._tone_raw_label = str(payload.get("rawLabel") or "")
            self._tone_score = float(payload.get("score") or 0.0)
            self._tone_at = float(payload.get("at") or time.time())
            self._tone_scores = dict(payload.get("scores") or {})

    def _update_db(self, db: float):
        with self._lock:
            self._last_db = float(db)
            self._last_update_ts = time.time()

    def _mark_stopped(self):
        with self._lock:
            self._running = False
        try:
            self._detector.stop()
            with self._lock:
                self._detector_status = self._detector.status()
        except Exception:
            pass
        try:
            self._tone_detector.stop()
            with self._lock:
                self._tone_status = self._tone_detector.status()
        except Exception:
            pass

    @staticmethod
    def _read_exact(stream, size: int) -> bytes:
        out = bytearray()
        while len(out) < size:
            chunk = stream.read(size - len(out))
            if not chunk:
                break
            out += chunk
        return bytes(out)

    def _stdout_loop(self):
        proc = self._proc
        if proc is None or proc.stdout is None:
            return

        try:
            while not self._stop_evt.is_set():
                hdr = self._read_exact(proc.stdout, HDR.size)
                if len(hdr) != HDR.size:
                    break

                nframes, nch, fmt, nbytes = HDR.unpack(hdr)
                if fmt != 1:
                    self._set_error(f"unexpected format code: {fmt}")
                    break
                if nframes <= 0 or nch <= 0 or nbytes <= 0:
                    continue

                payload = self._read_exact(proc.stdout, int(nbytes))
                if len(payload) != int(nbytes):
                    break

                samples = array("f")
                try:
                    samples.frombytes(payload)
                except Exception:
                    continue

                if not samples:
                    continue

                sum_sq = 0.0
                count = 0
                for x in samples:
                    xf = float(x)
                    if not math.isfinite(xf):
                        continue
                    sum_sq += xf * xf
                    count += 1

                if count == 0:
                    continue

                rms = math.sqrt(sum_sq / count)
                db = 20.0 * math.log10(max(rms, 1e-12))
                self._update_db(db)
                self._detector.push_audio(samples, int(nch))
                self._tone_detector.push_audio(samples, int(nch))
        except Exception as e:
            self._set_error(f"reader error: {e}")
        finally:
            self._mark_stopped()

    def _stderr_loop(self):
        proc = self._proc
        if proc is None or proc.stderr is None:
            return

        try:
            while not self._stop_evt.is_set():
                line = proc.stderr.readline()
                if not line:
                    break
                s = line.decode(errors="replace").strip()
                if s:
                    self._set_error(s)
        except Exception as e:
            self._set_error(f"stderr error: {e}")


class RequestHandler(BaseHTTPRequestHandler):
    bridge: TapBridge = None  # type: ignore

    def _write_json(self, code: int, payload: dict):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _write_html(self, code: int, html: str):
        body = html.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        if self.path == "/" or self.path == "/index.html":
            index_file = _runtime_base_dir() / "system_audio_tap_mvp.html"
            if index_file.exists():
                self._write_html(200, index_file.read_text(encoding="utf-8"))
                return
            self._write_html(404, "<h1>MVP page not found</h1>")
            return

        if self.path == "/state":
            self._write_json(200, self.bridge.state())
            return

        if self.path == "/events":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()

            try:
                while True:
                    payload = json.dumps(self.bridge.state())
                    self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
                    self.wfile.flush()
                    time.sleep(0.2)
            except (BrokenPipeError, ConnectionResetError):
                return
            except Exception:
                return

        self._write_json(404, {"error": "not found"})

    def do_POST(self):
        if self.path == "/start":
            try:
                self._write_json(200, self.bridge.start())
            except Exception as e:
                self._write_json(500, {"error": str(e)})
            return

        if self.path == "/stop":
            self._write_json(200, self.bridge.stop())
            return

        if self.path == "/reset_objection":
            self._write_json(200, self.bridge.reset_objection_state())
            return

        if self.path == "/tone_on":
            self._write_json(200, self.bridge.tone_on())
            return

        if self.path == "/tone_off":
            self._write_json(200, self.bridge.tone_off())
            return

        self._write_json(404, {"error": "not found"})

    def log_message(self, fmt: str, *args):
        return


class QuietThreadingHTTPServer(ThreadingHTTPServer):
    """Suppress noisy client disconnect tracebacks."""

    def handle_error(self, request, client_address):  # type: ignore[override]
        ex_t, ex_v, _tb = sys.exc_info()
        if ex_t is ConnectionResetError or isinstance(ex_v, ConnectionResetError):
            return
        super().handle_error(request, client_address)


def serve(host: str, port: int, helper_path: Optional[str], sample_rate: int):
    bridge = TapBridge(helper_path=helper_path, sample_rate=sample_rate)
    RequestHandler.bridge = bridge
    requested_port = int(port)
    used_port = requested_port
    server = None

    # Try a predictable localhost range first, so a static finder page can discover the service.
    for candidate in range(requested_port, requested_port + 51):
        try:
            server = QuietThreadingHTTPServer((host, candidate), RequestHandler)
            used_port = candidate
            break
        except OSError as e:
            if getattr(e, "errno", None) == errno.EADDRINUSE:
                continue
            raise

    # Last-resort fallback to any free port.
    if server is None:
        server = QuietThreadingHTTPServer((host, 0), RequestHandler)
        used_port = int(server.server_address[1])

    def _shutdown(_sig, _frame):
        bridge.shutdown()
        server.shutdown()

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    if used_port != requested_port:
        print(f"Port {requested_port} is in use, selected free port {used_port}.")
    print(f"Serving on http://{host}:{used_port}")
    print("Endpoints: GET /state, POST /start, POST /stop, GET /events, GET /")
    try:
        server.serve_forever()
    finally:
        bridge.shutdown()
        server.server_close()


def parse_args():
    p = argparse.ArgumentParser(description="Core Audio tap -> browser bridge (MVP)")
    p.add_argument("--host", default="127.0.0.1", help="Host to bind (default: 127.0.0.1)")
    p.add_argument("--port", type=int, default=8765, help="Port to bind (default: 8765)")
    p.add_argument("--helper-path", default=None, help="Path to system_audio_tap binary")
    p.add_argument("--sample-rate", type=int, default=48000, help="Metadata sample rate for state/events")
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()
    serve(host=args.host, port=args.port, helper_path=args.helper_path, sample_rate=args.sample_rate)
