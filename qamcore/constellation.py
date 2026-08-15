"""Gray-coded square QAM: mapping, hard slicing and soft demapping.

Every constellation here is square and separable -- I and Q are independent
Gray-coded PAM axes. That is worth stating because it is what makes the soft
demapper cheap: a 256QAM symbol is not searched over 256 points, it is two
16-level PAM searches, and the LLRs come out identical.

Bit order is wire format. For a symbol of ``m = 2k`` bits, the first ``k`` bits
(MSB first) are the I axis and the last ``k`` are Q. The bit pattern is the
Gray code of the level index, not the index itself.

LLR sign convention: **positive means the bit is more likely zero.** The
Viterbi decoder in conv.py assumes this. Flipping it silently turns the
decoder into a very expensive random number generator.
"""

from __future__ import annotations

import functools

import numpy as np
from numba import njit

SUPPORTED = (2, 4, 6, 8)  # bits per symbol: QPSK, 16QAM, 64QAM, 256QAM

# Two ways to lay the same bits out. Which one a link uses is a property of the
# link, not of the MODCOD -- the ladder still says "four bits per symbol at
# rate 3/4" and the family decides whether that is 16QAM or 16APSK. Everything
# downstream of here is unchanged either way.
QAM = "qam"
APSK = "apsk"
FAMILIES = (QAM, APSK)


def _gray_to_binary(g: int) -> int:
    b = g
    shift = 1
    while shift < 32:
        b ^= b >> shift
        shift <<= 1
    return b


@functools.lru_cache(maxsize=None)
def pam_levels(k: int) -> np.ndarray:
    """Level for each ``k``-bit Gray pattern, in units of the level spacing.

    Index into this with the integer formed by the k bits (MSB first) and you
    get the PAM amplitude: ..., -3, -1, +1, +3, ...
    """
    L = 1 << k
    out = np.empty(L, dtype=np.float64)
    for pattern in range(L):
        out[pattern] = 2 * _gray_to_binary(pattern) - (L - 1)
    return out


@functools.lru_cache(maxsize=None)
def _scale(bits_per_symbol: int) -> float:
    """Normalisation for unit average symbol energy."""
    k = bits_per_symbol // 2
    L = 1 << k
    mean_energy = 2.0 * (L * L - 1) / 3.0
    return 1.0 / np.sqrt(mean_energy)


@functools.lru_cache(maxsize=None)
def points(bits_per_symbol: int, family: str = QAM) -> np.ndarray:
    """All constellation points, indexed by the symbol's integer bit pattern."""
    if family == APSK:
        return _apsk_points(bits_per_symbol)
    k = bits_per_symbol // 2
    lv = pam_levels(k)
    s = _scale(bits_per_symbol)
    idx = np.arange(1 << bits_per_symbol)
    i_pat = idx >> k
    q_pat = idx & ((1 << k) - 1)
    return (lv[i_pat] + 1j * lv[q_pat]) * s


# --------------------------------------------------------------------------
# APSK
# --------------------------------------------------------------------------
#
# Points on concentric rings instead of on a square grid. The reason is
# amplifier compression: a square 256QAM has 32 distinct amplitudes, and an
# amplifier near saturation gives each of them a different gain and a different
# phase shift. An APSK of the same order has eight. Two consequences follow --
# the peak-to-average ratio is lower, so the same average power sits further
# from the knee; and what distortion remains is a per-ring gain and rotation,
# which is a handful of numbers rather than a surface.
#
# Ring k of n holds 4 + 8(k-1) points, so n rings hold 4n^2 -- exactly 4, 16,
# 64 and 256 at n = 1, 2, 4 and 8. The ladder's four constellation sizes come
# out of one construction with no special cases.
#
# Radii equalise the minimum distance: a ring's own neighbours are its nearest,
# so r_k = 1 / (2 sin(pi / n_k)). That is a rule rather than a table, and it is
# worth noting where it lands for the case everyone else has published --
# 16APSK comes out at an outer-to-inner ratio of 2.73, inside DVB-S2's range of
# 2.57 to 3.15 for the same 4+12 layout. The agreement is a check on the rule,
# not a coincidence to lean on: the thresholds below are measured, not assumed.


