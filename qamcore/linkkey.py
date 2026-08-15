"""Link keys -- the physical layer settings as one copyable token.

Everything the receiver needs before it can demodulate is deliberately absent
from the header. The frame is

    [ preamble ][ MODCOD codeword ][ data ]

and the preamble is a Zadoff-Chu symbol built *from* the geometry, so the
correlator has to know the transform size and prefix before it can find the
preamble at all. Nothing carried after the preamble -- MODCOD included -- can
tell a receiver how to hear the preamble. Only the payload description travels
in band; the geometry has to arrive some other way.

A link key is that other way: an operator copies one short string across
instead of matching five dials by eye.

**The key describes the link, not a preset.** That is the whole design, and it
was worth a second attempt to get right. A key naming only the card rate,
symbol rate and roll-off looks sufficient and is not: those three do not
determine the carrier frequency, the pilot spacing or the frame length, and
the presets disagree on all three. Reconstructing by searching the profile
table for a match then produces a *plausible* link rather than the right one --
measured, a hand-dialled 48 kHz / 9600 Bd / 0.25 link matched RADIO on all
three fields and came back tuned 1000-13000 Hz instead of 480-12480. It never
locks and nothing says why. So the key carries every field the geometry needs,
and the preset name it happens to match is a label shown to the operator.

Format:  QC2-<16 Crockford base32 chars>

carrying a 10-byte record:

    0       version (high nibble), mode (low nibble: 0 single, 1 OFDM)
    1       pilot index (bits 0-1), frame index (2-3), prefix index (4-5)
    2..3    sample rate / 100, little endian
    4..5    single: symbol rate          OFDM: band low, Hz
    6..7    single: carrier, Hz          OFDM: band high, Hz
    8       single: roll-off x 100       OFDM: subcarrier count index
    9       CRC-8, so a mistyped key is rejected rather than silently mistuned

The OFDM band is the one the carriers are *fitted into*, not the one they end
up occupying -- the latter sits inside it by up to a subcarrier, and refitting
against it would walk the signal inwards on every copy.

Crockford base32 leaves out I, L, O and U, so the key survives being read
aloud or written down. Decoding is case insensitive and ignores dashes.
"""

from __future__ import annotations

from . import profiles

_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_DECODE = {c: i for i, c in enumerate(_ALPHABET)}
# Crockford's stated substitutions, so a key read off a screen still decodes.
_DECODE.update({"I": 1, "L": 1, "O": 0, "U": 0})

PREFIX = "QC2"
VERSION = 2
_RECORD = 10
_BODY_CHARS = 16          # ceil(10 bytes * 8 / 5)

MODE_SINGLE = 0
MODE_OFDM = 1


class LinkKeyError(ValueError):
    """A key that is malformed, mistyped, or from a different version."""


def _crc8(data: bytes) -> int:
    crc = 0
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = ((crc << 1) ^ 0x07) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    return crc


