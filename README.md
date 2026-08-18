<img width="2560" height="801" alt="image" src="https://github.com/user-attachments/assets/5794a92f-ffae-4769-b420-6b91910cec53" />


# QAMcast

A single-carrier QAM broadcast modem for the audio band. It takes a live
stream — an internet radio station, or any file ffmpeg can open — and puts it
out as digital audio, HD Radio style: a fixed occupied bandwidth, adaptive
modulation and coding inside it, deep interleaving, and a receiver that can
join a broadcast already in progress.

## What it is for

**Feeding a broadcast FM transmitter, and receiving it back off an SDR.**

That is the whole target. Everything is sized for it: the occupied bandwidth
fits inside a station's audio path, the interleaver is deep enough to ride out
the fades and impulse noise of an off-air signal, and the receiver acquires
from the signal alone with no return path — because a broadcast has none.

The transmitter's output is ordinary mono audio at 44.1, 48 or 96 kHz, so it
goes wherever station audio goes: a sound card into the transmitter's input, a
virtual cable into an encoder, or a WAV file. The receiver takes the same
thing back from an SDR's audio output.

**Status: complete and verified end to end.** A stream goes in one end and
comes out as audio at the other.

Transmitter and receiver are separate programs sharing one `qamcore` package:
the core is the wire format and is never duplicated. Two copies that had
drifted by one constellation label would still start up, still lock, and
decode noise.

## Running it

```bash
python tx.py
```

```bash
python rx.py
```

Transmit UI at <http://127.0.0.1:8731>, receive at <http://127.0.0.1:8732>.
The `.bat` files walk through the same settings in a console. Both apps run
headless too:

```bash
python tx.py --no-ui --source https://stream.example/live.mp3 --bitrate 128k --device 7
```

```bash
python rx.py --no-ui --profile WIDE --device 12 --output 6 --record out.wav
```

`--device wav` writes `tx.wav` instead of using a sound card, and the receiver
reads it back with `--input tx.wav` — a complete loopback with no hardware.
`python tx.py --list-devices` enumerates.

```bash
python tools/selftest.py
```

## Channel profiles

The profile fixes the RF footprint. It is chosen by hand and **must match at
both ends** — the sample rate is the clock the whole frame geometry is
measured against, so it is not something a receiver can adapt to.

| Profile | Fs | Symbol rate | α | Carrier | Occupied | Payload range |
|---|---|---|---|---|---|---|
| **`FM44`** | 44.1 kHz | 14700 Bd | 0.15 | 8.9 kHz | 0.4–17.3 kHz | 6.1 – 87.4 kbps |
| `FM48` | 48 kHz | 16000 Bd | 0.15 | 9.7 kHz | 0.5–18.9 kHz | 6.6 – 95.1 kbps |
| `WIDE` | 96 kHz | 32000 Bd | 0.20 | 23.0 kHz | 3.8–42.2 kHz | 13.6 – 195.2 kbps |
| `WIDE48` | 48 kHz | 16000 Bd | 0.20 | 10.5 kHz | 0.9–20.1 kHz | 6.7 – 96.0 kbps |
| `WIDE44` | 44.1 kHz | 14700 Bd | 0.20 | 10.0 kHz | 1.2–18.8 kHz | 6.2 – 88.2 kbps |
| `RADIO` | 48 kHz | 9600 Bd | 0.25 | 7.0 kHz | 1.0–13.0 kHz | 4.0 – 57.2 kbps |
| `RADIO44` | 44.1 kHz | 8820 Bd | 0.25 | 7.0 kHz | 1.5–12.5 kHz | 3.7 – 52.5 kbps |
| `ACOUSTIC` | 48 kHz | 8000 Bd | 0.30 | 8.0 kHz | 2.8–13.2 kHz | 3.3 – 46.9 kbps |
| `ACOUSTIC44` | 44.1 kHz | 7350 Bd | 0.30 | 8.0 kHz | 3.2–12.8 kHz | 3.0 – 43.1 kbps |

**`FM44` is the default**, and it is there because of what a real transmitter
turned out to pass rather than what seemed safe. `RADIO` is sized for a 15 kHz
audio stage with room to spare; measured on an actual FM path and an SDR, the
link ran clean right up to the 19 kHz stereo pilot. `FM44` and `FM48` fill that
instead, stopping just below it. The roll-off is 0.15 rather than 0.20, which
is what buys the last kilohertz — at 14700 Bd that is an 18.4 kHz footprint
against 16.9 kHz — and a tighter roll-off is only affordable on a path this
clean.

`WIDE` is the one for a clean path — a virtual cable, or a link with the full
audio band available. `RADIO` and the `ACOUSTIC` pair are the narrower, more
rugged footprints, for paths that will not pass what `FM44` asks of them. `WIDE96` and `STANDARD` are aliases
for `WIDE` and `WIDE48`.

**192 kbps requires `WIDE`, and `WIDE` requires a 96 kHz sound card.** On an
ordinary 48 kHz card the usable band is about 19 kHz against 24 kHz of
Nyquist, which is 16 kBd and roughly 96 kbps at the top of the ladder. 44.1
kHz gives 88 kbps. That is a hard limit, not a tuning problem.

Symbol rates are not free choices either: samples-per-symbol has to be a whole
number, so the card's rate factorises the options (48000/16000 = 3,
44100/14700 = 3, 44100/8820 = 5).

### Custom: give it the band

A custom link is described by **two numbers — the lowest and highest frequency
your path passes** — and everything else is fitted to them. That is the only
question about a link an operator can answer without knowing anything about
this modem, and the rest follows from it with nothing left over:

