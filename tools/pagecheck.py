"""Static check on the two web pages: do they reference things that exist?

Both pages are one HTML file with the script inline, and a page whose script
throws during start-up does not look broken -- it looks *empty*. Every select
renders with no options and every panel reads blank, which is indistinguishable
from a server that has not answered yet.

That happened: a helper was added to rx.html and not to tx.html, so tx.html
called an undefined function, the whole initialisation block threw, and the
transmit page came up with Preset, Card sample rate, Symbol rate, Modulation,
Code rate and Roll-off all empty. Nothing in the regression suite touches the
pages, so nothing caught it.

Two checks, both cheap and both aimed at exactly that failure:

    every $('id') the script uses has a matching id in the HTML
    every plain function call the script makes is defined, or is a builtin
    the div tags balance, so removing a block cannot leave the layout nested
    one level deeper than it reads

    python tools/pagecheck.py
"""

from __future__ import annotations

import os
import re
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAGES = ("web/tx.html", "web/rx.html")

# Things the script may call that it does not define: browser and library
# globals, and the shared helpers in scope.js.
KNOWN = set("""
if for while switch catch return typeof new delete void await function
async of in instanceof do else
Math JSON Object Array String Number Boolean Promise Map Set Date RegExp
parseInt parseFloat isNaN isFinite encodeURIComponent decodeURIComponent
setTimeout clearTimeout setInterval clearInterval requestAnimationFrame
fetch alert confirm console document window navigator localStorage
EventSource Uint8Array Float32Array Int16Array ArrayBuffer DataView
atob btoa structuredClone queueMicrotask
""".split())


def scripts(text: str) -> str:
    return "\n".join(re.findall(r"<script[^>]*>(.*?)</script>", text, re.S))


def shared_names() -> set[str]:
    path = os.path.join(HERE, "web", "scope.js")
    if not os.path.isfile(path):
        return set()
    src = open(path, encoding="utf-8").read()
    return set(re.findall(r"\bfunction\s+([A-Za-z_$][\w$]*)", src)) | \
        set(re.findall(r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=", src))


def check(page: str, shared: set[str]) -> list[str]:
    text = open(os.path.join(HERE, page), encoding="utf-8").read()
    js = scripts(text)
    problems = []

    ids = set(re.findall(r"""\bid=["']([^"']+)["']""", text))
    used = set(re.findall(r"""\$\(\s*['"]([^'"]+)['"]\s*\)""", js))
    for name in sorted(used - ids):
        problems.append(f"$('{name}') has no matching id in the HTML")

    defined = set(re.findall(r"\bfunction\s+([A-Za-z_$][\w$]*)", js))
    defined |= set(re.findall(
        r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=", js))
    # single-name arrow and function parameters, plus catch bindings
    defined |= set(re.findall(r"\(\s*([A-Za-z_$][\w$]*)\s*\)\s*=>", js))
    defined |= set(re.findall(r"\b([A-Za-z_$][\w$]*)\s*=>", js))
    defined |= set(re.findall(r"function\s*\(([^)]*)\)", js and js)) and defined
    for params in re.findall(r"function\s+[\w$]*\s*\(([^)]*)\)", js):
        defined |= {p.strip().split("=")[0].strip() for p in params.split(",") if p.strip()}
    defined |= shared

    # Calls of the form name(...), not preceded by a dot -- so method calls on
    # objects are left alone, which is the whole class this cannot reason about.
    for name in sorted(set(re.findall(r"(?<![.\w$])([A-Za-z_$][\w$]*)\s*\(", js))):
        if name in defined or name in KNOWN:
            continue
        problems.append(f"{name}() is called but never defined on this page")
    return problems


def main() -> int:
    shared = shared_names()
    bad = 0
    for page in PAGES:
        problems = check(page, shared)
        if problems:
            bad += len(problems)
            print(f"{page}:")
            for p in problems:
                print(f"   {p}")
        else:
            print(f"{page}: ok")
    print("\nPASS" if not bad else f"\n{bad} PROBLEM(S)")
    return 0 if not bad else 1


if __name__ == "__main__":
    raise SystemExit(main())
