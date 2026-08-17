"""Channel profiles and the MODCOD ladder.

This module *is* the wire format's rate plan. Both ends compute every timing
and capacity number from the tables here, so a change to one constant changes
both ends at once. Nothing in this file may be duplicated elsewhere.

Two ideas carry the whole design:

**Profile** fixes the RF footprint -- sample rate, symbol rate, roll-off and
carrier. It is chosen by hand and must match at both ends, exactly like symbol
rate / roll-off / carrier in the DMT modem. It exists because a single
occupied bandwidth cannot serve every channel: 38 kHz centred at 23 kHz sails
through a virtual audio cable, will not survive a transmitter's 15 kHz audio
stage, and will not come out of a loudspeaker at all.

**MODCOD** fixes the payload rate inside that footprint -- constellation,
convolutional rate and Reed-Solomon parity. It is carried in the frame header,
adapts freely while the link is up, and never changes the occupied bandwidth.
That is the HD Radio-like property the user asked for: the channel slot stays
put, only the robustness moves.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import interleave
# Safe at module level, unlike framing: ofdm imports nothing from this package
# at import time -- it pulls framing in inside the functions that need it,
# precisely so this direction stays open. The spacing ladder lives there,
# next to the geometry it constrains, and is re-exported here so the UI and the
# link key read both ladders from one place.
from .ofdm import OFDM_SPACING_CHOICES  # noqa: F401


# --------------------------------------------------------------------------
# MODCOD ladder
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Modcod:
    """One rung of the rate ladder.

    ``index`` is what travels in the frame header, so these values are frozen
    wire format. Appending is safe; renumbering is not.
    """

    index: int
    bits_per_symbol: int      # 2=QPSK, 4=16QAM, 6=64QAM, 8=256QAM
    conv_num: int             # convolutional rate numerator
    conv_den: int             # convolutional rate denominator
    rs_k: int                 # Reed-Solomon message bytes out of RS_N
    required_evm_db: float    # EVM the receiver must measure, from tools/thresholds.py

    @property
    def conv_rate(self) -> float:
        return self.conv_num / self.conv_den

    @property
    def rs_rate(self) -> float:
        return self.rs_k / RS_N

    def modulation_for(self, family: str = "qam") -> str:
        """What this rung's constellation is called on that point set.

        A MODCOD is a bits-per-symbol and a code rate; which point set those
        bits land on is the profile's business, not the rung's. So the name
        needs the family to be right, and "16QAM" on an APSK link is simply
        the wrong name for what is being transmitted.
        """
        try:
            return MODULATION_NAMES[family][self.bits_per_symbol]
        except KeyError:
            raise ValueError(
                f"no name for {self.bits_per_symbol}-bit symbols on {family!r}"
            ) from None

    def name_for(self, family: str = "qam") -> str:
        return f"{self.modulation_for(family)} {self.conv_num}/{self.conv_den}"

    def label_for(self, family: str = "qam") -> str:
        return f"MODCOD{self.index:02d} {self.name_for(family)}"

    # The square-QAM spellings, for the places that genuinely have no profile
    # to hand -- the CLI's argument list and the ladder printout. Anything
    # describing a live link has a profile and should use the _for forms.
    @property
    def modulation(self) -> str:
        return self.modulation_for()

    @property
    def name(self) -> str:
        return self.name_for()

    def __str__(self) -> str:
        return self.label_for()


# What each constellation size is called, per point set. Keyed by the family
# names in constellation.py, which is what ``Profile.constellation_family``
# returns -- spelled out here rather than imported so this module stays free of
# the numeric stack it would otherwise pull in at import time.
#
# The bottom rung keeps its name on both: a single ring of four points is QPSK
# however it is drawn, and the APSK construction produces exactly that at two
# bits per symbol. Above it the layouts really do differ.
MODULATION_NAMES = {
    "qam": {2: "QPSK", 4: "16QAM", 6: "64QAM", 8: "256QAM"},
    "apsk": {2: "QPSK", 4: "16APSK", 6: "64APSK", 8: "256APSK"},
}

# Both spellings back to bits per symbol, so a name arriving from a UI or a
# command line is understood whichever family produced it.
BITS_BY_MODULATION = {name: bits
                      for table in MODULATION_NAMES.values()
                      for bits, name in table.items()}

RS_N = 255  # Reed-Solomon codeword length, fixed across the ladder

# The ladder. RS(255,223) carries the rugged half -- 16 correctable bytes mops
# up the burst errors a Viterbi decoder emits when it loses the trellis. The
# top three rungs relax to RS(255,239): at 64QAM 5/6 and above you are already
# demanding a clean channel, and the 6.3% saved is what lets the top rung clear
# 192 kbps in a 32 kBaud slot rather than falling ~1.5 kbps short of it.
#
# The threshold column is **required EVM, not channel Es/N0**, and the
# distinction is not pedantic. A receiver cannot measure Es/N0 -- it has no
# reference for the noise -- it measures EVM against its own decisions. The two
# differ by about 6 dB here (matched-filter noise bandwidth, roll-off, pilot
# overhead), so a table in Es/N0 makes auto-probe wrong by 6 dB in the
# optimistic direction. These are measured by tools/thresholds.py.
#
# These are monotonic, which they did not use to be. The old signalling put
# every field in a QPSK rate-1/2 header behind nothing but a CRC, which made
# the header weaker than the payload it described and pinned MODCOD 0-4 to a
# meaningless 6.5-7.9 dB jumble -- the header failed before their payload
# would have, so the rugged rungs bought throughput rather than robustness.
# Moving MODCOD into a BPSK codeword and the rest inside the payload removed
# that floor; QPSK 1/4 now measures 5.4 dB and each rung costs what it should.
MODCODS: tuple[Modcod, ...] = (
    Modcod(0, 2, 1, 4, 223,  5.4),
    Modcod(1, 2, 1, 3, 223,  5.9),
    Modcod(2, 2, 1, 2, 223,  6.4),
    Modcod(3, 2, 2, 3, 223,  6.9),
    Modcod(4, 2, 3, 4, 223,  8.7),
    Modcod(5, 4, 1, 2, 223, 11.5),
    Modcod(6, 4, 2, 3, 223, 12.1),
    Modcod(7, 4, 3, 4, 223, 13.4),
    Modcod(8, 6, 2, 3, 223, 17.8),
    Modcod(9, 6, 3, 4, 223, 20.6),
    Modcod(10, 6, 5, 6, 239, 19.5),
    Modcod(11, 8, 3, 4, 239, 25.0),
    Modcod(12, 8, 5, 6, 239, 24.9),
)

# The ladder above is the curated one -- the rungs worth climbing in order,
# each a sensible step in throughput for a step in required SNR. Manual
# operation wants the other combinations too, so the rest of the 4 x 6 grid is
# appended here rather than replacing anything: index is wire format, so
# adding is safe and renumbering is not.
#
# These are measured too, by tools/thresholds.py, on the same run as the
# ladder above.
_EXTRA = (
    (13, 2, 5, 6, 223,  8.4),
    (14, 4, 1, 4, 223, 10.6),
    (15, 4, 1, 3, 223, 10.2),
    (16, 4, 5, 6, 239, 13.5),
    (17, 6, 1, 4, 223, 15.5),
    (18, 6, 1, 3, 223, 15.8),
    (19, 6, 1, 2, 223, 17.0),
    (20, 8, 1, 4, 223, 21.2),
    (21, 8, 1, 3, 223, 22.1),
    (22, 8, 1, 2, 223, 22.9),
    (23, 8, 2, 3, 239, 24.1),
)
MODCODS = MODCODS + tuple(
    Modcod(i, bps, num, den, k, evm) for i, bps, num, den, k, evm in _EXTRA
)

MODCOD_BY_INDEX = {m.index: m for m in MODCODS}

# The curated subset, in ladder order, for anything that wants "the next rung
# up" rather than an arbitrary grid point.
LADDER = tuple(m for m in MODCODS if m.index <= 12)

CONSTELLATIONS = (2, 4, 6, 8)
CODE_RATES = ((1, 4), (1, 3), (1, 2), (2, 3), (3, 4), (5, 6))


def find_modcod(bits_per_symbol: int, num: int, den: int) -> Modcod:
    """The MODCOD for an explicit modulation and code rate."""
    for m in MODCODS:
        if m.bits_per_symbol == bits_per_symbol and m.conv_num == num and m.conv_den == den:
            return m
    raise ValueError(
        f"no MODCOD for {bits_per_symbol}-bit symbols at rate {num}/{den}"
    )

# Header is sent at a fixed, rugged setting because the receiver must read it
# *before* it knows which MODCOD the payload uses. QPSK rate-1/2, no outer code
# -- it is 8 bytes with its own CRC, and a header that fails CRC just costs one
# frame.
HEADER_BITS_PER_SYMBOL = 2
HEADER_CONV_NUM = 1
HEADER_CONV_DEN = 2


# --------------------------------------------------------------------------
# Channel profiles
# --------------------------------------------------------------------------

# Frame layout constants, shared by every profile.
PREAMBLE_SYMBOLS = 64   # Zadoff-Chu, length 64
HEADER_SYMBOLS = 64     # QPSK rate-1/2 -> 64 bits -> 8 bytes

# The PAD side channel and the transport packet headers come out of the same
# budget as the audio. Reserving it here means modcod_for_bitrate never hands
# back a rung that fits the codec exactly and leaves no room for the metadata
# or the length prefixes. Roughly: 800 bps of transport overhead at 50 audio
# packets per second, plus 400 bps of PAD, which resends a full metadata set
# about twice a second.
PAD_RESERVE_BPS = 1200


@dataclass(frozen=True)
class FrameCapacity:
    """What one frame actually carries, after every overhead is paid."""

    channel_bits: int      # bits the data symbols can hold
    info_bits: int         # bits surviving the convolutional rate
    frame_bytes: int       # bytes of interleaved RS-coded stream per frame
    frame_duration: float  # seconds
    rs_rate: float         # k/255 for this MODCOD
    stuffing_bits: int     # lost rounding info_bits down to whole bytes

    @property
    def coded_byte_rate(self) -> float:
        """Bytes per second of RS-coded stream -- what the interleaver sees."""
        return self.frame_bytes / self.frame_duration

    @property
    def net_bitrate(self) -> float:
        """Transport bits per second, after RS parity is removed."""
        return self.frame_bytes * 8 * self.rs_rate / self.frame_duration

    @property
    def stuffing_fraction(self) -> float:
        return self.stuffing_bits / self.info_bits if self.info_bits else 0.0


@dataclass(frozen=True)
class Profile:
    """A fixed RF footprint plus its frame geometry.

    ``sps`` is deliberately an integer at the transmitter -- it makes pulse
    shaping a plain polyphase upsample. The receiver resamples to its own
    working rate during timing recovery and does not rely on it.
    """

    name: str
    sample_rate: int          # soundcard Fs, Hz
    symbol_rate: int          # Bd
    rolloff: float            # RRC alpha
    carrier: float            # Hz, centre of the occupied band
    frame_symbols: int        # total symbols per frame, including overhead
    pilot_spacing: int        # one pilot every N symbols in the payload region
    description: str

    # OFDM profiles. The geometry is carried as plain numbers rather than an
    # OfdmGeometry so this module need not import ofdm, which imports framing,
    # which imports this one. Built on demand by `geometry` below.
    # "sc"   single carrier, square QAM
    # "apsk" single carrier, amplitude-and-phase rings -- same everything else
    # "ofdm" multicarrier, square QAM
    #
    # APSK is offered on single carrier only, and that is not an omission. Its
    # whole purpose is a lower peak-to-average ratio into a compressed
    # amplifier; an OFDM signal is the sum of hundreds of subcarriers and is
    # Gaussian whatever the subcarriers carry, so choosing the constellation
    # buys nothing there.
    mode: str = "sc"
    ofdm_fft: int = 0
    ofdm_cp: int = 0
    ofdm_bin_lo: int = 0
    ofdm_bin_hi: int = 0
    ofdm_symbols: int = 0     # data symbols per frame
    # The band this OFDM profile was fitted into, which is *not* the band it
    # reports: `band` gives the bins actually driven, and those sit inside the
    # target by up to a subcarrier. Re-fitting a different carrier count
    # against the reported band would walk the signal inwards a little more
    # every time, so the target is carried instead of recomputed.
    ofdm_band_lo: float = 0.0
    ofdm_band_hi: float = 0.0
    ofdm_cp_fraction: int = 0
    # Nominal subcarrier spacing, Hz, when this profile is one whose count sets
    # the bandwidth. Zero means the older reading, where the band is fixed and
    # the count divides it -- see make_ofdm_profile. Carried rather than
    # derived from ofdm_fft because it is the *nominal* figure both ends round
    # from, and rounding back out of the transform size would not always land
    # on the same choice.
    ofdm_spacing: float = 0.0
    # The single-carrier profile the target band came from. A label only --
    # nothing reconstructs geometry from it, so a profile built from a link key
    # can leave it empty.
    ofdm_base: str = ""

    # ---- geometry -------------------------------------------------------

    @property
    def sps(self) -> int:
        """Samples per symbol. Integer by construction for every profile."""
        q, r = divmod(self.sample_rate, self.symbol_rate)
        if r:
            raise ValueError(
                f"profile {self.name}: sample_rate {self.sample_rate} is not an "
                f"integer multiple of symbol_rate {self.symbol_rate}"
            )
        return q

    @property
    def is_ofdm(self) -> bool:
        return self.mode == "ofdm"

    @property
    def is_apsk(self) -> bool:
        return self.mode == "apsk"

    @property
    def constellation_family(self) -> str:
        """Which point set the MODCOD's bits-per-symbol lands on."""
        from .constellation import APSK, QAM
        return APSK if self.mode == "apsk" else QAM

    @property
    def ofdm_carriers(self) -> int:
        """Subcarriers this profile drives. OFDM profiles only."""
        return self.ofdm_bin_hi - self.ofdm_bin_lo + 1 if self.is_ofdm else 0

    @property
    def family(self) -> str:
        """The profile name with any '-count@spacing' suffix stripped."""
        if not self.is_ofdm:
            return self.name
        return self.name.split("@", 1)[0].rsplit("-", 1)[0]

    @property
    def geometry(self):
        """The OFDM geometry this profile describes. OFDM profiles only."""
        if not self.is_ofdm:
            raise ValueError(f"profile {self.name} is not an OFDM profile")
        from .ofdm import OfdmGeometry
        return OfdmGeometry(
            sample_rate=self.sample_rate, fft=self.ofdm_fft, cp=self.ofdm_cp,
            bin_lo=self.ofdm_bin_lo, bin_hi=self.ofdm_bin_hi,
            symbols_per_frame=self.ofdm_symbols,
        )

    @property
    def bandwidth(self) -> float:
        """Occupied bandwidth, Hz."""
        if self.is_ofdm:
            return self.geometry.bandwidth
        return self.symbol_rate * (1.0 + self.rolloff)

    @property
    def band(self) -> tuple[float, float]:
        """(low, high) edges of the occupied band, Hz."""
        if self.is_ofdm:
            # Exact for OFDM: these bins are driven and no others.
            return self.geometry.band
        half = self.bandwidth / 2.0
        return (self.carrier - half, self.carrier + half)

    @property
    def frame_duration(self) -> float:
        """Seconds per frame -- also the worst-case acquisition granularity."""
        if self.is_ofdm:
            return self.geometry.frame_duration
        return self.frame_symbols / self.symbol_rate

    # ---- frame layout ---------------------------------------------------
    #
    # preamble | header | [ pilot, data x (pilot_spacing-1) ] repeated
    #
    # The payload region is a whole number of pilot groups, so the frame
    # divides exactly and neither end has to special-case a ragged tail.

    @property
    def payload_region(self) -> int:
        return self.frame_symbols - PREAMBLE_SYMBOLS - HEADER_SYMBOLS

    @property
    def pilot_groups(self) -> int:
        groups, r = divmod(self.payload_region, self.pilot_spacing)
        if r:
            raise ValueError(
                f"profile {self.name}: payload region {self.payload_region} is not "
                f"a multiple of pilot spacing {self.pilot_spacing}"
            )
        return groups

    @property
    def data_symbols(self) -> int:
        """Symbols per frame that actually carry payload.

        The one number capacity() needs, and the reason an OFDM profile needs
        no special case anywhere downstream: the FEC, the interleaver and the
        transport chain all size themselves off this.
        """
        if self.is_ofdm:
            return self.geometry.payload_symbols
        return self.pilot_groups * (self.pilot_spacing - 1)

    @property
    def pilot_symbols(self) -> int:
        return self.pilot_groups

    @property
    def data_fraction(self) -> float:
        """Share of the frame that is payload. The rest is preamble, header
        and pilots -- the price of being able to join a broadcast mid-stream."""
        return self.data_symbols / self.frame_symbols

    # ---- capacity -------------------------------------------------------

    def gross_bits_per_frame(self, modcod: Modcod) -> int:
        """Channel bits carried by one frame's data symbols."""
        return self.data_symbols * modcod.bits_per_symbol

    def capacity(self, modcod: Modcod) -> FrameCapacity:
        """Exact per-frame budget, counting every byte actually spent.

        RS codewords are deliberately **allowed to straddle frame
        boundaries**. Rounding each frame down to a whole codeword looked
        tidier and cost 49% of the payload at the bottom of the ladder, where
        a frame holds barely two codewords' worth of bytes to begin with.

        Nothing is lost by letting them straddle. The receiver never has to
        search for codeword alignment, because the header states the RS and
        interleaver phases outright -- see framing.py. Carrying the phase
        costs 16 bits per frame and removes any dependence on frame counter
        arithmetic, counter wraps, or where the receiver happened to join.
        """
        from . import conv

        from . import framing

        channel_bits = self.gross_bits_per_frame(modcod)
        info_bits = conv.max_info_bits(channel_bits, modcod.conv_num, modcod.conv_den)
        # The signalling block sits inside the information bits, ahead of the
        # payload, so the frame carries that many fewer payload bytes.
        frame_bytes = info_bits // 8 - framing.SIGNALLING_BYTES
        if frame_bytes < 1:
            raise ValueError(f"{self.name}/{modcod} carries no whole bytes per frame")
        return FrameCapacity(
            channel_bits=channel_bits,
            info_bits=info_bits,
            frame_bytes=frame_bytes,
            frame_duration=self.frame_duration,
            rs_rate=modcod.rs_rate,
            stuffing_bits=info_bits - frame_bytes * 8,
        )

    def net_bitrate(self, modcod: Modcod) -> float:
        """Payload bits per second after both codes, stuffing included.

        This is the real number -- what the codec, the transport headers and
        the PAD channel actually have to share.
        """
        return self.capacity(modcod).net_bitrate

    def usable_bitrates(self) -> list[tuple[Modcod, float]]:
        return [(m, self.net_bitrate(m)) for m in MODCODS]

    # ---- selection ------------------------------------------------------

    def modcod_for_bitrate(self, bitrate: int, reserve: int = PAD_RESERVE_BPS) -> Modcod:
        """Lowest rung that carries ``bitrate`` plus the PAD reserve.

        Lowest rather than highest on purpose: every extra rung costs SNR, so
        the cheapest MODCOD that fits is always the most robust choice.
        """
        need = bitrate + reserve
        for m in MODCODS:
            if self.net_bitrate(m) >= need:
                return m
        raise ValueError(
            f"profile {self.name} tops out at "
            f"{self.net_bitrate(MODCODS[-1]) - reserve:.0f} bps; {bitrate} bps requested"
        )

    def modcod_for_evm(self, evm_db: float, margin_db: float = 2.0) -> Modcod | None:
        """Highest rung the measured EVM supports. The auto-probe decision.

        Takes EVM because that is what a receiver can actually measure, and
        compares it against a column measured in the same units -- see the note
        on the ladder. ``margin_db`` is fade headroom; a link sitting exactly on
        its threshold is a link that breaks up.
        """
        best = None
        for m in MODCODS:
            if m.required_evm_db + margin_db <= evm_db:
                best = m
        return best

    def validate(self) -> None:
        """Fail loudly at import if a profile is geometrically impossible."""
        _ = self.sps, self.pilot_groups
        low, high = self.band
        nyquist = self.sample_rate / 2.0
        if low <= 0:
            raise ValueError(
                f"profile {self.name}: band starts at {low:.0f} Hz -- the signal "
                f"would wrap around DC. Raise the carrier or lower the symbol rate."
            )
        if high >= nyquist:
            raise ValueError(
                f"profile {self.name}: band ends at {high:.0f} Hz, at or above the "
                f"{nyquist:.0f} Hz Nyquist limit. It would alias."
            )


