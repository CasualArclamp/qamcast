"""Fractional-sample interpolation, shared by the receiver and the channel.

One implementation, used in both places, for the same reason the wire format
lives in one module: the two were written separately, the receiver got a
windowed-sinc bank and the channel simulator kept a linear interpolator, and
the resulting distortion looked exactly like a receiver bug. It cost an
afternoon. Linear interpolation is about -20 dB accurate at the 0.4-of-Nyquist
occupancy these profiles use -- fine for a control signal, hopeless for
anything carrying 256QAM through it.

The bank is 16 taps of Kaiser-windowed sinc across 512 sub-sample phases,
which puts the interpolation error below -70 dB in band and comfortably out of
the way of the ~35 dB EVM that the top of the ladder needs.
"""

from __future__ import annotations

import functools

import numpy as np
from numba import njit

TAPS = 16
PHASES = 512
KAISER_BETA = 8.0

HALF = TAPS // 2
LEFT = HALF - 1     # samples needed before the base index
RIGHT = HALF        # samples needed at and after it


@functools.lru_cache(maxsize=None)
def bank() -> np.ndarray:
    """Polyphase filter bank, shape (PHASES, TAPS)."""
    j = np.arange(TAPS) - LEFT
    phases = np.arange(PHASES) / PHASES
    t = j[None, :] - phases[:, None]
    b = np.sinc(t) * np.kaiser(TAPS, KAISER_BETA)[None, :]
    # Unit DC gain per phase, so interpolating cannot change signal level.
    return b / b.sum(axis=1, keepdims=True)


@njit(cache=True)
def _apply(x, base, phase, taps, out):
    """Sixteen-tap dot product per output, straight out of the buffer."""
    for i in range(len(base)):
        s = base[i] - LEFT
        p = phase[i]
        acc = out[i]        # zero, and already the right type for x
        for t in range(TAPS):
            acc += taps[p, t] * x[s + t]
        out[i] = acc
    return out


def sample_at(x: np.ndarray, idx: np.ndarray) -> np.ndarray:
    """Interpolate ``x`` at fractional positions ``idx``.

    Caller guarantees ``idx`` stays within ``[LEFT, len(x) - RIGHT)``; this
    raises rather than clipping, because silently sampling off the end of a
    buffer produces a plausible-looking constellation with a hole in it.

    The arithmetic is one 16-tap dot product per output and nothing else. The
    obvious vectorisation -- gather the taps for every phase, gather a 16-wide
    window for every position, then einsum the two -- builds three megabytes of
    temporaries per WIDE frame to perform 131072 multiply-accumulates, and
    spends its time moving them rather than doing them.
    """
    idx = np.asarray(idx, dtype=np.float64)
    base = np.floor(idx).astype(np.int64)
    frac = idx - base
    if len(base) and (base[0] - LEFT < 0 or base[-1] + RIGHT >= len(x)):
        raise IndexError("interpolation window outside buffer")
    phase = np.minimum((frac * PHASES).astype(np.int64), PHASES - 1)
    out = np.zeros(len(base), dtype=np.result_type(x.dtype, np.float64))
    if not len(base):
        return out
    return _apply(np.ascontiguousarray(x), base, phase, bank(), out)
