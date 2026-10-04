"""Configuration, paths and logging.

Settings come from (later wins):
  built-in defaults  <  ~/.config/tinycode/config.toml  <  environment  <  CLI flags
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

DEFAULT_MODEL = "hf.co/bloomer010/Ling-3.0-tiny-GGUF:Q4_K_XL"


def _xdg(var: str, fallback: str) -> Path:
    base = os.environ.get(var)
    return Path(base) if base else Path.home() / fallback


def _ensure(d: Path) -> Path:
    try:
        d.mkdir(parents=True, exist_ok=True)
        return d
    except OSError:
        alt = Path(tempfile.gettempdir()) / "tinycode" / d.name
        alt.mkdir(parents=True, exist_ok=True)
        return alt


CONFIG_DIR = _xdg("XDG_CONFIG_HOME", ".config") / "tinycode"
DATA_DIR = _xdg("XDG_DATA_HOME", ".local/share") / "tinycode"
CACHE_DIR = _xdg("XDG_CACHE_HOME", ".cache") / "tinycode"
CONFIG_PATH = CONFIG_DIR / "config.toml"


def data_dir() -> Path:
    return _ensure(DATA_DIR)


def cache_dir() -> Path:
    return _ensure(CACHE_DIR)


def sessions_dir() -> Path:
    return _ensure(DATA_DIR / "sessions")


def log_path() -> Path:
    return cache_dir() / "tinycode.log"


def log(msg: str) -> None:
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    try:
        p = log_path()
        # keep the log from growing forever
        if p.exists() and p.stat().st_size > 5_000_000:
            p.replace(p.with_suffix(".log.1"))
        with open(p, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


@dataclass
class Config:
    # model + server
    model: str = DEFAULT_MODEL
    model_label: str = "Ling-3.0-tiny"
    host: str = "127.0.0.1:11434"
    # sampling / budget
    num_ctx: int = 32768
    num_predict: int = 16384         # hard cap per model step (stops runaway output)
    temperature: float = 0.6
    top_p: float = 0.95
    top_k: int = 20
    repeat_penalty: float = 1.0       # penalties hurt code (identifiers repeat)
    think: bool = True               # let the model reason before acting
    think_budget: int = 3000         # max reasoning tokens per step (0 = unlimited)
    preserve_thinking: bool = True   # keep reasoning in history (cache-friendly)
    # "native": Ollama's built-in tool calling (default, most reliable).
    # "stream": experimental — the model writes tool calls as text so files can
    #           be shown live while generated.
    tool_mode: str = "native"
    # agent
    max_steps: int = 40
    max_tool_chars: int = 12000      # per tool result sent back to the model
    auto_check: bool = True          # syntax/error check after every file change
    check_rounds: int = 3            # max automatic "fix the remaining errors" rounds
    app_check: bool = True           # run changed web pages in a simulated browser
    run_tests: bool = True           # run the project's tests before finishing (if any)
    test_timeout: int = 180
    bash_timeout: int = 120
    # permissions: "ask" | "auto-edit" | "yolo"
    mode: str = "ask"
    # hard-confine every tool (reads, writes, shell) to the project directory
    sandbox: bool = True
    # ollama lifecycle
    manage_server: bool = True       # start `ollama serve` if it is not running
    stop_server_on_exit: bool = True  # stop the server tinycode started/adopted
    unload_on_exit: bool = True      # free the model's RAM when tinycode quits
    # ui
    show_thinking: bool = False
    sidebar: bool = True
    theme: str = "tokyo-night"       # colour palette (run /theme to browse)
    # internal (not user facing)
    extra: dict = field(default_factory=dict)

    @property
    def base_url(self) -> str:
        h = self.host.rstrip("/")
        return h if h.startswith("http") else f"http://{h}"

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("extra", None)
        return d


_BOOL_TRUE = {"1", "true", "yes", "on"}


def _coerce(value: Any, like: Any) -> Any:
    if isinstance(like, bool):
        if isinstance(value, str):
            return value.strip().lower() in _BOOL_TRUE
        return bool(value)
    if isinstance(like, int):
        return int(value)
    if isinstance(like, float):
        return float(value)
    return str(value)


def _read_toml(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        if sys.version_info >= (3, 11):
            import tomllib
        else:  # pragma: no cover
            import tomli as tomllib  # type: ignore
    except ImportError:  # pragma: no cover
        log("config.toml present but no TOML parser available; ignoring it")
        return {}
    try:
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
    except Exception as exc:  # noqa: BLE001
        log(f"could not parse {path}: {exc}")
        sys.stderr.write(f"tinycode: ignoring invalid config {path}: {exc}\n")
        return {}
    # allow both flat keys and [model]/[agent]/... tables
    flat: dict = {}
    for k, v in data.items():
        if isinstance(v, dict):
            flat.update(v)
        else:
            flat[k] = v
    return flat


def load_config(overrides: dict | None = None) -> Config:
    cfg = Config()
    known = {f.name: f for f in fields(Config) if f.name != "extra"}
    explicit: set[str] = set()

    def apply(src: dict) -> None:
        for k, v in src.items():
            k = k.replace("-", "_")
            if k in known and v is not None:
                try:
                    setattr(cfg, k, _coerce(v, getattr(cfg, k)))
                    explicit.add(k)
                except (TypeError, ValueError):
                    log(f"bad config value for {k}: {v!r}")

    _migrate_config()
    apply(_read_toml(CONFIG_PATH))
    env = {}
    if os.environ.get("OLLAMA_HOST"):
        env["host"] = os.environ["OLLAMA_HOST"]
    for f in known:
        val = os.environ.get(f"TINYCODE_{f.upper()}")
        if val is not None:
            env[f] = val
    apply(env)
    apply(overrides or {})
    if cfg.mode not in ("ask", "auto-edit", "yolo"):
        cfg.mode = "ask"
    if cfg.tool_mode not in ("stream", "native"):
        cfg.tool_mode = "native"
    from .tui.theme import set_theme as _set_theme
    cfg.theme = _set_theme(cfg.theme)     # normalise aliases, drop unknown names
    cfg.num_ctx = max(2048, cfg.num_ctx)
    from . import models
    models.apply(cfg, explicit)
    cfg.extra["explicit"] = explicit
    return cfg


DEFAULT_CONFIG_TOML = """# tinycode configuration
# Everything is commented out, so tinycode's built-in defaults apply.
# Uncomment a line and change it to override that setting.