| Mode | Fitted from the band |
|---|---|
| QAM / APSK | symbol rate, roll-off, carrier frequency |
| OFDM | subcarrier count |

```bash
python tx.py --profile CUSTOM --band 400,18000 --source live.mp3
```

Two rules pick the single-carrier fit, in order. **Fastest symbol rate that
fits**, since the payload rate is proportional to it. **Then the most forgiving
roll-off that still reaches that rate** — a tighter roll-off is what lets the
rate go up, but the rates are quantised by the card, so once a rung is reached
a tighter one buys nothing and the extra timing margin is free. On a 44.1 kHz
card a 2–12 kHz band comes out at 7350 Bd with α 0.35, filling 9.92 kHz of the
10 available; a 0.4–18 kHz band comes out at 14700 Bd with α 0.15.

OFDM fills the band at the 100 Hz spacing, which is not a dial: it is the
measured middle of the useful range — the widest that clears a 1.1 ms room
echo, with three times the margin a real transmitter's drift needs. See
*Spacing and carrier count*. A count that fills a band is rarely one of the
ladder rungs, so it travels in the link key as "as many as this band holds",
derived identically at both ends from the band and the spacing the key already
carries. Measured across eight bands in all three modes, every fit lands inside
the band it was given and survives the key exactly.

Asking for more than the card can carry is refused with the number that is
wrong, rather than clamped and then reported as an aliasing warning about an
edge you never typed.

```bash
python tools/rates.py WIDE
```

## Link keys

Card rate, symbol rate, roll-off, carrier frequency, pilot spacing, frame
length, the constellation family — QAM or APSK — and, in OFDM, the subcarrier
spacing and count cannot travel in the header. The frame is

```
[ preamble ][ MODCOD codeword ][ data ]
```

and the preamble is a Zadoff-Chu symbol built *from* the geometry, so the
correlator has to know the transform size and prefix before it can find the
preamble at all. Nothing sent after the preamble — the MODCOD codeword
included — can tell a receiver how to hear the preamble. Only the payload
description configures itself in band.

A **link key** carries that geometry out of band instead. The transmit panel
shows one; paste it into the receiver's Link key box and press Apply, and every
dial follows:

```
QC3-600E00C04NC1P66N    48 kHz card · 9600 Bd · roll-off 0.25 · carrier 7 kHz
QC3-648E00C40E24WJBP    48 kHz card · OFDM 192 carriers · 100 Hz apart · 0.9-20.1 kHz
```

Sixteen Crockford base32 characters over a 10-byte record. Crockford leaves out
I, L, O and U, so a key survives being read aloud or written down, and decoding
ignores case, dashes and spaces. The CRC-8 rejects every single-character typo
tried; the receiver shows what it decoded before it moves anything, because a
key that got through and tuned the link somewhere wrong in silence is worse
than one that is refused.

**The key describes the link, not a preset**, and that distinction is the whole
design. A key naming only the card rate, symbol rate and roll-off looks
sufficient and is not — those three do not determine the carrier, the pilot
spacing or the frame length, and the presets disagree on all three. Five of the
seven set a frame length the automatic choice would not pick, and every one
places its carrier by hand. Rebuilding by searching the profile table for a
match then produces a *plausible* link rather than the right one: a hand-dialled
48 kHz / 9600 Bd / 0.25 link matched RADIO on all three fields and came back
tuned 1000–13000 Hz instead of 480–12480, with twice the frame. It never locks
and nothing says why. So the key carries every field the geometry needs, the
preset it happens to match is shown as a label, and a key that matches nothing
is a Custom link that still works exactly.

A key stays in force at the receiver until a dial is touched by hand, at which
point it is dropped and says so — the dials cannot express a carrier frequency
or a pilot spacing, so reading the settings back out of them would silently
discard three of the things the key exists to carry.

**The transmitter builds from its own key too**, rather than from its dials,
and that is a fix rather than tidiness. The panel solves against the selected
preset and takes its pilot spacing, carrier and frame length; Start used to
rebuild from the dials, which cannot express any of the three. So once the
solver moved the symbol rate — enough on its own to drop the page to Custom —
the transmitter went out on pilot spacing 64 while the key it was displaying
said 128. Copy that key across and the receiver interpolates carrier phase
between symbols that are payload rather than pilots: **sync holds at 0.99, the
constellation smears into rings, and nothing ever locks.** Measured, the
mismatch costs 30 dB of EVM — 52.4 dB down to 22.6 — which is why it limped on
a clean virtual cable and died on anything real. `tools/linkkey_roundtrip.py`
now runs the page's own solve-then-deviate sequence and requires that what the
panel shows is what goes on air.

That failure is also the hardest one to read off the receive panel, because
every number that would explain it is blank — Es/N0, carrier and clock are
only filled in on a locked frame. So the receiver now names it: a strong
correlation peak with nothing decoding means the card rate, symbol rate,
roll-off and carrier are already right (the preamble is built from them and
would not correlate otherwise), and what is left to check is the pilot
spacing, the frame length and the mode.

`tools/linkkey_check.py` covers the format — round trip, typos, formatting,
and that no custom link ever borrows a preset's name. `tools/linkkey_roundtrip.py`
covers the thing that actually matters: that a key copied across gives the
receiver the same physical layer the transmitter is using, over all 354
profile, spacing and carrier combinations and ten hand-dialled links.

In OFDM the receiver's symbol rate is derived from the geometry, not chosen —
it is disabled and shows `14080 sym/s (from geometry)`. It is not a dial to
match by hand, and it will not appear in the single-carrier symbol rate ladder.

## Setting the link

