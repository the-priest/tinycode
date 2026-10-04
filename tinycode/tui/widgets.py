"""TUI building blocks: prompt input, tool views, modals."""

from __future__ import annotations

import re

from typing import Any, Optional

from rich.console import Group
from rich.markup import escape
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text
from textual import events, on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Input, OptionList, Static, TextArea
from textual.widgets.option_list import Option

from . import theme as T
from ..tools import ToolResult


# ------------------------------------------------------------------ helpers

def render_diff(diff: str, max_lines: int = 80) -> Text:
    out = Text()
    lines = diff.splitlines()
    shown = 0
    old_no = new_no = 0
    for line in lines:
        if line.startswith(("---", "+++")):
            continue
        if shown >= max_lines:
            break
        if line.startswith("@@"):
            try:
                parts = line.split()
                old_no = int(parts[1].split(",")[0][1:])
                new_no = int(parts[2].split(",")[0][1:])
            except (IndexError, ValueError):
                pass
            if shown:
                out.append("   ⋮\n", style=T.DIM)
            continue
        if line.startswith("+"):
            out.append(f"{new_no:>5} ", style=T.MUTED)
            out.append("+ " + line[1:] + "\n", style=f"{T.GREEN} on #16261a")
            new_no += 1
        elif line.startswith("-"):
            out.append(f"{old_no:>5} ", style=T.MUTED)
            out.append("- " + line[1:] + "\n", style=f"{T.RED} on #2a1619")
            old_no += 1
        else:
            out.append(f"{new_no:>5} ", style=T.DIM)
            out.append("  " + line[1:] + "\n", style=T.TEXT_SOFT)
            old_no += 1
            new_no += 1
        shown += 1
    rest = sum(1 for l in lines if not l.startswith(("---", "+++", "@@"))) - shown
    if rest > 0:
        out.append(f"   … {rest} more diff lines\n", style=T.MUTED)
    out.rstrip()
    return out


DISPLAY_ROOT = ""   # project root, stripped from paths shown in the UI


def _rel(p: object) -> str:
    s = str(p or "")
    if DISPLAY_ROOT and s.startswith(DISPLAY_ROOT + "/"):
        return s[len(DISPLAY_ROOT) + 1:]
    if DISPLAY_ROOT and s == DISPLAY_ROOT:
        return "."
    return s


def tool_target(name: str, args: dict) -> str:
    if name in ("read_file", "write_file", "edit_file"):
        s = _rel(args.get("path", ""))
        if name == "read_file" and args.get("offset"):
            s += f":{args.get('offset')}"
        return s
    if name == "bash":
        return str(args.get("command", ""))
    if name == "glob":
        p = _rel(args.get("path"))
        return str(args.get("pattern", "")) + (f" in {p}" if p and p != "." else "")
    if name == "grep":
        s = repr(str(args.get("pattern", "")))
        if args.get("include"):
            s += f" ({args['include']})"
        if args.get("path") and _rel(args.get("path")) != ".":
            s += f" in {_rel(args['path'])}"
        return s
    if name == "list_dir":
        return _rel(args.get("path", ".") or ".") or "."
    if name == "todowrite":
        return f"{len(args.get('todos') or [])} items"
    if name == "fetch_url":
        return str(args.get("url", ""))
    return ", ".join(f"{k}={v!r}" for k, v in args.items())[:100]


def short(s: str, n: int) -> str:
    s = " ".join(s.split()) if "\n" in s else s
    return s if len(s) <= n else s[: n - 1] + "…"


# ------------------------------------------------------------- prompt input

