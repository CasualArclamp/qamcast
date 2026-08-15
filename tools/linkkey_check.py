"""Link key format: round trip, typo rejection and formatting tolerance.

    python tools/linkkey_check.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from qamcore import linkkey, profiles  # noqa: E402


def main() -> int:
    ok = True

    print("-- every preset, and every carrier count of every OFDM preset")
    keys = []
    for name, p in profiles.PROFILES.items():
        counts = profiles.OFDM_CARRIER_CHOICES if p.is_ofdm else (None,)
        for c in counts:
            q = profiles.with_carriers(p, c) if c else p
            key = linkkey.encode(q)
            back = linkkey.to_profile(linkkey.decode(key))
            if not linkkey._same_link(q, back):
                print(f"   MISMATCH {q.name}: {key} rebuilt as a different link")
                ok = False
            keys.append(key)
    print(f"   {len(keys)} keys, every one rebuilding the same physical layer")

    print("-- links that are not any preset")
    for sr, sym, ro, pilot, frame in ((48000, 12000, 0.25, 32, 2048),
                                      (44100, 11025, 0.35, 128, 8192),
                                      (96000, 24000, 0.15, 64, 4096)):
        p = profiles.make_profile(sr, sym, ro, pilot_spacing=pilot,
                                  frame_symbols=frame)
        key = linkkey.encode(p)
        back = linkkey.to_profile(linkkey.decode(key))
        good = linkkey._same_link(p, back)
        ok &= good
        print(f"   {key}  {linkkey.describe(linkkey.decode(key))}"
              f"  {'ok' if good else 'MISMATCH'}")

    print("-- a key only labels itself with a preset it matches exactly")
    # These three have a preset's card rate, symbol rate and roll-off but the
    # automatic carrier, pilot spacing and frame length. Anything that matched
    # on the first three alone would mislabel them, and a receiver trusting
    # that label would tune somewhere the transmitter is not.
    for sr, sym, ro in ((48000, 9600, 0.25), (96000, 32000, 0.20),
                        (48000, 8000, 0.30)):
        info = linkkey.decode(linkkey.encode(profiles.make_profile(sr, sym, ro)))
        label = linkkey.profile_name(info)
        if label:
            print(f"   MISLABELLED {sr}/{sym}/{ro} as {label}")
            ok = False
    for name, p in profiles.PROFILES.items():
        info = linkkey.decode(linkkey.encode(p))
        if linkkey.profile_name(info) != name:
            print(f"   {name} did not label itself "
                  f"({linkkey.profile_name(info) or 'nothing'})")
            ok = False
    print("   every preset labels itself, no custom link borrows a preset's name")

    base = linkkey.encode(profiles.PROFILES["OFDMRADIO"])
    body = base.split("-")[1]

    print("-- formatting a human might introduce")
    for v in (base.lower(), base.replace("-", ""), f"  {base}  ",
              "QC2-" + body.replace("1", "I").replace("0", "O")):
        try:
            linkkey.decode(v)
        except linkkey.LinkKeyError as exc:
            print(f"   REJECTED {v!r}: {exc}")
            ok = False
    print("   lower case, no dash, surrounding space, I/L/O/U lookalikes")

    print("-- a mistyped key must not tune the receiver somewhere wrong")
    caught = total = 0
    for pos in range(len(body)):
        for repl in "0123456789ABCDEFGHJKMNPQRSTVWXYZ":
            if repl == body[pos]:
                continue
            total += 1
            try:
                linkkey.decode("QC2-" + body[:pos] + repl + body[pos + 1:])
            except linkkey.LinkKeyError:
                caught += 1
    print(f"   single-character typos rejected: {caught}/{total} "
          f"({100 * caught / total:.1f}%)")
    if caught / total < 0.99:
        ok = False

    print("-- junk")
    for junk in ("", "hello", "QC2-SHORT", "QC9-ABCDEFGHJKMNPQ", "QC2-",
                 "QC1-27G02H0X0WCHP"):
        try:
            linkkey.decode(junk)
            print(f"   FAIL: accepted {junk!r}")
            ok = False
        except linkkey.LinkKeyError:
            pass
        except Exception as exc:                       # noqa: BLE001
            print(f"   FAIL: {junk!r} raised {type(exc).__name__}, "
                  f"not LinkKeyError")
            ok = False
    print("   empty, prose, wrong length, wrong version, an old QC1 key")

    print("\nPASS" if ok else "\nFAILURES")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