PROFILES: dict[str, Profile] = {
    # Virtual audio cable or a line-out to line-in cord. Near-ideal and flat,
    # so the roll-off is tight and the constellation can climb to 256QAM. This
    # is the only profile that reaches 192 kbps.
    "WIDE": Profile(
        name="WIDE",
        sample_rate=96000,
        symbol_rate=32000,
        rolloff=0.20,
        carrier=23000.0,
        frame_symbols=8192,
        pilot_spacing=128,
        description="96 kHz soundcard, virtual cable or line loopback",
    ),
    # Through a transmitter and receiver. Sits under the 15 kHz ceiling of a
    # broadcast audio stage with room to spare, and pilots are four times
    # denser than WIDE because this path drifts and fades.
    "RADIO": Profile(
        name="RADIO",
        sample_rate=48000,
        symbol_rate=9600,
        rolloff=0.25,
        carrier=7000.0,
        frame_symbols=4096,
        pilot_spacing=64,
        description="analog radio link, fits a 15 kHz audio path",
    ),
    # Loudspeaker to microphone. Kept clear of room rumble at the bottom and
    # of consumer speaker roll-off at the top, with the densest pilots of the
    # three -- an acoustic path is the most aggressive multipath here.
    "ACOUSTIC": Profile(
        name="ACOUSTIC",
        sample_rate=48000,
        symbol_rate=8000,
        rolloff=0.30,
        carrier=8000.0,
        frame_symbols=4096,
        pilot_spacing=32,
        description="loudspeaker to microphone, heavy multipath",
    ),
}

