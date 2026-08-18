"""Machine-specific paths, kept out of the repository.

Where ffmpeg lives and where the station playlists are kept is a property of
one computer, not of the project, so it does not belong in the source. Three
places are looked at, in order, and the first answer wins:

    1. an environment variable      QAMCAST_FFMPEG, QAMCAST_STATIONS
    2. qamcast.local.json           beside the project, and gitignored
    3. the project's own folders    ffmpeg/ and stations/

The middle one exists so that a working setup survives a clone without anyone
having to remember to set anything, and without a path from one machine ending
up in everybody else's checkout.
"""

from __future__ import annotations

import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCAL_FILE = os.path.join(ROOT, "qamcast.local.json")

# Environment variable per setting, checked before the file.
ENV = {"ffmpeg": "QAMCAST_FFMPEG", "stations": "QAMCAST_STATIONS",
       "exhale": "QAMCAST_EXHALE"}


def _file() -> dict:
    """qamcast.local.json, or an empty dict. Never raises: a broken local file
    should cost you the shortcut, not the program."""
    try:
        with open(LOCAL_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def setting(name: str, default: str | None = None) -> str | None:
    """One setting, from the environment or the local file."""
    env = os.environ.get(ENV.get(name, ""))
    if env:
        return env
    value = _file().get(name)
    return value if isinstance(value, str) and value else default


def project_dir(*parts: str) -> str:
    return os.path.join(ROOT, *parts)
