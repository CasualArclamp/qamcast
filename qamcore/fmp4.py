"""Fragmented MP4, for carrying xHE-AAC packets to and from ffmpeg.

This is the same job ogg.py does for Opus, forced by a different constraint.
Opus needs a container because ffmpeg will not emit or accept it bare.
xHE-AAC needs *this particular* container because it is the only streamable
one ffmpeg will decode it from:

    MP4        decodes.
    LOAS/LATM  does not. The muxer refuses outright -- "Muxing MPEG-4 AOT 42
               in LATM is not supported" -- and the aac_latm decoder cannot
               parse a USAC AudioSpecificConfig either. Measured, by feeding
               it a config ffmpeg itself reads happily out of an MP4, rebuilt
               at every plausible bit length: all nine were rejected.
    ADTS       cannot describe USAC at all; its profile field has no value
               meaning USAC.

A plain MP4 is not streamable -- its sample table sits in a moov box that can
only be written once the last sample is known. A *fragmented* MP4 is, and is
what DASH and HLS have carried for a decade: an init segment describing the
track, then one self-contained moof+mdat per batch of samples. It decodes
from a pipe with no seeking, which is the property that matters here.

Only what a single-track audio stream needs is implemented.
"""

from __future__ import annotations

import struct

MATRIX = struct.pack(">9i", 0x10000, 0, 0, 0, 0x10000, 0, 0, 0, 0x40000000)

# tfhd flags. default-base-is-moof makes a fragment's data offsets relative to
# its own moof, so a fragment means the same thing wherever it lands in the
# stream -- which, for a broadcast with no beginning, is the only thing it can
# usefully mean.
TFHD_DEFAULT_SAMPLE_DURATION = 0x000008
TFHD_DEFAULT_SAMPLE_SIZE = 0x000010
TFHD_DEFAULT_BASE_IS_MOOF = 0x020000
TRUN_DATA_OFFSET = 0x000001
TRUN_FIRST_SAMPLE_FLAGS = 0x000004
TRUN_SAMPLE_DURATION = 0x000100
TRUN_SAMPLE_SIZE = 0x000200
TRUN_SAMPLE_FLAGS = 0x000400
TRUN_SAMPLE_OFFSET = 0x000800


