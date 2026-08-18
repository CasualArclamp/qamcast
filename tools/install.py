"""First run: get this machine to the point where the self test passes.

    python tools/install.py           ask about each step
    python tools/install.py --yes     take the defaults, ask nothing
    python tools/install.py --xhe     include xHE-AAC without asking

Five steps, in the order they depend on each other:

    1  Python           a version that can run this
    2  packages         numpy, scipy, numba, sounddevice
    3  ffmpeg           the codec layer, and what this one can encode
    4  xHE-AAC          optional, and the only step needing a compiler
    5  self test        proof, rather than a list of things that looked fine

Nothing is downloaded or installed without being asked first, and every step
says what it is about to do before doing it. Steps 3 and 4 can be declined and
still leave a working install -- Opus needs nothing but ffmpeg, and ffmpeg is
the only hard requirement outside pip.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MIN_PYTHON = (3, 11)

# Two of these carry the sound card and the JIT, and both are worth naming
# when they fail: an install that silently lacks numba runs at a fraction of
# real time and looks like a slow computer.
PACKAGES = ("numpy", "scipy", "numba", "sounddevice")

ASK = True


def head(n: int, text: str) -> None:
    print()
    print(f"  [{n}/5]  {text}")
    print("  " + "-" * 62)


def say(text: str = "") -> None:
    print(f"    {text}" if text else "")


def ask(question: str, default: bool = True) -> bool:
    if not ASK:
        say(f"{question} -- taking the default: {'yes' if default else 'no'}")
        return default
    suffix = "Y/n" if default else "y/N"
    try:
        got = input(f"    {question} [{suffix}]: ").strip().lower()
    except EOFError:
        return default
    if not got:
        return default
    return got.startswith("y")


def run(cmd: list[str], **kw) -> int:
    say(f"$ {' '.join(cmd)}")
    print()
    try:
        return subprocess.run(cmd, **kw).returncode
    except (OSError, subprocess.SubprocessError) as exc:
        say(f"could not run it: {exc}")
        return 1


# --------------------------------------------------------------------------

def step_python() -> bool:
    head(1, "Python")
    v = sys.version_info
    say(f"{v.major}.{v.minor}.{v.micro} at {sys.executable}")
    if (v.major, v.minor) < MIN_PYTHON:
        say(f"too old -- this needs {MIN_PYTHON[0]}.{MIN_PYTHON[1]} or later, "
            f"for the typing syntax the source uses throughout.")
        return False
    say("fine.")
    return True


# sounddevice is a wrapper around PortAudio, and pip ships that library inside
# the Windows and macOS wheels but not the Linux one -- there it is expected to
# come from the distribution. So on Linux `pip install sounddevice` succeeds
# and `import sounddevice` then fails with a message about a missing library,
# which reads like a broken install rather than a missing package.
PORTAUDIO = {
    "apt": "sudo apt-get install -y libportaudio2",
    "dnf": "sudo dnf install -y portaudio",
    "pacman": "sudo pacman -S --needed portaudio",
    "zypper": "sudo zypper install -y libportaudio2",
}


def check_portaudio() -> bool:
    """Whether sounddevice can actually reach a sound system."""
    try:
        import sounddevice           # noqa: F401
        return True
    except ImportError:
        return False
    except Exception as exc:
        say(f"sounddevice is installed but cannot load: {exc}")
        if sys.platform.startswith("linux"):
            say("")
            say("That is PortAudio, which pip does not ship on Linux. Install")
            say("it from your distribution:")
            for tool, cmd in PORTAUDIO.items():
                if shutil.which(tool):
                    say(f"  {cmd}")
                    break
            else:
                say("  look for a portaudio or libportaudio2 package")
        say("")
        say("Without it both apps still work against WAV files; only the")
        say("sound-card paths are closed.")
        return False


def step_packages() -> bool:
    head(2, "Python packages")
    missing = []
    for name in PACKAGES:
        try:
            __import__(name)
            say(f"{name:<14} already here")
        except ImportError:
            missing.append(name)
            say(f"{name:<14} missing")
    if not missing:
        return check_portaudio() or True    # a warning, not a failure
    print()
    if not ask(f"Install {', '.join(missing)} with pip?"):
        say("skipped. The apps will not start without them.")
        return False
    req = os.path.join(ROOT, "requirements.txt")
    cmd = [sys.executable, "-m", "pip", "install"]
    cmd += ["-r", req] if os.path.exists(req) else list(PACKAGES)
    if run(cmd):
        say("pip did not finish. If this is a system Python, a virtual "
            "environment usually fixes it:")
        say(f"  {sys.executable} -m venv .venv")
        return False
    check_portaudio()
    return True


def winget_ffmpeg() -> bool:
    """Offer ffmpeg through Windows' own package manager, if it is there."""
    if sys.platform != "win32" or not shutil.which("winget"):
        return False
    print()
    say("winget can install ffmpeg for you. It is Microsoft's package")
    say("manager and the package is the Gyan build, which is the usual one")
    say("on Windows. It has libopus, so Opus works; it does not have")
    say("libfdk_aac, so HE-AAC encoding will not.")
    if not ask("Install ffmpeg with winget?", default=False):
        return False
    return run(["winget", "install", "--id", "Gyan.FFmpeg", "-e",
                "--accept-package-agreements",
                "--accept-source-agreements"]) == 0


