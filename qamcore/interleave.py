"""Forney convolutional byte interleaver -- the diversity delay.

This is the layer that turns "a car drove past and the signal dropped for a
tenth of a second" into "every codeword lost one or two bytes, all of which RS
put back". It is also the reason audio does not start the instant you press
play, and the reason it takes a few seconds to recover after a loss of lock.
Both ends of that trade were the user's choice: 5-8 seconds, HD Radio style.

Geometry. ``branches`` FIFOs, branch *j* delaying by ``j * increment`` branch
cycles. Input bytes are dealt to branches round-robin, so a byte in branch *j*
waits ``j * increment * branches`` byte-times. The deinterleaver mirrors it
with delay ``(branches-1-j) * increment``, so every byte experiences the same
total and the stream comes out in order.

    end-to-end delay = branches * (branches-1) * increment   byte-times
    memory each side = that / 2                              bytes

Why ``branches`` wants to be large: consecutive bytes on the channel come from
consecutive branches, which hold bytes from *different* codewords. With
``branches >= 255`` a burst shorter than 255 bytes puts at most one error in
any single codeword, which is the best a byte interleaver can do against
RS(255,k). Below that it degrades gracefully -- ceil(L / branches) errors per
codeword for a burst of L.
"""

from __future__ import annotations

import numpy as np

from . import rs

MAX_BRANCHES = rs.N  # 255; more spreading than this buys nothing against RS(255,k)


def geometry(delay_seconds: float, byte_rate: float) -> tuple[int, int]:
    """Pick (branches, increment) for a target delay at a given byte rate.

    Branches are maximised first, because spreading is what the interleaver is
    for; the increment then trims the delay to target. Capped at 255 since a
    burst already lands one byte per codeword there, and pushed no lower than
    the point where the delay would overshoot.
    """
    target = max(1.0, delay_seconds * byte_rate)
    branches = int(np.floor(np.sqrt(target)))
    branches = max(2, min(MAX_BRANCHES, branches))
    increment = max(1, int(round(target / (branches * (branches - 1)))))
    return branches, increment


def delay_bytes(branches: int, increment: int) -> int:
    return branches * (branches - 1) * increment


class _Branched:
    """Shared machinery. Subclasses only differ in the per-branch delay."""

    def __init__(self, branches: int, increment: int):
        if branches < 2:
            raise ValueError("need at least 2 branches")
        self.branches = branches
        self.increment = increment
        # Delay per branch, expressed in *stream* positions rather than branch
        # cycles: a byte dealt to branch j comes back out after delay_of(j)
        # cycles, and one cycle is `branches` bytes of stream.
        self._lag = np.array([self._delay_of(j) * branches
                              for j in range(branches)], dtype=np.int64)
        self._span = int(self._lag.max())
        self._hist = np.zeros(self._span, dtype=np.uint8)
        self._pos = 0
        self._cache: tuple[int, int, np.ndarray] | None = None

    def _delay_of(self, j: int) -> int:
        raise NotImplementedError

    @property
    def total_delay(self) -> int:
        return delay_bytes(self.branches, self.increment)

    # Where in the round-robin the next input byte lands. Carried in the frame
    # header so the receiver can check its deinterleaver against the
    # transmitter's on every frame; kept as a property because the stream
    # position it derives from is now what the state actually is.
    @property
    def _phase(self) -> int:
        return self._pos % self.branches

    @_phase.setter
    def _phase(self, value: int) -> None:
        self._pos += (int(value) - self._pos) % self.branches

    def reset(self) -> None:
        self._hist[:] = 0
        self._pos = 0

    def process(self, data: np.ndarray) -> np.ndarray:
        """Push bytes through. Output length always equals input length --
        the delay lives in the FIFOs, not in a changing rate.

        There are no FIFOs any more, which is the point. Dealing bytes to up to
        255 separate branch queues and splicing each one is a Python loop over
        the branches and several small numpy calls inside it, and it was the
        third most expensive thing in the receiver. But a Forney interleaver is
        not really a bank of queues: byte n leaves at position
        ``n + branches * delay_of(n mod branches)``, a fixed lag that depends on
        nothing but where n falls in the round robin. Keep the recent input and
        the whole stage is one gather.
        """
        data = np.asarray(data, dtype=np.uint8).ravel()
        n = len(data)
        if n == 0:
            return data.copy()
        buf = np.concatenate([self._hist, data]) if self._span else data
        # The gather depends only on the block length and the starting phase,
        # and a receiver feeds the same frame size over and over.
        phase = self._pos % self.branches
        if self._cache is None or self._cache[0] != n or self._cache[1] != phase:
            idx = np.arange(n)
            lag = self._lag[(idx + phase) % self.branches]
            self._cache = (n, phase, idx - lag + self._span)
        out = buf[self._cache[2]]
        if self._span:
            self._hist = buf[-self._span:].copy()
        self._pos += n
        return out


class Interleaver(_Branched):
    """Transmit side: branch j delays by j * increment."""

    def _delay_of(self, j: int) -> int:
        return j * self.increment


class Deinterleaver(_Branched):
    """Receive side: branch j delays by (branches-1-j) * increment."""

    def _delay_of(self, j: int) -> int:
        return (self.branches - 1 - j) * self.increment


def make_pair(delay_seconds: float, byte_rate: float) -> tuple[Interleaver, Deinterleaver]:
    branches, increment = geometry(delay_seconds, byte_rate)
    return Interleaver(branches, increment), Deinterleaver(branches, increment)
