"""Frame geometry: preamble, header, pilots, and the symbol-level assembly.

A frame is laid out as

    [ preamble 64 ] [ header 64 ] [ pilot, data x (spacing-1) ] x groups

and the whole design answers one question: **a receiver that switches on in
the middle of an ongoing broadcast must be able to work out everything it
needs from the next frame it sees.** There is no return path, no handshake and
no retransmission, so nothing may depend on having heard the start.

That forces three things.

The *preamble* is a constant-modulus Zadoff-Chu sequence with a sharp
autocorrelation peak, so acquisition is a correlation search rather than a
guess, and it repeats every frame rather than once at the start.

The *header slot* carries the MODCOD as one of 32 orthogonal BPSK codewords,
recovered by correlation. A receiver cannot demodulate the payload until it
knows the MODCOD, so that field cannot itself depend on the payload -- and
being the thing everything else waits on, it is worth 18 dB of processing
gain for no extra symbols.

Everything else -- codec, flags, interleaver and RS phases, frame counter --
rides *inside* the payload, protected by the same coding as the audio. It
carries the phases outright rather than leaving the receiver to derive them
from a counter, which buys immunity to counter wraps, to joining at an
arbitrary point, and to the whole class of bugs where two ends disagree about
a modular arithmetic convention.
"""

from __future__ import annotations

import functools

import numpy as np

from . import constellation, conv, profiles
from .profiles import HEADER_SYMBOLS, PREAMBLE_SYMBOLS, Modcod, Profile

# Bumped to 2 when the signalling block gained the interleaver depth and grew
# from six bytes to seven. The layout change alone would already make an old
# and a new build fail to talk -- the CRC covers the whole body -- but failing
# on an explicit version check says why.
WIRE_VERSION = 2

# Codec identifiers, wire format. Appending is safe; renumbering is not.
CODEC_OPUS = 0
CODEC_HE_AAC_V2 = 1
CODEC_HE_AAC_V1 = 2
CODEC_AAC_LC = 3
# MPEG-D USAC. Carried in LOAS/LATM rather than ADTS -- see split_loas in
# codec.py: the ADTS header's two-bit profile field covers the MPEG-2 AAC
# profiles and has no value meaning USAC, so ADTS cannot describe this stream
# at all. The field is three bits wide, so 5, 6 and 7 remain.
CODEC_XHE_AAC = 4
CODEC_NAMES = {
    CODEC_OPUS: "Opus",
    CODEC_HE_AAC_V2: "HE-AACv2",
    CODEC_HE_AAC_V1: "HE-AACv1",
    CODEC_AAC_LC: "AAC-LC",
    CODEC_XHE_AAC: "xHE-AAC",
}

FLAG_CONFIG = 0x1   # a codec config packet starts in this frame
FLAG_PAD = 0x2      # a PAD packet starts in this frame
# Energy dispersal off, for looking at what it does. See scramble() -- with it
# on, a payload of digital silence is indistinguishable from one of music; with
# it off the same silence becomes a comb of tones you can watch on the
# waterfall. Signalled rather than agreed, so it can be flipped mid-broadcast
# and the receiver follows within a frame.
#
# It disperses the *payload* only. The signalling block stays scrambled always,
# and has to: this flag lives inside it, so a receiver that needed the flag
# before it could read the block would have nowhere to start.
FLAG_NOSCRAMBLE = 0x4

# Control information is split in two, and the split is the point.
#
# **MODCOD travels as a BPSK codeword** in the header slot: one of 32
# orthogonal length-64 sequences, recovered by correlating against all of them
# and taking the largest. That is 18 dB of processing gain for the one field
# that gates everything, and it fits in the symbols the old header already
# used, so it costs no capacity at all.
#
# **Everything else travels inside the frame payload**, protected by the same
# Reed-Solomon and convolutional coding as the audio.
#
# The old arrangement put all of it in a QPSK rate-1/2 header with nothing but
# a CRC behind it, which made the header weaker than the payload it described.
# Measured, that put a floor of about 7 dB EVM under every rung of the ladder:
# the four most rugged MODCODs could not be used, because the header failed
# before their payload would have. Nothing now gates a frame except the FEC
# the payload carries anyway.

