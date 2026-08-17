"""What subcarrier spacing is worth, measured rather than argued.

With the spacing fixed and the carrier count setting the bandwidth, the two
dials finally mean separate things -- so "which spacing?" becomes a question
with an answer, and this is where it is answered.

The figure of merit is **bits per second per hertz occupied**, because
bandwidth is now what the carrier count buys. Spacing does not change it much
through any one mechanism; it changes it through three, and they do not all
point the same way:

    the cyclic prefix is a fixed fraction of the symbol, so its cost is flat
    the preamble and MODCOD codeword are two symbols per frame, and a frame is
        a fixed *duration* -- so a longer symbol means fewer symbols to spread
        those two across, and the overhead grows as the spacing narrows
    the pilot stride tightens below 48 carriers, which is a carrier-count
        effect rather than a spacing one, and is why 48 is the efficient floor

Efficiency therefore *rises* as the spacing widens. It is not the whole story,
which is the point of the other two columns:

    echo    what the prefix absorbs outright. Below about 0.6 ms there is no
            reason to be running OFDM at all -- that is where the
            single-carrier equaliser already reaches, and it costs no prefix.
    offset  the largest carrier error the estimator can still measure. It
            fits pilot phase against symbol index, so a longer symbol folds
            sooner. The `radio` channel preset drifts 15 Hz.

    python tools/spacing.py                 the table
    python tools/spacing.py --run           and actually decode at each one
    python tools/spacing.py --run --channel acoustic
"""

from __future__ import annotations

import argparse
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from qamcore import channel as CH  # noqa: E402
from qamcore import framing, ofdm, profiles, transport  # noqa: E402

# Where the single-carrier equaliser gives out, from the README's measurement.
# A spacing whose prefix does not comfortably beat this is a spacing with no
# reason to exist.
SINGLE_CARRIER_ECHO_MS = 0.6


def build(sample_rate: int, spacing: float, carriers: int, band: tuple,
          cp_fraction: int) -> profiles.Profile | None:
    try:
        return profiles.make_ofdm_profile(
            sample_rate, band[0], band[1], carriers, cp_fraction,
            spacing_hz=spacing, name=f"S{spacing:g}-{carriers}")
    except ValueError:
        return None


def widest(sample_rate: int, band: tuple, spacing: float, cp_fraction: int):
    """The most carriers of this spacing the band has room for."""
    best = None
    for carriers in profiles.OFDM_CARRIER_CHOICES:
        p = build(sample_rate, spacing, carriers, band, cp_fraction)
        if p is not None:
            best = (carriers, p)
    return best


def table(sample_rate: int, band: tuple, carriers: int | None, cp_fraction: int,
          modcod: profiles.Modcod) -> list[tuple]:
    """One row per spacing. ``carriers`` None means "as many as fit"."""
    rows = []
    for spacing in ofdm.OFDM_SPACING_CHOICES:
        if carriers is None:
            got = widest(sample_rate, band, spacing, cp_fraction)
            if got is None:
                continue
            n, p = got
        else:
            n, p = carriers, build(sample_rate, spacing, carriers, band,
                                   cp_fraction)
            if p is None:
                continue
        geo = p.geometry
        rate = p.net_bitrate(modcod)
        rows.append((spacing, n, geo, rate, rate / geo.bandwidth))
    return rows


def show(sample_rate: int, band: tuple, carriers: int | None, cp_fraction: int,
         modcod: profiles.Modcod) -> list[tuple]:
    rows = table(sample_rate, band, carriers, cp_fraction, modcod)
    if not rows:
        print("nothing fits")
        return rows
    best = max(rows, key=lambda r: r[4])
    usable = [r for r in rows
              if r[2].max_delay_spread * 1000 >= SINGLE_CARRIER_ECHO_MS]
    what = f"{carriers} carriers" if carriers else "as many carriers as fit"
    print(f"{sample_rate} Hz card, {band[0]:.0f}-{band[1]:.0f} Hz, {what}, "
          f"prefix 1/{cp_fraction}, {modcod}")
    print(f"{'spacing':>8} {'fft':>6} {'carr':>5} {'band kHz':>10} "
          f"{'echo ms':>8} {'offset':>7} {'sym/frm':>8} {'kbps':>7} "
          f"{'bps/Hz':>7}")
    for spacing, n, geo, rate, eff in rows:
        lo, hi = geo.band
        mark = ""
        if spacing == best[0]:
            mark += "  <- most efficient"
        if geo.max_delay_spread * 1000 < SINGLE_CARRIER_ECHO_MS:
            mark += "  (echo below single carrier)"
        print(f"{spacing:>7.0f}H {geo.fft:>6} {n:>5} "
              f"{lo/1000:>4.1f}-{hi/1000:<5.1f} "
              f"{geo.max_delay_spread*1000:>8.2f} {geo.max_freq_offset:>6.0f}H "
              f"{geo.symbols_per_frame:>8} {rate/1000:>7.1f} {eff:>7.2f}{mark}")
    if usable:
        pick = max(usable, key=lambda r: r[4])
        print(f"\nmost efficient spacing that is also worth running OFDM for: "
              f"{pick[0]:g} Hz")
        print(f"  {pick[4]:.2f} bps/Hz, {pick[3]/1000:.1f} kbps at "
              f"{pick[1]} carriers, {pick[2].max_delay_spread*1000:.2f} ms of "
              f"echo, ±{pick[2].max_freq_offset:.0f} Hz of offset")
    return rows


