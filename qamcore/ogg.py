"""Minimal Ogg page reader and writer, for carrying Opus packets.

Opus needs this and AAC does not, which is worth stating plainly: ADTS frames
are self-delimiting, so an AAC stream can be sliced into packets by reading
its own headers. Opus has no such framing of its own -- ffmpeg will only emit
it inside a container, and will only decode it from one. So the encoder's Ogg
output is parsed back into bare packets here, and the decoder's input is
rebuilt into Ogg pages.

Only the parts that matter for a single-stream Opus file are implemented.
Multiplexed streams, seeking and chaining are not.
"""

from __future__ import annotations

import functools
import struct

CAPTURE = b"OggS"
FLAG_CONTINUED = 0x01
FLAG_BOS = 0x02
FLAG_EOS = 0x04


@functools.lru_cache(maxsize=1)
def _crc_table() -> list[int]:
    """Ogg's CRC-32: poly 0x04C11DB7, no reflection, no final xor.

    Not the common zlib CRC-32 -- that one reflects input and output. ffmpeg
    rejects pages whose checksum was computed the usual way, which presents as
    a decoder that reads nothing and says nothing.
    """
    table = []
    for i in range(256):
        r = i << 24
        for _ in range(8):
            r = ((r << 1) ^ 0x04C11DB7) & 0xFFFFFFFF if r & 0x80000000 else (r << 1) & 0xFFFFFFFF
        table.append(r)
    return table


def crc32(data: bytes) -> int:
    table = _crc_table()
    crc = 0
    for b in data:
        crc = ((crc << 8) & 0xFFFFFFFF) ^ table[((crc >> 24) & 0xFF) ^ b]
    return crc


def _lacing(length: int) -> bytes:
    """Segment table entries for one packet.

    A packet is described by as many 255s as fit plus a final value below 255,
    so a packet whose length is an exact multiple of 255 needs an explicit
    terminating zero. Omitting it merges that packet into the next one.
    """
    out = bytearray(b"\xff" * (length // 255))
    out.append(length % 255)
    return bytes(out)


class OggWriter:
    """Builds Ogg pages from packets, for feeding a decoder."""

    def __init__(self, serial: int = 0x5141_4D00):
        self.serial = serial & 0xFFFFFFFF
        self.sequence = 0

    def page(self, packets: list[bytes], granule: int = 0,
             bos: bool = False, eos: bool = False) -> bytes:
        segments = bytearray()
        body = bytearray()
        for p in packets:
            segments.extend(_lacing(len(p)))
            body.extend(p)
        if len(segments) > 255:
            raise ValueError("too many segments for one page; split the packets")

        flags = (FLAG_BOS if bos else 0) | (FLAG_EOS if eos else 0)
        header = bytearray()
        header += CAPTURE
        header += bytes([0, flags])
        header += struct.pack("<q", granule)
        header += struct.pack("<I", self.serial)
        header += struct.pack("<I", self.sequence)
        header += b"\x00\x00\x00\x00"          # checksum placeholder
        header += bytes([len(segments)])
        header += segments
        page = bytes(header) + bytes(body)
        crc = crc32(page)
        page = page[:22] + struct.pack("<I", crc) + page[26:]
        self.sequence += 1
        return page

    def pages_for(self, packets: list[bytes], granule: int = 0) -> bytes:
        """Split packets across pages so no page exceeds 255 segments."""
        out = bytearray()
        batch: list[bytes] = []
        used = 0
        for p in packets:
            need = len(_lacing(len(p)))
            if used + need > 255:
                out += self.page(batch, granule)
                batch, used = [], 0
            batch.append(p)
            used += need
        if batch:
            out += self.page(batch, granule)
        return bytes(out)


class OggReader:
    """Streaming page parser. Feed bytes, take whole packets out."""

    def __init__(self) -> None:
        self._buf = bytearray()
        self._partial = bytearray()

    def feed(self, data: bytes) -> list[bytes]:
        self._buf.extend(data)
        return self._drain()

    def _drain(self) -> list[bytes]:
        packets: list[bytes] = []
        while True:
            start = self._buf.find(CAPTURE)
            if start < 0:
                # Keep a tail in case a capture pattern straddles the boundary.
                if len(self._buf) > 3:
                    del self._buf[:len(self._buf) - 3]
                break
            if start:
                del self._buf[:start]
            if len(self._buf) < 27:
                break
            nsegs = self._buf[26]
            if len(self._buf) < 27 + nsegs:
                break
            table = self._buf[27:27 + nsegs]
            body_len = sum(table)
            total = 27 + nsegs + body_len
            if len(self._buf) < total:
                break
            flags = self._buf[5]
            body = bytes(self._buf[27 + nsegs:total])
            del self._buf[:total]

            if not (flags & FLAG_CONTINUED):
                self._partial.clear()
            pos = 0
            for seg in table:
                self._partial.extend(body[pos:pos + seg])
                pos += seg
                if seg < 255:
                    packets.append(bytes(self._partial))
                    self._partial.clear()
        return packets
