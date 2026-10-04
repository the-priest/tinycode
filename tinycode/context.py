"""System prompt, project memory and context-window management."""

from __future__ import annotations

import json
import os
import platform
import subprocess
import time
from pathlib import Path
from typing import Optional

from .parsing import is_tool_result
from .tools import IGNORED_DIRS, TOOL_SCHEMAS

MEMORY_FILES = ("TINYCODE.md", "AGENTS.md", "CLAUDE.md", ".tinycode/instructions.md")

SYSTEM_PROMPT = """You are tinycode, an expert software engineer working as a coding agent in the user's terminal. You complete tasks by calling tools, then reply briefly.

# Environment
- Project root: {cwd}
- OS: {os} · Date: {date} · Git: {git}
- Top level: {listing}{project}

# How to work
0. Plan: for any task that creates or changes files, your FIRST call is todowrite with the steps, e.g. [{{"content": "Write the HTML and CSS", "status": "in_progress"}}, {{"content": "Add the game logic", "status": "pending"}}]. Update the statuses with todowrite as you finish each step.
1. Understand: locate code with glob / grep / list_dir and read it with read_file. Never guess what a file contains.
2. Change: edit_file for targeted edits, write_file for new files or full rewrites.
3. Verify: every file you write or edit is checked automatically (syntax errors, undefined names, unclosed tags/braces); the result is at the end of the tool output. If it reports a problem, fix it right away with edit_file before doing anything else. Run tests or the program with bash when it makes sense.
4. Finish: as soon as the work is complete, reply with a short summary and NO tool call. That ends your turn. Don't re-read files you just wrote — you already know their content. Static files (HTML/CSS) need no verification run.

# Rules
- Before each tool call, say in one short sentence what you are about to do.
- Write files in small chunks of about 80 lines: write_file with the first chunk, then write_file with append=true for each next chunk, until the file is complete.
- Write complete, working code. No placeholders such as "// TODO", "..." or "rest of the code here".
- Act with tools instead of describing what you would do. Don't ask for permission; the user approves risky actions themselves.
- edit_file: copy old_string exactly from the file WITHOUT the line-number prefix, with 2-3 lines of context so it is unique.
- Paths are relative to the project root.
- If a tool returns ERROR, read the message and correct your next call. Don't repeat a failing call unchanged.
- Never invent file contents, command output or results.
- Simple questions that need no files: answer directly, no tools.
- Keep replies short: a few lines of markdown. Match the existing code style.{memory}"""


def _git_info(cwd: Path) -> str:
    try:
        out = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=cwd,
                             capture_output=True, text=True, timeout=3)
        if out.returncode == 0:
            return f"yes (branch {out.stdout.strip()})"
    except (OSError, subprocess.SubprocessError):
        pass
    return "no"


def git_branch(cwd: Path) -> str:
    try:
        out = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=cwd,
                             capture_output=True, text=True, timeout=3)
        return out.stdout.strip() if out.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def _listing(cwd: Path, limit: int = 40) -> str:
    try:
        entries = sorted(cwd.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower()))
    except OSError:
        return "(unreadable)"
    names = [e.name + ("/" if e.is_dir() else "") for e in entries
             if e.name not in IGNORED_DIRS and not (e.name.startswith(".") and e.is_dir())]
    if not names:
        return "(empty directory)"
    more = f" … +{len(names) - limit} more" if len(names) > limit else ""
    return ", ".join(names[:limit]) + more


def detect_project(cwd: Path) -> str:
    """One line describing the project type and how to test it."""
    def has(*names: str) -> bool:
        return any((cwd / n).exists() for n in names)
    kinds, test = [], ""
    if has("package.json"):
        kinds.append("Node.js")
        try:
            pkg = json.loads((cwd / "package.json").read_text(encoding="utf-8"))
            scripts = pkg.get("scripts") or {}
            if "test" in scripts and "no test specified" not in str(scripts["test"]):
                runner = "pnpm" if has("pnpm-lock.yaml") else "yarn" if has("yarn.lock") \
                    else "bun" if has("bun.lockb", "bun.lock") else "npm"
                test = f"{runner} test"
            deps = {**(pkg.get("dependencies") or {}), **(pkg.get("devDependencies") or {})}
            for fw in ("react", "next", "vue", "svelte", "express", "vite"):
                if fw in deps:
                    kinds.append(fw)
            if "typescript" in deps or has("tsconfig.json"):
                kinds.append("TypeScript")
        except (OSError, ValueError, AttributeError):
            pass
    if has("pyproject.toml", "setup.py", "requirements.txt", "setup.cfg"):
        kinds.append("Python")
        if has("pytest.ini", "tests", "test", "conftest.py") or \
                "pytest" in _read(cwd / "pyproject.toml"):
            test = test or "python -m pytest -q"
    if has("Cargo.toml"):
        kinds.append("Rust")
        test = test or "cargo test"
    if has("go.mod"):
        kinds.append("Go")
        test = test or "go test ./..."
    if has("pom.xml"):
        kinds.append("Java (Maven)")
        test = test or "mvn -q test"
    if has("build.gradle", "build.gradle.kts"):
        kinds.append("Java/Kotlin (Gradle)")
        test = test or "./gradlew test"
    if has("Gemfile"):
        kinds.append("Ruby")
    if has("composer.json"):
        kinds.append("PHP")
    if has("CMakeLists.txt"):
        kinds.append("C/C++ (CMake)")
    if has("Makefile") and "test:" in _read(cwd / "Makefile"):
        test = test or "make test"
    if not kinds:
        return ""
    line = "\n- Project: " + ", ".join(dict.fromkeys(kinds))
    if test:
        line += f" · run tests with: {test}"
    return line