# --------------------------------------------------------------------------
# Ordinary sound cards
# --------------------------------------------------------------------------
#
# WIDE needs a 96 kHz card, and plenty of hardware -- and plenty of Windows
# configurations -- will only offer 48 kHz or 44.1 kHz. A 44.1 kHz-only card
# could not run *any* of the profiles above, because every one of them is
# defined at 48 or 96 kHz and the sample rate is not something the receiver
# can adapt to: it is the clock the whole frame geometry is measured against.
#
# Symbol rates here are not free choices. Samples-per-symbol has to be an
# integer, so the card's rate factorises the options:
#
#     48000 / 16000 = 3      44100 / 14700 = 3
#                            44100 /  8820 = 5
#                            44100 /  7350 = 6
#
# The ceiling drops with the card. At 48 kHz the usable band is about 19 kHz
# against 24 kHz of Nyquist, which is 16 kBd and roughly 96 kbps at the top of
# the ladder -- half of what WIDE manages. That is the cost of ordinary
# hardware, and it is a hard limit rather than a tuning problem.
#
# Roll-off is 0.20 rather than 0.25 on the two wide profiles so the upper band
# edge lands near 20 kHz instead of 21. A virtual cable would not care -- it is
# pure digital with no filter at all -- but a real card doing line-out to
# line-in has an anti-alias filter that is already rolling off by 21 kHz, and
# losing the top of the shaped spectrum shows up as ISI the equaliser then has
# to clean up for no reason.

