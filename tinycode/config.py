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
    repeat_penalty: float = 1.05
    think: bool = True               # let the model reason before acting
    # "native": Ollama's built-in tool calling (default, most reliable).
    # "stream": experimental — the model writes tool calls as text so files can
    #           be shown live while generated.
    tool_mode: str = "native"
    # agent
    max_steps: int = 40
    max_tool_chars: int = 12000      # per tool result sent back to the model
    auto_check: bool = True          # syntax/error check after every file change
    check_rounds: int = 3            # max automatic "fix the remaining errors" rounds
    bash_timeout: int = 120
    # permissions: "ask" | "auto-edit" | "yolo"
    mode: str = "ask"
    # ollama lifecycle
    manage_server: bool = True       # start `ollama serve` if it is not running
    stop_server_on_exit: bool = True  # stop the server tinycode started/adopted
    unload_on_exit: bool = True      # free the model's RAM when tinycode quits
    # ui
    show_thinking: bool = False
    sidebar: bool = True
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

    def apply(src: dict) -> None:
        for k, v in src.items():
            k = k.replace("-", "_")
            if k in known and v is not None:
                try:
                    setattr(cfg, k, _coerce(v, getattr(cfg, k)))
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
    cfg.num_ctx = max(2048, cfg.num_ctx)
    return cfg


DEFAULT_CONFIG_TOML = """# tinycode configuration
# Everything is commented out, so tinycode's built-in defaults apply.
# Uncomment a line and change it to override that setting.

[model]
# model = "hf.co/bloomer010/Ling-3.0-tiny-GGUF:Q4_K_XL"
# model_label = "Ling-3.0-tiny"
# host = "127.0.0.1:11434"
# num_ctx = 32768        # context window (tokens). Lower it if you run out of RAM.
# num_predict = 16384    # max tokens per model step
# temperature = 0.6
# think = true           # reasoning before acting (slower, more accurate)
# tool_mode = "native"   # native: Ollama tool calling · stream: experimental live view

[agent]
# max_steps = 40
# auto_check = true      # check syntax after every edit and before finishing
# bash_timeout = 120
# mode = "ask"           # ask | auto-edit | yolo

[ollama]
# manage_server = true        # start `ollama serve` automatically
# stop_server_on_exit = true  # stop it again when tinycode quits
# unload_on_exit = true       # free the model's RAM on quit

[ui]
# show_thinking = false
# sidebar = true
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
