"""The tools the model can call, plus permission classification.

Every tool returns a ToolResult. `output` goes back to the model; `summary`
and `detail` are for the UI. Errors are returned (never raised) so the model
can read them and correct itself.
"""

from __future__ import annotations

import fnmatch
import os
import re
import shlex
import shutil
import signal
import subprocess
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from . import __version__
from .config import Config
from .edits import EditError, apply_edit, diff_stats, unified_diff

IGNORED_DIRS = {
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv",
    "env", ".env", "dist", "build", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    ".tox", "target", ".next", ".nuxt", ".cache", ".idea", ".vscode", ".gradle",
    "coverage", ".terraform", "site-packages", ".eggs",
}
BINARY_EXT = {
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".webp", ".pdf", ".zip",
    ".gz", ".tar", ".xz", ".7z", ".so", ".dylib", ".dll", ".exe", ".bin", ".o",
    ".a", ".class", ".jar", ".pyc", ".woff", ".woff2", ".ttf", ".mp3", ".mp4",
    ".mov", ".gguf", ".safetensors", ".sqlite", ".db",
}

EDIT_TOOLS = {"write_file", "edit_file"}
SHELL_TOOLS = {"bash"}

# commands that are always safe to run without asking (read-only)
SAFE_COMMANDS = {
    "ls", "pwd", "cat", "head", "tail", "wc", "echo", "printf", "which", "whoami",
    "date", "uname", "file", "stat", "du", "df", "tree", "grep", "rg", "egrep",
    "fgrep", "sort", "uniq", "cut", "basename", "dirname", "realpath",
    "true", "diff", "cmp", "md5sum", "sha256sum", "less", "type", "id", "nl",
}
SAFE_GIT = {"status", "diff", "log", "show", "branch", "remote", "rev-parse",
            "ls-files", "blame", "describe", "tag", "shortlog", "config"}
DANGEROUS = [
    (re.compile(r"\brm\s+(-[a-zA-Z]*[rf][a-zA-Z]*\s+)+(/|~|\$HOME|\*|\.\.?)(\s|/?$)"),
     "recursive delete of a root/home/whole directory"),
    (re.compile(r"\bmkfs(\.\w+)?\b"), "formats a filesystem"),
    (re.compile(r"\bdd\s+.*\bof=/dev/"), "writes raw to a device"),
    (re.compile(r":\(\)\s*\{\s*:\|:&\s*\};:"), "fork bomb"),
    (re.compile(r"\b(shutdown|reboot|halt|poweroff)\b"), "powers off the machine"),
    (re.compile(r"\bchmod\s+-R\s+0?777\s+/"), "opens permissions on the whole system"),
    (re.compile(r"\bgit\s+push\b.*(--force|-f\b)"), "force-pushes git history"),
    (re.compile(r"\bgit\s+(reset\s+--hard|clean\s+-[a-z]*f)"), "discards local changes"),
    (re.compile(r"(curl|wget)\b[^|]*\|\s*(sudo\s+)?(ba|z)?sh\b"), "pipes a download into a shell"),
    (re.compile(r"\bsudo\b"), "runs as root"),
    (re.compile(r">\s*/dev/sd[a-z]"), "overwrites a disk"),
]


@dataclass
class ToolResult:
    output: str                     # sent to the model
    ok: bool = True
    summary: str = ""               # one-line UI summary
    detail: str = ""                # expandable UI body (plain text)
    diff: str = ""                  # unified diff for edits
    meta: dict = field(default_factory=dict)


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    head = text[: int(limit * 0.7)]
    tail = text[-int(limit * 0.25):]
    cut = len(text) - len(head) - len(tail)
    return f"{head}\n\n… [{cut} chars truncated — narrow the request to see more] …\n\n{tail}"


def _is_binary(p: Path) -> bool:
    if p.suffix.lower() in BINARY_EXT:
        return True
    try:
        with open(p, "rb") as fh:
            chunk = fh.read(4096)
        return b"\0" in chunk
    except OSError:
        return False


def _schema(name: str, desc: str, props: dict, required: list[str]) -> dict:
    return {"type": "function", "function": {
        "name": name, "description": desc,
        "parameters": {"type": "object", "properties": props, "required": required}}}


