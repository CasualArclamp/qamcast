"""Passband audio back to frames.

The acquisition strategy here is **frame-synchronous and data-aided** rather
than a set of feedback loops, and that is a deliberate choice worth
explaining, because the loop-based design is the more conventional one.

Every frame opens with the same 64-symbol Zadoff-Chu preamble. Correlating
against it gives, in one shot: frame position to sub-sample resolution, the
carrier frequency error from the phase slope across the preamble, and enough
known symbols to solve for equaliser taps directly. Timing between two
consecutive preambles is a straight line -- a sampling clock error is a
constant, not a random walk -- so symbol instants come from interpolating
between frame syncs instead of from a timing loop.

What that buys: no loop bandwidth to tune, no acquisition transient, no
hang-up on a false lock, and identical behaviour whether the receiver started
with the broadcast or joined it forty minutes in. What it costs: two frames of
buffering, and a hard dependence on the preamble surviving. On the channels
this modem targets -- where the alternative is a decision-directed loop trying
to acquire 256QAM through an acoustic echo -- that trade is strongly the right
way round.

Pipeline, in order:

    downconvert -> matched filter -> preamble correlation -> timing
    interpolation -> frequency/phase removal -> equalisation -> pilot
    tracking -> header decode -> payload decode
"""

from __future__ import annotations

import functools
from dataclasses import dataclass, field

import numpy as np

from . import constellation, framing, interp, rrc
from .profiles import HEADER_SYMBOLS, PREAMBLE_SYMBOLS, Modcod, Profile

# Equaliser length in symbols. Enough to span the delay spread the ACOUSTIC
# preset throws at it (6 ms at 8 kBd is 48 symbols) would be extravagant for
# WIDE; 25 taps covers the echoes that actually matter and keeps the
# least-squares solve well conditioned against 64 preamble symbols.
EQ_TAPS = 25
# 25 taps everywhere, and the reason is a hard constraint rather than a
# preference: the equaliser is trained on the 64-symbol preamble, and a
# least-squares solve for n taps gets only (64 - n + 1) equations from it.
# At 25 taps that is 40 equations for 25 unknowns -- comfortable. At 41 it is
# 24 for 41, which is underdetermined, and the solve returns noise that looks
# like an equaliser. Measured: clean-channel EVM is 56 dB at 25 taps and
# collapses to 6 dB at 41.
#
# 25 taps is +-12 symbols of correctable delay spread, which is:
#
#     WIDE      0.38 ms    a cable has no echo, this is ample
#     RADIO     1.25 ms    covers normal transmitter/receiver delay spread
#     ACOUSTIC  1.50 ms    a small room only; see the note in the README
#
# Beyond that the equaliser cannot reach the echo at all, no matter how many
# taps are requested. Fixing it properly means a longer preamble (wire format
# change) or multicarrier modulation -- not a bigger number here.
EQ_TAPS_BY_PROFILE = {"WIDE": 25, "WIDE48": 25, "WIDE44": 25,
                      "RADIO": 25, "RADIO44": 25,
                      "ACOUSTIC": 25, "ACOUSTIC44": 25}

# Correctable delay spread, in symbols each side of the main tap.
MAX_DELAY_SPREAD_SYMBOLS = EQ_TAPS // 2

# Exponential forgetting on the accumulated equaliser normal equations. 0.9
# gives an effective memory of ~10 frames, which is what makes taps longer
# than the preamble solvable at all -- see _solve_equaliser.
EQ_FORGET = 0.9
EQ_LEAK = 1e-4          # tap leakage, stops the equaliser wandering on noise
EQ_MU_PILOT = 0.05      # LMS step on known pilots
EQ_MU_DATA = 0.002      # LMS step on decisions -- an order down, they can be wrong

# Normalised correlation needed to call it a preamble. A true match measures
# 0.99 clean and still 0.95 through 20 ppm of clock error and a 15 Hz carrier
# offset; the correlation floor against modulated payload sits near 0.10.
#
# 0.5 rather than something closer to the floor because Zadoff-Chu sidelobes
# against random 256QAM payload reach past 0.25, and acquisition takes the
# *first* peak over threshold. A sidelobe that wins that race produces a
# confident lock at the wrong offset, which then reports an absurd carrier
# error -- 1.6 kHz was the symptom -- rather than simply failing to lock.
SYNC_THRESHOLD = 0.5

# How clearly the winning MODCOD codeword must beat the runner-up, as a
# fraction of its own correlation. The codewords are exactly orthogonal, so a
# clean frame scores near 1.0; anything under this is noise picking a winner.
MODCOD_MARGIN = 0.25

