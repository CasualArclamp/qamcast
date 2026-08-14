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

SUPPORTED = (2, 4, 6, 8)  # bits per symbol: QPSK, 16QAM, 64QAM, 256QAM


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
def points(bits_per_symbol: int) -> np.ndarray:
    """All constellation points, indexed by the symbol's integer bit pattern."""
    k = bits_per_symbol // 2
    lv = pam_levels(k)
    s = _scale(bits_per_symbol)
    idx = np.arange(1 << bits_per_symbol)
    i_pat = idx >> k
    q_pat = idx & ((1 << k) - 1)
    return (lv[i_pat] + 1j * lv[q_pat]) * s


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


def _check(bits_per_symbol: int) -> None:
    if bits_per_symbol not in SUPPORTED:
        raise ValueError(
            f"bits_per_symbol must be one of {SUPPORTED}, got {bits_per_symbol}"
        )


def modulate(bits: np.ndarray, bits_per_symbol: int) -> np.ndarray:
    """Pack a bit array into complex symbols of unit average energy."""
    _check(bits_per_symbol)
    bits = np.asarray(bits, dtype=np.uint8).ravel()
    if len(bits) % bits_per_symbol:
        raise ValueError(
            f"{len(bits)} bits is not a whole number of {bits_per_symbol}-bit symbols"
        )
    grouped = bits.reshape(-1, bits_per_symbol)
    weights = 1 << np.arange(bits_per_symbol - 1, -1, -1)
    patterns = grouped @ weights.astype(np.uint32)
    return points(bits_per_symbol)[patterns]


def slice_hard(symbols: np.ndarray, bits_per_symbol: int) -> np.ndarray:
    """Nearest constellation point for each symbol.

    Used by the decision-directed carrier loop and the equaliser, which need a
    reference before the FEC has had a chance to clean anything up. Done per
    axis by rounding to the nearest odd integer -- no search.
    """
    _check(bits_per_symbol)
    k = bits_per_symbol // 2
    L = 1 << k
    s = _scale(bits_per_symbol)
    lim = L - 1

    def quant(x: np.ndarray) -> np.ndarray:
        q = 2.0 * np.round((x / s - 1.0) / 2.0) + 1.0
        return np.clip(q, -lim, lim)

    return (quant(symbols.real) + 1j * quant(symbols.imag)) * s


def demodulate_hard(symbols: np.ndarray, bits_per_symbol: int) -> np.ndarray:
    """Hard bit decisions. Mostly for tests -- the real path is soft."""
    _check(bits_per_symbol)
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
) -> np.ndarray:
    """Max-log LLRs, one float per bit, positive meaning "probably zero".

    ``noise_var`` is the noise variance per complex symbol; each axis carries
    half of it. ``csi`` optionally gives a per-symbol channel amplitude from
    the equaliser -- symbols that arrived through a faded tap get their LLRs
    scaled down so the Viterbi trusts them less. Without it, a deep fade
    produces confident garbage, which is far more damaging to a convolutional
    decoder than honest uncertainty.
    """
    _check(bits_per_symbol)
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


def evm_db(received: np.ndarray, bits_per_symbol: int) -> float:
    """Error vector magnitude against the nearest points, in dB.

    The receiver's SNR estimate, and therefore the input to auto-probe. Blind
    -- it needs no reference symbols, so it works on live payload rather than
    only on pilots.
    """
    ideal = slice_hard(received, bits_per_symbol)
    err = received - ideal
    num = float(np.mean(np.abs(err) ** 2))
    den = float(np.mean(np.abs(ideal) ** 2))
    if num <= 0.0:
        return float("inf")
    return 10.0 * np.log10(den / num)