Symbol rate, MODCOD and bitrate are three views of one number:

```
net_bitrate = symbol_rate * data_fraction * bits_per_symbol * conv_rate * rs_rate
```

Fix any two and the third follows, so editing one is never self-contained —
something else has to move, and only you know which. That is what the **lock**
beside each field decides. Edit anything; whatever is still unlocked absorbs
the change.

- **Lock the symbol rate** for the HD Radio arrangement — a fixed slice of
  spectrum, with the MODCOD climbing and falling to fill it.
- **Lock the MODCOD** to hold a known robustness and let bandwidth follow.
- **Lock the bitrate** to hold audio quality and see what each symbol rate
  costs in constellation.

Locks are never silently overruled: ask for something that does not fit and
the panel says so and names what to unlock. Bitrate is typed rather than
picked from a list, because a list has to be built from the current ceiling —
picking from it narrows the symbol rate, which lowers the ceiling, which
removes the higher entries. Bare values under 1000 are read as kbps, so `96`
and `96000` both mean 96 kbps.

Card rate, symbol rate and roll-off **must match at both ends**. Modulation,
code rate and codec travel in the frame, so the receiver picks those up alone.

**Drive** sets the output level into the card, relative to the modulator's
−15 dBFS RMS, and can be moved while transmitting. Set it by the peak reading:
the waveform has a 10 dB peak-to-average ratio, so peaks want to sit near
−5 dBFS with the clipping counter at zero. Clipping is worse than noise here —
it folds energy across the whole constellation at once, and 256QAM has no
margin to absorb it.

## OFDM mode

Five profiles carry OFDM instead of a single carrier, four of them on the same
bands as their single-carrier namesakes so a link can move between the two
without re-planning the spectrum:

| Profile | Fs | Spacing | Carriers | Occupied | Top rate |
|---|---|---|---|---|---|
| `OFDM96` | 96 kHz | 100 Hz | 384 | 3.8–42.2 kHz | 176 kbps |
| `OFDM48` | 48 kHz | 100 Hz | 192 | 0.9–20.1 kHz | 88 kbps |
| `OFDM44` | 44.1 kHz | 100 Hz | 160 | 1.9–17.9 kHz | 73 kbps |
| `OFDMRADIO` | 48 kHz | 100 Hz | 112 | 1.4–12.6 kHz | 45 kbps |
| `OFDMREVERB` | 48 kHz | 25 Hz | 384 | 5.7–15.3 kHz | 42 kbps |

It exists because the single-carrier equaliser cannot be made to reach
further. It corrects ±12 symbols, and training a longer one on the *true*
transmitted symbols was measured to buy only 1–3.5 dB, so that limit is real
rather than a tuning failure. OFDM does not invert the echo at all: a cyclic
prefix longer than the delay spread makes the channel circular, and
equalisation becomes one exact divide per subcarrier.

Measured on WIDE48 against OFDM48, 16QAM, −6 dB echo, 30 dB SNR:

| echo | single carrier | OFDM |
|---|---|---|
| 0.00 ms | 29.2 dB | 28.7 dB |
| 0.60 ms | 12.0 dB | **25.4 dB** |
| 0.90 ms | no lock | **22.9 dB** |
| 1.33 ms | no lock | **18.6 dB** |
| 1.60 ms | no lock | **14.1 dB** |
| 2.50 ms | no lock | 10.3 dB |

The cost is about 12% of the payload symbol rate to the prefix and pilots, and
a 13.5 dB peak-to-average ratio against 10 — so the modulator sits 3 dB lower
and the drive wants setting with more care.

Everything above the physical layer is shared: the same FEC, the same
signalling, the same MODCOD table and thresholds, the same transport and PAD.
Only the symbol layout differs, which is why an OFDM profile needs no special
case in the transport chain or the interleaver.

**One thing OFDM needs that single carrier does not** is carrier frequency
correction. An offset is a fraction of a subcarrier spacing, and any fraction
destroys the orthogonality the scheme rests on — 15 Hz against 93.8 Hz spacing
took the lock rate from 23/23 to 1/23, while an echo and 20 ppm of clock error
did nothing at all. It is estimated from the pilots' phase progression across
a frame and corrected in the time domain before the transform, since the
damage is interference done before it. Measured recovery: exact to 0.01 Hz at
offsets of 5, 15 and 30 Hz.

### Spacing and carrier count

Two dials doing two separate jobs.

**Spacing is the mode.** It alone decides how much echo the guard interval
absorbs and how far off frequency the transmitter may drift, and those move in
opposite directions. **Carrier count is the bandwidth**, and therefore the
bitrate: the block is spacing × count across, so halving the count halves both
the width and the rate and changes nothing else about how the link behaves.

That separation is a correction. This used to be one dial doing both jobs
badly — the band was fixed and the count divided it, so the count *was* the
spacing. It moved echo tolerance up while moving offset tolerance down and left
the throughput alone, which is the one thing anyone would expect a "how many
carriers" control to change. Bandwidth could not be traded for ruggedness at
all, and that trade turns out to be the valuable one.

`tools/spacing.py` measures it. Holding the band and taking as many carriers as
fit, on a 48 kHz card at 256QAM 5/6:

| Spacing | Carriers | Occupied | Absorbs echo | Pulls in offset | bps/Hz |
|---|---|---|---|---|---|
| 400 Hz | 32 | 12.8 kHz | 0.31 ms | 178 Hz | 4.72 |
| 300 Hz | 64 | 19.2 kHz | 0.42 ms | 133 Hz | **4.85** |
| 200 Hz | 64 | 12.8 kHz | 0.62 ms | 89 Hz | 4.76 |
| 150 Hz | 128 | 19.2 kHz | 0.83 ms | 67 Hz | 4.71 |
| **100 Hz** | **192** | **19.2 kHz** | **1.25 ms** | **44 Hz** | **4.57** |
| 75 Hz | 256 | 19.2 kHz | 1.67 ms | 33 Hz | 4.39 |
| 50 Hz | 384 | 19.2 kHz | 2.50 ms | 22 Hz | 4.36 |
| 25 Hz | 384 | 9.6 kHz | 5.00 ms | 11 Hz | 4.36 |

**Spectral efficiency barely moves — 12% across a sixteen-fold change in
spacing — while echo tolerance moves 16×.** So the spacing is chosen for the
channel, not for the rate, and buying echo tolerance is far cheaper than it
looks. Efficiency does fall monotonically as the spacing narrows, and for a
reason worth naming: the prefix costs a fixed *fraction* either way, but the
preamble and MODCOD codeword are two symbols out of a frame of fixed duration,
so a longer symbol means fewer symbols to spread them across.

Decoding 40 frames at each spacing bears it out, and shows where the cliffs are:

| Spacing | `radio` (0.12 ms, 15 Hz) | `acoustic` (1.1 ms) | `reverb` (6.0 ms) |
|---|---|---|---|
| 400 Hz | 39/40 | 13/40 | 1/40 |
| 200 Hz | 39/40 | 24/40 | 1/40 |
| 100 Hz | 38/40 | **39/40** | 5/40 |
| 50 Hz | 37/40 | 39/40 | 35/40 |
| 25 Hz | **0/40** | 38/40 | **38/40** |

**100 Hz is the default**: the widest that clears the acoustic channel, with 3×
margin over the radio channel's drift. The 25 Hz column is the offset cliff —
11 Hz of tolerance against 15 Hz of drift, and it is a cliff rather than a
softening.

Both ends must be set the same, exactly like the profile — none of it travels
in the header. Both show in the profile name, so `OFDM48-64@100` names the link
completely and can be handed straight to `--profile` at the far end. Asking for
more carriers than the band can hold at a given spacing is refused rather than
allowed to spill past the upper edge — on an FM path that edge is the 19 kHz
stereo pilot.

**The spacing is not on the pages.** It is fixed at 100 Hz, the measured middle
of the range above, and the table is why: efficiency moves 12% across the whole
span while the two tolerances move sixteen-fold, so there is one sensible
answer for a path nobody has characterised, and the presets that want a
different one carry it themselves. `--spacing` is still there for measurement,
and `tools/spacing.py` is what it is for.

### Reverb, which used to be the case that failed

The `reverb` channel preset — 2.5 ms and 6 ms echoes — was built to be the case
this modem does *not* handle, so the limit could be measured rather than
assumed. It no longer is. `OFDMREVERB` trades half the bandwidth for eight
times the echo tolerance and decodes it:

| Profile | Occupied | reverb, 90 frames | Audio |
|---|---|---|---|
| `WIDE48` single carrier | 19.2 kHz | 0 locked | none |
| `OFDM48` | 19.2 kHz | 7 locked | none |
| `OFDMREVERB` | 9.6 kHz | **89 locked** | **180/180 packets bit-exact** |

At 16QAM 1/2, which is the rung the 12.1 dB EVM through that channel supports —
11.6 kbps, enough for speech-grade Opus. It is not a default: 11 Hz of offset
tolerance needs a path that does not drift, so it is for a loudspeaker in a
room, not for a transmitter.

**Frame duration is held near 220 ms across the whole range** rather than left
to scale with the symbol. The preamble overhead, the acquisition delay and the
interval between offset updates are all counted in frames, and letting those
swing by sixteen with the carrier count would mean moving one dial moved
everything. Fixing the duration leaves exactly one thing moving.

### Sync

Preamble detection correlates in quadrature — against the preamble and against
the same preamble shifted 90°, combined as a magnitude. That is the difference
between working and not. A single real correlation measures the cosine of the
carrier phase, and a frequency offset walks that phase steadily: 2 Hz against a
225 ms frame advances it 162° per frame, so the peak slid from 0.99 to 0.31
over ten frames, was rejected, re-acquired and slid again. It looked like a
timing fault and was not — the timing offset was zero throughout. The magnitude
of the two arms does not depend on the phase at all.

The tracking window also has to be able to look *backwards*. Trimming the
buffer to the predicted position quietly made the next search one-sided, so a
preamble arriving earlier than predicted could not be found: the peak search
pinned to the window edge and decayed as the real peak receded behind it. That
is what a sampling clock does in one of its two directions, and it cost a frame
every nine at 20 ppm. A signal this wide has a correlation about two samples
across, so two samples of unsearchable drift is the whole peak.

Together those two took OFDM from losing ~12% of frames ongoing on the `radio`
and `noisy` presets to zero resyncs and 43 of 43 frames on every carrier count
tried. The acceptance threshold scales with the preamble length, since that now
ranges from 72 samples to 1080: measured on noise, the 99.99th percentile of
the metric is 3.83/√n at every length, and the threshold sits just above it.

## APSK

A third mode, alongside QAM and OFDM, selected from the same dropdown. The
points sit on concentric rings instead of a square grid: ring *k* of *n* holds
4 + 8(k−1) points, so *n* rings hold exactly 4n² — 4, 16, 64 and 256 at n = 1,
2, 4 and 8, which is the ladder's four constellation sizes out of one
construction. Radii equalise the minimum distance, `r_k = 1 / (2 sin(π/n_k))`,
which puts 16APSK's outer-to-inner ratio at **2.73** — inside DVB-S2's 2.57 to
3.15 for the same 4+12 layout, which is a useful check on the rule.