def _ring_sizes(bits_per_symbol: int) -> list[int]:
    rings = int(round(np.sqrt((1 << bits_per_symbol) / 4)))
    return [4 + 8 * k for k in range(rings)]


@functools.lru_cache(maxsize=None)
def _apsk_points(bits_per_symbol: int) -> np.ndarray:
    """APSK points, indexed by the symbol's integer bit pattern."""
    sizes = _ring_sizes(bits_per_symbol)
    pts = []
    for n_k in sizes:
        r = 1.0 / (2.0 * np.sin(np.pi / n_k))
        # Offset each ring by half a spacing so the innermost four sit on the
        # diagonals, as DVB-S2's do, and no ring lines up with the one inside.
        a = np.pi / n_k + 2 * np.pi * np.arange(n_k) / n_k
        pts.append(r * np.exp(1j * a))
    ordered = np.concatenate(pts)
    ordered /= np.sqrt(np.mean(np.abs(ordered) ** 2))   # unit average energy

    # Gray-code the position in that ordering. Walking a ring in angle changes
    # one bit at a time, which is what matters: a ring's own neighbours are its
    # nearest, so they account for nearly every symbol error. The jump between
    # rings gets no such guarantee, and does not need one -- the rings are
    # further apart than the points along them.
    out = np.empty(len(ordered), dtype=complex)
    for index, point in enumerate(ordered):
        out[index ^ (index >> 1)] = point
    return out


@functools.lru_cache(maxsize=None)
def _bit_table(bits_per_symbol: int) -> np.ndarray:
    """Bit ``j`` of pattern ``p``, shape (2**m, m), as bool."""
    pats = np.arange(1 << bits_per_symbol)
    shifts = np.arange(bits_per_symbol - 1, -1, -1)
    return ((pats[:, None] >> shifts) & 1).astype(bool)


@functools.lru_cache(maxsize=None)
def _bit_masks(k: int) -> tuple[np.ndarray, np.ndarray]:
    """For each of the k bit positions, boolean masks over the L patterns
    selecting those where the bit is 0 and where it is 1."""
    L = 1 << k
    pats = np.arange(L)
    zero = np.empty((k, L), dtype=bool)
    for j in range(k):
        bit = (pats >> (k - 1 - j)) & 1
        zero[j] = bit == 0
    return zero, ~zero


@njit(cache=True)
def _llr_kernel(symbols, pts, zero, out, inv):
    """Max-log LLRs against an arbitrary point set.

    The square-QAM demapper next door searches two PAM axes instead of the
    whole constellation, which is what makes it cheap. APSK has no such
    structure -- that is the point of it -- so this is the honest general form:
    for every bit, the nearest point that carries it as a one against the
    nearest that carries it as a zero.

    Written out rather than broadcast in numpy because the broadcast form
    builds an (N, 256) complex array per frame, which for a WIDE frame is
    thirteen megabytes of temporaries to do eight hundred thousand distance
    calculations.
    """
    n, m = symbols.shape[0], out.shape[1]
    npts = pts.shape[0]
    BIG = 1e30
    for i in range(n):
        yr = symbols[i].real
        yi = symbols[i].imag
        for j in range(m):
            out[i, j] = 0.0
        # One pass over the points, keeping the best zero and one per bit.
        for j in range(m):
            d0 = BIG
            d1 = BIG
            for p in range(npts):
                dr = yr - pts[p].real
                di = yi - pts[p].imag
                d = dr * dr + di * di
                if zero[p, j]:
                    if d < d0:
                        d0 = d
                else:
                    if d < d1:
                        d1 = d
            out[i, j] = (d1 - d0) * inv
    return out


def _apsk_llr(symbols, bits_per_symbol, noise_var, csi):
    pts = np.ascontiguousarray(points(bits_per_symbol, APSK))
    # zero[p, j] is True when point p carries bit j as a zero.
    zero = np.ascontiguousarray(~_bit_table(bits_per_symbol))
    symbols = np.ascontiguousarray(symbols, dtype=np.complex128)
    out = np.zeros((len(symbols), bits_per_symbol), dtype=np.float64)
    inv = 1.0 / (2.0 * max(noise_var, 1e-12))
    llr = _llr_kernel(symbols, pts, zero, out, inv)
    if csi is not None:
        llr = llr * np.abs(csi)[:, None]
    return llr.ravel()


