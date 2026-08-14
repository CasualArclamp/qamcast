"""K=7 convolutional code with rate adaptation, and a soft-decision Viterbi.

One mother code, six rates. The mother is the standard NASA/CCSDS K=7 rate-1/2
pair (0o171, 0o133) -- the same code the DMT modem already uses. Rates above
1/2 come from puncturing it with the DVB-S patterns; rates below 1/2 come from
repeating mother bits.

That last choice is a deliberate trade. A purpose-built rate-1/3 or rate-1/4
K=7 code (three or four generators) would beat repetition by roughly 0.2-0.4 dB.
Repetition buys something worth more here: **the decoder never changes.** All
six rates run on one 64-state trellis, because de-adapting is just LLR
bookkeeping -- punctured bits enter as 0.0 (perfect ambiguity, the trellis
ignores them) and repeated bits enter as the sum of their copies (which is
exactly the right combining for independent observations of the same bit).
One decoder is one thing to get right and one thing to keep fast.

The trellis is terminated with six flush bits at the end of every frame. That
costs almost nothing and makes each frame independently decodable, which is
what lets a receiver join a broadcast already in progress.
"""

from __future__ import annotations

import functools

import numpy as np
from numba import njit

K = 7                     # constraint length
STATES = 1 << (K - 1)     # 64
G1 = 0o171
G2 = 0o133
FLUSH_BITS = K - 1        # 6

# Rate adaptation patterns, indexed by (num, den).
#
# Each entry is a period over mother-code output bits in transmission order
# (X0, Y0, X1, Y1, ...). 0 drops the bit, 1 sends it once, 2 sends it twice.
# Puncturing patterns are the DVB-S ones; the sub-1/2 entries are repetition.
_PATTERNS: dict[tuple[int, int], tuple[int, ...]] = {
    (1, 4): (2, 2),                          # X,X,Y,Y      -> 4 bits per input
    (1, 3): (2, 1, 1, 2),                    # balanced repeat over 2 inputs
    (1, 2): (1, 1),                          # mother code, untouched
    (2, 3): (1, 1, 0, 1),                    # DVB-S X=10  Y=11
    (3, 4): (1, 1, 0, 1, 1, 0),              # DVB-S X=101 Y=110
    (5, 6): (1, 1, 0, 1, 1, 0, 0, 1, 1, 0),  # DVB-S X=10101 Y=11010
}


def pattern(num: int, den: int) -> tuple[int, ...]:
    try:
        return _PATTERNS[(num, den)]
    except KeyError:
        raise ValueError(f"no rate adaptation pattern for {num}/{den}") from None


@functools.lru_cache(maxsize=None)
def _pattern_arrays(num: int, den: int) -> tuple[np.ndarray, int, int]:
    """(pattern, mother bits per period, channel bits per period)."""
    p = np.asarray(pattern(num, den), dtype=np.int64)
    return p, len(p), int(p.sum())


TOP = K - 2                    # bit position of the newest bit in the state
STATE_MASK = (1 << TOP) - 1    # bits that survive a shift


@functools.lru_cache(maxsize=None)
def _trellis() -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """(out_x, out_y, next_state), each indexed [state, input_bit].

    The state is a shift register in the ordinary sense: the newest previous
    input sits in the *top* bit and everything ages downward, so

        full = (b << 6) | s   has   bit 6 = b_n, bit 5 = b_(n-1), ... bit 0 = b_(n-6)

    which is the alignment the generator polynomials are written against.
    Shifting the other way -- newest bit at the bottom -- reverses the register
    against the polynomials and quietly yields a *different, weaker* code: the
    unpunctured pair below measures dfree 8 instead of 10, and the punctured
    rates degrade to the point of being catastrophic. It still encodes and
    decodes perfectly on a clean channel, so nothing looks wrong until you put
    noise through it. See tools/dfree.py.
    """
    out_x = np.zeros((STATES, 2), dtype=np.uint8)
    out_y = np.zeros((STATES, 2), dtype=np.uint8)
    nxt = np.zeros((STATES, 2), dtype=np.int64)
    for s in range(STATES):
        for b in (0, 1):
            full = (b << (K - 1)) | s
            out_x[s, b] = bin(full & G1).count("1") & 1
            out_y[s, b] = bin(full & G2).count("1") & 1
            nxt[s, b] = (s >> 1) | (b << TOP)
    return out_x, out_y, nxt


