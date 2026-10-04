"""Run web apps the model builds, to catch the bugs syntax checks can't see.

A page "looking right" isn't the same as working. This module loads an HTML
page together with its local scripts in a small simulated browser
(assets/domsim.js, plain Node.js, nothing to install), runs the scripts, clicks
every button, presses keys, and probes common apps (a calculator really gets
2 + 3 =). It reports crashes with file:line, broken wiring between HTML and JS,
and wrong or invalid output, so the model can fix them before finishing.

Static checks run without Node: missing local script/style files and element
ids that the JS looks up but the HTML never defines.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Optional

ASSET = Path(__file__).parent / "assets" / "domsim.js"
VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta",
        "param", "source", "track", "wbr"}
OPTIONAL_END = {"p", "li", "dt", "dd", "option", "optgroup", "thead", "tbody", "tfoot", "tr",
                "td", "th", "colgroup", "caption", "html", "head", "body"}
JS_TYPES = {"", "text/javascript", "application/javascript", "module", "application/ecmascript",
            "text/ecmascript"}
TEMPLATE_MARK = re.compile(r"\{\{|\{%|\{#|<%|<\?(php|=)")


@dataclass
class AppReport:
    page: str
    ran: bool = False
    errors: list[str] = field(default_factory=list)       # problems to fix
    notes: list[str] = field(default_factory=list)        # informational
    trace: list[str] = field(default_factory=list)        # what the simulation did
    probes: list[dict] = field(default_factory=list)
    expects: list[dict] = field(default_factory=list)
    skipped: str = ""
    clicks: int = 0
    elapsed_ms: int = 0

    @property
    def ok(self) -> bool:
        return not self.errors and all(e.get("ok") for e in self.expects)


# --------------------------------------------------------------- parsing

class _Page(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.nodes: list[dict] = []
        self.stack: list[int] = []
        self.scripts: list[dict] = []
        self.links: list[tuple[str, int]] = []
        self._script: Optional[dict] = None

    def _parent(self) -> int:
        return self.stack[-1] if self.stack else -1

    def handle_starttag(self, tag: str, attrs: list) -> None:
        a = {k.lower(): (v if v is not None else "") for k, v in attrs}
        line = self.getpos()[0]
        if tag == "script":
            self._script = {"attrs": a, "line": line, "code": [], "code_line": None}
        if tag == "link" and "stylesheet" in a.get("rel", "").lower() and a.get("href"):
            self.links.append((a["href"], line))
        idx = len(self.nodes)
        self.nodes.append({"t": tag, "a": a, "c": [], "l": line, "p": self._parent()})
        if self._parent() >= 0:
            self.nodes[self._parent()]["c"].append(idx)
        if tag not in VOID:
            self.stack.append(idx)

    def handle_startendtag(self, tag: str, attrs: list) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in VOID and self.stack and self.nodes[self.stack[-1]]["t"] == tag:
            self.stack.pop()

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self._script is not None:
            sc = self._script
            sc["code"] = "".join(sc["code"])
            self.scripts.append(sc)
            self._script = None
        for i in range(len(self.stack) - 1, -1, -1):
            if self.nodes[self.stack[i]]["t"] == tag:
                del self.stack[i:]
                return

    def handle_data(self, data: str) -> None:
        if self._script is not None:
            if self._script["code_line"] is None:
                self._script["code_line"] = self.getpos()[0]
            self._script["code"].append(data)
            return
        if self.stack and data:
            self.nodes[self.stack[-1]]["c"].append(data)


def _local(ref: str) -> bool:
    return bool(ref) and not re.match(r"^([a-z][a-z0-9+.-]*:|//|#|data:)", ref, re.I)


def _strip_ref(ref: str) -> str:
    return ref.split("#", 1)[0].split("?", 1)[0]


def collect(html_path: Path) -> dict:
    """Parse a page into the simulator's input plus static findings."""
    text = html_path.read_text(encoding="utf-8", errors="replace")
    if text.startswith("﻿"):
        text = text[1:]
    p = _Page()
    try:
        p.feed(text)
        p.close()
    except Exception:  # noqa: BLE001 - html.parser is lenient; be safe anyway
        pass
    base = html_path.parent
    page = {"html_file": html_path.name, "nodes": p.nodes, "scripts": [], "remote": False,
            "missing": [], "js_sources": {}, "template": bool(TEMPLATE_MARK.search(text))}
    for sc in p.scripts:
        a = sc["attrs"]
        typ = (a.get("type") or "").strip().lower()
        src = a.get("src")
        if typ not in JS_TYPES:
            continue
        if src:
            if not _local(src):
                page["remote"] = True
                continue
            f = (base / _strip_ref(src)).resolve()
            if not f.is_file():
                page["missing"].append((f'<script src="{src}">', sc["line"]))
                continue
            code = f.read_text(encoding="utf-8", errors="replace")
            rel = os.path.relpath(f, base)
            page["js_sources"][rel] = code
            entry = {"file": rel, "code": code, "inline": False, "line": 1}
        else:
            code = sc["code"] if isinstance(sc["code"], str) else "".join(sc["code"])
            if not code.strip():
                continue
            line = sc["code_line"] or sc["line"]
            page["js_sources"].setdefault(html_path.name, "")
            entry = {"file": html_path.name, "code": code, "inline": True, "line": line}
        if typ == "module" and re.search(r"^\s*(import|export)\b", code, re.M):
            entry = {"skip": f"{entry['file']}: ES module with import/export (not simulated)"}
        page["scripts"].append(entry)
    for href, line in p.links:
        if _local(href) and not (base / _strip_ref(href)).is_file():
            page["missing"].append((f'<link href="{href}">', line))
    page["html_text"] = text
    return page