@dataclass
class FrameResult:
    """One demodulated frame, plus everything the UI wants to display."""

    header: framing.Header | None
    symbols: np.ndarray                 # equalised payload symbols, for the scope
    payload: np.ndarray | None = None   # frame bytes, if the header decoded
    modcod: Modcod | None = None
    evm_db: float = 0.0
    snr_db: float = 0.0
    freq_offset_hz: float = 0.0
    timing_ppm: float = 0.0
    corr_peak: float = 0.0
    modcod_margin: float = 0.0
    # Per-ring correction applied on an APSK link, for the scope to show what
    # the amplifier is doing. None on a QAM link, and on a clean APSK one.
    ring_gains: np.ndarray | None = None

    @property
    def locked(self) -> bool:
        return self.header is not None


@dataclass
class DemodStats:
    frames_seen: int = 0
    headers_ok: int = 0
    headers_failed: int = 0
    resyncs: int = 0        # times the frame sync had to be re-acquired

    @property
    def header_error_rate(self) -> float:
        total = self.headers_ok + self.headers_failed
        return self.headers_failed / total if total else 0.0


class Demodulator:
    """Streaming receiver. Feed it passband samples, take frames out."""

    def __init__(self, profile: Profile, eq_taps: int | None = None):
        self.profile = profile
        self.sps = profile.sps
        # Sized from the delay spread the profile is built for, not a constant.
        # A cable needs a handful of taps; a room needs enough to reach the
        # last echo that still carries energy.
        if eq_taps is None:
            eq_taps = EQ_TAPS_BY_PROFILE.get(profile.name, EQ_TAPS)
        self.eq_taps = eq_taps
        self.stats = DemodStats()

        self._mf = rrc.StreamMatched(profile.sps, profile.rolloff)
        self._phase = 0.0
        self._dphase = 2.0 * np.pi * profile.carrier / profile.sample_rate

        self._buf = np.zeros(0, dtype=np.complex128)   # baseband, matched filtered
        self._buf_start = 0                            # absolute index of _buf[0]
        self._consumed = 0                             # absolute samples discarded

        self._prev_sync: float | None = None           # absolute sample of last preamble
        self._eq = np.zeros(eq_taps, dtype=np.complex128)
        self._eq[eq_taps // 2] = 1.0
        self._eq_valid = False
        self._agc = 1.0
        self._rr = None
        self._rt = None

        # Preamble upsampled and matched-filtered exactly as the transmitter
        # would have sent it, so the correlation is against what is really on
        # the wire rather than against bare symbols.
        pre = framing.preamble()
        ref = rrc.shape(pre, profile.sps, profile.rolloff)
        self._ref = np.conj(ref[::-1]) / np.linalg.norm(ref)
        self._ref_len = len(ref)

    # -- front end -------------------------------------------------------

    def _downconvert(self, x: np.ndarray) -> np.ndarray:
        n = len(x)
        ph = self._phase + self._dphase * np.arange(n)
        self._phase = float((self._phase + self._dphase * n) % (2.0 * np.pi))
        # cos and sin rather than exp of an imaginary argument, which is the
        # same two transcendentals plus a complex exponential's worth of
        # machinery around them for a real part that is always exactly one.
        return x * (np.cos(ph) - 1j * np.sin(ph))

    def feed(self, samples: np.ndarray) -> None:
        """Append passband samples. Cheap; do the work in :meth:`frames`."""
        x = np.asarray(samples, dtype=np.float64).ravel()
        if not len(x):
            return
        bb = self._mf.process(self._downconvert(x))
        self._buf = np.concatenate([self._buf, bb])

    # -- acquisition -----------------------------------------------------

    def _correlate(self, region: np.ndarray) -> np.ndarray:
        """Normalised correlation against the shaped preamble, in [0, 1].

        Normalising by the sliding window energy matters more than it looks.
        An absolute threshold against, say, the median of the correlation
        works during the initial wide search, where most of the window really
        is elsewhere -- but the per-frame search spans only about a hundred
        samples centred on the expected peak, so its median *is* the peak's
        own skirt and every genuine sync gets rejected. Dividing by the local
        energy gives a figure of merit that means the same thing regardless of
        window width, signal level or AGC state.
        """
        n = self._ref_len
        if len(region) < n:
            return np.zeros(0)
        raw = np.convolve(region, self._ref, mode="valid")
        # Sliding energy from prefix sums, not a second convolution. The kernel
        # there is all ones, so the convolution was computing a running total
        # the long way round -- len(region) * n multiply-accumulates for a
        # quantity that two subtractions per output give exactly. On a WIDE
        # acquisition scan that kernel is 200 long, so it was 200x the
        # arithmetic for the same numbers.
        energy = region.real ** 2 + region.imag ** 2
        run = np.empty(len(region) + 1)
        run[0] = 0.0
        np.cumsum(energy, out=run[1:])
        power = run[n:] - run[:-n]
        return raw / np.sqrt(np.maximum(power, 1e-20))

    @staticmethod
    def _refine_peak(mag: np.ndarray, k: int) -> float:
        """Sub-sample peak position by parabolic fit.

        Worth the four lines: at 3 samples per symbol a whole-sample timing
        error is a third of a symbol, which closes the 256QAM eye on its own.
        """
        if k <= 0 or k >= len(mag) - 1:
            return float(k)
        a, b, c = mag[k - 1], mag[k], mag[k + 1]
        denom = a - 2.0 * b + c
        if abs(denom) < 1e-20:
            return float(k)
        return float(k) + 0.5 * (a - c) / denom

    def _first_periodic_peak(self, mag: np.ndarray, lo: int) -> int | None:
        """Earliest correlation peak that has a partner one frame later.

        Threshold alone is not enough to acquire on. A single peak proves only
        that something correlated; a *pair* one frame apart is the structure
        that distinguishes a preamble from a lucky sidelobe, and checking for
        it costs one array slice. Without it, acquisition can lock to a
        sidelobe and stay confidently wrong -- the payload never decodes but
        the receiver reports lock, which is the worst of both.
        """
        nominal = self.profile.frame_symbols * self.sps
        slack = max(self.sps * 4, int(nominal * 2e-3))
        above = np.flatnonzero(mag >= SYNC_THRESHOLD)
        if not len(above):
            return None
        # Walk candidate peaks earliest first, collapsing each cluster.
        i = 0
        while i < len(above):
            k0 = int(above[i])
            win_end = min(len(mag), k0 + self._ref_len)
            k = k0 + int(np.argmax(mag[k0:win_end]))
            j = k + nominal
            if j + slack < len(mag):
                partner = mag[max(0, j - slack):j + slack]
                if len(partner) and partner.max() >= SYNC_THRESHOLD:
                    return k
            elif j - slack < len(mag):
                # Not enough buffer to confirm; accept rather than stall, the
                # per-frame search will drop it next round if it was wrong.
                return k
            while i < len(above) and above[i] < k + self._ref_len:
                i += 1
        return None

    def _find_sync(self, search_from: int, search_to: int,
                   first: bool = False) -> tuple[float, float] | None:
        """Locate one preamble in [search_from, search_to) absolute samples.

        ``first`` selects the *earliest* preamble above threshold rather than
        the tallest. That distinction only matters during initial acquisition,
        where the search spans the whole buffer and every frame's peak is
        equally good -- taking the global maximum there means floating-point
        noise decides which frame to lock to, and everything before it is
        thrown away. Locking to the earliest costs nothing and keeps the
        startup gap to one frame.
        """
        lo = max(0, search_from - self._buf_start)
        hi = min(len(self._buf), search_to - self._buf_start + self._ref_len)
        if hi - lo < self._ref_len:
            return None
        corr = self._correlate(self._buf[lo:hi])
        if not len(corr):
            return None
        mag = np.abs(corr)
        if first:
            k = self._first_periodic_peak(mag, lo)
            if k is None:
                return None
        else:
            k = int(np.argmax(mag))
        if mag[k] < SYNC_THRESHOLD:
            return None
        pos = self._refine_peak(mag, k)
        # 'valid' convolution with the time-reversed conjugate reference makes
        # index k the correlation at lag k, so k *is* the offset of the
        # preamble's first sample within the search region. No further shift.
        return self._buf_start + lo + pos, float(mag[k])

    # -- symbol extraction ----------------------------------------------

    def _interpolate(self, start: float, count: int, step: float) -> np.ndarray:
        """Windowed-sinc interpolation of ``count`` symbols from ``start``.

        A cubic (Catmull-Rom) interpolator lives here in most textbook
        receivers and is not good enough for this one. Its error is about
        -20 dB at the 0.4-of-Nyquist occupancy these profiles run at, which
        caps EVM far below what 256QAM needs.

        The reason that is easy to miss: with a perfect sampling clock the
        symbol instants land on whole samples, the interpolator is never
        actually asked to interpolate, and everything measures beautifully.
        The error only appears once there is clock error -- that is, on every
        real link and no synthetic one.
        """
        idx = start + step * np.arange(count) - self._buf_start
        return interp.sample_at(self._buf, idx)

    # -- carrier ---------------------------------------------------------

    def _estimate_carrier(self, pre: np.ndarray) -> tuple[float, float]:
        """(frequency error in Hz, phase) from the preamble.

        The product of received against known preamble rotates at exactly the
        carrier error, so a linear fit of its unwrapped phase recovers both.
        """
        known = framing.preamble()
        prod = pre * np.conj(known)
        ph = np.unwrap(np.angle(prod))
        n = np.arange(len(ph))
        A = np.vstack([n, np.ones_like(n)]).T
        slope, intercept = np.linalg.lstsq(A, ph, rcond=None)[0]
        return slope * self.profile.symbol_rate / (2.0 * np.pi), float(intercept)

    # -- equaliser -------------------------------------------------------

    def _solve_equaliser(self, pre: np.ndarray) -> np.ndarray:
        """Least-squares equaliser taps from the known preamble.

        Solves directly rather than letting an adaptive filter converge:
        there are 64 known symbols available at the top of every frame, which
        is plenty to condition 25 taps, and a direct solve has no convergence
        transient to eat into the payload.
        """
        known = framing.preamble()
        n = self.eq_taps
        valid = len(pre) - n + 1
        if valid < 1:
            return self._eq
        # Rows are sliding windows of the received preamble.
        R = np.lib.stride_tricks.sliding_window_view(pre, n)[:valid][:, ::-1]
        target = known[n // 2: n // 2 + valid]

        # Accumulate the normal equations across frames rather than solving
        # each frame alone. One 64-symbol preamble yields only (64 - n + 1)
        # equations, which for the 57 taps ACOUSTIC needs is 8 -- hopelessly
        # underdetermined, and a single-frame solve just returns noise.
        # The channel changes far more slowly than the frame rate, so summing
        # with exponential forgetting turns ten frames into ~170 equations and
        # the same solve becomes well posed.
        rr = R.conj().T @ R
        rt = R.conj().T @ target
        if self._rr is None:
            self._rr, self._rt = rr, rt
        else:
            self._rr = EQ_FORGET * self._rr + rr
            self._rt = EQ_FORGET * self._rt + rt
        reg = 1e-3 * np.trace(self._rr).real / n * np.eye(n)
        try:
            return np.linalg.solve(self._rr + reg, self._rt)
        except np.linalg.LinAlgError:
            return self._eq

    def _apply_equaliser(self, symbols: np.ndarray, taps: np.ndarray) -> np.ndarray:
        pad = len(taps) // 2
        padded = np.concatenate([np.zeros(pad, dtype=complex), symbols,
                                 np.zeros(pad, dtype=complex)])
        return np.convolve(padded, taps, mode="valid")[:len(symbols)]

    # -- main loop -------------------------------------------------------

    def frames(self) -> list[FrameResult]:
        """Demodulate whatever complete frames are buffered."""
        out: list[FrameResult] = []
        nominal = self.profile.frame_symbols * self.sps
        # Wide enough to absorb any plausible clock error over one frame.
        slack = max(self.sps * 4, int(nominal * 2e-3))

        while True:
            if self._prev_sync is None:
                # Acquisition needs a *pair* of peaks a frame apart, so it
                # cannot possibly succeed until that much is buffered -- and
                # this runs on every feed, each one re-correlating the whole
                # buffer from scratch. Without the guard a 96 kHz card spent a
                # dozen full-buffer scans proving it could not yet know.
                if len(self._buf) < nominal + slack + self._ref_len:
                    break
                found = self._find_sync(self._buf_start,
                                        self._buf_start + len(self._buf),
                                        first=True)
                if found is None:
                    self._trim(len(self._buf) - self._ref_len - nominal)
                    break
                self._prev_sync, _ = found
                continue

            # The next preamble sits about one frame on.
            guess = self._prev_sync + nominal
            if self._buf_start + len(self._buf) < guess + slack + self._ref_len:
                break
            found = self._find_sync(int(guess - slack), int(guess + slack))
            if found is None:
                # Lost it. Drop this frame and re-acquire from scratch.
                #
                # The accumulated equaliser normal equations go too. They are
                # summed with forgetting across frames, so after a dropout
                # they describe a channel that may no longer exist -- and the
                # thing that made the signal disappear is exactly the sort of
                # event that changes it. Keeping them biases the solve for the
                # ten or so frames it takes them to decay, which is the worst
                # possible moment to be equalising for the wrong channel.
                self._prev_sync = None
                self._eq_valid = False
                self._rr = None
                self._rt = None
                self._eq = np.zeros(self.eq_taps, dtype=np.complex128)
                self._eq[self.eq_taps // 2] = 1.0
                self.stats.resyncs += 1
                self._trim(int(guess - self._buf_start))
                continue
            nxt, peak = found
            result = self._demod_frame(self._prev_sync, nxt, peak)
            if result is not None:
                out.append(result)
            self._prev_sync = nxt
            self._trim(int(nxt - self._buf_start) - self.sps * 4)
        return out

    def _trim(self, upto: int) -> None:
        if upto > 0:
            upto = min(upto, len(self._buf))
            self._buf = self._buf[upto:]
            self._buf_start += upto

    def _demod_frame(self, start: float, nxt: float, peak: float) -> FrameResult | None:
        p = self.profile
        span = nxt - start
        step = span / p.frame_symbols
        try:
            sym = self._interpolate(start, p.frame_symbols, step)
        except IndexError:
            return None
        self.stats.frames_seen += 1

        # AGC on the preamble, which has known unit modulus.
        pre_raw = sym[:PREAMBLE_SYMBOLS]
        amp = float(np.sqrt(np.mean(np.abs(pre_raw) ** 2))) or 1.0
        sym = sym / amp

        pre = sym[:PREAMBLE_SYMBOLS]
        df, phi = self._estimate_carrier(pre)
        rot = np.exp(-1j * (phi + 2.0 * np.pi * df / p.symbol_rate
                            * np.arange(p.frame_symbols)))
        sym = sym * rot

        taps = self._solve_equaliser(sym[:PREAMBLE_SYMBOLS])
        # Blend with the previous frame's solution: the channel changes far
        # more slowly than once per frame, and averaging trades a little
        # tracking speed for a lot of noise on the tap estimates.
        self._eq = taps if not self._eq_valid else 0.5 * self._eq + 0.5 * taps
        self._eq_valid = True
        eq = self._apply_equaliser(sym, self._eq)

        eq = self._track_pilots(eq)

        # MODCOD first, from the BPSK codeword. Correlation against the whole
        # codebook, so this survives conditions that would lose a coded header
        # outright -- which is the point of moving it out of one.
        cw = eq[PREAMBLE_SYMBOLS:PREAMBLE_SYMBOLS + HEADER_SYMBOLS]
        modcod_index, margin = framing.detect_modcod(cw)

        from .profiles import MODCOD_BY_INDEX
        modcod = MODCOD_BY_INDEX.get(modcod_index)
        slots = framing.data_slots(p)
        result = FrameResult(
            header=None, symbols=eq[slots], freq_offset_hz=float(df),
            timing_ppm=float((step / self.sps - 1.0) * 1e6), corr_peak=peak,
            modcod_margin=float(margin),
        )
        if modcod is None or margin < MODCOD_MARGIN:
            self.stats.headers_failed += 1
            return result

        data = eq[slots]
        family = self.profile.constellation_family
        if family == constellation.APSK and modcod.bits_per_symbol > 2:
            # Take the amplifier's per-ring gain and rotation back out. Only
            # worth doing where there is more than one ring: at QPSK the
            # constellation is already constant modulus and there is nothing
            # for compression to distort differently between points.
            fixed, gains = constellation.derings(data, modcod.bits_per_symbol)
            if constellation.evm_db(fixed, modcod.bits_per_symbol, family) > \
                    constellation.evm_db(data, modcod.bits_per_symbol, family):
                data = fixed
                eq = eq.copy()
                eq[slots] = fixed
                result.ring_gains = gains
        result.symbols = data
        result.modcod = modcod
        result.evm_db = constellation.evm_db(
            data, modcod.bits_per_symbol, family)
        result.snr_db = result.evm_db
        noise_var = 10.0 ** (-result.evm_db / 10.0)
        hdr, payload = framing.parse_frame(p, modcod, eq, max(noise_var, 1e-4))
        if hdr is None:
            self.stats.headers_failed += 1
            return result
        self.stats.headers_ok += 1
        result.header = hdr
        result.payload = payload
        return result

    def _track_pilots(self, sym: np.ndarray) -> np.ndarray:
        """Remove residual phase drift using the scattered pilots.

        The preamble fixes phase at the top of the frame; over the next few
        thousand symbols an uncorrected residual frequency error of even a
        fraction of a hertz walks the constellation round. Interpolating phase
        between pilots costs nothing and is what keeps 256QAM standing up at
        the far end of a frame.
        """
        p = self.profile
        pilots = framing.pilot_slots(p)
        if not len(pilots):
            return sym
        known = framing.pilot_sequence(p.pilot_groups)
        err = sym[pilots] * np.conj(known)
        ph = np.unwrap(np.angle(err))
        full = np.interp(np.arange(len(sym)), pilots, ph)
        return sym * np.exp(-1j * full)