class PromptInput(TextArea):
    """Multi-line prompt. Enter sends, Shift+Enter / Ctrl+J inserts a newline,
    Up/Down walk the history, Tab completes."""

    class Submitted(Message):
        def __init__(self, text: str) -> None:
            self.text = text
            super().__init__()

    class Navigate(Message):
        """Up/down/tab/escape when the completion menu may want them."""
        def __init__(self, key: str) -> None:
            self.key = key
            super().__init__()

    BINDINGS = [Binding("ctrl+j", "newline", "newline", show=False)]

    def __init__(self, **kw: Any) -> None:
        super().__init__(soft_wrap=True, show_line_numbers=False, tab_behavior="focus",
                         compact=True, **kw)
        self.past: list[str] = []
        self._hist_i: Optional[int] = None
        self._draft = ""
        self.menu_open = False

    def action_newline(self) -> None:
        self.insert("\n")

    async def _on_key(self, event: events.Key) -> None:
        key = event.key
        if key in ("enter",):
            event.stop()
            event.prevent_default()
            if self.menu_open:
                self.post_message(self.Navigate("enter"))
                return
            text = self.text
            if text.endswith("\\"):
                self.load_text(text[:-1] + "\n")
                self.move_cursor(self.document.end)
                return
            if text.strip():
                self.post_message(self.Submitted(text))
            return
        if key in ("shift+enter", "alt+enter", "ctrl+enter"):
            event.stop()
            event.prevent_default()
            self.insert("\n")
            return
        if key in ("tab",) and self.menu_open:
            event.stop()
            event.prevent_default()
            self.post_message(self.Navigate("tab"))
            return
        if key in ("up", "down"):
            if self.menu_open:
                event.stop()
                event.prevent_default()
                self.post_message(self.Navigate(key))
                return
            row, _ = self.cursor_location
            last = self.document.line_count - 1
            if (key == "up" and row == 0) or (key == "down" and row == last):
                if self._history_step(-1 if key == "up" else 1):
                    event.stop()
                    event.prevent_default()
                    return
        await super()._on_key(event)

    def _history_step(self, d: int) -> bool:
        if not self.past:
            return False
        if self._hist_i is None:
            if d > 0:
                return False
            self._draft = self.text
            self._hist_i = len(self.past) - 1
        else:
            self._hist_i += d
        if self._hist_i < 0:
            self._hist_i = 0
            return True
        if self._hist_i >= len(self.past):
            self._hist_i = None
            self.load_text(self._draft)
        else:
            self.load_text(self.past[self._hist_i])
        self.move_cursor(self.document.end)
        return True

    def remember(self, text: str) -> None:
        if text and (not self.past or self.past[-1] != text):
            self.past.append(text)
            self.past = self.past[-300:]
        self._hist_i = None
        self._draft = ""


# ------------------------------------------------------------- transcript

def _code(code: str, path: str, start: int = 1) -> Syntax:
    try:
        lexer = Syntax.guess_lexer(path or "x.txt", code)
    except Exception:  # noqa: BLE001
        lexer = "text"
    return Syntax(code, lexer, theme="monokai", line_numbers=True, start_line=start,
                  background_color=T.BG, word_wrap=False, indent_guides=False)


def _hunk_rows(diff: str) -> list[tuple]:
    """Unified diff -> rows of (kind, old_no, old_text, new_no, new_text)."""
    rows: list[tuple] = []
    old_no = new_no = 0
    rem: list[tuple[int, str]] = []
    add: list[tuple[int, str]] = []

    def flush() -> None:
        for i in range(max(len(rem), len(add))):
            o = rem[i] if i < len(rem) else (None, "")
            n = add[i] if i < len(add) else (None, "")
            rows.append(("chg", o[0], o[1], n[0], n[1]))
        rem.clear()
        add.clear()

    for line in diff.splitlines():
        if line.startswith(("---", "+++")):
            continue
        if line.startswith("@@"):
            flush()
            try:
                parts = line.split()
                old_no = int(parts[1].split(",")[0][1:])
                new_no = int(parts[2].split(",")[0][1:])
            except (IndexError, ValueError):
                pass
            if rows:
                rows.append(("gap", None, "", None, ""))
            continue
        if line.startswith("-"):
            rem.append((old_no, line[1:]))
            old_no += 1
        elif line.startswith("+"):
            add.append((new_no, line[1:]))
            new_no += 1
        else:
            flush()
            rows.append(("ctx", old_no, line[1:], new_no, line[1:]))
            old_no += 1
            new_no += 1
    flush()
    return rows


def render_split_diff(diff: str, max_rows: int = 60) -> Table:
    """Side-by-side before | after view."""
    tbl = Table(box=None, show_header=True, header_style=f"bold {T.MUTED}", expand=True,
                padding=(0, 1), pad_edge=False)
    tbl.add_column("", justify="right", style=T.DIM, width=5, no_wrap=True)
    tbl.add_column("before", ratio=1, no_wrap=True, overflow="ellipsis")
    tbl.add_column("", justify="right", style=T.DIM, width=5, no_wrap=True)
    tbl.add_column("after", ratio=1, no_wrap=True, overflow="ellipsis")
    rows = _hunk_rows(diff)
    for kind, on, ot, nn, nt in rows[:max_rows]:
        if kind == "gap":
            tbl.add_row("", Text("⋮", style=T.DIM), "", Text("⋮", style=T.DIM))
            continue
        if kind == "ctx":
            tbl.add_row(str(on), Text(ot, style=T.TEXT_SOFT), str(nn), Text(nt, style=T.TEXT_SOFT))
            continue
        left = Text(ot, style=f"{T.RED} on #2a1619") if on is not None else Text("")
        right = Text(nt, style=f"{T.GREEN} on #16261a") if nn is not None else Text("")
        tbl.add_row("" if on is None else str(on), left, "" if nn is None else str(nn), right)
    if len(rows) > max_rows:
        tbl.add_row("", Text(f"… {len(rows) - max_rows} more rows (ctrl+o)", style=T.MUTED), "", "")
    return tbl


