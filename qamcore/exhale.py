"""exhale, the xHE-AAC encoder, and the plumbing to stream through it.

Nothing in ffmpeg can encode xHE-AAC. The open-source Fraunhofer FDK ships a
USAC *decoder* and no USAC encoder, so `libfdk_aac` is linked into this build
and still rejects ``-profile:a usac``, ``xhe`` and ``aac_usac``; ffmpeg's own
AAC encoder is LC only, and so is MediaFoundation's. That is not something
this project can fix from the outside, so it goes around it: exhale is a small
open-source USAC encoder that reads WAVE on stdin, and this drives it.

    ffmpeg -i <source> -f wav -   ->   exhale <preset> s <n> -   ->   LOAS

exhale's stdout mode is guarded by ``ENABLE_STDOUT_LOAS`` in its own source,
off in the upstream tree; tools/build_exhale.py turns it on and builds it. It
emits LOAS/LATM, which is the wrong container for us -- ffmpeg cannot decode
USAC out of LOAS (see fmp4.py) -- but every frame carries a whole
StreamMuxConfig, so it is a perfectly good way to hand over a configuration
and a sequence of access units. This unwraps it back to those two things.
"""

from __future__ import annotations

import os
import shutil
import subprocess

# Preset letters a-g, and the stereo bitrate each is nominally worth. Only the
# eSBR presets can be used: exhale's stdout mode requires one, and they are the
# right ones anyway at these rates. The digit presets 0-9 start at 48 kbps
# without SBR and only make sense where there are bits to spare.
#
# exhale's own note is worth repeating: it implements the frequency-domain
# coding tools and not ACELP or the low-rate stereo tools, so 36 kbps stereo is
# not what the standard can do at 36 kbps -- it is what this encoder can. Use
# the lowest rung only when the channel forces it.
PRESETS: tuple[tuple[str, int], ...] = (
    ("a", 36000), ("b", 48000), ("c", 60000), ("d", 72000),
    ("e", 84000), ("f", 96000), ("g", 108000),
)
MIN_BITRATE = PRESETS[0][1]

# How often exhale codes a frame that needs no history. A receiver joining a
# broadcast already in progress can only start at one of these, and a receiver
# that has just lost some can only rejoin at one, so this sets both the join
# latency and the recovery time. exhale's own documentation suggests up to 2.5
# seconds, which is the right answer for a file being seeked and the wrong one
# for a broadcast nobody hears the start of.
#
# 10 is its minimum, and measured against 20 s of demanding material the whole
# span costs almost nothing:
#
#     interval    worst-case join    bitrate
#         10            427 ms       52.8 kbps
#         23            981 ms       51.4 kbps
#         58           2475 ms       50.8 kbps
#
# Four per cent of the rate for six times the responsiveness, on a link with
# no way to ask for a repeat.
INDEP_PERIOD = 10

FRAME_SAMPLES = 2048    # what the eSBR presets emit, at any rate 32-48 kHz

# Where a built exhale is expected to be. tools/build_exhale.py installs into
# the first of these; the environment variable is for anyone who has one
# elsewhere already.
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CANDIDATES = (
    os.path.join(_ROOT, "bin", "exhale.exe"),
    os.path.join(_ROOT, "bin", "exhale"),
)


class ExhaleError(RuntimeError):
    pass


def find_exhale(explicit: str | None = None) -> str | None:
    """A usable exhale, or None. Never raises -- callers offer or hide xHE-AAC
    on the strength of the answer, and a missing encoder is not an error until
    something asks to use it."""
    for path in (explicit, os.environ.get("QAMCAST_EXHALE")):
        if path and os.path.exists(path):
            return path
    for path in CANDIDATES:
        if os.path.exists(path):
            return path
    return shutil.which("exhale")


def preset_for(bitrate: int) -> str | None:
    """The fastest preset that fits inside ``bitrate``, or None if none does."""
    best = None
    for letter, rate in PRESETS:
        if rate <= bitrate:
            best = letter
    return best


def preset_bitrate(letter: str) -> int:
    return dict(PRESETS)[letter]


def command(exe: str, preset: str, indep: int = INDEP_PERIOD) -> list[str]:
    """exhale's expert command line for WAVE on stdin, LOAS on stdout.

    Five arguments exactly: the count is how exhale decides where its input
    and output are, so this shape is not cosmetic. ``s`` is the seamless flag,
    which its stdout mode requires.
    """
    return [exe, preset, "s", str(indep), "-"]