TOOL_SCHEMAS: list[dict] = [
    _schema("read_file",
            "Read a text file. Returns numbered lines. Always read a file before editing it.",
            {"path": {"type": "string", "description": "file path (relative to project root)"},
             "offset": {"type": "integer", "description": "first line, 1-based (optional)"},
             "limit": {"type": "integer", "description": "max lines, default 400 (optional)"}},
            ["path"]),
    _schema("edit_file",
            "Replace text in an existing file. old_string must be copied exactly from "
            "the file (no line numbers) and be unique; include a few lines of context.",
            {"path": {"type": "string"},
             "old_string": {"type": "string", "description": "exact text to replace"},
             "new_string": {"type": "string", "description": "replacement text"},
             "replace_all": {"type": "boolean", "description": "replace every occurrence"}},
            ["path", "old_string", "new_string"]),
    _schema("write_file",
            "Create or overwrite a file. Write big files in chunks of ~80 lines: the first "
            "chunk normally, each next chunk with append=true. Prefer edit_file for small "
            "changes.",
            {"path": {"type": "string"}, "content": {"type": "string"},
             "append": {"type": "boolean", "description": "add to the end instead of overwriting"}},
            ["path", "content"]),
    _schema("bash",
            "Run a shell command in the project root (each call starts there; use "
            "`cd dir && cmd`). Use for tests, builds, git, installing, running code.",
            {"command": {"type": "string"},
             "timeout": {"type": "integer", "description": "seconds, default 120"}},
            ["command"]),
    _schema("glob",
            "Find files by name pattern, e.g. '**/*.py' or 'src/**/test_*.ts'. "
            "Newest first.",
            {"pattern": {"type": "string"},
             "path": {"type": "string", "description": "directory to search (optional)"}},
            ["pattern"]),
    _schema("grep",
            "Search file contents with a regular expression. Returns file:line:text.",
            {"pattern": {"type": "string"},
             "path": {"type": "string", "description": "file or directory (optional)"},
             "include": {"type": "string", "description": "file glob filter, e.g. '*.py'"},
             "ignore_case": {"type": "boolean"}},
            ["pattern"]),
    _schema("list_dir",
            "Show a directory tree (2 levels deep by default).",
            {"path": {"type": "string"}, "depth": {"type": "integer"}}, []),
    _schema("todowrite",
            "Create or update your task list for multi-step work. Send the full list "
            "every time. status: pending | in_progress | completed.",
            {"todos": {"type": "array", "items": {
                "type": "object",
                "properties": {"content": {"type": "string"},
                               "status": {"type": "string",
                                          "enum": ["pending", "in_progress", "completed"]}},
                "required": ["content", "status"]}}},
            ["todos"]),
    _schema("fetch_url",
            "Fetch a web page or API URL and return its text.",
            {"url": {"type": "string"}}, ["url"]),
]
TOOL_NAMES = [t["function"]["name"] for t in TOOL_SCHEMAS]