def render_any_diff(diff: str, width: int, expanded: bool) -> Any:
    if width >= 90:
        return render_split_diff(diff, 400 if expanded else 40)
    return render_diff(diff, 400 if expanded else 40)


class ToolCallView(Vertical):
    """⏺ Edit(src/app.py)
         ⎿ +3 -1   then a diff, a code preview or the command output"""

    DEFAULT_CSS = f"""
    ToolCallView {{ height: auto; margin: 1 0 0 0; }}
    ToolCallView .tc-head {{ height: auto; }}
    ToolCallView .tc-sum {{ height: auto; color: {T.MUTED}; padding: 0 0 0 2; }}
    ToolCallView .tc-body {{ height: auto; max-height: 30; padding: 0 0 0 4;
                             color: {T.TEXT_SOFT}; overflow-y: auto; }}
    ToolCallView .tc-body.-expanded {{ max-height: 300; }}
    ToolCallView .tc-live {{ height: auto; max-height: 24; padding: 0 0 0 4; color: {T.MUTED}; }}
    """

    def __init__(self, name: str, args: dict, expanded: bool = False) -> None:
        super().__init__()
        self.tool = name
        self.args = args
        self.state = "running"
        self.expanded = expanded
        self.live_lines: list[str] = []
        self.result: Optional[ToolResult] = None
        self._blink_on = False
        self.head = Static(self._head_text(), classes="tc-head")
        self.summary = Static(Text("  ⎿ running…", style=T.MUTED), classes="tc-sum")
        self.live = Static("", classes="tc-live")
        self.body = Static("", classes="tc-body")
        self.body.display = False
        self.live.display = False

    def compose(self) -> ComposeResult:
        yield self.head
        yield self.summary
        yield self.live
        yield self.body

    def _head_text(self) -> Text:
        color = {"running": T.YELLOW, "ok": T.GREEN, "error": T.RED, "writing": T.BLUE,
                 "denied": T.RED, "waiting": T.ORANGE}[self.state]
        dot = "⏺" if not (self.state in ("running", "writing") and self._blink_on) else "◯"
        t = Text()
        t.append(f"{dot} ", style=color)
        t.append(T.TOOL_LABELS.get(self.tool, self.tool or "Tool"), style=f"bold {T.TEXT}")
        target = tool_target(self.tool, self.args)
        if target:
            t.append("(", style=T.MUTED)
            t.append(short(target, 90), style=T.TEXT_SOFT)
            t.append(")", style=T.MUTED)
        return t

    def blink(self) -> None:
        if self.state in ("running", "writing"):
            self._blink_on = not self._blink_on
            self.head.update(self._head_text())

    # -- live drafting (stream mode) -----------------------------------
    def draft(self, name: str, args: dict) -> None:
        """Show a tool call while the model is still writing it."""
        self.state = "writing"
        if name:
            self.tool = name
        self.args = {k: v for k, v in args.items()}
        self.head.update(self._head_text())
        code = args.get("content")
        if code is None and self.tool == "edit_file":
            code = args.get("new_string")
        if isinstance(code, str) and code:
            lines = code.split("\n")
            tail = 18
            start = max(0, len(lines) - tail)
            self.live.display = True
            self.live.update(_code("\n".join(lines[start:]), str(args.get("path", "")),
                                   start + 1))
            verb = "writing" if self.tool == "write_file" else "editing"
            self.summary.update(Text(f"  ⎿ {verb}… {len(lines)} lines", style=T.BLUE))
        else:
            self.summary.update(Text("  ⎿ writing…", style=T.BLUE))

    def set_waiting(self) -> None:
        self.state = "waiting"
        self.head.update(self._head_text())
        self.live.display = False
        self.summary.update(Text("  ⎿ waiting for your approval…", style=T.ORANGE))

    def set_running(self, args: Optional[dict] = None) -> None:
        self.state = "running"
        if args is not None:
            self.args = args
        self.head.update(self._head_text())
        self.live.display = False
        self.live_lines = []
        self.summary.update(Text("  ⎿ running…", style=T.MUTED))

    def add_live(self, text: str) -> None:
        self.state = "running"
        self.live_lines.extend(text.rstrip("\n").split("\n"))
        self.live_lines = self.live_lines[-10:]
        self.live.display = True
        self.live.update(Text("\n".join(short(l, 160) for l in self.live_lines), style=T.MUTED))

    # -- result --------------------------------------------------------
    def finish(self, result: ToolResult, seconds: float) -> None:
        self.result = result
        denied = result.summary == "denied"
        self.state = "denied" if denied else ("ok" if result.ok else "error")
        self.head.update(self._head_text())
        self.live.display = False
        color = T.MUTED if result.ok else T.RED
        summary = result.summary or ("done" if result.ok else result.output.split("\n")[0])
        s = Text("  ⎿ ", style=T.MUTED)
        s.append(short(summary, 140), style=color)
        if self._preview_lines() and not self.expanded:
            s.append("  (ctrl+o for all)", style=T.DIM)
        self.summary.update(s)
        self._render_body()

    def _preview_lines(self) -> int:
        """How many lines are hidden behind ctrl+o."""
        r = self.result
        if r is None or r.diff or self.tool == "todowrite":
            return 0
        text = r.detail or ("" if r.ok else r.output)
        n = text.count("\n") + 1 if text else 0
        return n if n > 6 else 0

    def _width(self) -> int:
        try:
            return self.size.width or self.app.size.width
        except Exception:  # noqa: BLE001
            return 80

    def _render_body(self) -> None:
        r = self.result
        if r is None:
            return
        body: Any = None
        appended = self.tool == "write_file" and r.ok and bool(self.args.get("append"))
        new_file = self.tool == "write_file" and r.ok and r.diff and \
            "@@ -0,0 " in r.diff
        if appended or new_file:
            code = str(self.args.get("content", "")).rstrip("\n")
            lines = code.split("\n")
            start = 1
            m = re.search(r"now (\d+) lines", r.output)
            if appended and m:
                start = max(1, int(m.group(1)) - len(lines) + 1)
            n = len(lines) if self.expanded else 20
            body = _code("\n".join(lines[:n]), str(self.args.get("path", "")), start)
            if len(lines) > n:
                body = Group(body, Text(f"   … {len(lines) - n} more lines (ctrl+o)",
                                        style=T.MUTED))
        elif r.diff:
            body = render_any_diff(r.diff, self._width(), self.expanded)
        elif r.detail or not r.ok:
            text = (r.detail or r.output).expandtabs(4)
            style = T.RED if not r.ok else T.MUTED
            if self.tool == "todowrite":
                body = None
            elif self.expanded:
                body = Text(text[:30000], style=style if not r.ok else T.TEXT_SOFT)
            else:
                body = Text(short_block(text, 6), style=style)
        if body is None:
            self.body.display = False
        else:
            self.body.update(body)
            self.body.display = True
        self.body.set_class(self.expanded, "-expanded")

    def set_expanded(self, on: bool) -> None:
        self.expanded = on
        self._render_body()


