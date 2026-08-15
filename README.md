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
| `WIDE` | 96 kHz | 32000 Bd | 0.20 | 23.0 kHz | 3.8–42.2 kHz | 13.6 – 195.2 kbps |
| `WIDE48` | 48 kHz | 16000 Bd | 0.20 | 10.5 kHz | 0.9–20.1 kHz | 6.7 – 96.0 kbps |
| `WIDE44` | 44.1 kHz | 14700 Bd | 0.20 | 10.0 kHz | 1.2–18.8 kHz | 6.2 – 88.2 kbps |
| `RADIO` | 48 kHz | 9600 Bd | 0.25 | 7.0 kHz | 1.0–13.0 kHz | 4.0 – 57.2 kbps |
| `RADIO44` | 44.1 kHz | 8820 Bd | 0.25 | 7.0 kHz | 1.5–12.5 kHz | 3.7 – 52.5 kbps |
| `ACOUSTIC` | 48 kHz | 8000 Bd | 0.30 | 8.0 kHz | 2.8–13.2 kHz | 3.3 – 46.9 kbps |
| `ACOUSTIC44` | 44.1 kHz | 7350 Bd | 0.30 | 8.0 kHz | 3.2–12.8 kHz | 3.0 – 43.1 kbps |

`WIDE` is the one for a clean path — a virtual cable, or a link with the full
audio band available. `RADIO` is sized to survive a transmitter's 15 kHz audio
stage with room to spare. The last two are the narrowest and most rugged
footprints, for the worst-behaved paths. `WIDE96` and `STANDARD` are aliases
for `WIDE` and `WIDE48`.

**192 kbps requires `WIDE`, and `WIDE` requires a 96 kHz sound card.** On an
ordinary 48 kHz card the usable band is about 19 kHz against 24 kHz of
Nyquist, which is 16 kBd and roughly 96 kbps at the top of the ladder. 44.1
kHz gives 88 kbps. That is a hard limit, not a tuning problem.

Symbol rates are not free choices either: samples-per-symbol has to be a whole
number, so the card's rate factorises the options (48000/16000 = 3,
44100/14700 = 3, 44100/8820 = 5).

```bash
python tools/rates.py WIDE
```

## Link keys

Card rate, symbol rate, roll-off, carrier frequency, pilot spacing, frame
length and — in OFDM — the subcarrier count cannot travel in the header. The
frame is

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
QC2-402Y00C04NC1P6FR    48 kHz card · 9600 Bd · roll-off 0.25 · carrier 7 kHz
QC2-448E00C40E24W00B    48 kHz card · OFDM 24 carriers · 0.9-20.1 kHz
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

`tools/linkkey_check.py` covers the format — round trip, typos, formatting,
and that no custom link ever borrows a preset's name. `tools/linkkey_roundtrip.py`
covers the thing that actually matters: that a key copied across gives the
receiver the same physical layer the transmitter is using, over all 43
profile-and-carrier combinations and five hand-dialled links.

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

Four profiles carry the same bands over OFDM instead of a single carrier, so a
link can move between the two without re-planning the spectrum:

| Profile | Fs | Default carriers | Occupied | Top rate |
|---|---|---|---|---|
| `OFDM96` | 96 kHz | 384 | 3.8–42.2 kHz | 176 kbps |
| `OFDM48` | 48 kHz | 192 | 0.9–20.1 kHz | 88 kbps |
| `OFDM44` | 44.1 kHz | 192 | 1.2–18.8 kHz | 79 kbps |
| `OFDMRADIO` | 48 kHz | 128 | 1.0–13.0 kHz | 48 kbps |

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

### Choosing the carrier count

The band is fixed by the profile, so the number of carriers *is* the subcarrier
spacing, and the spacing sets both halves of the only trade OFDM has. It is
selectable at both ends — 24, 32, 48, 64, 96, 128, 192, 256 or 384 — because
which half matters depends on the link. On `OFDM48`:

| Carriers | Spacing | Absorbs echo | Pulls in offset | Top rate |
|---|---|---|---|---|
| 24 | 750 Hz | 0.17 ms | 333 Hz | 82 kbps |
| 32 | 585 Hz | 0.21 ms | 261 Hz | 89 kbps |
| 48 | 393 Hz | 0.31 ms | 175 Hz | 94 kbps |
| 64 | 300 Hz | 0.42 ms | 133 Hz | 93 kbps |
| 96 | 198 Hz | 0.62 ms | 88 Hz | 92 kbps |
| 128 | 150 Hz | 0.83 ms | 67 Hz | 90 kbps |
| 192 | 100 Hz | 1.25 ms | 44 Hz | 88 kbps |
| 256 | 75 Hz | 1.67 ms | 33 Hz | 84 kbps |
| 384 | 50 Hz | 2.50 ms | 22 Hz | 84 kbps |