class Tools:
    def __init__(self, workdir: Path, cfg: Config,
                 on_todos: Callable[[list[dict]], None] | None = None):
        self.workdir = workdir
        self.cfg = cfg
        self.on_todos = on_todos
        self.todos: list[dict] = []
        self.todos_auto = False     # True while the list is tinycode's own step log
        self.read_files: set[str] = set()
        # path -> (mtime_ns, size) when the model last saw the whole file
        self.seen: dict[str, tuple[int, int]] = {}
        # undo support: path -> original bytes (None = file did not exist)
        self.checkpoint: dict[str, Optional[bytes]] = {}
        self.changed: dict[str, list[int]] = {}   # path -> [added, removed]
        self.cancel = threading.Event()
        self.on_output: Callable[[str], None] | None = None  # live bash output
        self._rg = shutil.which("rg")

    # ------------------------------------------------------------- helpers
    def resolve(self, path: Any) -> Path:
        s = str(path or ".").strip().strip("'\"")
        if s.startswith("@"):
            s = s[1:]
        p = Path(os.path.expanduser(s))
        p = p if p.is_absolute() else (self.workdir / p)
        try:
            return p.resolve()
        except OSError:
            return p

    def rel(self, p: Path) -> str:
        try:
            r = os.path.relpath(p, self.workdir)
        except ValueError:
            return str(p)
        return str(p) if r.startswith("..") else r

    def inside(self, p: Path) -> bool:
        try:
            p.resolve().relative_to(self.workdir.resolve())
            return True
        except (ValueError, OSError):
            return False

    def _limit(self) -> int:
        # never let one tool result eat more than ~40% of the context window
        return max(2000, min(self.cfg.max_tool_chars, int(self.cfg.num_ctx * 3.5 * 0.4)))

    def _record(self, p: Path) -> None:
        key = str(p)
        if key not in self.checkpoint:
            self.checkpoint[key] = p.read_bytes() if p.exists() else None

    def _track(self, p: Path, diff: str) -> None:
        a, r = diff_stats(diff)
        cur = self.changed.setdefault(self.rel(p), [0, 0])
        cur[0] += a
        cur[1] += r

    # ------------------------------------------------------- permissions
    def classify(self, name: str, args: dict, mode: str) -> tuple[bool, str]:
        """Return (needs_approval, warning)."""
        if name in EDIT_TOOLS:
            p = self.resolve(args.get("path"))
            if not self.inside(p):
                return True, f"writes outside the project: {p}"
            return mode == "ask", ""
        if name in SHELL_TOOLS:
            cmd = str(args.get("command", ""))
            for rx, why in DANGEROUS:
                if rx.search(cmd):
                    return True, f"⚠ this command {why}"
            if is_safe_command(cmd):
                return False, ""
            return mode != "yolo", ""
        return False, ""

    def preview(self, name: str, args: dict) -> tuple[str, Optional[str]]:
        """Dry-run an edit. Returns (diff, error). An error means the call
        would fail anyway, so there is no need to ask the user."""
        try:
            p = self.resolve(args.get("path"))
            if name == "write_file":
                before = p.read_text(encoding="utf-8", errors="replace") if p.is_file() else ""
                after = str(args.get("content", ""))
                if args.get("append") and before:
                    after = before + ("" if before.endswith("\n") else "\n") + after
                return unified_diff(before, after, self.rel(p)), None
            if name == "edit_file":
                if not p.is_file():
                    return "", f"no such file: {self.rel(p)}"
                before = p.read_text(encoding="utf-8", errors="replace")
                after, _, _ = apply_edit(before, str(args.get("old_string", "")),
                                         str(args.get("new_string", "")),
                                         bool(args.get("replace_all")))
                return unified_diff(before, after, self.rel(p)), None
        except EditError as exc:
            return "", str(exc)
        except OSError as exc:
            return "", str(exc)
        return "", None

    # ------------------------------------------------------------ dispatch
    def execute(self, name: str, args: dict) -> ToolResult:
        handler = getattr(self, f"t_{name}", None)
        if handler is None:
            return ToolResult(f"ERROR: unknown tool '{name}'. Available: "
                              + ", ".join(TOOL_NAMES), ok=False,
                              summary=f"unknown tool {name}")
        try:
            return handler(**args)
        except TypeError as exc:
            msg = str(exc).replace("t_", "", 1)
            return ToolResult(f"ERROR: bad arguments for {name}: {msg}", ok=False,
                              summary="bad arguments")
        except Exception as exc:  # noqa: BLE001
            return ToolResult(f"ERROR: {type(exc).__name__}: {exc}", ok=False,
                              summary=f"{type(exc).__name__}: {exc}"[:120])

    # --------------------------------------------------------------- tools
    def t_read_file(self, path: str, offset: int = 1, limit: int = 400) -> ToolResult:
        p = self.resolve(path)
        if not p.exists():
            hint = self._similar_paths(p.name)
            return ToolResult(f"ERROR: file not found: {self.rel(p)}{hint}", ok=False,
                              summary="file not found")
        if p.is_dir():
            return ToolResult(f"ERROR: {self.rel(p)} is a directory; use list_dir.",
                              ok=False, summary="is a directory")
        if _is_binary(p):
            return ToolResult(f"ERROR: {self.rel(p)} is a binary file "
                              f"({p.stat().st_size} bytes).", ok=False, summary="binary file")
        offset = max(1, int(offset or 1))
        limit = min(max(1, int(limit or 400)), 2000)
        st = p.stat()
        text = p.read_text(encoding="utf-8", errors="replace")
        lines = text.splitlines()
        if offset == 1 and limit >= len(lines) and \
                self.seen.get(str(p)) == (st.st_mtime_ns, st.st_size):
            return ToolResult(
                f"{self.rel(p)} has not changed since you last read or wrote it "
                f"({len(lines)} lines), so its content is already in this conversation. "
                "If the task is done, stop and give the user a short summary.",
                summary="unchanged since last seen — skipped re-read")
        chunk = lines[offset - 1: offset - 1 + limit]
        body = "\n".join(f"{i:>6}\t{l if len(l) <= 500 else l[:500] + ' …'}"
                         for i, l in enumerate(chunk, start=offset))
        end = offset - 1 + len(chunk)
        if not lines:
            body = "(empty file)"
        header = f"{self.rel(p)} — {len(lines)} lines"
        if offset > 1 or end < len(lines):
            header += f" (showing {offset}-{end}; use offset to read more)"
        self.read_files.add(str(p))
        if offset == 1 and end >= len(lines):
            self.seen[str(p)] = (st.st_mtime_ns, st.st_size)
        out = _truncate(f"{header}\n{body}", self._limit())
        return ToolResult(out, summary=f"read {len(chunk)} lines", detail=body)

    def t_write_file(self, path: str, content: str = "", append: bool = False) -> ToolResult:
        p = self.resolve(path)
        if p.is_dir():
            return ToolResult(f"ERROR: {self.rel(p)} is a directory.", ok=False,
                              summary="is a directory")
        content = str(content)
        before = p.read_text(encoding="utf-8", errors="replace") if p.exists() else ""
        existed = p.exists()
        if append and existed:
            sep = "" if not before or before.endswith("\n") else "\n"
            content = before + sep + content
        self._record(p)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        diff = unified_diff(before, content, self.rel(p))
        self._track(p, diff)
        self.read_files.add(str(p))
        st = p.stat()
        self.seen[str(p)] = (st.st_mtime_ns, st.st_size)
        n = content.count("\n") + (0 if content.endswith("\n") or not content else 1)
        verb = "appended to" if (append and existed) else ("overwrote" if existed else "created")
        add, rem = diff_stats(diff)
        return ToolResult(f"{verb} {self.rel(p)} (now {n} lines)",
                          summary=f"{verb} · {n} lines (+{add} -{rem})", diff=diff)

    def t_edit_file(self, path: str, old_string: str = "", new_string: str = "",
                    replace_all: bool = False) -> ToolResult:
        p = self.resolve(path)
        if not p.is_file():
            hint = self._similar_paths(p.name)
            return ToolResult(f"ERROR: file not found: {self.rel(p)}{hint}", ok=False,
                              summary="file not found")
        before = p.read_text(encoding="utf-8", errors="replace")
        try:
            after, n, strategy = apply_edit(before, str(old_string), str(new_string),
                                            bool(replace_all))
        except EditError as exc:
            return ToolResult(f"ERROR: {exc}", ok=False, summary=str(exc).split("\n")[0])
        self._record(p)
        p.write_text(after, encoding="utf-8")
        diff = unified_diff(before, after, self.rel(p))
        self._track(p, diff)
        add, rem = diff_stats(diff)
        note = "" if strategy == "exact" else f" (matched via {strategy})"
        return ToolResult(f"edited {self.rel(p)}: {n} replacement(s){note}. "
                          f"+{add} -{rem} lines.",
                          summary=f"+{add} -{rem}{note}", diff=diff)

    def _walk(self, base: Path):
        for root, dirs, files in os.walk(base):
            dirs[:] = sorted(d for d in dirs if d not in IGNORED_DIRS
                             and not d.endswith(".egg-info"))
            yield root, dirs, files

    def _similar_paths(self, name: str) -> str:
        if not name:
            return ""
        found = []
        for root, _d, files in self._walk(self.workdir):
            for f in files:
                if f == name or f.lower() == name.lower():
                    found.append(self.rel(Path(root) / f))
            if len(found) >= 5:
                break
        return (" Did you mean: " + ", ".join(found)) if found else ""

    def t_list_dir(self, path: str = ".", depth: int = 2) -> ToolResult:
        p = self.resolve(path)
        if not p.is_dir():
            return ToolResult(f"ERROR: not a directory: {self.rel(p)}", ok=False,
                              summary="not a directory")
        depth = max(1, min(int(depth or 2), 4))
        lines: list[str] = []
        count = [0]

        def walk(d: Path, level: int, prefix: str) -> None:
            try:
                entries = sorted(d.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower()))
            except OSError:
                return
            entries = [e for e in entries if not (e.is_dir() and e.name in IGNORED_DIRS)]
            for e in entries:
                if count[0] >= 400:
                    return
                count[0] += 1
                if e.is_dir():
                    lines.append(f"{prefix}{e.name}/")
                    if level < depth:
                        walk(e, level + 1, prefix + "  ")
                else:
                    try:
                        size = e.stat().st_size
                    except OSError:
                        size = 0
                    lines.append(f"{prefix}{e.name}  ({_human(size)})")

        walk(p, 1, "")
        if not lines:
            return ToolResult(f"{self.rel(p)}/ is empty", summary="empty")
        more = "\n… (truncated)" if count[0] >= 400 else ""
        out = f"{self.rel(p)}/\n" + "\n".join(lines) + more
        return ToolResult(_truncate(out, self._limit()),
                          summary=f"{count[0]} entries", detail=out)

    def t_glob(self, pattern: str, path: str = ".") -> ToolResult:
        base = self.resolve(path)
        pattern = str(pattern).strip()
        if not base.is_dir():
            return ToolResult(f"ERROR: not a directory: {self.rel(base)}", ok=False,
                              summary="not a directory")
        matches: list[Path] = []
        has_slash = "/" in pattern
        pat = pattern[2:] if pattern.startswith("./") else pattern
        for root, _dirs, files in self._walk(base):
            for f in files:
                fp = Path(root) / f
                relp = os.path.relpath(fp, base)
                if (fnmatch.fnmatch(relp, pat)
                        or (pat.startswith("**/") and fnmatch.fnmatch(relp, pat[3:]))
                        or (not has_slash and fnmatch.fnmatch(f, pat))):
                    matches.append(fp)
            if len(matches) > 2000:
                break
        if not matches:
            return ToolResult(f"no files match {pattern!r}", summary="no matches")

        def mtime(p: Path) -> float:
            try:
                return p.stat().st_mtime
            except OSError:
                return 0.0
        matches.sort(key=mtime, reverse=True)
        shown = matches[:200]
        out = "\n".join(self.rel(m) for m in shown)
        if len(matches) > 200:
            out += f"\n… and {len(matches) - 200} more"
        return ToolResult(_truncate(out, self._limit()),
                          summary=f"{len(matches)} file(s)", detail=out)

    def t_grep(self, pattern: str, path: str = ".", include: str | None = None,
               ignore_case: bool = False) -> ToolResult:
        base = self.resolve(path)
        if not base.exists():
            return ToolResult(f"ERROR: no such path: {self.rel(base)}", ok=False,
                              summary="no such path")
        lines: list[str] = []
        if self._rg:
            cmd = [self._rg, "--line-number", "--no-heading", "--color=never",
                   "--max-columns", "300", "--max-count", "50", "--hidden",
                   "--glob", "!.git"]
            if ignore_case:
                cmd.append("-i")
            if include:
                cmd += ["--glob", include]
            cmd += ["-e", str(pattern), str(base)]
            try:
                proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60,
                                      cwd=str(self.workdir))
            except subprocess.TimeoutExpired:
                return ToolResult("ERROR: search timed out", ok=False, summary="timeout")
            if proc.returncode == 2 and not proc.stdout:
                return ToolResult(f"ERROR: {proc.stderr.strip()[:300]}", ok=False,
                                  summary="bad pattern")
            prefix = str(self.workdir) + os.sep
            lines = [l.replace(prefix, "", 1) for l in proc.stdout.splitlines()]
        else:
            try:
                rx = re.compile(str(pattern), re.I if ignore_case else 0)
            except re.error as exc:
                return ToolResult(f"ERROR: bad regex: {exc}", ok=False, summary="bad regex")
            files = [base] if base.is_file() else (
                Path(r) / f for r, _d, fs in self._walk(base) for f in fs)
            for fp in files:
                if include and not fnmatch.fnmatch(fp.name, include):
                    continue
                try:
                    if fp.stat().st_size > 2_000_000 or _is_binary(fp):
                        continue
                    for i, line in enumerate(fp.read_text(encoding="utf-8",
                                                          errors="ignore").splitlines(), 1):
                        if rx.search(line):
                            lines.append(f"{self.rel(fp)}:{i}:{line[:300]}")
                except OSError:
                    continue
                if len(lines) >= 500:
                    break
        if not lines:
            return ToolResult(f"no matches for {pattern!r}", summary="no matches")
        total = len(lines)
        out = "\n".join(lines[:250])
        if total > 250:
            out += f"\n… {total - 250} more matches (narrow the pattern or path)"
        nfiles = len({l.split(':', 1)[0] for l in lines})
        return ToolResult(_truncate(out, self._limit()),
                          summary=f"{total} match(es) in {nfiles} file(s)", detail=out)

    def t_bash(self, command: str, timeout: int = 0) -> ToolResult:
        command = str(command).strip()
        if not command:
            return ToolResult("ERROR: empty command", ok=False, summary="empty command")
        timeout = int(timeout or self.cfg.bash_timeout)
        timeout = max(1, min(timeout, 1800))
        env = dict(os.environ)
        env.update({"PAGER": "cat", "GIT_PAGER": "cat", "GIT_TERMINAL_PROMPT": "0",
                    "TERM": "dumb", "NO_COLOR": "1", "PYTHONUNBUFFERED": "1",
                    "TINYCODE": "1", "DEBIAN_FRONTEND": "noninteractive"})
        shell = "/bin/bash" if os.path.exists("/bin/bash") else None
        start = time.monotonic()
        try:
            proc = subprocess.Popen(
                command, shell=True, executable=shell, cwd=str(self.workdir),
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                env=env, start_new_session=True)
        except OSError as exc:
            return ToolResult(f"ERROR: {exc}", ok=False, summary=str(exc))
        chunks: list[str] = []
        size = [0]

        def reader() -> None:
            assert proc.stdout is not None
            for raw in iter(proc.stdout.readline, b""):
                s = raw.decode("utf-8", "replace")
                if size[0] < 2_000_000:
                    chunks.append(s)
                    size[0] += len(s)
                if self.on_output:
                    try:
                        self.on_output(s)
                    except Exception:  # noqa: BLE001
                        pass

        t = threading.Thread(target=reader, daemon=True)
        t.start()
        status = None
        while True:
            try:
                proc.wait(timeout=0.1)
                break
            except subprocess.TimeoutExpired:
                pass
            if self.cancel.is_set():
                status = "interrupted by the user"
                break
            if time.monotonic() - start > timeout:
                status = f"timed out after {timeout}s"
                break
        if status:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
                time.sleep(0.3)
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                pass
        t.join(timeout=2)
        out = "".join(chunks).rstrip()
        elapsed = time.monotonic() - start
        code = proc.returncode if proc.returncode is not None else -1
        if status:
            text = f"$ {command}\n{out}\n[{status}]"
            return ToolResult(_truncate(text, self._limit()), ok=False,
                              summary=status, detail=out)
        text = f"$ {command}\n{out if out else '(no output)'}\n[exit code {code}]"
        nlines = out.count("\n") + 1 if out else 0
        return ToolResult(_truncate(text, self._limit()), ok=(code == 0),
                          summary=f"exit {code} · {nlines} lines · {elapsed:.1f}s",
                          detail=out, meta={"exit": code})

    def t_fetch_url(self, url: str) -> ToolResult:
        url = str(url).strip()
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        try:
            req = urllib.request.Request(url, headers={"User-Agent": f"tinycode/{__version__}"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                ctype = resp.headers.get("Content-Type", "")
                raw = resp.read(1_000_000)
        except urllib.error.HTTPError as exc:
            return ToolResult(f"ERROR: HTTP {exc.code} for {url}", ok=False,
                              summary=f"HTTP {exc.code}")
        except (urllib.error.URLError, OSError) as exc:
            return ToolResult(f"ERROR: {getattr(exc, 'reason', exc)}", ok=False,
                              summary="fetch failed")
        text = raw.decode("utf-8", "replace")
        if "html" in ctype or text.lstrip().lower().startswith(("<!doctype", "<html")):
            text = html_to_text(text)
        return ToolResult(_truncate(text.strip(), self._limit()),
                          summary=f"{len(text)} chars", detail=text[:20000])

    def t_todowrite(self, todos: Any = None) -> ToolResult:
        if isinstance(todos, str):
            import json
            try:
                todos = json.loads(todos)
            except ValueError:
                todos = [{"content": l.strip("-*• ").strip(), "status": "pending"}
                         for l in todos.splitlines() if l.strip()]
        clean: list[dict] = []
        for t in todos or []:
            if isinstance(t, str):
                t = {"content": t, "status": "pending"}
            if not isinstance(t, dict):
                continue
            status = str(t.get("status", "pending")).lower().replace("-", "_").replace(" ", "_")
            if status in ("done", "complete", "finished"):
                status = "completed"
            if status in ("in-progress", "active", "doing", "started"):
                status = "in_progress"
            if status not in ("pending", "in_progress", "completed"):
                status = "pending"
            content = str(t.get("content") or t.get("task") or t.get("title") or "").strip()
            if content:
                clean.append({"content": content, "status": status})
        self.todos = clean
        self.todos_auto = False
        if self.on_todos:
            self.on_todos(clean)
        done = sum(1 for t in clean if t["status"] == "completed")
        listing = "\n".join(
            f"[{'x' if t['status'] == 'completed' else ('~' if t['status'] == 'in_progress' else ' ')}] "
            f"{t['content']}" for t in clean)
        return ToolResult(f"todo list updated ({done}/{len(clean)} done):\n{listing}",
                          summary=f"{done}/{len(clean)} done", detail=listing)

    # ---------------------------------------------------------------- undo
    def take_checkpoint(self) -> dict[str, Optional[bytes]]:
        cp, self.checkpoint = self.checkpoint, {}
        return cp

    def restore(self, cp: dict[str, Optional[bytes]]) -> list[str]:
        restored = []
        for path, data in cp.items():
            p = Path(path)
            try:
                if data is None:
                    if p.exists():
                        p.unlink()
                else:
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_bytes(data)
                restored.append(self.rel(p))
                self.changed.pop(self.rel(p), None)
            except OSError:
                pass
        return restored


def _human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024  # type: ignore[assignment]
    return f"{n:.1f} TB"


def html_to_text(html: str) -> str:
    html = re.sub(r"(?is)<(script|style|noscript|svg|head)[^>]*>.*?</\1>", " ", html)
    html = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</h\d>|</li>|</tr>", "\n", html)
    html = re.sub(r"(?i)<li[^>]*>", "• ", html)
    html = re.sub(r"<[^>]+>", " ", html)
    import html as htmlmod
    text = htmlmod.unescape(html)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return text.strip()


def is_safe_command(cmd: str) -> bool:
    """True for read-only commands that never need approval."""
    if not cmd or re.search(r"[>`]|\$\(|<\(|\btee\b", cmd):
        return False
    for part in re.split(r"&&|\|\||;|\|", cmd):
        part = part.strip()
        if not part:
            continue
        try:
            words = shlex.split(part)
        except ValueError:
            return False
        if not words:
            continue
        exe = os.path.basename(words[0])
        if exe == "git":
            sub = next((w for w in words[1:] if not w.startswith("-")), "")
            if sub not in SAFE_GIT:
                return False
            if sub in ("branch", "tag", "config", "remote") and len(words) > 2 and \
                    any(not w.startswith("-") for w in words[2:]) and sub != "config":
                return False
            if sub == "config" and not any(w in ("--get", "--list", "-l") for w in words):
                return False
            continue
        if exe == "find":
            if any(w in ("-exec", "-execdir", "-delete", "-ok", "-fprint") for w in words):
                return False
            continue
        if exe in ("python", "python3", "node", "go", "cargo", "rustc", "java") and \
                words[1:] in (["--version"], ["-V"], ["version"]):
            continue
        if exe == "sed" and "-i" not in " ".join(words) and "-n" in words:
            continue
        if exe not in SAFE_COMMANDS:
            return False
        if exe == "sort" and any(w.startswith("-o") or w == "--output" for w in words):
            return False
    return True