# The interleaver depth rides here rather than in the link key, and that split
# is the same one as everywhere else: the key carries what a receiver needs
# *before* it can hear anything, and the signalling carries what it needs to
# decode what it has already heard. The depth is the second kind -- it changes
# nothing about the waveform, and the block it lives in is protected by the
# same FEC as the payload it describes. So the receiver follows the
# transmitter's choice with no dial of its own, exactly as it does for MODCOD.
#
# The field total must be a whole number of bytes: the body is packed with
# np.packbits and the CRC is appended to it. Adding two bits took 32 to 34, so
# it goes to 40 and the remaining six are reserved -- they cost one byte per
# frame, which is 0.03% at FM44 and under half a per cent at the very bottom of
# the ladder, and buy room for the next field without another format break.
SIGNALLING_FIELDS: tuple[tuple[str, int], ...] = (
    ("version", 2),
    ("codec", 3),
    ("flags", 3),
    ("il_phase", 8),
    ("rs_phase", 8),
    ("frame_count", 8),
    ("interleaver", 2),
    ("reserved", 6),
)
SIGNALLING_BITS = sum(w for _, w in SIGNALLING_FIELDS)   # 40
SIGNALLING_CRC_BITS = 16
SIGNALLING_BYTES = (SIGNALLING_BITS + SIGNALLING_CRC_BITS) // 8   # 7
assert SIGNALLING_BITS % 8 == 0, "signalling body must be whole bytes"

# Frame counter width, which sets how large a gap the receiver can measure
# unambiguously. 8 bits is 256 frames -- a minute or more at any profile's
# frame rate, and far beyond the interleaver depth that bounds bridging.
FRAME_COUNT_MOD = 1 << 8

MODCOD_CODEWORDS = 32   # 5 bits of MODCOD index

# Fixed scrambler seed for the signalling block; see build_frame.
SIGNALLING_SEED = 0



# --------------------------------------------------------------------------
# MODCOD codeword
# --------------------------------------------------------------------------

@functools.lru_cache(maxsize=None)
def modcod_codebook(length: int = HEADER_SYMBOLS) -> np.ndarray:
    """``MODCOD_CODEWORDS`` orthogonal BPSK sequences, shape (32, length).

    Walsh-Hadamard rows, whitened by a fixed PRBS. The elementwise +-1
    multiply leaves every inner product untouched -- the set stays exactly
    orthogonal -- while removing the DC-heavy, tone-like structure raw
    Hadamard rows have, which would otherwise put visible lines in the
    transmitted spectrum.
    """
    n = 1
    h = np.ones((1, 1))
    while n < length:
        h = np.block([[h, h], [h, -h]])
        n *= 2
    reg = 0x2F
    prbs = np.empty(length)
    for i in range(length):
        bit = ((reg >> 5) ^ (reg >> 4)) & 1
        reg = ((reg << 1) | bit) & 0x3F
        prbs[i] = 1.0 if bit else -1.0
    return h[:MODCOD_CODEWORDS, :length] * prbs


def encode_modcod(index: int, length: int = HEADER_SYMBOLS) -> np.ndarray:
    """The BPSK codeword announcing ``index``, as unit-energy symbols.

    ``length`` is fixed at HEADER_SYMBOLS on the single-carrier path, where the
    codeword has a slot of its own. OFDM lays it across the subcarriers
    instead, and a narrow geometry has fewer of those than 64, so it asks for
    a shorter one -- see ofdm._modcod_length for which lengths keep the set
    orthogonal.
    """
    if not 0 <= index < MODCOD_CODEWORDS:
        raise ValueError(f"MODCOD index {index} outside 0..{MODCOD_CODEWORDS - 1}")
    book = modcod_codebook(length)
    if index >= len(book):
        raise ValueError(
            f"a {length}-symbol codeword carries only {len(book)} MODCODs"
        )
    return book[index].astype(np.complex128)


def detect_modcod(symbols: np.ndarray) -> tuple[int, float]:
    """Recover the MODCOD index, and how clearly it won.

    Correlates against the whole codebook and takes the largest magnitude.
    Magnitude rather than real part because a residual phase error of more
    than 90 degrees would otherwise flip the sign and pick nothing; the set
    contains no codeword and its own negation, so nothing is lost by ignoring
    the sign.

    The second return value is the winner's margin over the runner-up,
    normalised. Near 1 means unambiguous; near 0 means the correlation peak
    is not really a peak, and the caller should treat the frame as unlocked.
    """
    book = modcod_codebook(len(symbols))
    corr = np.abs(book @ np.conj(symbols)) / len(symbols)
    order = np.argsort(corr)[::-1]
    best = float(corr[order[0]])
    second = float(corr[order[1]]) if len(order) > 1 else 0.0
    margin = (best - second) / best if best > 1e-9 else 0.0
    return int(order[0]), margin


