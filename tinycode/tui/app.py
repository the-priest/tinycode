"""The full-screen terminal UI."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widget import Widget
from textual.widgets import Collapsible, Markdown, OptionList, ProgressBar, Static
from textual.widgets.option_list import Option

from .. import APP_NAME, __version__, models
from ..agent import Agent, AgentUI, Decision
from ..config import Config, log, log_path, save_setting, write_default_config
from ..context import expand_mentions, git_branch
from ..ollama import Ollama, OllamaError
from ..parsing import extract_text_tool_calls, is_tool_result, is_user_turn
from ..session import Session, ago, list_sessions
from ..tools import IGNORED_DIRS, ToolResult
from . import theme as T
from .widgets import (ApprovalScreen, Entry, Palette, PickerScreen, PromptInput, TextPrompt,
                      ToolCallView, short, tool_target)

LOGO = ["▀█▀ █ █▄ █ █▄█ █▀▀ █▀█ █▀▄ █▀▀",
        " █  █ █ ▀█  █  █▄▄ █▄█ █▄▀ ██▄"]
GRADIENT = ["#7aa2f7", "#7aa2f7", "#7dcfff", "#7dcfff", "#73daca", "#9ece6a", "#9ece6a",
            "#bb9af7"]
SPINNER = "·✢✳✶✻✽✻✶✳✢"

INIT_PROMPT = """Analyze this codebase and create a TINYCODE.md file in the project root that will guide future coding sessions here. Look at the README, the build/config files (package.json, pyproject.toml, Makefile, Cargo.toml, go.mod, …) and the main source folders first. Include:
1. A one-paragraph overview of what the project is.
2. The exact commands to install, build, run, lint and test (including how to run a single test).
3. The high-level architecture: key directories and files and what they do.
4. Code-style conventions you observe (formatting, naming, imports, error handling).
Keep it under 60 lines. If TINYCODE.md already exists, improve it instead."""

COMMANDS: list[tuple[str, str]] = [
    ("/help", "show commands and keys"),
    ("/settings", "settings, models and commands (ctrl+p)"),
    ("/new", "start a fresh conversation (alias /clear)"),
    ("/resume", "resume a previous conversation"),
    ("/undo", "revert the file changes of the last turn"),
    ("/compact", "summarize the conversation to free context"),
    ("/mode", "ask · auto-edit · yolo  (shift+tab cycles)"),
    ("/think", "toggle model reasoning on/off (ctrl+t)"),
    ("/init", "create a TINYCODE.md guide for this project"),
    ("/diff", "show files changed this session (git diff)"),
    ("/model", "switch model (presets, unrestricted builds, any Ollama tag)"),
    ("/cost", "token usage for this session"),
    ("/export", "save the conversation as markdown"),
    ("/config", "open the path of the config file"),
    ("/sidebar", "toggle the sidebar (ctrl+b)"),
    ("/quit", "exit (unloads the model)"),
]


def todos_present(agent: Any) -> bool:
    return bool(agent.tools.todos)


def fmt_k(n: int) -> str:
    if n >= 1024 and n % 1024 == 0:
        return f"{n // 1024}k"
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


class Bridge(AgentUI):
    """Renders agent events into the transcript."""

    def __init__(self, app: "TinyCodeApp"):
        self.app = app
        self.think_box: Optional[Collapsible] = None
        self.think_body: Optional[Static] = None
        self.think_buf: list[str] = []
        self.think_paint = 0.0
        self.md: Optional[Markdown] = None
        self.md_row: Optional[Widget] = None
        self.md_stream: Any = None
        self.md_text: list[str] = []
        self.tool_views: dict[str, ToolCallView] = {}
        self.live_tokens = 0
        self.draft_view: Optional[ToolCallView] = None

    def _reset_step(self) -> None:
        self.draft_view = None
        self.think_box = self.think_body = None
        self.think_buf = []
        self.md = self.md_row = None
        self.md_stream = None
        self.md_text = []

    async def step_start(self, step: int) -> None:
        self.live_tokens += self.app.gen_estimate
        self.app.gen_estimate = 0
        self._reset_step()
        self.app.step = step
        self.app.set_activity("Thinking" if self.app.agent.think else "Working")

    async def thinking_delta(self, text: str) -> None:
        self.think_buf.append(text)
        self.live_tokens += max(1, len(text) // 4)
        if self.think_box is None:
            self.think_body = Static("", classes="think-body")
            self.think_box = Collapsible(self.think_body, title="∴ Thinking…",
                                         collapsed=False, classes="think")
            await self.app.emit(self.think_box)
        now = time.monotonic()
        if now - self.think_paint > 0.1 and self.think_body is not None:
            self.think_paint = now
            full = "".join(self.think_buf).strip()
            if not self.app.show_thinking:
                full = "\n".join(full.split("\n")[-6:])   # live tail
            self.think_body.update(Text(full, style=f"italic {T.MUTED}"))
            last = "".join(self.think_buf).strip().split("\n")[-1]
            self.app.set_activity("Thinking", short(last, 70))

    async def thinking_end(self, seconds: float) -> None:
        if self.think_box is not None and self.think_body is not None:
            self.think_body.update(Text("".join(self.think_buf).strip(), style=f"italic {T.MUTED}"))
            self.think_box.title = f"∴ Thought for {seconds:.1f}s"
            self.think_box.collapsed = not self.app.show_thinking
        self.app.set_activity("Writing")

    async def content_delta(self, text: str) -> None:
        self.live_tokens += max(1, len(text) // 4)
        if self.md is None:
            self.md = Markdown("", classes="answer")
            self.md_row = Horizontal(Static("⏺", classes="answer-dot"), self.md,
                                     classes="answer-row")
            await self.app.emit(self.md_row)
            self.md_stream = Markdown.get_stream(self.md)
        self.md_text.append(text)
        await self.md_stream.write(text)

    async def content_end(self, final_text: str) -> None:
        if self.md_stream is not None:
            await self.md_stream.stop()
            self.md_stream = None
        streamed = "".join(self.md_text).strip()
        if self.md is None and final_text:
            self.md = Markdown(final_text, classes="answer")
            self.md_row = Horizontal(Static("⏺", classes="answer-dot"), self.md,
                                     classes="answer-row")
            await self.app.emit(self.md_row)
        elif self.md is not None:
            if not final_text and self.md_row is not None:
                await self.md_row.remove()
                self.md = self.md_row = None
            elif final_text != streamed:
                await self.md.update(final_text)
        self.md_text = []

    async def tool_draft(self, name: str, args: dict) -> None:
        if self.draft_view is None:
            self.draft_view = ToolCallView(name or "", args, expanded=self.app.expanded)
            await self.app.emit(self.draft_view)
            self.app.current_tool = self.draft_view
        self.draft_view.draft(name, args)
        self.app.gen_estimate = int(sum(len(str(v)) for v in args.values()) / 3.5)
        label = T.TOOL_LABELS.get(name, name or "tool call")
        path = args.get("path") or args.get("command") or ""
        self.app.set_activity(f"Writing {label}", short(str(path), 50))

    async def tool_start(self, call_id: str, name: str, args: dict) -> None:
        self.live_tokens += self.app.gen_estimate
        self.app.gen_estimate = 0
        view = self.draft_view
        self.draft_view = None
        if view is not None and view.tool in (name, ""):
            view.tool = name
            view.set_running(args)
        else:
            if view is not None:
                await view.remove()
            view = ToolCallView(name, args, expanded=self.app.expanded)
            await self.app.emit(view)
        self.tool_views[call_id] = view
        self.app.current_tool = view
        self.app.set_activity(f"Running {T.TOOL_LABELS.get(name, name)}",
                              short(tool_target(name, args), 60))

    def tool_output(self, call_id: str, text: str) -> None:
        view = self.tool_views.get(call_id)
        if view is not None:
            self.app.call_threadsafe(view.add_live, text)

    async def tool_end(self, call_id: str, name: str, args: dict,
                       result: ToolResult, seconds: float) -> None:
        view = self.tool_views.pop(call_id, None)
        if view is not None:
            view.finish(result, seconds)
        self.app.current_tool = None
        self.app.refresh_side()
        self.app.set_activity("Thinking" if self.app.agent.think else "Working")

    async def approve(self, name: str, args: dict, diff: str, warning: str) -> Decision:
        view = self.app.current_tool
        if view is not None:
            view.set_waiting()
        key = Agent._always_key(name, args)
        label = ("for file edits this session" if key == "edit"
                 else f"for `{key.split(':', 1)[1]}` commands this session"
                 if key.startswith("bash:") else f"for {name} this session")
        self.app.set_activity("Waiting for you")
        fut: asyncio.Future = asyncio.get_running_loop().create_future()

        def done(res: Any) -> None:
            if not fut.done():
                fut.set_result(res)
        self.app.push_screen(ApprovalScreen(name, args, diff, warning, label), callback=done)
        self.app.pending_approval = fut
        try:
            choice, feedback = await fut
        finally:
            self.app.pending_approval = None
        if view is not None:
            view.set_running()
        if choice == "always":
            self.app.notify(f"won't ask again {label}", timeout=3)
            return Decision(True, always=True)
        return Decision(choice == "yes", feedback=feedback or "")

    async def generating(self, seconds: float, est_tokens: int, started: bool,
                         phase: str = "writing", eta: float = 0.0) -> None:
        if phase == "reading":
            left = f" · ~{eta:.0f}s left" if eta >= 1 else ""
            self.app.set_activity("Reading", f"the model is processing the conversation{left}")
            return
        if self.think_box is not None and self.think_box.title.startswith("∴ Thinking"):
            self.think_box.title = "∴ Thought"
            self.think_box.collapsed = not self.app.show_thinking
        self.app.gen_estimate = est_tokens
        self.app.set_activity(
            "Writing", f"~{fmt_k(est_tokens)} tokens so far — the model shows a tool call "
                       "once it's complete")

    async def notice(self, text: str, level: str = "info") -> None:
        color = {"info": T.TEXT_SOFT, "warn": T.YELLOW, "error": T.RED,
                 "dim": T.MUTED, "ok": T.GREEN}.get(level, T.TEXT_SOFT)
        await self.app.emit(Static(Text(f"  {text}", style=color), classes="notice"))

    async def stats(self, info: dict) -> None:
        self.app.last_stats = info
        self.app.refresh_side()

    async def todos(self, todos: list[dict]) -> None:
        self.app.refresh_side()

    async def status(self, text: str) -> None:
        pass


class TinyCodeApp(App):
    TITLE = APP_NAME
    ENABLE_COMMAND_PALETTE = False

    CSS = f"""
    Screen {{ background: {T.BG}; color: {T.TEXT}; layers: base overlay; }}
    #topbar {{ height: 1; background: {T.PANEL}; color: {T.TEXT_SOFT}; padding: 0 1; }}
    #body {{ height: 1fr; }}
    #main {{ width: 1fr; height: 1fr; }}
    #log {{ height: 1fr; padding: 0 2 1 2; background: {T.BG};
            scrollbar-size-vertical: 1; scrollbar-background: {T.BG};
            scrollbar-color: {T.DIM}; scrollbar-color-hover: {T.BLUE}; }}

    .banner {{ height: auto; margin: 1 0 0 0; }}
    .welcome {{ height: auto; color: {T.MUTED}; margin: 1 0 0 0; padding: 0 1;
                border: round {T.BORDER}; }}
    .user {{ height: auto; margin: 1 0 0 0; padding: 0 1; background: {T.BG_ALT};
             border-left: thick {T.BLUE}; color: {T.TEXT}; }}
    .notice {{ height: auto; margin: 1 0 0 0; }}
    .answer-row {{ height: auto; margin: 1 0 0 0; }}
    .answer-dot {{ width: 2; color: {T.TEXT}; }}
    .answer {{ height: auto; margin: 0; padding: 0; background: {T.BG}; }}
    Markdown {{ background: {T.BG}; }}
    MarkdownFence {{ max-height: 40; }}
    .think {{ height: auto; margin: 1 0 0 0; padding: 0; border: none;
              background: {T.BG}; }}
    .think CollapsibleTitle {{ color: {T.MUTED}; padding: 0; }}
    .think CollapsibleTitle:hover {{ color: {T.TEXT_SOFT}; background: {T.BG}; }}
    .think CollapsibleTitle:focus {{ background: {T.BG}; color: {T.TEXT_SOFT}; }}
    .think Contents {{ padding: 0 0 0 2; height: auto; }}
    .think-body {{ height: auto; max-height: 30; overflow-y: auto; }}
    .pull {{ height: auto; margin: 1 0 0 0; }}
    .pull ProgressBar {{ margin: 0 0 0 2; }}

    #menu {{ height: auto; max-height: 10; margin: 0 2; background: {T.PANEL};
             border: round {T.BORDER}; display: none; }}
    #menu > .option-list--option-highlighted {{ background: {T.DIM}; }}
    #status {{ height: 1; padding: 0 3; color: {T.MUTED}; }}
    #prompt-box {{ height: auto; margin: 0 1; border: round {T.BORDER};
                   background: {T.BG}; padding: 0 1; }}
    #prompt-box:focus-within {{ border: round {T.BLUE}; }}
    #prompt-box.-busy {{ border: round {T.DIM}; }}
    #caret {{ width: 2; color: {T.BLUE}; }}
    #prompt {{ height: auto; max-height: 12; min-height: 1; background: {T.BG};
               border: none; padding: 0; }}
    #prompt .text-area--cursor-line {{ background: {T.BG}; }}
    #bottombar {{ height: 1; padding: 0 2; color: {T.MUTED}; }}

    #side {{ width: 38; height: 1fr; background: {T.BG_ALT};
             border-left: solid {T.BORDER}; padding: 1 1; }}
    #side.-hidden {{ display: none; }}
    .side-panel {{ height: auto; margin: 0 0 1 0; }}
    """

    BINDINGS = [
        Binding("ctrl+q", "quit", "quit", priority=True),
        Binding("ctrl+d", "quit", show=False, priority=True),
        Binding("ctrl+c", "ctrl_c", show=False, priority=True),
        Binding("escape", "escape", show=False),
        Binding("shift+tab", "cycle_mode", show=False, priority=True),
        Binding("ctrl+t", "toggle_think", show=False, priority=True),
        Binding("ctrl+o", "toggle_expand", show=False, priority=True),
        Binding("ctrl+b", "toggle_sidebar", show=False, priority=True),
        Binding("ctrl+l", "clear_screen", show=False, priority=True),
        Binding("ctrl+n", "new_session", show=False, priority=True),
        Binding("ctrl+p", "palette", show=False, priority=True),
    ]

    def __init__(self, cfg: Config, workdir: Path, no_boot: bool = False,
                 resume: Optional[Session] = None, initial_prompt: Optional[str] = None):
        super().__init__()
        self.cfg = cfg
        self.workdir = workdir
        self.no_boot = no_boot
        self.client = Ollama(cfg)
        self.bridge = Bridge(self)
        self.agent = Agent(cfg, self.client, workdir, self.bridge)
        self.session = resume or Session(cwd=str(workdir))
        self.session.cwd = str(workdir)
        if resume:
            self.agent.load_messages(resume.messages)
            self.agent.tools.todos = list(resume.todos or [])
            self.agent.tools.todos_auto = bool(getattr(resume, "todos_auto", False))
            self.agent.tokens_in = resume.tokens_in
            self.agent.tokens_out = resume.tokens_out
        self.initial_prompt = initial_prompt
        self.ready = False
        self.step = 0
        self.show_thinking = cfg.show_thinking
        self.expanded = False
        self.activity = "Booting"
        self.activity_detail = ""
        self.boot_status = "starting…"
        self.last_stats: dict = {}
        self.gen_estimate = 0
        self.queue: list[str] = []
        self.current_tool: Optional[ToolCallView] = None
        self.pending_approval: Optional[asyncio.Future] = None
        self.branch = git_branch(workdir)
        from . import widgets as _w
        _w.DISPLAY_ROOT = str(workdir)
        self.model_info: dict = {}
        self._spin = 0
        self._turn_start = 0.0
        self._files_cache: Optional[list[str]] = None
        self._menu_items: list[tuple[str, str]] = []
        self._menu_kind = ""
        self._shutdown_lock = threading.Lock()
        self._shutdown_done = False
        self._last_ctrl_c = 0.0
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    # ------------------------------------------------------------ layout
    def compose(self) -> ComposeResult:
        yield Static(id="topbar")
        with Horizontal(id="body"):
            with Vertical(id="main"):
                yield VerticalScroll(id="log")
                yield OptionList(id="menu")
                yield Static(id="status")
                with Horizontal(id="prompt-box"):
                    yield Static("❯", id="caret")
                    yield PromptInput(id="prompt")
                yield Static(id="bottombar")
            with VerticalScroll(id="side"):
                yield Static(id="side-session", classes="side-panel")
                yield Static(id="side-context", classes="side-panel")
                yield Static(id="side-plan", classes="side-panel")
                yield Static(id="side-files", classes="side-panel")
                yield Static(id="side-problems", classes="side-panel")

    async def on_mount(self) -> None:
        self._loop = asyncio.get_running_loop()
        self.log_view = self.query_one("#log", VerticalScroll)
        self.log_view.anchor()
        self.prompt = self.query_one("#prompt", PromptInput)
        self.menu = self.query_one("#menu", OptionList)
        if not self.cfg.sidebar or self.size.width < 100:
            self.query_one("#side").add_class("-hidden")
        self.prompt.focus()
        self.set_interval(0.1, self._tick)
        self.set_interval(0.5, self._blink)
        self.refresh_chrome()
        self.refresh_side()
        await self._welcome()
        if self.session.messages:
            await self.replay(self.session.messages)
        if self.no_boot:
            self._set_ready()
            await self.bridge.notice("--no-boot: assuming Ollama and the model are ready", "dim")
        else:
            self._begin_boot()

    def call_threadsafe(self, fn: Callable, *args: Any) -> None:
        if self._loop is None:
            return
        try:
            self._loop.call_soon_threadsafe(lambda: fn(*args))
        except RuntimeError:
            pass

    async def _welcome(self) -> None:
        banner = Text()
        for row in LOGO:
            for i, ch in enumerate(row):
                banner.append(ch, style=f"bold {GRADIENT[min(len(GRADIENT) - 1, i * len(GRADIENT) // len(row))]}")
            banner.append("\n")
        banner.append(f"v{__version__} · a fast local coding agent", style=T.MUTED)
        await self.emit(Static(banner, classes="banner"))
        tips = Text()
        tips.append("cwd    ", style=T.MUTED)
        tips.append(self._dir_display() + (f"  ⎇ {self.branch}" if self.branch else ""),
                    style=T.TEXT)
        tips.append("\nmodel  ", style=T.MUTED)
        tips.append(f"{self.cfg.model_label}", style=T.TEXT)
        tips.append(f"  ctx {fmt_k(self.cfg.num_ctx)} · reasoning {'on' if self.agent.think else 'off'}",
                    style=T.MUTED)
        if self.agent.memory_files:
            tips.append("\nmemory ", style=T.MUTED)
            tips.append(", ".join(Path(p).name for p in self.agent.memory_files), style=T.TEXT)
        tips.append("\n\n", style=T.MUTED)
        for k, v in (("ctrl+p", "settings & models"), ("/", "commands"), ("@", "attach a file"),
                     ("!", "run a shell command"), ("shift+tab", "permission mode"),
                     ("esc", "interrupt")):
            tips.append(f" {k} ", style=f"bold {T.BLUE}")
            tips.append(f"{v}  ", style=T.MUTED)
        if not self.agent.memory_files:
            tips.append("\n\ntip: run /init to create a TINYCODE.md guide for this project",
                        style=T.MUTED)
        await self.emit(Static(tips, classes="welcome"))

    # ----------------------------------------------------------- chrome
    def _dir_display(self) -> str:
        p, home = str(self.workdir), str(Path.home())
        if p == home:
            return "~"
        return "~" + p[len(home):] if p.startswith(home + os.sep) else p

    def refresh_chrome(self) -> None:
        top = Text()
        top.append(" ◆ ", style=f"bold {T.BLUE}")
        top.append("tinycode", style=f"bold {T.TEXT}")
        top.append("  ·  ", style=T.DIM)
        top.append(self.cfg.model_label, style=T.TEXT_SOFT)
        top.append("  ·  ", style=T.DIM)
        top.append(short(self._dir_display(), 50), style=T.TEXT_SOFT)
        if self.branch:
            top.append(f"  ⎇ {self.branch}", style=T.PURPLE)
        try:
            self.query_one("#topbar", Static).update(top)
        except Exception:  # noqa: BLE001
            pass
        self._refresh_bottom()

    def _refresh_bottom(self) -> None:
        color, label = T.MODE_STYLE[self.agent.mode]
        b = Text()
        if self.agent.mode == "ask":
            b.append("? ", style=T.MUTED)
            b.append("ask before changes", style=T.MUTED)
        else:
            b.append("⏵⏵ ", style=color)
            b.append(label, style=color)
        b.append(" (shift+tab)", style=T.DIM)
        b.append("   reasoning ", style=T.DIM)
        b.append("on" if self.agent.think else "off",
                 style=T.TEAL if self.agent.think else T.MUTED)
        b.append(" (ctrl+t)", style=T.DIM)
        used = self.agent.context_used()
        pct = int(100 * used / max(1, self.cfg.num_ctx))
        pc = T.GREEN if pct < 60 else (T.YELLOW if pct < 85 else T.RED)
        b.append("   ctx ", style=T.DIM)
        b.append(f"{pct}%", style=pc)
        if self.queue:
            b.append(f"   {len(self.queue)} queued", style=T.YELLOW)
        b.append("   ctrl+p", style=T.BLUE)
        b.append(" settings", style=T.DIM)
        try:
            self.query_one("#bottombar", Static).update(b)
        except Exception:  # noqa: BLE001
            pass

    def set_activity(self, verb: str, detail: str = "") -> None:
        self.activity = verb
        self.activity_detail = detail

    def _tick(self) -> None:
        self._spin += 1
        try:
            st = self.query_one("#status", Static)
        except Exception:  # noqa: BLE001
            return
        if self.agent.busy:
            frame = SPINNER[self._spin % len(SPINNER)]
            el = time.monotonic() - self._turn_start
            t = Text()
            t.append(f"{frame} ", style=T.ORANGE)
            t.append(f"{self.activity}…", style=f"bold {T.ORANGE}")
            if self.activity_detail:
                t.append(f" {self.activity_detail}", style=T.MUTED)
            t.append(f"  ({el:.0f}s · ↓ {fmt_k(self.bridge.live_tokens + self.gen_estimate)} tokens"
                     " · esc to interrupt)",
                     style=T.DIM)
            st.update(t)
        elif not self.ready:
            frame = SPINNER[self._spin % len(SPINNER)]
            st.update(Text(f"{frame} {self.boot_status}", style=T.YELLOW))
        else:
            s = self.last_stats
            t = Text()
            if s.get("tok_s"):
                t.append(f"{s['tok_s']:.0f} tok/s", style=T.DIM)
                t.append(" · ", style=T.DIM)
            t.append("ready", style=T.MUTED)
            st.update(t)

    def _blink(self) -> None:
        if self.current_tool is not None:
            self.current_tool.blink()
        if self.agent.busy:
            self._refresh_bottom()

    def refresh_side(self) -> None:
        self._refresh_bottom()
        try:
            side = self.query_one("#side")
        except Exception:  # noqa: BLE001
            return
        if side.has_class("-hidden"):
            return
        a = self.agent
        # session
        t = Text()
        t.append("SESSION\n", style=f"bold {T.BLUE}")
        d = self._dir_display()
        rows = [("dir", short(d, 24) if len(d) <= 24 else "…" + d[-23:]),
                ("branch", self.branch or "—"),
                ("model", self.cfg.model_label),
                ("ollama", self._ollama_state()),
                ("mode", a.mode),
                ("steps", str(self.step))]
        for k, v in rows:
            t.append(f"{k:<8}", style=T.MUTED)
            t.append(f"{v}\n", style=T.TEXT_SOFT)
        self.query_one("#side-session", Static).update(t)
        # context gauge
        used = a.context_used()
        pct = min(1.0, used / max(1, self.cfg.num_ctx))
        width = 22
        filled = int(pct * width)
        col = T.GREEN if pct < 0.6 else (T.YELLOW if pct < 0.85 else T.RED)
        c = Text()
        c.append("CONTEXT\n", style=f"bold {T.BLUE}")
        c.append("█" * filled, style=col)
        c.append("░" * (width - filled), style=T.DIM)
        c.append(f" {int(pct * 100)}%\n", style=col)
        c.append(f"{fmt_k(used)} / {fmt_k(self.cfg.num_ctx)} tokens\n", style=T.MUTED)
        c.append(f"in {fmt_k(a.tokens_in)} · out {fmt_k(a.tokens_out)}", style=T.MUTED)
        if self.last_stats.get("tok_s"):
            c.append(f" · {self.last_stats['tok_s']:.0f} tok/s", style=T.MUTED)
        self.query_one("#side-context", Static).update(c)
        # plan
        p = Text()
        p.append("STEPS\n" if a.tools.todos_auto and todos_present(a) else "PLAN\n",
                 style=f"bold {T.BLUE}")
        todos = a.tools.todos
        if not todos:
            p.append("no plan yet", style=T.DIM)
        icons = {"completed": ("✔", T.GREEN), "in_progress": ("▶", T.YELLOW),
                 "pending": ("○", T.MUTED)}
        for td in todos[:14]:
            ic, colr = icons[td["status"]]
            p.append(f"{ic} ", style=colr)
            done_style = T.MUTED if a.tools.todos_auto else f"strike {T.MUTED}"
            style = done_style if td["status"] == "completed" else (
                f"bold {T.TEXT}" if td["status"] == "in_progress" else T.TEXT_SOFT)
            p.append(short(td["content"], 31) + "\n", style=style)
        self.query_one("#side-plan", Static).update(p)
        # files
        f = Text()
        f.append("CHANGED FILES\n", style=f"bold {T.BLUE}")
        if not a.tools.changed:
            f.append("none yet", style=T.DIM)
        for path, (add, rem) in list(a.tools.changed.items())[-12:]:
            f.append(short(path, 22).ljust(23), style=T.TEXT_SOFT)
            f.append(f"+{add}", style=T.GREEN)
            f.append(f" -{rem}\n", style=T.RED)
        self.query_one("#side-files", Static).update(f)
        # problems found by the automatic checks
        pr = Text()
        probs = a.tools.problems
        pr.append("PROBLEMS\n", style=f"bold {T.BLUE}")
        if not probs:
            ran = a.tools.checks_run > 0
            pr.append("✓ none" if ran else "nothing checked yet",
                      style=T.GREEN if ran else T.DIM)
        if len(probs) > 8:
            pr.append(f"… {len(probs) - 8} more files\n", style=T.MUTED)
        for path, res in list(probs.items())[-8:]:
            n = len(res.errors)
            pr.append(short(path, 22).ljust(23), style=T.TEXT_SOFT)
            if n:
                pr.append(f"✗ {n}\n", style=T.RED)
                for p in res.errors[:2]:
                    pr.append(f"  {p.line}: {short(p.message, 30)}\n", style=T.MUTED)
            else:
                pr.append("… unfinished\n", style=T.YELLOW)
        pr.rstrip()
        self.query_one("#side-problems", Static).update(pr)

    def _ollama_state(self) -> str:
        if self.no_boot:
            return "external"
        if not self.ready:
            return "starting"
        if self.client.spawned:
            return "ours · stops on exit"
        return "adopted · stops on exit" if self.client.owned else "external · kept"

    # ------------------------------------------------------- transcript
    async def emit(self, widget: Widget) -> None:
        await self.log_view.mount(widget)

    async def emit_user(self, text: str) -> None:
        t = Text(text, style=T.TEXT)
        await self.emit(Static(t, classes="user"))

    async def replay(self, messages: list[dict]) -> None:
        """Re-render a resumed conversation."""
        await self.bridge.notice(f"resumed session · {sum(1 for m in messages if m.get('role') == 'user')} turns", "dim")
        pending: list[ToolCallView] = []
        for m in messages:
            role = m.get("role")
            content = str(m.get("content", ""))
            if role == "user" and content.startswith("<tool_response"):
                if pending:
                    v = pending.pop(0)
                    body = content.split("\n", 1)[-1].rsplit("</tool_response>", 1)[0].strip()
                    v.finish(ToolResult(body, ok=not body.startswith("ERROR"),
                                        summary=short(body.split("\n")[0], 100)), 0)
            elif role == "user" and not is_user_turn(m):
                await self.bridge.notice(short(content.split("\n")[0], 140), "dim")
            elif role == "user":
                await self.emit_user(content[:2000])
            elif role == "assistant" and "<tool_call>" in content:
                calls, text = extract_text_tool_calls(content)
                if text:
                    await self.emit(Horizontal(Static("⏺", classes="answer-dot"),
                                               Markdown(text, classes="answer"),
                                               classes="answer-row"))
                for c in calls:
                    fn = c["function"]
                    v = ToolCallView(fn["name"], fn["arguments"])
                    await self.emit(v)
                    pending.append(v)
            elif role == "assistant":
                for tc in m.get("tool_calls") or []:
                    fn = tc.get("function") or {}
                    v = ToolCallView(fn.get("name", "?"), fn.get("arguments") or {})
                    await self.emit(v)
                    pending.append(v)
                if m.get("content"):
                    await self.emit(Horizontal(Static("⏺", classes="answer-dot"),
                                               Markdown(str(m["content"]), classes="answer"),
                                               classes="answer-row"))
            elif role == "tool" and pending:
                v = pending.pop(0)
                out = str(m.get("content", ""))
                v.finish(ToolResult(out, ok=not out.startswith("ERROR"),
                                    summary=short(out.split("\n")[0], 100)), 0)

    # ------------------------------------------------------------- boot
    def _begin_boot(self) -> None:
        try:
            self.client.begin_server()   # main thread: PDEATHSIG binds to it
        except OllamaError as exc:
            self.run_worker(self._boot_failed(str(exc)))
            return
        self.run_worker(self._boot(), group="boot", exclusive=True)

    async def _boot_failed(self, msg: str) -> None:
        self.boot_status = "boot failed"
        log(f"boot failed: {msg}")
        await self.bridge.notice(f"✗ {msg}", "error")
        await self.bridge.notice(f"log: {log_path()}  ·  run `tinycode doctor` to diagnose", "dim")

    async def _boot(self) -> None:
        try:
            self.boot_status = "starting ollama…"
            await asyncio.to_thread(self.client.wait_up)
            self.boot_status = "checking model…"
            if not await asyncio.to_thread(self.client.has_model):
                await self._pull()
            self.boot_status = f"loading {self.cfg.model_label} into memory…"
            info = await asyncio.to_thread(self.client.load)
            self.model_info = info or {}
            size = self.model_info.get("size")
            where = ""
            if size:
                vram = self.model_info.get("size_vram") or 0
                where = " on GPU" if vram and vram >= size * 0.9 else (
                    " CPU+GPU" if vram else " on CPU")
            self._set_ready()
            await self.bridge.notice(
                f"✓ {self.cfg.model_label} ready"
                + (f" ({size / 1e9:.1f} GB{where})" if size else ""), "ok")
            self.refresh_side()
            if self.initial_prompt:
                self.queue.insert(0, self.initial_prompt)
                self.initial_prompt = None
            if self.queue and not self.agent.busy:
                self.run_worker(self._turn(self.queue.pop(0)), group="agent")
        except OllamaError as exc:
            await self._boot_failed(str(exc))
        except Exception as exc:  # noqa: BLE001
            await self._boot_failed(f"unexpected boot error: {exc!r}")

    async def _pull(self) -> None:
        await self.bridge.notice(f"downloading {self.cfg.model_label} ({self.cfg.model}), "
                                 "one time only…", "warn")
        bar = ProgressBar(total=100, show_eta=True)
        label = Static(Text("  starting download…", style=T.MUTED))
        box = Vertical(label, bar, classes="pull")
        await self.emit(box)

        def progress(status: str, frac: float) -> None:
            def ui() -> None:
                label.update(Text(f"  {status}", style=T.MUTED))
                if frac >= 0:
                    bar.update(progress=frac * 100)
            self.call_threadsafe(ui)
            self.boot_status = f"downloading model… {frac * 100:.0f}%" if frac >= 0 else status
        await asyncio.to_thread(self.client.pull, progress)
        bar.update(progress=100)
        label.update(Text("  ✓ download complete", style=T.GREEN))

    def _set_ready(self) -> None:
        self.ready = True
        self.refresh_side()

    # --------------------------------------------------------- shutdown
    def shutdown_blocking(self) -> None:
        with self._shutdown_lock:
            if self._shutdown_done:
                return
            self._shutdown_done = True
        try:
            self.agent.cancel()
        except Exception:  # noqa: BLE001
            pass
        try:
            self.session.messages = self.agent.messages
            self.session.save()
        except Exception:  # noqa: BLE001
            pass
        if self.no_boot:
            return
        log("shutdown: unloading model / stopping managed ollama")
        try:
            if self.cfg.unload_on_exit and self.client.alive(timeout=1.5):
                self.client.unload(timeout=8)
        except Exception as exc:  # noqa: BLE001
            log(f"unload failed: {exc!r}")
        try:
            self.client.stop_server()
        except Exception as exc:  # noqa: BLE001
            log(f"stop_server failed: {exc!r}")

    async def action_quit(self) -> None:
        self.boot_status = "shutting down…"
        self.ready = False
        try:
            self.query_one("#status", Static).update(
                Text("⏻ unloading model and stopping ollama…", style=T.YELLOW))
        except Exception:  # noqa: BLE001
            pass
        await asyncio.to_thread(self.shutdown_blocking)
        self.exit()

    def on_unmount(self) -> None:
        self.shutdown_blocking()

    # ------------------------------------------------------------ input
    @on(PromptInput.Submitted)
    async def _on_submit(self, ev: PromptInput.Submitted) -> None:
        text = ev.text.strip()
        self.prompt.load_text("")
        self._hide_menu()
        if not text:
            return
        self.prompt.remember(text)
        await self._submit(text)

    async def _submit(self, text: str) -> None:
        if text.startswith("/"):
            await self._command(text)
            return
        if text.startswith("!"):
            if self.agent.busy:
                self.notify("busy — wait for the current turn", severity="warning")
                return
            await self._shell(text[1:].strip())
            return
        if not self.ready:
            if self.boot_status == "boot failed":
                self.notify("tinycode could not start the model — see the error above "
                            "and run `tinycode doctor`", severity="error")
                return
            self.notify("still starting up — your message will be sent when ready")
            self.queue.append(text)
            self._refresh_bottom()
            return
        if self.agent.busy:
            self.queue.append(text)
            self.notify("queued — will send when the current turn finishes", timeout=2)
            self._refresh_bottom()
            return
        self.agent.busy = True   # claim the agent now so a fast second message queues
        self.run_worker(self._turn(text), group="agent")

    async def _turn(self, text: str) -> None:
        self.agent.busy = True
        self.bridge.live_tokens = 0
        self.gen_estimate = 0
        self._turn_start = time.monotonic()
        self.query_one("#prompt-box").add_class("-busy")
        await self.emit_user(text)
        expanded, attached = expand_mentions(text, self.workdir, sandbox=self.cfg.sandbox)
        if attached:
            await self.bridge.notice("attached " + ", ".join(attached), "dim")
        self.log_view.anchor()
        try:
            await self.agent.run(expanded)
        except Exception as exc:  # noqa: BLE001
            log(f"turn crashed: {exc!r}")
            await self.bridge.notice(f"internal error: {exc!r}", "error")
        finally:
            try:
                self.query_one("#prompt-box").remove_class("-busy")
            except Exception:  # noqa: BLE001 - app may be shutting down
                pass
            self.agent.busy = False
            el = time.monotonic() - self._turn_start
            if el > 3 and not self.agent._cancel:
                await self.bridge.notice(f"✻ done in {el:.0f}s", "dim")
            self.session.messages = self.agent.messages
            self.session.todos = self.agent.tools.todos
            self.session.todos_auto = self.agent.tools.todos_auto
            self.session.tokens_in = self.agent.tokens_in
            self.session.tokens_out = self.agent.tokens_out
            await asyncio.to_thread(self.session.save)
            self.refresh_side()
            if self.queue and self.ready:
                nxt = self.queue.pop(0)
                self.agent.busy = True
                self.run_worker(self._turn(nxt), group="agent")
            self._refresh_bottom()

    async def _shell(self, cmd: str) -> None:
        if not cmd:
            return
        view = ToolCallView("bash", {"command": cmd}, expanded=True)
        await self.emit(view)
        tools = self.agent.tools
        tools.on_output = lambda s: self.call_threadsafe(view.add_live, s)
        tools.cancel.clear()
        try:
            res = await asyncio.to_thread(tools.t_bash, cmd, 0, False)
        finally:
            tools.on_output = None
        res.detail = res.detail or res.output
        view.finish(res, 0)
        self.agent.messages.append({"role": "user", "content":
                                    f"[I ran a shell command myself]\n{res.output}"})

    # -------------------------------------------------------- completion
    @on(PromptInput.Changed)
    def _on_change(self, ev: PromptInput.Changed) -> None:
        text = self.prompt.text
        row, col = self.prompt.cursor_location
        line = self.prompt.document.get_line(row)[:col] if text else ""
        if text.startswith("/") and "\n" not in text and " " not in text:
            items = [(c, f"{c:<10} {d}") for c, d in COMMANDS if c.startswith(text)]
            self._show_menu(items, "cmd")
            return
        word = line.split(" ")[-1] if line else ""
        if word.startswith("@") and len(word) >= 1:
            q = word[1:].lower()
            files = self._project_files()
            if q:
                scored = []
                for f in files:
                    fl = f.lower()
                    base = fl.rsplit("/", 1)[-1]
                    if base.startswith(q):
                        scored.append((0, len(f), f))
                    elif q in base:
                        scored.append((1, len(f), f))
                    elif q in fl:
                        scored.append((2, len(f), f))
                scored.sort()
                cand = [s[2] for s in scored[:8]]
            else:
                cand = files[:8]
            if q and q in (f.lower() for f in cand) and not text.endswith("/"):
                self._hide_menu()   # already a complete path
                return
            self._show_menu([(f"@{c}", f"@{c}") for c in cand], "file")
            return
        self._hide_menu()

    def _project_files(self) -> list[str]:
        if self._files_cache is not None:
            return self._files_cache
        files: list[str] = []
        try:
            out = subprocess.run(["git", "ls-files", "-co", "--exclude-standard"],
                                 cwd=self.workdir, capture_output=True, text=True, timeout=5)
            if out.returncode == 0:
                files = [l for l in out.stdout.splitlines() if l][:20000]
        except (OSError, subprocess.SubprocessError):
            pass
        if not files:
            for root, dirs, fs in os.walk(self.workdir):
                dirs[:] = [d for d in dirs if d not in IGNORED_DIRS and not d.startswith(".")]
                for f in fs:
                    files.append(os.path.relpath(os.path.join(root, f), self.workdir))
                if len(files) > 20000:
                    break
        self._files_cache = sorted(files, key=lambda f: (f.count("/"), f))
        return self._files_cache

    def _show_menu(self, items: list[tuple[str, str]], kind: str) -> None:
        if not items:
            self._hide_menu()
            return
        self._menu_items = items
        self._menu_kind = kind
        self.menu.clear_options()
        self.menu.add_options([Option(label, id=str(i)) for i, (_, label) in enumerate(items)])
        self.menu.highlighted = 0
        self.menu.display = True
        self.prompt.menu_open = True

    def _hide_menu(self) -> None:
        if self.menu.display:
            self.menu.display = False
        self.prompt.menu_open = False

    @on(PromptInput.Navigate)
    async def _on_nav(self, ev: PromptInput.Navigate) -> None:
        if not self.menu.display:
            return
        n = len(self._menu_items)
        hi = self.menu.highlighted or 0
        if ev.key == "up":
            self.menu.highlighted = (hi - 1) % n
        elif ev.key == "down":
            self.menu.highlighted = (hi + 1) % n
        elif ev.key in ("tab", "enter"):
            value = self._menu_items[hi][0]
            if self._menu_kind == "cmd":
                self._hide_menu()
                if ev.key == "enter":
                    self.prompt.load_text("")
                    self.prompt.remember(value)
                    await self._submit(value)
                else:
                    self.prompt.load_text(value + " ")
                    self.prompt.move_cursor(self.prompt.document.end)
            else:
                row, col = self.prompt.cursor_location
                line = self.prompt.document.get_line(row)
                start = line[:col].rfind("@")
                self.prompt.replace(value + " ", (row, start), (row, col))
                self._hide_menu()

    # ---------------------------------------------------------- actions
    async def action_ctrl_c(self) -> None:
        if isinstance(self.screen, ModalScreen):
            self.screen.dismiss(("no", "") if isinstance(self.screen, ApprovalScreen) else None)
            return
        if self.agent.busy:
            self.agent.cancel()
            return
        if self.prompt.text:
            self.prompt.load_text("")
            return
        now = time.monotonic()
        if now - self._last_ctrl_c < 1.5:
            await self.action_quit()
            return
        self._last_ctrl_c = now
        self.notify("press ctrl+c again to quit", timeout=1.5)

    def action_escape(self) -> None:
        if self.menu.display:
            self._hide_menu()
            return
        if self.agent.busy:
            self.agent.cancel()
            self.queue.clear()
            return
        if self.prompt.text:
            self.prompt.load_text("")

    def action_cycle_mode(self) -> None:
        if isinstance(self.screen, ModalScreen):
            return
        order = ["ask", "auto-edit", "yolo"]
        self.agent.mode = order[(order.index(self.agent.mode) + 1) % len(order)]
        self.refresh_side()

    def action_toggle_think(self) -> None:
        self.agent.think = not self.agent.think
        self.notify(f"reasoning {'on — slower, more careful' if self.agent.think else 'off — faster'}",
                    timeout=2)
        self.refresh_side()

    def action_toggle_expand(self) -> None:
        self.expanded = not self.expanded
        self.show_thinking = self.expanded or self.cfg.show_thinking
        for v in self.query(ToolCallView):
            v.set_expanded(self.expanded)
        for box in self.query(Collapsible):
            if box.has_class("think"):
                box.collapsed = not self.show_thinking

    def action_toggle_sidebar(self) -> None:
        side = self.query_one("#side")
        side.toggle_class("-hidden")
        self.refresh_side()

    async def action_clear_screen(self) -> None:
        await self.log_view.remove_children()

    async def action_new_session(self) -> None:
        await self._command("/new")

    # --------------------------------------------------------- settings
    def action_palette(self, query: str = "") -> None:
        if isinstance(self.screen, Palette):
            self.screen.dismiss(None)
            return
        if isinstance(self.screen, ModalScreen):
            return
        self.push_screen(
            Palette("Settings", self._settings_entries, self._settings_act,
                    foot="↑↓ move · enter or click to change · type to search · saved automatically",
                    query=query),
            callback=self._palette_done)

    @staticmethod
    def _flag(v: bool) -> tuple[str, str]:
        return ("on", T.GREEN) if v else ("off", T.MUTED)

    def _settings_entries(self) -> list[Entry]:
        c, a = self.cfg, self.agent
        side_on = not self.query_one("#side").has_class("-hidden")

        def flag(id_: str, label: str, on: bool, group: str, detail: str = "",
                 key: str = "") -> Entry:
            v, col = self._flag(on)
            return Entry(id_, label, v, group, key, detail, col)

        twin = models.counterpart(c.model)
        unc = models.is_uncensored(c.model)
        mcol = T.MODE_STYLE[a.mode][0]
        e = [
            Entry("model", "Switch model", c.model_label, "Model", "",
                  "Pick a recommended model, an unrestricted build, or any Ollama tag. "
                  "Downloads it if needed.", T.CYAN),
            flag("uncensored", "Unrestricted", unc, "Model",
                 f"switch to {twin.label}" if twin else "pick an abliterated model (no refusals)"),
            flag("think", "Reasoning", a.think, "Model", "think before acting: slower, more careful",
                 "ctrl+t"),
            Entry("think_budget", "Reasoning budget",
                  f"{c.think_budget:,} tokens" if c.think_budget else "unlimited", "Model",
                  detail="cut off a step that thinks longer than this"),
            Entry("num_ctx", "Context window", f"{fmt_k(c.num_ctx)} tokens", "Model",
                  detail="more remembers more, but needs more RAM"),
            Entry("mode", "Permission mode", a.mode, "Agent", "shift+tab",
                  T.MODE_STYLE[a.mode][1], mcol if a.mode != "ask" else T.TEXT_SOFT),
            flag("sandbox", "Sandbox to project", c.sandbox, "Agent",
                 "tools can't touch anything outside this folder"),
            flag("auto_check", "Check code after every edit", c.auto_check, "Agent",
                 "syntax and bug checks fed back to the model"),
            flag("app_check", "Run web apps before finishing", c.app_check, "Agent",
                 "click through pages in a simulated browser"),
            flag("run_tests", "Run tests before finishing", c.run_tests, "Agent",
                 "pytest, npm test, cargo test… when code changed"),
            flag("sidebar", "Sidebar", side_on, "Interface", key="ctrl+b"),
            flag("show_thinking", "Show reasoning expanded", c.show_thinking, "Interface"),
        ]
        cmds = [("new", "New conversation", "ctrl+n"), ("resume", "Resume a conversation", ""),
                ("undo", "Undo last turn's file changes", ""),
                ("compact", "Compact conversation", ""), ("diff", "Files changed", ""),
                ("init", "Create TINYCODE.md for this project", ""),
                ("export", "Export conversation as markdown", ""), ("cost", "Token usage", ""),
                ("config", "Config file location", ""), ("help", "Help", ""),
                ("quit", "Quit", "ctrl+q")]
        e += [Entry(f"cmd:/{k}", label, group="Commands", key=key) for k, label, key in cmds]
        return e

    def _save(self, key: str, value: Any) -> None:
        try:
            save_setting(key, value)
        except OSError as exc:
            self.notify(f"couldn't save {key}: {exc}", severity="warning")

    def _settings_act(self, oid: str) -> bool:
        """Toggles change in place (True keeps the palette open); everything
        else closes it and runs in _palette_run."""
        c, a = self.cfg, self.agent
        if oid == "think":
            a.think = c.think = not a.think
            self._save("think", a.think)
        elif oid == "mode":
            order = ["ask", "auto-edit", "yolo"]
            a.mode = order[(order.index(a.mode) + 1) % len(order)]
        elif oid == "sandbox":
            c.sandbox = not c.sandbox
            self._save("sandbox", c.sandbox)
            a.refresh_system_prompt()
        elif oid in ("auto_check", "app_check", "run_tests"):
            setattr(c, oid, not getattr(c, oid))
            self._save(oid, getattr(c, oid))
        elif oid == "sidebar":
            self.action_toggle_sidebar()
            c.sidebar = not self.query_one("#side").has_class("-hidden")
            self._save("sidebar", c.sidebar)
        elif oid == "show_thinking":
            c.show_thinking = not c.show_thinking
            self.show_thinking = self.expanded or c.show_thinking
            for box in self.query(Collapsible):
                if box.has_class("think"):
                    box.collapsed = not self.show_thinking
            self._save("show_thinking", c.show_thinking)
        else:
            return False
        self.refresh_side()
        return True

    def _palette_done(self, choice: Optional[str]) -> None:
        if choice:
            self.run_worker(self._palette_run(choice), group="palette")

    async def _palette_run(self, oid: str) -> None:
        c = self.cfg
        if oid.startswith("cmd:"):
            await self._command(oid[4:])
        elif oid == "model":
            await self._model_picker()
        elif oid == "uncensored":
            twin = models.counterpart(c.model)
            if twin:
                await self._switch_model(twin.key)
            else:
                await self._model_picker(
                    query="recommended" if models.is_uncensored(c.model) else "unrestricted")
        elif oid == "think_budget":
            pick = await self._choose("Reasoning budget", [
                (1000, "fastest, for simple edits"), (2000, ""),
                (3000, "default"), (6000, "hard problems"),
                (0, "never cut the model off")],
                c.think_budget, lambda v: f"{v:,} tokens" if v else "unlimited")
            if pick is not None:
                c.think_budget = pick
                self._save("think_budget", pick)
                self.notify(f"reasoning budget: {pick:,} tokens" if pick else
                            "reasoning budget: unlimited", timeout=2)
        elif oid == "num_ctx":
            pick = await self._choose("Context window", [
                (8192, "least RAM, short tasks"), (16384, "low RAM"), (32768, "default"),
                (65536, "big projects, needs more RAM"), (131072, "huge, needs lots of RAM")],
                c.num_ctx, lambda v: f"{fmt_k(v)} tokens")
            if pick is not None and pick != c.num_ctx:
                c.num_ctx = pick
                self._save("num_ctx", pick)
                self.refresh_side()
                await self._reload_model(f"context window: {fmt_k(pick)} tokens")

    async def _choose(self, title: str, options: list[tuple[int, str]], current: int,
                      fmt: Callable[[int], str]) -> Optional[int]:
        entries = [Entry(str(v), fmt(v), mark="●" if v == current else "", sub=note)
                   for v, note in options]
        choice = await self._pick_palette(title, entries, select=str(current),
                                          foot="● current · enter select · esc close")
        return int(choice) if choice is not None else None

    async def _pick_palette(self, title: str, entries: list[Entry], query: str = "",
                            foot: str = "", select: str = "") -> Optional[str]:
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self.push_screen(Palette(title, lambda: entries, lambda _id: False, foot=foot,
                                 query=query, select=select),
                         callback=lambda r: fut.done() or fut.set_result(r))
        return await fut

    @staticmethod
    def _installed(tag: str, names: set[str]) -> bool:
        return tag in names or (":" not in tag and f"{tag}:latest" in names)

    async def _model_picker(self, query: str = "") -> None:
        found = [] if self.no_boot else await asyncio.to_thread(self.client.list_models)
        names = {m.get("name", "") for m in found} | {m.get("model", "") for m in found}
        cur = self.cfg.model

        def entry(p: models.Preset, group: str) -> Entry:
            have = self._installed(p.tag, names)
            return Entry(f"m:{p.key}", p.label, "installed" if have else f"↓ {p.size}", group,
                         detail=f"{p.notes}\n{p.tag}",
                         color=T.TEAL if have else T.MUTED,
                         mark="●" if p.tag == cur else "",
                         sub=f"{p.params.split(' (')[0]} · {p.speed}")

        e = [entry(p, "Recommended") for p in models.PRESETS if not p.uncensored]
        e += [entry(p, "Unrestricted — refusals removed") for p in models.PRESETS if p.uncensored]
        known = {p.tag for p in models.PRESETS}
        for m in sorted(found, key=lambda m: m.get("name", "")):
            name = m.get("name", "")
            if name and name not in known and not any(self._installed(t, {name}) for t in known):
                e.append(Entry(f"t:{name}", models.label_for(name), "installed", "Installed",
                               detail=f"{name}\n{(m.get('size') or 0) / 1e9:.1f} GB on disk",
                               color=T.TEAL, mark="●" if name == cur else ""))
        e.append(Entry("custom", "Any Ollama model…", "", "Other",
                       detail="Type a tag, e.g. qwen3:8b or hf.co/user/repo-GGUF:Q4_K_M.\n"
                              "It must support tool calling."))
        current = next((x.id for x in e if x.mark), "")
        choice = await self._pick_palette(
            "Switch model", e, query=query, select=current,
            foot="● current · enter to switch (downloads if needed) · esc close")
        if not choice:
            return
        if choice == "custom":
            fut: asyncio.Future = asyncio.get_running_loop().create_future()
            self.push_screen(TextPrompt("Use any Ollama model",
                                        "e.g. qwen3:8b  or  hf.co/user/repo-GGUF:Q4_K_M",
                                        "It is downloaded if needed. It must support tool calling."),
                             callback=lambda r: fut.done() or fut.set_result(r))
            choice = await fut
            if choice:
                await self._switch_model(choice)
            return
        await self._switch_model(choice.split(":", 1)[1])

    async def _switch_model(self, name: str) -> None:
        if self.agent.busy:
            self.notify("busy — press esc to interrupt first", severity="warning")
            return
        p = models.find(name)
        tag = p.tag if p else name.strip()
        c = self.cfg
        if not tag:
            return
        if tag == c.model:
            await self.bridge.notice(f"already using {c.model_label}", "dim")
            return
        saved = {k: getattr(c, k) for k in ("model", "model_label", *models.SAMPLING_KEYS)}
        saved_extra = {k: c.extra.get(k) for k in ("stop", "min_p", "family")}
        explicit = set(c.extra.get("explicit") or ()) - {"model", "model_label"}
        old_label = c.model_label
        self.ready = False
        try:
            if not self.no_boot and await asyncio.to_thread(self.client.alive, 1.5):
                self.boot_status = f"unloading {old_label}…"
                await asyncio.to_thread(self.client.unload, 8)   # free its RAM first
            c.model = tag
            models.apply(c, explicit)
            self.agent.model_changed()
            self.refresh_chrome()
            await self.bridge.notice(f"switching to {c.model_label}…", "dim")
            size = ""
            if not self.no_boot:
                self.boot_status = "checking model…"
                if not await asyncio.to_thread(self.client.has_model):
                    await self._pull()
                self.boot_status = f"loading {c.model_label} into memory…"
                self.model_info = await asyncio.to_thread(self.client.load) or {}
                if self.model_info.get("size"):
                    size = f" ({self.model_info['size'] / 1e9:.1f} GB)"
            self._save("model", p.key if p else tag)
            self._save("model_label", None)
            c.extra["explicit"] = explicit | {"model"}
            await self.bridge.notice(f"✓ now using {c.model_label}{size} · saved as your default",
                                     "ok")
        except OllamaError as exc:
            for k, v in saved.items():
                setattr(c, k, v)
            c.extra.update(saved_extra)
            self.agent.model_changed()
            await self.bridge.notice(f"✗ couldn't switch to {tag}: {exc}", "error")
            await self.bridge.notice(f"still using {c.model_label}", "dim")
        finally:
            self._set_ready()
            self.refresh_chrome()

    async def _reload_model(self, why: str) -> None:
        """Reload the model so a new context size takes effect now rather
        than on the next message."""
        if self.no_boot or not self.ready or self.agent.busy:
            await self.bridge.notice(f"✓ {why} · takes effect on the next message", "ok")
            return
        self.ready = False
        self.boot_status = f"reloading {self.cfg.model_label}…"
        try:
            self.model_info = await asyncio.to_thread(self.client.load) or {}
            self.agent.model_changed()
            await self.bridge.notice(f"✓ {why} · model reloaded", "ok")
        except OllamaError as exc:
            await self.bridge.notice(f"✗ reload failed: {exc}", "error")
        finally:
            self._set_ready()

    # --------------------------------------------------------- commands
    async def _command(self, text: str) -> None:
        cmd, _, arg = text.partition(" ")
        cmd = cmd.lower()
        arg = arg.strip()
        if self.agent.busy and cmd not in ("/quit", "/exit", "/q", "/help", "/cost",
                                           "/mode", "/think", "/sidebar", "/settings"):
            self.notify("busy — press esc to interrupt first", severity="warning")
            return
        note = self.bridge.notice
        if cmd in ("/quit", "/exit", "/q"):
            await self.action_quit()
        elif cmd in ("/help", "/?"):
            await self._help()
        elif cmd in ("/new", "/clear", "/reset"):
            self.session.messages = self.agent.messages
            self.session.save()
            self.agent.reset()
            self.session = Session(cwd=str(self.workdir))
            self.step = 0
            await self.log_view.remove_children()
            await self._welcome()
            await note("✓ new conversation", "ok")
            self.refresh_side()
        elif cmd == "/undo":
            restored = self.agent.undo()
            if restored:
                await note("↶ reverted: " + ", ".join(restored), "ok")
            else:
                await note("nothing to undo", "dim")
            self.refresh_side()
        elif cmd == "/compact":
            if not self.ready:
                return
            await note("compacting conversation…", "dim")
            self.agent.busy = True
            self._turn_start = time.monotonic()
            self.set_activity("Compacting")
            try:
                summary = await self.agent.compact()
                await note("✓ conversation compacted", "ok")
                await self.emit(Horizontal(Static("⏺", classes="answer-dot"),
                                           Markdown(summary, classes="answer"),
                                           classes="answer-row"))
            except OllamaError as exc:
                await note(f"compact failed: {exc}", "error")
            finally:
                self.agent.busy = False
            self.refresh_side()
        elif cmd == "/mode":
            if arg in ("ask", "auto-edit", "yolo"):
                self.agent.mode = arg
            else:
                self.action_cycle_mode()
            await note(f"mode: {self.agent.mode} — {T.MODE_STYLE[self.agent.mode][1]}", "dim")
            self.refresh_side()
        elif cmd in ("/yolo",):
            self.agent.mode = "ask" if self.agent.mode == "yolo" else "yolo"
            await note(f"mode: {self.agent.mode}", "dim")
            self.refresh_side()
        elif cmd == "/think":
            self.action_toggle_think()
        elif cmd == "/sidebar":
            self.action_toggle_sidebar()
        elif cmd == "/init":
            self.run_worker(self._init_then_refresh(), group="agent")
        elif cmd == "/diff":
            await self._diff()
        elif cmd == "/cost":
            a = self.agent
            await note(f"tokens in {a.tokens_in:,} · out {a.tokens_out:,} · context "
                       f"{a.context_used():,}/{self.cfg.num_ctx:,} · cost $0.00 (local)", "info")
        elif cmd == "/export":
            path = self.workdir / f"tinycode-{self.session.id}.md"
            path.write_text(self._export_md(), encoding="utf-8")
            await note(f"✓ saved {path.name}", "ok")
        elif cmd == "/config":
            p = write_default_config()
            await note(f"config: {p}  (restart tinycode after editing)", "info")
        elif cmd in ("/settings", "/options", "/palette"):
            self.action_palette()
        elif cmd in ("/model", "/models"):
            await self._model(arg)
        elif cmd == "/resume":
            await self._resume()
        else:
            await note(f"unknown command {cmd} — try /help", "warn")

    async def _init_then_refresh(self) -> None:
        if not self.ready:
            self.notify("still starting up")
            return
        await self._turn(INIT_PROMPT)
        self.agent.refresh_system_prompt()
        if self.agent.memory_files:
            await self.bridge.notice("✓ project memory loaded: " + ", ".join(
                Path(p).name for p in self.agent.memory_files), "ok")

    async def _help(self) -> None:
        t = Text()
        t.append("Commands\n", style=f"bold {T.BLUE}")
        for c, d in COMMANDS:
            t.append(f"  {c:<10}", style=T.TEXT)
            t.append(f" {d}\n", style=T.MUTED)
        t.append("\nInput\n", style=f"bold {T.BLUE}")
        for k, d in (("@path", "attach a file or folder to your message"),
                     ("!cmd", "run a shell command yourself (output goes into context)"),
                     ("\\ + enter", "new line (also shift+enter / ctrl+j)"),
                     ("↑ / ↓", "message history")):
            t.append(f"  {k:<10}", style=T.TEXT)
            t.append(f" {d}\n", style=T.MUTED)
        t.append("\nKeys\n", style=f"bold {T.BLUE}")
        for k, d in (("ctrl+p", "settings, models and commands"),
                     ("esc", "interrupt the agent"), ("shift+tab", "cycle permission mode"),
                     ("ctrl+t", "reasoning on/off"), ("ctrl+o", "expand tool output & thinking"),
                     ("ctrl+b", "sidebar"), ("ctrl+n", "new conversation"),
                     ("ctrl+l", "clear screen"), ("ctrl+c ×2", "quit (ctrl+q / ctrl+d too)")):
            t.append(f"  {k:<10}", style=T.TEXT)
            t.append(f" {d}\n", style=T.MUTED)
        t.rstrip()
        await self.emit(Static(t, classes="welcome"))

    async def _diff(self) -> None:
        try:
            out = await asyncio.to_thread(
                subprocess.run, ["git", "diff", "--stat"], cwd=self.workdir,
                capture_output=True, text=True, timeout=10)
            stat = out.stdout.strip() if out.returncode == 0 else ""
        except (OSError, subprocess.SubprocessError):
            stat = ""
        changed = self.agent.tools.changed
        t = Text()
        t.append("Changed this session\n", style=f"bold {T.BLUE}")
        if not changed:
            t.append("  none\n", style=T.MUTED)
        for p, (a, r) in changed.items():
            t.append(f"  {p}  ", style=T.TEXT)
            t.append(f"+{a}", style=T.GREEN)
            t.append(f" -{r}\n", style=T.RED)
        if stat:
            t.append("\ngit diff --stat\n", style=f"bold {T.BLUE}")
            t.append(stat, style=T.TEXT_SOFT)
        t.rstrip()
        await self.emit(Static(t, classes="welcome"))

    async def _model(self, arg: str) -> None:
        if arg:
            await self._switch_model(arg)
        else:
            await self._model_picker()

    async def _pick(self, title: str, items: list[tuple[str, str]]) -> Optional[str]:
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self.push_screen(PickerScreen(title, items),
                         callback=lambda r: fut.done() or fut.set_result(r))
        return await fut

    async def _resume(self) -> None:
        items = await asyncio.to_thread(list_sessions, str(self.workdir))
        if not items:
            await self.bridge.notice("no saved conversations for this directory", "dim")
            return
        choice = await self._pick("Resume a conversation", [
            (str(s["path"]), f"{ago(s['updated']):>8}  ·  {s['turns']:>2} turns  ·  {short(s['title'], 60)}")
            for s in items])
        if not choice:
            return
        try:
            s = Session.load(Path(choice))
        except (OSError, ValueError) as exc:
            await self.bridge.notice(f"could not load: {exc}", "error")
            return
        self.session.messages = self.agent.messages
        self.session.save()
        self.session = s
        self.agent.reset()
        self.agent.load_messages(s.messages)
        self.agent.tools.todos = list(s.todos or [])
        self.agent.tools.todos_auto = bool(getattr(s, "todos_auto", False))
        await self.log_view.remove_children()
        await self.replay(s.messages)
        self.refresh_side()

    def _export_md(self) -> str:
        out = [f"# tinycode session {self.session.id}\n"]
        for m in self.agent.messages[1:]:
            role = m.get("role")
            if is_tool_result(m):
                out.append("```\n" + str(m.get("content", ""))[:3000] + "\n```\n")
            elif role == "user":
                out.append(f"## You\n\n{m.get('content', '')}\n")
            elif role == "assistant":
                for tc in m.get("tool_calls") or []:
                    fn = tc.get("function") or {}
                    out.append(f"**{fn.get('name')}** `{json.dumps(fn.get('arguments'))[:300]}`\n")
                if m.get("content"):
                    out.append(f"## tinycode\n\n{m['content']}\n")
            elif role == "tool":
                out.append("```\n" + str(m.get("content", ""))[:3000] + "\n```\n")
        return "\n".join(out)
