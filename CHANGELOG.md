# Changelog

## v1.3.1 — a switch for turning energy dispersal off

**Added**

- **Energy dispersal can be switched off from the testing panel**, which is the
  fastest way to see what it does. Measured spectral flatness of the
  transmitted frame (1.0 noise-like, small tonal):

  | Payload | Dispersal on | off |
  |---|---|---|
  | Compressed audio, `FM44` | 0.033 | 0.023 |
  | **Digital silence, `FM44`** | **0.033** | **0.003** |
  | Digital silence, `OFDM44` | 0.163 | 0.048 |

  Eleven times more tonal on silence, essentially unchanged on music — the
  scrambler's whole case in two rows.
- Safe to flip while on air: it travels in a spare frame flag, so the receiver
  follows within a frame and the waterfall changes while you watch. Decoding is
  unaffected, measured bit-exact in both modes with it off.
- The receive panel says so when a transmitter has it off, since the signal
  looks wrong on a waterfall and is not.

The signalling block stays scrambled either way — the flag lives inside it, so
a receiver needing the flag before it could read the block would have nowhere
to start. No wire format change: the flag bit was already spare.

## v1.3.0 — a custom link is just its band

**Changed**

- **Custom mode now asks for two numbers: the lowest and highest frequency your
  path passes.** Everything else is fitted to them — on a single carrier the
  symbol rate, roll-off and carrier frequency; in OFDM the subcarrier count.
  That is the only question about a link an operator can answer without knowing
  anything about this modem, and the rest follows with nothing left over.
  `--band 400,18000` on both apps, or the two boxes on both pages.
- **Custom works in OFDM now.** It was withdrawn in v1.1.0 because a symbol
  rate, a roll-off and a carrier describe nothing an OFDM link has. A band
  describes both.
- **The subcarrier spacing dropdown is gone**, fixed at 100 Hz — the measured
  middle of the useful range, and the one sensible answer for a path nobody has
  characterised. Efficiency moves 12% across the whole span while echo and
  drift tolerance move sixteen-fold, so it was a dial with one right setting.
  Presets that want another carry it; `--spacing` remains for measurement.
- The derived symbol rate and roll-off are shown but not editable in band mode,
  since they are outputs.

**Fitting rules**, in order: fastest symbol rate that fits, then the most
forgiving roll-off that still reaches it. The rates are quantised by the card,
so once a rung is reached a tighter roll-off buys nothing and the extra timing
margin is free. A 2–12 kHz band on a 44.1 kHz card comes out at 7350 Bd with
α 0.35, filling 9.92 kHz of the 10 available.

**Link key** — a band-filling carrier count is rarely a ladder rung, so it
travels as "as many as this band holds", derived identically at both ends from
the band and spacing already in the record. No format change; existing keys are
byte-identical.

**Fixed**

- A link key naming a band-filled OFDM link was applied one ladder rung
  narrower than it said: the panel fed the key's own count back through the
  rung table, and 175 carriers came out as 160.
- Asking for a band wider than the card can carry is refused with the number
  that is wrong, instead of being clamped and then reported as an aliasing
  warning about an edge you never typed.
- `tools/pagecheck.py` follows one hop of indirection, so a control read
  through a helper is no longer a false positive needing an exemption.

## v1.2.0 — selectable interleaver depth

**Added**

- **Interleaver depth is a dial: 2, 6, 12 or 24 seconds**, six being the
  previous fixed value and still the default. Dropdown on the transmit page,
  `--interleave` on the CLI.
- It **does not need matching by hand.** The depth rides in the signalling
  block, so the receiver reads it off the first frame it decodes and
  reconfigures itself — same arrangement as MODCOD. The receive panel reports
  what it is following.

**Measured** — `FM44` at 64QAM 5/6, longest dropout costing no audio at all:

| Depth | Rides out | Wait at startup |
|---|---|---|
| 2 s | 61 ms | 2.0 s |
| 6 s | 103 ms | 6.0 s |
| 12 s | 211 ms | 12.1 s |
| 24 s | 422 ms | 24.1 s |

Past that threshold deeper is *worse* — once a burst overwhelms the code,
spreading it wider turns a concentrated loss into a diffuse one. At a 1 s
dropout the 2 s rung lost 61 audio packets and the 24 s rung lost 492.

**Fixed**

- The interleaver geometry overshot badly on long delays: it maximised
  branches first and rounded the increment afterwards, and once branches
  saturated at 255 one step of the increment was seven seconds. A 12 s request
  came back as 14.8 s. It now searches both, preferring the most branches among
  the geometries that hit the depth within 5% — so the delivered depth is
  within 4.5% everywhere, and the branch count *rises* at the deep end (230 at
  12 and 24 s, where the old rule would have taken 255 with a 23% overshoot).

