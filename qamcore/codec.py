"""Audio coding: Opus and HE-AAC, driven through ffmpeg.

Opus is the default and the recommendation. It spans 16-192 kbps in one codec
with no profile ladder, is royalty-free, and has packet-loss concealment built
in -- which matters here more than it would elsewhere, because this is a
one-way broadcast with no retransmission. When a frame is lost, something has
to fill the hole, and Opus does it natively.

HE-AACv2 is fully supported and takes a ladder, because FDK clamps HE-AACv2
stereo near 160 kbps: asking for 192 there silently yields about 166. The
mapping below steps HE-AACv2 -> HE-AACv1 -> AAC-LC as the bitrate rises, which
is also what the codecs actually want -- Parametric Stereo earns its keep at
32 kbps and costs quality once there are bits to spare. The chosen profile is
signalled in the frame header and carried in-band as a config packet, so the
receiver reconfigures itself and you never have to tell it.

Packet framing differs by codec and is handled in one place:

    AAC   ADTS frames are self-delimiting, so the stream is sliced by reading
          its own headers.
    Opus  has no framing of its own; ffmpeg emits and accepts it only inside
          a container, so ogg.py unwraps and rewraps it.
"""

from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import threading
from dataclasses import dataclass

from . import framing, ogg

# Where to look for an ffmpeg that has libfdk_aac. The bundled Windows builds
# and anything on PATH will decode everything here, but only a --enable-nonfree
# build can *encode* HE-AACv2; Opus needs only libopus, which is ubiquitous.
FFMPEG_CANDIDATES = (
    r"C:\Users\Gaming\Desktop\HE-AAC FFMPEG GUI\bin\ffmpeg.exe",
    r"C:\Users\Gaming\Desktop\HE-AAC FFMPEG GUI\bin\ffmpeg",
)

SAMPLE_RATE = 48000
CHANNELS = 2
OPUS_FRAME_MS = 20


class CodecError(RuntimeError):
    pass


def find_ffmpeg(explicit: str | None = None) -> str:
    if explicit:
        if not os.path.exists(explicit):
            raise CodecError(f"ffmpeg not found at {explicit}")
        return explicit
    for path in FFMPEG_CANDIDATES:
        if os.path.exists(path):
            return path
    found = shutil.which("ffmpeg")
    if found:
        return found
    raise CodecError(
        "no ffmpeg found. Set --ffmpeg to a build with libfdk_aac if you want "
        "HE-AACv2; Opus works with any recent build."
    )


def has_encoder(ffmpeg: str, name: str) -> bool:
    try:
        out = subprocess.run([ffmpeg, "-hide_banner", "-encoders"],
                             capture_output=True, text=True, timeout=20).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return any(line.split()[1:2] == [name] for line in out.splitlines() if line.strip())


# --------------------------------------------------------------------------
# Profile selection
# --------------------------------------------------------------------------

# What each codec actually does to the signal, keyed by the id that travels in
# the frame. Keyed that way because the receiver never sees the bitrate the
# encoder was asked for -- it learns the codec from the header and nothing
# else, so anything it displays has to be derivable from this alone.
#
#   SBR  Spectral Band Replication: the top octave is not coded, it is
#        reconstructed from the band below it plus a little side information.
#   PS   Parametric Stereo: one mono channel is coded and the stereo image is
#        rebuilt from a handful of parameters. Cheap, and audibly so once
#        there are bits to spare.
CODEC_FEATURES = {
    framing.CODEC_OPUS: (False, False),
    framing.CODEC_AAC_LC: (False, False),
    framing.CODEC_HE_AAC_V1: (True, False),
    framing.CODEC_HE_AAC_V2: (True, True),
}


def features(codec_id: int) -> dict:
    """SBR and PS flags for a codec id, for anything holding only that."""
    sbr, ps = CODEC_FEATURES.get(codec_id, (False, False))
    return {"sbr": sbr, "ps": ps}


@dataclass(frozen=True)
class CodecChoice:
    codec_id: int          # framing.CODEC_*
    encoder: str           # ffmpeg encoder name
    profile: str | None    # libfdk_aac -profile:a value
    container: str         # "ogg" or "adts"

    @property
    def name(self) -> str:
        return framing.CODEC_NAMES[self.codec_id]

    @property
    def sbr(self) -> bool:
        return CODEC_FEATURES.get(self.codec_id, (False, False))[0]

    @property
    def ps(self) -> bool:
        return CODEC_FEATURES.get(self.codec_id, (False, False))[1]


