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
import tempfile
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from . import __version__
from .config import Config
from . import checks, webcheck
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
# Commands that always need approval, even in yolo mode. Matched on the
# *command words* of each pipeline segment (via a shell-like tokenizer), so
# code inside quotes -- e.g. python -c "server.shutdown()" -- never matches.
_ROOTISH = {"/", "/*", "~", "~/", "~/*", "$HOME", "$HOME/", "${HOME}", "*", ".", "..", "./*",
            "/bin", "/boot", "/dev", "/etc", "/home", "/lib", "/opt", "/root", "/sbin",
            "/srv", "/usr", "/var"}
_WRAPPERS = {"sudo", "doas", "nohup", "time", "exec", "env", "nice", "command", "builtin",
             "xargs", "stdbuf", "timeout"}
_SHELLS = {"sh", "bash", "zsh", "dash", "ksh", "fish"}


def _segments(cmd: str) -> list[list[str]]:
    """Split a shell command into pipeline segments of words (quotes respected).
    Returns [] when it can't be tokenized (unbalanced quotes)."""
    lex = shlex.shlex(cmd, posix=True, punctuation_chars=";&|()<>")
    lex.whitespace_split = True
    lex.commenters = ""
    segs: list[list[str]] = [[]]
    try:
        for tok in lex:
            if tok and set(tok) <= set(";&|()\n"):
                segs.append([tok])          # keep the operator as its own marker
                segs.append([])
            else:
                segs[-1].append(tok)
    except ValueError:
        return []
    return [s for s in segs if s]


def _command_words(words: list[str]) -> list[str]:
    """Strip leading VAR=x assignments and wrappers like sudo/nohup/env."""
    i = 0
    while i < len(words):
        w = words[i]
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", w):
            i += 1
        elif os.path.basename(w) in _WRAPPERS:
            i += 1
            while i < len(words) and words[i].startswith("-"):   # e.g. sudo -u x
                i += 2 if words[i] in ("-u", "-g", "-n") else 1
        else:
            break
    return words[i:]


