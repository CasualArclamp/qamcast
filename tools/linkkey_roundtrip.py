"""Transmitter page -> link key -> receiver page -> Start, as the apps do it.

The format test next door proves a key survives being encoded and decoded.
This one proves the thing that actually matters: that copying the key across
gives the receiver *the same physical layer the transmitter is using*. Those
are different claims, and the gap between them is where the first version of
this failed -- a key that round-tripped perfectly still rebuilt a hand-dialled
48 kHz / 9600 Bd / 0.25 link as RADIO, 500 Hz away and with twice the frame.

    python tools/linkkey_roundtrip.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from qamcore import linkkey, profiles  # noqa: E402

import rx  # noqa: E402
import tx  # noqa: E402


def check(label: str, page: dict, verbose: bool) -> bool:
    """One trip: what the TX page shows, through a key, to what RX would build."""
    sent = tx.build_profile(page)
    plan = tx.solve({**page, "driver": "preset", "modulation": "QPSK",
                     "code_rate": "1/2", "bitrate": 0})
    if plan.get("error"):
        print(f"BAD {label:26s} solve: {plan['error']}")
        return False
    key = plan.get("link_key")
    if not key:
        print(f"BAD {label:26s} no link key in the solve response")
        return False

    # The mode the page asked for is the mode it must get, from both paths.
    # This is the check that was missing: with a named preset the family rides
    # along on the preset itself, so everything looked fine, while a CUSTOM
    # page carrying mode=apsk silently built square QAM -- wrong key, wrong
    # transmission, wrong demodulator, and nothing said so.
    want = page.get("mode") or "sc"
    if not sent.is_ofdm and sent.mode != want:
        print(f"BAD {label:26s} page asked for {want!r}, Start builds "
              f"{sent.mode!r}")
        return False
    if not sent.is_ofdm and plan.get("mode") != want:
        print(f"BAD {label:26s} page asked for {want!r}, the panel describes "
              f"{plan.get('mode')!r}")
        return False

    # The key the panel displays must describe the link the transmitter is
    # actually going to build. Nothing enforced that, and they diverged: the
    # panel took the pilot spacing, the carrier and the frame length from the
    # preset it was solving against, while Start rebuilt the profile from the
    # dials alone -- and the dials cannot express any of the three. A key
    # copied across then tuned the receiver to pilots the transmitter was not
    # sending. Sync still ran at 0.99, nothing locked, and the constellation
    # smeared into rings because the phase interpolated between pilots that
    # were payload symbols.
    built = tx.build_profile(page)
    keyed = linkkey.to_profile(linkkey.decode(key))
    if not linkkey._same_link(built, keyed):
        print(f"BAD {label:26s} the panel's key describes a different link "
              f"from the one Start builds")
        print(f"      key   pilot {keyed.pilot_spacing} frame "
              f"{keyed.frame_symbols} carrier {keyed.carrier:.0f}")
        print(f"      Start pilot {built.pilot_spacing} frame "
              f"{built.frame_symbols} carrier {built.carrier:.0f}")
        return False

    # exactly what rx.py does with a pasted key, then what Start sends
    read = rx.Receiver().read_link_key({"key": key})
    if read.get("error"):
        print(f"BAD {label:26s} receiver rejected the key: {read['error']}")
        return False
    got = tx.build_profile({"link_key": key})

    same = linkkey._same_link(sent, got)
    if not same or verbose:
        def show(p):
            lo, hi = p.band
            extra = (f"{p.ofdm_carriers} carriers" if p.is_ofdm
                     else f"{p.symbol_rate} Bd pilot{p.pilot_spacing} "
                          f"frame{p.frame_symbols}")
            return f"{lo:.0f}-{hi:.0f} Hz {extra}"
        print(f"{'ok ' if same else 'BAD'} {label:26s} {key}")
        print(f"      tx {show(sent)}")
        if not same:
            print(f"      rx {show(got)}")
    return same


def main() -> int:
    verbose = "-v" in sys.argv
    ok = True
    n = 0

    for name, p in profiles.PROFILES.items():
        if not p.is_ofdm:
            page = {"profile": name, "carriers": 0, "mode": p.mode,
                    "sample_rate": p.sample_rate, "symbol_rate": p.symbol_rate,
                    "rolloff": p.rolloff}
            ok &= check(name, page, verbose)
            n += 1
            continue
        for s in profiles.OFDM_SPACING_CHOICES:
            for c in profiles.OFDM_CARRIER_CHOICES:
                # Only combinations the band can hold; the rest are refused at
                # the dial, so a key for them cannot arise.
                try:
                    profiles.with_carriers(profiles.with_spacing(p, s), c)
                except ValueError:
                    continue
                page = {"profile": name, "carriers": c, "spacing": s,
                        "mode": p.mode, "sample_rate": p.sample_rate,
                        "symbol_rate": p.symbol_rate, "rolloff": p.rolloff}
                ok &= check(f"{name}/{c}@{s}", page, verbose)
                n += 1

    # Hand-dialled links, including three whose card rate, symbol rate and
    # roll-off are a preset's exactly. Those are the ones a key that named a
    # preset instead of a link would get wrong.
    #
    # Each is run on both point sets. A CUSTOM page is the only place the
    # family has nowhere to hide -- there is no preset carrying it -- so it is
    # the only place the mode has to travel from the page to the server on its
    # own, and it is exactly where it used to be dropped.
    for label, sr, sym, ro in (("custom = RADIO's numbers", 48000, 9600, 0.25),
                               ("custom = WIDE's numbers", 96000, 32000, 0.20),
                               ("custom = ACOUSTIC's numbers", 48000, 8000, 0.30),
                               ("custom, unlike any preset", 48000, 12000, 0.25),
                               ("custom on a 44.1 kHz card", 44100, 11025, 0.35)):
        for mode in ("sc", "apsk"):
            page = {"profile": "CUSTOM", "carriers": 0, "sample_rate": sr,
                    "symbol_rate": sym, "rolloff": ro, "mode": mode}
            ok &= check(f"{label} [{mode}]", page, verbose)
            n += 1

    ok &= deviation(verbose)
    ok, n = bands(verbose, ok, n)

    print(f"\n{n} links copied across by key")
    print("PASS" if ok else "FAILURES")
    return 0 if ok else 1


def bands(verbose: bool, ok: bool, n: int) -> tuple[bool, int]:
    """Custom links described only by the band they have to fit.

    The band is now the whole of a custom link, in both modes, so it is the
    path most of them take. Two things have to hold: the fitted link must sit
    inside the band it was given -- spilling past an edge the operator named is
    the failure the OFDM fitter already refuses -- and it must survive a key,
    which for OFDM means a carrier count that is usually nowhere near a ladder
    rung and travels as "fill this band" instead.
    """
    print("\n-- custom links given only a band")
    cases = [(44100, 400, 18000), (48000, 300, 20000), (96000, 3000, 42000),
             (48000, 1000, 13000), (44100, 3000, 12000), (48000, 500, 6000),
             (44100, 8000, 16000), (96000, 200, 20000)]
    for sr, lo, hi in cases:
        for mode in ("sc", "apsk", "ofdm"):
            page = {"profile": "CUSTOM", "mode": mode, "sample_rate": sr,
                    "band_lo": lo, "band_hi": hi, "carriers": 0}
            built = tx.build_profile(page)
            blo, bhi = built.band
            # Half a hertz of slack: the band edges are whole hertz and the
            # occupied edges are not.
            if blo < lo - 0.5 or bhi > hi + 0.5:
                print(f"BAD {sr} {lo}-{hi} [{mode}] fitted "
                      f"{blo:.0f}-{bhi:.0f}, outside the band it was given")
                ok = False
            ok &= check(f"band {lo}-{hi} @{sr//1000}k [{mode}]", page, verbose)
            n += 1
    return ok, n


def deviation(verbose: bool) -> bool:
    """The page's own two-step: solve against a preset, then Start as Custom.

    This is not a contrived sequence, it is what the transmit page does. The
    solver is allowed to move the symbol rate, and once it has, the preset name
    no longer describes the link -- so the page drops to Custom and Start sends
    Custom. The panel, and the key it is showing, were computed against the
    preset; the profile Start builds is computed from the dials. The dials
    cannot express a pilot spacing, a carrier or a frame length, so all three
    silently reverted to their automatic values while the key kept the
    preset's.

    Measured, that put the transmitter on pilot spacing 64 and the receiver --
    following the key -- on 128. The receiver then interpolated carrier phase
    between symbols that were payload rather than pilots, which rotates the
    constellation into smeared rings, holds sync at 0.99, and never locks.
    """
    print("\n-- the page's solve-then-deviate sequence")
    good = True
    for name, p in profiles.PROFILES.items():
        if p.is_ofdm:
            continue        # no symbol rate for the solver to move
        # A bitrate low enough that the solver narrows the symbol rate, which
        # is what makes the page call deviate().
        panel = tx.solve({"profile": name, "mode": p.mode, "driver": "bitrate",
                          "sample_rate": p.sample_rate,
                          "symbol_rate": p.symbol_rate, "rolloff": p.rolloff,
                          "bitrate": 16000, "modulation": "QPSK",
                          "code_rate": "1/2", "locks": []})
        if panel.get("error"):
            continue
        if "symbol_rate" not in panel.get("moved", []):
            continue        # nothing deviated, so nothing to check here
        # What the page sends to Start afterwards: Custom, and the dials.
        start = tx.build_profile({
            "profile": "CUSTOM", "mode": p.mode,
            "link_key": panel.get("link_key"),
            "sample_rate": panel["sample_rate"],
            "symbol_rate": panel["symbol_rate"], "rolloff": panel["rolloff"]})
        keyed = linkkey.to_profile(linkkey.decode(panel["link_key"]))
        same = linkkey._same_link(start, keyed)
        good &= same
        if not same or verbose:
            print(f"{'ok ' if same else 'BAD'} {name} -> "
                  f"{panel['symbol_rate']} Bd")
            if not same:
                print(f"      panel key  pilot {keyed.pilot_spacing} "
                      f"frame {keyed.frame_symbols} "
                      f"carrier {keyed.carrier:.0f}")
                print(f"      on air     pilot {start.pilot_spacing} "
                      f"frame {start.frame_symbols} "
                      f"carrier {start.carrier:.0f}")
    if good:
        print("   what the panel shows is what goes on air")
    return good


if __name__ == "__main__":
    raise SystemExit(main())