AAC_ALIASES = ("aac", "he-aac", "heaac", "he-aacv2")

# The HE-AAC ladder, as (upper bitrate bound inclusive, choice, why). Kept as
# data rather than a chain of ifs because the UI has to show the user where
# the switches are, and a second copy of these thresholds would be a second
# thing to get wrong.
#
# The bounds follow FDK's usable ranges rather than round numbers: Parametric
# Stereo is a win below ~48k, SBR alone holds to ~96k, and plain LC is better
# above that -- quite apart from the ~160k ceiling that makes HE-AACv2 at 192k
# a silent downgrade.
AAC_LADDER: tuple[tuple[int | None, "CodecChoice", str], ...] = (
    (48000, CodecChoice(framing.CODEC_HE_AAC_V2, "libfdk_aac", "aac_he_v2", "adts"),
     "SBR plus Parametric Stereo -- most efficient where bits are scarce"),
    (96000, CodecChoice(framing.CODEC_HE_AAC_V1, "libfdk_aac", "aac_he", "adts"),
     "SBR without PS; stereo is coded properly again"),
    (None, CodecChoice(framing.CODEC_AAC_LC, "libfdk_aac", "aac_low", "adts"),
     "plain LC -- HE-AACv2 clamps near 160k, so it would be a downgrade here"),
)

OPUS_CHOICE = CodecChoice(framing.CODEC_OPUS, "libopus", None, "ogg")

# Every code point the frame header can name, keyed by that code point. Used
# by passthrough, which starts from what the source *is* rather than from what
# to encode it as.
CHOICE_BY_ID = {c.codec_id: c for c in
                [OPUS_CHOICE] + [ch for _, ch, _ in AAC_LADDER]}


def _choice_for_id(codec_id: int) -> CodecChoice:
    try:
        return CHOICE_BY_ID[codec_id]
    except KeyError:
        raise CodecError(f"no codec for id {codec_id}") from None


def choose(codec: str, bitrate: int) -> CodecChoice:
    """Pick encoder and profile for a requested codec and bitrate."""
    codec = codec.lower()
    if codec == "opus":
        return OPUS_CHOICE
    if codec in AAC_ALIASES:
        for limit, choice, _ in AAC_LADDER:
            if limit is None or bitrate <= limit:
                return choice
    raise CodecError(f"unknown codec {codec!r}; expected 'opus' or 'aac'")


# --------------------------------------------------------------------------
# Probing a source, and passing it through untouched
# --------------------------------------------------------------------------
#
# Re-encoding an already-compressed stream is generational loss: the decoder's
# artefacts become the encoder's input, and it pays for the privilege in CPU
# and latency. When the source is already a codec this wire format can carry,
# the packets can be copied straight into frames instead -- bit-exact from the
# station to the receiver's decoder.
#
# It is not always possible, and the reasons are worth stating plainly rather
# than failing at Start:
#
#   * The codec has to be one the frame header can name. MP3, FLAC and Vorbis
#     have no code point here, so they must be re-encoded.
#   * The stream's own bitrate has to fit the channel. Nothing can be done
#     about a 128k stream on a 96k link except re-encode it.

# ffprobe's `profile` strings for AAC, mapped to what travels in the header.
AAC_PROFILES = {
    "he-aacv2": framing.CODEC_HE_AAC_V2,
    "he-aac": framing.CODEC_HE_AAC_V1,
    "lc": framing.CODEC_AAC_LC,
    "main": framing.CODEC_AAC_LC,
    "ltp": framing.CODEC_AAC_LC,
}


def find_ffprobe(ffmpeg: str | None = None) -> str:
    """ffprobe living beside the ffmpeg in use, or on PATH."""
    exe = find_ffmpeg(ffmpeg)
    for name in ("ffprobe.exe", "ffprobe"):
        cand = os.path.join(os.path.dirname(exe), name)
        if os.path.exists(cand):
            return cand
    found = shutil.which("ffprobe")
    if found:
        return found
    raise CodecError(f"no ffprobe found beside {exe} or on PATH")


