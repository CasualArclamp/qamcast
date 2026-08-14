"""Print the rate card for every channel profile.

    python tools/rates.py            all profiles
    python tools/rates.py WIDE       just one

This is the table both ends compute from. If a number here surprises you, the
wire format is what changed -- not the display.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from qamcore import profiles  # noqa: E402


def main(argv: list[str]) -> int:
    names = [a.upper() for a in argv[1:]] or list(profiles.PROFILES)
    for name in names:
        try:
            p = profiles.get_profile(name)
        except ValueError as e:
            print(e, file=sys.stderr)
            return 1
        print(profiles.describe(p))
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
