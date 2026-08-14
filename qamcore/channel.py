"""Channel impairments, applied on the transmit side.

Deliberately here and not in the receiver. If the receiver could inject its
own noise it would be marking its own homework -- a bug that quietly improved
the estimate would improve the measured result too. Impairing the transmitted
audio instead means the receiver under test is byte-for-byte the same one you
use on a real link, and the impairment is in the signal, where a real one
would be.

Everything is expressed the way it is measured on air:

    snr_db          Es/N0 in the occupied bandwidth
    freq_offset_hz  carrier error -- transmitter and receiver crystals disagree
    clock_ppm       sample rate error, which walks the symbol timing
    multipath       (delay_seconds, complex gain) echoes
    doppler_hz      fading rate for a Rayleigh-faded path
    phase_noise_deg oscillator jitter, RMS
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from . import interp
from .profiles import Profile


@dataclass
class ChannelConfig:
    """Impairments. All default to off, so an unconfigured channel is clean."""

    snr_db: float | None = None
    freq_offset_hz: float = 0.0
    clock_ppm: float = 0.0
    phase_noise_deg: float = 0.0
    multipath: list[tuple[float, float]] = field(default_factory=list)
    doppler_hz: float = 0.0
    seed: int | None = None

    @property
    def active(self) -> bool:
        return (
            self.snr_db is not None
            or self.freq_offset_hz
            or self.clock_ppm
            or self.phase_noise_deg
            or self.multipath
            or self.doppler_hz
        )

    def summary(self) -> str:
        if not self.active:
            return "clean"
        bits = []
        if self.snr_db is not None:
            bits.append(f"{self.snr_db:.1f} dB SNR")
        if self.freq_offset_hz:
            bits.append(f"{self.freq_offset_hz:+.1f} Hz")
        if self.clock_ppm:
            bits.append(f"{self.clock_ppm:+.1f} ppm")
        if self.phase_noise_deg:
            bits.append(f"{self.phase_noise_deg:.1f}deg jitter")
        if self.multipath:
            bits.append(f"{len(self.multipath)} echo(s)")
        if self.doppler_hz:
            bits.append(f"{self.doppler_hz:.1f} Hz Doppler")
        return ", ".join(bits)


# Named channels for the two hard cases the user is targeting. Numbers chosen
# to be representative rather than worst case -- if it works here it is worth
# trying on the real thing, not the other way round.
PRESETS: dict[str, ChannelConfig] = {
    "clean": ChannelConfig(),
    "noisy": ChannelConfig(snr_db=25.0, freq_offset_hz=2.0, clock_ppm=5.0),
    # Echo taps are (delay_seconds, gain) with a *real* gain, because in the
    # audio band that is what an echo is -- a delayed copy. Complex taps
    # belong at RF, and by the time a signal has been through a transmitter
    # and a receiver the residual shows up here as real delay spread.
    "radio": ChannelConfig(
        snr_db=20.0, freq_offset_hz=15.0, clock_ppm=20.0, phase_noise_deg=1.5,
        multipath=[(0.0, 1.0), (120e-6, -0.35)],
    ),
    # A hard surface close by -- desk, wall, laptop lid. Delay spread here is
    # 1.1 ms, inside the +-1.5 ms the 25-tap equaliser can correct at 8 kBd.
    "acoustic": ChannelConfig(
        snr_db=22.0, freq_offset_hz=3.0, clock_ppm=50.0, phase_noise_deg=2.0,
        multipath=[(0.0, 1.0), (1.1e-3, 0.45)],
        doppler_hz=1.0,
    ),
    # A real room, and deliberately kept as a preset that **fails**. Echoes at
    # 2.5 and 6 ms are 20 and 48 symbols out at 8 kBd, well past what an
    # equaliser trained on a 64-symbol preamble can reach. It is here so the
    # limit can be measured rather than assumed, and so nobody concludes from
    # the passing presets that room acoustics are handled. They are not: this
    # is the case single-carrier QAM is genuinely bad at, and the reason real
    # broadcast systems use multicarrier.
    "reverb": ChannelConfig(
        snr_db=22.0, freq_offset_hz=3.0, clock_ppm=50.0, phase_noise_deg=2.0,
        multipath=[(0.0, 1.0), (2.5e-3, 0.5), (6.0e-3, -0.25)],
        doppler_hz=1.0,
    ),
}


class Channel:
    """Applies a :class:`ChannelConfig` to a stream of passband samples.

    Stateful so it can be driven a frame at a time without discontinuities at
    the joins -- the filter memory, the offset phase and the fading process
    all carry forward.
    """

    def __init__(self, profile: Profile, config: ChannelConfig):
        self.profile = profile
        self.config = config
        self.fs = profile.sample_rate
        self.rng = np.random.default_rng(config.seed)
        self._taps, self._delays = self._build_taps()
        self._hist = np.zeros(max(self._delays) + 1 if len(self._delays) else 1)
        self._off_phase = 0.0
        self._fade_phase = self.rng.uniform(0, 2 * np.pi, size=max(1, len(self._taps)))
        # Start clear of the interpolator's left wing so the very first
        # block has history to work with.
        self._resample_pos = float(interp.LEFT)
        self._resid = np.zeros(0)
        self._hilb = StreamHilbert()

    def _build_taps(self) -> tuple[np.ndarray, np.ndarray]:
        if not self.config.multipath:
            return np.array([1.0]), np.array([0])
        delays = np.array([int(round(d * self.fs)) for d, _ in self.config.multipath])
        gains = np.array([float(g) for _, g in self.config.multipath])
        return gains, delays

    def _apply_multipath(self, x: np.ndarray) -> np.ndarray:
        if len(self._taps) == 1 and self._delays[0] == 0:
            return x * self._taps[0]
        keep = int(max(self._delays))
        buf = np.concatenate([self._hist, x])
        out = np.zeros(len(x))
        for i, (g, d) in enumerate(zip(self._taps, self._delays)):
            gain = np.full(len(x), g)
            if self.config.doppler_hz and i > 0:
                # Slow fade on the echoes so the equaliser has something that
                # actually moves, rather than a static channel it can solve
                # once and coast on.
                step = 2 * np.pi * self.config.doppler_hz / self.fs
                ph = self._fade_phase[i] + step * np.arange(len(x))
                self._fade_phase[i] = float((ph[-1] + step) % (2 * np.pi))
                gain = g * (0.6 + 0.4 * np.cos(ph))
            start = keep - d
            out += gain * buf[start:start + len(x)]
        self._hist = buf[-keep:] if keep else np.zeros(1)
        return out

    def _apply_clock(self, x: np.ndarray) -> np.ndarray:
        """Resample by (1 + ppm/1e6) to walk the symbol timing."""
        if not self.config.clock_ppm:
            return x
        ratio = 1.0 + self.config.clock_ppm * 1e-6
        x = np.concatenate([self._resid, x])
        if len(x) < interp.TAPS + 2:
            self._resid = x
            return np.zeros(0)
        # Leave the last sample unconsumed so the interpolation always has a
        # right-hand neighbour; it carries over as residue to the next block.
        n_out = int(np.floor((len(x) - 1 - interp.RIGHT - self._resample_pos) / ratio))
        if n_out <= 0:
            self._resid = x
            return np.zeros(0)
        idx = self._resample_pos + ratio * np.arange(n_out)
        # Windowed sinc, not linear. Linear interpolation here is a -20 dB
        # impairment that varies with fractional position across the block,
        # and it looks for all the world like a receiver defect: EVM ramping
        # across every frame, pilot amplitudes wandering, timing verified fine.
        keep_from = max(0, int(np.floor(idx[0])) - interp.LEFT)
        out = interp.sample_at(x, idx)
        nxt = idx[-1] + ratio
        consumed = max(keep_from, int(np.floor(nxt)) - interp.LEFT)
        self._resample_pos = nxt - consumed
        self._resid = x[consumed:]
        return out

    def process(self, x: np.ndarray) -> np.ndarray:
        cfg = self.config
        if not cfg.active:
            return x
        y = self._apply_multipath(np.asarray(x, dtype=np.float64))

        if cfg.freq_offset_hz or cfg.phase_noise_deg:
            n = len(y)
            ph = self._off_phase + 2 * np.pi * cfg.freq_offset_hz / self.fs * np.arange(n)
            self._off_phase = float(
                (self._off_phase + 2 * np.pi * cfg.freq_offset_hz / self.fs * n) % (2 * np.pi)
            )
            if cfg.phase_noise_deg:
                ph = ph + np.deg2rad(cfg.phase_noise_deg) * self.rng.standard_normal(n)
            # Rotating a real signal means shifting it, which needs its
            # analytic form; a plain multiply would just amplitude-modulate.
            analytic = self._hilb.process(y)
            y = np.real(analytic * np.exp(1j * ph))

        y = self._apply_clock(y)

        if cfg.snr_db is not None and len(y):
            # Es/N0 is defined in the occupied bandwidth, so the noise power
            # is scaled by the fraction of Nyquist the signal actually uses.
            # Quoting SNR in the full soundcard bandwidth instead would
            # flatter the link by 4 dB on WIDE and nearly 8 dB on ACOUSTIC.
            sig_power = float(np.mean(y ** 2))
            frac = self.profile.bandwidth / (self.profile.sample_rate / 2.0)
            noise_power = sig_power / (10.0 ** (cfg.snr_db / 10.0)) / frac
            y = y + np.sqrt(noise_power) * self.rng.standard_normal(len(y))
        return y


class StreamHilbert:
    """Analytic signal from a real one, with state across blocks.

    An FFT Hilbert (the ``scipy.signal.hilbert`` approach) is exact for one
    isolated block and wrong at both of its edges, because the transform is
    circular and the true impulse response is not short. Driving the channel a
    frame at a time then stamps that edge error into the signal once per
    frame, which downstream looks like a periodic burst of EVM that no amount
    of equalisation removes.

    An odd-length FIR Hilbert with a proper delay line has no such seam. The
    in-phase arm is delayed by the filter's group delay so the two arms stay
    aligned.
    """

    def __init__(self, taps: int = 127):
        if taps % 2 == 0:
            taps += 1
        self.n = taps
        self.delay = taps // 2
        k = np.arange(taps) - self.delay
        h = np.zeros(taps)
        odd = k % 2 != 0
        h[odd] = 2.0 / (np.pi * k[odd])
        self.h = h * np.hamming(taps)
        self._tail_i = np.zeros(self.delay)
        self._tail_q = np.zeros(taps - 1)

    def process(self, x: np.ndarray) -> np.ndarray:
        xi = np.concatenate([self._tail_i, x])
        self._tail_i = xi[-self.delay:] if self.delay else np.zeros(0)
        inphase = xi[:len(x)]

        xq = np.concatenate([self._tail_q, x])
        self._tail_q = xq[-(self.n - 1):]
        quad = np.convolve(xq, self.h, mode="valid")[:len(x)]
        return inphase + 1j * quad