It exists because a square 256QAM has 32 distinct amplitudes and an amplifier
near saturation gives each a different gain and a different phase; the APSK of
the same order has 8. The receiver exploits that: the whole distortion is a
handful of complex numbers, so it estimates one gain and rotation per ring from
the decisions and takes them back out. That is decision-directed, which this
codebase has been burned by before, but here it is 2 to 8 unknowns against
thousands of symbols rather than 25 free taps — over-determined enough that a
fifth of the decisions being wrong barely moves it.

**Measured, it does not pay off here, and that is worth saying plainly.**
Against `WIDE48` at 32 dB SNR through a Rapp amplifier model with AM/PM,
counting frames recovered bit-exact out of 87:

| MODCOD | linear | 8 dB backoff | 5 dB | 4 dB |
|---|---|---|---|---|
| 16-point, 3/4 | 84 / 84 | 87 / 87 | 87 / 87 | 87 / 87 |
| 64-point, 5/6 | 84 / 84 | 87 / 87 | 75 / 76 | 31 / 3 |
| 256-point, 5/6 | 84 / 82 | 84 / 73 | 0 / 0 | 0 / 0 |

*(QAM / APSK)*

The reason is specific to a pulse-shaped single carrier. The constellation's
own peak-to-average advantage is 1.75 dB at 256 points — but on the wire, after
root-raised-cosine shaping, the measured envelope advantage is only **0.86 dB**,
because between symbol instants the envelope takes every value regardless of
where the points are. Meanwhile APSK gives up 0.7–0.9 dB of minimum distance.
The two roughly cancel, and what compression does to the *pulse shape* is
intersymbol interference, which no per-ring correction can undo.

So APSK is shipped, complete and tested, and QAM remains the default. It is
worth trying on a real amplifier — the model here is a model, and a path whose
nonlinearity is a hard clipper in an audio stage rather than a smooth RF
compression would weigh it differently — but on this simulator it loses.

**The family has to match at both ends**, exactly like the profile, and it has
a distinctive failure. Nothing about it travels in the header, so a receiver
told the wrong one still finds the preamble, still tracks the carrier and still
draws a clean constellation — the preamble, the pilots and the MODCOD codeword
are all family-independent — and simply never locks, because the payload is
being sliced against the wrong point set. Sync 0.99 with no lock and no header
errors moving is that. It is what the link key exists to prevent; the Mode
dropdown is a convenience for setting the two ends by hand.

## MODCOD ladder

MODCOD sets the payload rate *inside* the footprint, and travels in the frame.

| idx | modulation | RS | net (WIDE) | min EVM |
|---|---|---|---|---|
| 0 | QPSK 1/4 | 255,223 | 13.6 k | 5.4 dB |
| 2 | QPSK 1/2 | 255,223 | 27.3 k | 6.4 dB |
| 5 | 16QAM 1/2 | 255,223 | 54.6 k | 11.5 dB |
| 7 | 16QAM 3/4 | 255,223 | 82.0 k | 13.4 dB |
| 9 | 64QAM 3/4 | 255,223 | 123.0 k | 20.6 dB |
| 12 | 256QAM 5/6 | 255,239 | 195.1 k | 24.9 dB |

Indices 0–12 are the curated ladder; 13–23 fill in the rest of the grid for
manual use. All 24 thresholds are measured rather than inferred — re-measure
any time with `tools/thresholds.py`.

Coding is Reed-Solomon over GF(256) outside a K=7 convolutional code
(G1 = 0o171, G2 = 0o133) with DVB-S puncturing and soft-decision Viterbi, and
a Forney convolutional interleaver between them.

### Interleaver depth

The depth is the deliberate trade: a fade or a burst of impulse noise is spread
across many RS codewords instead of destroying a few outright, and you wait for
it at startup and after any loss of lock. It is selectable — **2, 6, 12 or 24
seconds**, six being the default — because how long a dropout a path throws at
you is a property of the path.

Measured on `FM44` at 64QAM 5/6, RS(255,239), corrupting a contiguous run of
bytes with sync left untouched, the longest dropout that costs **no audio at
all**:

| Depth | Geometry | Rides out | Wait at startup |
|---|---|---|---|
| 2 s | 133 branches × 1 | 61 ms | 2.0 s |
| **6 s** | 230 branches × 1 | **103 ms** | 6.0 s |
| 12 s | 230 branches × 2 | 211 ms | 12.1 s |
| 24 s | 230 branches × 4 | 422 ms | 24.1 s |

It doubles as the depth doubles, which is what it should do — a burst of *L*
bytes lands roughly `255L / (L + delay)` errors in each codeword, so the
correctable burst grows with the delay. The 2 s rung is off that line because
it is short enough that the branch count falls too, and branches are what
spread a burst *across* codewords in the first place.

**Past that threshold, deeper is worse**, and that is worth knowing before
reaching for 24 s. Once a burst overwhelms the code, spreading it wider turns a
concentrated loss into a diffuse one: at a 1 s dropout the 2 s rung lost 61
audio packets and the 24 s rung lost 492. Interleaving buys you a cliff edge
further out, not a gentler slope.

The depth **does not have to be matched by hand.** It rides in the signalling
block, so the receiver reads it off the first frame it decodes and reconfigures
itself — the same arrangement as MODCOD, and for the same reason: it changes
nothing about the waveform, so it can be protected by the payload's own FEC
rather than having to be known in advance. `--interleave 12` on the
transmitter, or the dropdown; the receiver has no dial for it and reports what
it is following.