def short_block(text: str, n: int) -> str:
    lines = text.rstrip().split("\n")
    if len(lines) <= n:
        return "\n".join(short(l, 200) for l in lines)
    return "\n".join(short(l, 200) for l in lines[:n]) + f"\n… +{len(lines) - n} lines"


# ------------------------------------------------------------- modals

class ApprovalScreen(ModalScreen):
    """Permission prompt: yes / always / no with feedback."""

    DEFAULT_CSS = f"""
    ApprovalScreen {{ align: center bottom; background: {T.BG} 20%; }}
    #ap {{ width: 100%; max-width: 140; height: auto; max-height: 90%;
           background: {T.PANEL}; border: round {T.YELLOW}; padding: 0 2;
           margin: 0 1 1 1; }}
    #ap-title {{ color: {T.YELLOW}; text-style: bold; height: auto; margin: 1 0 0 0; }}
    #ap-warn {{ color: {T.RED}; text-style: bold; height: auto; }}
    #ap-scroll {{ height: auto; max-height: 30; margin: 1 0; background: {T.BG_ALT};
                  padding: 0 1; }}
    #ap-q {{ height: auto; color: {T.TEXT}; }}
    #ap-opts {{ height: auto; margin: 0 0 1 0; background: {T.PANEL}; border: none; }}
    #ap-opts > .option-list--option-highlighted {{ background: {T.DIM}; }}
    #ap-fb {{ display: none; margin: 0 0 1 0; }}
    """

    BINDINGS = [
        Binding("1,y", "pick(0)", show=False),
        Binding("2,a", "pick(1)", show=False),
        Binding("3,n", "pick(2)", show=False),
        Binding("escape", "deny", show=False),
    ]

    def __init__(self, name: str, args: dict, diff: str, warning: str, always_label: str):
        super().__init__()
        self.tool = name
        self.args = args
        self.diff = diff
        self.warning = warning
        self.always_label = always_label

    def compose(self) -> ComposeResult:
        label = T.TOOL_LABELS.get(self.tool, self.tool)
        with Vertical(id="ap"):
            title = {"bash": "Run this command?", "write_file": "Write this file?",
                     "edit_file": "Make this edit?"}.get(self.tool, f"Allow {label}?")
            yield Static(f"⚠  {title}", id="ap-title")
            if self.warning:
                yield Static(escape(self.warning), id="ap-warn")
            with VerticalScroll(id="ap-scroll"):
                yield Static(self._body())
            opts = [Option("1. Yes", id="yes"),
                    Option(f"2. Yes, and don't ask again {escape(self.always_label)}", id="always"),
                    Option("3. No, and tell tinycode what to do differently (esc)", id="no")]
            if self.warning:
                opts[1] = Option("2. Yes (this one is always asked)", id="yes2")
            yield OptionList(*opts, id="ap-opts")
            yield Input(placeholder="what should tinycode do instead? (enter to send, empty = just deny)",
                        id="ap-fb")

    def _body(self) -> Any:
        if self.tool == "bash":
            t = Text("$ ", style=T.GREEN)
            t.append(str(self.args.get("command", "")), style=f"bold {T.TEXT}")
            return t
        path = str(self.args.get("path", ""))
        head = Text(path + "\n", style=f"bold {T.CYAN}")
        if self.diff:
            try:
                wide = self.app.size.width >= 100
            except Exception:  # noqa: BLE001
                wide = False
            if wide:
                return Group(head, render_split_diff(self.diff, 400))
            head.append_text(render_diff(self.diff, 400))
        elif self.tool == "write_file":
            head.append(str(self.args.get("content", ""))[:6000], style=T.TEXT_SOFT)
        else:
            head.append("(no changes)", style=T.MUTED)
        return head

    def on_mount(self) -> None:
        ol = self.query_one(OptionList)
        ol.highlighted = 0
        ol.focus()

    @on(OptionList.OptionSelected, "#ap-opts")
    def _picked(self, ev: OptionList.OptionSelected) -> None:
        self._choose(ev.option.id or "no")

    def action_pick(self, idx: int) -> None:
        if self.query_one("#ap-fb", Input).display:
            return
        ids = [o.id for o in self.query_one(OptionList).options]
        if idx < len(ids):
            self._choose(ids[idx] or "no")

    def _choose(self, oid: str) -> None:
        if oid in ("yes", "yes2"):
            self.dismiss(("yes", ""))
        elif oid == "always":
            self.dismiss(("always", ""))
        else:
            fb = self.query_one("#ap-fb", Input)
            fb.display = True
            fb.focus()

    def action_deny(self) -> None:
        fb = self.query_one("#ap-fb", Input)
        self.dismiss(("no", fb.value if fb.display else ""))

    @on(Input.Submitted, "#ap-fb")
    def _fb(self, ev: Input.Submitted) -> None:
        self.dismiss(("no", ev.value))