def step_ffmpeg() -> bool:
    head(3, "ffmpeg")
    sys.path.insert(0, ROOT)
    try:
        from qamcore import codec
    except ImportError as exc:
        say(f"cannot check yet -- {exc}")
        say("finish step 2 and run this again.")
        return False

    try:
        exe = codec.find_ffmpeg()
    except codec.CodecError:
        say("not found on PATH.")
        if not winget_ffmpeg():
            say("")
            say("ffmpeg is the one requirement outside pip -- every codec")
            say("here reads and writes through it. Either put one in the")
            say("project's ffmpeg/ folder, or install it and run this again:")
            say("  https://ffmpeg.org/download.html")
            say("See ffmpeg/README.md for which build, and where else it looks.")
            return False
        say("installed. You may need a new terminal for PATH to catch up.")
        try:
            exe = codec.find_ffmpeg()
        except codec.CodecError:
            say("still not on this shell's PATH. Open a new terminal and "
                "run this again.")
            return False

    say(f"found {exe}")
    print()
    say("what it can encode, asked by trying each one:")
    for c in codec.codec_options(exe):
        mark = "yes" if c["encode"] else "no "
        say(f"  {mark}   {c['label']:<9} {c['why']}")
        if not c["encode"] and c["value"] != "xhe":
            say(f"        {c['unavailable']}")
    print()
    say("Opus is the default and is all most installs need. HE-AAC wants an")
    say("ffmpeg built --enable-nonfree with libfdk_aac.")
    return True


def step_xhe(want: bool | None) -> bool:
    head(4, "xHE-AAC  (optional)")
    sys.path.insert(0, ROOT)
    try:
        from qamcore import exhale
    except ImportError as exc:
        say(f"cannot check -- {exc}")
        return False

    ok, why = exhale.usable()
    if ok:
        say(f"already built: {exhale.find_exhale()}")
        return True

    say("xHE-AAC is the strongest codec here where bits are scarce, and the")
    say("only one ffmpeg cannot make: the open-source Fraunhofer FDK ships a")
    say("USAC decoder and no USAC encoder. So it needs exhale, a small")
    say("open-source encoder, built from source. One time, about a minute.")
    print()
    say("You do not need this for Opus or HE-AAC, and you do not need it to")
    say("*receive* xHE-AAC either -- ffmpeg decodes it fine. Only to send it.")
    print()

    if want is None:
        want = ask("Set up xHE-AAC?", default=False)
    if not want:
        say("skipped. Run tools/build_exhale.py later if you change your mind.")
        return True

    have_cmake = bool(shutil.which("cmake"))
    have_cxx = bool(shutil.which("g++") or shutil.which("cl")
                    or os.path.exists(r"C:\msys64\mingw64\bin\g++.exe"))
    say(f"cmake            {'found' if have_cmake else 'MISSING'}")
    say(f"C++ compiler     {'found' if have_cxx else 'MISSING'}")
    if not (have_cmake and have_cxx):
        print()
        say("Both are needed to build it. On Windows the shortest route is")
        say("MSYS2 (https://www.msys2.org), then in its shell:")
        say("  pacman -S mingw-w64-x86_64-gcc mingw-w64-x86_64-cmake \\")
        say("            mingw-w64-x86_64-ninja")
        say("Then run: python tools/build_exhale.py")
        return True                      # not fatal; the rest still works

    if run([sys.executable, os.path.join(ROOT, "tools", "build_exhale.py")]):
        say("the build did not finish. Everything else still works; "
            "xHE-AAC will show as unavailable.")
    return True


def step_selftest() -> bool | None:
    """True if it ran and passed, False if it ran and failed, None if skipped.

    Kept apart because "we did not check" and "we checked and it works" are
    different things to tell someone at the end, and collapsing them is how an
    installer comes to claim a machine is ready when nothing was tried.
    """
    head(5, "self test")
    say("Runs the whole chain against a file -- generates a tone, encodes it,")
    say("modulates it to tx.wav, demodulates that back and checks the audio.")
    say("No sound card is touched. About a minute.")
    print()
    if not ask("Run it now?"):
        say("skipped. Run it later with:  python tools/selftest.py")
        return None
    return run([sys.executable, os.path.join(ROOT, "tools", "selftest.py"),
                "WIDE48", "64k", "opus"]) == 0


def main() -> int:
    global ASK
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--yes", action="store_true",
                    help="take every default without asking")
    group = ap.add_mutually_exclusive_group()
    group.add_argument("--xhe", action="store_true",
                       help="set up xHE-AAC without asking")
    group.add_argument("--no-xhe", action="store_true",
                       help="skip xHE-AAC without asking")
    a = ap.parse_args()
    ASK = not a.yes

    print()
    print("  " + "=" * 62)
    print("    QAMcast  --  first run")
    print("  " + "=" * 62)

    if not step_python():
        return 1
    packages = step_packages()
    ffmpeg = step_ffmpeg() if packages else False
    if packages:
        step_xhe(True if a.xhe else False if a.no_xhe else None)
    ready = packages and ffmpeg
    tested = step_selftest() if ready else None

    print()
    print("  " + "=" * 62)
    if tested is True:
        print("    Ready, and proved it. Next: devices.bat to find your sound")
        print("    card, then tx.bat and rx.bat in two windows.")
    elif tested is False:
        print("    The self test did not pass. Everything is installed, so")
        print("    the output above says what is actually wrong.")
    elif ready:
        print("    Installed, but nothing was run. Prove it when you like:")
        print("      python tools/selftest.py")
    else:
        print("    Not finished -- see the steps above. Nothing is broken;")
        print("    run this again once the missing piece is there.")
    print("  " + "=" * 62)
    print()
    return 0 if ready and tested is not False else 1


if __name__ == "__main__":
    raise SystemExit(main())
