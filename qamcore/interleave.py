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
        self._fifos = [
            np.zeros(self._delay_of(j), dtype=np.uint8) for j in range(branches)
        ]
        self._phase = 0

    def _delay_of(self, j: int) -> int:
        raise NotImplementedError

    @property
    def total_delay(self) -> int:
        return delay_bytes(self.branches, self.increment)

    def reset(self) -> None:
        for j in range(self.branches):
            self._fifos[j][:] = 0
        self._phase = 0

    def process(self, data: np.ndarray) -> np.ndarray:
        """Push bytes through. Output length always equals input length --
        the delay lives in the FIFOs, not in a changing rate."""
        data = np.asarray(data, dtype=np.uint8).ravel()
        if len(data) == 0:
            return data.copy()
        out = np.empty_like(data)
        I = self.branches
        p = self._phase
        for j in range(I):
            first = (j - p) % I
            if first >= len(data):
                continue
            sub = data[first::I]
            fifo = self._fifos[j]
            if fifo.size == 0:
                out[first::I] = sub
                continue
            if len(sub) >= fifo.size:
                # FIFO fully turns over: emit it, then the head of sub
                out[first::I] = np.concatenate([fifo, sub[:len(sub) - fifo.size]])
                self._fifos[j] = sub[len(sub) - fifo.size:].copy()
            else:
                out[first::I] = fifo[:len(sub)]
                self._fifos[j] = np.concatenate([fifo[len(sub):], sub])
        self._phase = (p + len(data)) % I
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
