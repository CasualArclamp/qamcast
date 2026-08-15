"""End-to-end loopback through qamcore, with no soundcard involved.

    python tools/loopback.py                     WIDE, top MODCOD, clean
    python tools/loopback.py --profile RADIO --modcod 7 --channel radio
    python tools/loopback.py --snr 22 --frames 40

Runs the whole chain -- transport, FEC, framing, modulation, channel,
demodulation -- and reports what came back. This is the test that matters:
every module passing on its own still leaves the interfaces between them
unverified, and that is where wire formats go wrong.
"""

from __future__ import annotations

import argparse
import os
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from qamcore import channel as CH  # noqa: E402
from qamcore import demodulator as D  # noqa: E402
from qamcore import framing, modulator, ofdm, profiles, transport  # noqa: E402


def run(profile_name: str, modcod_index: int, preset: str, snr: float | None,
        frames: int, verbose: bool) -> int:
    profile = profiles.get_profile(profile_name)
    modcod = profiles.MODCOD_BY_INDEX[modcod_index]
    cap = profile.capacity(modcod)

    cfg = CH.PRESETS[preset]
    if snr is not None:
        cfg = CH.ChannelConfig(**{**cfg.__dict__, "snr_db": snr})
    cfg.seed = 12345

    print(f"{profile.name} / {modcod}  ({cap.net_bitrate/1000:.1f} kbps net)")
    print(f"channel: {cfg.summary()}")
    print()

    tx = transport.TransmitChain(profile, modcod)
    mod = (ofdm.CodedModulator(profile) if profile.is_ofdm
           else modulator.Modulator(profile))
    chan = CH.Channel(profile, cfg)
    dem = (ofdm.CodedDemodulator(profile) if profile.is_ofdm
           else D.Demodulator(profile))
    rx = transport.ReceiveChain(profile, modcod)

    pad = transport.Pad("QAM TEST", "Loopback", "qamcore")
    payloads: list[bytes] = []
    sent_seq: list[bytes] = []
    audio_sent: list[bytes] = []

    rng = np.random.default_rng(7)
    t0 = time.time()
    audio_i = 0
    for n in range(frames):
        while tx.backlog < cap.frame_bytes * 2:
            blob = bytes(rng.integers(0, 256, 400).astype(np.uint8))
            tx.push_audio(blob)
            audio_sent.append(blob)
            audio_i += 1
            if audio_i % 40 == 0:
                tx.push_pad(pad)
        payload, il, rsp = tx.next_frame()
        payloads.append(payload.tobytes())
        count = n % framing.FRAME_COUNT_MOD
        hdr = framing.Header(modcod.index, framing.CODEC_OPUS, il, rsp, count)
        sent_seq.append(payload.tobytes())
        chunk = mod.modulate_frame(modcod, hdr, payload)
        dem.feed(chan.process(chunk))
    encode_time = time.time() - t0

    t0 = time.time()
    results = dem.frames()
    decode_time = time.time() - t0

    locked = [r for r in results if r.locked]
    print(f"frames sent {frames}, demodulated {len(results)}, header OK {len(locked)}")
    if not locked:
        print("NO LOCK")
        return 1

    # The counter in the header wraps every FRAME_COUNT_MOD frames, so it
    # cannot index the transmit list on its own -- a run longer than that
    # would compare against the wrong frame and report failures that are
    # entirely the test's own doing. Unwrap it into an absolute index first.
    exact = 0
    absolute = None
    prev = None
    for r in locked:
        if r.payload is None or r.header is None:
            continue
        c = r.header.frame_count
        if absolute is None:
            absolute = c
        else:
            absolute += (c - prev) % framing.FRAME_COUNT_MOD
        prev = c
        if absolute < len(sent_seq) and r.payload.tobytes() == sent_seq[absolute]:
            exact += 1
    print(f"payload bit-exact: {exact}/{len(locked)} frames")

    ev = np.array([r.evm_db for r in locked])
    fo = np.array([r.freq_offset_hz for r in locked])
    tp = np.array([r.timing_ppm for r in locked])
    print(f"EVM  {ev.mean():6.1f} dB  (min {ev.min():.1f})")
    print(f"freq {fo.mean():+6.2f} Hz  (spread {fo.std():.2f})")
    print(f"clock{tp.mean():+6.1f} ppm")
    print(f"sync corr {np.mean([r.corr_peak for r in locked]):.3f} (1.0 = perfect)")

    packets = []
    for r in locked:
        if r.payload is not None:
            # With the frame counter, exactly as rx.py feeds it. Leaving it off
            # looked harmless and was not: without a counter the chain cannot
            # measure a gap, so a single missed frame takes the resync path
            # instead of the bridging one and throws away the several seconds
            # of interleaver history that the deep interleaver exists to
            # protect. On a channel that loses the odd frame that turned a
            # recoverable gap into no audio at all, and made the tool report a
            # failure the receiver itself does not have.
            packets.extend(rx.push_frame(r.payload, r.header.il_phase,
                                         r.header.rs_phase,
                                         r.header.frame_count))
    got_audio = [pl for t, pl in packets if t == transport.PKT_AUDIO]
    got_pad = [pl for t, pl in packets if t == transport.PKT_PAD]
    # The receiver misses the first frame (interpolator warm-up) and one
    # interleaver depth is still in flight, so the recovered stream is a
    # contiguous *window* of what was sent. Find where it starts, then require
    # it to match exactly from there.
    good = 0
    offset = -1
    if got_audio:
        try:
            offset = audio_sent.index(got_audio[0])
            good = sum(1 for i, pl in enumerate(got_audio)
                       if offset + i < len(audio_sent) and pl == audio_sent[offset + i])
        except ValueError:
            offset = -1
    print()
    print(f"audio packets recovered {len(got_audio)} (from #{offset}), bit-exact {good}")
    if got_pad:
        print(f"PAD: {transport.Pad.decode(got_pad[-1])}")
    print(f"RS: {rx.stats}")
    print()
    audio_seconds = frames * profile.frame_duration
    print(f"{audio_seconds:.1f} s of audio: modulate {encode_time:.2f} s, "
          f"demodulate {decode_time:.2f} s "
          f"({audio_seconds/max(decode_time,1e-9):.1f}x real time)")

    # What counts is the audio out, not whether every frame arrived
    # undamaged. Requiring frames to be bit-exact *before* the FEC runs makes
    # the test fail on exactly the channels Reed-Solomon was added for -- the
    # RADIO preset corrects 22 byte errors and delivers every audio packet
    # intact, which is the system working, not failing.
    ok = (len(locked) >= frames - 2
          and len(got_audio) > 0
          and good == len(got_audio))
    print("PASS" if ok else "FAIL")
    if not ok:
        if len(locked) < frames - 2:
            print(f"  only {len(locked)}/{frames} frames locked")
        if got_audio and good != len(got_audio):
            print(f"  {len(got_audio) - good} audio packets corrupt")
        if not got_audio:
            print("  no audio packets recovered")
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", default="WIDE")
    ap.add_argument("--modcod", type=int, default=12)
    ap.add_argument("--channel", default="clean", choices=sorted(CH.PRESETS))
    ap.add_argument("--snr", type=float, default=None)
    ap.add_argument("--frames", type=int, default=30)
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()
    return run(a.profile, a.modcod, a.channel, a.snr, a.frames, a.verbose)


if __name__ == "__main__":
    raise SystemExit(main())