def probe(source: str, ffmpeg: str | None = None, timeout: float = 20.0) -> dict:
    """What codec, rate and layout a source actually carries.

    Returns a dict with ``codec``, ``profile``, ``bitrate``, ``sample_rate``,
    ``channels``, and a ``passthrough`` sub-dict saying whether the packets
    could be copied rather than re-encoded, and if not, why not.
    """
    exe = find_ffprobe(ffmpeg)
    cmd = [exe, "-hide_banner", "-v", "error"]
    if source.lower().startswith(("http://", "https://")):
        # A live stream never ends, so bound how long ffprobe will listen.
        cmd += ["-rw_timeout", str(int(timeout * 1_000_000))]
    cmd += ["-select_streams", "a:0", "-show_entries",
            "stream=codec_name,profile,sample_rate,channels,bit_rate",
            "-show_entries", "format=format_name,bit_rate",
            "-of", "json", source]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True,
                             timeout=timeout + 10)
    except subprocess.TimeoutExpired:
        raise CodecError(f"timed out probing {source}") from None
    except OSError as exc:
        raise CodecError(f"could not run ffprobe: {exc}") from None
    try:
        data = json.loads(out.stdout or "{}")
    except json.JSONDecodeError:
        data = {}
    streams = data.get("streams") or []
    if not streams:
        err = (out.stderr or "").strip().splitlines()
        raise CodecError(f"no audio stream in {source}"
                         + (f" -- {err[-1]}" if err else ""))

    st = streams[0]
    fmt = data.get("format") or {}

    def _int(*values):
        for v in values:
            try:
                return int(v)
            except (TypeError, ValueError):
                continue
        return None

    info = {
        "source": source,
        "codec": (st.get("codec_name") or "").lower(),
        "profile": st.get("profile") or "",
        "bitrate": _int(st.get("bit_rate"), fmt.get("bit_rate")),
        "sample_rate": _int(st.get("sample_rate")),
        "channels": _int(st.get("channels")),
        "container": fmt.get("format_name") or "",
    }
    info["label"] = describe(info)
    info["passthrough"] = passthrough_choice(info)
    return info


def describe(info: dict) -> str:
    bits = []
    name = info.get("codec", "")
    prof = (info.get("profile") or "").strip()
    bits.append(f"{name.upper()} {prof}".strip() if prof else name.upper())
    if info.get("bitrate"):
        bits.append(f"{info['bitrate']/1000:.0f} kbps")
    if info.get("sample_rate"):
        bits.append(f"{info['sample_rate']/1000:g} kHz")
    if info.get("channels"):
        bits.append("stereo" if info["channels"] == 2 else f"{info['channels']} ch")
    return ", ".join(b for b in bits if b)


def passthrough_choice(info: dict) -> dict:
    """Can this source be copied rather than re-encoded, and as what?

    ``{"ok": bool, "codec_id": int | None, "reason": str}``. The reason is
    written to be shown to a user, because "passthrough unavailable" with no
    explanation is the kind of thing that gets debugged twice.
    """
    name = (info.get("codec") or "").lower()
    if name == "opus":
        return {"ok": True, "codec_id": framing.CODEC_OPUS,
                "reason": "Opus packets copy straight into frames."}
    if name == "aac":
        prof = (info.get("profile") or "LC").strip().lower().replace(" ", "")
        cid = AAC_PROFILES.get(prof)
        if cid is None:
            return {"ok": False, "codec_id": None,
                    "reason": f"AAC profile {info.get('profile')!r} has no code "
                              f"point in the frame header."}
        out = {"ok": True, "codec_id": cid,
               "reason": "ADTS frames copy straight into frames."}
        # ffprobe reports "HE-AAC" for both v1 and v2, because Parametric
        # Stereo is signalled inside the SBR extension payload rather than in
        # the ADTS header it reads -- confirmed here against a stream that is
        # almost certainly v2. So the PS flag is genuinely unknown, and saying
        # "no PS" would be asserting something unmeasured.
        #
        # It costs nothing where it matters: both decode through the same AAC
        # decoder, which picks PS up from the bitstream on its own, so the
        # audio is right either way. Only the displayed flag is uncertain.
        if cid == framing.CODEC_HE_AAC_V1:
            out["ps_undetermined"] = True
            out["reason"] += (" Signalled as HE-AACv1; if the stream is really "
                              "v2 the decoder still finds the Parametric "
                              "Stereo, but nothing in the header says so.")
        return out
    pretty = name.upper() or "this source"
    return {"ok": False, "codec_id": None,
            "reason": f"{pretty} cannot be carried directly -- the frame header "
                      f"names only Opus and AAC, so it has to be re-encoded."}