@functools.lru_cache(maxsize=None)
def _predecessors() -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """For each state s', its two predecessors and the outputs on those edges.

    ``s'`` top bit is the input that caused the transition, and its remaining
    bits are the predecessor's upper bits -- so the two predecessors differ
    only in the bit that just fell off the bottom.
    """
    out_x, out_y, _ = _trellis()
    prev = np.zeros((STATES, 2), dtype=np.int64)
    px = np.zeros((STATES, 2), dtype=np.int8)
    py = np.zeros((STATES, 2), dtype=np.int8)
    for sp in range(STATES):
        b = sp >> TOP
        for j in (0, 1):
            p = ((sp & STATE_MASK) << 1) | j
            prev[sp, j] = p
            px[sp, j] = out_x[p, b]
            py[sp, j] = out_y[p, b]
    return prev, px, py


# --------------------------------------------------------------------------
# Encoder
# --------------------------------------------------------------------------

@njit(cache=True)
def _encode_mother(bits, out_x, out_y, nxt, out):
    s = 0
    for i in range(bits.shape[0]):
        b = bits[i]
        out[2 * i] = out_x[s, b]
        out[2 * i + 1] = out_y[s, b]
        s = nxt[s, b]
    return s


def encode(bits: np.ndarray, num: int, den: int) -> np.ndarray:
    """Encode ``bits`` (plus flush) and rate-adapt. Returns channel bits.

    Pads with extra zeros past the flush when the mother stream would not be a
    whole number of pattern periods. That is free: the trellis is already
    parked in state 0 by the flush, so the extra bits only keep it there, and
    the decoder ignores them because it reads exactly as many mother LLRs as
    ``info_bits + FLUSH_BITS`` calls for. Callers sizing a frame should use
    :func:`max_info_bits`, which lands on an aligned length anyway.
    """
    bits = np.asarray(bits, dtype=np.uint8).ravel()
    _, per_in, _ = _pattern_arrays(num, den)
    total = len(bits) + FLUSH_BITS
    step = per_in // 2  # per_in is even for every pattern
    pad = (-total) % step
    padded = np.concatenate([bits, np.zeros(FLUSH_BITS + pad, dtype=np.uint8)])
    out_x, out_y, nxt = _trellis()
    mother = np.empty(2 * len(padded), dtype=np.uint8)
    _encode_mother(padded, out_x, out_y, nxt, mother)
    return adapt(mother, num, den)


