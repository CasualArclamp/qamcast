"""Scope feed: what the constellation and waterfall are drawn from.

Decoupled from the DSP loop on purpose. Both apps block on audio -- the
transmitter on a sound card write, the receiver on a read -- so telemetry
emitted from inside those loops can only go as fast as the block size allows.
On WIDE that is one frame per 256 ms, which is four updates a second and looks
like it has crashed.

So the loop pushes into this buffer and returns immediately, and a separate
thread reads snapshots at whatever rate the UI wants. Two consequences worth
naming:

**The waterfall advances in real time, not in blocks.** A read cursor walks
the ring buffer at the sample rate, so each snapshot FFTs the next window
along rather than re-FFTing the newest one. Without that, a 25 Hz UI fed by
4 Hz frames draws six identical lines then a new one, and the waterfall
staircases instead of scrolling.

**The constellation only changes when a frame does.** It is tagged with a
sequence number and omitted from the payload when unchanged, so the UI keeps
drawing the last one instead of the wire carrying a few thousand numbers 25
times a second to say nothing new.
"""

from __future__ import annotations

import base64
import threading
import time

import numpy as np

FFT_SIZE = 1024
DEFAULT_BINS = 192
FLOOR_DB = -90.0

# Weight given to each new periodogram. Low enough to average away the
# snapshot variance, high enough that the display still follows a real change.
SPEC_SMOOTH = 0.25

# How many pieces a frame's symbols are handed out in. Four matches a 20 Hz
# publisher against frames of roughly a quarter second, so the cloud finishes
# filling just as the next frame arrives.
SYMBOL_SLICES = 4

# Expected hits per constellation point in a single slice. The freshest slice
# is the bright one, so this decides how complete the cloud looks. Symbols land
# on random points, so covering a fraction f of N points takes N*ln(1/(1-f))
# draws -- three per point is 95%, and going higher buys very little.
DRAWS_PER_POINT = 3
SYMBOLS_MIN = 900
SYMBOLS_MAX = 3200


def symbol_budget(bits_per_symbol: int) -> int:
    """How many symbols to show for a constellation of this size.

    A constant does not work across the ladder. 900 is generous for QPSK's
    four points and thin for 256QAM's 256: a quarter of 900 is one slice, and
    one slice was lighting 151 of the 256 -- so a third of the grid was always
    missing or fading, and the constellation looked damaged when the signal
    was regular to 0.18%. Only 256QAM asks for more than the floor.
    """
    want = DRAWS_PER_POINT * (1 << int(bits_per_symbol)) * SYMBOL_SLICES
    return int(min(SYMBOLS_MAX, max(SYMBOLS_MIN, want)))


def bin_spectrum(mag: np.ndarray, bins: int) -> np.ndarray:
    """Average ``mag`` into ``bins`` groups covering all of it.

    The obvious `mag[:len(mag)//bins*bins].reshape(bins, -1)` silently drops
    the remainder, and the remainder is not small: 512 FFT bins into 192
    display bins keeps 384 and throws the top quarter of the spectrum away.
    The axis then runs to 18 kHz while everything drawn against it -- the
    occupied-band markers, the full canvas width -- still assumes 24, so a
    signal reaching 20.1 kHz appears to run off the end and never come back
    down.

    Group edges from linspace instead, so every bin is accounted for whatever
    the ratio happens to be.
    """
    if len(mag) <= bins:
        return mag
    edges = np.linspace(0, len(mag), bins + 1).astype(int)
    sums = np.add.reduceat(mag, edges[:-1])
    return sums / np.maximum(np.diff(edges), 1)


def pack_int8(values: np.ndarray, scale: float) -> str:
    """Quantise to int8 and base64 it.

    A constellation of 900 points is 1800 numbers. As JSON floats that is
    roughly 11 kB per update; as base64 int8 it is 2.4 kB. At 25 updates a
    second the difference is the whole reason this can run smoothly over an
    HTTP event stream.
    """
    q = np.clip(np.round(np.asarray(values, dtype=np.float64) * scale), -127, 127)
    return base64.b64encode(q.astype(np.int8).tobytes()).decode("ascii")


