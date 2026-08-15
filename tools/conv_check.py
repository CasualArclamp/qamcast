"""Check the Viterbi against a plain reference implementation.

The fast decoder packs its decisions into bit words, tabulates the four
possible branch metrics and renormalises the path metrics only occasionally.
None of that is supposed to change a single output bit, and "supposed to" is
not a test -- so this runs the textbook version alongside it and requires the
two to agree exactly, over every rate, at signal levels from barely-decodable
to numerically extreme.

    python tools/conv_check.py [trials-per-rate]
"""

from __future__ import annotations

import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from qamcore import conv  # noqa: E402


def tables():
    """Predecessors and edge output bits, built straight from the trellis.

    Deliberately not imported from conv._predecessors: that now returns only
    half the branch indices, because the fast decoder exploits a symmetry. A
    reference that borrowed the same tables would be assuming the very thing
    it is here to check.
    """
    out_x, out_y, _ = conv._trellis()
    prev = np.zeros((conv.STATES, 2), dtype=np.int64)
    px = np.zeros((conv.STATES, 2), dtype=np.int64)
    py = np.zeros((conv.STATES, 2), dtype=np.int64)
    for sp in range(conv.STATES):
        b = sp >> conv.TOP
        for j in (0, 1):
            p = ((sp & conv.STATE_MASK) << 1) | j
            prev[sp, j] = p
            px[sp, j] = out_x[p, b]
            py[sp, j] = out_y[p, b]
    return prev, px, py


def reference(llr, prev, px, py, n_steps):
    """The obvious implementation: a byte of decision per state per step, the
    metric worked out from the output bits per edge, renormalised every step."""
    NEG = -1e18
    pm = np.full(conv.STATES, NEG)
    pm[0] = 0.0
    dec = np.empty((n_steps, conv.STATES), dtype=np.uint8)
    for t in range(n_steps):
        lx, ly = llr[2 * t], llr[2 * t + 1]
        nm = np.empty(conv.STATES)
        for sp in range(conv.STATES):
            best, bestj = NEG, 0
            for j in (0, 1):
                bm = (1.0 - 2.0 * px[sp, j]) * lx + (1.0 - 2.0 * py[sp, j]) * ly
                cand = pm[prev[sp, j]] + bm
                if cand > best:
                    best, bestj = cand, j
            nm[sp] = best
            dec[t, sp] = bestj
        pm = nm - nm.max()
    s = 0
    out = np.empty(n_steps, dtype=np.uint8)
    for t in range(n_steps - 1, -1, -1):
        out[t] = s >> (conv.K - 2)
        s = prev[s, dec[t, s]]
    return out


def main() -> int:
    trials = int(sys.argv[1]) if len(sys.argv) > 1 else 60
    rng = np.random.default_rng(20260815)
    prev, px, py = tables()
    rates = [(1, 4), (1, 3), (1, 2), (2, 3), (3, 4), (5, 6)]
    # Scales spanning what the demapper actually produces: a few dB of margin
    # up to the clamped-noise case on a clean channel, where LLRs reach 1e12
    # and a decoder that renormalises lazily has the most to lose.
    scales = (0.5, 3.0, 1e3, 1e12)
    worst = 0
    for num, den in rates:
        for trial in range(trials):
            info = int(rng.integers(48, 400)) * 2
            bits = rng.integers(0, 2, info).astype(np.uint8)
            chan = conv.encode(bits, num, den)
            scale = scales[trial % len(scales)]
            # Antipodal LLRs plus noise: positive means zero, per the convention.
            llr = (1.0 - 2.0 * chan) * scale
            llr = llr + rng.normal(0.0, scale * 0.8, len(llr))

            fast = conv.decode(llr, num, den, info)
            mother = conv.deadapt(np.asarray(llr, float), num, den)
            n_steps = info + conv.FLUSH_BITS
            ref = reference(mother[:2 * n_steps], prev, px, py, n_steps)[:info]

            bad = int(np.count_nonzero(fast != ref))
            worst = max(worst, bad)
            if bad:
                print(f"FAIL {num}/{den} trial {trial}: {bad} of {info} bits "
                      f"differ from the reference decoder (scale {scale:g})")
                return 1
        print(f"  {num}/{den}  {trials} frames, every bit identical")
    print(f"\nPASS - fast and reference decoders agree exactly "
          f"({len(rates) * trials} frames)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