def decode_at(p: profiles.Profile, modcod: profiles.Modcod, preset: str,
              frames: int) -> tuple[int, int, float]:
    """Run the real chain at this geometry. Returns (locked, sent, EVM)."""
    cfg = CH.PRESETS[preset]
    cfg.seed = 12345
    tx = transport.TransmitChain(p, modcod)
    mod = ofdm.CodedModulator(p)
    chan = CH.Channel(p, cfg)
    dem = ofdm.CodedDemodulator(p)
    cap = p.capacity(modcod)
    rng = np.random.default_rng(7)
    for n in range(frames):
        while tx.backlog < cap.frame_bytes * 2:
            tx.push_audio(bytes(rng.integers(0, 256, 400).astype(np.uint8)))
        payload, il, rsp = tx.next_frame()
        hdr = framing.Header(modcod.index, framing.CODEC_OPUS, il, rsp,
                             n % framing.FRAME_COUNT_MOD)
        dem.feed(chan.process(mod.modulate_frame(modcod, hdr, payload)))
    got = [r for r in dem.frames() if r.locked]
    evm = float(np.mean([r.evm_db for r in got])) if got else float("nan")
    return len(got), frames, evm


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sample-rate", type=int, default=44100)
    ap.add_argument("--carriers", type=int, default=None,
                    help="fix the count; default is as many as the band holds, "
                         "which is what compares spacings fairly")
    ap.add_argument("--cp", type=int, default=8,
                    choices=list(profiles.OFDM_CP_CHOICES))
    ap.add_argument("--modcod", type=int, default=12)
    ap.add_argument("--band", default=None,
                    help="lo,hi Hz; defaults to the card's OFDM profile")
    ap.add_argument("--run", action="store_true",
                    help="decode at each spacing as well as tabulating it")
    ap.add_argument("--channel", default="radio", choices=sorted(CH.PRESETS))
    ap.add_argument("--frames", type=int, default=40)
    a = ap.parse_args()

    if a.band:
        band = tuple(float(v) for v in a.band.split(","))
    else:
        src = next((p for p in profiles.PROFILES.values()
                    if p.is_ofdm and p.sample_rate == a.sample_rate), None)
        if src is None:
            print(f"no OFDM profile at {a.sample_rate} Hz; give --band",
                  file=sys.stderr)
            return 1
        band = (src.ofdm_band_lo, src.ofdm_band_hi)

    modcod = profiles.MODCOD_BY_INDEX[a.modcod]
    rows = show(a.sample_rate, band, a.carriers, a.cp, modcod)
    if not a.run:
        return 0

    print(f"\ndecoding {a.frames} frames per spacing through the "
          f"'{a.channel}' channel")
    cfg = CH.PRESETS[a.channel]
    echo = max((d for d, _ in cfg.multipath), default=0.0) * 1000
    print(f"  that channel: {echo:.2f} ms of delay spread, "
          f"{cfg.freq_offset_hz:g} Hz offset, {cfg.snr_db:g} dB SNR")
    print(f"{'spacing':>8} {'echo ms':>8} {'offset':>7} {'locked':>9} "
          f"{'EVM':>8}")
    for spacing, n, geo, _rate, _eff in rows:
        p = build(a.sample_rate, spacing, n, band, a.cp)
        try:
            ok, sent, evm = decode_at(p, modcod, a.channel, a.frames)
        except Exception as exc:                      # a geometry that cannot run
            print(f"{spacing:>7.0f}H {'':>8} {'':>7} {type(exc).__name__}: {exc}")
            continue
        verdict = "" if ok >= sent - 3 else "   <- loses frames"
        print(f"{spacing:>7.0f}H {geo.max_delay_spread*1000:>8.2f} "
              f"{geo.max_freq_offset:>6.0f}H {ok:>4}/{sent:<4} "
              f"{evm:>7.1f}dB{verdict}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