class ScopeFeed:
    """Thread-safe hand-off between the DSP loop and the UI publisher."""

    # Chosen from the densest constellation: 256QAM's outer level sits at
    # 1.150, so this puts it at code 100 of 127 -- using the range instead of
    # the 74 that a scale of 64 reached, which halves the position error the
    # quantiser adds, while still leaving 27% headroom before a noisy outer
    # symbol clips. The value travels with the data, so both ends stay in step.
    CONST_SCALE = 87.0

    def __init__(self, sample_rate: int, seconds: float = 1.5,
                 bins: int = DEFAULT_BINS):
        self.sample_rate = sample_rate
        self.bins = bins
        self._buf = np.zeros(max(FFT_SIZE * 4, int(sample_rate * seconds)))
        self._written = 0          # total samples ever pushed
        self._cursor = 0.0         # scope read position, in samples
        self._last_tick = time.time()
        self._symbols: np.ndarray | None = None
        self._sym_pos = 0
        self._seq = 0
        self._spec_avg: np.ndarray | None = None
        self._lock = threading.Lock()
        self._window = np.hanning(FFT_SIZE)

    # -- producer side ---------------------------------------------------

    def push_audio(self, x: np.ndarray) -> None:
        x = np.asarray(x, dtype=np.float64).ravel()
        if not len(x):
            return
        n = len(self._buf)
        if len(x) >= n:
            x = x[-n:]
        with self._lock:
            start = self._written % n
            end = start + len(x)
            if end <= n:
                self._buf[start:end] = x
            else:
                split = n - start
                self._buf[start:] = x[:split]
                self._buf[:end - n] = x[split:]
            self._written += len(x)

    def push_symbols(self, symbols: np.ndarray) -> None:
        with self._lock:
            self._symbols = np.asarray(symbols)
            self._sym_pos = 0
            self._seq += 1

    # -- consumer side ---------------------------------------------------

    def _read_window(self) -> np.ndarray | None:
        """Next FFT window, advancing the cursor by real elapsed time."""
        now = time.time()
        elapsed = now - self._last_tick
        self._last_tick = now
        n = len(self._buf)

        oldest = max(0, self._written - n)
        self._cursor = max(self._cursor + elapsed * self.sample_rate, oldest)
        # Never read past what has been written, and keep a whole window
        # behind the write pointer.
        newest = self._written - FFT_SIZE
        if newest < 0:
            return None
        if self._cursor > newest:
            self._cursor = float(newest)

        start = int(self._cursor)
        if start < oldest:
            start = oldest
        idx = (np.arange(FFT_SIZE) + start) % n
        return self._buf[idx]

    def snapshot(self, last_seq: int = -1) -> dict:
        """Spectrum every call; the next slice of the constellation.

        A frame's symbols are handed over all at once, but they were on air
        spread across the frame's whole duration -- 256 ms on WIDE. Sending
        them in slices therefore shows them at roughly the rate they were
        actually transmitted, and turns a constellation that lurches four
        times a second into one that fills in continuously. With the fade in
        the canvas it reads like a scope with persistence, which is what it
        physically is.
        """
        with self._lock:
            seg = self._read_window()
            symbols = self._symbols
            seq = self._seq
            pos = self._sym_pos
            if symbols is not None and pos < len(symbols):
                step = max(1, len(symbols) // SYMBOL_SLICES)
                chunk = symbols[pos:pos + step]
                self._sym_pos = pos + step
            else:
                chunk = None

        out: dict = {"seq": seq}
        if seg is not None and np.any(seg):
            mag = np.abs(np.fft.rfft(seg * self._window)) ** 2
            usable = bin_spectrum(mag[: FFT_SIZE // 2], self.bins)
            # A single periodogram of a random data signal has an
            # exponentially distributed power per bin -- about 5.6 dB of
            # standard deviation -- so one snapshot is a jagged mess you
            # cannot read a band edge off. Average in the power domain across
            # snapshots; at 20 Hz this settles in a couple of hundred ms and
            # still tracks a change fast enough to watch.
            if self._spec_avg is None or len(self._spec_avg) != len(usable):
                self._spec_avg = usable
            else:
                self._spec_avg = (1.0 - SPEC_SMOOTH) * self._spec_avg + SPEC_SMOOTH * usable
            avg = self._spec_avg
            peak = avg.max() or 1.0
            db = 10.0 * np.log10(np.maximum(avg / peak, 1e-12))
            out["spec"] = pack_int8(np.maximum(db, FLOOR_DB), 1.0)

        if chunk is not None and len(chunk):
            flat = np.empty(len(chunk) * 2)
            flat[0::2] = chunk.real
            flat[1::2] = chunk.imag
            out["const"] = pack_int8(flat, self.CONST_SCALE)
            out["const_scale"] = self.CONST_SCALE
            out["const_first"] = pos == 0
        return out


class Publisher(threading.Thread):
    """Pushes telemetry to the UI at a steady rate, whatever the DSP is doing."""

    def __init__(self, telemetry, build, hz: float = 20.0):
        super().__init__(daemon=True)
        self.telemetry = telemetry
        self.build = build
        self.interval = 1.0 / hz
        self._stop = threading.Event()

    def run(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                state = self.build()
            except Exception:
                continue
            if state:
                self.telemetry.publish(state)

    def stop(self) -> None:
        self._stop.set()