PROFILES.update({
    # The 48 kHz equivalent of WIDE: same clean-cable assumption, same sparse
    # pilots, half the throughput.
    "WIDE48": Profile(
        name="WIDE48",
        sample_rate=48000,
        symbol_rate=16000,
        rolloff=0.20,
        carrier=10500.0,
        frame_symbols=4096,
        pilot_spacing=128,
        description="48 kHz soundcard, virtual cable or line loopback",
    ),
    # 44.1 kHz. Everything scales by 44100/48000, which keeps sps at 3.
    "WIDE44": Profile(
        name="WIDE44",
        sample_rate=44100,
        symbol_rate=14700,
        rolloff=0.20,
        carrier=10000.0,
        frame_symbols=4096,
        pilot_spacing=128,
        description="44.1 kHz soundcard, virtual cable or line loopback",
    ),
    # 44.1 kHz versions of the two constrained-channel profiles, so a card
    # that only does 44.1 is not shut out of them either.
    "RADIO44": Profile(
        name="RADIO44",
        sample_rate=44100,
        symbol_rate=8820,
        rolloff=0.25,
        carrier=7000.0,
        frame_symbols=4096,
        pilot_spacing=64,
        description="analog radio link on a 44.1 kHz card",
    ),
    "ACOUSTIC44": Profile(
        name="ACOUSTIC44",
        sample_rate=44100,
        symbol_rate=7350,
        rolloff=0.30,
        carrier=8000.0,
        frame_symbols=4096,
        pilot_spacing=32,
        description="loudspeaker to microphone on a 44.1 kHz card",
    ),
})

# Aliases. WIDE predates the others and is the 96 kHz one; spelling it WIDE96
# is clearer once there are three of them, so both names work.
# --------------------------------------------------------------------------
# OFDM profiles
# --------------------------------------------------------------------------
#
# Same bands as their single-carrier namesakes, so a link can be moved between
# the two without re-planning the spectrum. What changes is what happens inside
# that band: instead of one carrier equalised by inverting the channel, a few
# hundred narrow ones with a cyclic prefix that removes the echo outright.
#
# The trade, measured on WIDE48 with a -6 dB echo at 30 dB SNR: OFDM gives up
# about 12% of the payload symbol rate to the prefix and the pilots, and in
# exchange it still decodes at 1.6 ms of delay spread where the single-carrier
# equaliser cannot lock past 0.6 ms.
#
# Bin counts are computed rather than written down, so a band edge can never
# disagree with the geometry that serves it.
#
# How many carriers fill that band is the one dial the mode has, and it is a
# selectable one rather than a design-time constant. See the note in ofdm.py:
# with the band fixed, the carrier count sets the subcarrier spacing, and the
# spacing decides how much echo the prefix absorbs against how much frequency
# error the signal shrugs off. Throughput barely moves, because that is set by
# the occupied bandwidth and not by how finely it is divided.

# What the dials offer. Both ends must be set the same, exactly like the
# profile itself -- none of this travels in the header.
#
# The count ladder is finer than it used to be, and that is a consequence of
# the count meaning bandwidth. When it merely divided a fixed band, the gaps
# did not matter -- every rung occupied the same width. Now each rung *is* a
# width, so a coarse ladder leaves the band part-empty: at 200 Hz spacing on a
# 48 kHz card, the old ladder jumped 64 -> 96, which is 12.8 kHz -> 19.2 kHz
# across a 19.2 kHz band, so a third of it went unused for want of a rung.
OFDM_CARRIER_CHOICES = (24, 32, 48, 64, 80, 96, 112, 128, 160, 192, 224, 256,
                        320, 384)

# Kept to 16 so a count and a spacing share one byte in a link key.
assert len(OFDM_CARRIER_CHOICES) <= 16

OFDM_CP_CHOICES = (4, 8, 16, 32)

# The spacing a link lands on unless it is told otherwise, measured rather
# than picked. From tools/spacing.py, decoding 40 frames per spacing:
#
#   radio     0.12 ms echo, 15 Hz drift    every spacing 400-50 Hz passes
#   acoustic  1.10 ms echo,  3 Hz drift    100 Hz and narrower passes
#   reverb    6.00 ms echo,  3 Hz drift    50 Hz partial, 25 Hz passes
#
# 100 Hz is the widest that clears the acoustic channel, it holds 3x margin
# over the radio channel's drift, and it gives up 7% of the spectral
# efficiency available at the top of the range. Narrower is for a genuinely
# reverberant path and costs bandwidth, not much else; wider is for a drifting
# one with no echo to speak of.
OFDM_DEFAULT_SPACING = 100