def box(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I4s", len(payload) + 8, kind) + payload


def full(kind: bytes, version: int, flags: int, payload: bytes) -> bytes:
    return box(kind, struct.pack(">I", (version << 24) | flags) + payload)


def _descriptor(tag: int, payload: bytes) -> bytes:
    """An MPEG-4 descriptor, with its length in the expandable base-128 form."""
    length = bytearray()
    n = len(payload)
    while True:
        length.insert(0, n & 0x7F)
        n >>= 7
        if not n:
            break
    for i in range(len(length) - 1):
        length[i] |= 0x80
    return bytes([tag]) + bytes(length) + payload


def _esds(asc: bytes, bitrate: int) -> bytes:
    """The decoder configuration box, carrying the AudioSpecificConfig.

    objectTypeIndication 0x40 is "MPEG-4 audio" -- it does not name USAC, and
    nothing here needs to. Which MPEG-4 audio object type this is lives inside
    the ASC, where a decoder reads it as the first field.
    """
    dsi = _descriptor(0x05, asc)
    dec = _descriptor(0x04, struct.pack(">BB", 0x40, 0x15)
                      + b"\x00\x00\x00"                        # bufferSizeDB
                      + struct.pack(">II", bitrate, bitrate) + dsi)
    sl = _descriptor(0x06, b"\x02")
    es = _descriptor(0x03, struct.pack(">HB", 1, 0) + dec + sl)
    return full(b"esds", 0, 0, es)


class Writer:
    """Builds a fragmented MP4 stream from access units, for a decoder.

    ``init_segment()`` once, then ``fragment()`` per batch. The decode time
    advances by ``frame_samples`` per access unit, which is what lets a
    decoder present them at the right rate; it is not used to find them.
    """

    def __init__(self, asc: bytes, sample_rate: int = 48000, channels: int = 2,
                 frame_samples: int = 2048, bitrate: int = 64000):
        self.asc = asc
        self.sample_rate = sample_rate
        self.channels = channels
        self.frame_samples = frame_samples
        self.bitrate = bitrate
        self.sequence = 1
        self.decode_time = 0

    def init_segment(self) -> bytes:
        ftyp = box(b"ftyp", b"iso5" + struct.pack(">I", 512) + b"iso5iso6mp41")

        mvhd = full(b"mvhd", 0, 0,
                    struct.pack(">IIII", 0, 0, self.sample_rate, 0)
                    + struct.pack(">IHH", 0x00010000, 0x0100, 0)
                    + b"\x00" * 8 + MATRIX + b"\x00" * 24
                    + struct.pack(">I", 2))
        tkhd = full(b"tkhd", 0, 3,
                    struct.pack(">IIIII", 0, 0, 1, 0, 0)
                    + b"\x00" * 8 + struct.pack(">HHHH", 0, 1, 0x0100, 0)
                    + MATRIX + struct.pack(">II", 0, 0))
        mdhd = full(b"mdhd", 0, 0,
                    struct.pack(">IIII", 0, 0, self.sample_rate, 0)
                    + struct.pack(">HH", 0x55C4, 0))
        hdlr = full(b"hdlr", 0, 0,
                    struct.pack(">I4s", 0, b"soun") + b"\x00" * 12
                    + b"QAMcast\x00")
        smhd = full(b"smhd", 0, 0, struct.pack(">hH", 0, 0))
        dref = full(b"dref", 0, 0,
                    struct.pack(">I", 1) + full(b"url ", 0, 1, b""))
        dinf = box(b"dinf", dref)

        mp4a = box(b"mp4a",
                   b"\x00" * 6 + struct.pack(">H", 1)
                   + struct.pack(">HHI", 0, 0, 0)
                   + struct.pack(">HHHH", self.channels, 16, 0, 0)
                   + struct.pack(">I", self.sample_rate << 16)
                   + _esds(self.asc, self.bitrate))
        stsd = full(b"stsd", 0, 0, struct.pack(">I", 1) + mp4a)
        # Empty sample tables. Everything about the samples arrives later, in
        # the fragments; these exist because the box order is not optional.
        stbl = box(b"stbl", stsd
                   + full(b"stts", 0, 0, struct.pack(">I", 0))
                   + full(b"stsc", 0, 0, struct.pack(">I", 0))
                   + full(b"stsz", 0, 0, struct.pack(">II", 0, 0))
                   + full(b"stco", 0, 0, struct.pack(">I", 0)))
        minf = box(b"minf", smhd + dinf + stbl)
        mdia = box(b"mdia", mdhd + hdlr + minf)
        trak = box(b"trak", tkhd + mdia)
        trex = full(b"trex", 0, 0,
                    struct.pack(">IIIII", 1, 1, self.frame_samples, 0, 0))
        moov = box(b"moov", mvhd + trak + box(b"mvex", trex))
        return ftyp + moov

    def fragment(self, aus: list[bytes]) -> bytes:
        if not aus:
            return b""
        mfhd = full(b"mfhd", 0, 0, struct.pack(">I", self.sequence))
        tfhd = full(b"tfhd", 0,
                    TFHD_DEFAULT_BASE_IS_MOOF | TFHD_DEFAULT_SAMPLE_DURATION,
                    struct.pack(">II", 1, self.frame_samples))
        tfdt = full(b"tfdt", 1, 0, struct.pack(">Q", self.decode_time))
        trun = full(b"trun", 0, TRUN_DATA_OFFSET | TRUN_SAMPLE_SIZE,
                    struct.pack(">Ii", len(aus), 0)
                    + b"".join(struct.pack(">I", len(a)) for a in aus))
        moof = box(b"moof", mfhd + box(b"traf", tfhd + tfdt + trun))
        # The data offset is measured from the start of the moof, so it can
        # only be filled in once the moof is the size it is going to be.
        at = moof.index(b"trun") + 12
        moof = moof[:at] + struct.pack(">i", len(moof) + 8) + moof[at + 4:]

        self.sequence += 1
        self.decode_time += self.frame_samples * len(aus)
        return moof + box(b"mdat", b"".join(aus))


def _boxes(data: bytes, start: int, end: int):
    """Walk the boxes in ``data[start:end]``, yielding (kind, body)."""
    off = start
    while off + 8 <= end:
        size, kind = struct.unpack(">I4s", data[off:off + 8])
        head = 8
        if size == 1:
            size = struct.unpack(">Q", data[off + 8:off + 16])[0]
            head = 16
        if size < head or off + size > end:
            return
        yield kind, data[off + head:off + size]
        off += size


def _find(data: bytes, path: tuple[bytes, ...]) -> bytes | None:
    for kind, body in _boxes(data, 0, len(data)):
        if kind == path[0]:
            return body if len(path) == 1 else _find(body, path[1:])
    return None


def asc_from_esds(esds: bytes) -> bytes | None:
    """The AudioSpecificConfig inside an esds box, by walking descriptors."""
    stack = [(4, len(esds))]                              # past version, flags
    while stack:
        pos, end = stack.pop()
        while pos < end:
            tag = esds[pos]
            pos += 1
            size = 0
            while pos < end:
                byte = esds[pos]
                pos += 1
                size = (size << 7) | (byte & 0x7F)
                if not byte & 0x80:
                    break
            if pos + size > end:
                return None
            if tag == 0x05:
                return esds[pos:pos + size]
            if tag == 0x03:
                stack.append((pos + 3, pos + size))       # ES_ID and flags
            elif tag == 0x04:
                stack.append((pos + 13, pos + size))      # decoder config
            pos += size
    return None


class Reader:
    """Streaming fragmented-MP4 parser. Feed bytes, take access units out.

    Used on the transmit side: ffmpeg will remux an existing xHE-AAC source
    into this and into nothing else that can be read from a pipe, so this is
    how packets are recovered for passthrough.
    """

    def __init__(self) -> None:
        self._buf = bytearray()
        self._sizes: list[int] = []
        self.config: bytes | None = None
        self.frame_samples: int | None = None

    def feed(self, data: bytes) -> list[bytes]:
        self._buf.extend(data)
        out: list[bytes] = []
        while True:
            if len(self._buf) < 8:
                break
            size, kind = struct.unpack(">I4s", self._buf[:8])
            head = 8
            if size == 1:
                if len(self._buf) < 16:
                    break
                size = struct.unpack(">Q", self._buf[8:16])[0]
                head = 16
            if size < head:
                del self._buf[:1]              # not a box; step past it
                continue
            if len(self._buf) < size:
                break
            body = bytes(self._buf[head:size])
            del self._buf[:size]

            if kind == b"moov":
                self._read_moov(body)
            elif kind == b"moof":
                self._read_moof(body)
            elif kind == b"mdat" and self._sizes:
                pos = 0
                for n in self._sizes:
                    if pos + n > len(body):
                        break
                    out.append(body[pos:pos + n])
                    pos += n
                self._sizes = []
        return out

    def _read_moov(self, body: bytes) -> None:
        stsd = _find(body, (b"trak", b"mdia", b"minf", b"stbl", b"stsd"))
        if stsd is None:
            return
        # stsd is a full box holding an entry count, then sample entries; an
        # audio entry has 28 bytes of its own before any child boxes.
        for _, entry in _boxes(stsd, 8, len(stsd)):
            if len(entry) <= 28:
                continue
            esds = (_find(entry[28:], (b"esds",))
                    or _find(entry[28:], (b"wave", b"esds")))
            if esds is not None:
                self.config = asc_from_esds(esds)
                return

    def _read_moof(self, body: bytes) -> None:
        traf = _find(body, (b"traf",))
        if traf is None:
            return
        default_size = 0
        for kind, child in _boxes(traf, 0, len(traf)):
            if kind == b"tfhd":
                flags = struct.unpack(">I", child[:4])[0] & 0xFFFFFF
                pos = 8                                   # flags and track_ID
                if flags & 0x000001:
                    pos += 8                              # base_data_offset
                if flags & 0x000002:
                    pos += 4                              # sample description
                if flags & TFHD_DEFAULT_SAMPLE_DURATION:
                    self.frame_samples = struct.unpack(">I", child[pos:pos + 4])[0]
                    pos += 4
                if flags & TFHD_DEFAULT_SAMPLE_SIZE:
                    default_size = struct.unpack(">I", child[pos:pos + 4])[0]
            elif kind == b"trun":
                flags = struct.unpack(">I", child[:4])[0] & 0xFFFFFF
                count = struct.unpack(">I", child[4:8])[0]
                pos = 8
                if flags & TRUN_DATA_OFFSET:
                    pos += 4
                if flags & TRUN_FIRST_SAMPLE_FLAGS:
                    pos += 4
                stride = 4 * (bool(flags & TRUN_SAMPLE_DURATION)
                              + bool(flags & TRUN_SAMPLE_SIZE)
                              + bool(flags & TRUN_SAMPLE_FLAGS)
                              + bool(flags & TRUN_SAMPLE_OFFSET))
                sizes = []
                for i in range(count):
                    at = pos + i * stride + 4 * bool(flags & TRUN_SAMPLE_DURATION)
                    if flags & TRUN_SAMPLE_SIZE and at + 4 <= len(child):
                        sizes.append(struct.unpack(">I", child[at:at + 4])[0])
                    else:
                        sizes.append(default_size)
                self._sizes = sizes
