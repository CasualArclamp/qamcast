"""Transport: packets, PAD, and the outer-code chain.

Above the frame layer sits a byte stream of self-delimiting packets -- audio,
metadata, codec config, stuffing. Below it sit Reed-Solomon and the
interleaver. This module is the join.

The awkward part is that a receiver joins mid-broadcast and has to find packet
boundaries in a stream it has only just started listening to. The solution is
the one MPEG-TS uses for its section tables: every RS message block begins
with a **pointer byte** giving the offset of the first packet header inside
that block, or 0xFF if none starts there. One byte per 223 or 239 buys
resynchronisation within a single codeword, both at startup and after any
codeword the RS decoder could not repair.

Rate matching is by stuffing. The channel runs at a constant byte rate set by
the MODCOD, the audio encoder does not, and the difference is made up with
stuffing packets rather than by varying the frame size.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

import numpy as np

from . import framing, interleave, profiles, rs
from .profiles import INTERLEAVER_DEPTH_SECONDS, Modcod, Profile

# Packet types. Wire format.
PKT_STUFF = 0x00
PKT_AUDIO = 0x01
PKT_PAD = 0x02
PKT_CONFIG = 0x03

POINTER_NONE = 0xFF
# Gaps are measured modulo the frame counter's range. Taken from framing
# rather than restated here: the two were briefly out of step -- framing said
# 8 bits, this said 10 -- which is precisely the kind of quiet disagreement
# the single-copy rule exists to prevent.
FRAME_COUNT_MOD = framing.FRAME_COUNT_MOD
HEADER_LEN = 3          # type byte + 2-byte length
MAX_PAYLOAD = 0xFFFF


def make_packet(ptype: int, payload: bytes) -> bytes:
    if len(payload) > MAX_PAYLOAD:
        raise ValueError(f"packet payload {len(payload)} exceeds {MAX_PAYLOAD}")
    return bytes([ptype, (len(payload) >> 8) & 0xFF, len(payload) & 0xFF]) + payload


@dataclass
class Pad:
    """Programme-associated data -- what the receiver puts on screen."""

    station: str = ""
    title: str = ""
    artist: str = ""

    def encode(self) -> bytes:
        parts = [s.encode("utf-8")[:255] for s in (self.station, self.title, self.artist)]
        out = bytearray()
        for p in parts:
            out.append(len(p))
            out.extend(p)
        return bytes(out)

    @classmethod
    def decode(cls, data: bytes) -> "Pad | None":
        fields = []
        pos = 0
        for _ in range(3):
            if pos >= len(data):
                return None
            n = data[pos]
            pos += 1
            if pos + n > len(data):
                return None
            fields.append(data[pos:pos + n].decode("utf-8", errors="replace"))
            pos += n
        return cls(*fields)

    def __bool__(self) -> bool:
        return bool(self.station or self.title or self.artist)


# --------------------------------------------------------------------------
# Transmit
# --------------------------------------------------------------------------

class TransmitChain:
    """Packets in, one frame's worth of interleaved bytes out.

    Holds the interleaver, so it also owns the diversity delay. Changing
    MODCOD rebuilds it, which restarts that delay -- see the note in
    :meth:`reconfigure`.
    """

    def __init__(self, profile: Profile, modcod: Modcod,
                 depth: int | None = None):
        self.profile = profile
        self.reconfigure(modcod, depth)

    def reconfigure(self, modcod: Modcod, depth: int | None = None) -> None:
        """Switch MODCOD or interleaver depth, rebuilding the outer chain.

        This discards whatever is in the interleaver, so the receiver sees a
        gap of one interleaver depth. That is acceptable because both of these
        are operator settings -- you pick a bitrate and a depth for the station
        and leave them -- not something that adapts second to second.
        Auto-probe *recommends* a rung; a human applies it.
        """
        self.modcod = modcod
        self.depth = (profiles.INTERLEAVER_DEFAULT_INDEX if depth is None
                      else int(depth))
        self.capacity = self.profile.capacity(modcod)
        branches, increment = profiles.interleaver_geometry(
            self.profile, modcod, self.depth)
        self.interleaver = interleave.Interleaver(branches, increment)
        self.branches = branches
        self._queue = bytearray()        # transport stream awaiting RS
        self._out = bytearray()          # interleaved bytes awaiting a frame
        self._emitted = 0                # total interleaved bytes handed out
        self._block_len = modcod.rs_k - 1
        self._stream_pos = 0             # stream offset of self._queue[0]
        self._boundaries: deque[int] = deque()  # stream offsets of packet starts

    # -- input ----------------------------------------------------------

    def push(self, ptype: int, payload: bytes) -> None:
        self._boundaries.append(self._stream_pos + len(self._queue))
        self._queue.extend(make_packet(ptype, payload))

    def push_audio(self, frame: bytes) -> None:
        self.push(PKT_AUDIO, frame)

    def push_pad(self, pad: Pad) -> None:
        self.push(PKT_PAD, pad.encode())

    def push_config(self, config: bytes) -> None:
        self.push(PKT_CONFIG, config)

    @property
    def backlog(self) -> int:
        """Transport bytes waiting. A backlog that only grows means the codec
        is outrunning the channel."""
        return len(self._queue)

    # -- output ---------------------------------------------------------

    def _build_block(self) -> np.ndarray:
        """One RS message block: pointer byte plus ``rs_k - 1`` stream bytes.

        The pointer is the offset of the first packet *header* that begins
        inside this block, tracked against absolute stream position so that a
        packet spanning several blocks does not confuse it. 0xFF means no
        packet starts here, which happens whenever one payload is long enough
        to fill a whole block on its own.
        """
        need = self._block_len
        while len(self._queue) < need:
            # Top up with stuffing. A short fall of 1 or 2 bytes cannot be
            # expressed as a packet, so this overshoots and the surplus simply
            # carries into the next block.
            short = need - len(self._queue)
            self.push(PKT_STUFF, bytes(max(0, short - HEADER_LEN)))

        block = bytes(self._queue[:need])
        del self._queue[:need]
        start = self._stream_pos
        self._stream_pos += need

        while self._boundaries and self._boundaries[0] < start:
            self._boundaries.popleft()
        pointer = POINTER_NONE
        if self._boundaries and self._boundaries[0] < start + need:
            pointer = self._boundaries[0] - start
        return np.frombuffer(bytes([pointer]) + block, dtype=np.uint8)

    def next_frame(self) -> tuple[np.ndarray, int, int]:
        """Return ``(payload_bytes, il_phase, rs_phase)`` for the next frame."""
        want = self.capacity.frame_bytes
        while len(self._out) < want:
            msg = self._build_block()
            codeword = rs.encode(msg, self.modcod.rs_k)
            self._out.extend(self.interleaver.process(codeword).tobytes())
        payload = np.frombuffer(bytes(self._out[:want]), dtype=np.uint8)
        del self._out[:want]
        il_phase = self._emitted % self.branches
        rs_phase = self._emitted % rs.N
        self._emitted += want
        return payload, il_phase, rs_phase


# --------------------------------------------------------------------------
# Receive
# --------------------------------------------------------------------------

@dataclass
class ReceiveStats:
    codewords: int = 0
    rs_failed: int = 0
    rs_corrected: int = 0
    packets: int = 0
    resyncs: int = 0          # times alignment was rebuilt from scratch
    bridged_frames: int = 0   # frames lost but bridged without resynchronising

    @property
    def block_error_rate(self) -> float:
        return self.rs_failed / self.codewords if self.codewords else 0.0


@dataclass
class ReceiveChain:
    """Interleaved bytes in, packets out.

    Mirrors :class:`TransmitChain`. Alignment comes entirely from the phases
    in the frame header, so this can be started at any point in a broadcast
    and will produce correct packets once the interleaver has filled.
    """

    profile: Profile
    modcod: Modcod
    depth: int | None = None
    stats: ReceiveStats = field(default_factory=ReceiveStats)

    def __post_init__(self) -> None:
        self.reconfigure(self.modcod, self.depth)

    def reconfigure(self, modcod: Modcod, depth: int | None = None) -> None:
        self.modcod = modcod
        self.depth = (profiles.INTERLEAVER_DEFAULT_INDEX if depth is None
                      else int(depth))
        self.capacity = self.profile.capacity(modcod)
        branches, increment = profiles.interleaver_geometry(
            self.profile, modcod, self.depth)
        self.deinterleaver = interleave.Deinterleaver(branches, increment)
        self.branches = branches
        self.total_delay = interleave.delay_bytes(branches, increment)
        self._stream = bytearray()       # deinterleaved bytes awaiting RS
        self._packets = bytearray()      # message bytes awaiting packet parse
        self._synced = False
        self._primed = False
        self._last_count: int | None = None
        self._discard = 0                # invalid prefix while interleaver fills
        self._skip = 0                   # bytes to the first codeword boundary

    @property
    def fill_seconds(self) -> float:
        """How long until the interleaver has filled and audio can start."""
        rate = self.capacity.coded_byte_rate
        return self.total_delay / rate if rate else 0.0

    @property
    def fill_fraction(self) -> float:
        """How full the deinterleaver is, 0 to 1.

        Exposed rather than left to callers to infer from private state: the
        UI needs it for its progress bar, and the attribute it used to read
        stopped existing in a refactor without anything noticing, because a
        bar that never moves looks like a link that has not locked.
        """
        if not self._primed:
            return 0.0
        if self.total_delay <= 0:
            return 1.0
        return 1.0 - min(1.0, self._discard / self.total_delay)

    def _prime(self, il_phase: int, rs_phase: int) -> None:
        """Align to the transmitter and start the deinterleaver from empty.

        Output byte j of the deinterleaver is RS-stream byte
        (rs_phase + j - total_delay). Everything before j = total_delay is the
        deinterleaver's own empty FIFOs draining, and the first valid byte
        after that sits at RS phase ``rs_phase``.
        """
        self.deinterleaver.reset()
        self.deinterleaver._phase = il_phase % self.branches
        self._discard = self.total_delay
        self._skip = (rs.N - rs_phase % rs.N) % rs.N
        self._stream.clear()
        self._packets.clear()
        self._synced = False
        self._primed = True

    def push_frame(self, payload: np.ndarray, il_phase: int, rs_phase: int,
                   frame_count: int | None = None) -> list[tuple[int, bytes]]:
        """Feed one frame; return whatever packets completed.

        The header states the interleaver and RS phases on *every* frame, and
        this checks them on every frame. Reading them only once, at the point
        of joining, is enough right up until the receiver misses a frame --
        after which the transmitter's phase has moved on and the
        deinterleaver's has not, the two disagree forever, and nothing decodes
        again until the whole receiver is restarted.

        A gap is normally bridged rather than resynchronised. Feeding zeros
        for the frames that went missing keeps every branch of the
        deinterleaver aligned and hands the damage to Reed-Solomon, which is
        precisely the job the deep interleaver exists to make possible.
        Resynchronising instead would throw away the several seconds of good
        history still sitting in the FIFOs and go silent while they refill.

        That only holds while some of that history is still valid. Once the
        gap exceeds the interleaver's own depth every branch is stale anyway,
        and a clean restart is both cheaper and more honest.
        """
        payload = np.asarray(payload, dtype=np.uint8).ravel()
        frame_bytes = max(1, len(payload))

        if not self._primed:
            self._prime(il_phase, rs_phase)
        else:
            expected = self.deinterleaver._phase
            actual = il_phase % self.branches
            if actual != expected:
                gap = None
                if frame_count is not None and self._last_count is not None:
                    gap = (frame_count - self._last_count - 1) % FRAME_COUNT_MOD
                max_bridge = self.total_delay // frame_bytes
                if gap is not None and 0 < gap <= max_bridge:
                    filler = np.zeros(gap * frame_bytes, dtype=np.uint8)
                    self._stream.extend(
                        self.deinterleaver.process(filler).tobytes())
                    self.stats.bridged_frames += gap
                    if self.deinterleaver._phase != actual:
                        # The counter and the phase disagree, so one of them is
                        # wrong; trust neither and start over.
                        self._prime(il_phase, rs_phase)
                        self.stats.resyncs += 1
                else:
                    self._prime(il_phase, rs_phase)
                    self.stats.resyncs += 1

        self._last_count = frame_count
        self._stream.extend(self.deinterleaver.process(payload).tobytes())
        return self._drain()

    def _drain(self) -> list[tuple[int, bytes]]:
        for attr in ("_discard", "_skip"):
            pending = getattr(self, attr)
            if pending:
                take = min(pending, len(self._stream))
                del self._stream[:take]
                setattr(self, attr, pending - take)
                if getattr(self, attr):
                    return []
        while len(self._stream) >= rs.N:
            block = np.frombuffer(bytes(self._stream[:rs.N]), dtype=np.uint8)
            del self._stream[:rs.N]
            msg, nerr = rs.decode(block, self.modcod.rs_k)
            self.stats.codewords += 1
            if nerr < 0:
                self.stats.rs_failed += 1
                # Lost block: drop what we were assembling and wait for the
                # next pointer rather than splicing corrupt bytes into a packet.
                self._packets.clear()
                self._synced = False
                continue
            self.stats.rs_corrected += nerr
            pointer = int(msg[0])
            body = msg[1:].tobytes()
            if not self._synced:
                if pointer == POINTER_NONE:
                    continue
                if pointer >= len(body):
                    continue
                self._packets = bytearray(body[pointer:])
                self._synced = True
            else:
                self._packets.extend(body)
        return self._parse_packets()

    def _parse_packets(self) -> list[tuple[int, bytes]]:
        found: list[tuple[int, bytes]] = []
        while len(self._packets) >= HEADER_LEN:
            ptype = self._packets[0]
            length = (self._packets[1] << 8) | self._packets[2]
            if ptype > PKT_CONFIG:
                # Not a packet header we recognise -- alignment is gone.
                self._synced = False
                self._packets.clear()
                break
            if len(self._packets) < HEADER_LEN + length:
                break
            payload = bytes(self._packets[HEADER_LEN:HEADER_LEN + length])
            del self._packets[:HEADER_LEN + length]
            if ptype != PKT_STUFF:
                found.append((ptype, payload))
                self.stats.packets += 1
        return found