**Wire format**

- Signalling grows from six bytes to seven (two bits of depth, six reserved),
  and `WIRE_VERSION` goes to 2. Old and new builds will not interoperate.

## v1.1.1 — the transmitter now sends what its link key says

**Fixed**

- **The panel and the transmission could describe different links.** The panel
  solves against the selected preset and takes its pilot spacing, carrier and
  frame length; Start rebuilt from the dials, which cannot express any of the
  three. Once the solver moved the symbol rate — enough on its own to drop the
  page to Custom — the transmitter went out on pilot spacing 64 while the key
  it was showing said 128.
- Copying that key to a receiver made it interpolate carrier phase between
  symbols that were payload rather than pilots: **sync held at 0.99, the
  constellation smeared into rings, and nothing ever locked.** Measured, the
  mismatch costs 30 dB of EVM (52.4 → 22.6), which is why it limped on a clean
  virtual cable and failed on anything real. It affected QAM as well as APSK.
- Start now builds from the key the panel is displaying, so the two cannot
  part company.

**Added**

- The receiver names this failure instead of showing a blank panel: a strong
  correlation peak with nothing decoding means the card rate, symbol rate,
  roll-off and carrier are already right, and what is left to check is the
  pilot spacing, the frame length and the mode.
- `tools/linkkey_roundtrip.py` runs the page's own solve-then-deviate sequence
  and requires that what the panel shows is what goes on air.

## v1.1.0 — OFDM: spacing and carrier count are now two dials

**Changed — the carrier count means bandwidth now, not spacing**

- The band used to be fixed and the count divided it, so the count *was* the
  subcarrier spacing: it moved echo tolerance and drift tolerance in opposite
  directions and left the bitrate alone. Now the **spacing** is its own dial and
  sets ruggedness, and the **count** sets the occupied width and so the bitrate.
  Halving the count halves the bandwidth and the rate and changes nothing else.
- New **Subcarrier spacing** dial: 400 / 300 / 200 / 150 / 100 / 75 / 50 / 25 Hz,
  on both pages and as `--spacing` on both apps. Default 100 Hz, measured.
- Finer carrier ladder (24…384 in 14 steps), because each rung is now a width.
- Asking for more carriers than the band holds is refused rather than allowed to
  spill past the upper edge — on an FM path that edge is the 19 kHz pilot.
- Profile names carry both dials: `OFDM48-64@100`.
- **Link keys are now QC3.** QC2 keys are rejected, not reinterpreted: the same
  count means a different link under the new dial.

**Added**

- `OFDMREVERB` — 25 Hz spacing, half the bandwidth, eight times the echo
  tolerance. It decodes the `reverb` channel (6 ms echoes), which the README
  documented as the case this modem fails: 89/90 frames and 180/180 audio
  packets bit-exact, against 0 for single carrier and 7 for `OFDM48`.
- `tools/spacing.py` — tabulates and decodes at every spacing, so the choice is
  measured rather than argued.

**Measured**

- Spectral efficiency moves 12% across a 16× change in spacing while echo
  tolerance moves 16×, so spacing is chosen for the channel, not the rate.
- The 25 Hz cliff is real: 11 Hz of offset tolerance against the `radio`
  channel's 15 Hz drift decodes 0 frames in 40.

## v1.0.1 — APSK mode fixes

**Fixed**

- **Selecting APSK now actually transmits APSK.** The Mode dropdown filtered
  the preset list but was never sent to the server, so any edit that dropped
  the preset to Custom silently reverted the link to square QAM — wrong link
  key, wrong transmission, wrong demodulator, and nothing said so.
- **Constellations are named for the mode**: 16APSK / 64APSK / 256APSK instead
  of 16QAM everywhere, in the dropdown, the status line and the rate card.
- **Switching mode lands on the 44.1 kHz profile** (APSK44, OFDM44) rather than
  the 96 kHz one. Landing a 44.1 kHz card on a 96 kHz geometry is what made the
  receiver false-sync and the constellation spin.
- **Applying a link key sets the Mode selector from the key**, and refills the
  presets, so an APSK or OFDM key no longer lands on a page filtered to QAM.
- A link key in force now overrides a stale Mode selector rather than the
  other way round.
- Receiver roll-off list is built from the profiles; 0.30 was missing, so
  ACOUSTIC was planned at 0.25.
- Custom dropped from the OFDM preset list — it cannot describe an OFDM link.

**Added**

- `rx.py --mode` and `rx.py --link-key`.
- `tx.py --mode`, and `--modulation` accepts the APSK spellings.
- `tools/pagecheck.py` checks that every dial in the Link panel is sent to both
  `solve` and `start`, which is the check that would have caught the above.

## v1.0.0

First release.
