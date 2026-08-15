"""No-hardware self test: real audio, right through the modem and back.

    python tools/selftest.py                  WIDE48 at 64k, the default
    python tools/selftest.py WIDE 128k
    python tools/selftest.py WIDE44 48k aac

Encodes a generated test tone, modulates it to `tx.wav`, demodulates that file
back, decodes it, and checks what came out. No sound card is touched, so this
answers "is the install working" separately from "is my audio wiring right" --
which are the two things a first test otherwise conflates.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import wave

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from qamcore import codec, profiles  # noqa: E402

TONE_L, TONE_R = 440, 660
SECONDS = 30


def make_tone(path: str, ffmpeg: str) -> None:
    subprocess.run(
        [ffmpeg, "-hide_banner", "-loglevel", "error",
         "-f", "lavfi", "-i", f"sine=frequency={TONE_L}:duration={SECONDS}:sample_rate=48000",
         "-f", "lavfi", "-i", f"sine=frequency={TONE_R}:duration={SECONDS}:sample_rate=48000",
         "-filter_complex", "[0][1]amerge=inputs=2,volume=0.5",
         "-ac", "2", "-ar", "48000", "-y", path],
        check=True)


def dominant(x: np.ndarray, rate: int) -> float:
    if len(x) < 16384:
        return 0.0
    seg = x[len(x) // 3:][:65536]
    if len(seg) < 16384:
        seg = x[:65536]
    f = np.fft.rfftfreq(len(seg), 1 / rate)
    s = np.abs(np.fft.rfft(seg * np.hanning(len(seg))))
    return float(f[np.argmax(s)])


def main() -> int:
    profile_name = sys.argv[1] if len(sys.argv) > 1 else profiles.DEFAULT_PROFILE
    bitrate_txt = sys.argv[2] if len(sys.argv) > 2 else "64k"
    codec_name = sys.argv[3] if len(sys.argv) > 3 else "opus"

    try:
        profile = profiles.get_profile(profile_name)
    except ValueError as exc:
        print(exc)
        return 1
    bitrate = int(float(bitrate_txt.rstrip("kK")) * (1000 if bitrate_txt[-1] in "kK" else 1))
    try:
        modcod = profile.modcod_for_bitrate(bitrate)
    except ValueError as exc:
        print(f"FAIL  {exc}")
        return 1

    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    tone = os.path.join(here, "selftest_tone.wav")
    txwav = os.path.join(here, "tx.wav")
    out = os.path.join(here, "selftest_out.wav")

    print(f"profile   {profile.name}  ({profile.sample_rate} Hz card, "
          f"{profile.symbol_rate} Bd, {profile.band[0]:.0f}-{profile.band[1]:.0f} Hz)")
    print(f"audio     {codec_name} at {bitrate/1000:.0f} kbps")
    print(f"modcod    {modcod}  ({profile.net_bitrate(modcod)/1000:.1f} kbps channel)")
    print(f"delay     {profiles.interleaver_delay(profile, modcod):.1f} s of interleaving")
    print()

    try:
        ffmpeg = codec.find_ffmpeg()
    except codec.CodecError as exc:
        print(f"FAIL  {exc}")
        return 1

    print("[1/3] generating test tone ...", flush=True)
    make_tone(tone, ffmpeg)

    print("[2/3] transmitting to tx.wav ...", flush=True)
    import tx as TX
    t = TX.Transmitter()
    t.start({"source": tone, "codec": codec_name, "bitrate": bitrate,
             "profile": profile.name, "device": "wav", "station": "SELFTEST",
             "title": "Test Tone", "artist": "qamcore"})
    deadline = time.time() + SECONDS + 6
    while time.time() < deadline and not t.error:
        time.sleep(0.5)
    t.stop()
    if t.error:
        print(f"FAIL  transmit: {t.error}")
        return 1
    with wave.open(txwav, "rb") as w:
        sent = w.getnframes() / w.getframerate()
    print(f"      {sent:.1f} s of passband audio at {profile.sample_rate} Hz")

    print("[3/3] receiving tx.wav ...", flush=True)
    import rx as RX
    r = RX.Receiver()
    r.start({"profile": profile.name, "device": "wav", "outdev": "none",
             "record": out})
    deadline = time.time() + sent + 15
    while time.time() < deadline:
        time.sleep(0.5)
        if not r._state.get("running") and r._state.get("frames"):
            break
    state = dict(r._state)
    r.stop()

    print()
    ok = True
    locked = state.get("frames", 0) > 0
    print(f"  lock            {'yes' if locked else 'NO'}   "
          f"sync {state.get('corr') or 0:.3f}")
    print(f"  modcod          {state.get('modcod') or '-'}  (detected from the header)")
    print(f"  codec           {state.get('codec') or '-'}")
    print(f"  EVM             {state.get('evm') or 0:.1f} dB   "
          f"needs {modcod.required_evm_db:.1f}")
    print(f"  RS failures     {state.get('rs_failed', 0)}")
    print(f"  PAD             {state.get('pad_station') or '-'} / "
          f"{state.get('pad_title') or '-'}")

    if not locked:
        ok = False
    if state.get("rs_failed"):
        ok = False

    if os.path.exists(out):
        with wave.open(out, "rb") as w:
            n, rate = w.getnframes(), w.getframerate()
            pcm = np.frombuffer(w.readframes(n), dtype="<i2").astype(float)
        secs = n / rate
        left = dominant(pcm[0::2], rate)
        right = dominant(pcm[1::2], rate)
        print(f"  audio out       {secs:.1f} s   "
              f"(sent {sent:.1f} s, minus interleaver)")
        print(f"  tones           {left:.0f} Hz left / {right:.0f} Hz right   "
              f"(sent {TONE_L} / {TONE_R})")
        if secs < 1.0 or abs(left - TONE_L) > 20 or abs(right - TONE_R) > 20:
            ok = False
    else:
        print("  audio out       NONE")
        ok = False

    for f in (tone,):
        try:
            os.remove(f)
        except OSError:
            pass

    print()
    print("PASS - the modem works end to end on this machine." if ok else
          "FAIL - see above.")
    print(f"       recovered audio: {out}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
