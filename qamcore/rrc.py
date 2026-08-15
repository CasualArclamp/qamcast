"""Root-raised-cosine pulse shaping.

Split-Nyquist: the transmitter shapes with an RRC and the receiver filters with
the same RRC, so the cascade is a full raised cosine and intersymbol
interference is zero at the sampling instants. Neither half is Nyquist on its
own, which is why the receiver cannot skip its matched filter and why the two
ends must agree on ``alpha`` -- a roll-off mismatch shows up as a constellation
that is nearly right and never quite locks.
"""

from __future__ import annotations

import numpy as np
from numba import njit

# Filter length in symbols each side of centre. Set by the top of the ladder,
# not the bottom: 256QAM at alpha=0.2 has half-decision-distance 0.077 in
# unit-energy terms, and a truncated RRC leaks residual ISI straight into that
# budget. Measured cascade ISI at sps=3, alpha=0.2 is -46 dB at span 8 and
# -60 dB at span 16, which is the difference between spending a dB of margin
# on filter truncation and not noticing it. 97 taps is nothing to convolve.
DEFAULT_SPAN = 16


def design(sps: int, alpha: float, span: int = DEFAULT_SPAN) -> np.ndarray:
    """RRC impulse response, unit energy, length ``2*span*sps + 1``.

    Computed from the closed form with the two removable singularities handled
    explicitly -- at t=0 and at |t| = Ts/(4*alpha) the general expression is
    0/0, and letting numpy produce a NaN there puts a hole in the middle of the
    filter that is remarkably hard to spot downstream.
    """
    if not 0.0 < alpha <= 1.0:
        raise ValueError(f"alpha must be in (0, 1], got {alpha}")

    n = np.arange(-span * sps, span * sps + 1, dtype=np.float64)
    t = n / sps  # time in symbol periods
    h = np.empty_like(t)

    # t == 0
    at_zero = np.isclose(t, 0.0)
    h[at_zero] = 1.0 + alpha * (4.0 / np.pi - 1.0)

    # |t| == 1/(4*alpha)
    at_sing = np.isclose(np.abs(t), 1.0 / (4.0 * alpha))
    if at_sing.any():
        h[at_sing] = (alpha / np.sqrt(2.0)) * (
            (1.0 + 2.0 / np.pi) * np.sin(np.pi / (4.0 * alpha))
            + (1.0 - 2.0 / np.pi) * np.cos(np.pi / (4.0 * alpha))
        )

    ordinary = ~(at_zero | at_sing)
    to = t[ordinary]
    num = np.sin(np.pi * to * (1.0 - alpha)) + 4.0 * alpha * to * np.cos(
        np.pi * to * (1.0 + alpha)
    )
    den = np.pi * to * (1.0 - (4.0 * alpha * to) ** 2)
    h[ordinary] = num / den

    return h / np.linalg.norm(h)


def shape(symbols: np.ndarray, sps: int, alpha: float, span: int = DEFAULT_SPAN) -> np.ndarray:
    """Upsample complex symbols by ``sps`` and RRC filter them.

    Returns ``len(symbols) * sps`` samples: the filter's group delay is
    trimmed off both ends so sample ``k*sps`` is symbol ``k``. That keeps frame
    boundaries at predictable sample offsets, which matters because the
    transmitter has to splice frames back to back without a discontinuity.
    """
    h = design(sps, alpha, span)
    up = np.zeros(len(symbols) * sps, dtype=np.complex128)
    up[::sps] = symbols
    full = np.convolve(up, h, mode="full")
    delay = (len(h) - 1) // 2
    return full[delay:delay + len(up)]


def matched(samples: np.ndarray, sps: int, alpha: float, span: int = DEFAULT_SPAN) -> np.ndarray:
    """Receive-side matched filter. Same filter, same delay compensation."""
    h = design(sps, alpha, span)
    full = np.convolve(samples, h, mode="full")
    delay = (len(h) - 1) // 2
    return full[delay:delay + len(samples)]


class StreamShaper:
    """Pulse shaper that keeps filter state across calls.

    The transmitter emits frames continuously, and shaping each one
    independently would leave a seam at every frame boundary where the filter
    tails should have overlapped -- audible as a tick and visible as a spectral
    splatter that widens the occupied band. This carries the tail forward.
    """

    def __init__(self, sps: int, alpha: float, span: int = DEFAULT_SPAN):
        self.sps = sps
        self.h = design(sps, alpha, span)
        self.delay = (len(self.h) - 1) // 2
        self._tail = np.zeros(len(self.h) - 1, dtype=np.complex128)
        self._primed = False

    def process(self, symbols: np.ndarray) -> np.ndarray:
        """Overlap-add, group delay removed so symbol k sits at sample k*sps.

        The delay is dropped once, on the first call, which makes that first
        chunk ``delay`` samples shorter than the rest. Keeping the same
        alignment as the one-shot ``shape()`` matters less to the receiver --
        which acquires by correlating for the preamble and never assumes an
        absolute offset -- than it does to anyone comparing the two while
        chasing a bug.
        """
        up = np.zeros(len(symbols) * self.sps, dtype=np.complex128)
        up[::self.sps] = symbols
        full = np.convolve(up, self.h, mode="full")
        full[:len(self._tail)] += self._tail
        self._tail = full[len(up):].copy()
        out = full[:len(up)]
        if not self._primed:
            self._primed = True
            out = out[self.delay:]
        return out

    def flush(self) -> np.ndarray:
        """Remaining filter tail, for a clean end of transmission."""
        out = self._tail.copy()
        self._tail = np.zeros(len(self.h) - 1, dtype=np.complex128)
        self._primed = False
        return out


@njit(cache=True)
def _fir(x, h, out):
    """Complex signal, real taps, overlap-add output. Accumulates into ``out``.

    Written out rather than left to np.convolve because the taps are real and
    the signal is not: numpy promotes the shorter operand and does four real
    multiplies per tap where two will do. This runs on every sample the
    receiver ever sees, at the card rate, so the factor is worth having.
    """
    k = len(h)
    for i in range(len(x)):
        xr = x[i].real
        xi = x[i].imag
        for j in range(k):
            hj = h[j]
            out[i + j] += complex(xr * hj, xi * hj)
    return out


class StreamMatched:
    """Matched filter with state, for the receiver's continuous input."""

    def __init__(self, sps: int, alpha: float, span: int = DEFAULT_SPAN):
        self.h = np.ascontiguousarray(design(sps, alpha, span))
        self.delay = (len(self.h) - 1) // 2
        self._tail = np.zeros(len(self.h) - 1, dtype=np.complex128)
        self._primed = False

    def process(self, samples: np.ndarray) -> np.ndarray:
        n = len(samples)
        full = np.zeros(n + len(self.h) - 1, dtype=np.complex128)
        full[:len(self._tail)] = self._tail
        _fir(np.ascontiguousarray(samples), self.h, full)
        self._tail = full[n:].copy()
        out = full[:n]
        if not self._primed:
            self._primed = True
            out = out[self.delay:]
        return out
