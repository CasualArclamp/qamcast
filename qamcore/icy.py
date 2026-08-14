"""Now-playing metadata from Icecast and Shoutcast streams.

The song title does not travel in the audio. It rides in the HTTP layer: a
client that asks with ``Icy-MetaData: 1`` gets a stream where every
``icy-metaint`` bytes of audio are followed by a short metadata block. So this
is read over its own connection rather than out of the decoder -- ffmpeg
surfaces the ICY *headers* (station name, genre, URL) but not ``StreamTitle``,
and emits nothing when the song changes.

Raw sockets rather than urllib because Shoutcast servers answer with
``ICY 200 OK`` instead of an HTTP status line, which urllib rejects outright.
One of the stations in the folder does exactly that.
"""

from __future__ import annotations

import re
import socket
import ssl
import threading
import time
from dataclasses import dataclass, field
from urllib.parse import urlsplit

USER_AGENT = "qamcore/1.0"

# Enough of the audio has to be read to reach the next metadata block, so a
# poll costs icy-metaint bytes and takes as long as that much audio takes to
# arrive. On SomaFM that is 45 kB, about 11 seconds at 32 kbps, which paces
# the polling on its own.
DEFAULT_INTERVAL = 20.0
DEFAULT_TIMEOUT = 15.0
MAX_METAINT = 1 << 20        # a server asking for more is not worth following

TITLE_RE = re.compile(r"StreamTitle='(?P<t>.*?)';", re.S)
URL_RE = re.compile(r"StreamUrl='(?P<u>.*?)';", re.S)


@dataclass
class NowPlaying:
    station: str = ""
    artist: str = ""
    title: str = ""
    raw: str = ""            # the StreamTitle exactly as sent
    art: str = ""            # StreamUrl, usually cover art
    at: float = field(default_factory=time.time)

    def as_dict(self) -> dict:
        return {"station": self.station, "artist": self.artist,
                "title": self.title, "raw": self.raw, "art": self.art}


def split_title(raw: str, swap: bool = False) -> tuple[str, str]:
    """``StreamTitle`` into (artist, title).

    There is no standard. Most stations send ``Artist - Title`` and one in the
    folder here sends ``Title - Artist`` -- "It's Just For You - The
    Herbaliser" is a Herbaliser track, not a band called It's Just For You.
    Nothing in the protocol distinguishes them, so the common convention is
    the default and ``swap`` is offered for the rest. The raw string is kept
    either way, so nothing is lost to a wrong guess.
    """
    raw = (raw or "").strip()
    if " - " not in raw:
        return "", raw
    left, _, right = raw.partition(" - ")
    left, right = left.strip(), right.strip()
    return (right, left) if swap else (left, right)


def _connect(url: str, timeout: float):
    parts = urlsplit(url)
    host = parts.hostname
    if not host:
        raise ValueError(f"no host in {url!r}")
    port = parts.port or (443 if parts.scheme == "https" else 80)
    path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    sock = socket.create_connection((host, port), timeout)
    try:
        if parts.scheme == "https":
            sock = ssl.create_default_context().wrap_socket(
                sock, server_hostname=host)
        request = (f"GET {path} HTTP/1.0\r\n"
                   f"Host: {parts.netloc}\r\n"
                   f"User-Agent: {USER_AGENT}\r\n"
                   f"Icy-MetaData: 1\r\n"
                   f"Connection: close\r\n\r\n")
        sock.sendall(request.encode("latin-1"))
    except Exception:
        sock.close()
        raise
    return sock


def _read_headers(sock, timeout: float) -> tuple[dict, bytes]:
    buf = b""
    deadline = time.time() + timeout
    while b"\r\n\r\n" not in buf:
        if time.time() > deadline or len(buf) > 65536:
            raise TimeoutError("no headers")
        chunk = sock.recv(4096)
        if not chunk:
            raise ConnectionError("closed before headers")
        buf += chunk
    head, _, body = buf.partition(b"\r\n\r\n")
    lines = head.decode("latin-1", "replace").splitlines()
    headers: dict[str, str] = {}
    for line in lines[1:]:
        key, _, value = line.partition(":")
        if key:
            headers[key.strip().lower()] = value.strip()
    return headers, body


def _read_exact(sock, body: bytes, want: int, timeout: float) -> bytes:
    deadline = time.time() + timeout
    while len(body) < want:
        if time.time() > deadline:
            raise TimeoutError(f"only {len(body)} of {want} bytes")
        chunk = sock.recv(65536)
        if not chunk:
            raise ConnectionError("closed early")
        body += chunk
    return body


def fetch(url: str, timeout: float = DEFAULT_TIMEOUT,
          swap: bool = False) -> NowPlaying:
    """One metadata block from a stream. Raises on anything that goes wrong."""
    if not url.lower().startswith(("http://", "https://")):
        raise ValueError("ICY metadata is an HTTP thing; this is not a URL")
    sock = _connect(url, timeout)
    try:
        headers, body = _read_headers(sock, timeout)
        station = headers.get("icy-name", "")
        try:
            metaint = int(headers.get("icy-metaint") or 0)
        except ValueError:
            metaint = 0
        if not 0 < metaint <= MAX_METAINT:
            # Plenty of servers carry no song metadata at all. The station
            # name from the headers is still worth having.
            return NowPlaying(station=station)

        # metaint bytes of audio, then a length byte counting 16-byte units.
        body = _read_exact(sock, body, metaint + 1, timeout)
        length = body[metaint] * 16
        if length:
            body = _read_exact(sock, body, metaint + 1 + length, timeout)
            blob = body[metaint + 1:metaint + 1 + length]
        else:
            blob = b""
    finally:
        try:
            sock.close()
        except OSError:
            pass

    meta = blob.rstrip(b"\x00").decode("utf-8", "replace")
    m = TITLE_RE.search(meta)
    raw = m.group("t").strip() if m else ""
    u = URL_RE.search(meta)
    artist, title = split_title(raw, swap)
    return NowPlaying(station=station, artist=artist, title=title, raw=raw,
                      art=u.group("u").strip() if u else "")


class Poller:
    """Re-reads a stream's metadata on a thread and calls back on changes.

    Only on changes: the PAD channel is retransmitted once a second anyway, so
    pushing an unchanged title into it every poll would be noise.
    """

    def __init__(self, url: str, on_change, interval: float = DEFAULT_INTERVAL,
                 swap: bool = False):
        self.url = url
        self.on_change = on_change
        self.interval = interval
        self.swap = swap
        self.current: NowPlaying | None = None
        self.error: str | None = None
        self.polls = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)

    def set_swap(self, swap: bool) -> None:
        """Re-split the title we already have, without waiting for a poll."""
        self.swap = swap
        if self.current and self.current.raw:
            self.current.artist, self.current.title = split_title(
                self.current.raw, swap)
            self._emit(self.current)

    def _emit(self, np: NowPlaying) -> None:
        try:
            self.on_change(np)
        except Exception as exc:                      # never kill the thread
            self.error = f"metadata callback: {exc}"

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                got = fetch(self.url, swap=self.swap)
                self.polls += 1
                self.error = None
                if (self.current is None or got.raw != self.current.raw
                        or got.station != self.current.station):
                    self.current = got
                    self._emit(got)
            except Exception as exc:
                # A station that drops a connection is normal; keep trying
                # rather than giving up on the whole broadcast.
                self.error = f"{type(exc).__name__}: {exc}"
            self._stop.wait(self.interval)