def make_ofdm_profile(sample_rate: int, band_lo: float, band_hi: float,
                      carriers: int | None, cp_fraction: int = 8,
                      name: str = "CUSTOM", description: str = "",
                      base: str = "", template: Profile | None = None,
                      spacing_hz: float = 0.0) -> Profile:
    """An OFDM profile: a spacing, a carrier count, and a band to sit in.

    This is what a link key rebuilds into, so it must produce exactly what
    ``_ofdm``, ``with_carriers`` and ``with_spacing`` produce from the same
    inputs -- they all go through :func:`ofdm.for_spacing`, which is the single
    place a wanted spacing and count become a geometry.

    The band is a placement envelope, not a target to fill: the block is
    ``carriers * spacing`` wide and is centred in it. Asking for more than fits
    is an error rather than a signal that quietly runs past the upper edge.

    ``template`` supplies the single-carrier fields the OFDM path never reads
    -- symbol rate, roll-off, carrier, frame length, pilot spacing. They exist
    only because ``validate`` checks them, and every geometry question routes
    through ``is_ofdm`` before it gets to them. Without a template they are
    filled with values chosen to satisfy that check and nothing else.
    """
    from .ofdm import for_spacing

    # carriers of None means fill the band, which is what a custom link wants
    # and what the ladder cannot express -- see for_spacing.
    geo = for_spacing(sample_rate, spacing_hz or OFDM_DEFAULT_SPACING, carriers,
                      band_lo, band_hi, cp_fraction)
    if template is not None:
        sc = dict(symbol_rate=template.symbol_rate, rolloff=template.rolloff,
                  carrier=template.carrier, frame_symbols=template.frame_symbols,
                  pilot_spacing=template.pilot_spacing)
    else:
        sc = dict(symbol_rate=sample_rate // 4, rolloff=0.25,
                  carrier=sample_rate / 4.0, frame_symbols=4096,
                  pilot_spacing=64)
    out = Profile(
        name=name, sample_rate=sample_rate, description=description, **sc,
        mode="ofdm", ofdm_fft=geo.fft, ofdm_cp=geo.cp,
        ofdm_bin_lo=geo.bin_lo, ofdm_bin_hi=geo.bin_hi,
        ofdm_symbols=geo.symbols_per_frame,
        ofdm_band_lo=float(band_lo), ofdm_band_hi=float(band_hi),
        ofdm_cp_fraction=cp_fraction, ofdm_base=base,
        ofdm_spacing=float(spacing_hz or OFDM_DEFAULT_SPACING),
    )
    out.validate()
    return out


def _ofdm(name: str, base: str, carriers: int, cp_fraction: int,
          description: str, spacing: float = 0.0) -> Profile:
    src = PROFILES[base]
    lo, hi = src.band
    return make_ofdm_profile(src.sample_rate, lo, hi, carriers, cp_fraction,
                             name=name, description=description, base=base,
                             template=src, spacing_hz=spacing)


# Both numbers here are defaults rather than limits: each profile builds at any
# count in OFDM_CARRIER_CHOICES and any spacing in OFDM_SPACING_CHOICES.
#
# The counts are the widest rung that fits each band at the default spacing, so
# a profile starts by using the whole footprint it was given and the dial only
# ever narrows it. The spacings are the measured default, except OFDMREVERB,
# which exists to be the case the rest of this modem does not handle -- see the
# note on OFDM_DEFAULT_SPACING and tools/spacing.py.
PROFILES.update({
    "OFDM96": _ofdm("OFDM96", "WIDE", 384, 8,
                    "96 kHz card, OFDM, widest footprint"),
    "OFDM48": _ofdm("OFDM48", "WIDE48", 192, 8,
                    "48 kHz card, OFDM, full audio band"),
    "OFDM44": _ofdm("OFDM44", "WIDE44", 160, 8,
                    "44.1 kHz card, OFDM, full audio band"),
    "OFDMRADIO": _ofdm("OFDMRADIO", "RADIO", 112, 4,
                       "48 kHz card, OFDM, fits a 15 kHz transmitter stage, "
                       "long prefix for multipath"),
    # Half the bandwidth, eight times the echo tolerance. The trade the old
    # carrier dial could not express, and the only geometry here that decodes
    # a 6 ms room echo -- 38 frames in 40, against 0 for single carrier. Not a
    # default: 11 Hz of carrier-offset tolerance needs a path that does not
    # drift, so it is for a loudspeaker in a room, not a transmitter.
    "OFDMREVERB": _ofdm("OFDMREVERB", "WIDE48", 384, 8,
                        "48 kHz card, OFDM, 25 Hz spacing for a reverberant "
                        "room; needs a path that does not drift", spacing=25),
})

# --------------------------------------------------------------------------
# The default
# --------------------------------------------------------------------------
#
# What a real FM path turned out to pass, rather than what the RADIO profiles
# assume. RADIO is sized for a 15 kHz audio stage with room to spare, which is
# the safe assumption and, measured on an actual transmitter and SDR, a
# pessimistic one: the path ran clean right up to the stereo pilot. These fill
# it instead, stopping just below 19 kHz.
#
# Roll-off is 0.15 rather than 0.20, which is what buys the last kilohertz --
# at 14700 Bd it is the difference between an 18.4 kHz footprint and a 16.9 kHz
# one. A tighter roll-off is a longer pulse and more sensitive to timing, and
# on a path this clean that is affordable.

PROFILES.update({
    "FM44": Profile(
        name="FM44",
        sample_rate=44100,
        symbol_rate=14700,
        rolloff=0.15,
        carrier=8894.0,
        frame_symbols=4096,
        pilot_spacing=64,
        description="44.1 kHz card, fills an FM audio path below the 19 kHz pilot",
    ),
    "FM48": Profile(
        name="FM48",
        sample_rate=48000,
        symbol_rate=16000,
        rolloff=0.15,
        carrier=9680.0,
        frame_symbols=4096,
        pilot_spacing=64,
        description="48 kHz card, fills an FM audio path below the 19 kHz pilot",
    ),
})

# What both apps and both pages start on. Measured on a real transmitter and
# receiver rather than chosen for tidiness.
DEFAULT_PROFILE = "FM44"


def default_for_mode(mode: str) -> str:
    """The preset a page should land on when it switches to ``mode``.

    Not the first of the filtered list, which is what this used to be. The
    list is sorted by descending card rate, so the first entry is always the
    96 kHz one -- and a mode selector that drops a 44.1 kHz card onto a 96 kHz
    geometry the moment it is touched is not a cosmetic mismatch. The receiver
    then false-syncs on noise, reports a carrier offset of a few hundred hertz
    that changes every frame, and draws a constellation that visibly spins.

    So: the measured default when this mode has it, otherwise the profile on
    the same card rate, and only then whatever is left.
    """
    same = [p for p in PROFILES.values() if p.mode == mode]
    if not same:
        return "CUSTOM"
    for p in same:
        if p.name == DEFAULT_PROFILE:
            return p.name
    want = PROFILES[DEFAULT_PROFILE].sample_rate
    for p in same:
        if p.sample_rate == want:
            return p.name
    return same[0].name


# --------------------------------------------------------------------------
# APSK profiles
# --------------------------------------------------------------------------
#
# The same footprints as their single-carrier namesakes -- identical band,
# symbol rate, roll-off, frame and pilots -- with the bits laid out on rings
# instead of a square grid. Nothing about the spectrum changes, so a link moves
# between the two without re-planning anything, and the MODCOD ladder, the FEC
# and the framing are untouched.
#
# What changes is what happens to the signal in an amplifier that is being
# driven hard. See constellation.py for the construction and the README for the
# measured trade: APSK gives up a little on a linear channel and takes it back
# several times over once the amplifier is into compression.


def _apsk(name: str, base: str, description: str) -> Profile:
    src = PROFILES[base]
    return Profile(
        name=name, sample_rate=src.sample_rate, symbol_rate=src.symbol_rate,
        rolloff=src.rolloff, carrier=src.carrier,
        frame_symbols=src.frame_symbols, pilot_spacing=src.pilot_spacing,
        description=description, mode="apsk",
    )


PROFILES.update({
    "APSK96": _apsk("APSK96", "WIDE",
                    "96 kHz card, APSK, widest footprint"),
    "APSK48": _apsk("APSK48", "WIDE48",
                    "48 kHz card, APSK, full audio band"),
    "APSK44": _apsk("APSK44", "WIDE44",
                    "44.1 kHz card, APSK, full audio band"),
    "APSKRADIO": _apsk("APSKRADIO", "RADIO",
                       "48 kHz card, APSK, fits a 15 kHz transmitter stage, "
                       "for a compressed amplifier"),
})