# --------------------------------------------------------------------------
# In-payload signalling
# --------------------------------------------------------------------------


# --------------------------------------------------------------------------
# Preamble and pilots
# --------------------------------------------------------------------------

ZC_ROOT = 25  # coprime to 64, so the sequence has the full-length property


@functools.lru_cache(maxsize=None)
def preamble(length: int = PREAMBLE_SYMBOLS) -> np.ndarray:
    """Zadoff-Chu sequence, unit modulus.

    Constant modulus matters twice over: it keeps the preamble from dominating
    the peak-to-average ratio, and it makes the correlation peak depend on
    phase alignment alone rather than on amplitude.
    """
    n = np.arange(length)
    return np.exp(-1j * np.pi * ZC_ROOT * n * (n + 1) / length)


@functools.lru_cache(maxsize=None)
def pilot_sequence(count: int) -> np.ndarray:
    """Known QPSK symbols for equaliser tracking.

    Driven by a PRBS rather than a constant: a repeating pilot value would put
    a discrete line in the spectrum at the pilot rate, which wastes power and
    is exactly the sort of thing that shows up in someone's spectrum analyser
    as an unexplained carrier.
    """
    reg = 0x1FF
    bits = np.empty(2 * count, dtype=np.uint8)
    for i in range(2 * count):
        bit = ((reg >> 8) ^ (reg >> 4)) & 1
        reg = ((reg << 1) | bit) & 0x1FF
        bits[i] = bit
    return constellation.modulate(bits, 2)


# --------------------------------------------------------------------------
# Scrambler
# --------------------------------------------------------------------------

@functools.lru_cache(maxsize=None)
def _scramble_mask(length: int, seed: int) -> np.ndarray:
    """PRBS byte mask, x^15 + x^14 + 1, seeded per frame.

    Energy dispersal. Compressed audio is already high-entropy, but stuffing
    runs and digital silence are not, and a long constant run turns into a
    discrete tone on the air.
    """
    reg = seed & 0x7FFF
    if reg == 0:
        reg = 0x4A80
    out = np.empty(length, dtype=np.uint8)
    for i in range(length):
        byte = 0
        for _ in range(8):
            bit = ((reg >> 14) ^ (reg >> 13)) & 1
            reg = ((reg << 1) | bit) & 0x7FFF
            byte = (byte << 1) | bit
        out[i] = byte
    return out


def scramble(data: np.ndarray, frame_count: int) -> np.ndarray:
    """Apply (and, being XOR, also remove) energy dispersal."""
    data = np.asarray(data, dtype=np.uint8).ravel()
    return data ^ _scramble_mask(len(data), 0x4A80 ^ (frame_count & 0x3FF))


# --------------------------------------------------------------------------
# Header
# --------------------------------------------------------------------------

def crc16(data: np.ndarray | bytes) -> int:
    """CRC-16-CCITT, poly 0x1021, init 0xFFFF."""
    crc = 0xFFFF
    for b in bytes(data):
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


