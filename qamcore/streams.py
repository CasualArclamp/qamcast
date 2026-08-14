"""The station list: .pls and .m3u playlists on disk, grouped into stations.

A folder of playlists is not a station list. It is a flat pile of files where
one station's 32k, 64k and 128k feeds are three separate entries, and where
the filename and the actual stream frequently disagree -- ``groovesalad130.pls``
serves ``groovesalad-128-aac``. So the rate and codec are read from the *URL*,
never from the filename, and entries that differ only in rate are folded back
into one station with a list of the rates it offers.

Playlists also list mirrors. SomaFM gives six for every feed, which are the
same audio from six hosts; only the first is offered and the rest are kept as
fallbacks.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

# Where the station playlists live. Anything ffmpeg can open still works by
# typing it in; this is only the shortcut list.
DEFAULT_DIR = r"C:\Users\Gaming\Desktop\Radio Stream"

# SomaFM and most Icecast setups encode the feed in the path: name-rate-codec.
# Nothing depends on this matching -- an unrecognised URL simply becomes a
# station with one unlabelled feed.
URL_RE = re.compile(r"/(?P<name>[a-z0-9_]+)-(?P<rate>\d+)-(?P<codec>[a-z0-9]+)\b",
                    re.I)

# "SomaFM: Groove Salad (#3): A nicely chilled plate of..." -> the mirror
# number is noise, and the description belongs on the station, not the feed.
TITLE_RE = re.compile(r"^(?P<name>.*?)\s*\(#\d+\)\s*:?\s*(?P<desc>.*)$")

CODEC_LABELS = {"aac": "AAC", "mp3": "MP3", "opus": "Opus", "flac": "FLAC",
                "ogg": "Ogg Vorbis"}


@dataclass
class Feed:
    """One playable rate of one station."""
    url: str
    mirrors: list[str] = field(default_factory=list)
    bitrate: int | None = None        # bits per second, from the URL
    codec: str | None = None          # "aac", "mp3", ... from the URL
    source: str = ""                  # playlist it came from

    @property
    def label(self) -> str:
        if self.bitrate and self.codec:
            return f"{self.bitrate // 1000}k {CODEC_LABELS.get(self.codec, self.codec.upper())}"
        if self.codec:
            return CODEC_LABELS.get(self.codec, self.codec.upper())
        if self.bitrate:
            return f"{self.bitrate // 1000}k"
        return "default"

    def as_dict(self) -> dict:
        return {"url": self.url, "label": self.label, "bitrate": self.bitrate,
                "codec": self.codec, "mirrors": self.mirrors,
                "source": os.path.basename(self.source)}


@dataclass
class Station:
    key: str
    name: str
    description: str = ""
    feeds: list[Feed] = field(default_factory=list)

    def as_dict(self) -> dict:
        # Cheapest first: on a fixed channel the lowest rate is the one most
        # likely to fit, so it is the sensible default selection.
        feeds = sorted(self.feeds, key=lambda f: (f.bitrate or 1 << 30, f.label))
        return {"key": self.key, "name": self.name,
                "description": self.description,
                "feeds": [f.as_dict() for f in feeds]}


def parse_playlist(path: str) -> list[tuple[str, str]]:
    """(url, title) pairs from a .pls or .m3u file."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError:
        return []

    if path.lower().endswith(".pls"):
        files: dict[int, str] = {}
        titles: dict[int, str] = {}
        for line in text.splitlines():
            # Keys are case-insensitive in practice: real files in this folder
            # use both "NumberOfEntries" and "numberofentries".
            key, _, value = line.partition("=")
            key = key.strip().lower()
            value = value.strip()
            if key.startswith("file") and key[4:].isdigit():
                files[int(key[4:])] = value
            elif key.startswith("title") and key[5:].isdigit():
                titles[int(key[5:])] = value
        return [(files[i], titles.get(i, "")) for i in sorted(files) if files[i]]

    out, pending = [], ""
    for line in text.splitlines():
        line = line.strip()
        if line.upper().startswith("#EXTINF"):
            pending = line.partition(",")[2].strip()
        elif line and not line.startswith("#"):
            out.append((line, pending))
            pending = ""
    return out


def _describe(url: str, title: str) -> tuple[str, str, str, int | None, str | None]:
    """(station key, station name, description, bitrate, codec)."""
    m = URL_RE.search(url)
    rate = codec = None
    if m:
        codec = m.group("codec").lower()
        try:
            rate = int(m.group("rate")) * 1000
        except ValueError:
            rate = None

    # Station name first, everything after it a description. Three separators
    # cover what real playlists use, and a whole title as the name is a
    # dropdown entry nobody can read.
    name, desc = title, ""
    tm = TITLE_RE.match(title)
    if tm:
        name, desc = tm.group("name"), tm.group("desc")
    elif ":" in title:
        name, _, desc = (p.strip() for p in title.partition(":"))
    elif " - " in title:
        name, _, desc = (p.strip() for p in title.partition(" - "))

    # Group by the name in the URL when there is one -- that is what actually
    # identifies the station across its rates. Titles differ per mirror and
    # per feed, so grouping on them would split a station into several.
    key = (m.group("name").lower() if m
           else re.sub(r"[^a-z0-9]+", "", (name or url).lower())[:40] or url)
    return key, (name or key).strip(), desc.strip(), rate, codec


def load(directory: str | None = None) -> list[Station]:
    """Every station found in ``directory``, rates folded together."""
    directory = directory or DEFAULT_DIR
    if not os.path.isdir(directory):
        return []

    stations: dict[str, Station] = {}
    seen: set[tuple[str, str]] = set()
    for entry in sorted(os.listdir(directory)):
        if not entry.lower().endswith((".pls", ".m3u", ".m3u8")):
            continue
        path = os.path.join(directory, entry)
        urls = parse_playlist(path)
        if not urls:
            continue

        # All entries in one playlist are mirrors of a single feed.
        url, title = urls[0]
        mirrors = [u for u, _ in urls[1:]]
        key, name, desc, rate, codec = _describe(url, title)

        st = stations.get(key)
        if st is None:
            st = stations[key] = Station(key=key, name=name, description=desc)
        elif desc and not st.description:
            st.description = desc

        # Two playlists can name the same feed; keep one.
        sig = (key, url)
        if sig in seen:
            continue
        seen.add(sig)
        st.feeds.append(Feed(url=url, mirrors=mirrors, bitrate=rate,
                             codec=codec, source=path))

    return sorted(stations.values(), key=lambda s: s.name.lower())


def as_dicts(directory: str | None = None) -> list[dict]:
    return [s.as_dict() for s in load(directory)]
