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


# Below this the interleaver stops being one: spreading a burst across a
# handful of branches puts several errors in the same codeword. Well under the
# 255 that saturates the benefit, but far enough above 2 that trimming the
# branch count to hit a delay target cannot quietly destroy the spreading.
MIN_USEFUL_BRANCHES = 64

# How far the delivered depth may sit from the requested one before accuracy
# starts to matter more than spreading. Generous on purpose: the depth is a
# handful of seconds chosen by feel, and five per cent of it is not something
# an operator can perceive, whereas losing two thirds of the branches is a
# real loss of burst protection.
DELAY_TOLERANCE = 0.05


def geometry(delay_seconds: float, byte_rate: float) -> tuple[int, int]:
    """Pick (branches, increment) for a target delay at a given byte rate.

    Both are integers and the delay is their product with (branches - 1), so
    not every target is reachable. What is reachable changes character once
    branches saturates: at 255 the only knob left is the increment, and one
    step of it is 255 x 254 bytes -- seven seconds at a typical rate. Choosing
    the branches first and rounding the increment afterwards therefore lands
    wherever that coarse grid happens to fall. Measured on FM44 at 64QAM 5/6,
    a 12 second request came back as 14.8, a 23% overshoot, and the panel
    honestly reported the 14.8.

    So search instead: for each increment, the branch count that hits the
    target is about sqrt(target / increment), and trimming a few branches off
    255 buys a far closer delay than stepping the increment does. Ties go to
    more branches, because spreading is what the interleaver is for -- but
    only down to MIN_USEFUL_BRANCHES, below which a closer delay would be
    bought with the thing being paid for.
    """
    target = max(1.0, delay_seconds * byte_rate)
    # The floor is relaxed when the target is genuinely small: a link wanting
    # only a few thousand bytes of delay cannot have 64 branches, and should
    # get the spreading it can afford rather than an error.
    floor = min(MIN_USEFUL_BRANCHES, max(2, int(np.sqrt(target))))
    close: list[tuple[int, int]] = []
    fallback: tuple[float, tuple[int, int]] | None = None
    for increment in range(1, 4097):
        # b(b-1) * increment = target, solved for b and looked at either side,
        # since the exact root is rarely an integer.
        root = 0.5 + np.sqrt(max(0.0, target / increment) + 0.25)
        for branches in {int(np.floor(root)), int(np.ceil(root))}:
            branches = max(floor, min(MAX_BRANCHES, branches))
            err = abs(delay_bytes(branches, increment) - target) / target
            if err <= DELAY_TOLERANCE:
                close.append((branches, increment))
            if fallback is None or err < fallback[0]:
                fallback = (err, (branches, increment))
        if root <= floor:
            break                      # larger increments only shrink branches
    # Spreading first, delay second. Every candidate here already hits the
    # requested depth to within the tolerance, so the one to take is simply
    # the one that spreads a burst widest -- which is what the interleaver is
    # for. Choosing on delay accuracy instead was measured to drop a six
    # second link from 229 branches to 87 for a 0.4% gain in a number nobody
    # can hear.
    if close:
        return max(close, key=lambda bi: bi[0])
    return fallback[1]


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
