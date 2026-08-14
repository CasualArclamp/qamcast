"""List sound devices.

Deliberately imports nothing from qamcore. The launchers call this every time
they start, and pulling in numpy and numba to print a list of device names
adds seconds to something that should be instant.

    python tools/devices.py            MME only -- one entry per device
    python tools/devices.py out        outputs only
    python tools/devices.py --all      every host API

Windows enumerates each card once per host API, so a machine with a dozen
devices shows fifty entries. MME is the default here because it lists every
device exactly once and accepts any sample rate. The catch is that it accepts
them by **resampling**, which costs signal quality; WASAPI runs the card at
its real rate or refuses. Use --all when you want to pick a WASAPI entry.
"""

from __future__ import annotations

import sys

RATES = (44100, 48000, 96000)
DEFAULT_API = "MME"


def accepts(index: int, rate: int, output: bool) -> bool:
    import sounddevice as sd
    try:
        if output:
            sd.check_output_settings(device=index, samplerate=rate, channels=1)
        else:
            sd.check_input_settings(device=index, samplerate=rate, channels=1)
        return True
    except Exception:
        return False


def show(which: str, every_api: bool) -> int:
    try:
        import sounddevice as sd
    except ImportError:
        print("sounddevice is not installed:  pip install sounddevice", file=sys.stderr)
        return 1

    devices = sd.query_devices()
    try:
        default_in, default_out = sd.default.device
    except Exception:
        default_in = default_out = -1

    for output in ([False, True] if which == "all" else [which == "out"]):
        kind = "OUTPUT" if output else "INPUT"
        chan = "max_output_channels" if output else "max_input_channels"
        default = default_out if output else default_in
        rows = []
        for i, d in enumerate(devices):
            if d[chan] < 1:
                continue
            api = sd.query_hostapis(d["hostapi"])["name"]
            if not every_api and DEFAULT_API not in api:
                continue
            name = d["name"] if not every_api else f"{d['name']} [{api}]"
            marks = "  ".join(
                f"{'yes' if accepts(i, r, output) else ' - ':>4}" for r in RATES)
            rows.append((i, name[:44], marks, i == default))

        print()
        print(f"{kind} devices" + " " * 32 + "  44.1k  48k  96k")
        print("-" * 72)
        for i, name, marks, is_default in rows:
            print(f"{i:>3}{' *' if is_default else '  '} {name:<44} {marks}")
        if not rows:
            print("  none")

    print()
    print("  * = Windows default")
    if not every_api:
        print(f"  Showing {DEFAULT_API} only. Run  devices.bat --all  for every host API;")
        print("  a WASAPI entry runs the card at its real rate instead of resampling.")
    print()
    return 0


if __name__ == "__main__":
    args = [a.lower() for a in sys.argv[1:]]
    every = "--all" in args or "-a" in args
    rest = [a for a in args if not a.startswith("-")]
    which = rest[0] if rest and rest[0] in ("in", "out") else "all"
    raise SystemExit(show(which, every))
