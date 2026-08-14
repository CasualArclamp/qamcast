"""Measure the EVM each MODCOD actually needs, and print a table to paste
into profiles.py.

    python tools/thresholds.py                 WIDE, every rung
    python tools/thresholds.py --profile RADIO
    python tools/thresholds.py --modcod 12 --frames 20

The `required_evm_db` column in profiles.py starts life as textbook estimates, and
**auto-probe reads that column** -- it is what turns a measured EVM into a
recommended rung. Estimates make the recommendation an educated guess. These
numbers come from pushing real frames through the real chain, so they include
the pilot overhead, the equaliser, the interpolator and every other thing the
textbook figure does not know about.

Threshold means quasi-error-free: the lowest level at which every RS codeword
in the run decodes. That is the right criterion for audio, where one failed
codeword is an audible hole rather than a statistic.

The reported figure is **EVM, not channel SNR**, because EVM is the only one
of the two a receiver can measure -- it has no separate view of the noise. The
channel SNR that produced each threshold is shown alongside for reference; the
two differ by roughly 6 dB and confusing them makes auto-probe optimistic by
exactly that much.
"""

from __future__ import annotations

import argparse
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from qamcore import channel as CH  # noqa: E402
from qamcore import demodulator as D  # noqa: E402
from qamcore import framing, modulator, profiles, transport  # noqa: E402


def trial(profile, modcod, snr_db: float, frames: int, seed: int = 3) -> tuple[bool, float]:
    """Run `frames` frames at `snr_db`. Returns (clean, mean EVM)."""
    rng = np.random.default_rng(seed)
    cap = profile.capacity(modcod)
    tx = transport.TransmitChain(profile, modcod)
    mod = modulator.Modulator(profile)
    chan = CH.Channel(profile, CH.ChannelConfig(snr_db=snr_db, seed=seed))
    dem = D.Demodulator(profile)
    rx = transport.ReceiveChain(profile, modcod)

    for n in range(frames):
        while tx.backlog < cap.frame_bytes * 2:
            tx.push_audio(bytes(rng.integers(0, 256, 400).astype(np.uint8)))
        payload, il, rsp = tx.next_frame()
        hdr = framing.Header(modcod.index, framing.CODEC_OPUS, il, rsp,
                             n % framing.FRAME_COUNT_MOD)
        dem.feed(chan.process(mod.modulate_frame(modcod, hdr, payload)))

    results = [r for r in dem.frames() if r.locked and r.payload is not None]
    if len(results) < frames // 2:
        return False, 0.0
    for r in results:
        rx.push_frame(r.payload, r.header.il_phase, r.header.rs_phase)
    evm = float(np.mean([r.evm_db for r in results]))
    clean = rx.stats.rs_failed == 0 and dem.stats.headers_failed == 0
    return clean, evm


def threshold(profile, modcod, frames: int, lo: float, hi: float,
              step: float = 0.5) -> tuple[float | None, float]:
    """Lowest SNR that decodes cleanly, searched coarsely then refined."""
    # Coarse pass down from the top: a rung that fails even at `hi` is not
    # worth bisecting.
    ok_at = None
    evm_at = 0.0
    snr = hi
    while snr >= lo:
        clean, evm = trial(profile, modcod, snr, frames)
        if clean:
            ok_at, evm_at = snr, evm
            snr -= 2.0
        else:
            break
    if ok_at is None:
        return None, 0.0
    # Refine upward from the first failure in 0.5 dB steps.
    snr = ok_at - 2.0 + step
    while snr <= ok_at:
        clean, evm = trial(profile, modcod, snr, frames)
        if clean:
            return snr, evm
        snr += step
    return ok_at, evm_at


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", default="WIDE")
    ap.add_argument("--frames", type=int, default=14)
    ap.add_argument("--modcod", type=int, default=None)
    a = ap.parse_args()

    profile = profiles.get_profile(a.profile)
    rungs = ([profiles.MODCOD_BY_INDEX[a.modcod]] if a.modcod is not None
             else list(profiles.MODCODS))

    print(f"{profile.name}, {a.frames} frames per point, quasi-error-free")
    print()
    print("idx  modulation      table   measured   delta   channel SNR")
    print("---  --------------  ------  --------  ------   -----------")
    measured: dict[int, float] = {}
    for m in rungs:
        lo = max(0.0, m.required_evm_db - 6.0)
        hi = m.required_evm_db + 10.0
        snr, evm = threshold(profile, m, a.frames, lo, hi)
        if snr is None:
            print(f"{m.index:>3}  {m.name:<14}  {m.required_evm_db:>5.1f}    no lock       -")
            continue
        measured[m.index] = evm
        print(f"{m.index:>3}  {m.name:<14}  {m.required_evm_db:>6.1f}  {evm:>8.1f}  "
              f"{evm - m.required_evm_db:>+6.1f}   {snr:>6.1f} dB")

    if measured:
        print()
        print("Paste into profiles.py MODCODS (required_evm_db is the last field):")
        print()
        for m in profiles.MODCODS:
            v = measured.get(m.index, m.required_evm_db)
            print(f"    Modcod({m.index:>2}, {m.bits_per_symbol}, {m.conv_num}, "
                  f"{m.conv_den}, {m.rs_k}, {v:>5.1f}),")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
