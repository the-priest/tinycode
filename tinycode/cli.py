"""Command-line entry point.

  tinycode [DIR]                 interactive TUI
  tinycode -p "prompt" [DIR]     headless: run one task, print the answer
  tinycode -c                    continue the last conversation in this dir
  tinycode doctor                check the installation
  tinycode config                create/show the config file
  tinycode update                upgrade tinycode in place
"""

from __future__ import annotations

import argparse
import asyncio
import atexit
import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

from . import APP_NAME, __version__
from .config import CONFIG_PATH, Config, load_config, log, log_path, write_default_config


def parse_args(argv: list[str]) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        prog=APP_NAME,
        description="tinycode — a fast, fully local coding agent (Ollama).",
        epilog="subcommands: doctor · config · update   |   docs: /help inside the app")
    ap.add_argument("directory", nargs="?", default=".", help="project directory (default: .)")
    ap.add_argument("-p", "--print", dest="prompt", metavar="PROMPT",
                    help="headless: run PROMPT to completion and print the answer")
    ap.add_argument("-c", "--continue", dest="cont", action="store_true",
                    help="continue the most recent conversation in this directory")
    ap.add_argument("-m", "--model", help="Ollama model tag to use")
    ap.add_argument("--mode", choices=["ask", "auto-edit", "yolo"],
                    help="permission mode (default: ask)")
    ap.add_argument("--yolo", action="store_true", help="same as --mode yolo")
    ap.add_argument("--think", dest="think", action="store_true", default=None,
                    help="enable model reasoning")
    ap.add_argument("--no-think", dest="think", action="store_false",
                    help="disable model reasoning (faster)")
    ap.add_argument("--ctx", type=int, metavar="TOKENS", help="context window size")
    ap.add_argument("--keep-server", action="store_true",
                    help="don't stop the Ollama server on exit")
    ap.add_argument("--no-boot", action="store_true",
                    help="don't start/load anything (UI testing)")
    ap.add_argument("--version", action="version", version=f"{APP_NAME} {__version__}")
    return ap.parse_args(argv)


def build_config(args: argparse.Namespace) -> Config:
    over: dict[str, Any] = {}
    if args.model:
        over["model"] = args.model
        over["model_label"] = args.model.split("/")[-1]
    if args.yolo:
        over["mode"] = "yolo"
    elif args.mode:
        over["mode"] = args.mode
    if args.think is not None:
        over["think"] = args.think
    if args.ctx:
        over["num_ctx"] = args.ctx
    if args.keep_server:
        over["stop_server_on_exit"] = False
    return load_config(over)


def install_exit_guards(shutdown) -> None:
    """Unload the model / stop the server on every exit path: quit, window
    close (SIGHUP), SIGTERM, or a crash."""
    def handler(signum: int, _frame: Any) -> None:
        log(f"signal {signum}: shutting down")
        try:
            shutdown()
        finally:
            os._exit(128 + signum)

    for sig in (getattr(signal, "SIGHUP", None), signal.SIGTERM):
        if sig is None:
            continue
        try:
            signal.signal(sig, handler)
        except (ValueError, OSError, RuntimeError):
            pass
    atexit.register(shutdown)


# ------------------------------------------------------------------ headless

def run_headless(cfg: Config, workdir: Path, prompt: str, cont: bool) -> int:
    from .agent import Agent, AgentUI, Decision
    from .context import expand_mentions
    from .ollama import Ollama, OllamaError
    from .session import Session, latest_session

    err = sys.stderr
    color = err.isatty()

    def c(code: str, s: str) -> str:
        return f"\033[{code}m{s}\033[0m" if color else s

    class UI(AgentUI):
        async def thinking_end(self, seconds: float) -> None:
            err.write(c("2;3", f"∴ thought for {seconds:.1f}s\n"))

        async def tool_start(self, call_id: str, name: str, args: dict) -> None:
            from .tui.widgets import tool_target
            err.write(c("33", f"⏺ {name}") + c("2", f"({tool_target(name, args)[:100]})") + "\n")

        async def tool_end(self, call_id, name, args, result, seconds) -> None:
            col = "2" if result.ok else "31"
            err.write(c(col, f"  ⎿ {result.summary or result.output.splitlines()[0][:120]}") + "\n")

        async def notice(self, text: str, level: str = "info") -> None:
            err.write(c("33" if level in ("warn", "error") else "2", f"  {text}") + "\n")

        async def approve(self, name, args, diff, warning) -> Decision:
            if not sys.stdin.isatty():
                return Decision(False, feedback="headless run without --yolo / --mode")
            from .tui.widgets import tool_target
            err.write(c("33;1", f"\nallow {name}: {tool_target(name, args)[:200]}?") +
                      (c("31", f"  {warning}") if warning else "") + " [y/N] ")
            err.flush()
            return Decision(sys.stdin.readline().strip().lower() in ("y", "yes"))

    client = Ollama(cfg)
    done = {"v": False}

    def shutdown() -> None:
        if done["v"]:
            return
        done["v"] = True
        try:
            if cfg.unload_on_exit and client.alive(timeout=1.5):
                client.unload(timeout=8)
            client.stop_server()
        except Exception:  # noqa: BLE001
            pass

    install_exit_guards(shutdown)
    try:
        client.begin_server()
        client.wait_up()
        if not client.has_model():
            err.write(c("33", f"downloading {cfg.model}…\n"))
            last = [""]

            def prog(status: str, frac: float) -> None:
                msg = f"{status} {frac * 100:.0f}%" if frac >= 0 else status
                if msg != last[0]:
                    last[0] = msg
                    err.write("\r" + msg[:70].ljust(70))
            client.pull(prog)
            err.write("\n")
        client.load()
    except OllamaError as exc:
        err.write(c("31", f"tinycode: {exc}\n"))
        return 1

    agent = Agent(cfg, client, workdir, UI())
    session = latest_session(str(workdir)) if cont else None
    if session:
        agent.load_messages(session.messages)
    else:
        session = Session(cwd=str(workdir))
    text, _ = expand_mentions(prompt, workdir)
    try:
        answer = asyncio.run(agent.run(text))
    except KeyboardInterrupt:
        agent.cancel()
        return 130
    finally:
        session.messages = agent.messages
        session.save()
        shutdown()
    sys.stdout.write(answer + "\n")
    return 0