Throughput barely moves, and that is not luck: the payload rate comes from the
occupied bandwidth, not from how finely it is divided. What moves is which
impairment breaks the link first — an echo longer than the third column, or a
transmitter further off frequency than the fourth. Loopback bears the table
out. The `acoustic` preset (1.1 ms echo) needs 96 carriers or more; the `radio`
preset (15 Hz offset) fails at 384 on `OFDM48` and gets no lock at all at 384
on `OFDMRADIO`, whose 31 Hz spacing puts 15 Hz half a subcarrier out.

Both ends must be set the same, exactly like the profile — none of it travels
in the header. `--carriers 64` on both apps, or the dropdown on both pages. A
count shows in the profile name, so `OFDM48-64` names the link completely and
can be handed straight to `--profile` at the far end.

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
a Forney convolutional interleaver about six seconds deep between them. That
depth is the deliberate trade: a fade or a burst of impulse noise is spread
across many RS codewords instead of destroying a few outright, and you wait
for it at startup and after any loss of lock.

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
- **Everything else** — codec, flags, interleaver and RS phases, frame
  counter — rides inside the payload, protected by the same coding as the
  audio. Six bytes, about 0.1% of the frame.

The phases are stated outright rather than derived from a counter, which buys
immunity to counter wraps, to joining at an arbitrary point, and to the whole
class of bugs where two ends disagree about a modular arithmetic convention.

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

Codec config and PAD are **retransmitted once a second, not sent once**. A
receiver joining mid-broadcast has missed anything sent at the start — and so
has the interleaver's fill region.

## Riding out a dropout

The receiver holds a **reservoir** of decoded audio — six seconds by default —
and does not start playing until it is full. A loss of lock shorter than that
never reaches the speaker.

It has to work that way round, and the reason is worth stating because the
obvious alternative does not exist. The transmitter sends at exactly one times
real time, because a live stream *produces* at one times real time and there is
nothing further ahead to send; pulling harder on an Icecast socket gets you the
server's connect burst once and real time forever after. So the receiver cannot
be handed a reservoir. It can only make one, by declining to play the first few
seconds it is given.

The costs are real and all of them are latency:

- audio starts `reservoir` seconds late, on top of the interleaver fill
- you are permanently that far behind live
- an underrun costs the whole wait again, because playing fragments as they
  land sounds worse than one clean pause

A dropout that fits inside the reservoir is inaudible but spends it. At exactly
one times real time it would stay spent until the next dropout emptied it
altogether, so play-out runs **0.5% slow** while short — about eight cents of
pitch, inaudible on music, and it rebuilds a second of reservoir in a little
over three minutes. Resampling goes through the same windowed-sinc bank the
demodulator uses; dropping or doubling samples instead would put a click in the
audio at exactly the moment the listener is least inclined to forgive one.

**The other way to do this is time diversity** — transmit every packet twice, a
few seconds apart, the way a satellite service rides out a bridge. It protects
the same interval with no startup wait, and it costs exactly half the payload
rate:

| Profile / MODCOD | Max audio | Max audio with a full repeat |
|---|---|---|
| `RADIO` 256QAM 5/6 | 55.9 kbps | 27.3 kbps |
| `WIDE48` 64QAM 5/6 | 70.6 kbps | 34.7 kbps |
| `OFDM48` 64QAM 5/6 | 64.5 kbps | 31.7 kbps |

Spare channel capacity does not buy it. A link configured sensibly runs a few
kbps under its ceiling, and a few kbps repeats a few per cent of the packets —
fragments, not audio. Halving the codec rate buys it; nothing else does.

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
preset deliberately exceeds it, and is kept as a failing test so the limit
stays measured rather than assumed.

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
channel presets — including all four OFDM profiles at all nine carrier counts,
which is where the `acoustic` preset starts sorting them by prefix length.
`tools/dfree.py` confirms every punctured rate matches its published free
distance.

A profile name carries its carrier count, so the end-to-end check runs at any
of them:

```bash
python tools/selftest.py OFDM48-32 32k opus
```

`tools/conv_check.py` runs the Viterbi decoder against a plain textbook
implementation over every rate and requires them to agree on every bit, and
`tools/dfree.py` checks the code itself.

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
`libfdk_aac` only if you want HE-AAC. VB-CABLE if you want to loop the two
apps together without hardware.

## Layout

```
qamcore/            the wire format — one copy, both ends
  profiles.py       channel profiles, MODCOD ladder, frame capacity
  constellation.py  Gray-coded QAM, soft LLR demapping
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
  codec.py          Opus and HE-AAC through ffmpeg, probing, passthrough
  streams.py        .pls/.m3u playlists folded into stations and their rates
  icy.py            Icecast/Shoutcast now-playing metadata
  ofdm.py           OFDM: geometry, modulator, demodulator, coded frames
  linkkey.py        the physical layer as one copyable token
  ogg.py            Ogg pages, so Opus packets can be carried bare
  scope.py          telemetry for the spectrum and constellation displays
  webui.py          local UI: static files, SSE telemetry, JSON control
tx.py               transmitter
rx.py               receiver
web/                the two pages, styling, scopes
tools/              selftest, loopback, rate card, thresholds, decoder check
```
