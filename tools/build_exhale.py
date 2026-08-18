"""Build exhale, the xHE-AAC encoder, into bin/.

Nothing in ffmpeg encodes xHE-AAC and nothing can be linked into it that
does -- the open-source Fraunhofer FDK ships a USAC decoder and no USAC
encoder. exhale is a small open-source USAC encoder, so this fetches its
source, makes two changes to it, builds it, and puts the result where
qamcore/exhale.py looks.

    python tools/build_exhale.py

No binary is downloaded and none is committed: the source comes from the
upstream repository at a pinned revision, and the build happens here.

**The two changes**, both in exhale's own command-line application and both
only affecting its pipe output:

  1. ``ENABLE_STDOUT_LOAS`` is turned on. exhale writes MPEG-4 files, which
     cannot be produced live -- the sample table can only be written once the
     last sample is known. Its source carries a mode that writes LOAS frames
     to stdout instead, which streams; it is off by default.

  2. ``audioMuxLengthBytes`` is corrected. That mode declares a frame length
     three bytes longer than the frame is, because it counts the syncword and
     the length field themselves. ISO/IEC 14496-3 defines the field as the
     length of the AudioMuxElement that *follows* the header. Uncorrected,
     every reader loses sync after the first frame: ffmpeg's own LOAS demuxer
     recovers 17 frames from a stream of 235. With it, 469 frames of a
     twenty-second stream come out as 469, with no bytes left over.

Requires cmake and a C++ compiler. On Windows an MSYS2 mingw64 toolchain is
found automatically if it is in the usual place.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCE = "https://gitlab.com/ecodis/exhale.git"
REVISION = "c33cf75b002806ca41e7a1c0deadfcf622cbd786"   # v1.2.2 plus fixes

APP = os.path.join("src", "app", "exhaleApp.cpp")

# Each patch is (what must be there, what to put instead). Matched exactly, so
# a source that has moved on is a loud failure rather than a quiet miscompile.
PATCHES = (
    ("#define ENABLE_STDOUT_LOAS 0",
     "#define ENABLE_STDOUT_LOAS 1"),
    ("const uint32_t audioMuxLengthBytes = payloadOffset + 1 + auSize / 255 + auSize;",
     "const uint32_t audioMuxLengthBytes = payloadOffset - 3 + 1 + auSize / 255 + auSize;"),
)

MINGW = (r"C:\msys64\mingw64\bin", r"C:\msys64\ucrt64\bin",
         r"C:\ProgramData\mingw64\mingw64\bin")

# Link the compiler's own runtime in rather than depending on it. A MinGW
# build otherwise needs libgcc_s_seh-1.dll, libstdc++-6.dll and libwinpthread
# beside it or on PATH, and they are only on PATH inside an MSYS2 shell -- so
# the binary runs where it was built and nowhere else, which is the worst
# possible failure because the build looks like it worked. Costs about a
# megabyte on a program that is otherwise half of one.
STATIC = "-static-libgcc -static-libstdc++ -static"

# What a self-contained Windows binary is allowed to import. Anything else
# means the static link did not take.
SYSTEM_DLLS = {"kernel32.dll", "msvcrt.dll", "user32.dll", "advapi32.dll",
               "ucrtbase.dll", "api-ms-win-crt-", "shell32.dll", "ole32.dll"}


def run(cmd, **kw):
    print("   $", " ".join(str(c) for c in cmd))
    done = subprocess.run(cmd, **kw)
    if done.returncode:
        raise SystemExit(f"failed: {' '.join(str(c) for c in cmd)}")
    return done


def toolchain() -> dict:
    """Somewhere to get a C++ compiler and a build tool from."""
    env = dict(os.environ)
    if not shutil.which("g++", path=env.get("PATH")) and sys.platform == "win32":
        for path in MINGW:
            if os.path.exists(os.path.join(path, "g++.exe")):
                env["PATH"] = path + os.pathsep + env["PATH"]
                break
    cxx = shutil.which("g++", path=env["PATH"])
    if cxx:
        generator = ("Ninja" if shutil.which("ninja", path=env["PATH"])
                     else "MinGW Makefiles" if sys.platform == "win32"
                     else "Unix Makefiles")
        return {"env": env, "cxx": cxx, "generator": generator}
    if shutil.which("cl", path=env["PATH"]) or sys.platform != "win32":
        return {"env": env, "cxx": None, "generator": None}   # cmake decides
    raise SystemExit(
        "no C++ compiler found. Install one and re-run:\n"
        "  Windows: MSYS2 (pacman -S mingw-w64-x86_64-gcc mingw-w64-x86_64-cmake\n"
        "           mingw-w64-x86_64-ninja), or Visual Studio Build Tools\n"
        "  Linux:   your distribution's g++ and cmake")


def fetch(work: str) -> None:
    if os.path.exists(os.path.join(work, ".git")):
        print(f"-- reusing {work}")
        run(["git", "-C", work, "fetch", "--quiet", "origin", REVISION])
    else:
        os.makedirs(os.path.dirname(work), exist_ok=True)
        print(f"-- cloning {SOURCE}")
        run(["git", "clone", "--quiet", SOURCE, work])
    run(["git", "-C", work, "checkout", "--quiet", "--force", REVISION])


def patch(work: str) -> None:
    path = os.path.join(work, APP)
    text = open(path, encoding="utf-8", errors="surrogateescape").read()
    for before, after in PATCHES:
        if after in text:
            continue
        if before not in text:
            raise SystemExit(
                f"exhale's source has changed and this patch no longer applies:\n"
                f"  looked for: {before}\n"
                f"in {path}")
        text = text.replace(before, after, 1)
        print(f"-- patched: {before.split('=')[0].strip()}")
    open(path, "w", encoding="utf-8", errors="surrogateescape").write(text)


def build(work: str, tools: dict) -> str:
    out = os.path.join(work, "build")
    cmd = ["cmake", "-S", work, "-B", out, "-DCMAKE_BUILD_TYPE=Release"]
    if tools["generator"]:
        cmd += ["-G", tools["generator"]]
    if tools["cxx"]:
        cmd += [f"-DCMAKE_CXX_COMPILER={tools['cxx'].replace(os.sep, '/')}",
                f"-DCMAKE_EXE_LINKER_FLAGS={STATIC}"]
        # A cache configured before the linker flags existed would keep the
        # old link line and quietly produce a binary that only runs here.
        cache = os.path.join(out, "CMakeCache.txt")
        if os.path.exists(cache):
            kept = open(cache, encoding="utf-8", errors="replace").read()
            if STATIC not in kept:
                print("-- link flags changed; discarding the old build")
                shutil.rmtree(out, ignore_errors=True)
    run(cmd, env=tools["env"])
    run(["cmake", "--build", out, "--config", "Release"], env=tools["env"])

    for name in ("exhale.exe", "exhale"):
        for where in (("src", "app"), ("src", "app", "Release"), ()):
            cand = os.path.join(out, *where, name)
            if os.path.exists(cand):
                return cand
    raise SystemExit(f"built, but no exhale binary found under {out}")


def imports(binary: str, env: dict) -> list[str]:
    """Which DLLs a Windows binary needs, where that can be asked.

    Checked because the failure it catches is invisible until someone else
    runs it: the build succeeds, the binary works in the shell that built it,
    and it dies with a missing-DLL dialog anywhere else.
    """
    objdump = shutil.which("objdump", path=env.get("PATH"))
    if not objdump or not binary.endswith(".exe"):
        return []
    try:
        out = subprocess.run([objdump, "-p", binary], capture_output=True,
                             text=True, timeout=60).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    names = [line.split(":", 1)[1].strip()
             for line in out.splitlines() if "DLL Name:" in line]
    return [n for n in names
            if not any(n.lower().startswith(s) for s in SYSTEM_DLLS)]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--work", default=os.path.join(ROOT, "build", "exhale"),
                    help="where to keep the source tree and build")
    ap.add_argument("--into", default=os.path.join(ROOT, "bin"),
                    help="where to install the binary")
    ap.add_argument("--check", action="store_true",
                    help="report whether a usable exhale is already here, "
                         "and build nothing. Exits 0 if there is one.")
    a = ap.parse_args()

    if a.check:
        sys.path.insert(0, ROOT)
        from qamcore import exhale
        ok, why = exhale.usable()
        print(f"exhale: {exhale.find_exhale() or 'not found'}")
        if not ok:
            print(why)
        return 0 if ok else 1

    if not shutil.which("cmake"):
        raise SystemExit("cmake not found; exhale's build needs it")
    tools = toolchain()

    fetch(a.work)
    patch(a.work)
    binary = build(a.work, tools)

    needed = imports(binary, tools["env"])
    if needed:
        raise SystemExit(
            "built, but the binary depends on DLLs that are not part of "
            "Windows:\n  " + "\n  ".join(needed) + "\n\n"
            "It would run here and fail with a missing-DLL dialog anywhere "
            "else. The static link did not take -- check that "
            f"CMAKE_EXE_LINKER_FLAGS={STATIC} reached the build.")

    os.makedirs(a.into, exist_ok=True)
    dest = os.path.join(a.into, os.path.basename(binary))
    shutil.copy2(binary, dest)
    os.chmod(dest, 0o755)
    print()
    print(f"-- installed {dest}")
    if binary.endswith(".exe"):
        print("-- self-contained: needs nothing but Windows' own DLLs")

    # Asked with the build toolchain's directories taken back off PATH, since
    # that is what every other program on this machine will see.
    plain = dict(os.environ)
    sys.path.insert(0, ROOT)
    from qamcore import exhale
    ok, why = exhale.usable(dest, env=plain)
    print(f"-- usable: {ok}{'' if ok else '  -- ' + why}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