# ------------------------------------------------------------------ doctor

def doctor() -> int:
    from .ollama import Ollama
    cfg = load_config()
    ok_all = True

    def line(ok: Optional[bool], label: str, detail: str = "") -> None:
        nonlocal ok_all
        tty = sys.stdout.isatty()
        mark = {True: "\033[32m✓\033[0m" if tty else "ok",
                False: "\033[31m✗\033[0m" if tty else "FAIL",
                None: "\033[33m!\033[0m" if tty else "--"}[ok]
        if ok is False:
            ok_all = False
        print(f" {mark} {label:<22} {detail}")

    print(f"tinycode {__version__} doctor\n")
    line(sys.version_info >= (3, 9), "python", sys.version.split()[0] + f"  ({sys.executable})")
    try:
        import textual
        line(True, "textual", textual.__version__)
    except ImportError:
        line(False, "textual", "missing — pip install textual")
    exe = Ollama.binary()
    line(bool(exe), "ollama binary", exe or "not found — https://ollama.com/download")
    client = Ollama(cfg)
    up = client.alive()
    line(True if up else None, "ollama server",
         f"running at {cfg.base_url} (v{client.version()})" if up
         else "not running (tinycode starts it automatically)")
    if up:
        has = client.has_model()
        line(has, "model", cfg.model if has else f"{cfg.model} not pulled — run tinycode once")
        info = client.show() if has else {}
        caps = info.get("capabilities") or []
        if caps:
            line("tools" in caps, "tool calling", ", ".join(caps))
    else:
        line(None, "model", f"{cfg.model} (can't check: server down)")
    line(True if shutil.which("rg") else None, "ripgrep",
         shutil.which("rg") or "optional — faster search (apt install ripgrep)")
    line(True if shutil.which("git") else None, "git", shutil.which("git") or "optional")
    line(True, "config", str(CONFIG_PATH) + ("" if CONFIG_PATH.exists() else " (defaults)"))
    line(True, "log", str(log_path()))
    try:
        mem = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 1e9
        line(mem >= 8 or None, "memory", f"{mem:.1f} GB" + ("" if mem >= 8 else
                                                             " — 8 GB+ recommended"))
    except (ValueError, OSError, AttributeError):
        pass
    print("\nall good!" if ok_all else "\nsome checks failed (see above)")
    return 0 if ok_all else 1


def update() -> int:
    ref = os.environ.get("TINYCODE_REF", "main")
    url = f"https://github.com/the-priest/tinycode/archive/refs/heads/{ref}.tar.gz"
    print(f"updating tinycode from {url}")
    return subprocess.call([sys.executable, "-m", "pip", "install", "--upgrade",
                            "--disable-pip-version-check", "-q", url])


# ------------------------------------------------------------------ main

def main(argv: Optional[list[str]] = None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in ("doctor", "config", "update"):
        if argv[0] == "doctor":
            raise SystemExit(doctor())
        if argv[0] == "config":
            print(write_default_config())
            raise SystemExit(0)
        raise SystemExit(update())

    args = parse_args(argv)
    workdir = Path(args.directory).expanduser().resolve()
    if not workdir.is_dir():
        sys.stderr.write(f"tinycode: not a directory: {workdir}\n")
        raise SystemExit(2)
    cfg = build_config(args)
    log(f"{APP_NAME} {__version__} start · dir={workdir} · model={cfg.model}")

    if args.prompt is not None:
        prompt = args.prompt
        if prompt == "-" or (not prompt and not sys.stdin.isatty()):
            prompt = sys.stdin.read()
        raise SystemExit(run_headless(cfg, workdir, prompt, args.cont))

    try:
        from .tui.app import TinyCodeApp
    except ImportError as exc:  # pragma: no cover
        sys.stderr.write(f"tinycode needs the 'textual' package ({exc}).\n"
                         "Re-run the installer or: pip install textual\n")
        raise SystemExit(1)
    from .session import latest_session

    resume = latest_session(str(workdir)) if args.cont else None
    app = TinyCodeApp(cfg, workdir, no_boot=args.no_boot, resume=resume)
    install_exit_guards(app.shutdown_blocking)
    app.run()


if __name__ == "__main__":
    main()
