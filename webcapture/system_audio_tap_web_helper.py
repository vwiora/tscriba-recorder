#!/usr/bin/env python3
import argparse
import errno
import json
import math
import signal
import struct
import subprocess
import sys
import threading
import time
from array import array
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional

HDR = struct.Struct("<IHHI")


def _runtime_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            return Path(meipass).resolve()
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


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

            return self.state()

    def stop(self) -> dict:
        with self._lock:
            self._stop_evt.set()
            proc = self._proc
            self._proc = None
            self._running = False

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

    def _set_error(self, msg: str):
        with self._lock:
            self._error = msg

    def _update_db(self, db: float):
        with self._lock:
            self._last_db = float(db)
            self._last_update_ts = time.time()

    def _mark_stopped(self):
        with self._lock:
            self._running = False

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

        self._write_json(404, {"error": "not found"})

    def log_message(self, fmt: str, *args):
        return


def serve(host: str, port: int, helper_path: Optional[str], sample_rate: int):
    bridge = TapBridge(helper_path=helper_path, sample_rate=sample_rate)
    RequestHandler.bridge = bridge
    requested_port = int(port)
    used_port = requested_port
    server = None

    # Try a predictable localhost range first, so a static finder page can discover the service.
    for candidate in range(requested_port, requested_port + 51):
        try:
            server = ThreadingHTTPServer((host, candidate), RequestHandler)
            used_port = candidate
            break
        except OSError as e:
            if getattr(e, "errno", None) == errno.EADDRINUSE:
                continue
            raise

    # Last-resort fallback to any free port.
    if server is None:
        server = ThreadingHTTPServer((host, 0), RequestHandler)
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