def ladder(codec: str, bitrate: int | None = None) -> list[dict]:
    """The rungs a codec steps through, and which one ``bitrate`` lands on.

    Opus returns a single rung: it spans the whole range in one configuration,
    and saying so is more useful than showing nothing.
    """
    codec = codec.lower()
    if codec == "opus":
        return [{"name": OPUS_CHOICE.name, "from": None, "to": None,
                 "range": "16-192k", "why": "one configuration across the "
                 "whole range, with packet-loss concealment built in",
                 "sbr": OPUS_CHOICE.sbr, "ps": OPUS_CHOICE.ps, "active": True}]
    if codec not in AAC_ALIASES:
        raise CodecError(f"unknown codec {codec!r}; expected 'opus' or 'aac'")

    # Which rung is live comes from choose() rather than being re-derived, so
    # what the UI highlights is by construction what the encoder will run.
    live = choose(codec, bitrate).codec_id if bitrate is not None else None

    rungs, low = [], None
    for limit, choice, why in AAC_LADDER:
        if limit is None:
            span = f"above {low // 1000}k"
        elif low is None:
            span = f"to {limit // 1000}k"
        else:
            span = f"{low // 1000}-{limit // 1000}k"
        rungs.append({"name": choice.name, "from": low, "to": limit,
                      "range": span, "why": why,
                      "sbr": choice.sbr, "ps": choice.ps,
                      "active": choice.codec_id == live})
        low = limit
    return rungs


# --------------------------------------------------------------------------
# ADTS framing
# --------------------------------------------------------------------------

def split_adts(buf: bytearray) -> list[bytes]:
    """Pull whole ADTS frames off the front of ``buf``, in place."""
    frames: list[bytes] = []
    while len(buf) >= 7:
        if not (buf[0] == 0xFF and (buf[1] & 0xF0) == 0xF0):
            # Lost sync; step to the next plausible syncword.
            nxt = buf.find(b"\xff", 1)
            if nxt < 0:
                buf.clear()
                break
            del buf[:nxt]
            continue
        length = ((buf[3] & 0x03) << 11) | (buf[4] << 3) | (buf[5] >> 5)
        if length < 7:
            del buf[:1]
            continue
        if len(buf) < length:
            break
        frames.append(bytes(buf[:length]))
        del buf[:length]
    return frames


# --------------------------------------------------------------------------
# Encoder
# --------------------------------------------------------------------------

