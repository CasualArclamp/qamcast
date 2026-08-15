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
        counts = profiles.OFDM_CARRIER_CHOICES if p.is_ofdm else (0,)
        for c in counts:
            page = {"profile": name, "carriers": c,
                    "sample_rate": p.sample_rate, "symbol_rate": p.symbol_rate,
                    "rolloff": p.rolloff}
            ok &= check(f"{name}/{c}" if c else name, page, verbose)
            n += 1

    # Hand-dialled links, including three whose card rate, symbol rate and
    # roll-off are a preset's exactly. Those are the ones a key that named a
    # preset instead of a link would get wrong.
    for label, sr, sym, ro in (("custom = RADIO's numbers", 48000, 9600, 0.25),
                               ("custom = WIDE's numbers", 96000, 32000, 0.20),
                               ("custom = ACOUSTIC's numbers", 48000, 8000, 0.30),
                               ("custom, unlike any preset", 48000, 12000, 0.25),
                               ("custom on a 44.1 kHz card", 44100, 11025, 0.35)):
        page = {"profile": "CUSTOM", "carriers": 0, "sample_rate": sr,
                "symbol_rate": sym, "rolloff": ro}
        ok &= check(label, page, verbose)
        n += 1

    print(f"\n{n} links copied across by key")
    print("PASS" if ok else "FAILURES")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
