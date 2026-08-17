"""OFDM: the multipath answer to the single-carrier delay-spread limit.

Single-carrier QAM equalises an echo by inverting it, and that only works as
far as the equaliser reaches. Here it reaches 25 taps -- +-12 symbols, 0.38 ms
on WIDE -- because the equaliser is trained on a 64-symbol preamble and a
least-squares solve for n taps gets only 64-n+1 equations out of it. Measured,
even training a longer equaliser on the *true* transmitted symbols buys only
1 to 3.5 dB, so the limit is real and not a tuning failure.

OFDM does not invert the echo at all. A cyclic prefix longer than the delay
spread turns the channel's linear convolution into a circular one, and a
circular convolution is a per-subcarrier multiply. Equalisation is then one
complex divide per subcarrier, exact by construction, for any echo that fits
inside the prefix. The cost is the prefix itself -- pure overhead -- and a
higher peak-to-average ratio.

Built as discrete multitone, which is to say a real IFFT straight at the card
rate rather than a complex baseband upconverted afterwards. The output has to
be real for a sound card either way, and doing it this way means the carrier
placement *is* the choice of which bins to fill: no mixer, no image, no
interpolation filter. The occupied band is exactly the bins used.

Frame layout, each symbol prefixed by its cyclic prefix:

    [ preamble ][ MODCOD codeword ][ data x symbols_per_frame ]

The preamble is a known symbol, so one division against it gives the channel
on every subcarrier at once -- the same "one shot from a known reference"
principle the single-carrier front end is built on.
"""

from __future__ import annotations

import functools
from dataclasses import dataclass

import numpy as np

# framing is imported inside the functions that need it, not here. It imports
# profiles, and profiles imports this module for OfdmGeometry -- at module
# level that is a cycle, and it fails on the profile validation that runs at
# import time. Nothing above the coded wrappers needs framing anyway.

# Subcarriers carrying a known reference inside the data symbols, for tracking
# phase drift between preambles. One in twelve costs 8% and is what keeps
# 256QAM standing up across a frame.
PILOT_EVERY = 12

# A fraction is the wrong rule at the narrow end, though. A 24-carrier geometry
# with one pilot in twelve gets two of them, and a straight line through two
# noisy points is not a fit -- it is an interpolation, with nothing left over to
# average the noise away, and the phase tilt below is fitted rather than
# averaged for a reason. Below this width the stride tightens instead. It costs
# throughput on the small geometries and is what makes them work at all.
MIN_PILOTS = 4


