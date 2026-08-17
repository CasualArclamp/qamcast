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

There is a second way a control can be dead without looking it: the handler
runs, the page updates, and the setting is simply never put in the message to
the server. The mode selector shipped like that -- it filtered the preset list,
so it plainly worked, while neither solve nor start was ever told which
constellation family had been chosen.

Four checks, all cheap and all aimed at those two failures:

    every $('id') the script uses has a matching id in the HTML
    every plain function call the script makes is defined, or is a builtin
    every dial in the Link panel is sent to both solve and start
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


# Controls that do reach the server, but not by name in the payload. Each is
# read through something else, so the literal check below cannot see it.
INDIRECT = {
    "web/tx.html": {
        # readBitrate() normalises "96 kbps" to bits before it travels.
        "bitrate": "read through readBitrate()",
    },
    "web/rx.html": {
        # Copied into KEY on Apply; KEY is what travels, and it stays in force
        # until a dial is touched. The box itself is deliberately not read at
        # Start -- a key typed but never applied must not take effect.
        "linkKey": "read into KEY by applyKey()",
    },
}


def scripts(text: str) -> str:
    return "\n".join(re.findall(r"<script[^>]*>(.*?)</script>", text, re.S))


def balanced(text: str, start: int, open_pat: str, close_pat: str) -> int:
    """End offset of the construct opening at ``start``, or -1."""
    depth = 0
    for m in re.finditer(f"{open_pat}|{close_pat}", text[start:]):
        depth += 1 if re.match(open_pat, m.group()) else -1
        if depth == 0:
            return start + m.end()
    return -1


def panel(text: str, heading: str) -> str:
    """The HTML of the panel carrying this <h2>, found by walking div tags."""
    at = text.find(f"<h2>{heading}</h2>")
    if at < 0:
        return ""
    for start in reversed([m.start() for m in re.finditer(r"<div\b", text[:at])]):
        end = balanced(text, start, r"<div\b", r"</div>")
        if end > at:
            return text[start:end]
    return ""


def payloads(js: str) -> dict[str, str]:
    """The object literal passed to each control({cmd: ...}) call, by command."""
    out: dict[str, str] = {}
    for m in re.finditer(r"control\(\s*\{", js):
        start = m.end() - 1
        end = balanced(js, start, r"\{", r"\}")
        if end < 0:
            continue
        body = js[start:end]
        cmd = re.search(r"""cmd\s*:\s*['"](\w+)['"]""", body)
        if cmd:
            out[cmd.group(1)] = out.get(cmd.group(1), "") + body
    return out


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

    problems += link_panel(page, text, js)
    problems += div_balance(text)
    return problems


def link_panel(page: str, text: str, js: str) -> list[str]:
    """Every dial in the Link panel must actually reach the server.

    The Link panel *is* the physical layer: none of it travels in the header,
    so a control there that the payload does not carry is a control that does
    nothing -- and does nothing silently, which is the worst version. The mode
    selector shipped exactly like that. It filtered the preset list, so it
    looked alive, while neither solve nor start was ever told which
    constellation family had been picked. Choosing APSK then quietly
    transmitted QAM, wrote a QAM link key, and left the receiver slicing an
    APSK signal against a square grid.

    Checked against both commands, because they answer different questions and
    must agree: solve draws the panel, start builds the link.
    """
    body = panel(text, "Link")
    if not body:
        return ["no Link panel found -- this check is not running"]
    controls = []
    for m in re.finditer(r"<(select|input|textarea)\b([^>]*)>", body):
        got = re.search(r"""\bid=["']([^"']+)["']""", m.group(2))
        # Readonly is an output. The transmit page's key box is one: it shows
        # what the settings came to, and nothing reads it back.
        if got and "readonly" not in m.group(2).lower():
            controls.append(got.group(1))

    sent = payloads(js)
    exempt = INDIRECT.get(page, {})
    out = []
    for cmd in ("solve", "start"):
        if cmd not in sent:
            out.append(f"no control({{cmd: '{cmd}'}}) call found")
            continue
        reads = set(re.findall(r"""\$\(\s*['"]([^'"]+)['"]\s*\)""", sent[cmd]))
        for name in controls:
            if name in reads or name in exempt:
                continue
            out.append(f"the Link panel's #{name} is never sent to '{cmd}' -- "
                       f"changing it would do nothing")
    return out


def div_balance(text: str) -> list[str]:
    """Divs must close as often as they open.

    Removing a block and leaving its closing tag behind does not break the
    page loudly; it nests everything after it one level deeper, so a panel
    quietly moves inside its neighbour. That happened when the audio reservoir
    came out.
    """
    opens = len(re.findall(r"<div\b", text))
    closes = len(re.findall(r"</div>", text))
    if opens != closes:
        return [f"{opens} <div> against {closes} </div> -- the layout is "
                f"nested differently from how it reads"]
    return []


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