@functools.lru_cache(maxsize=None)
def ring_of(bits_per_symbol: int) -> np.ndarray:
    """Which ring each APSK pattern sits on, indexed by bit pattern."""
    sizes = _ring_sizes(bits_per_symbol)
    by_position = np.concatenate([np.full(n, k) for k, n in enumerate(sizes)])
    out = np.empty(len(by_position), dtype=np.int64)
    for index, ring in enumerate(by_position):
        out[index ^ (index >> 1)] = ring       # same Gray relabelling
    return out


def derings(symbols: np.ndarray, bits_per_symbol: int,
            damping: float = 1.0) -> tuple[np.ndarray, np.ndarray]:
    """Undo an amplifier's per-ring gain and rotation.

    This is the half of APSK that makes it worth having, and without it the
    ring layout is simply a worse constellation -- measured, 0.8 dB of minimum
    distance given away for nothing.

    A compressed amplifier applies a gain and a phase shift that depend only on
    the envelope, so on a constellation whose points sit at a handful of radii
    the whole distortion is a handful of complex numbers. Estimating them is
    decision-directed, which is usually a thing to be wary of -- the decisions
    come from the signal you are trying to fix. It is safe *here* because the
    estimate is so heavily over-determined: two to eight unknowns against
    thousands of symbols, so even a fifth of the decisions being wrong barely
    moves the answer. Contrast the decision-directed equaliser this codebase
    tried and threw away, which had 25 free complex taps and no such margin.

    Returns the corrected symbols and the per-ring corrections applied.
    """
    pts = points(bits_per_symbol, APSK)
    rings = ring_of(bits_per_symbol)
    n_rings = int(rings.max()) + 1
    d2 = np.abs(symbols[:, None] - pts[None, :]) ** 2
    nearest = np.argmin(d2, axis=1)
    ideal = pts[nearest]
    which = rings[nearest]

    fix = np.ones(n_rings, dtype=complex)
    for r in range(n_rings):
        sel = which == r
        # A ring with almost nothing on it says nothing about its own gain;
        # leaving it alone is better than fitting it to a handful of outliers.
        if np.count_nonzero(sel) < 16:
            continue
        rx = symbols[sel]
        denom = float(np.sum(np.abs(rx) ** 2))
        if denom <= 0:
            continue
        k = complex(np.sum(ideal[sel] * np.conj(rx)) / denom)
        fix[r] = 1.0 + damping * (k - 1.0)
    return symbols * fix[which], fix


def _check(bits_per_symbol: int, family: str = QAM) -> None:
    if bits_per_symbol not in SUPPORTED:
        raise ValueError(
            f"bits_per_symbol must be one of {SUPPORTED}, got {bits_per_symbol}"
        )
    if family not in FAMILIES:
        raise ValueError(f"family must be one of {FAMILIES}, got {family!r}")


def modulate(bits: np.ndarray, bits_per_symbol: int,
             family: str = QAM) -> np.ndarray:
    """Pack a bit array into complex symbols of unit average energy."""
    _check(bits_per_symbol, family)
    bits = np.asarray(bits, dtype=np.uint8).ravel()
    if len(bits) % bits_per_symbol:
        raise ValueError(
            f"{len(bits)} bits is not a whole number of {bits_per_symbol}-bit symbols"
        )
    grouped = bits.reshape(-1, bits_per_symbol)
    weights = 1 << np.arange(bits_per_symbol - 1, -1, -1)
    patterns = grouped @ weights.astype(np.uint32)
    return points(bits_per_symbol, family)[patterns]