## Frame

```
[ preamble 64 ][ MODCOD codeword 64 ][ pilot, data x (spacing-1) ] x groups
```

Everything follows from one requirement: **a receiver switching on
mid-broadcast must work out everything it needs from the next frame it sees.**

- The **preamble** is Zadoff-Chu, repeated every frame, giving frame position
  to sub-sample resolution, carrier error from its phase slope, and equaliser
  training — all from one correlation.
- **MODCOD travels as a BPSK codeword**: one of 32 orthogonal length-64
  sequences, recovered by correlating against the whole codebook. Nothing can
  be demodulated until the MODCOD is known, so that field cannot depend on the
  payload — and being what everything waits on, it is worth 18 dB of
  processing gain. It occupies the symbols the old coded header used, so it
  costs no capacity.
- **Everything else** — codec, flags, interleaver and RS phases, interleaver
  depth, frame counter — rides inside the payload, protected by the same coding
  as the audio. Seven bytes, about 0.1% of the frame.

The phases are stated outright rather than derived from a counter, which buys
immunity to counter wraps, to joining at an arbitrary point, and to the whole
class of bugs where two ends disagree about a modular arithmetic convention.

### Energy dispersal, and a switch for watching it work

The payload is XORed with a PRBS seeded per frame, so whatever the codec
produces goes out looking like noise. Compressed audio is already high-entropy;
digital silence and stuffing runs are not, and a run of identical bytes is a run
of identical symbols, which is a tone.

The testing panel can turn it off, which is the fastest way to see why it is
there. Measured spectral flatness of the transmitted frame — 1.0 is noise-like,
small is tonal:

| Payload | Dispersal on | off |
|---|---|---|
| Compressed audio, `FM44` | 0.033 | 0.023 |
| **Digital silence, `FM44`** | **0.033** | **0.003** |
| Digital silence, `OFDM44` | 0.163 | 0.048 |

Eleven times more tonal on silence, and essentially unchanged on music — which
is the scrambler's whole case in two rows.

It is safe to flip while on air, and that is the point: the setting rides in the
frame flags, so the receiver follows within a frame and the waterfall changes
while you watch. Decoding is unaffected — measured bit-exact in both modes with
it off. The **signalling block stays scrambled either way**, and has to: the
flag lives inside it, so a receiver needing the flag before it could read the
block would have nowhere to start.

Putting MODCOD in a codeword rather than a coded header removed a floor: the
old header was QPSK rate-1/2 behind nothing but a CRC, making it weaker than
the payload it described, and it failed at about 7 dB EVM whatever the payload
could have survived. The rugged rungs now differentiate properly — QPSK 1/4
through 3/4 measure 5.4, 5.9, 6.4, 6.9, 8.7 dB, where before they were a
meaningless 7.1, 7.9, 6.5, 6.8, 7.4.

## Sources

Anything ffmpeg opens. **Preset station** reads `.pls` and `.m3u` playlists
from a folder and turns them into a station picker with a second dropdown for
the rates that station offers — the rate and codec come from the URL, never
the filename, because the two disagree (`groovesalad130.pls` serves
`groovesalad-128-aac`).

```bash
python tx.py --list-stations
```

### Passing a stream through

Re-encoding an already-compressed stream is generational loss. **Passthrough**
copies the source's own packets into frames instead, so the station's bits
reach the receiver's decoder untouched.

```bash
python tx.py --source https://ice1.somafm.com/dronezone-64-aac --passthrough --profile WIDE48
python tx.py --probe https://ice1.somafm.com/dronezone-64-aac
```

The source is probed rather than trusted — a URL ending `-64-aac` is a
filename, not a guarantee. It needs the codec to be one the frame header can
name (MP3, FLAC and Vorbis have no code point) and the stream's rate to fit
the channel; both refusals say which. The stream then sets the bitrate, so the
UI fills it in from the probe and locks it.

Measured end to end over a 25 dB channel: 776 of 776 packets bit-exact.

One thing passthrough cannot tell you is whether an HE-AAC stream is v1 or v2
— Parametric Stereo is signalled inside the SBR payload, not the ADTS header,
so ffprobe reports plain `HE-AAC` either way. It is signalled as v1 and shown
as "SBR (+ PS if present)" rather than claiming there is none. The audio is
unaffected: both decode through the same AAC decoder, which finds PS on its
own.

### Song titles

**Follow the station** puts the real artist and title on the PAD channel. The
song is not in the audio — it rides in the HTTP layer as ICY metadata, so it
is read over its own connection (ffmpeg surfaces the ICY headers but not
`StreamTitle`, and says nothing when the track changes).

There is no standard for the order. Most stations send `Artist - Title` and
some send the reverse, so that is the default and **this station sends Title –
Artist** is offered for the rest. The raw string is always shown.

### Codecs

**Opus is the default**: one codec across 16–192 kbps, royalty-free, with
packet-loss concealment built in — which matters when there is no
retransmission. It is encoded in *constrained* VBR, because plain VBR
overshoots `-b:a` by 25–55% on demanding material (measured 228 kbps against a
192 kbps request), and on a fixed-rate channel that is an ever-growing
backlog.

HE-AAC steps down a ladder as the rate rises, because FDK clamps HE-AACv2
stereo near 160 kbps:

| bitrate | profile | SBR | PS |
|---|---|---|---|
| to 48k | HE-AACv2 | yes | yes |
| 48–96k | HE-AACv1 | yes | — |
| above 96k | AAC-LC | — | — |

