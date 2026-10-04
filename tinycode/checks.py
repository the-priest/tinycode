"""Fast, local syntax / error checks run after every file change.

The goal is to catch the mistakes a small model makes (a missing brace, an
unclosed tag, an undefined name) *immediately*, so it can fix them in its next
step instead of the user finding them later. Every checker is cheap (well under
a second), runs only on the changed file, and silently skips when its tool
isn't installed. False positives are worse than misses here (they send the
model chasing ghosts), so checkers are deliberately conservative.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Optional

TIMEOUT = 15


@dataclass
class Problem:
    line: int
    message: str
    col: int = 0
    severity: str = "error"          # error | warning
    where: str = ""                  # e.g. "inline <script>"


@dataclass
class CheckResult:
    path: str
    language: str = ""
    checkers: list[str] = field(default_factory=list)
    problems: list[Problem] = field(default_factory=list)
    incomplete: list[str] = field(default_factory=list)   # e.g. ["<script>", "{"]
    skipped: str = ""                # why nothing could be checked

    @property
    def errors(self) -> list[Problem]:
        return [p for p in self.problems if p.severity == "error"]

    @property
    def checked(self) -> bool:
        return bool(self.checkers)

    @property
    def ok(self) -> bool:
        return self.checked and not self.errors and not self.incomplete


# ------------------------------------------------------------------ helpers

def _run(cmd: list[str], cwd: Optional[str] = None, stdin: Optional[str] = None
         ) -> Optional[subprocess.CompletedProcess]:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT,
                              cwd=cwd, input=stdin,
                              env={**os.environ, "NO_COLOR": "1", "TERM": "dumb"})
    except (OSError, subprocess.SubprocessError):
        return None


def _which(name: str) -> Optional[str]:
    return shutil.which(name)


_EOF_HINTS = ("unexpected end of input", "unexpected eof", "was never closed",
              "unterminated triple-quoted", "unexpected end of file",
              "expected an indented block", "end of input", "eof while",
              "unterminated", "expecting value: line", "unclosed")


def _looks_eof(msg: str, line: int, nlines: int) -> bool:
    m = msg.lower()
    return any(h in m for h in _EOF_HINTS) and line >= max(1, nlines - 2)


# ------------------------------------------------------------------ python

_RUFF_RULES = ("F821,F822,F823,F811,F632,F701,F702,F704,F706,F707,F722,F631,F633,"
               "F502,F503,F504,F505,F506,F507,F508,F509,F521,F522,F523,F524,F525")


def check_python(path: Path, text: str, res: CheckResult) -> None:
    res.language = "python"
    res.checkers.append("python compile")
    nlines = text.count("\n") + 1
    try:
        compile(text, str(path), "exec", dont_inherit=True)
    except SyntaxError as exc:
        line = exc.lineno or 1
        msg = f"SyntaxError: {exc.msg}"
        if _looks_eof(exc.msg or "", line, nlines):
            res.incomplete.append(exc.msg or "unexpected end of file")
        else:
            res.problems.append(Problem(line, msg, exc.offset or 0))
        return          # linters can't say more about a file that doesn't parse
    except (ValueError, RecursionError, MemoryError) as exc:
        res.problems.append(Problem(1, f"{type(exc).__name__}: {exc}"))
        return

    ruff = _which("ruff")
    if ruff:
        out = _run([ruff, "check", "--no-cache", "--quiet", "--output-format", "json",
                    "--select", _RUFF_RULES, "--stdin-filename", str(path), "-"],
                   cwd=str(path.parent), stdin=text)
        if out is not None and out.stdout.strip().startswith("["):
            res.checkers.append("ruff")
            try:
                for d in json.loads(out.stdout):
                    loc = d.get("location") or {}
                    res.problems.append(Problem(
                        int(loc.get("row") or 1), f"{d.get('code')}: {d.get('message')}",
                        int(loc.get("column") or 0)))
            except (ValueError, TypeError):
                pass
            return
    # pyflakes fallback (only real errors, not style)
    try:
        import pyflakes.api  # type: ignore  # noqa: F401
        has_pyflakes = True
    except ImportError:
        has_pyflakes = False
    if has_pyflakes:
        out = _run([sys.executable, "-m", "pyflakes", "-"], stdin=text)
        if out is not None:
            res.checkers.append("pyflakes")
            for line in (out.stdout + out.stderr).splitlines():
                m = re.match(r".*?:(\d+):(?:(\d+):?)?\s*(.*)", line)
                if m and ("undefined name" in m.group(3) or "redefinition" in m.group(3)
                          or "outside" in m.group(3)):
                    res.problems.append(Problem(int(m.group(1)), m.group(3),
                                                int(m.group(2) or 0)))


# --------------------------------------------------------------- javascript

def _node_check(code: str, suffix: str, line_offset: int = 0
                ) -> tuple[bool, list[Problem], list[str]]:
    """node --check on code. Returns (ran, problems, incomplete)."""
    node = _which("node")
    if not node:
        return False, [], []
    fd, tmp = tempfile.mkstemp(suffix=suffix, prefix="tinycode-check-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(code)
        out = _run([node, "--check", tmp])
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    if out is None:
        return False, [], []
    if out.returncode == 0:
        return True, [], []
    err = out.stderr
    m = re.search(re.escape(tmp) + r":(\d+)", err)
    line = int(m.group(1)) if m else 1
    msg_m = re.search(r"^(\w*Error: .+)$", err, re.M)
    msg = msg_m.group(1) if msg_m else err.strip().splitlines()[-1][:200] if err.strip() else "syntax error"
    nlines = code.count("\n") + 1
    if _looks_eof(msg, line, nlines):
        return True, [], [msg]
    return True, [Problem(line + line_offset, msg)], []


def check_js(path: Path, text: str, res: CheckResult) -> None:
    res.language = "javascript"
    suffix = path.suffix if path.suffix in (".mjs", ".cjs") else ".js"
    if suffix == ".js" and re.search(r"^\s*(import|export)\s", text, re.M):
        suffix = ".mjs"
    ran, probs, inc = _node_check(text, suffix)
    if ran:
        res.checkers.append("node --check")
        res.problems += probs
        res.incomplete += inc
    else:
        res.skipped = "install Node.js to check JavaScript"


_TS_SYNTAX = re.compile(r"\((\d+),(\d+)\): error (TS1\d{3}): (.*)")


def check_ts(path: Path, text: str, res: CheckResult) -> None:
    res.language = "typescript"
    tsc = _which("tsc")
    if not tsc:
        res.skipped = "install TypeScript (tsc) to check .ts files"
        return
    out = _run([tsc, "--noEmit", "--noResolve", "--skipLibCheck", "--isolatedModules",
                "--jsx", "preserve", "--target", "es2022", str(path)],
               cwd=str(path.parent))
    if out is None:
        return
    res.checkers.append("tsc (syntax)")
    nlines = text.count("\n") + 1
    for line in out.stdout.splitlines():
        m = _TS_SYNTAX.search(line)
        if m:      # TS1xxx = syntax errors only (no type errors from missing deps)
            ln = int(m.group(1))
            if _looks_eof(m.group(4), ln, nlines) or "expected" in m.group(4).lower() \
                    and ln >= nlines:
                res.incomplete.append(m.group(4))
            else:
                res.problems.append(Problem(ln, f"{m.group(3)}: {m.group(4)}", int(m.group(2))))


# --------------------------------------------------------------------- html

VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta",
        "param", "source", "track", "wbr", "keygen", "command", "basefont", "frame",
        "isindex", "image"}
# elements whose end tag may be omitted per the HTML spec
OPTIONAL_END = {"html", "head", "body", "p", "li", "dt", "dd", "option", "optgroup",
                "thead", "tbody", "tfoot", "tr", "td", "th", "colgroup", "caption",
                "rt", "rp", "rb", "rtc", "menuitem"}
JS_TYPES = {"", "text/javascript", "application/javascript", "module", "text/babel-x",
            "application/ecmascript", "text/ecmascript"}


class _HTMLChecker(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[tuple[str, int]] = []
        self.problems: list[Problem] = []
        self.scripts: list[tuple[int, str, bool]] = []   # (start line, code, is_module)
        self.styles: list[tuple[int, str]] = []
        self._raw: Optional[tuple[str, int, bool]] = None
        self._buf: list[str] = []
        self.in_svg = 0

    def handle_starttag(self, tag: str, attrs: list) -> None:
        line = self.getpos()[0]
        if tag in VOID:
            return
        if tag == "svg" or tag == "math":
            self.in_svg += 1
        if tag == "script":
            a = dict(attrs)
            typ = (a.get("type") or "").strip().lower()
            self._raw = ("script", line, typ == "module") if not a.get("src") and \
                typ in JS_TYPES else ("ignore", line, False)
            self._buf = []
        elif tag == "style":
            self._raw = ("style", line, False)
            self._buf = []
        self.stack.append((tag, line))

    def handle_startendtag(self, tag: str, attrs: list) -> None:
        pass     # <tag/> is complete

    def handle_data(self, data: str) -> None:
        if self._raw is not None:
            self._buf.append(data)

    def handle_endtag(self, tag: str) -> None:
        line = self.getpos()[0]
        if tag in VOID:
            return
        if self._raw is not None and tag in ("script", "style"):
            kind, start, mod = self._raw
            code = "".join(self._buf)
            if kind == "script":
                self.scripts.append((start, code, mod))
            elif kind == "style":
                self.styles.append((start, code))
            self._raw = None
        if tag in ("svg", "math") and self.in_svg:
            self.in_svg -= 1
        if not any(t == tag for t, _ in self.stack):
            if tag not in OPTIONAL_END:
                self.problems.append(Problem(line, f"</{tag}> has no matching <{tag}>"))
            return
        # pop to the matching tag; anything non-optional in between is unclosed
        while self.stack:
            t, ln = self.stack.pop()
            if t == tag:
                break
            if t not in OPTIONAL_END and not self.in_svg:
                self.problems.append(Problem(
                    ln, f"<{t}> (line {ln}) is not closed before </{tag}> on line {line}"))


def check_html(path: Path, text: str, res: CheckResult) -> None:
    res.language = "html"
    p = _HTMLChecker()
    try:
        p.feed(text)
        p.close()
    except Exception as exc:  # noqa: BLE001 - html.parser is lenient; be safe anyway
        res.problems.append(Problem(1, f"HTML parse error: {exc}"))
        return
    res.checkers.append("html structure")
    res.problems += p.problems
    # unclosed raw block at EOF = the file is unfinished (or a missing </script>)
    if p._raw is not None:
        res.incomplete.append(f"<{p._raw[0] if p._raw[0] != 'ignore' else 'script'}> "
                              f"from line {p._raw[1]} is never closed")
    open_tags = [t for t, _ in p.stack if t not in OPTIONAL_END]
    if open_tags and p._raw is None:
        res.incomplete.append("unclosed " + ", ".join(f"<{t}>" for t in open_tags[-4:]))
    # inline scripts: real JS syntax check with correct line numbers
    ran_any = False
    for start, code, mod in p.scripts:
        padded = "\n" * (start - 1) + code      # keep the file's line numbers
        ran, probs, inc = _node_check(padded, ".mjs" if mod else ".js")
        if ran:
            ran_any = True
            for pr in probs:
                pr.where = f"inline <script> starting on line {start}"
            res.problems += probs
            if inc:
                res.problems.append(Problem(
                    start, f"{inc[0]} — the <script> starting on line {start} has an "
                           "unclosed bracket, brace or string", where="inline <script>"))
    if ran_any:
        res.checkers.append("node --check (inline scripts)")
    for start, css in p.styles:
        for pr in _css_problems(css, start):
            res.problems.append(pr)


# ---------------------------------------------------------------------- css

def _css_problems(css: str, line_offset: int = 1) -> list[Problem]:
    out: list[Problem] = []
    depth = 0
    i = 0
    line = line_offset
    opened: list[int] = []
    n = len(css)
    while i < n:
        ch = css[i]
        if ch == "\n":
            line += 1
        elif css.startswith("/*", i):
            end = css.find("*/", i + 2)
            if end < 0:
                out.append(Problem(line, "comment /* is never closed"))
                return out
            line += css.count("\n", i, end)
            i = end + 2
            continue
        elif ch in "\"'":
            end = i + 1
            while end < n and css[end] != ch and css[end] != "\n":
                end += 2 if css[end] == "\\" else 1
            i = end + 1
            continue
        elif ch == "{":
            depth += 1
            opened.append(line)
        elif ch == "}":
            if depth == 0:
                out.append(Problem(line, "unexpected } (no matching {)"))
            else:
                depth -= 1
                opened.pop()
        i += 1
    if depth:
        out.append(Problem(opened[-1], f"{{ opened on line {opened[-1]} is never closed"))
    return out


def check_css(path: Path, text: str, res: CheckResult) -> None:
    res.language = "css"
    res.checkers.append("css braces")
    probs = _css_problems(text, 1)
    nlines = text.count("\n") + 1
    for pr in probs:
        if "never closed" in pr.message and nlines - pr.line < 400:
            res.incomplete.append(pr.message)
        else:
            res.problems.append(pr)


# ------------------------------------------------------------ data formats

def check_json(path: Path, text: str, res: CheckResult) -> None:
    res.language = "json"
    res.checkers.append("json")
    if not text.strip():
        return
    try:
        json.loads(text)
    except json.JSONDecodeError as exc:
        if exc.pos >= len(text.rstrip()):       # ran out of input = unfinished
            res.incomplete.append(exc.msg)
        else:
            res.problems.append(Problem(exc.lineno, f"invalid JSON: {exc.msg}", exc.colno))


def check_toml(path: Path, text: str, res: CheckResult) -> None:
    res.language = "toml"
    try:
        if sys.version_info >= (3, 11):
            import tomllib
        else:  # pragma: no cover
            import tomli as tomllib  # type: ignore
    except ImportError:
        return
    res.checkers.append("toml")
    try:
        tomllib.loads(text)
    except Exception as exc:  # noqa: BLE001
        m = re.search(r"line (\d+)", str(exc))
        res.problems.append(Problem(int(m.group(1)) if m else 1, f"invalid TOML: {exc}"))


def check_yaml(path: Path, text: str, res: CheckResult) -> None:
    res.language = "yaml"
    try:
        import yaml  # type: ignore
    except ImportError:
        res.skipped = "install PyYAML to check YAML"
        return
    res.checkers.append("yaml")
    try:
        list(yaml.safe_load_all(text))
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        res.problems.append(Problem((mark.line + 1) if mark else 1,
                                    f"invalid YAML: {getattr(exc, 'problem', exc)}"))


# --------------------------------------------------- compilers / interpreters

def _cmd_check(path: Path, text: str, res: CheckResult, lang: str, cmd: list[str],
               rx: str, label: str) -> None:
    res.language = lang
    if not _which(cmd[0]):
        res.skipped = f"install {cmd[0]} to check {lang}"
        return
    out = _run(cmd + [str(path)], cwd=str(path.parent))
    if out is None:
        return
    res.checkers.append(label)
    nlines = text.count("\n") + 1
    seen = set()
    for line in (out.stdout + "\n" + out.stderr).splitlines():
        m = re.search(rx, line)
        if not m:
            continue
        ln = int(m.group("line"))
        msg = m.group("msg").strip()
        if re.fullmatch(r"`.*'", msg):     # bash echoes the offending source line
            continue
        if (ln, msg) in seen:
            continue
        seen.add((ln, msg))
        if _looks_eof(msg, ln, nlines):
            res.incomplete.append(msg)
        else:
            res.problems.append(Problem(ln, msg))
        if len(res.problems) >= 20:
            break


def check_bash(path: Path, text: str, res: CheckResult) -> None:
    _cmd_check(path, text, res, "shell", ["bash", "-n"],
               r":\s*line (?P<line>\d+):\s*(?P<msg>.+)", "bash -n")


def check_c(path: Path, text: str, res: CheckResult) -> None:
    cpp = path.suffix in (".cpp", ".cc", ".cxx", ".hpp", ".hh", ".hxx")
    comp = "g++" if cpp else "gcc"
    if not _which(comp):
        comp = "clang++" if cpp else "clang"
    _cmd_check(path, text, res, "c++" if cpp else "c",
               [comp, "-fsyntax-only", "-fno-diagnostics-color", "-w"],
               r":(?P<line>\d+):\d+:\s*(?:fatal )?error:\s*(?P<msg>.+)", f"{comp} -fsyntax-only")


def check_go(path: Path, text: str, res: CheckResult) -> None:
    _cmd_check(path, text, res, "go", ["gofmt", "-e", "-l"],
               r":(?P<line>\d+):\d+:\s*(?P<msg>.+)", "gofmt -e")


def check_php(path: Path, text: str, res: CheckResult) -> None:
    _cmd_check(path, text, res, "php", ["php", "-l"],
               r"(?:Parse|Fatal) error:\s*(?P<msg>.+?) in .+? on line (?P<line>\d+)", "php -l")


def check_ruby(path: Path, text: str, res: CheckResult) -> None:
    _cmd_check(path, text, res, "ruby", ["ruby", "-c"],
               r":(?P<line>\d+):\s*(?P<msg>.+)", "ruby -c")


CHECKERS = {
    ".py": check_python, ".pyw": check_python,
    ".js": check_js, ".mjs": check_js, ".cjs": check_js,
    ".ts": check_ts, ".tsx": check_ts, ".mts": check_ts, ".cts": check_ts, ".jsx": check_ts,
    ".html": check_html, ".htm": check_html, ".xhtml": check_html,
    ".css": check_css,
    ".json": check_json, ".toml": check_toml, ".yaml": check_yaml, ".yml": check_yaml,
    ".sh": check_bash, ".bash": check_bash,
    ".c": check_c, ".h": check_c, ".cpp": check_c, ".cc": check_c, ".cxx": check_c,
    ".hpp": check_c, ".hh": check_c, ".hxx": check_c,
    ".go": check_go, ".php": check_php, ".rb": check_ruby,
}


def supported(path: Path) -> bool:
    return path.suffix.lower() in CHECKERS


def check_file(path: Path, text: Optional[str] = None) -> CheckResult:
    res = CheckResult(path=str(path))
    fn = CHECKERS.get(path.suffix.lower())
    if fn is None:
        if path.suffix == "" and text is not None and text.startswith("#!") and "sh" in \
                text.split("\n", 1)[0]:
            fn = check_bash
        else:
            res.skipped = "no checker for this file type"
            return res
    if text is None:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            res.skipped = str(exc)
            return res
    try:
        fn(path, text, res)
    except Exception as exc:  # noqa: BLE001 - a checker bug must never break a tool call
        res.skipped = f"checker failed: {exc}"
    res.problems.sort(key=lambda p: p.line)
    return res


# ------------------------------------------------------------ presentation

def summary(res: CheckResult) -> str:
    """Short UI summary, e.g. '✓ syntax ok' or '✗ 2 problems'."""
    if not res.checked:
        return ""
    if res.errors:
        n = len(res.errors)
        return f"✗ {n} problem{'s' if n != 1 else ''}"
    if res.incomplete:
        return "… incomplete"
    return "✓ checks pass"


def for_model(res: CheckResult, text: Optional[str], rel: str, final: bool = False) -> str:
    """Diagnostics appended to a tool result so the model can fix them now."""
    if not res.checked:
        return ""
    tools = ", ".join(res.checkers)
    if not res.errors and not res.incomplete:
        return f"\n✓ Checked {rel} ({tools}): no problems found."
    lines = (text or "").split("\n")
    out = []
    if res.errors:
        out.append(f"\n✗ Automatic check found {len(res.errors)} problem(s) in {rel} "
                   f"({tools}). Fix them now with edit_file:")
        for p in res.errors[:12]:
            where = f" [{p.where}]" if p.where else ""
            out.append(f"  line {p.line}: {p.message}{where}")
            if 1 <= p.line <= len(lines):
                out.append(f"  {p.line:>6} | {lines[p.line - 1][:200]}")
        if len(res.errors) > 12:
            out.append(f"  … and {len(res.errors) - 12} more")
    if res.incomplete:
        what = "; ".join(dict.fromkeys(res.incomplete))[:300]
        if final:
            out.append(f"\n✗ {rel} is unfinished: {what}. Complete the file.")
        else:
            out.append(f"\n… {rel} looks unfinished ({what}). That's fine if you are still "
                       "writing it in chunks — continue with append=true until it is complete.")
    return "\n".join(out)