class Header:
    """Per-frame control block. Everything a receiver needs to decode the
    payload it is attached to."""

    __slots__ = ("modcod", "codec", "flags", "il_phase", "rs_phase",
                 "frame_count", "version", "interleaver")

    def __init__(self, modcod: int, codec: int, il_phase: int, rs_phase: int,
                 frame_count: int, flags: int = 0, version: int = WIRE_VERSION,
                 interleaver: int = 0):
        self.modcod = modcod
        self.codec = codec
        self.flags = flags
        self.il_phase = il_phase
        self.rs_phase = rs_phase
        self.frame_count = frame_count
        self.version = version
        # Index into profiles.INTERLEAVER_CHOICES. Not the depth in seconds:
        # the seconds are a table both ends already share, and an index is two
        # bits where a duration would be far more.
        self.interleaver = interleaver

    def __repr__(self) -> str:
        return (
            f"Header(modcod={self.modcod}, codec={CODEC_NAMES.get(self.codec, '?')}, "
            f"il={self.il_phase}, rs={self.rs_phase}, n={self.frame_count}, "
            f"depth={self.interleaver}, flags={self.flags:#x})"
        )

    def to_signalling(self) -> np.ndarray:
        """The six bytes that ride inside the payload. MODCOD is not among
        them -- it travels as the BPSK codeword."""
        values = {
            "version": self.version, "codec": self.codec, "flags": self.flags,
            "il_phase": self.il_phase, "rs_phase": self.rs_phase,
            "frame_count": self.frame_count, "interleaver": self.interleaver,
            "reserved": 0,
        }
        bits: list[int] = []
        for name, width in SIGNALLING_FIELDS:
            v = int(values[name])
            if not 0 <= v < (1 << width):
                raise ValueError(f"signalling field {name}={v} does not fit in {width} bits")
            bits.extend((v >> (width - 1 - i)) & 1 for i in range(width))
        payload = np.packbits(np.array(bits, dtype=np.uint8))
        crc = crc16(payload.tobytes())
        return np.concatenate([payload, np.array([crc >> 8, crc & 0xFF], dtype=np.uint8)])

    @classmethod
    def from_signalling(cls, data: np.ndarray, modcod: int) -> "Header | None":
        """Parse and check. Returns None on CRC failure -- a bad block costs
        one frame, which is cheaper than trusting it."""
        data = np.asarray(data, dtype=np.uint8).ravel()
        if len(data) < SIGNALLING_BYTES:
            return None
        body = data[:SIGNALLING_BITS // 8]
        want = crc16(body.tobytes())
        got = (int(data[SIGNALLING_BITS // 8]) << 8) | int(data[SIGNALLING_BITS // 8 + 1])
        if got != want:
            return None
        bits = np.unpackbits(body)
        values: dict[str, int] = {}
        pos = 0
        for name, width in SIGNALLING_FIELDS:
            v = 0
            for i in range(width):
                v = (v << 1) | int(bits[pos + i])
            pos += width
            values[name] = v
        if values["version"] != WIRE_VERSION:
            return None
        return cls(modcod=modcod, codec=values["codec"], flags=values["flags"],
                   il_phase=values["il_phase"], rs_phase=values["rs_phase"],
                   frame_count=values["frame_count"], version=values["version"],
                   interleaver=values["interleaver"])


# --------------------------------------------------------------------------
# Frame assembly
# --------------------------------------------------------------------------

def data_slots(profile: Profile) -> np.ndarray:
    """Indices within a frame that carry payload symbols.

    Everything else is preamble, header or pilot. Computed once and shared, so
    the modulator and demodulator cannot disagree about the layout.
    """
    idx = []
    pos = PREAMBLE_SYMBOLS + HEADER_SYMBOLS
    for _ in range(profile.pilot_groups):
        idx.extend(range(pos + 1, pos + profile.pilot_spacing))
        pos += profile.pilot_spacing
    return np.array(idx, dtype=np.int64)


def pilot_slots(profile: Profile) -> np.ndarray:
    pos = PREAMBLE_SYMBOLS + HEADER_SYMBOLS
    return np.arange(profile.pilot_groups, dtype=np.int64) * profile.pilot_spacing + pos


def channel_bits(profile: Profile, modcod: Modcod, header: Header,
                 payload_bytes: np.ndarray) -> np.ndarray:
    """Signalling and payload, scrambled and convolutionally coded.

    Everything a frame carries except the layout: the same bits go out
    whether they are laid along a single carrier or spread across OFDM
    subcarriers, which is what lets the two modes share one FEC chain, one
    signalling format and one set of measured MODCOD thresholds.

    ``payload_bytes`` is the interleaved, RS-coded byte stream for this frame,
    exactly ``capacity.frame_bytes`` long.
    """
    cap = profile.capacity(modcod)
    payload_bytes = np.asarray(payload_bytes, dtype=np.uint8).ravel()
    if len(payload_bytes) != cap.frame_bytes:
        raise ValueError(
            f"frame needs exactly {cap.frame_bytes} payload bytes, got {len(payload_bytes)}"
        )

    # Signalling rides at the front of the information bits, ahead of the
    # interleaved payload and inside the same convolutional code. It is not
    # interleaved: the deinterleaver cannot run until the phases in here have
    # been read, so they have to survive on the inner code alone.
    #
    # It is also scrambled with a *fixed* seed while the payload uses the
    # frame counter. The counter lives inside the signalling, so seeding the
    # signalling from it would require knowing it in order to read it.
    plain = bool(header.flags & FLAG_NOSCRAMBLE)
    scrambled = np.concatenate([
        scramble(header.to_signalling(), SIGNALLING_SEED),
        payload_bytes if plain else scramble(payload_bytes, header.frame_count),
    ])
    info = np.unpackbits(scrambled)
    # Pad to the exact info-bit count the rate adapter expects.
    info = np.concatenate([info, np.zeros(cap.info_bits - len(info), dtype=np.uint8)])
    channel = conv.encode(info, modcod.conv_num, modcod.conv_den)

    need = cap.channel_bits
    if len(channel) < need:
        # Fill any remainder with scrambler output so the tail of the frame
        # carries noise-like energy instead of a run of constant symbols. With
        # dispersal off the filler goes too, and deliberately: the tail is then
        # a constant run, which is precisely the thing being demonstrated.
        filler = (np.zeros(need - len(channel), dtype=np.uint8) if plain
                  else np.unpackbits(_scramble_mask(
                      (need - len(channel) + 7) // 8,
                      0x1234 ^ header.frame_count)))
        channel = np.concatenate([channel, filler[:need - len(channel)]])
    return channel[:need]


def build_frame(profile: Profile, modcod: Modcod, header: Header,
                payload_bytes: np.ndarray) -> np.ndarray:
    """Assemble one single-carrier frame's worth of symbols."""
    channel = channel_bits(profile, modcod, header, payload_bytes)
    symbols = np.empty(profile.frame_symbols, dtype=np.complex128)
    symbols[:PREAMBLE_SYMBOLS] = preamble()
    symbols[PREAMBLE_SYMBOLS:PREAMBLE_SYMBOLS + HEADER_SYMBOLS] = encode_modcod(modcod.index)
    symbols[pilot_slots(profile)] = pilot_sequence(profile.pilot_groups)
    symbols[data_slots(profile)] = constellation.modulate(
        channel, modcod.bits_per_symbol, profile.constellation_family)
    return symbols


def parse_frame(profile: Profile, modcod: Modcod, symbols: np.ndarray,
                noise_var: float, csi: np.ndarray | None = None
                ) -> tuple["Header | None", np.ndarray | None]:
    """Recover (header, payload bytes) from equalised symbols.

    Returns ``(None, None)`` when the signalling CRC fails, which is the
    receiver's lock test now that MODCOD arrives separately as a codeword.
    """
    slots = data_slots(profile)
    return decode_payload(profile, modcod, symbols[slots], noise_var,
                          csi=None if csi is None else csi[slots])


def decode_payload(profile: Profile, modcod: Modcod, data: np.ndarray,
                   noise_var: float, csi: np.ndarray | None = None
                   ) -> tuple["Header | None", np.ndarray | None]:
    """Payload-carrying symbols back to (header, payload bytes).

    The layout-free half of parse_frame, so OFDM can hand over the symbols it
    pulled off its subcarriers and get the identical decode.
    """
    cap = profile.capacity(modcod)
    llr = constellation.demodulate_soft(
        data, modcod.bits_per_symbol, noise_var, csi=csi,
        family=profile.constellation_family,
    )
    need = conv.channel_bits_for(cap.info_bits, modcod.conv_num, modcod.conv_den)
    info = conv.decode(llr[:need], modcod.conv_num, modcod.conv_den, cap.info_bits)
    total = SIGNALLING_BYTES + cap.frame_bytes
    block = np.packbits(info[:total * 8])
    header = Header.from_signalling(
        scramble(block[:SIGNALLING_BYTES], SIGNALLING_SEED), modcod.index)
    if header is None:
        return None, None
    # The header settles whether the payload was dispersed, which is why the
    # flag has to live in a block that is always dispersed itself.
    payload = block[SIGNALLING_BYTES:]
    if not header.flags & FLAG_NOSCRAMBLE:
        payload = scramble(payload, header.frame_count)
    return header, payload
