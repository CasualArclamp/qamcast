"""xHE-AAC, end to end, through the real Encoder and Decoder.

Four things have to hold, and each would fail differently:

  1. **The encoder runs.** ffmpeg decodes the source to WAVE, exhale encodes
     it, and the LOAS it writes unwraps into exactly the access units the
     stream should contain -- counted against the audio's own length, with no
     bytes left over.
  2. **The decoder plays them.** The bare access units and the configuration
     rebuild into a fragmented MP4 that ffmpeg decodes, at every preset.
  3. **Passthrough is bit-exact.** An existing xHE-AAC file, relayed, gives
     back the same access units it went in as. Nothing is re-encoded.
  4. **The audio survives.** The round trip correlates with what went in.
     A container that decodes to the wrong audio is worse than one that does
     not decode at all.

    python tools/xhecheck.py
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np

from qamcore import codec, exhale, fmp4, framing

SECONDS = 12.0
# Tonal rather than noisy: the round trip is measured by correlation, and
# noise cannot be coded, so noise in the source only measures the noise.
TONE = ("aevalsrc="
        "0.30*sin(2*PI*220*t)+0.20*sin(2*PI*554*t)+0.12*sin(2*PI*880*t)|"
        "0.28*sin(2*PI*277*t)+0.18*sin(2*PI*440*t)+0.10*sin(2*PI*1108*t)"
        f":s=48000:d={SECONDS}")


def source_wav(ffmpeg: str, path: str) -> None:
    subprocess.run([ffmpeg, "-hide_banner", "-v", "error", "-y",
                    "-f", "lavfi", "-i", TONE, "-ac", "2", "-ar", "48000",
                    "-c:a", "pcm_s16le", path], check=True)


def drain(enc: codec.Encoder, limit: float = 90.0) -> list[bytes]:
    """Everything the encoder produces, until it stops producing."""
    packets: list[bytes] = []
    started = time.time()
    quiet = 0.0
    while time.time() - started < limit:
        got = enc.packets()
        if got:
            packets.extend(got)
            quiet = 0.0
        else:
            time.sleep(0.05)
            quiet += 0.05
            if packets and quiet > 2.0:
                break
            if not enc.alive and quiet > 1.0:
                break
    return packets


def play(packets: list[bytes], config: bytes, ffmpeg: str,
         batch: int = 8) -> bytes:
    dec = codec.Decoder(framing.CODEC_XHE_AAC, config, ffmpeg=ffmpeg)
    dec.start()
    for i in range(0, len(packets), batch):
        dec.feed(packets[i:i + batch])
    time.sleep(2.5)
    out = dec.pcm()
    dec.stop()
    return out


def secs(pcm: bytes) -> float:
    return len(pcm) / 2 / codec.CHANNELS / codec.SAMPLE_RATE


def correlate(pcm: bytes, wav: str, skip: float = 1.0) -> tuple[float, float]:
    """Correlation with the source, and the gain applied to it.

    The first second is skipped, because exhale levels to -23 LUFS whenever it
    writes to a pipe and the leveller takes about that long to settle: over
    second zero the gain slides from 0.62 to 0.21, and correlating across a
    moving gain measures the slide rather than the codec.
    """
    got = np.frombuffer(pcm, dtype="<i2").astype(np.float64)
    src = np.frombuffer(open(wav, "rb").read()[78:], dtype="<i2").astype(np.float64)
    start = int(skip * codec.SAMPLE_RATE) * codec.CHANNELS
    n = min(len(got), len(src)) // 2 * 2
    if n - start < codec.SAMPLE_RATE:
        return 0.0, 0.0
    a, b = got[start:n], src[start:n]
    gain = float(np.sqrt(np.mean(a ** 2)) / max(np.sqrt(np.mean(b ** 2)), 1e-9))
    return float(np.corrcoef(a, b)[0, 1]), gain


def main() -> int:
    ffmpeg = codec.find_ffmpeg()
    ok, why = exhale.usable()
    print(f"ffmpeg  {ffmpeg}")
    print(f"exhale  {exhale.find_exhale()}")
    if not ok:
        print(f"\n{why}")
        return 1

    tmp = tempfile.mkdtemp(prefix="xhecheck")
    wav = os.path.join(tmp, "tone.wav")
    source_wav(ffmpeg, wav)
    expected = round(SECONDS * codec.SAMPLE_RATE / exhale.FRAME_SAMPLES)
    failures = 0

    print(f"\n{SECONDS:.0f} s of tone, about {expected} access units expected\n")
    print("  rung    units  config   kbps   decoded   correlation   gain")
    for letter, nominal in exhale.PRESETS:
        enc = codec.Encoder(wav, "xhe", nominal, ffmpeg=ffmpeg)
        enc.start()
        packets = drain(enc)
        config, error = enc.config, enc.error
        enc.stop()
        if not packets:
            print(f"  {nominal // 1000:>3}k    -- nothing encoded: {error}")
            failures += 1
            continue
        pcm = play(packets, config, ffmpeg)
        rate = 8 * sum(map(len, packets)) / max(secs(pcm), 1e-9)
        r, gain = correlate(pcm, wav)
        good = (abs(len(packets) - expected) <= 2
                and secs(pcm) > SECONDS - 0.5 and r > 0.99)
        failures += not good
        print(f"  {nominal // 1000:>3}k    {len(packets):>4}   {len(config):>3} B  "
              f"{rate / 1000:5.1f}   {secs(pcm):5.2f} s      {r:.4f}    {gain:.3f}"
              f"{'' if good else '   <-- FAILED'}")

    # A real xHE-AAC file to relay, which exhale can now make.
    print()
    m4a = os.path.join(tmp, "source.m4a")
    subprocess.run([exhale.find_exhale(), "b", "s", str(exhale.INDEP_PERIOD),
                    wav, m4a], capture_output=True, check=True)
    info = codec.probe(m4a, ffmpeg=ffmpeg)
    through = info["passthrough"]
    print(f"passthrough of {info['label']}")
    print(f"  probe says: {through['ok']}, id {through['codec_id']} "
          f"({'xHE-AAC' if through['codec_id'] == framing.CODEC_XHE_AAC else '?'})")
    if not through["ok"] or through["codec_id"] != framing.CODEC_XHE_AAC:
        print("  <-- FAILED: an xHE-AAC source was not recognised as one")
        return 1

    enc = codec.Encoder(m4a, "xhe", 48000, ffmpeg=ffmpeg,
                        passthrough=through["codec_id"])
    enc.start()
    relayed = drain(enc)
    config = enc.config
    enc.stop()

    # What the file itself contains, read a different way, to compare against.
    reader = fmp4.Reader()
    remux = subprocess.run(
        [ffmpeg, "-hide_banner", "-v", "error", "-i", m4a, "-c:a", "copy",
         "-movflags", "frag_keyframe+empty_moov+default_base_moof",
         "-frag_duration", "200000", "-f", "mp4", "pipe:1"],
        capture_output=True, check=True).stdout
    original = reader.feed(remux)
    same = relayed == original
    pcm = play(relayed, config, ffmpeg) if relayed else b""
    r, gain = correlate(pcm, wav) if pcm else (0.0, 0.0)
    print(f"  {len(relayed)} units relayed, {len(original)} in the source, "
          f"bit-exact {same}")
    print(f"  decoded {secs(pcm):.2f} s, correlation {r:.4f}, gain {gain:.3f}")
    failures += not (same and r > 0.99)

    print()
    print("all checks passed" if not failures else f"{failures} FAILED")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