class PickerScreen(ModalScreen):
    """Generic list picker (sessions, models)."""

    DEFAULT_CSS = f"""
    PickerScreen {{ align: center middle; background: {T.BG} 60%; }}
    #pk {{ width: 90; max-width: 95%; height: auto; max-height: 80%;
           background: {T.PANEL}; border: round {T.BLUE}; padding: 1 2; }}
    #pk-title {{ color: {T.BLUE}; text-style: bold; margin: 0 0 1 0; }}
    #pk-list {{ height: auto; max-height: 30; background: {T.PANEL}; border: none; }}
    #pk-hint {{ color: {T.MUTED}; margin: 1 0 0 0; }}
    """
    BINDINGS = [Binding("escape", "close", show=False)]

    def __init__(self, title: str, items: list[tuple[str, str]]):
        super().__init__()
        self.title_text = title
        self.items = items

    def compose(self) -> ComposeResult:
        with Vertical(id="pk"):
            yield Static(self.title_text, id="pk-title")
            yield OptionList(*[Option(label, id=key) for key, label in self.items], id="pk-list")
            yield Static("↑↓ choose · enter select · esc cancel", id="pk-hint")

    def on_mount(self) -> None:
        self.query_one(OptionList).focus()

    @on(OptionList.OptionSelected)
    def _sel(self, ev: OptionList.OptionSelected) -> None:
        self.dismiss(ev.option.id)

    def action_close(self) -> None:
        self.dismiss(None)

