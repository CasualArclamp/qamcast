# Changelog

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