class Encoder:
    """Reads any ffmpeg-openable source and yields compressed packets.

    The source can be an Icecast or Shoutcast URL, an HLS playlist, a local
    file, or anything else ffmpeg accepts -- it is passed through untouched.
    Reading happens on a thread because a network stream stalls unpredictably
    and the modulator must not stall with it.
    """

    def __init__(self, source: str, codec: str, bitrate: int,
                 ffmpeg: str | None = None, loop: bool = False,
                 passthrough: int | None = None):
        self.source = source
        self.bitrate = bitrate
        # ``passthrough`` is the codec id the source already carries, from
        # probe(). Given one, nothing is re-encoded: the packets are copied.
        # The container still has to match what the receiver's decoder is fed,
        # which is the same ADTS or Ogg either way, so only the codec choice
        # and the ffmpeg command change.
        self.passthrough = passthrough
        self.choice = (_choice_for_id(passthrough) if passthrough is not None
                       else choose(codec, bitrate))
        self.ffmpeg = find_ffmpeg(ffmpeg)
        self.loop = loop
        self._proc: subprocess.Popen | None = None
        self._thread: threading.Thread | None = None
        self._packets: queue.Queue[bytes] = queue.Queue()
        self._config: bytes | None = None
        self._stop = threading.Event()
        self._ogg = ogg.OggReader()
        self._adts = bytearray()
        self.error: str | None = None

        if (self.passthrough is None and self.choice.encoder == "libfdk_aac"
                and not has_encoder(self.ffmpeg, "libfdk_aac")):
            raise CodecError(
                f"{self.ffmpeg} has no libfdk_aac, so it cannot encode HE-AAC. "
                f"Use --codec opus, or point --ffmpeg at a --enable-nonfree build."
            )

    def _command(self) -> list[str]:
        cmd = [self.ffmpeg, "-hide_banner", "-loglevel", "error"]
        if self.loop:
            cmd += ["-stream_loop", "-1"]
        # Reconnect belongs to the HTTP protocol layer, so it must only be
        # offered for network sources -- ffmpeg rejects the whole invocation
        # with "Option reconnect not found" when the input is a local file.
        if self.source.lower().startswith(("http://", "https://", "rtmp://", "rtsp://")):
            cmd += ["-reconnect", "1", "-reconnect_streamed", "1",
                    "-reconnect_delay_max", "5"]
        cmd += ["-i", self.source, "-vn"]
        if self.passthrough is not None:
            # No -ac and no -ar: those would force a decode, which is the one
            # thing passthrough exists to avoid. The stream keeps its own
            # channel count and sample rate all the way to the far end, where
            # the decoder resamples to the player's rate as it always has.
            cmd += ["-c:a", "copy", "-f",
                    "ogg" if self.choice.container == "ogg" else "adts",
                    "pipe:1"]
            return cmd
        cmd += ["-ac", str(CHANNELS), "-ar", str(SAMPLE_RATE),
                "-c:a", self.choice.encoder, "-b:a", str(self.bitrate)]
        if self.choice.profile:
            cmd += ["-profile:a", self.choice.profile]
        if self.choice.container == "ogg":
            # Constrained VBR, not the default unconstrained kind. With plain
            # VBR, -b:a is a soft target that libopus will overshoot by 25-55%
            # on demanding material -- measured 228 kbps against a 192 kbps
            # request. On a channel whose rate is fixed by the MODCOD, that is
            # not a quality decision, it is an ever-growing backlog and
            # eventually an underrun. Constrained VBR holds the average while
            # still letting quiet passages cost less than loud ones.
            cmd += ["-vbr", "constrained",
                    "-frame_duration", str(OPUS_FRAME_MS), "-f", "ogg"]
        else:
            cmd += ["-f", "adts"]
        cmd += ["pipe:1"]
        return cmd

    def start(self) -> None:
        self._proc = subprocess.Popen(
            self._command(), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            bufsize=0,
        )
        self._thread = threading.Thread(target=self._pump, daemon=True)
        self._thread.start()
        threading.Thread(target=self._pump_stderr, daemon=True).start()

    def _pump_stderr(self) -> None:
        """Drain stderr continuously, keeping the last few lines.

        Reading it only after the process exits is a deadlock waiting to
        happen: a failing input can produce more diagnostics than the 64 kB
        pipe buffer holds, at which point ffmpeg blocks writing, never exits,
        and the wait-then-read never runs. The symptom is an encoder that
        produces no audio and no error either.
        """
        assert self._proc and self._proc.stderr
        tail: list[str] = []
        try:
            for raw in iter(self._proc.stderr.readline, b""):
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue
                tail.append(line)
                del tail[:-6]
                if not self._stop.is_set():
                    self.error = " | ".join(tail)[:400]
        except (OSError, ValueError):
            pass

    def _pump(self) -> None:
        assert self._proc and self._proc.stdout
        try:
            while not self._stop.is_set():
                chunk = self._proc.stdout.read(4096)
                if not chunk:
                    break
                for pkt in self._extract(chunk):
                    self._packets.put(pkt)
        except (OSError, ValueError):
            pass
        # stderr is drained by its own thread; see _pump_stderr.

    def _extract(self, chunk: bytes) -> list[bytes]:
        if self.choice.container == "ogg":
            out = []
            for pkt in self._ogg.feed(chunk):
                # An Ogg Opus stream opens with OpusHead then OpusTags. The
                # head is the decoder config and has to reach the receiver;
                # the tags are metadata we replace with our own PAD channel.
                if pkt.startswith(b"OpusHead"):
                    self._config = pkt
                    continue
                if pkt.startswith(b"OpusTags"):
                    continue
                out.append(pkt)
            return out
        # split_adts consumes in place and leaves any partial frame behind, so
        # append to the persistent buffer rather than a temporary.
        self._adts.extend(chunk)
        return split_adts(self._adts)

    @property
    def config(self) -> bytes | None:
        """Decoder configuration, once seen. OpusHead for Opus; None for AAC,
        whose ADTS headers already carry everything a decoder needs."""
        return self._config

    def packets(self, limit: int = 256) -> list[bytes]:
        out = []
        while len(out) < limit:
            try:
                out.append(self._packets.get_nowait())
            except queue.Empty:
                break
        return out

    @property
    def pending(self) -> int:
        return self._packets.qsize()

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def stop(self) -> None:
        self._stop.set()
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._proc.kill()