# ----------------------------------------------------------- static checks

_GET_ID = re.compile(r"""getElementById\(\s*(['"])([^'"\s]+)\1\s*\)|querySelector(?:All)?\(\s*(['"])#([\w-]+)\3\s*\)""")


def static_findings(page: dict) -> list[str]:
    out = [f"{what} (line {line} of {page['html_file']}) points to a file that doesn't exist"
           for what, line in page["missing"]]
    if page.get("template"):
        return out            # server-side templates: ids may come from the template engine
    defined = {n["a"]["id"] for n in page["nodes"] if n["a"].get("id")}
    all_js = "\n".join(s.get("code", "") for s in page["scripts"] if "code" in s)
    dyn = set(re.findall(r"""\.id\s*=\s*['"`]([\w-]+)['"`]""", all_js))
    dyn |= set(re.findall(r"""\bid\s*=\s*\\?["']([\w-]+)\\?["']""", all_js))
    dyn |= set(re.findall(r"""setAttribute\(\s*['"]id['"]\s*,\s*['"]([\w-]+)['"]""", all_js))
    seen = set()
    for s in page["scripts"]:
        if "code" not in s:
            continue
        for m in _GET_ID.finditer(s["code"]):
            ident = m.group(2) or m.group(4)
            if ident in defined or ident in dyn or ident in seen:
                continue
            seen.add(ident)
            line = s["code"].count("\n", 0, m.start()) + (s["line"] if s["inline"] else 1)
            out.append(f'{s["file"]} line {line} looks up id "{ident}", but no element in '
                       f'{page["html_file"]} has id="{ident}"')
    return out


# ------------------------------------------------------------ simulation

def node_path() -> Optional[str]:
    return shutil.which("node")


