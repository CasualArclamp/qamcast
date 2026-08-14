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

from . import framing

# Subcarriers carrying a known reference inside the data symbols, for tracking
# phase drift between preambles. One in twelve costs 8% and is what keeps
# 256QAM standing up across a frame.
PILOT_EVERY = 12


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

    @functools.cached_property
    def pilot_bins(self) -> np.ndarray:
        """Subcarriers carrying a reference inside a data symbol."""
        offsets = np.arange(0, self.carriers, PILOT_EVERY)
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
        return (f"{self.fft}-point, {self.carriers} carriers "
                f"({self.bin_spacing:.1f} Hz apart), CP {self.cp} "
                f"= {self.max_delay_spread*1000:.2f} ms, "
                f"{lo/1000:.1f}-{hi/1000:.1f} kHz, "
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


@functools.lru_cache(maxsize=None)
def _preamble_time(geo: OfdmGeometry) -> np.ndarray:
    """The preamble symbol as transmitted, prefix included."""
    spec = np.zeros(geo.fft // 2 + 1, dtype=complex)
    spec[geo.bin_lo:geo.bin_hi + 1] = reference(geo.carriers)
    return _to_time(spec, geo)


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

def _pilot_values(geo: OfdmGeometry, index: int) -> np.ndarray:
    """References carried on the pilot subcarriers of data symbol ``index``.

    Drawn from the same Zadoff-Chu sequence as the preamble, rotated per
    symbol so a run of data symbols never puts an identical waveform on the
    wire twice -- repeated symbols would add coherently and put a line in the
    spectrum.
    """
    ref = reference(len(geo.pilot_bins), seed=1)
    return ref * np.exp(2j * np.pi * (index + 1) * np.arange(len(ref)) / len(ref))


def _modcod_spectrum(geo: OfdmGeometry, modcod_index: int) -> np.ndarray:
    """The MODCOD codeword spread across every occupied subcarrier.

    The codeword is 64 symbols and there are more subcarriers than that, so it
    is tiled. The receiver averages the repeats before correlating, which is
    free processing gain on the one field everything else waits for -- the
    payload cannot be demapped until the MODCOD is known.
    """
    word = framing.encode_modcod(modcod_index)
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
SYNC_THRESHOLD = 0.35
MODCOD_MARGIN = 0.25


@dataclass
class OfdmResult:
    """One demodulated frame."""

    symbols: np.ndarray                  # equalised payload, for the scope
    modcod_index: int | None = None
    modcod_margin: float = 0.0
    evm_db: float = 0.0
    corr_peak: float = 0.0
    timing_offset: int = 0

    @property
    def locked(self) -> bool:
        return self.modcod_index is not None


@dataclass
class OfdmStats:
    frames_seen: int = 0
    modcod_ok: int = 0
    modcod_failed: int = 0
    resyncs: int = 0


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
        self._ref_norm = self._ref / np.linalg.norm(self._ref)
        self._known = reference(geo.carriers)
        self._chan: np.ndarray | None = None

    def feed(self, samples: np.ndarray) -> None:
        self._buf = np.concatenate([self._buf, np.asarray(samples, dtype=float)])

    def _metric(self, lo: int, hi: int) -> tuple[int, float] | None:
        """Normalised preamble correlation over an absolute sample range.

        Normalised by the local energy so the threshold means the same thing
        at any level -- there is no AGC ahead of this.
        """
        n = len(self._ref)
        lo = max(lo, self._start)
        hi = min(hi, self._start + len(self._buf) - n)
        if hi <= lo:
            return None
        a, b = lo - self._start, hi - self._start + n
        seg = self._buf[a:b]
        corr = np.correlate(seg, self._ref_norm, mode="valid")
        energy = np.sqrt(np.convolve(seg ** 2, np.ones(n), mode="valid"))
        metric = np.abs(corr) / np.maximum(energy, 1e-12)
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
                if found is None or found[1] < SYNC_THRESHOLD:
                    if end - self._start > 2 * geo.frame_samples:
                        self._trim(self._start + geo.frame_samples)
                    break
                self._next = found[0]
                continue

            # Enough slack for a hundred ppm of clock error over a frame, and
            # for the odd sample a resampler adds or drops at startup.
            slack = max(16, int(geo.frame_samples * 2e-3))
            if end < self._next + geo.frame_samples + slack:
                break
            found = self._metric(self._next - slack, self._next + slack)
            if found is None:
                break
            start, peak = found
            if peak < SYNC_THRESHOLD:
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
            self._trim(start + geo.frame_samples)
        return out

    def _trim(self, absolute: int) -> None:
        """Drop everything before an absolute sample index."""
        cut = absolute - self._start
        if cut > 0:
            self._buf = self._buf[cut:]
            self._start += cut

    def _demod(self, frame: np.ndarray, peak: float) -> OfdmResult:
        geo = self.geo
        self.stats.frames_seen += 1
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
        word = framing.encode_modcod(0)
        reps = len(rx_mc) // len(word)
        if reps >= 1:
            # Fold the tiled repeats before correlating -- free processing
            # gain on the field everything else waits for.
            folded = rx_mc[:reps * len(word)].reshape(reps, len(word)).mean(axis=0)
        else:
            folded = rx_mc
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
        payload = np.empty((geo.symbols_per_frame, geo.data_carriers), complex)
        for i in range(geo.symbols_per_frame):
            eq = _from_time(sym[i + 2], geo)[bins] / chan
            # Residual phase drift between preambles, from the pilots. A
            # common rotation on all subcarriers is what a small timing or
            # frequency error looks like here.
            err = eq[pilot_at] * np.conj(_pilot_values(geo, i))
            rot = np.exp(-1j * np.angle(np.sum(err)))
            payload[i] = eq[data_at] * rot
        result.symbols = payload.ravel()
        return result