# --------------------------------------------------------------------------
# Decoder
# --------------------------------------------------------------------------

class Decoder:
    """Compressed packets in, interleaved 16-bit PCM out.

    Holds a long-lived ffmpeg process rather than starting one per packet:
    both codecs carry state between frames, and restarting would discard it
    every 20 ms.
    """

    def __init__(self, codec_id: int, config: bytes | None = None,
                 ffmpeg: str | None = None):
        self.codec_id = codec_id
        self.ffmpeg = find_ffmpeg(ffmpeg)
        self.config = config
        self._proc: subprocess.Popen | None = None
        self._pcm: queue.Queue[bytes] = queue.Queue()
        self._stop = threading.Event()
        self._ogg = ogg.OggWriter()
        self._started = False
        self._granule = 0

    def _command(self) -> list[str]:
        fmt = "ogg" if self.codec_id == framing.CODEC_OPUS else "aac"
        return [self.ffmpeg, "-hide_banner", "-loglevel", "error",
                "-f", fmt, "-i", "pipe:0",
                "-f", "s16le", "-ac", str(CHANNELS), "-ar", str(SAMPLE_RATE),
                "pipe:1"]

    def start(self) -> None:
        if self.codec_id == framing.CODEC_OPUS and not self.config:
            raise CodecError("Opus decoding needs the OpusHead config packet")
        self._proc = subprocess.Popen(
            self._command(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, bufsize=0,
        )
        threading.Thread(target=self._pump, daemon=True).start()
        if self.codec_id == framing.CODEC_OPUS:
            assert self._proc.stdin and self.config
            # Rebuild the two mandatory header pages the container expects.
            head = self._ogg.page([self.config], granule=0, bos=True)
            tags = self._ogg.page([b"OpusTags" + b"\x00\x00\x00\x00" + b"\x00\x00\x00\x00"])
            self._proc.stdin.write(head + tags)
            self._proc.stdin.flush()
        self._started = True

    def _pump(self) -> None:
        assert self._proc and self._proc.stdout
        try:
            while not self._stop.is_set():
                chunk = self._proc.stdout.read(4096)
                if not chunk:
                    break
                self._pcm.put(chunk)
        except (OSError, ValueError):
            pass

    def feed(self, packets: list[bytes]) -> None:
        if not self._started or not self._proc or not self._proc.stdin:
            return
        if not packets:
            return
        try:
            if self.codec_id == framing.CODEC_OPUS:
                self._granule += len(packets) * SAMPLE_RATE * OPUS_FRAME_MS // 1000
                self._proc.stdin.write(self._ogg.pages_for(packets, self._granule))
            else:
                self._proc.stdin.write(b"".join(packets))
            self._proc.stdin.flush()
        except (BrokenPipeError, OSError):
            pass

    def pcm(self) -> bytes:
        out = bytearray()
        while True:
            try:
                out.extend(self._pcm.get_nowait())
            except queue.Empty:
                break
        return bytes(out)

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def stop(self) -> None:
        self._stop.set()
        if self._proc:
            try:
                if self._proc.stdin:
                    self._proc.stdin.close()
            except OSError:
                pass
            if self._proc.poll() is None:
                self._proc.terminate()
                try:
                    self._proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self._proc.kill()