[model]
# model = "ling"         # a preset (see `tinycode models`) or any Ollama tag
# host = "127.0.0.1:11434"
# num_ctx = 32768        # context window (tokens). Lower it if you run out of RAM.
# num_predict = 16384    # max tokens per model step
# temperature = 0.6
# think = true           # reasoning before acting (slower, more accurate)
# tool_mode = "native"   # native: Ollama tool calling · stream: experimental live view

[agent]
# max_steps = 40
# auto_check = true      # check syntax after every edit and before finishing
# app_check = true       # run changed web pages in a simulated browser before finishing
# run_tests = true       # run the project's tests before finishing, when code changed
# bash_timeout = 120
# mode = "ask"           # ask | auto-edit | yolo
# sandbox = true         # never touch anything outside the project directory

[ollama]
# manage_server = true        # start `ollama serve` automatically
# stop_server_on_exit = true  # stop it again when tinycode quits
# unload_on_exit = true       # free the model's RAM on quit

[ui]
# show_thinking = false
# sidebar = true
# theme = "tokyo-night"   # /theme browses all (catppuccin-mocha, nord, gruvbox-dark, dracula, …)
"""

# The 2.0.0 template wrote every value out explicitly. If a user's file is
# still exactly that, it holds no real choices: upgrade it so new defaults apply.
_V200_TEMPLATE = """# tinycode configuration
# Every key is optional; delete what you don't need.

[model]
model = "hf.co/bloomer010/Ling-3.0-tiny-GGUF:Q4_K_XL"
model_label = "Ling-3.0-tiny"
host = "127.0.0.1:11434"
num_ctx = 16384        # context window (tokens). Lower it if you run out of RAM.
num_predict = 4096     # max tokens per model step
temperature = 0.6
top_p = 0.95
top_k = 20
think = true           # reasoning before acting (slower, more accurate)

[agent]
max_steps = 40
bash_timeout = 120
mode = "ask"           # ask | auto-edit | yolo

[ollama]
manage_server = true        # start `ollama serve` automatically
stop_server_on_exit = true  # stop it again when tinycode quits
unload_on_exit = true       # free the model's RAM on quit

[ui]
show_thinking = false
sidebar = true
"""


def _migrate_config() -> None:
    try:
        if CONFIG_PATH.is_file() and CONFIG_PATH.read_text(encoding="utf-8") == _V200_TEMPLATE:
            CONFIG_PATH.write_text(DEFAULT_CONFIG_TOML, encoding="utf-8")
            log("upgraded untouched 2.0.0 config to the new defaults")
    except OSError:
        pass


def write_default_config() -> Path:
    _ensure(CONFIG_DIR)
    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text(DEFAULT_CONFIG_TOML, encoding="utf-8")
    return CONFIG_PATH


_SECTIONS = {
    "model": ("model", "model_label", "host", "num_ctx", "num_predict", "temperature", "top_p",
              "top_k", "repeat_penalty", "think", "think_budget", "preserve_thinking",
              "tool_mode"),
    "agent": ("max_steps", "max_tool_chars", "auto_check", "check_rounds", "app_check",
              "run_tests", "test_timeout", "bash_timeout", "mode", "sandbox"),
    "ollama": ("manage_server", "stop_server_on_exit", "unload_on_exit"),
    "ui": ("show_thinking", "sidebar", "theme"),
}


def _toml_value(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    return '"' + str(v).replace("\\", "\\\\").replace('"', '\\"') + '"'


def save_setting(key: str, value: Any) -> Path:
    """Set one key in config.toml, keeping the rest of the file (and its
    comments) as it is. Replaces an existing or commented-out line, or adds
    the key to its section. value=None comments the key out."""
    import re
    write_default_config()
    text = CONFIG_PATH.read_text(encoding="utf-8")
    lines = text.split("\n")
    if value is None:      # unset: comment the line out
        rx = re.compile(rf"^\s*{re.escape(key)}\s*=")
        lines = ["# " + l.lstrip() if rx.match(l) else l for l in lines]
        CONFIG_PATH.write_text("\n".join(lines), encoding="utf-8")
        return CONFIG_PATH
    line = f"{key} = {_toml_value(value)}"
    rx = re.compile(rf"^\s*#?\s*{re.escape(key)}\s*=")
    for i, l in enumerate(lines):
        if rx.match(l):
            comment = ""
            m = re.search(r"\s+#\s.*$", l.split("=", 1)[1]) if "=" in l else None
            if m and not l.lstrip().startswith("#"):
                comment = m.group(0)
            lines[i] = line + comment
            break
    else:
        section = next((s for s, keys in _SECTIONS.items() if key in keys), "agent")
        try:
            at = lines.index(f"[{section}]") + 1
            lines.insert(at, line)
        except ValueError:
            lines += ["", f"[{section}]", line]
    CONFIG_PATH.write_text("\n".join(lines), encoding="utf-8")
    return CONFIG_PATH