def run_page(html_path: Path, steps: Optional[list] = None, probes: bool = True,
             timeout: float = 25.0) -> AppReport:
    rep = AppReport(page=html_path.name)
    try:
        page = collect(html_path)
    except OSError as exc:
        rep.skipped = f"could not read the page: {exc}"
        return rep
    rep.errors += static_findings(page)
    node = node_path()
    if not node:
        rep.skipped = "Node.js is not installed, so the page could not be run (static checks only)"
        return rep
    if page.get("template"):
        rep.skipped = "this page is a server-side template ({{ }} / {% %} / <?php), so it can't be run directly"
        return rep
    # simulator filenames are aliases so paths with spaces map back reliably
    alias: dict[str, str] = {}
    scripts = []
    for i, s in enumerate(page["scripts"]):
        if "skip" in s:
            scripts.append(s)
            continue
        real = s["file"]
        a = alias.setdefault(real, f"tcfile{len(alias)}{Path(real).suffix or '.js'}")
        scripts.append({**s, "file": a})
    html_alias = alias.setdefault(page["html_file"], f"tcfile{len(alias)}.html")
    payload = {"nodes": page["nodes"], "scripts": scripts, "html_file": html_alias,
               "files": list(alias.values()), "remote_scripts": page["remote"],
               "steps": steps or [], "probes": probes}
    back = {v: k for k, v in alias.items()}
    try:
        proc = subprocess.run([node, str(ASSET)], input=json.dumps(payload), capture_output=True,
                              text=True, timeout=timeout, cwd=str(html_path.parent))
    except subprocess.TimeoutExpired:
        rep.errors.append("the page did not finish loading within "
                          f"{timeout:.0f}s (an infinite loop at startup?)")
        return rep
    except OSError as exc:
        rep.skipped = f"could not start node: {exc}"
        return rep
    try:
        res = json.loads(proc.stdout or "{}")
    except ValueError:
        rep.skipped = "the simulator crashed: " + (proc.stderr or "no output").strip()[-300:]
        return rep
    rep.ran = True
    rep.elapsed_ms = int(res.get("elapsed_ms") or 0)
    sources = {k: v for k, v in page["js_sources"].items()}
    sources[page["html_file"]] = page["html_text"]
    for e in res.get("errors", []):
        f = back.get(e.get("file") or "", e.get("file"))
        where = f" — {f} line {e['line']}" if f and e.get("line") else ""
        msg = f"{e.get('phase')}: {e.get('type')}: {e.get('message')}{where}"
        src = sources.get(f) if f else None
        if src and e.get("line"):
            lines = src.split("\n")
            if 1 <= e["line"] <= len(lines):
                msg += f"\n       {e['line']:>4} | {lines[e['line'] - 1].strip()[:160]}"
        if e.get("hint"):
            msg += f"\n       hint: {e['hint']}"
        msg = re.sub(r"tcfile\d+\.\w+", lambda m: back.get(m.group(0), m.group(0)), msg)
        rep.errors.append(msg)
    for w in res.get("warnings", [])[:8]:
        rep.notes.append(f"{w.get('phase')}: {w.get('message')}")
    for t in res.get("trace", [])[:40]:
        if t.get("alert"):
            rep.trace.append(f"{t['phase']}: alert(\"{t['alert']}\")")
        elif t.get("changes"):
            rep.trace.append(f"{t['phase']} → " + ", ".join(t["changes"]))
    rep.probes = res.get("probes", [])
    rep.expects = res.get("expects", [])
    rep.clicks = int(res.get("clicks") or 0)
    for s in res.get("skipped", []):
        rep.notes.append(f"not run: {back.get(s, s)}")
    return rep


def pages_for(paths: list[Path], root: Path, limit: int = 3) -> list[Path]:
    """HTML pages affected by changes to these files (the files themselves, or
    pages that load a changed .js/.css file)."""
    pages: list[Path] = []
    others = []
    for p in paths:
        if p.suffix.lower() in (".html", ".htm") and p.is_file():
            pages.append(p)
        elif p.suffix.lower() in (".js", ".mjs", ".css") and p.is_file():
            others.append(p)
    for f in others:
        d = f.parent
        for _ in range(3):
            for h in sorted(d.glob("*.htm*")):
                try:
                    txt = h.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                if f.name in txt and h not in pages:
                    pages.append(h)
            if d == root or d.parent == d:
                break
            d = d.parent
    return pages[:limit]


def for_model(rep: AppReport, rel: str, final: bool = False) -> str:
    if rep.skipped and not rep.errors:
        return f"\n(app check skipped for {rel}: {rep.skipped})"
    lines = []
    head = (f"ran {rel} in a simulated browser" + (f", clicked {rep.clicks} element(s)" if rep.clicks else "")
            if rep.ran else f"checked {rel}")
    bad_expects = [e for e in rep.expects if not e.get("ok")]
    if rep.errors or bad_expects:
        lines.append(f"\n✗ App check ({head}) found {len(rep.errors) + len(bad_expects)} problem(s):")
        for i, e in enumerate(rep.errors[:10], 1):
            lines.append(f"  {i}. {e}")
        for e in bad_expects:
            lines.append(f"  - {e['message']}")
        lines.append("Fix them in the code" + (", then give your final summary." if final else ", then run test_app again."))
    else:
        lines.append(f"\n✓ App check ({head}): no errors.")
        for e in rep.expects:
            lines.append(f"  - {e['message']}")
        for p in rep.probes:
            lines.append(f"  - {p['name']} = {p['got']} ✓")
    if rep.trace and (rep.errors or bad_expects or rep.expects):
        lines.append("What happened:")
        lines += [f"  · {t}" for t in rep.trace[:12]]
    return "\n".join(lines)