def _read(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8", errors="replace")[:20000]
    except OSError:
        return ""


def load_memory(cwd: Path, max_chars: int = 6000) -> tuple[str, list[str]]:
    """Instructions from the user's global ~/.config/tinycode/TINYCODE.md plus
    the project's TINYCODE.md (or AGENTS.md / CLAUDE.md if there is none)."""
    from .config import CONFIG_DIR
    candidates = [CONFIG_DIR / "TINYCODE.md"]
    project = next((cwd / n for n in MEMORY_FILES[:3] if (cwd / n).is_file()), None)
    if project:
        candidates.append(project)
    candidates.append(cwd / MEMORY_FILES[3])
    parts, used = [], []
    for p in candidates:
        try:
            text = p.read_text(encoding="utf-8", errors="replace").strip() if p.is_file() else ""
        except OSError:
            text = ""
        if text:
            parts.append(f"## From {p.name}\n{text}")
            used.append(str(p))
    text = "\n\n".join(parts)
    if len(text) > max_chars:
        text = text[:max_chars] + "\n… (truncated)"
    return text, used


def tools_prompt() -> str:
    """Tool instructions for "stream" tool mode, where calls are plain text."""
    lines = []
    for t in TOOL_SCHEMAS:
        f = t["function"]
        props = f["parameters"]["properties"]
        req = set(f["parameters"]["required"])
        sig = ", ".join(k + ("" if k in req else "?") for k in props)
        lines.append(f"- {f['name']}({sig}): {f['description']}")
    return TOOLS_SECTION.format(tools="\n".join(lines))


TOOLS_SECTION = """

# Tools
To use a tool, first write ONE short sentence saying what you are doing, then the call in exactly this format:
<tool_call>
{{"name": "TOOL_NAME", "arguments": {{"arg": "value"}}}}
</tool_call>
Then STOP. The result comes back to you inside <tool_response>…</tool_response>. Never write a <tool_response> yourself.
- One tool call per reply. The arguments must be valid JSON: escape double quotes as \\" and newlines as \\n inside strings.
- When the task is finished, reply normally without any tool call.

Available tools (? = optional):
{tools}

Example:
Let me look at the main module first.
<tool_call>
{{"name": "read_file", "arguments": {{"path": "src/main.py"}}}}
</tool_call>"""


def build_system_prompt(cwd: Path, tool_mode: str = "stream") -> tuple[str, list[str]]:
    memory, used = load_memory(cwd)
    mem = f"\n\n# Project instructions (follow these)\n{memory}" if memory else ""
    osname = f"{platform.system()} {platform.release()}".strip()
    prompt = SYSTEM_PROMPT.format(
        cwd=str(cwd), os=osname, date=time.strftime("%Y-%m-%d"),
        git=_git_info(cwd), listing=_listing(cwd), project=detect_project(cwd),
        memory="")
    if tool_mode == "stream":
        prompt += tools_prompt()
    return prompt + mem, used


# ------------------------------------------------------------ token budget

CHARS_PER_TOKEN = 3.5
_SCHEMA_CHARS = len(json.dumps(TOOL_SCHEMAS))


def estimate_tokens(messages: list[dict], with_tools: bool = True) -> int:
    chars = sum(len(m.get("content") or "") + len(json.dumps(m.get("tool_calls") or ""))
                + 12 for m in messages)
    if with_tools:
        chars += _SCHEMA_CHARS
    return int(chars / CHARS_PER_TOKEN)


PRUNED = "[older tool output removed to save context — call the tool again if needed]"


def _pruned_like(m: dict) -> str:
    if m.get("role") == "user":
        return f"<tool_response>\n{PRUNED}\n</tool_response>"
    return PRUNED


def prune(messages: list[dict], budget: int, keep_recent_tools: int = 4) -> tuple[list[dict], int]:
    """Shrink history to fit `budget` tokens. Cheapest first:
    1. blank out old tool results (keeping the most recent few),
    2. drop the oldest whole turns.
    Returns (messages, n_changes)."""
    msgs = [dict(m) for m in messages]
    changes = 0
    if estimate_tokens(msgs) <= budget:
        return msgs, 0

    tool_idx = [i for i, m in enumerate(msgs) if is_tool_result(m)]
    for i in tool_idx[:-keep_recent_tools] if keep_recent_tools else tool_idx:
        if PRUNED not in (msgs[i].get("content") or "") and len(msgs[i].get("content") or "") > 200:
            msgs[i]["content"] = _pruned_like(msgs[i])
            changes += 1
            if estimate_tokens(msgs) <= budget:
                return msgs, changes

    # also shrink big assistant tool-call arguments (file contents written earlier)
    for m in msgs[1:-6]:
        for tc in m.get("tool_calls") or []:
            fn = tc.get("function") or {}
            args = fn.get("arguments")
            if isinstance(args, dict):
                for k in ("content", "old_string", "new_string"):
                    if isinstance(args.get(k), str) and len(args[k]) > 400:
                        args = dict(args)
                        args[k] = args[k][:200] + " …[trimmed]"
                        changes += 1
                fn = dict(fn, arguments=args)
                tc["function"] = fn
    if estimate_tokens(msgs) <= budget:
        return msgs, changes

    # drop oldest turns (a turn starts at a user message), keep the latest one
    system = msgs[0] if msgs and msgs[0].get("role") == "system" else None
    body = msgs[1:] if system else msgs
    user_starts = [i for i, m in enumerate(body)
                   if m.get("role") == "user" and not is_tool_result(m)]
    dropped_users: list[str] = []
    while len(user_starts) > 1 and estimate_tokens(([system] if system else []) + body) > budget:
        cut = user_starts[1]
        dropped_users += [str(m.get("content", ""))[:160] for m in body[:cut]
                          if m.get("role") == "user" and not is_tool_result(m)
                          and not str(m.get("content", "")).startswith("[")]
        body = body[cut:]
        user_starts = [i for i, m in enumerate(body)
                       if m.get("role") == "user" and not is_tool_result(m)]
        changes += 1
    if dropped_users:
        note = {"role": "user", "content":
                "[Context note: earlier messages were removed to fit the context window. "
                "Earlier requests were: " + " | ".join(dropped_users[-6:]) + "]"}
        assistant_ack = {"role": "assistant", "content": "Understood."}
        body = [note, assistant_ack] + body
    # last resort: blank all tool outputs except the very last one
    if estimate_tokens(([system] if system else []) + body) > budget:
        tools = [i for i, m in enumerate(body) if is_tool_result(m)]
        for i in tools[:-1]:
            body[i] = dict(body[i], content=_pruned_like(body[i]))
            changes += 1
    return ([system] if system else []) + body, changes


COMPACT_PROMPT = """Summarize the conversation so far so the work can continue in a fresh context.
Write concise markdown with these sections:
## Goal
## Done so far (files changed, commands run, results)
## Current state / open problems
## Next steps
Include exact file paths, function names and error messages that matter. No preamble."""


def transcript_for_summary(messages: list[dict], max_chars: int = 30000) -> str:
    out = []
    for m in messages:
        role = m.get("role")
        if role == "system":
            continue
        c = str(m.get("content") or "")
        if is_tool_result(m):
            c = c[:800]
            out.append(f"[tool result]\n{c}")
        elif role == "assistant":
            calls = m.get("tool_calls") or []
            for tc in calls:
                fn = tc.get("function") or {}
                out.append(f"[assistant called {fn.get('name')}({json.dumps(fn.get('arguments'))[:300]})]")
            if c:
                out.append(f"[assistant]\n{c[:2000]}")
        else:
            out.append(f"[user]\n{c[:2000]}")
    text = "\n\n".join(out)
    return text[-max_chars:]


def expand_mentions(text: str, cwd: Path, max_chars: int = 20000) -> tuple[str, list[str]]:
    """Inline @file references: '@src/app.py' attaches that file's contents."""
    import re
    attached: list[str] = []
    blocks: list[str] = []
    total = 0
    for m in re.finditer(r"(?<![\w/])@([\w./\-~]+[\w/])", text):
        raw = m.group(1)
        p = Path(os.path.expanduser(raw))
        p = p if p.is_absolute() else cwd / p
        if p.is_file():
            try:
                content = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if total + len(content) > max_chars:
                content = content[: max(0, max_chars - total)] + "\n… (truncated)"
            total += len(content)
            numbered = "\n".join(f"{i:>6}\t{l}" for i, l in
                                 enumerate(content.splitlines(), 1))
            blocks.append(f'<file path="{raw}">\n{numbered}\n</file>')
            attached.append(raw)
        elif p.is_dir():
            try:
                names = sorted(x.name + ("/" if x.is_dir() else "") for x in p.iterdir())[:100]
            except OSError:
                continue
            blocks.append(f'<directory path="{raw}">\n' + "\n".join(names) + "\n</directory>")
            attached.append(raw + "/")
        if total >= max_chars:
            break
    if blocks:
        text = text + "\n\n" + "\n\n".join(blocks)
    return text, attached


def first_line(s: Optional[str], n: int = 80) -> str:
    s = (s or "").strip().splitlines()[0] if (s or "").strip() else ""
    return s if len(s) <= n else s[: n - 1] + "…"
