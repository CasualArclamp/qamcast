"""Symbols to real passband audio.

The output is a **real** signal, not complex IQ, because it has to survive a
soundcard, a transmitter's audio stage or a loudspeaker. That single fact is
what forces the carrier up to the middle of the audio band and caps the
occupied bandwidth at rather less than half the sample rate.

Level. ``level_dbfs`` is the RMS of the output, honestly: the baseband is
normalised by sqrt(sps) first, because upsampling by ``sps`` before a
unit-energy filter divides signal power by ``sps`` and would otherwise leave
the output 4.8 dB below whatever you asked for on WIDE.

The -15 dBFS default follows from the measured peak-to-average ratio, which is
10 dB for 256QAM at alpha 0.2 -- not the ~7 dB that QPSK would suggest, since
the constellation itself contributes. That puts peaks near -5 dBFS with room
to spare. Clipping is worse than noise here: it folds energy across the whole
constellation at once, and 256QAM has no margin to absorb it.
"""

from __future__ import annotations

import numpy as np

from . import framing, rrc
from .profiles import Modcod, Profile

DEFAULT_LEVEL_DBFS = -15.0


class Modulator:
    """Frame symbols in, passband samples out.

    Stateful on purpose. The pulse shaper carries its filter tail between
    calls and the carrier keeps its phase, so frames splice together without a
    seam. Rebuilding either per frame puts a click at every frame boundary and
    splatters the spectrum well outside the profile's band.
    """

    def __init__(self, profile: Profile, level_dbfs: float = DEFAULT_LEVEL_DBFS):
        self.profile = profile
        # sqrt(sps) undoes the power lost to upsampling, so `level_dbfs` is
        # the RMS you actually get out.
        self.level = 10.0 ** (level_dbfs / 20.0) * np.sqrt(profile.sps)
        self.shaper = rrc.StreamShaper(profile.sps, profile.rolloff)
        self._phase = 0.0
        self._dphase = 2.0 * np.pi * profile.carrier / profile.sample_rate

    def reset(self) -> None:
        self.shaper = rrc.StreamShaper(self.profile.sps, self.profile.rolloff)
        self._phase = 0.0

    def _upconvert(self, baseband: np.ndarray) -> np.ndarray:
        n = len(baseband)
        ph = self._phase + self._dphase * np.arange(n)
        self._phase = float((self._phase + self._dphase * n) % (2.0 * np.pi))
        # sqrt(2) restores the power a real passband signal loses relative to
        # its complex baseband, so `level` means the same thing either side.
        return np.sqrt(2.0) * np.real(baseband * np.exp(1j * ph))

    def modulate_symbols(self, symbols: np.ndarray) -> np.ndarray:
        return self.level * self._upconvert(self.shaper.process(symbols))

    def modulate_frame(self, modcod: Modcod, header: framing.Header,
                       payload_bytes: np.ndarray) -> np.ndarray:
        symbols = framing.build_frame(self.profile, modcod, header, payload_bytes)
        return self.modulate_symbols(symbols)

    def flush(self) -> np.ndarray:
        """Filter tail, for a clean end of transmission."""
        return self.level * self._upconvert(self.shaper.flush())


def to_int16(samples: np.ndarray) -> np.ndarray:
    """Clip and convert for a soundcard or a WAV file.

    Clipping is counted by the caller if it cares -- silently saturating a
    signal whose whole point is amplitude accuracy deserves at least a warning.
    """
    return np.clip(samples * 32767.0, -32768, 32767).astype(np.int16)


def clipping_fraction(samples: np.ndarray) -> float:
    return float(np.mean(np.abs(samples) >= 1.0))