Those switches are otherwise invisible, so the transmit UI draws the ladder
with the live rung lit. It needs a `--enable-nonfree` ffmpeg with `libfdk_aac`.

**xHE-AAC (MPEG-D USAC)** encodes, decodes and passes through, and none of it
goes through ffmpeg's encoders, because none of them can do it:

- the open-source Fraunhofer FDK ships a USAC decoder and **no USAC encoder**,
  which is why `libfdk_aac` is present in this build and still rejects
  `-profile:a usac`, `xhe` and `aac_usac`;
- ffmpeg's native AAC encoder is LC only, and so is MediaFoundation's;
- no encoder in this build advertises USAC at all.

So it uses one that can. [exhale](https://gitlab.com/ecodis/exhale) is a small
open-source USAC encoder; `tools/build_exhale.py` fetches its source at a
pinned revision, makes two changes to it, builds it, and puts the result in
`bin/`. Nothing is downloaded pre-built and no binary is committed.

```bash
python tools/build_exhale.py
```

The chain is `ffmpeg → WAVE → exhale → access units`. The codec dropdown asks
whether that will work before offering it, and shows the reason when it will
not — a listed encoder that fails at Start is a control that looks alive and
does nothing.

Its rungs are exhale's presets, chosen by bitrate the way the HE-AAC ladder is,
from **36 kbps stereo** up to 108 in twelves. Below 36 the dropdown says so
rather than failing later. That floor is exhale's, not the standard's: it
implements the frequency-domain coding tools and not ACELP or the low-rate
stereo tools, so 36 kbps here is not what xHE-AAC does at 36 kbps.

It is **constant quality, not constant bitrate** — there is no rate control to
ask. On easy material it runs well under the rung it was given (a tone at the
48k rung measured 15.7 kbps); on demanding material it can run over. Leave the
link some headroom, or use Opus, which honours the number.

**The packets travel bare, with the configuration sent out of band** — the same
arrangement Opus uses, and for the same reason: no self-delimiting AAC
transport can carry USAC. ADTS cannot describe it at all, its profile field
having no value that means USAC. LOAS can describe it, and ffmpeg will not
decode it: the muxer refuses outright — *"Muxing MPEG-4 AOT 42 in LATM is not
supported"* — and the `aac_latm` decoder cannot parse a USAC config either,
measured by feeding it one that ffmpeg itself reads happily out of an MP4,
rebuilt at all nine plausible bit lengths and rejected at every one.

What ffmpeg does decode is MP4. A plain MP4 cannot be streamed — its sample
table can only be written once the last sample is known — so the receiver
rebuilds the packets into a **fragmented MP4**, the thing DASH and HLS have
carried for a decade, and pipes that in. `fmp4.py` writes it and reads it.

Measured, through the real encoder and decoder:

| | |
|---|---|
| every rung, 36k to 108k | 282 access units of 282, decoded 12.03 s of 12.03 |
| round trip vs. the source | correlation 0.9996 at 36k, rising to 0.9999 at 108k |
| passthrough of an xHE-AAC file | 282 units relayed, **bit-exact**, correlation 0.9999 |
| container transparency | our fragments decode **byte-identically** to the encoder's own MP4 |
| joining mid-broadcast | audio within 341 ms, worst of 24 starting points |

`python tools/xhecheck.py` runs all of that.

One behaviour to know about: exhale **levels to −23 LUFS** whenever it writes
to a pipe, so the transmitted loudness is normalised and the first second is
the leveller settling. Passthrough is untouched.

Codec config and PAD are **retransmitted once a second, not sent once**. A
receiver joining mid-broadcast has missed anything sent at the start — and so
has the interleaver's fill region.

## Receiver

Acquisition is frame-synchronous and data-aided, with no feedback loops:
correlate for the preamble, measure carrier error from its phase slope, solve
a least-squares equaliser against it, then track with the pilots. Volume and
pause apply to the monitoring output only — a recording keeps the audio as
recovered, and pause mutes and stays live rather than holding your place,
since a broadcast carries on regardless.

The audio bitrate shown is **measured** off the packets that arrive, not read
from the frame: nothing on the wire states it, so this is the only honest
answer available, and it tracks VBR drift and dropouts.

After a loss of lock the receiver re-acquires on its own. It checks the
interleaver and RS phases every frame and bridges short gaps with fill rather
than restarting the chain — a 2, 10, 30 or 120-frame dropout all recover.

### The delay-spread limit

The equaliser is trained on the 64-symbol preamble, and a least-squares solve
for *n* taps gets only `64 - n + 1` equations from it. 25 taps is 40 equations
for 25 unknowns; 41 taps is 24 for 41, which is underdetermined and returns
noise that looks like an equaliser. Measured clean-channel EVM: **56 dB at 25
taps, 6 dB at 41.**

25 taps is ±12 symbols, so 0.38 ms of correctable delay spread on `WIDE` and
1.25 ms on `RADIO`. That is comfortable for a direct feed and for most
off-air paths; severe multipath is the known weakness of single-carrier QAM
and was flagged before any code was written. The channel simulator's `reverb`
preset deliberately exceeds it, and is kept as a failing test for single
carrier so the limit stays measured rather than assumed. OFDM at 25 Hz spacing
now decodes it — see *Reverb, which used to be the case that failed*.

## Shutting down

Both apps close their devices on the way out however they are ended: Ctrl+C,
a `kill`, a closed console, or an unhandled exception. That sounds like it
should go without saying and it did not.

`stop()` used to set a flag, join the worker with a five second timeout, and
return `{"ok": True}` whether or not the worker had actually stopped. When the
capture device had gone away — unplugged, or a virtual cable with nothing
writing to it — the worker sat in a blocking `read()` and never saw the flag.
So `stop()` lied, the interpreter exited, the daemon thread was killed
mid-read, and **PortAudio's callback carried on playing**: it runs on a native
thread, not a Python one, and does not get killed with them. The audio kept
going after the process was, to all appearances, closed.

Now the devices are held on the app object as well as in the worker, and
`stop()` closes them itself rather than hoping the worker will. Closing the
capture stream is also what unblocks the read, so the worker usually unwinds a
moment later; if it does not, `stop()` says so instead of claiming a clean
exit. Nothing covers Task Manager's End Task — the OS reclaims the device
there, which is why that case never showed the symptom.

## Verified

Transmitter to `tx.wav` to receiver, no hardware involved:

```
WIDE, 96 kHz:   locks within one frame, sync corr 0.99
                MODCOD and codec auto-detected from the frame
                EVM 47-52 dB, zero RS failures, PAD received
                440 Hz left / 660 Hz right - stereo intact

WIDE48, 48 kHz: locked, EVM 46-52 dB, zero RS failures
                34.7 s recovered from 39.7 s sent (the difference is
                the interleaver filling)
```

`tools/loopback.py` passes on every profile over the clean, noisy and radio
channel presets — including every OFDM profile at every spacing and count,
which is where the `acoustic` preset starts sorting them by prefix length.
`tools/dfree.py` confirms every punctured rate matches its published free
distance.

A profile name carries its spacing and carrier count, so the end-to-end check runs at any
of them:

```bash
python tools/selftest.py OFDM48-96@100 32k opus
```

`tools/conv_check.py` runs the Viterbi decoder against a plain textbook
implementation over every rate and requires them to agree on every bit, and
`tools/dfree.py` checks the code itself. `tools/pagecheck.py` checks the two
web pages statically — that every `$('id')` the script uses exists, that every
function it calls is defined, that every dial in the Link panel is actually
sent to the server, and that the div tags balance. A page whose script throws
during start-up does not look broken, it looks *empty*, and shipping exactly
that is what the tool is for; a control that is wired to a handler but left out
of the payload looks alive and does nothing, which is how the mode selector
shipped.

## Speed

The receive path — demodulate, Viterbi, Reed-Solomon, deinterleave, transport
— fed in soundcard-sized blocks with no audio hardware involved:

| Profile | Real time | One core |
|---|---|---|
| `WIDE` (96 kHz, 32 kBd) | 72× | 1.4% |
| `WIDE48` | 153× | 0.7% |
| `RADIO` | 210× | 0.5% |
| `OFDM96` | 146× | 0.7% |
| `OFDM48` | 254× | 0.4% |
| `OFDMRADIO` | 253× | 0.4% |

That is 2.3–3.2× quicker than it was, and the narrow OFDM geometries are 7.8×
quicker, which was the outlier worth chasing: `OFDM48` at 24 carriers has 145
data symbols in a frame where 384 carriers has 12, and a per-symbol Python loop
made the cheapest geometry the most expensive thing in the receiver. Batched
across symbols, its cost tracks its carrier count.

The rest came from four places, all of them arithmetic that was being done the
long way rather than anything structural: the Viterbi butterfly (see conv.py —
4.2× on its own, and bit-identical), syndrome evaluation against a constant
(9.5×), a 16-tap interpolation that was building three megabytes of temporaries
per frame, and a Forney interleaver expressed as 255 branch queues when it is
really one gather. Nothing about the wire format changed; every loopback and
the full carrier matrix produce identical EVM figures to the tenth of a dB.

## Licence

MIT — use it for anything, including commercially, as long as the copyright
and licence notice come with it. See [LICENSE](LICENSE).

## Requirements

Python 3.14, numpy, scipy, numba, sounddevice. ffmpeg for the codec layer;
`libfdk_aac` only if you want HE-AAC. For xHE-AAC, cmake and a C++ compiler
once, to build exhale — see `tools/build_exhale.py`. VB-CABLE if you want to
loop the two apps together without hardware.

## Layout

```
qamcore/            the wire format — one copy, both ends
  profiles.py       channel profiles, MODCOD ladder, frame capacity
  constellation.py  Gray-coded QAM and APSK, soft LLR demapping
  rrc.py            root-raised-cosine shaping and matched filtering
  interp.py         shared fractional-sample interpolation
  conv.py           K=7 convolutional, six rates, numba soft Viterbi
  rs.py             Reed-Solomon over GF(256)
  interleave.py     Forney convolutional interleaver
  framing.py        preamble, MODCOD codeword, pilots, frame assembly
  transport.py      packets, PAD, the outer-code chain
  modulator.py      symbols to real passband audio
  demodulator.py    passband audio back to frames
  channel.py        transmit-side impairments, for testing
  codec.py          codec choice, probing, passthrough, encode and decode
  streams.py        .pls/.m3u playlists folded into stations and their rates
  icy.py            Icecast/Shoutcast now-playing metadata
  ofdm.py           OFDM: geometry, modulator, demodulator, coded frames
  linkkey.py        the physical layer as one copyable token
  ogg.py            Ogg pages, so Opus packets can be carried bare
  fmp4.py           fragmented MP4, the only container ffmpeg decodes USAC from
  exhale.py         the xHE-AAC encoder, and unwrapping what it writes
  scope.py          telemetry for the spectrum and constellation displays
  webui.py          local UI: static files, SSE telemetry, JSON control
tx.py               transmitter
rx.py               receiver
web/                the two pages, styling, scopes
tools/              selftest, loopback, rate card, thresholds, decoder check
```
