"""Free distance of the punctured convolutional codes, by Dijkstra.

Run this after touching anything in conv.py. A convolutional code that has
been broken subtly still encodes and decodes perfectly on a clean channel --
the damage only shows up as lost coding gain once there is noise, which is
exactly the kind of bug that survives a test suite and ships.

The numbers to expect for K=7 (171,133):

    rate 1/2 -> 10    rate 2/3 -> 6    rate 3/4 -> 5    rate 5/6 -> 4

Because convolutional codes are linear, the minimum-weight error event is just
the minimum-weight nonzero codeword: a path that leaves the zero state and
returns to it. Puncturing makes branch weight depend on position within the
pattern period, so the search state carries the phase alongside the trellis
state, and the answer is the minimum over every starting phase.

This module reads its trellis from qamcore.conv rather than rebuilding one.
An earlier version rebuilt it, kept the old convention after conv.py was
fixed, and cheerfully reported the bug as still present.
"""

from __future__ import annotations

import heapq
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from qamcore import conv  # noqa: E402

EXPECTED = {(1, 2): 10, (2, 3): 6, (3, 4): 5, (5, 6): 4}


def dfree(num: int, den: int, pattern=None, max_weight: int = 60) -> int | None:
    """Minimum weight of a nonzero codeword, or None if none found under
    ``max_weight`` (which for a catastrophic code is the honest answer)."""
    pat = list(pattern if pattern is not None else conv.pattern(num, den))
    inphase = len(pat) // 2
    out_x, out_y, nxt = conv._trellis()

    def branch(s: int, b: int, ph: int) -> tuple[int, int]:
        w = pat[2 * ph] * int(out_x[s, b]) + pat[2 * ph + 1] * int(out_y[s, b])
        return w, int(nxt[s, b])

    best = None
    for start in range(inphase):
        # First branch is forced to input 1 so the path is a genuine departure
        # from the all-zero codeword rather than the trivial empty path.
        w0, s0 = branch(0, 1, start)
        pq = [(w0, s0, (start + 1) % inphase)]
        seen: dict[tuple[int, int], int] = {}
        while pq:
            w, s, ph = heapq.heappop(pq)
            if best is not None and w >= best:
                break
            if w > max_weight:
                break
            if s == 0:
                best = w if best is None else min(best, w)
                break
            if seen.get((s, ph), 1 << 30) <= w:
                continue
            seen[(s, ph)] = w
            for b in (0, 1):
                bw, ns = branch(s, b, ph)
                heapq.heappush(pq, (w + bw, ns, (ph + 1) % inphase))
    return best


def main() -> int:
    print(f"K={conv.K}  G1=0o{conv.G1:o}  G2=0o{conv.G2:o}")
    print()
    print("rate   pattern                              dfree  expected")
    bad = 0
    for r in [(1, 4), (1, 3), (1, 2), (2, 3), (3, 4), (5, 6)]:
        d = dfree(*r)
        exp = EXPECTED.get(r)
        if exp is None:
            note = "     (repetition, no standard value)"
        elif d == exp:
            note = ""
        else:
            note = "   <-- WRONG"
            bad += 1
        pat = str(tuple(int(v) for v in conv.pattern(*r)))
        print(f"{r[0]}/{r[1]}    {pat:<36} {str(d):<6} {exp if exp else '-'}{note}")
    print()
    print("FAIL" if bad else "all punctured rates match the published free distances")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