def _b32(data: bytes) -> str:
    n = int.from_bytes(data, "big")
    bits = len(data) * 8
    pad = (-bits) % 5
    n <<= pad
    return "".join(_ALPHABET[(n >> (i * 5)) & 31]
                   for i in range((bits + pad) // 5 - 1, -1, -1))


def _unb32(text: str, nbytes: int) -> bytes:
    n = 0
    for ch in text:
        try:
            n = (n << 5) | _DECODE[ch]
        except KeyError:
            raise LinkKeyError(f"{ch!r} is not a link key character") from None
    return (n >> (len(text) * 5 - nbytes * 8)).to_bytes(nbytes, "big")


def _index(value, choices, what: str) -> int:
    try:
        return list(choices).index(value)
    except ValueError:
        raise LinkKeyError(
            f"{what} {value} is not one of {', '.join(str(c) for c in choices)}"
        ) from None


def _u16(value: float, what: str) -> tuple[int, int]:
    n = int(round(value))
    if not 0 <= n < 65536:
        raise LinkKeyError(f"{what} {value} is out of range for a link key")
    return n & 0xFF, n >> 8


def encode(profile: profiles.Profile) -> str:
    """Pack a profile's physical layer into its key."""
    body = bytearray(_RECORD - 1)
    body[0] = (VERSION << 4) | (MODE_OFDM if profile.is_ofdm else MODE_SINGLE)
    body[2], body[3] = _u16(profile.sample_rate / 100.0, "sample rate")

    if profile.is_ofdm:
        flags = _index(profile.ofdm_cp_fraction, profiles.OFDM_CP_CHOICES,
                       "prefix fraction") << 4
        body[4], body[5] = _u16(profile.ofdm_band_lo, "band low")
        body[6], body[7] = _u16(profile.ofdm_band_hi, "band high")
        body[8] = _index(profile.ofdm_carriers, profiles.OFDM_CARRIER_CHOICES,
                         "subcarrier count")
    else:
        flags = (_index(profile.pilot_spacing, profiles.PILOT_CHOICES,
                        "pilot spacing")
                 | _index(profile.frame_symbols, profiles.FRAME_CHOICES,
                          "frame length") << 2)
        body[4], body[5] = _u16(profile.symbol_rate, "symbol rate")
        body[6], body[7] = _u16(profile.carrier, "carrier")
        roll = int(round(profile.rolloff * 100))
        if not 0 <= roll < 256:
            raise LinkKeyError(f"roll-off {profile.rolloff} is out of range")
        body[8] = roll
    body[1] = flags
    return f"{PREFIX}-{_b32(bytes(body) + bytes([_crc8(bytes(body))]))}"


def decode(key: str) -> dict:
    """Unpack a key. Raises LinkKeyError on anything that is not one."""
    text = "".join(str(key).split()).replace("-", "").replace("_", "").upper()
    if text.startswith(PREFIX):
        text = text[len(PREFIX):]
    elif text[:2] == "QC":
        raise LinkKeyError(
            f"this key is from a different link key version; this build "
            f"reads {PREFIX} keys")
    if len(text) != _BODY_CHARS:
        raise LinkKeyError(
            f"a link key is {len(PREFIX) + 1 + _BODY_CHARS} characters "
            f"like {PREFIX}-{'X' * _BODY_CHARS}")

    raw = _unb32(text, _RECORD)
    if _crc8(raw[:-1]) != raw[-1]:
        raise LinkKeyError("checksum failed -- the key is mistyped")
    if raw[0] >> 4 != VERSION:
        raise LinkKeyError(f"link key version {raw[0] >> 4} is not supported")

    mode = raw[0] & 0x0F
    flags = raw[1]
    sample_rate = (raw[2] | (raw[3] << 8)) * 100
    a = raw[4] | (raw[5] << 8)
    b = raw[6] | (raw[7] << 8)

    if mode == MODE_OFDM:
        idx = raw[8]
        if idx >= len(profiles.OFDM_CARRIER_CHOICES):
            raise LinkKeyError("the key names a subcarrier count this build "
                               "does not have")
        info = {
            "mode": "ofdm",
            "sample_rate": sample_rate,
            "band": [float(a), float(b)],
            "carriers": profiles.OFDM_CARRIER_CHOICES[idx],
            "cp_fraction": profiles.OFDM_CP_CHOICES[(flags >> 4) & 3],
        }
    elif mode == MODE_SINGLE:
        info = {
            "mode": "single",
            "sample_rate": sample_rate,
            "symbol_rate": a,
            "carrier": float(b),
            "rolloff": raw[8] / 100.0,
            "pilot_spacing": profiles.PILOT_CHOICES[flags & 3],
            "frame_symbols": profiles.FRAME_CHOICES[(flags >> 2) & 3],
            "carriers": None,
        }
    else:
        raise LinkKeyError(f"unknown link mode {mode}")
    return info


def to_profile(info: dict, name: str = "") -> profiles.Profile:
    """Rebuild the physical layer the key describes.

    Named after the preset it matches when one does, purely so the receiver's
    status line reads the same as the transmitter's. Nothing is taken from that
    preset -- the geometry is built from the key alone.
    """
    label = name or profile_name(info) or "CUSTOM"
    try:
        if info["mode"] == "ofdm":
            lo, hi = info["band"]
            return profiles.make_ofdm_profile(
                info["sample_rate"], lo, hi, info["carriers"],
                info["cp_fraction"], name=label,
                description="from a link key")
        return profiles.make_profile(
            sample_rate=info["sample_rate"], symbol_rate=info["symbol_rate"],
            rolloff=info["rolloff"], carrier=info["carrier"],
            pilot_spacing=info["pilot_spacing"],
            frame_symbols=info["frame_symbols"], name=label)
    except ValueError as exc:
        raise LinkKeyError(f"the key describes an impossible link: {exc}") from None


def describe(info: dict) -> str:
    """One line an operator can check the key against before applying it."""
    parts = [f"{info['sample_rate'] / 1000:g} kHz card"]
    if info["carriers"]:
        lo, hi = info["band"]
        parts.append(f"OFDM {info['carriers']} carriers")
        parts.append(f"{lo / 1000:g}-{hi / 1000:g} kHz")
    else:
        parts.append(f"{info['symbol_rate']} Bd")
        parts.append(f"roll-off {info['rolloff']:g}")
        parts.append(f"carrier {info['carrier'] / 1000:g} kHz")
    return " · ".join(parts)


def profile_name(info: dict) -> str:
    """The preset that matches this key exactly, or '' if none does.

    Compared against what each preset actually builds, field by field, rather
    than against the two or three numbers that are convenient to check. A
    near-match is not a match: the point of the key is that it names one link,
    and a label claiming otherwise would be worse than no label.
    """
    try:
        want = to_profile(info, name="CUSTOM")
    except LinkKeyError:
        return ""
    for name, p in profiles.PROFILES.items():
        candidates = [p]
        if p.is_ofdm and info["carriers"]:
            candidates = [profiles.with_carriers(p, info["carriers"])]
        for q in candidates:
            if _same_link(q, want):
                return q.name
    return ""


def _same_link(a: profiles.Profile, b: profiles.Profile) -> bool:
    """Whether two profiles put the same signal on the wire."""
    if a.mode != b.mode or a.sample_rate != b.sample_rate:
        return False
    if a.is_ofdm:
        return (a.ofdm_fft == b.ofdm_fft and a.ofdm_cp == b.ofdm_cp
                and a.ofdm_bin_lo == b.ofdm_bin_lo
                and a.ofdm_bin_hi == b.ofdm_bin_hi
                and a.ofdm_symbols == b.ofdm_symbols)
    return (a.symbol_rate == b.symbol_rate
            and abs(a.rolloff - b.rolloff) < 1e-9
            and abs(a.carrier - b.carrier) < 0.5
            and a.pilot_spacing == b.pilot_spacing
            and a.frame_symbols == b.frame_symbols)