def adapt(mother: np.ndarray, num: int, den: int) -> np.ndarray:
    """Apply the puncture/repeat pattern to mother-code bits."""
    p, per_in, _ = _pattern_arrays(num, den)
    if len(mother) % per_in:
        raise ValueError(
            f"{len(mother)} mother bits is not a multiple of the {per_in}-bit "
            f"{num}/{den} pattern period"
        )
    reps = np.tile(p, len(mother) // per_in)
    return np.repeat(mother, reps)


def deadapt(llr: np.ndarray, num: int, den: int) -> np.ndarray:
    """Inverse of :func:`adapt`, in the LLR domain.

    Punctured positions become 0.0 -- a bit the trellis has no opinion about.
    Repeated positions become the sum of their copies, which is the correct
    combining rule for repeated observations under an LLR sum convention.
    """
    p, per_in, per_out = _pattern_arrays(num, den)
    if len(llr) % per_out:
        raise ValueError(
            f"{len(llr)} channel LLRs is not a multiple of the {per_out}-bit "
            f"{num}/{den} output period"
        )
    periods = len(llr) // per_out
    out = np.zeros(periods * per_in, dtype=np.float64)
    src = 0
    for i, count in enumerate(p):
        if count == 0:
            continue
        idx = np.arange(periods) * per_in + i
        for _ in range(int(count)):
            out[idx] += llr[src::per_out][:periods]
            src += 1
    return out


def channel_bits_for(info_bits: int, num: int, den: int) -> int:
    """Channel bits produced by encoding ``info_bits`` info bits."""
    _, per_in, per_out = _pattern_arrays(num, den)
    mother = 2 * (info_bits + FLUSH_BITS)
    if mother % per_in:
        raise ValueError(f"{info_bits} info bits does not align to the {num}/{den} pattern")
    return mother // per_in * per_out


def max_info_bits(channel_bits: int, num: int, den: int) -> int:
    """Largest info-bit count whose encoding fits in ``channel_bits``.

    Rounded down to keep the mother stream a whole number of pattern periods,
    so callers never have to reason about a ragged final period.
    """
    _, per_in, per_out = _pattern_arrays(num, den)
    periods = channel_bits // per_out
    info = periods * per_in // 2 - FLUSH_BITS
    return max(0, info)


# --------------------------------------------------------------------------
# Soft-decision Viterbi
# --------------------------------------------------------------------------

@njit(cache=True)
def _viterbi(llr, prev, px, py, n_steps, terminated):
    NEG = -1e18
    pm = np.full(STATES, NEG, dtype=np.float64)
    pm[0] = 0.0
    dec = np.empty((n_steps, STATES), dtype=np.uint8)
    nm = np.empty(STATES, dtype=np.float64)

    for t in range(n_steps):
        lx = llr[2 * t]
        ly = llr[2 * t + 1]
        for sp in range(STATES):
            best = NEG
            bestj = 0
            for j in range(2):
                p = prev[sp, j]
                # LLR convention: positive means bit 0, so a bit expected to be
                # 0 contributes +llr and a bit expected to be 1 contributes -llr.
                bm = (1.0 - 2.0 * px[sp, j]) * lx + (1.0 - 2.0 * py[sp, j]) * ly
                cand = pm[p] + bm
                if cand > best:
                    best = cand
                    bestj = j
            nm[sp] = best
            dec[t, sp] = bestj
        for i in range(STATES):
            pm[i] = nm[i]
        # Renormalise to stop the metrics running away over a long frame.
        m = pm[0]
        for i in range(1, STATES):
            if pm[i] > m:
                m = pm[i]
        for i in range(STATES):
            pm[i] -= m

    if terminated:
        s = 0
    else:
        s = 0
        best = pm[0]
        for i in range(1, STATES):
            if pm[i] > best:
                best = pm[i]
                s = i

    out = np.empty(n_steps, dtype=np.uint8)
    for t in range(n_steps - 1, -1, -1):
        out[t] = s >> (K - 2)   # newest bit lives in the top of the state
        j = dec[t, s]
        s = prev[s, j]
    return out


def decode(llr: np.ndarray, num: int, den: int, info_bits: int) -> np.ndarray:
    """Decode channel LLRs back to ``info_bits`` information bits.

    ``llr`` follows the constellation module's convention: positive means the
    bit is more likely zero. The trellis both starts and ends in state 0
    because the encoder flushes, so the traceback needs no guessing.
    """
    mother = deadapt(np.asarray(llr, dtype=np.float64).ravel(), num, den)
    n_steps = info_bits + FLUSH_BITS
    need = 2 * n_steps
    if len(mother) < need:
        raise ValueError(
            f"need {need} mother LLRs for {info_bits} info bits, got {len(mother)}"
        )
    prev, px, py = _predecessors()
    bits = _viterbi(mother[:need], prev, px, py, n_steps, True)
    return bits[:info_bits]
