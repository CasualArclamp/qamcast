# Changelog

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