def slice_hard(symbols: np.ndarray, bits_per_symbol: int,
               family: str = QAM) -> np.ndarray:
    """Nearest constellation point for each symbol.

    Used by the decision-directed carrier loop and the equaliser, which need a
    reference before the FEC has had a chance to clean anything up. Done per
    axis by rounding to the nearest odd integer -- no search.
    """
    _check(bits_per_symbol, family)
    if family == APSK:
        # No separable structure to exploit -- the rings do not line up with
        # the axes, by construction. Nearest of the whole set it is.
        pts = points(bits_per_symbol, family)
        d2 = np.abs(symbols[:, None] - pts[None, :]) ** 2
        return pts[np.argmin(d2, axis=1)]
    k = bits_per_symbol // 2
    L = 1 << k
    s = _scale(bits_per_symbol)
    lim = L - 1

    def quant(x: np.ndarray) -> np.ndarray:
        q = 2.0 * np.round((x / s - 1.0) / 2.0) + 1.0
        return np.clip(q, -lim, lim)

    return (quant(symbols.real) + 1j * quant(symbols.imag)) * s


def demodulate_hard(symbols: np.ndarray, bits_per_symbol: int,
                    family: str = QAM) -> np.ndarray:
    """Hard bit decisions. Mostly for tests -- the real path is soft."""
    _check(bits_per_symbol, family)
    if family == APSK:
        pts = points(bits_per_symbol, family)
        d2 = np.abs(symbols[:, None] - pts[None, :]) ** 2
        patterns = np.argmin(d2, axis=1)
        shifts = np.arange(bits_per_symbol - 1, -1, -1)
        return ((patterns[:, None] >> shifts) & 1).astype(np.uint8).ravel()
    k = bits_per_symbol // 2
    s = _scale(bits_per_symbol)
    lv = pam_levels(k)

    def nearest_pattern(x: np.ndarray) -> np.ndarray:
        d = np.abs(x[:, None] / s - lv[None, :])
        return np.argmin(d, axis=1)

    i_pat = nearest_pattern(symbols.real)
    q_pat = nearest_pattern(symbols.imag)
    patterns = (i_pat << k) | q_pat
    shifts = np.arange(bits_per_symbol - 1, -1, -1)
    return ((patterns[:, None] >> shifts) & 1).astype(np.uint8).ravel()


def demodulate_soft(
    symbols: np.ndarray,
    bits_per_symbol: int,
    noise_var: float,
    csi: np.ndarray | None = None,
    family: str = QAM,
) -> np.ndarray:
    """Max-log LLRs, one float per bit, positive meaning "probably zero".

    ``noise_var`` is the noise variance per complex symbol; each axis carries
    half of it. ``csi`` optionally gives a per-symbol channel amplitude from
    the equaliser -- symbols that arrived through a faded tap get their LLRs
    scaled down so the Viterbi trusts them less. Without it, a deep fade
    produces confident garbage, which is far more damaging to a convolutional
    decoder than honest uncertainty.
    """
    _check(bits_per_symbol, family)
    if family == APSK:
        return _apsk_llr(symbols, bits_per_symbol, noise_var, csi)
    k = bits_per_symbol // 2
    s = _scale(bits_per_symbol)
    lv = pam_levels(k)
    zero_mask, one_mask = _bit_masks(k)

    axis_var = max(noise_var, 1e-12) / 2.0

    def axis_llr(x: np.ndarray) -> np.ndarray:
        # squared distance to every level: (N, L)
        d2 = (x[:, None] / s - lv[None, :]) ** 2
        out = np.empty((len(x), k), dtype=np.float64)
        for j in range(k):
            d0 = np.min(d2[:, zero_mask[j]], axis=1)
            d1 = np.min(d2[:, one_mask[j]], axis=1)
            out[:, j] = (d1 - d0) * (s * s) / (2.0 * axis_var)
        return out

    i_llr = axis_llr(symbols.real)
    q_llr = axis_llr(symbols.imag)

    llr = np.concatenate([i_llr, q_llr], axis=1)
    if csi is not None:
        llr *= np.abs(csi)[:, None]
    return llr.ravel()


def evm_db(received: np.ndarray, bits_per_symbol: int,
           family: str = QAM) -> float:
    """Error vector magnitude against the nearest points, in dB.

    The receiver's SNR estimate, and therefore the input to auto-probe. Blind
    -- it needs no reference symbols, so it works on live payload rather than
    only on pilots.
    """
    ideal = slice_hard(received, bits_per_symbol, family)
    err = received - ideal
    num = float(np.mean(np.abs(err) ** 2))
    den = float(np.mean(np.abs(ideal) ** 2))
    if num <= 0.0:
        return float("inf")
    return 10.0 * np.log10(den / num)
