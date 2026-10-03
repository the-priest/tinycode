"""TUI building blocks: prompt input, tool views, modals."""

from __future__ import annotations

from typing import Any, Optional

from rich.markup import escape
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


def tool_target(name: str, args: dict) -> str:
    if name in ("read_file", "write_file", "edit_file"):
        s = str(args.get("path", ""))
        if name == "read_file" and args.get("offset"):
            s += f":{args.get('offset')}"
        return s
    if name == "bash":
        return str(args.get("command", ""))
    if name == "glob":
        p = args.get("path")
        return str(args.get("pattern", "")) + (f" in {p}" if p and p != "." else "")
    if name == "grep":
        s = repr(str(args.get("pattern", "")))
        if args.get("include"):
            s += f" ({args['include']})"
        if args.get("path") and args.get("path") != ".":
            s += f" in {args['path']}"
        return s
    if name == "list_dir":
        return str(args.get("path", ".") or ".")
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

class ToolCallView(Vertical):
    """⏺ Edit(src/app.py)
         ⎿ +3 -1  (diff / output below)"""

    DEFAULT_CSS = f"""
    ToolCallView {{ height: auto; margin: 1 0 0 0; }}
    ToolCallView .tc-head {{ height: auto; }}
    ToolCallView .tc-sum {{ height: auto; color: {T.MUTED}; padding: 0 0 0 2; }}
    ToolCallView .tc-body {{ height: auto; max-height: 24; padding: 0 0 0 4;
                             color: {T.TEXT_SOFT}; overflow-y: auto; }}
    ToolCallView .tc-body.-expanded {{ max-height: 200; }}
    ToolCallView .tc-live {{ height: auto; max-height: 8; padding: 0 0 0 4; color: {T.MUTED}; }}
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
        color = {"running": T.YELLOW, "ok": T.GREEN, "error": T.RED,
                 "denied": T.RED, "waiting": T.ORANGE}[self.state]
        dot = "⏺" if not (self.state == "running" and self._blink_on) else "◯"
        t = Text()
        t.append(f"{dot} ", style=color)
        t.append(T.TOOL_LABELS.get(self.tool, self.tool), style=f"bold {T.TEXT}")
        target = tool_target(self.tool, self.args)
        if target:
            t.append("(", style=T.MUTED)
            t.append(short(target, 90), style=T.TEXT_SOFT)
            t.append(")", style=T.MUTED)
        return t

    def blink(self) -> None:
        if self.state == "running":
            self._blink_on = not self._blink_on
            self.head.update(self._head_text())

    def set_waiting(self) -> None:
        self.state = "waiting"
        self.head.update(self._head_text())
        self.summary.update(Text("  ⎿ waiting for your approval…", style=T.ORANGE))

    def add_live(self, text: str) -> None:
        self.state = "running"
        self.live_lines.extend(text.rstrip("\n").split("\n"))
        self.live_lines = self.live_lines[-6:]
        self.live.display = True
        self.live.update(Text("\n".join(short(l, 160) for l in self.live_lines), style=T.MUTED))

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
        if seconds >= 1.0 and self.tool == "bash":
            pass  # bash summary already carries its timing
        has_body = bool(result.diff or (result.detail and self.tool not in ("todowrite",)))
        if not result.ok and not result.diff:
            has_body = True
        if has_body and not result.diff:
            s.append("  (ctrl+o to expand)" if not self.expanded else "", style=T.DIM)
        self.summary.update(s)
        self._render_body()

    def _render_body(self) -> None:
        r = self.result
        if r is None:
            return
        if r.diff:
            self.body.update(render_diff(r.diff, 200 if self.expanded else 40))
            self.body.display = True
        elif not r.ok and r.output:
            self.body.update(Text(r.output[:3000] if self.expanded else short_block(r.output, 8),
                                  style=T.RED))
            self.body.display = True
        elif r.detail and self.expanded and self.tool != "todowrite":
            self.body.update(Text(r.detail[:20000], style=T.TEXT_SOFT))
            self.body.display = True
        elif r.detail and self.tool == "bash":
            self.body.update(Text(short_block(r.detail, 5), style=T.MUTED))
            self.body.display = True
        else:
            self.body.display = False
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
    """Claude-Code style permission prompt."""

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

    def _body(self) -> Text:
        if self.tool == "bash":
            t = Text("$ ", style=T.GREEN)
            t.append(str(self.args.get("command", "")), style=f"bold {T.TEXT}")
            return t
        path = str(self.args.get("path", ""))
        head = Text(path + "\n", style=f"bold {T.CYAN}")
        if self.diff:
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

