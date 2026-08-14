"""Tiny local web UI: static files, server-sent telemetry, JSON control.

Server-sent events rather than websockets, deliberately. The traffic here is
almost entirely one-way -- the modem produces telemetry several times a
second and the browser occasionally sends a command -- and SSE gets that with
a plain HTTP response and no handshake, no framing layer, and no dependency.
Controls go back as ordinary POSTs.

Bound to 127.0.0.1 by default. A transmitter's control surface is not
something to expose to the network by accident.
"""

from __future__ import annotations

import json
import mimetypes
import os
import queue
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

WEB_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web")


def profile_list() -> list[dict]:
    """Profiles as the UI needs them, newest card rates last.

    Built from the profile table rather than written into the HTML, so adding
    a profile makes it appear in both dropdowns without touching either page.
    """
    from . import profiles as P

    out = []
    top = max(P.LADDER, key=lambda m: m.bits_per_symbol * m.conv_rate * m.rs_rate)
    for name, p in P.PROFILES.items():
        khz = p.sample_rate / 1000
        out.append({
            "name": name,
            "sample_rate": p.sample_rate,
            "symbol_rate": p.symbol_rate,
            "rolloff": p.rolloff,
            "carrier": p.carrier,
            "pilot_spacing": p.pilot_spacing,
            "max_bitrate": int(p.net_bitrate(top)),
            "band": [round(p.band[0]), round(p.band[1])],
            "label": f"{name} - {khz:g} kHz card, up to {p.net_bitrate(top) / 1000:.0f} kbps",
            "description": p.description,
        })
    out.sort(key=lambda d: (-d["sample_rate"], -d["max_bitrate"]))
    return out


def modcod_list() -> list[dict]:
    """Every modulation and code rate the header can express."""
    from . import profiles as P

    return [{
        "index": m.index,
        "modulation": m.modulation,
        "code_rate": f"{m.conv_num}/{m.conv_den}",
        "bits_per_symbol": m.bits_per_symbol,
        "rs": m.rs_k,
        "required_evm_db": m.required_evm_db,
        "ladder": m.index <= 12,
    } for m in P.MODCODS]


def sample_rates() -> list[int]:
    from . import profiles as P

    return sorted({p.sample_rate for p in P.PROFILES.values()} | {44100, 48000, 96000})


def symbol_rates(sample_rate: int) -> list[int]:
    from . import profiles as P

    return P.valid_symbol_rates(sample_rate)


class Telemetry:
    """Fan-out of state snapshots to whatever browsers are listening.

    Each subscriber gets a bounded queue that drops its oldest entry when
    full. A browser tab that has been backgrounded must not be able to stall
    the modem by not reading its socket.
    """

    def __init__(self, depth: int = 8):
        self._subs: list[queue.Queue] = []
        self._lock = threading.Lock()
        self._depth = depth
        self._latest: dict[str, Any] = {}

    def publish(self, state: dict[str, Any]) -> None:
        self._latest = state
        data = json.dumps(state)
        with self._lock:
            for q in self._subs:
                if q.full():
                    try:
                        q.get_nowait()
                    except queue.Empty:
                        pass
                try:
                    q.put_nowait(data)
                except queue.Full:
                    pass

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=self._depth)
        with self._lock:
            self._subs.append(q)
        if self._latest:
            q.put_nowait(json.dumps(self._latest))
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._subs:
                self._subs.remove(q)


def serve(page: str, telemetry: Telemetry,
          on_control: Callable[[dict], dict] | None = None,
          port: int = 8731, host: str = "127.0.0.1") -> ThreadingHTTPServer:
    """Start the UI server on a background thread and return it."""

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_args) -> None:
            pass  # the modem's own output is the interesting one

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            path = self.path.split("?", 1)[0]
            if path == "/":
                path = "/" + page
            if path == "/events":
                return self._events()
            name = os.path.normpath(path.lstrip("/")).replace("\\", "/")
            if name.startswith("..") or os.path.isabs(name):
                return self._send(403, b"no", "text/plain")
            full = os.path.join(WEB_DIR, name)
            if not os.path.isfile(full):
                return self._send(404, b"not found", "text/plain")
            ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
            with open(full, "rb") as fh:
                self._send(200, fh.read(), ctype)

        def _events(self) -> None:
            q = telemetry.subscribe()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            try:
                while True:
                    try:
                        data = q.get(timeout=15)
                        self.wfile.write(f"data: {data}\n\n".encode())
                    except queue.Empty:
                        # Comment frame; keeps proxies and idle timeouts happy.
                        self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                telemetry.unsubscribe(q)

        def do_POST(self) -> None:
            if self.path.split("?", 1)[0] != "/control" or on_control is None:
                return self._send(404, b"not found", "text/plain")
            length = int(self.headers.get("Content-Length") or 0)
            try:
                payload = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                return self._send(400, b'{"error":"bad json"}', "application/json")
            try:
                result = on_control(payload) or {}
            except Exception as exc:  # surface control errors in the UI
                result = {"error": str(exc)}
            self._send(200, json.dumps(result).encode(), "application/json")

    class Server(ThreadingHTTPServer):
        daemon_threads = True

        def handle_error(self, request, client_address) -> None:
            """Swallow disconnects, report everything else.

            A closed browser tab aborts a long-lived event stream, and the
            default handler prints a full traceback for it. On a transmitter
            meant to run for hours that turns an ordinary page reload into
            console noise, and buries anything that actually matters.
            ConnectionError covers aborted, reset and broken-pipe; real faults
            still surface.
            """
            if isinstance(sys.exc_info()[1], ConnectionError):
                return
            super().handle_error(request, client_address)

    server = Server((host, port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server