def usable(exe: str | None = None) -> tuple[bool, str]:
    """Whether exhale is present and this build can write LOAS to a pipe.

    Both halves are asked by running it, because the useful failure is the
    second one: an exhale built from the upstream tree without the stdout
    switch turned on runs perfectly and writes an MP4 file named ``-``.
    """
    exe = exe or find_exhale()
    if not exe:
        return False, ("no exhale found. Run tools/build_exhale.py to build "
                       "one, or set QAMCAST_EXHALE to an existing binary.")
    try:
        done = subprocess.run([exe, "-h"], capture_output=True, timeout=20)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"could not run {os.path.basename(exe)}: {exc}"
    banner = (done.stdout or b"") + (done.stderr or b"")
    if b"exhale" not in banner.lower():
        return False, f"{os.path.basename(exe)} does not look like exhale."
    return True, ""


# --------------------------------------------------------------------------
# LOAS, unwrapped
# --------------------------------------------------------------------------
#
# exhale writes one audioSyncStream frame per access unit, and every one of
# them repeats the whole StreamMuxConfig:
#
#     56 Ex xx        syncword 0x2B7, then 13 bits of length
#     20 00           useSameStreamMux=0, then a StreamMuxConfig whose
#                     subframe, program and layer counts are all zero
#     <ASC ...>       the AudioSpecificConfig, ending three bits into its
#                     last byte
#     ..0 0011        frameLengthType=0, then latmBufferFullness
#     FC              the rest of the fullness, then two zero flags
#     <lengths>       PayloadLengthInfo: 255s and then a remainder
#     <access unit>
#
# The configuration is constant for a stream, so it is lifted out once and the
# rest of every frame is the access unit. Where the config ends is found rather
# than assumed: the two marker bytes are checked, and then the length fields
# must add up to exactly the bytes that remain. That is a strong enough test
# that a wrong offset cannot survive it.


def _payload_offset(frame: bytes) -> int | None:
    for i in range(6, min(len(frame) - 1, 96)):
        if frame[i] != 0xFC or (frame[i - 1] & 0x1F) != 0x03:
            continue
        pos = i + 1
        total = 0
        while pos < len(frame):
            byte = frame[pos]
            pos += 1
            total += byte
            if byte < 255:
                break
        else:
            continue
        if pos + total == len(frame):
            return i + 1
    return None


class Unpacker:
    """exhale's LOAS output in, a config and bare access units out."""

    def __init__(self) -> None:
        self.config: bytes | None = None
        self._buf = bytearray()
        self.malformed = 0

    def feed(self, chunk: bytes) -> list[bytes]:
        self._buf.extend(chunk)
        out: list[bytes] = []
        for frame in _frames(self._buf):
            unit = self._unwrap(frame)
            if unit is None:
                self.malformed += 1
            else:
                out.append(unit)
        return out

    def _unwrap(self, frame: bytes) -> bytes | None:
        if len(frame) < 10 or frame[3] != 0x20 or frame[4] != 0x00:
            return None
        offset = _payload_offset(frame)
        if offset is None:
            return None
        if self.config is None:
            # The last byte of the config holds three bits of it and five bits
            # of what follows; masking those off leaves the byte-padded form an
            # esds box wants.
            asc = bytearray(frame[5:offset - 1])
            asc[-1] &= 0xE0
            self.config = bytes(asc)
        pos = offset
        while pos < len(frame) and frame[pos] == 255:
            pos += 1
        return frame[pos + 1:]


def _frames(buf: bytearray) -> list[bytes]:
    """Whole LOAS frames off the front of ``buf``, in place."""
    out: list[bytes] = []
    while len(buf) >= 3:
        if buf[0] != 0x56 or (buf[1] & 0xE0) != 0xE0:
            nxt = buf.find(b"\x56", 1)
            if nxt < 0:
                buf.clear()
                break
            del buf[:nxt]
            continue
        length = 3 + (((buf[1] & 0x1F) << 8) | buf[2])
        if len(buf) < length:
            break
        out.append(bytes(buf[:length]))
        del buf[:length]
    return out