OFDM_DEFAULT_CARRIERS = {n: p.ofdm_carriers
                         for n, p in PROFILES.items() if p.is_ofdm}


def _refit(profile: Profile, carriers: int, spacing: float) -> Profile:
    """Rebuild an OFDM profile at a different count or spacing.

    Always from the band the profile was *fitted into*, never the one it
    currently occupies -- the block sits inside that envelope by up to a
    carrier, so refitting against where it ended up would walk it inwards a
    little more on every turn of either dial.
    """
    # Both dials in the name, because both are needed to say which link this
    # is. "OFDM48-24" was enough when the count divided a fixed band; now 24
    # carriers at 400 Hz and 24 at 25 Hz are a 9.6 kHz signal and a 0.6 kHz
    # one, and a name that cannot tell them apart is a name that cannot be
    # handed to --profile at the far end.
    return make_ofdm_profile(
        profile.sample_rate, profile.ofdm_band_lo, profile.ofdm_band_hi,
        carriers, profile.ofdm_cp_fraction,
        name=f"{profile.family}-{carriers}@{spacing:g}",
        description=profile.description,
        base=profile.ofdm_base, template=profile, spacing_hz=spacing,
    )


def with_carriers(profile: Profile, carriers: int | None) -> Profile:
    """The same profile at a different number of subcarriers.

    The count is the bandwidth: this narrows or widens the signal and leaves
    the echo and carrier-offset tolerances exactly where they were.
    """
    if not profile.is_ofdm or carriers is None:
        return profile
    carriers = int(carriers)
    if carriers == profile.ofdm_carriers:
        return profile
    if carriers not in OFDM_CARRIER_CHOICES:
        raise ValueError(
            f"{carriers} subcarriers is not one of "
            f"{', '.join(str(c) for c in OFDM_CARRIER_CHOICES)}"
        )
    return _refit(profile, carriers, profile.ofdm_spacing)


def with_spacing(profile: Profile, spacing: float | None) -> Profile:
    """The same profile at a different subcarrier spacing.

    The spacing is the ruggedness: this moves the echo and carrier-offset
    tolerances and leaves the carrier count alone -- so the occupied bandwidth
    moves with it, because the count is a count and not a width.

    If the band cannot hold the count at the new spacing, the count comes down
    to the widest rung that fits rather than the call failing. Widening the
    spacing is a deliberate request for a more rugged link, and refusing it
    because the signal would not fit the old footprint would be answering a
    question nobody asked.
    """
    if not profile.is_ofdm or spacing is None:
        return profile
    spacing = float(spacing)
    if spacing == profile.ofdm_spacing:
        return profile
    if spacing not in OFDM_SPACING_CHOICES:
        raise ValueError(
            f"{spacing:g} Hz spacing is not one of "
            f"{', '.join(str(c) for c in OFDM_SPACING_CHOICES)} Hz"
        )
    want = profile.ofdm_carriers
    for carriers in sorted(OFDM_CARRIER_CHOICES, reverse=True):
        if carriers > want:
            continue
        try:
            return _refit(profile, carriers, spacing)
        except ValueError:
            continue
    raise ValueError(
        f"{spacing:g} Hz spacing does not fit "
        f"{profile.ofdm_band_lo:.0f}-{profile.ofdm_band_hi:.0f} Hz at any "
        f"carrier count"
    )

ALIASES = {"WIDE96": "WIDE", "STANDARD": "WIDE48"}

for _p in PROFILES.values():
    _p.validate()


def get_profile(name: str) -> Profile:
    key = name.upper()
    key = ALIASES.get(key, key)
    if key in PROFILES:
        return PROFILES[key]
    # "OFDM48-64@100" -- an OFDM profile at a chosen carrier count and spacing.
    # Both are in the name so it still describes the link once either dial has
    # moved, and so it round-trips: whatever the status panel shows can be
    # handed straight back to --profile at the far end. The spacing may be
    # omitted, in which case the base profile's own is kept.
    body, _, spacing = key.partition("@")
    head, sep, tail = body.rpartition("-")
    if sep and tail.isdigit() and head in PROFILES and PROFILES[head].is_ofdm:
        p = PROFILES[head]
        if spacing:
            try:
                p = with_spacing(p, float(spacing))
            except ValueError as exc:
                raise ValueError(f"profile {name!r}: {exc}") from None
        return with_carriers(p, int(tail))
    raise ValueError(
        f"unknown profile {name!r}; expected one of {', '.join(PROFILES)}"
    )


def profiles_for_rate(sample_rate: int) -> list[Profile]:
    """Profiles a card running at ``sample_rate`` can actually carry."""
    return [p for p in PROFILES.values() if p.sample_rate == sample_rate]


# --------------------------------------------------------------------------
# Building a profile by hand
# --------------------------------------------------------------------------
#
# The named profiles are presets. Everything they contain can be set directly
# instead -- symbol rate, roll-off and carrier -- which is how the DMT modem
# is operated and is the more honest interface once you know what the knobs
# do. The only rule is the same one the presets obey: **both ends must be set
# identically**, because none of it travels in the header.

MIN_SPS = 3     # below this the RRC has too few samples per symbol to shape
MAX_SPS = 16    # above this you are wasting bandwidth on a slow link
FRAME_TARGET_SECONDS = 0.25
FRAME_CHOICES = (2048, 4096, 8192)
PILOT_CHOICES = (32, 64, 128)
# The roll-offs a link key can express -- it carries alpha x 100 in a byte, so
# the constraint is really only that they are whole percents, but a short list
# is what the presets use and what a band fit should choose between.
ROLLOFF_CHOICES = (0.15, 0.20, 0.25, 0.30, 0.35)