def pilot_stride(carriers: int) -> int:
    """One pilot every N subcarriers, for a geometry this wide."""
    return max(2, min(PILOT_EVERY, carriers // MIN_PILOTS))


@dataclass(frozen=True)
class OfdmGeometry:
    """Everything about the frame that both ends must agree on exactly."""

    sample_rate: int
    fft: int                  # IFFT size, real transform, so fft//2+1 bins
    cp: int                   # cyclic prefix, samples
    bin_lo: int               # first occupied bin
    bin_hi: int               # last occupied bin, inclusive
    symbols_per_frame: int    # data symbols, excluding preamble and codeword

    # ---- spectrum -------------------------------------------------------

    @property
    def bin_spacing(self) -> float:
        """Subcarrier spacing, Hz."""
        return self.sample_rate / self.fft

    @property
    def carriers(self) -> int:
        return self.bin_hi - self.bin_lo + 1

    @property
    def band(self) -> tuple[float, float]:
        """Occupied band. Exact -- these bins and no others are driven."""
        return (self.bin_lo * self.bin_spacing,
                (self.bin_hi + 1) * self.bin_spacing)

    @property
    def bandwidth(self) -> float:
        lo, hi = self.band
        return hi - lo

    # ---- timing ---------------------------------------------------------

    @property
    def symbol_samples(self) -> int:
        """One OFDM symbol on the wire, prefix included."""
        return self.fft + self.cp

    @property
    def symbol_duration(self) -> float:
        return self.symbol_samples / self.sample_rate

    @property
    def max_delay_spread(self) -> float:
        """Echo delay the prefix absorbs outright, seconds.

        Not "the equaliser can usually cope"; anything within this is removed
        exactly, and anything beyond it breaks subcarrier orthogonality and
        cannot be recovered by a one-tap equaliser at all. The edge is sharp,
        which is the whole appeal.
        """
        return self.cp / self.sample_rate

    @property
    def max_freq_offset(self) -> float:
        """Carrier offset the receiver can still measure, Hz.

        The estimator fits the pilots' common phase against symbol index, so
        the advance from one symbol to the next has to stay under half a turn
        -- past that the unwrap folds it back and reports the wrong offset
        confidently. The bound is one over twice the symbol duration, so it
        falls as the spacing narrows: 178 Hz at 400 Hz spacing on a 48 kHz
        card, 11 Hz at 25 Hz spacing.

        This is the cliff, not a gradual softening, and it is what stops the
        spacing being narrowed indefinitely for echo tolerance. Measured
        through the `radio` preset, which drifts 15 Hz: every spacing from
        400 Hz down to 50 Hz decodes 35 or more frames in 40, and 25 Hz --
        11 Hz of tolerance against 15 Hz of drift -- decodes none at all.
        """
        return 0.5 / self.symbol_duration

    @property
    def frame_symbols(self) -> int:
        """OFDM symbols per frame, including preamble and codeword."""
        return self.symbols_per_frame + 2

    @property
    def frame_samples(self) -> int:
        return self.frame_symbols * self.symbol_samples

    @property
    def frame_duration(self) -> float:
        return self.frame_samples / self.sample_rate

    # ---- payload --------------------------------------------------------

    @property
    def pilot_every(self) -> int:
        """Subcarriers between references inside a data symbol."""
        return pilot_stride(self.carriers)

    @functools.cached_property
    def pilot_bins(self) -> np.ndarray:
        """Subcarriers carrying a reference inside a data symbol."""
        offsets = np.arange(0, self.carriers, self.pilot_every)
        return self.bin_lo + offsets

    @functools.cached_property
    def data_bins(self) -> np.ndarray:
        """Subcarriers carrying payload."""
        allb = np.arange(self.bin_lo, self.bin_hi + 1)
        return allb[~np.isin(allb, self.pilot_bins)]

    @property
    def data_carriers(self) -> int:
        return len(self.data_bins)

    @property
    def payload_symbols(self) -> int:
        """QAM symbols of payload per frame."""
        return self.data_carriers * self.symbols_per_frame

    @property
    def symbol_rate(self) -> float:
        """Payload QAM symbols per second.

        The figure to compare against a single-carrier profile's baud: it
        already has the prefix, the pilots and the preamble taken out.
        """
        return self.payload_symbols / self.frame_duration

    def describe(self) -> str:
        lo, hi = self.band
        return (f"{self.carriers} carriers {self.bin_spacing:.0f} Hz apart, "
                f"{self.fft}-point, {lo/1000:.1f}-{hi/1000:.1f} kHz. "
                f"Absorbs {self.max_delay_spread*1000:.2f} ms of echo, "
                f"pulls in {self.max_freq_offset:.0f} Hz of carrier offset. "
                f"{self.symbol_rate:.0f} payload sym/s")


def geometry(sample_rate: int, band_lo: float, band_hi: float, fft: int = 512,
             cp_fraction: int = 8, symbols_per_frame: int = 18) -> OfdmGeometry:
    """Fit a geometry to a wanted band on a given card.

    ``cp_fraction`` is the prefix as a fraction of the symbol: 8 means an
    eighth, which costs 11% of the throughput and buys fft/8 samples of delay
    spread. Shorter prefixes are cheaper and less forgiving; the trade is the
    only real dial in an OFDM design.
    """
    spacing = sample_rate / fft
    bin_lo = max(1, int(np.ceil(band_lo / spacing)))
    bin_hi = min(fft // 2 - 1, int(np.floor(band_hi / spacing)) - 1)
    if bin_hi <= bin_lo:
        raise ValueError(
            f"{band_lo:.0f}-{band_hi:.0f} Hz gives no usable subcarriers at "
            f"{spacing:.1f} Hz spacing"
        )
    return OfdmGeometry(sample_rate=sample_rate, fft=fft, cp=fft // cp_fraction,
                        bin_lo=bin_lo, bin_hi=bin_hi,
                        symbols_per_frame=symbols_per_frame)


# --------------------------------------------------------------------------
# Spacing and carrier count
# --------------------------------------------------------------------------
#
# Two numbers, two separate jobs:
#
#   **spacing** is the mode. It alone decides how much echo the prefix absorbs
#       and how much carrier offset the estimator can pull in, and it is the
#       only thing that decides either.
#   **carrier count** is the bandwidth, and therefore the bitrate. The block is
#       count x spacing wide. Halving the count halves the occupied width and
#       halves the payload rate, and changes nothing else.
#
# This used to be one dial doing both jobs badly: the band was fixed and the
# count divided it, so the count *was* the spacing -- it moved echo tolerance
# up while moving offset tolerance down, and left the throughput alone, which
# is the one thing an operator would expect a "how many carriers" control to
# change. Bandwidth could not be traded for ruggedness at all, and that trade
# turns out to be the valuable one. See tools/spacing.py: the `reverb` channel,
# 6 ms of delay spread and documented in the README as the case this modem
# fails, decodes 38/40 at 25 Hz spacing on 384 carriers -- 9.6 kHz of band
# instead of 19.2. The old dial could not get there from a fixed band.
#
# Spectral efficiency barely moves across the whole range -- 4.91 bps/Hz at
# 300 Hz down to 4.34 at 25 Hz, twelve per cent for a sixteen-fold change in
# spacing -- so the spacing is chosen for the channel rather than for the rate,
# and the cost of buying echo tolerance is much lower than it looks.

# Frame length aimed for, seconds. Held roughly constant across carrier counts
# on purpose. The preamble overhead, the acquisition delay and the interval
# between carrier-offset updates are all counted in frames, so letting the
# frame duration swing by a factor of sixteen with the carrier count would mean
# changing the carrier count quietly changed all of those too. Fixing the
# duration leaves exactly one thing moving -- the trade above -- which is the
# whole point of putting it on a dial.
FRAME_TARGET_SECONDS = 0.22

# The MODCOD codeword is laid across the subcarriers, and there are 32 of them
# to tell apart. Below 24 columns the Walsh construction stops even producing
# 32 distinct rows, so the narrowest geometry that can signal every MODCOD is
# the narrowest geometry there is.
MIN_CARRIERS = 24

# Floor on the data symbols in a frame, which only bites where the symbols are
# so long that the target duration would not hold many. Two of them are
# preamble and codeword, so a frame of six would spend a quarter of the air on
# overhead -- and the carrier-offset estimate is a straight line fitted through
# one point per symbol, which wants more than a handful to mean anything.
MIN_FRAME_SYMBOLS = 12


# Spacings are nominal. The transform has to be a whole number of samples, so
# the delivered spacing is sample_rate / round(sample_rate / nominal) and comes
# out within a fraction of a hertz. Both ends derive it the same way from the
# same two numbers, so they agree exactly; the nominal figure is a label.
#
# What the spacing dial offers, Hz. The span is set at one end by there being
# no point going wider -- see the measured table in tools/spacing.py, where
# 300 Hz already absorbs less echo than the single-carrier equaliser it is
# supposed to beat -- and at the other by the carrier-offset estimator, which
# is what fails first as the symbol lengthens.
OFDM_SPACING_CHOICES = (400, 300, 200, 150, 100, 75, 50, 25)


def fft_for_spacing(sample_rate: int, spacing_hz: float) -> int:
    """Transform size delivering the nearest spacing to the one asked for."""
    if spacing_hz <= 0:
        raise ValueError(f"spacing must be positive, got {spacing_hz}")
    fft = int(round(sample_rate / spacing_hz)) & ~1
    if fft < 8:
        raise ValueError(
            f"{spacing_hz:g} Hz spacing needs a {fft}-point transform at "
            f"{sample_rate} Hz, which is too small to carry a signal"
        )
    return fft


def for_spacing(sample_rate: int, spacing_hz: float, carriers: int,
                band_lo: float, band_hi: float, cp_fraction: int = 8,
                symbols_per_frame: int | None = None) -> OfdmGeometry:
    """A geometry of ``carriers`` subcarriers at a fixed spacing.

    The band is a placement envelope rather than a target to fill: the block is
    ``carriers`` wide whatever the band is, and is centred in it. Centred, not
    anchored low, so reducing the count takes the same amount off each edge and
    the signal stays where the operator put it.
    """
    if carriers < MIN_CARRIERS:
        raise ValueError(f"at least {MIN_CARRIERS} carriers, got {carriers}")
    fft = fft_for_spacing(sample_rate, spacing_hz)
    spacing = sample_rate / fft
    top = fft // 2 - 1
    if carriers > top:
        raise ValueError(
            f"{carriers} carriers at {spacing:.1f} Hz needs "
            f"{carriers * spacing / 1000:.1f} kHz, past the "
            f"{sample_rate / 2000:.1f} kHz Nyquist limit of this card"
        )
    lo = max(1, int(np.ceil(band_lo / spacing)))
    hi = min(top, int(np.floor(band_hi / spacing)) - 1)
    room = hi - lo + 1
    if room < carriers:
        # Refused rather than allowed to spill. The band is not a suggestion --
        # on an FM path its upper edge is the 19 kHz stereo pilot, and a block
        # that quietly ran past it would be transmitting into the pilot while
        # every panel still reported the band it was asked for.
        raise ValueError(
            f"{carriers} carriers at {spacing:.0f} Hz spacing need "
            f"{carriers * spacing / 1000:.1f} kHz, and "
            f"{band_lo:.0f}-{band_hi:.0f} Hz has room for {max(0, room)}. "
            f"Use fewer carriers, or a narrower spacing."
        )
    lo += (room - carriers) // 2              # centred in the envelope
    cp = max(1, fft // cp_fraction)
    if symbols_per_frame is None:
        want = FRAME_TARGET_SECONDS * sample_rate / (fft + cp)
        symbols_per_frame = max(MIN_FRAME_SYMBOLS, int(round(want)) - 2)
    return OfdmGeometry(sample_rate=sample_rate, fft=fft, cp=cp,
                        bin_lo=lo, bin_hi=lo + carriers - 1,
                        symbols_per_frame=symbols_per_frame)


# --------------------------------------------------------------------------
# Known references
# --------------------------------------------------------------------------

@functools.lru_cache(maxsize=None)
def reference(carriers: int, seed: int = 1) -> np.ndarray:
    """Unit-modulus reference sequence for the preamble and the pilots.

    Zadoff-Chu, for the same reason the single-carrier preamble uses it: flat
    magnitude across every subcarrier, so dividing by it to estimate the
    channel never amplifies noise on a weak reference, and a constant-modulus
    symbol keeps the preamble's peak-to-average ratio down.
    """
    n = carriers
    q = seed
    k = np.arange(n)
    return np.exp(-1j * np.pi * q * k * (k + 1) / n)


def _preamble_spectrum(geo: OfdmGeometry) -> np.ndarray:
    spec = np.zeros(geo.fft // 2 + 1, dtype=complex)
    spec[geo.bin_lo:geo.bin_hi + 1] = reference(geo.carriers)
    return spec


@functools.lru_cache(maxsize=None)
def _preamble_time(geo: OfdmGeometry) -> np.ndarray:
    """The preamble symbol as transmitted, prefix included."""
    return _to_time(_preamble_spectrum(geo), geo)


@functools.lru_cache(maxsize=None)
def _preamble_quad(geo: OfdmGeometry) -> np.ndarray:
    """The preamble shifted 90 degrees -- the other half of the matched filter.

    Multiplying every bin by -j shifts each component a quarter cycle, which is
    the Hilbert transform of the preamble computed exactly rather than
    estimated. Exactly, because the transform is over the same periodic body
    the prefix is copied from, so there are no window edges to get wrong.
    """
    return _to_time(-1j * _preamble_spectrum(geo), geo)


def _to_time(spec: np.ndarray, geo: OfdmGeometry) -> np.ndarray:
    """One OFDM symbol: real IFFT, then prepend the cyclic prefix."""
    body = np.fft.irfft(spec, n=geo.fft)
    return np.concatenate([body[-geo.cp:], body])


def _from_time(block: np.ndarray, geo: OfdmGeometry) -> np.ndarray:
    """Strip the prefix and transform back. Inverse of _to_time."""
    return np.fft.rfft(block[geo.cp:geo.cp + geo.fft])


# --------------------------------------------------------------------------
# Modulator
# --------------------------------------------------------------------------

@functools.lru_cache(maxsize=None)
def _pilot_matrix(geo: OfdmGeometry) -> np.ndarray:
    """Every data symbol's pilot references at once, shape (symbols, pilots).

    Drawn from the same Zadoff-Chu sequence as the preamble, rotated per
    symbol so a run of data symbols never puts an identical waveform on the
    wire twice -- repeated symbols would add coherently and put a line in the
    spectrum.

    Built whole and cached rather than one row at a time. It depends on nothing
    but the geometry, and both ends were rebuilding a row per symbol -- 5655
    identical rotations per second of audio on the narrow geometries.
    """
    ref = reference(len(geo.pilot_bins), seed=1)
    n = len(ref)
    k = np.arange(n)
    turns = np.arange(1, geo.symbols_per_frame + 1)[:, None] * k[None, :] / n
    return ref[None, :] * np.exp(2j * np.pi * turns)


def _pilot_values(geo: OfdmGeometry, index: int) -> np.ndarray:
    """References carried on the pilot subcarriers of data symbol ``index``."""
    return _pilot_matrix(geo)[index]


def _modcod_length(carriers: int) -> int:
    """Codeword length to lay across a geometry this wide.

    A power of two wherever possible, because the codewords are Walsh rows and
    those are orthogonal over a power-of-two length and *not* over an arbitrary
    truncation of one. Cutting 64 columns down to 48 leaves pairs correlating
    at 0.33, which drags the detector's clean margin from 1.0 to 0.67 and eats
    the noise headroom on the one field every other field waits for. Taking 32
    columns instead -- and letting the spare carriers hold a partial repeat the
    receiver ignores -- is exact.

    Below 32 carriers there is no exact answer: 32 codewords cannot be mutually
    orthogonal in fewer than 32 dimensions. The narrowest geometry takes the
    0.67 margin, which is still well clear of the 0.25 needed to accept.
    """
    from . import framing
    if carriers < framing.MODCOD_CODEWORDS:
        return carriers
    n = framing.MODCOD_CODEWORDS
    while n * 2 <= min(carriers, framing.HEADER_SYMBOLS):
        n *= 2
    return n


def _modcod_spectrum(geo: OfdmGeometry, modcod_index: int) -> np.ndarray:
    """The MODCOD codeword spread across every occupied subcarrier.

    Tiled where the carriers outnumber the codeword. The receiver averages the
    whole repeats before correlating, which is free processing gain on the one
    field everything else waits for -- the payload cannot be demapped until the
    MODCOD is known. Any ragged tail is still transmitted, so the occupied band
    stays filled; the receiver simply does not fold it in.
    """
    from . import framing
    word = framing.encode_modcod(modcod_index, _modcod_length(geo.carriers))
    reps = int(np.ceil(geo.carriers / len(word)))
    return np.tile(word, reps)[:geo.carriers]


class Modulator:
    """QAM symbols in, real passband samples out.

    Stateless between frames: OFDM symbols are independent by construction,
    and the cyclic prefix is exactly what removes the inter-symbol memory a
    pulse-shaped single-carrier signal has to carry.
    """

    def __init__(self, geo: OfdmGeometry, level_dbfs: float = -18.0):
        self.geo = geo
        # Lower than the single-carrier default. Summing hundreds of
        # independent subcarriers is the textbook route to a Gaussian
        # amplitude distribution, so the peak-to-average ratio is worse than
        # any single constellation's -- measured near 12 dB against 10 for
        # 256QAM. Clipping an OFDM symbol damages every subcarrier at once.
        self.level = 10.0 ** (level_dbfs / 20.0)
        self._scale = np.sqrt(geo.fft) * self.level

    def frame(self, modcod_index: int, payload: np.ndarray) -> np.ndarray:
        """One frame: preamble, MODCOD codeword, then the payload symbols."""
        geo = self.geo
        payload = np.asarray(payload, dtype=complex).ravel()
        if len(payload) != geo.payload_symbols:
            raise ValueError(
                f"frame needs exactly {geo.payload_symbols} QAM symbols, "
                f"got {len(payload)}"
            )
        out = [_preamble_time(geo)]

        spec = np.zeros(geo.fft // 2 + 1, dtype=complex)
        spec[geo.bin_lo:geo.bin_hi + 1] = _modcod_spectrum(geo, modcod_index)
        out.append(_to_time(spec, geo))

        data = payload.reshape(geo.symbols_per_frame, geo.data_carriers)
        for i in range(geo.symbols_per_frame):
            spec = np.zeros(geo.fft // 2 + 1, dtype=complex)
            spec[geo.pilot_bins] = _pilot_values(geo, i)
            spec[geo.data_bins] = data[i]
            out.append(_to_time(spec, geo))
        return np.concatenate(out) * self._scale


# --------------------------------------------------------------------------
# Demodulator
# --------------------------------------------------------------------------

# Normalised correlation needed to accept a preamble. The same reasoning as
# the single-carrier front end: well above the sidelobe floor against random
# payload, so acquisition cannot lock to a sidelobe and report nonsense.
#
# It cannot be one number, because the preamble is one OFDM symbol and that is
# 1080 samples at 384 carriers and 72 at 24. A normalised correlation against
# unrelated data falls off as 1/sqrt(length), and measured on pure noise the
# 99.99th percentile of the metric comes out at 3.83/sqrt(length) at every
# length tried -- 0.16 at 540 samples, 0.29 at 180, 0.45 at 72. A single 0.35
# is a sensible floor for the wide geometries and *below the noise* for the
# narrow ones, which would have them acquiring on silence.
#
# Set just above that floor rather than as high as the signal would allow,
# because the two errors are not symmetric. Accepting a sidelobe costs two
# frames: the MODCOD codeword then fails to detect, the next prediction misses,
# and the receiver re-acquires. Rejecting a real preamble costs the link. The
# measured true peak runs 1.00 on a clean channel and 0.50-0.70 through
# multipath, so there is room either way -- but only in this direction is
# getting it wrong cheap.
SYNC_THRESHOLD = 0.35
SYNC_FLOOR_K = 4.4          # 1.15x the measured noise floor of 3.83/sqrt(n)
SYNC_THRESHOLD_MAX = 0.55   # never above what multipath leaves of the peak
MODCOD_MARGIN = 0.25


def sync_threshold(preamble_samples: int) -> float:
    """Correlation to accept, for a preamble of this length."""
    return float(min(SYNC_THRESHOLD_MAX,
                     max(SYNC_THRESHOLD,
                         SYNC_FLOOR_K / np.sqrt(preamble_samples))))


@dataclass
class OfdmResult:
    """One demodulated frame."""

    symbols: np.ndarray                  # equalised payload, for the scope
    modcod_index: int | None = None
    modcod_margin: float = 0.0
    evm_db: float = 0.0
    corr_peak: float = 0.0
    timing_offset: int = 0
    cfo_hz: float = 0.0

    @property
    def locked(self) -> bool:
        return self.modcod_index is not None


@dataclass
class OfdmStats:
    frames_seen: int = 0
    modcod_ok: int = 0
    modcod_failed: int = 0
    resyncs: int = 0

    # Named as the single-carrier stats are, so the receiver's status panel
    # reads either without knowing which mode produced it.
    @property
    def headers_ok(self) -> int:
        return self.modcod_ok

    @property
    def headers_failed(self) -> int:
        return self.modcod_failed

    @property
    def header_error_rate(self) -> float:
        total = self.modcod_ok + self.modcod_failed
        return self.modcod_failed / total if total else 0.0


class Demodulator:
    """Real passband samples in, equalised payload symbols out.

    Acquisition is per frame and data-aided, exactly as in the single-carrier
    receiver: correlate for the known preamble, then work forward from it. No
    loops to tune, and joining a broadcast already in progress is the same
    code path as starting with one.

    Timing only has to land inside the cyclic prefix. An offset within it is a
    linear phase ramp across the subcarriers, and since the preamble sees the
    identical ramp, the channel estimate absorbs it exactly. That tolerance --
    tens of samples rather than a fraction of one -- is the other half of what
    the prefix buys.
    """

    def __init__(self, geo: OfdmGeometry):
        self.geo = geo
        self.stats = OfdmStats()
        self._buf = np.zeros(0)
        self._start = 0            # absolute sample index of _buf[0]
        self._next: int | None = None   # where the next preamble is expected
        self._ref = _preamble_time(geo)
        norm = np.linalg.norm(self._ref)
        self._ref_norm = self._ref / norm
        # Scaled by the in-phase reference's norm, not its own. The two are
        # equal to a rounding error -- a signal and its Hilbert transform carry
        # the same energy -- but making that an assumption rather than a shared
        # constant would put a small gain error between the two arms, which is
        # exactly what a quadrature detector must not have.
        self._ref_quad = _preamble_quad(geo) / norm
        self._sync = sync_threshold(len(self._ref))
        # How far either side of the prediction to look for the next preamble:
        # a hundred ppm of clock error over a frame, and the odd sample a
        # resampler adds or drops at startup.
        self._slack = max(16, int(geo.frame_samples * 2e-3))
        self._known = reference(geo.carriers)
        self._chan: np.ndarray | None = None
        # Running carrier frequency offset estimate, Hz. OFDM needs this in a
        # way single carrier does not: an offset is a fraction of a subcarrier
        # spacing, and any fraction destroys the orthogonality the whole
        # scheme rests on, turning every neighbour into interference. Measured
        # here, 15 Hz against 93.8 Hz spacing -- 16% -- took the lock rate from
        # 23/23 to 1/23 while an echo and 20 ppm of clock error did nothing.
        self._cfo = 0.0

    def feed(self, samples: np.ndarray) -> None:
        self._buf = np.concatenate([self._buf, np.asarray(samples, dtype=float)])

    def _metric(self, lo: int, hi: int) -> tuple[int, float] | None:
        """Normalised preamble correlation over an absolute sample range.

        Normalised by the local energy so the threshold means the same thing
        at any level -- there is no AGC ahead of this.

        Correlated in quadrature -- against the preamble and against the same
        preamble shifted 90 degrees, combined as a magnitude. That is not
        refinement, it is the difference between working and not. A single real
        correlation measures cos(theta) of the carrier phase, and a frequency
        offset walks theta steadily: measured on the `noisy` preset, 2 Hz
        against a 225 ms frame advances it 162 degrees per frame, so the peak
        slid from 0.99 to 0.31 over ten frames, was rejected, re-acquired, and
        slid again -- losing a frame roughly every ten, forever. It looked like
        a timing fault and was not; the timing offset was zero throughout. The
        magnitude of the two arms does not depend on theta at all.
        """
        n = len(self._ref)
        lo = max(lo, self._start)
        hi = min(hi, self._start + len(self._buf) - n)
        if hi <= lo:
            return None
        a, b = lo - self._start, hi - self._start + n
        seg = self._buf[a:b]
        ci = np.correlate(seg, self._ref_norm, mode="valid")
        cq = np.correlate(seg, self._ref_quad, mode="valid")
        energy = np.sqrt(np.convolve(seg ** 2, np.ones(n), mode="valid"))
        metric = np.hypot(ci, cq) / np.maximum(energy, 1e-12)
        best = int(np.argmax(metric))
        return lo + best, float(metric[best])

    def frames(self) -> list[OfdmResult]:
        """Demodulate whatever complete frames are buffered.

        Each preamble is searched for near where the last one predicts, not
        assumed to be exactly a frame later. A fixed stride looks right and is
        not: a sampling clock a hundred ppm out moves the next preamble by a
        sample or so per frame, and the error accumulates until the search is
        landing on a sidelobe. Measured here as a lock that held for one frame
        and then collapsed to a 0.38 correlation against the echo.
        """
        out: list[OfdmResult] = []
        geo = self.geo
        while True:
            end = self._start + len(self._buf)
            if self._next is None:
                # Acquisition: scan forward for anything over threshold.
                found = self._metric(self._start, self._start + geo.frame_samples)
                if found is None or found[1] < self._sync:
                    if end - self._start > 2 * geo.frame_samples:
                        self._trim(self._start + geo.frame_samples)
                    break
                self._next = found[0]
                continue

            # Enough slack for a hundred ppm of clock error over a frame, and
            # for the odd sample a resampler adds or drops at startup.
            slack = self._slack
            if end < self._next + geo.frame_samples + slack:
                break
            found = self._metric(self._next - slack, self._next + slack)
            if found is None:
                break
            start, peak = found
            if peak < self._sync:
                # Lost it. Re-acquire from scratch rather than drifting on a
                # prediction that is no longer anchored to anything.
                self.stats.resyncs += 1
                self._next = None
                self._trim(start)
                continue
            a = start - self._start
            res = self._demod(self._buf[a:a + geo.frame_samples], peak)
            res.timing_offset = start - self._next
            out.append(res)
            self._next = start + geo.frame_samples
            # Keep the slack window's worth of history rather than cutting to
            # the prediction. Cutting there quietly makes the next search
            # one-sided, because _metric will not read before the buffer
            # starts, and a preamble arriving *earlier* than predicted then
            # cannot be found at all -- the argmax pins to the window edge and
            # the metric decays as the real peak recedes behind it. That is
            # what a sampling clock does in one of its two directions, and it
            # cost a frame every nine on a 20 ppm channel: the correlation of a
            # signal this wide is only about two samples across, so two samples
            # of unsearchable drift is the whole peak.
            self._trim(start + geo.frame_samples - slack)
        return out

    def _trim(self, absolute: int) -> None:
        """Drop everything before an absolute sample index."""
        cut = absolute - self._start
        if cut > 0:
            self._buf = self._buf[cut:]
            self._start += cut

    def _derotate(self, frame: np.ndarray) -> np.ndarray:
        """Shift the whole frame down by the estimated offset.

        Done in the time domain, before the transform, because the damage is
        inter-carrier interference rather than a phase error -- correcting
        phases after the FFT would leave every subcarrier still leaking into
        its neighbours. The signal is real, so it goes via its analytic form
        to be shifted at all.
        """
        if abs(self._cfo) < 0.05:
            return frame
        from scipy.signal import hilbert
        t = np.arange(len(frame)) / self.geo.sample_rate
        return np.real(hilbert(frame) * np.exp(-2j * np.pi * self._cfo * t))

    def _demod(self, frame: np.ndarray, peak: float) -> OfdmResult:
        geo = self.geo
        self.stats.frames_seen += 1
        frame = self._derotate(frame)
        sym = frame.reshape(geo.frame_symbols, geo.symbol_samples)
        bins = slice(geo.bin_lo, geo.bin_hi + 1)

        # One division against the known preamble gives the channel on every
        # subcarrier at once. No iteration, no adaptation -- for a channel
        # whose delay spread fits the prefix this *is* the exact answer.
        rx_pre = _from_time(sym[0], geo)[bins]
        chan = rx_pre / self._known
        # Smooth across frequency: neighbouring subcarriers see almost the
        # same channel, so a three-bin average halves the noise on the
        # estimate at a resolution the channel does not actually vary at.
        #
        # Edge-replicated, not zero-padded. `mode="same"` would divide the
        # first and last estimate by three while summing only two terms,
        # putting them 3.5 dB out -- measured as 5 dB EVM on the edge
        # subcarriers of an otherwise exact clean channel.
        chan = np.convolve(np.r_[chan[0], chan, chan[-1]],
                           np.ones(3) / 3, mode="valid")
        chan[np.abs(chan) < 1e-9] = 1e-9
        self._chan = chan

        rx_mc = _from_time(sym[1], geo)[bins] / chan
        from . import framing
        width = _modcod_length(geo.carriers)
        reps = max(1, len(rx_mc) // width)
        # Fold the whole repeats before correlating -- free processing gain on
        # the field everything else waits for.
        folded = rx_mc[:reps * width].reshape(reps, width).mean(axis=0)
        index, margin = framing.detect_modcod(folded)

        result = OfdmResult(symbols=np.zeros(0, dtype=complex),
                            corr_peak=peak, modcod_margin=float(margin))
        if margin < MODCOD_MARGIN:
            self.stats.modcod_failed += 1
            return result
        self.stats.modcod_ok += 1
        result.modcod_index = int(index)

        pilot_at = geo.pilot_bins - geo.bin_lo
        data_at = geo.data_bins - geo.bin_lo

        # Every data symbol at once, rather than a pass per symbol. The maths
        # is unchanged -- rfft, unwrap and polyfit all take an axis and do the
        # same arithmetic per row -- but the per-symbol overhead is not. A
        # 24-carrier geometry has 145 data symbols in a frame where a
        # 384-carrier one has 12, and paying numpy's dispatch cost 145 times a
        # frame made the narrowest geometry the slowest thing in the receiver
        # by a factor of three. Batched, its cost tracks its carrier count the
        # way it should.
        eq = np.fft.rfft(sym[2:, geo.cp:geo.cp + geo.fft], axis=1)[:, bins] / chan

        # Residual phase drift since the preamble, from the pilots. A rotation
        # common to every subcarrier is what a small timing or frequency error
        # looks like here.
        err = eq[:, pilot_at] * np.conj(_pilot_matrix(geo))
        # Fit phase against subcarrier, not just an average of it. A carrier
        # offset rotates every subcarrier equally, but a sampling clock offset
        # -- a different fault, and one a soundcard always has -- tilts the
        # phase across frequency instead. Averaging sees the tilt as nothing at
        # all and leaves the band edges rotated, which is where the errors then
        # appear.
        ph = np.unwrap(np.angle(err), axis=1)
        # polyfit takes a column per fit, so this is one least-squares solve
        # with symbols_per_frame right-hand sides instead of that many solves.
        slope_f, intercept = np.polyfit(pilot_at, ph.T, 1)
        common = intercept
        fix = np.exp(-1j * (slope_f[:, None] * np.arange(geo.carriers)[None, :]
                            + intercept[:, None]))
        result.symbols = (eq * fix)[:, data_at].ravel()

        # That common phase advances linearly with symbol index when there is
        # a frequency offset, so its slope measures the offset directly. Fed
        # back for the next frame rather than applied to this one: the damage
        # is interference done before the transform, and only a correction
        # applied ahead of it can undo that.
        slope = np.polyfit(np.arange(geo.symbols_per_frame),
                           np.unwrap(common), 1)[0]
        step = slope / (2 * np.pi * geo.symbol_duration)
        # Damped, because a single frame's estimate carries the noise of one
        # frame's pilots and an offset does not move quickly.
        self._cfo += 0.5 * step
        result.cfo_hz = self._cfo
        return result


# --------------------------------------------------------------------------
# Coded frames
# --------------------------------------------------------------------------
#
# These two wrap the raw OFDM layer in the same FEC, signalling and framing
# the single-carrier path uses, and present the same interface as
# modulator.Modulator and demodulator.Demodulator. That is deliberate: the
# apps then choose a class and change nothing else, and both modes share one
# transport chain, one signalling format and one set of measured MODCOD
# thresholds rather than growing a second of each.


class CodedResult:
    """Shaped like demodulator.FrameResult, so callers need not care which."""

    __slots__ = ("header", "symbols", "payload", "modcod", "evm_db", "snr_db",
                 "freq_offset_hz", "timing_ppm", "corr_peak", "modcod_margin")

    def __init__(self, symbols):
        self.header = None
        self.symbols = symbols
        self.payload = None
        self.modcod = None
        self.evm_db = 0.0
        self.snr_db = 0.0
        self.freq_offset_hz = 0.0
        self.timing_ppm = 0.0
        self.corr_peak = 0.0
        self.modcod_margin = 0.0

    @property
    def locked(self) -> bool:
        return self.header is not None


class CodedModulator:
    """Coded frames to real passband audio, for an OFDM profile."""

    def __init__(self, profile, level_dbfs: float | None = None):
        self.profile = profile
        geo = profile.geometry
        self.geo = geo
        self._mod = Modulator(geo) if level_dbfs is None else Modulator(geo, level_dbfs)
        # What went out on the subcarriers last frame, for the constellation
        # display. The single-carrier path reads these back out of the frame
        # by slot index; OFDM has no such slots, so it keeps them.
        self.last_symbols = np.zeros(0, dtype=complex)

    def modulate_frame(self, modcod, header, payload_bytes) -> np.ndarray:
        from . import constellation, framing
        bits = framing.channel_bits(self.profile, modcod, header, payload_bytes)
        self.last_symbols = constellation.modulate(
            bits, modcod.bits_per_symbol, self.profile.constellation_family)
        return self._mod.frame(modcod.index, self.last_symbols)

    def modulate_symbols(self, symbols: np.ndarray) -> np.ndarray:
        raise NotImplementedError("OFDM frames are built from coded bits")

    def flush(self) -> np.ndarray:
        # Nothing to flush: OFDM symbols carry no filter tail between them,
        # which is the same property the cyclic prefix exists to create.
        return np.zeros(0)


class CodedDemodulator:
    """Real passband audio to decoded frames, for an OFDM profile."""

    def __init__(self, profile):
        self.profile = profile
        self.geo = profile.geometry
        self._dem = Demodulator(self.geo)
        self.stats = self._dem.stats

    def feed(self, samples: np.ndarray) -> None:
        self._dem.feed(samples)

    def frames(self) -> list[CodedResult]:
        from . import constellation, framing
        from .profiles import MODCOD_BY_INDEX
        out = []
        for r in self._dem.frames():
            res = CodedResult(r.symbols)
            res.corr_peak = r.corr_peak
            res.modcod_margin = r.modcod_margin
            # Timing is reported in ppm for the same reason as the
            # single-carrier path: it is the sampling clock difference, and a
            # sample or two per frame is meaningful only against the frame.
            res.timing_ppm = (r.timing_offset / self.geo.frame_samples) * 1e6
            if not r.locked:
                out.append(res)
                continue
            modcod = MODCOD_BY_INDEX.get(r.modcod_index)
            if modcod is None:
                out.append(res)
                continue
            res.modcod = modcod
            res.evm_db = constellation.evm_db(
                r.symbols, modcod.bits_per_symbol,
                self.profile.constellation_family)
            res.snr_db = res.evm_db
            noise_var = 10.0 ** (-res.evm_db / 10.0)
            hdr, payload = framing.decode_payload(
                self.profile, modcod, r.symbols, max(noise_var, 1e-4))
            if hdr is not None:
                res.header = hdr
                res.payload = payload
            out.append(res)
        return out