def danger_reason(cmd: str) -> str:
    """Why a shell command is dangerous, or '' if it isn't."""
    if re.search(r":\(\)\s*\{\s*:\|:&\s*\};\s*:", cmd):
        return "is a fork bomb"
    segs = _segments(cmd)
    if not segs:
        segs = [cmd.split()]
    for seg in segs:
        first = next((w for w in seg if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", w)), "")
        if os.path.basename(first) in ("sudo", "doas", "pkexec"):
            return "runs as root (sudo)"
    prev_op = ""
    prev_cmd = ""
    for seg in segs:
        if len(seg) == 1 and set(seg[0]) <= set(";&|()\n"):
            prev_op = seg[0]
            continue
        words = _command_words(seg)
        if not words:
            continue
        exe = os.path.basename(words[0])
        args = words[1:]
        flags = "".join(a[1:] for a in args if a.startswith("-") and not a.startswith("--"))
        if exe in ("shutdown", "reboot", "halt", "poweroff"):
            return "powers off or restarts the machine"
        if exe == "systemctl" and any(a in ("poweroff", "reboot", "halt", "suspend",
                                            "hibernate", "kexec") for a in args):
            return "powers off or restarts the machine"
        if exe in ("init", "telinit") and any(a in ("0", "6") for a in args):
            return "powers off or restarts the machine"
        if exe == "rm" and ("r" in flags.lower() or "--recursive" in args) and \
                any(a.rstrip("/") in {r.rstrip("/") for r in _ROOTISH} or a in _ROOTISH
                    for a in args if not a.startswith("-")):
            return "deletes a whole system, home or project directory"
        if exe.startswith("mkfs") or exe in ("wipefs", "fdisk", "parted", "sfdisk"):
            return "formats or repartitions a disk"
        if exe == "dd" and any(a.startswith("of=/dev/") for a in args):
            return "writes raw data to a device"
        if exe in ("chmod", "chown") and ("R" in flags or "--recursive" in args) and \
                any(a in ("/", "/*") for a in args):
            return "changes permissions on the whole system"
        if exe == "git" and args:
            sub = next((a for a in args if not a.startswith("-")), "")
            if sub == "push" and any(a in ("--force", "-f", "--force-with-lease") or
                                     (a.startswith("-") and not a.startswith("--") and "f" in a)
                                     for a in args):
                return "force-pushes git history"
            if sub == "reset" and "--hard" in args:
                return "discards local changes (git reset --hard)"
            if sub == "clean" and "f" in flags:
                return "deletes untracked files (git clean)"
        if exe in _SHELLS and prev_op == "|" and prev_cmd in ("curl", "wget"):
            return "pipes a download straight into a shell"
        prev_cmd = exe
        prev_op = ""
    for seg in segs:                         # > /dev/sdX redirections
        for i, w in enumerate(seg[:-1]):
            if w in (">", ">>", "1>", "2>") and \
                    re.match(r"/dev/(sd|nvme|hd|vd|mmcblk|disk)", seg[i + 1]):
                return "overwrites a disk"
    return ""


# device files that are always fine to touch from inside the sandbox
_DEV_OK = {"/dev/null", "/dev/zero", "/dev/full", "/dev/random", "/dev/urandom",
           "/dev/stdin", "/dev/stdout", "/dev/stderr", "/dev/tty"}


def _token_outside(token: str, root: Path) -> bool:
    """True if a single shell word points at a path outside `root`."""
    t = token
    if not t or t in _DEV_OK or t.startswith("/dev/fd/"):
        return False
    if t.startswith("--") and "=" in t:
        t = t.split("=", 1)[1]
    elif t.startswith("-"):
        return False                     # an option, not a path argument
    if not (t in (".", "..") or "/" in t or t.startswith("~")):
        return False
    t = t.replace("${HOME}", str(Path.home())).replace("$HOME", str(Path.home()))
    t = os.path.expanduser(t)
    p = Path(t)
    if not p.is_absolute():
        p = root / p
    try:
        p = p.resolve()
    except OSError:
        p = Path(os.path.abspath(str(p)))
    try:
        p.relative_to(root)
        return False
    except ValueError:
        return True


def escape_reason(cmd: str, workdir: Path) -> str:
    """Why a shell command reaches outside the project directory, or ''.

    This is best-effort: it inspects the command words and scans quoted
    strings for absolute paths. It cannot sandbox arbitrary code (e.g. a
    script that builds a path at runtime), but it stops the model from
    plainly reading or writing outside the project (../, ~, /etc, ...)."""
    try:
        root = Path(workdir).resolve()
    except OSError:
        root = Path(workdir)
    segs = _segments(cmd)
    if segs:
        words = [w for seg in segs for w in seg]
    else:
        try:
            words = shlex.split(cmd, posix=True)
        except ValueError:
            return "the command could not be parsed, so it stays inside the project"
    for w in words:
        if _token_outside(w, root):
            return f"it reaches outside the project directory ({w})"
    # catch paths hidden inside quotes, e.g. python -c "open('/etc/passwd')"
    for m in re.finditer(r"(?<![\w:/.-])(/(?![/\s])[^\s'\"`)]+)", cmd):
        tok = m.group(1)
        if _token_outside(tok, root):
            return f"it reaches outside the project directory ({tok})"
    # relative escapes hidden in quoted code: open('../secret'), sh -c "cd ../.."
    for m in re.finditer(r"\.\.(?:/[^\s'\"`)]*)+", cmd):
        tok = m.group(0)
        if _token_outside(tok, root):
            return f"it reaches outside the project directory ({tok})"
    # home-relative paths hidden in quoted code: open('~/secret')
    for m in re.finditer(r"(?<![\w~])~/[^\s'\"`)]*", cmd):
        tok = m.group(0)
        if _token_outside(tok, root):
            return f"it reaches outside the project directory ({tok})"
    return ""


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
    _schema("test_app",
            "Run a web page (HTML/JS) in a simulated browser: loads it, clicks every button, "
            "presses keys and reports crashes, broken wiring and wrong results. Optional steps "
            "test the main feature, e.g. [{\"click\": \"5\"}, {\"click\": \"+\"}, "
            "{\"click\": \"2\"}, {\"click\": \"=\"}, {\"expect\": [\"#display\", \"7\"]}]. "
            "Step kinds: click (button text or CSS selector), type [selector, text], key, "
            "wait (ms), expect [selector, text].",
            {"path": {"type": "string", "description": "the .html file (optional)"},
             "steps": {"type": "array", "items": {"type": "object"},
                       "description": "optional scenario to check behaviour"}}, []),
    _schema("check",
            "Check code for syntax errors and obvious bugs (undefined names, unclosed "
            "tags/braces, invalid JSON…). Give a file or folder, or nothing to check every "
            "file changed so far. Files are also checked automatically after each change.",
            {"path": {"type": "string", "description": "file or folder (optional)"}}, []),
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
        self.problems: dict[str, "checks.CheckResult"] = {}  # rel path -> latest failed check
        self.checks_run = 0
        self._base_cache: dict[str, Optional[set]] = {}
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

    # ------------------------------------------------------------ sandbox
    def blocked(self, name: str, args: dict) -> str:
        """If the sandbox forbids this call, return a short reason. '' = allowed."""
        if not self.cfg.sandbox:
            return ""
        if name in SHELL_TOOLS:
            return escape_reason(str(args.get("command", "")), self.workdir)
        if name in EDIT_TOOLS or name in ("read_file", "list_dir", "glob", "grep",
                                          "check", "test_app"):
            p = self.resolve(args.get("path"))
            if not self.inside(p):
                return (f"{name} is limited to the project directory; "
                        f"{self.rel(p)} is outside it")
        return ""

    def _confine(self, p: Path) -> Optional["ToolResult"]:
        """Return an error result when `p` escapes the project (sandbox on)."""
        if self.cfg.sandbox and not self.inside(p):
            return ToolResult(
                f"ERROR: {self.rel(p)} is outside the project directory "
                f"({self.workdir}). tinycode only works inside the directory it "
                "was started in.",
                ok=False, summary="outside the project")
        return None

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
            why = danger_reason(cmd)
            if why:
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
            blocked = self._confine(p)
            if blocked:
                return "", blocked.output
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
        blocked = self._confine(p)
        if blocked:
            return blocked
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
        if offset == 1 and limit >= len(lines) and self.rel(p) not in self.problems and \
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
        full = f"{header}\n{body}"
        out = _truncate(full, self._limit())
        clipped = out != full or any(len(l) > 500 for l in chunk)
        if offset == 1 and end >= len(lines) and not clipped:
            self.seen[str(p)] = (st.st_mtime_ns, st.st_size)
        else:
            self.seen.pop(str(p), None)
        return ToolResult(out, summary=f"read {len(chunk)} lines", detail=body)

    def t_write_file(self, path: str, content: str = "", append: bool = False) -> ToolResult:
        p = self.resolve(path)
        blocked = self._confine(p)
        if blocked:
            return blocked
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
        return self._with_check(p, content, ToolResult(
            f"{verb} {self.rel(p)} (now {n} lines)",
            summary=f"{verb} · {n} lines (+{add} -{rem})", diff=diff))

    def t_edit_file(self, path: str, old_string: str = "", new_string: str = "",
                    replace_all: bool = False) -> ToolResult:
        p = self.resolve(path)
        blocked = self._confine(p)
        if blocked:
            return blocked
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
        return self._with_check(p, after, ToolResult(
            f"edited {self.rel(p)}: {n} replacement(s){note}. +{add} -{rem} lines.",
            summary=f"+{add} -{rem}{note}", diff=diff))

    # ------------------------------------------------------------- checks
    @staticmethod
    def _checkable(p: Path, text: Optional[str] = None) -> bool:
        if checks.supported(p):
            return True
        if p.suffix == "":
            if text is None:
                try:
                    with open(p, "rb") as fh:
                        text = fh.read(64).decode("utf-8", "replace")
                except OSError:
                    return False
            return text.startswith("#!") and "sh" in text.split("\n", 1)[0]
        return False

    @staticmethod
    def _keys(res: "checks.CheckResult", text: str) -> set:
        lines = text.split("\n")
        keys = set()
        for pr in res.problems:
            src = lines[pr.line - 1].strip() if 1 <= pr.line <= len(lines) else ""
            keys.add((pr.message, src))
        return keys

    def _baseline(self, p: Path) -> Optional[set]:
        """Problem keys the file already had before this turn (None = new file)."""
        key = str(p)
        if key in self._base_cache:
            return self._base_cache[key]
        orig = self.checkpoint.get(key)
        keys: Optional[set] = None
        if orig is not None:
            text = orig.decode("utf-8", "replace")
            tmpdir = tempfile.mkdtemp(prefix="tinycode-base-")
            try:
                tp = Path(tmpdir) / p.name
                tp.write_text(text, encoding="utf-8")
                keys = self._keys(checks.check_file(tp, text), text)
            except OSError:
                keys = set()
            finally:
                shutil.rmtree(tmpdir, ignore_errors=True)
        self._base_cache[key] = keys
        return keys

    def _check_new(self, p: Path, text: str) -> tuple["checks.CheckResult", int]:
        """Check a changed file, hiding problems that were already there before
        this turn (the model shouldn't be pushed to fix code it didn't break)."""
        res = checks.check_file(p, text)
        base = self._baseline(p) if res.problems else None
        hidden = 0
        if base:
            lines = text.split("\n")
            keep = []
            for pr in res.problems:
                src = lines[pr.line - 1].strip() if 1 <= pr.line <= len(lines) else ""
                if (pr.message, src) in base:
                    hidden += 1
                else:
                    keep.append(pr)
            res.problems = keep
        self._note_check(p, res)
        return res, hidden

    def _with_check(self, p: Path, text: str, result: ToolResult) -> ToolResult:
        """Run the fast syntax/error check on a file the model just changed and
        put the findings in the tool result, so it can fix them right away."""
        if not self.cfg.auto_check or not self._checkable(p, text):
            return result
        res, hidden = self._check_new(p, text)
        if res.errors or res.incomplete:
            self.seen.pop(str(p), None)      # it will need to look at the file again
        if not res.checked:
            return result
        result.output += checks.for_model(res, text, self.rel(p))
        if hidden:
            result.output += (f"\n({hidden} problem(s) that were already in this file before "
                              "your change are not shown.)")
        label = checks.summary(res)
        if label:
            result.summary = f"{result.summary} · {label}" if result.summary else label
        result.meta["check"] = res
        return result

    def _note_check(self, p: Path, res: "checks.CheckResult") -> None:
        key = self.rel(p)
        self.checks_run += 1
        if res.checked and (res.errors or res.incomplete):
            self.problems[key] = res
        else:
            self.problems.pop(key, None)

    def check_paths(self, paths: list[Path]) -> list["checks.CheckResult"]:
        out = []
        for p in paths:
            if self.cancel.is_set():
                break
            if not p.exists():
                self.problems.pop(self.rel(p), None)
                continue
            if p.is_file() and self._checkable(p):
                res = checks.check_file(p)
                self._note_check(p, res)
                out.append(res)
        return out

    def check_turn(self) -> list[tuple[Path, "checks.CheckResult", str]]:
        """Re-check every file changed this turn (baseline-filtered)."""
        out = []
        for key in list(self.checkpoint):
            if self.cancel.is_set():
                break
            p = Path(key)
            if not p.is_file():
                self.problems.pop(self.rel(p), None)
                continue
            if not self._checkable(p):
                continue
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            res, _hidden = self._check_new(p, text)
            out.append((p, res, text))
        return out

    # ------------------------------------------------------------ app check
    def app_report_result(self, reports: list["webcheck.AppReport"], final: bool = False,
                          ) -> ToolResult:
        parts = []
        for r in reports:
            parts.append(webcheck.for_model(r, self.rel(self.workdir / r.page)
                                            if not os.path.isabs(r.page) else r.page,
                                            final).strip())
        text = "\n\n".join(parts) or "no web page to test"
        bad = [r for r in reports if not r.ok and not (r.skipped and not r.errors)]
        nprob = sum(len(r.errors) + sum(1 for e in r.expects if not e.get("ok")) for r in bad)
        if bad:
            summary = f"✗ {nprob} problem(s)"
        elif any(r.ran for r in reports):
            probes = sum(len(r.probes) for r in reports)
            clicks = sum(r.clicks for r in reports)
            summary = f"✓ app works · {clicks} clicks" + (f" · {probes} checks" if probes else "")
            if any(r.expects for r in reports):
                summary += f" · {sum(len(r.expects) for r in reports)} expectations met"
        else:
            summary = reports[0].skipped if reports else "nothing to test"
        return ToolResult(_truncate(text, self._limit()), ok=not bad, summary=summary,
                          detail=text, meta={"app": reports})

    def t_test_app(self, path: str = "", steps: Any = None) -> ToolResult:
        if isinstance(steps, str):
            from .parsing import loads_lenient
            steps = loads_lenient(steps)
        if steps is not None and not isinstance(steps, list):
            steps = [steps] if isinstance(steps, dict) else None
        if path:
            p = self.resolve(path)
            blocked = self._confine(p)
            if blocked:
                return blocked
            if p.is_dir():
                p = p / "index.html"
            if not p.is_file():
                return ToolResult(f"ERROR: no such page: {self.rel(p)}", ok=False,
                                  summary="no such page")
            pages = [p] if p.suffix.lower() in (".html", ".htm") else \
                webcheck.pages_for([p], self.workdir)
        else:
            changed = [self.resolve(k) for k in self.changed]
            pages = webcheck.pages_for(changed, self.workdir) or \
                [p for p in (self.workdir / "index.html",) if p.is_file()] or \
                sorted(self.workdir.glob("*.htm*"))[:1]
        if not pages:
            return ToolResult("ERROR: no HTML page found to test. Give the path of the .html file.",
                              ok=False, summary="no page")
        reports = []
        for p in pages:
            r = webcheck.run_page(p, steps=steps or None)
            r.page = self.rel(p)
            reports.append(r)
        return self.app_report_result(reports)

    CODE_EXT = {".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java",
                ".kt", ".rb", ".php", ".c", ".cc", ".cpp", ".h", ".hpp"}

    def test_turn(self) -> Optional[tuple[str, int, str]]:
        """Run the project's test command if code changed this turn.
        Returns (command, exit_code, output_tail) or None if not applicable."""
        if not self.cfg.run_tests or self.cancel.is_set():
            return None
        if not any(Path(k).suffix.lower() in self.CODE_EXT for k in self.checkpoint):
            return None
        from .context import project_info
        _kinds, cmd = project_info(self.workdir)
        if not cmd:
            return None
        if cmd.startswith("python -m pytest") and not any(
                self.workdir.glob(pat) for pat in ("test_*.py", "*_test.py", "tests/**/*.py",
                                                    "test/**/*.py")):
            return None
        res = self.t_bash(cmd, timeout=self.cfg.test_timeout)
        code = res.meta.get("exit", 1 if not res.ok else 0)
        if "timed out" in res.summary:
            return None                     # don't block finishing on a slow suite
        tail = "\n".join((res.detail or res.output).splitlines()[-60:])
        return cmd, int(code), tail

    def app_check_turn(self) -> list["webcheck.AppReport"]:
        """Run every web page affected by this turn's changes (finish gate)."""
        if not self.cfg.app_check:
            return []
        changed = [Path(k) for k in self.checkpoint if Path(k).exists()]
        out = []
        for p in webcheck.pages_for(changed, self.workdir):
            if self.cancel.is_set():
                break
            r = webcheck.run_page(p)
            r.page = self.rel(p)
            out.append(r)
        return out

    def t_check(self, path: str = "") -> ToolResult:
        """Check one file, a directory, or (no path) every file changed this session."""
        if path:
            base = self.resolve(path)
            blocked = self._confine(base)
            if blocked:
                return blocked
            if not base.exists():
                return ToolResult(f"ERROR: no such path: {self.rel(base)}", ok=False,
                                  summary="no such path")
            if base.is_file():
                targets = [base]
            else:
                targets = [Path(r) / f for r, _d, fs in self._walk(base) for f in fs
                           if checks.supported(Path(f))][:200]
        else:
            targets = [p for p in (self.resolve(k) for k in self.changed) if p.exists()] or \
                [Path(r) / f for r, _d, fs in self._walk(self.workdir) for f in fs
                 if checks.supported(Path(f))][:200]
        results = [r for r in self.check_paths(targets) if r.checked]
        if self.cancel.is_set():
            return ToolResult("check interrupted by the user", ok=False, summary="interrupted")
        if not results:
            return ToolResult("nothing to check (no supported files found, or the checkers "
                              "for these file types are not installed)", summary="nothing to check")
        bad = [r for r in results if r.errors or r.incomplete]
        lines = []
        for r in bad:
            p = Path(r.path)
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                text = ""
            lines.append(checks.for_model(r, text, self.rel(p), final=True).strip())
        if not bad:
            return ToolResult(f"✓ {len(results)} file(s) checked, no problems found.",
                              summary=f"✓ {len(results)} file(s) clean")
        n = sum(len(r.errors) + (1 if r.incomplete else 0) for r in bad)
        text = "\n\n".join(lines)
        return ToolResult(_truncate(text, self._limit()), ok=False,
                          summary=f"✗ {n} problem(s) in {len(bad)} file(s)", detail=text)

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
        blocked = self._confine(p)
        if blocked:
            return blocked
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
        blocked = self._confine(base)
        if blocked:
            return blocked
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
        blocked = self._confine(base)
        if blocked:
            return blocked
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

    def t_bash(self, command: str, timeout: int = 0, sandbox: bool = True) -> ToolResult:
        command = str(command).strip()
        if not command:
            return ToolResult("ERROR: empty command", ok=False, summary="empty command")
        if sandbox and self.cfg.sandbox:
            why = escape_reason(command, self.workdir)
            if why:
                return ToolResult(
                    f"ERROR: refused — {why}. tinycode is confined to "
                    f"{self.workdir}. Use paths inside the project.",
                    ok=False, summary="outside the project")
        timeout = int(timeout or self.cfg.bash_timeout)
        timeout = max(1, min(timeout, 1800))
        env = dict(os.environ)
        env.update({"PAGER": "cat", "GIT_PAGER": "cat", "GIT_TERMINAL_PROMPT": "0",
                    "TERM": "dumb", "NO_COLOR": "1", "PYTHONUNBUFFERED": "1",
                    "TINYCODE": "1", "DEBIAN_FRONTEND": "noninteractive",
                    # fast successive edits can leave a same-size .pyc with the same
                    # mtime second, so Python would run stale code: never write them
                    "PYTHONDONTWRITEBYTECODE": "1"})
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
        self._base_cache = {}
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
                self.problems.pop(self.rel(p), None)
                self.seen.pop(str(p), None)
            except OSError:
                pass
        if self.cfg.auto_check:
            self.check_paths([Path(k) for k in cp if Path(k).exists()])
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