def valid_symbol_rates(sample_rate: int) -> list[int]:
    """Symbol rates a given card rate can carry, fastest first.

    Samples per symbol has to be a whole number, so the card rate factorises
    the choices -- there is nothing to tune between them. 48 kHz gives 16000,
    12000, 9600, 8000, 6000 and so on; 44.1 kHz gives 14700, 11025, 8820,
    7350, 6300.
    """
    out = []
    for sps in range(MIN_SPS, MAX_SPS + 1):
        if sample_rate % sps == 0:
            out.append(sample_rate // sps)
    return out


def _frame_for(symbol_rate: int) -> int:
    """Frame length giving roughly FRAME_TARGET_SECONDS."""
    want = symbol_rate * FRAME_TARGET_SECONDS
    return min(FRAME_CHOICES, key=lambda f: abs(f - want))


# --------------------------------------------------------------------------
# Building a link from the band it has to fit
# --------------------------------------------------------------------------
#
# The other way to describe a custom link, and the one an operator can actually
# answer. "What symbol rate, roll-off and carrier?" is three questions about
# our implementation; "what does your path pass?" is one question about their
# equipment, and the three fall out of it.
#
# They fall out with nothing left over, which is why this is worth doing rather
# than being a convenience wrapper: a symbol rate has to divide the card rate,
# a roll-off comes from a short list, and the carrier is wherever the resulting
# footprint centres. Only some combinations exist, and picking between them by
# hand means computing symbol_rate x (1 + alpha) repeatedly and checking it
# against the band -- which is exactly what a computer should do.


def check_band(sample_rate: int, band_lo: float, band_hi: float) -> None:
    """Reject a band this card cannot carry, saying which end is wrong."""
    nyquist = sample_rate / 2.0
    if band_hi <= band_lo:
        raise ValueError(
            f"the band's upper edge ({band_hi:.0f} Hz) must be above its "
            f"lower edge ({band_lo:.0f} Hz)")
    if band_lo < 0:
        raise ValueError(f"the band cannot start below DC ({band_lo:.0f} Hz)")
    if band_hi > nyquist:
        raise ValueError(
            f"{band_hi:.0f} Hz is above the {nyquist:.0f} Hz Nyquist limit of "
            f"a {sample_rate} Hz card. Raise the card rate, or lower the top "
            f"of the band.")


def fit_band(sample_rate: int, band_lo: float, band_hi: float
             ) -> tuple[int, float, float]:
    """(symbol_rate, rolloff, carrier) filling a band as fully as it can.

    Two rules, in order:

    **Fastest symbol rate that fits.** The payload rate is proportional to it,
    so this is simply "use the band you were given".

    **Then the most forgiving roll-off that still reaches that rate.** A
    tighter roll-off is a narrower footprint for the same rate, so it is what
    lets the rate go up -- but the rates are quantised by the card, and once a
    rung is reached a tighter roll-off buys nothing further. Measured on a
    44.1 kHz card across a 17.6 kHz band: alpha 0.15 and alpha 0.20 both reach
    14700 Bd, so the answer is 0.20, and the extra timing margin is free. Only
    where a wider roll-off would actually cost a rung does it lose.
    """
    check_band(sample_rate, band_lo, band_hi)
    width = band_hi - band_lo
    best: tuple[int, float] | None = None
    for rolloff in ROLLOFF_CHOICES:
        for rate in valid_symbol_rates(sample_rate):     # fastest first
            if rate * (1.0 + rolloff) <= width:
                if best is None or rate > best[0] or (
                        rate == best[0] and rolloff > best[1]):
                    best = (rate, rolloff)
                break
    if best is None:
        slowest = min(valid_symbol_rates(sample_rate) or [0])
        need = slowest * (1.0 + min(ROLLOFF_CHOICES))
        raise ValueError(
            f"{width:.0f} Hz is too narrow for a {sample_rate} Hz card: the "
            f"slowest symbol rate it can carry is {slowest} Bd, which needs "
            f"{need:.0f} Hz. Widen the band, or lower the card rate.")
    rate, rolloff = best
    # Centred in the band rather than pushed to one edge. The footprint is
    # usually a little narrower than the band -- the rates are quantised -- and
    # splitting the slack leaves guard at both ends instead of all at one.
    carrier = float(round((band_lo + band_hi) / 2.0))
    return rate, rolloff, carrier


def make_profile_for_band(sample_rate: int, band_lo: float, band_hi: float,
                          pilot_spacing: int = 64,
                          frame_symbols: int | None = None,
                          name: str = "CUSTOM", mode: str = "sc") -> Profile:
    """A single-carrier profile filling the given band."""
    rate, rolloff, carrier = fit_band(sample_rate, band_lo, band_hi)
    return make_profile(sample_rate, rate, rolloff, carrier=carrier,
                        pilot_spacing=pilot_spacing,
                        frame_symbols=frame_symbols, name=name, mode=mode)


def make_profile(sample_rate: int, symbol_rate: int, rolloff: float = 0.25,
                 carrier: float | None = None, pilot_spacing: int = 64,
                 frame_symbols: int | None = None,
                 name: str = "CUSTOM", mode: str = "sc") -> Profile:
    """Build a profile from explicit parameters.

    ``carrier`` defaults to sitting the occupied band just above DC with a
    little guard, which is what leaves the most room under Nyquist. Frame
    length is chosen for about a quarter of a second, which keeps acquisition
    quick without spending much of the frame on preamble and header.
    """
    if sample_rate % symbol_rate:
        valid = ", ".join(str(r) for r in valid_symbol_rates(sample_rate))
        raise ValueError(
            f"{symbol_rate} Bd needs a whole number of samples per symbol at "
            f"{sample_rate} Hz. Valid rates here: {valid}"
        )
    bandwidth = symbol_rate * (1.0 + rolloff)
    if carrier is None:
        carrier = bandwidth / 2.0 + max(400.0, sample_rate * 0.01)
    # Whole hertz. The automatic placement lands on fractions -- 7882.875 Hz on
    # a 44.1 kHz card -- and a fraction of a hertz is spurious precision on a
    # carrier that the receiver estimates from the preamble anyway. It also has
    # to survive a link key, which carries the carrier in whole hertz, and a
    # carrier that changed by three hertz on the way through would make the two
    # ends disagree about where the band is for no reason at all.
    carrier = float(round(carrier))
    if pilot_spacing not in PILOT_CHOICES:
        raise ValueError(f"pilot spacing must be one of {PILOT_CHOICES}")
    if frame_symbols is None:
        frame_symbols = _frame_for(symbol_rate)

    profile = Profile(
        name=name, sample_rate=sample_rate, symbol_rate=symbol_rate,
        rolloff=rolloff, carrier=float(carrier), frame_symbols=frame_symbols,
        pilot_spacing=pilot_spacing, mode=mode,
        description=f"custom, {sample_rate} Hz card",
    )
    profile.validate()
    return profile


def resolve(spec) -> Profile:
    """A named preset, or a profile built from explicit parameters."""
    if isinstance(spec, Profile):
        return spec
    if isinstance(spec, str):
        return get_profile(spec)
    return make_profile(**spec)


# --------------------------------------------------------------------------
# Solving between symbol rate and bitrate
# --------------------------------------------------------------------------
#
# These two are the same number seen from either end, related by the MODCOD
# and the frame overhead:
#
#     net_bitrate = symbol_rate * data_fraction * bits_per_symbol
#                   * conv_rate * rs_rate
#
# so fixing either one determines the other. The UI drives both directions off
# the pair below, which is why editing one field moves the other.


def bitrate_for_symbol_rate(sample_rate: int, symbol_rate: int, modcod: Modcod,
                            rolloff: float = 0.25, pilot_spacing: int = 64) -> float:
    """Payload bits per second this symbol rate yields on this MODCOD."""
    p = make_profile(sample_rate, symbol_rate, rolloff, pilot_spacing=pilot_spacing)
    return p.net_bitrate(modcod)


def symbol_rate_for_bitrate(sample_rate: int, bitrate: int, modcod: Modcod,
                            rolloff: float = 0.25, pilot_spacing: int = 64,
                            reserve: int = PAD_RESERVE_BPS) -> int | None:
    """Slowest valid symbol rate that still carries ``bitrate``.

    Slowest rather than fastest because symbol rate is bandwidth: the narrower
    the signal, the more of the card's usable band is left as guard, and the
    less there is to go wrong at the edges. Returns None if even the fastest
    rate this card supports cannot carry it.
    """
    need = bitrate + reserve
    best = None
    for rate in valid_symbol_rates(sample_rate):
        try:
            got = bitrate_for_symbol_rate(sample_rate, rate, modcod, rolloff, pilot_spacing)
        except ValueError:
            continue
        if got >= need:
            best = rate if best is None else min(best, rate)
    return best


def plan(sample_rate: int, symbol_rate: int, modcod: Modcod,
         rolloff: float = 0.25, pilot_spacing: int = 64,
         carrier: float | None = None,
         frame_symbols: int | None = None, mode: str = "sc",
         depth: int | None = None) -> dict:
    """Everything the UI shows for one combination of settings.

    ``carrier`` matters because it decides where the occupied band sits, and
    the named profiles place theirs by hand. Leaving it to the automatic
    choice here would draw a band the transmitter is not going to use -- on
    WIDE, 2.8 kHz below the real upper edge, which is exactly the margin the
    Nyquist warning exists to catch.

    ``frame_symbols`` matters for the same reason and in the same direction:
    RADIO, ACOUSTIC and their 44.1 kHz twins all set a frame length the
    automatic choice would not pick, so planning without it reported a frame
    half as long as the one actually transmitted -- and the interleaver depth
    and acquisition time quoted beside it were wrong to match.
    """
    p = make_profile(sample_rate, symbol_rate, rolloff, carrier=carrier,
                     pilot_spacing=pilot_spacing, frame_symbols=frame_symbols,
                     mode=mode)
    cap = p.capacity(modcod)
    family = p.constellation_family
    lo, hi = p.band
    return {
        "sample_rate": sample_rate,
        "symbol_rate": symbol_rate,
        "sps": p.sps,
        "rolloff": rolloff,
        "carrier": p.carrier,
        "band": [round(lo), round(hi)],
        "bandwidth": round(p.bandwidth),
        "nyquist": sample_rate / 2,
        "fits": hi < sample_rate / 2 and lo > 0,
        "frame_symbols": p.frame_symbols,
        "frame_ms": round(p.frame_duration * 1000),
        "pilot_spacing": pilot_spacing,
        "mode": p.mode,
        "modcod": modcod.index,
        "modcod_name": modcod.label_for(family),
        "modulation": modcod.modulation_for(family),
        "code_rate": f"{modcod.conv_num}/{modcod.conv_den}",
        "rs": f"{RS_N},{modcod.rs_k}",
        "required_evm_db": modcod.required_evm_db,
        "net_bitrate": round(cap.net_bitrate),
        "max_audio_bitrate": max(0, round(cap.net_bitrate) - PAD_RESERVE_BPS),
        "interleaver_seconds": round(interleaver_delay(p, modcod, depth), 1),
        "interleave": interleaver_seconds(depth),
        "interleave_choices": list(INTERLEAVER_CHOICES),
    }


# --------------------------------------------------------------------------
# Interleaver depth
# --------------------------------------------------------------------------

# HD Radio-style diversity delay. Deep enough that a fade or a burst of
# impulse noise is spread across many RS codewords instead of destroying a few
# outright -- and long enough that you wait for it at startup and after any
# loss of lock. That wait is the deliberate trade the user chose.
#
# It is a dial rather than a constant because the right answer is a property of
# the path, and the two ends of the trade are both real:
#
#   shallow  audio starts quickly and recovers quickly, and a fade longer than
#            the depth takes out whole codewords rather than a byte from each
#   deep     rides out a fade many times longer, at the cost of waiting the
#            same amount at startup and after every loss of lock
#
# A burst of length L is spread across ceil(L / branches) errors per codeword,
# and branches saturates at 255, so past that point extra depth works by
# spacing the branches further apart in time rather than by spreading wider.
# That is still what matters against a *fade*, which is long and contiguous.
#
# Unlike the profile, this does not have to be agreed in advance: it travels in
# the signalling block, so the receiver follows whatever the transmitter picks,
# the same way it follows MODCOD. Four rungs, because the index is two bits on
# the wire.
INTERLEAVER_CHOICES = (2.0, 6.0, 12.0, 24.0)
INTERLEAVER_DEPTH_SECONDS = 6.0          # the default rung
INTERLEAVER_DEFAULT_INDEX = INTERLEAVER_CHOICES.index(INTERLEAVER_DEPTH_SECONDS)


def interleaver_seconds(index: int | None) -> float:
    """Depth in seconds for a wire index, forgiving of a missing one."""
    if index is None:
        return INTERLEAVER_DEPTH_SECONDS
    try:
        return INTERLEAVER_CHOICES[int(index)]
    except (IndexError, ValueError, TypeError):
        return INTERLEAVER_DEPTH_SECONDS


def interleaver_index(seconds: float | None) -> int:
    """Wire index for a depth in seconds. Nearest rung, not an exact match.

    Zero counts as unset, not as a request for the shallowest rung. The UI
    sends numbers as ``+field.value || 0``, so a dropdown that has not been
    filled in yet arrives as 0 -- and nearest-rung would read that as two
    seconds, quietly making the shallowest setting the default.
    """
    if not seconds:
        return INTERLEAVER_DEFAULT_INDEX
    return min(range(len(INTERLEAVER_CHOICES)),
               key=lambda i: abs(INTERLEAVER_CHOICES[i] - float(seconds)))


def channel_byte_rate(profile: Profile, modcod: Modcod) -> float:
    """Bytes per second entering the interleaver -- RS codewords, not payload.

    The interleaver sits between the two codes, so it sees the *coded* rate.
    Sizing it from the payload rate would come up short by the RS overhead.
    """
    return profile.net_bitrate(modcod) / 8.0 / modcod.rs_rate


def interleaver_geometry(profile: Profile, modcod: Modcod,
                         depth: int | None = None) -> tuple[int, int]:
    """(branches, increment) for this profile, MODCOD and depth rung.

    Scaled by byte rate so the *time* spanned stays at the chosen depth on
    every rung of the ladder. A fixed geometry would give six seconds of
    protection at QPSK 1/4 and well under one at 256QAM, which is backwards --
    the fast modes are the fragile ones.
    """
    return interleave.geometry(interleaver_seconds(depth),
                               channel_byte_rate(profile, modcod))


def interleaver_delay(profile: Profile, modcod: Modcod,
                      depth: int | None = None) -> float:
    """Actual end-to-end delay the interleaver adds, in seconds.

    The delivered figure, not the one asked for: the geometry lands on whole
    branches and a whole increment, so it comes out near the rung rather than
    on it, and near is what the panel should quote.
    """
    branches, increment = interleaver_geometry(profile, modcod, depth)
    return interleave.delay_bytes(branches, increment) / channel_byte_rate(profile, modcod)


def describe(profile: Profile) -> str:
    """Human-readable rate card. Printed by --list-rates on both apps."""
    low, high = profile.band
    out = [
        f"{profile.name}: {profile.description}",
        f"  {profile.sample_rate} Hz Fs, {profile.symbol_rate} Bd, "
        f"alpha {profile.rolloff}, carrier {profile.carrier:.0f} Hz, {profile.sps} sps",
        f"  occupies {low:.0f}-{high:.0f} Hz ({profile.bandwidth:.0f} Hz wide)",
        f"  frame {profile.frame_symbols} sym = {profile.frame_duration * 1000:.0f} ms, "
        f"{profile.data_fraction * 100:.1f}% payload",
        "",
        "  idx  modulation      RS       net bps   min EVM  interleave",
        "  ---  --------------  -------  --------  -------  ----------",
    ]
    family = profile.constellation_family
    for m, rate in profile.usable_bitrates():
        out.append(
            f"  {m.index:>3}  {m.name_for(family):<14}  {RS_N},{m.rs_k}  "
            f"{rate:>8.0f}  {m.required_evm_db:>6.1f}  "
            f"{interleaver_delay(profile, m):>5.1f} s  "
            f"{interleaver_geometry(profile, m)[0]}x{interleaver_geometry(profile, m)[1]}"
        )
    return "\n".join(out)
