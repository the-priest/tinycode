"""Colour palettes for the TUI.

Thirteen schemes, picked to match the colour sets people already run in
terminal setups on KDE/Konsole, fish and the like.  The names are the same
ones those ecosystems use, so ``theme = "catppuccin-mocha"`` feels familiar.

Only the *active* palette is exposed as module globals (``T.BG``, ``T.BLUE``
…), so every widget keeps referring to ``theme as T`` and switching is a
matter of calling :func:`set_theme` and redrawing.

Each palette defines the same keys::

    BG BG_ALT PANEL BORDER MUTED DIM TEXT TEXT_SOFT
    BLUE CYAN GREEN YELLOW ORANGE RED PURPLE TEAL
    GREEN_BG RED_BG SYNTAX

``SYNTAX`` names a Pygments theme used for the code previews in the
transcript; it is validated at import and falls back to ``monokai``.
"""

from __future__ import annotations

try:  # pygments ships with rich, but never let it break startup
    import pygments.styles as _pyg_styles
except Exception:  # noqa: BLE001
    _pyg_styles = None

DEFAULT_THEME = "tokyo-night"

# Keys every palette must provide (the last two are the diff backgrounds).
_KEYS = (
    "BG", "BG_ALT", "PANEL", "BORDER", "MUTED", "DIM", "TEXT", "TEXT_SOFT",
    "BLUE", "CYAN", "GREEN", "YELLOW", "ORANGE", "RED", "PURPLE", "TEAL",
    "GREEN_BG", "RED_BG", "SYNTAX",
)

# The order shown in the theme picker (roughly dark neutral -> colourful).
PALETTES: dict[str, dict[str, str]] = {
    "tokyo-night": {
        "BG": "#0f1117", "BG_ALT": "#13151d", "PANEL": "#161821", "BORDER": "#2a2e42",
        "MUTED": "#565f89", "DIM": "#3b4261", "TEXT": "#c0caf5", "TEXT_SOFT": "#a9b1d6",
        "BLUE": "#7aa2f7", "CYAN": "#7dcfff", "GREEN": "#9ece6a", "YELLOW": "#e0af68",
        "ORANGE": "#ff9e64", "RED": "#f7768e", "PURPLE": "#bb9af7", "TEAL": "#73daca",
        "GREEN_BG": "#16261a", "RED_BG": "#2a1619", "SYNTAX": "monokai",
    },
    "tokyo-night-storm": {
        "BG": "#24283b", "BG_ALT": "#1f2335", "PANEL": "#1f2335", "BORDER": "#3b4261",
        "MUTED": "#565f89", "DIM": "#292e42", "TEXT": "#c0caf5", "TEXT_SOFT": "#a9b1d6",
        "BLUE": "#7aa2f7", "CYAN": "#7dcfff", "GREEN": "#9ece6a", "YELLOW": "#e0af68",
        "ORANGE": "#ff9e64", "RED": "#f7768e", "PURPLE": "#bb9af7", "TEAL": "#73daca",
        "GREEN_BG": "#1e3325", "RED_BG": "#3a2027", "SYNTAX": "monokai",
    },
    "catppuccin-mocha": {
        "BG": "#1e1e2e", "BG_ALT": "#181825", "PANEL": "#181825", "BORDER": "#45475a",
        "MUTED": "#6c7086", "DIM": "#313244", "TEXT": "#cdd6f4", "TEXT_SOFT": "#a6adc8",
        "BLUE": "#89b4fa", "CYAN": "#89dceb", "GREEN": "#a6e3a1", "YELLOW": "#f9e2af",
        "ORANGE": "#fab387", "RED": "#f38ba8", "PURPLE": "#cba6f7", "TEAL": "#94e2d5",
        "GREEN_BG": "#1e3a2a", "RED_BG": "#3a1f2b", "SYNTAX": "dracula",
    },
    "catppuccin-macchiato": {
        "BG": "#24273a", "BG_ALT": "#1e2030", "PANEL": "#1e2030", "BORDER": "#494d64",
        "MUTED": "#6e738d", "DIM": "#363a4f", "TEXT": "#cad3f5", "TEXT_SOFT": "#a5adcb",
        "BLUE": "#8aadf4", "CYAN": "#91d7e3", "GREEN": "#a6da95", "YELLOW": "#eed49f",
        "ORANGE": "#f5a97f", "RED": "#ed8796", "PURPLE": "#c6a0f6", "TEAL": "#8bd5ca",
        "GREEN_BG": "#1f3a2c", "RED_BG": "#3a1f28", "SYNTAX": "dracula",
    },
    "nord": {
        "BG": "#2e3440", "BG_ALT": "#292e39", "PANEL": "#3b4252", "BORDER": "#4c566a",
        "MUTED": "#7b88a1", "DIM": "#434c5e", "TEXT": "#eceff4", "TEXT_SOFT": "#d8dee9",
        "BLUE": "#88c0d0", "CYAN": "#8fbcbb", "GREEN": "#a3be8c", "YELLOW": "#ebcb8b",
        "ORANGE": "#d08770", "RED": "#bf616a", "PURPLE": "#b48ead", "TEAL": "#8fbcbb",
        "GREEN_BG": "#2b3a30", "RED_BG": "#3a2a2c", "SYNTAX": "nord",
    },
    "gruvbox-dark": {
        "BG": "#1d2021", "BG_ALT": "#282828", "PANEL": "#282828", "BORDER": "#3c3836",
        "MUTED": "#928374", "DIM": "#504945", "TEXT": "#ebdbb2", "TEXT_SOFT": "#d5c4a1",
        "BLUE": "#83a598", "CYAN": "#8ec07c", "GREEN": "#b8bb26", "YELLOW": "#fabd2f",
        "ORANGE": "#fe8019", "RED": "#fb4934", "PURPLE": "#d3869b", "TEAL": "#8ec07c",
        "GREEN_BG": "#2b3320", "RED_BG": "#3a2422", "SYNTAX": "gruvbox-dark",
    },
    "dracula": {
        "BG": "#282a36", "BG_ALT": "#21222c", "PANEL": "#21222c", "BORDER": "#44475a",
        "MUTED": "#6272a4", "DIM": "#44475a", "TEXT": "#f8f8f2", "TEXT_SOFT": "#c8c8d0",
        "BLUE": "#8be9fd", "CYAN": "#8be9fd", "GREEN": "#50fa7b", "YELLOW": "#f1fa8c",
        "ORANGE": "#ffb86c", "RED": "#ff5555", "PURPLE": "#bd93f9", "TEAL": "#50fa7b",
        "GREEN_BG": "#1e3a28", "RED_BG": "#3a1f22", "SYNTAX": "dracula",
    },
    "one-dark": {
        "BG": "#282c34", "BG_ALT": "#21252b", "PANEL": "#21252b", "BORDER": "#3e4451",
        "MUTED": "#5c6370", "DIM": "#3e4451", "TEXT": "#abb2bf", "TEXT_SOFT": "#9da5b4",
        "BLUE": "#61afef", "CYAN": "#56b6c2", "GREEN": "#98c379", "YELLOW": "#e5c07b",
        "ORANGE": "#d19a66", "RED": "#e06c75", "PURPLE": "#c678dd", "TEAL": "#56b6c2",
        "GREEN_BG": "#26332a", "RED_BG": "#3a2529", "SYNTAX": "one-dark",
    },
    "kanagawa": {
        "BG": "#1f1f28", "BG_ALT": "#16161d", "PANEL": "#2a2a37", "BORDER": "#363646",
        "MUTED": "#727169", "DIM": "#2a2a37", "TEXT": "#dcd7ba", "TEXT_SOFT": "#c8c093",
        "BLUE": "#7e9cd8", "CYAN": "#7fb4ca", "GREEN": "#98bb6c", "YELLOW": "#e6c384",
        "ORANGE": "#ffa066", "RED": "#e46876", "PURPLE": "#957fb8", "TEAL": "#7aa89f",
        "GREEN_BG": "#22301f", "RED_BG": "#351f24", "SYNTAX": "gruvbox-dark",
    },
    "everforest": {
        "BG": "#2d353b", "BG_ALT": "#272e33", "PANEL": "#343f44", "BORDER": "#475258",
        "MUTED": "#859289", "DIM": "#3d484d", "TEXT": "#d3c6aa", "TEXT_SOFT": "#9da9a0",
        "BLUE": "#7fbbb3", "CYAN": "#83c092", "GREEN": "#a7c080", "YELLOW": "#dbbc7f",
        "ORANGE": "#e69875", "RED": "#e67e80", "PURPLE": "#d699b6", "TEAL": "#83c092",
        "GREEN_BG": "#2b3a2c", "RED_BG": "#3a2a2b", "SYNTAX": "gruvbox-dark",
    },
    "solarized-dark": {
        "BG": "#002b36", "BG_ALT": "#073642", "PANEL": "#073642", "BORDER": "#586e75",
        "MUTED": "#657b83", "DIM": "#073642", "TEXT": "#93a1a1", "TEXT_SOFT": "#839496",
        "BLUE": "#268bd2", "CYAN": "#2aa198", "GREEN": "#859900", "YELLOW": "#b58900",
        "ORANGE": "#cb4b16", "RED": "#dc322f", "PURPLE": "#6c71c4", "TEAL": "#2aa198",
        "GREEN_BG": "#0a2f1e", "RED_BG": "#3a1a1a", "SYNTAX": "solarized-dark",
    },
    "ayu-mirage": {
        "BG": "#1f2430", "BG_ALT": "#191e2a", "PANEL": "#242936", "BORDER": "#33415e",
        "MUTED": "#6c7393", "DIM": "#2a3040", "TEXT": "#cbccc6", "TEXT_SOFT": "#a3a6b0",
        "BLUE": "#73d0ff", "CYAN": "#95e6cb", "GREEN": "#bae67e", "YELLOW": "#ffd580",
        "ORANGE": "#ffa759", "RED": "#f28779", "PURPLE": "#d4bfff", "TEAL": "#95e6cb",
        "GREEN_BG": "#24331f", "RED_BG": "#3a2422", "SYNTAX": "monokai",
    },
    "material-ocean": {
        "BG": "#0f111a", "BG_ALT": "#0b0e14", "PANEL": "#1a1c25", "BORDER": "#2c3040",
        "MUTED": "#4b526d", "DIM": "#232735", "TEXT": "#a6accd", "TEXT_SOFT": "#8f93b0",
        "BLUE": "#82aaff", "CYAN": "#89ddff", "GREEN": "#c3e88d", "YELLOW": "#ffcb6b",
        "ORANGE": "#f78c6c", "RED": "#f07178", "PURPLE": "#c792ea", "TEAL": "#89ddff",
        "GREEN_BG": "#1b2b1c", "RED_BG": "#331f24", "SYNTAX": "material",
    },
}

# Display order for pickers and docs.
NAMES: list[str] = list(PALETTES)

# Loose spellings people type from muscle memory.
_ALIASES = {
    "tokyo": "tokyo-night", "tokyonight": "tokyo-night", "tokyo-night": "tokyo-night",
    "storm": "tokyo-night-storm",
    "catppuccin": "catppuccin-mocha", "mocha": "catppuccin-mocha",
    "macchiato": "catppuccin-macchiato",
    "gruvbox": "gruvbox-dark",
    "onedark": "one-dark", "atom-one-dark": "one-dark",
    "solarized": "solarized-dark",
    "ayu": "ayu-mirage", "mirage": "ayu-mirage",
    "material": "material-ocean", "ocean": "material-ocean",
}


def _valid_syntax(theme: str) -> str:
    try:
        if _pyg_styles is None:
            return "monokai"
        return theme if theme in set(_pyg_styles.get_all_styles()) else "monokai"
    except Exception:  # noqa: BLE001 - never let a style lookup break startup
        return "monokai"


# Make sure every palette is complete and references a real syntax theme.
for _name, _pal in PALETTES.items():
    _missing = [k for k in _KEYS if k not in _pal]
    if _missing:  # pragma: no cover - authoring guard
        raise ValueError(f"theme {_name!r} is missing {', '.join(_missing)}")
    _pal["SYNTAX"] = _valid_syntax(_pal["SYNTAX"])


TOOL_LABELS = {
    "read_file": "Read", "write_file": "Write", "edit_file": "Edit", "bash": "Bash",
    "glob": "Glob", "grep": "Grep", "list_dir": "List", "todowrite": "Plan",
    "fetch_url": "Fetch", "check": "Check", "test_app": "Test app",
}

_active = DEFAULT_THEME
MODE_STYLE: dict[str, tuple[str, str]] = {}


def _apply_derived() -> None:
    """Rebuild anything that captured palette colours at import time."""
    global MODE_STYLE
    MODE_STYLE = {
        "ask": (MUTED, "ask before edits & commands"),
        "auto-edit": (PURPLE, "auto-accept edits"),
        "yolo": (RED, "yolo — everything auto-approved"),
    }


def set_theme(name: str) -> str:
    """Activate *name* (accepting loose spellings) and return the real name."""
    key = str(name or "").strip().lower().replace("_", "-").replace(" ", "-")
    key = _ALIASES.get(key, key)
    if key not in PALETTES:
        key = DEFAULT_THEME
    global _active
    _active = key
    globals().update(PALETTES[key])
    _apply_derived()
    return key


def active() -> str:
    """Name of the palette currently in use."""
    return _active


def is_theme(name: str) -> bool:
    key = str(name or "").strip().lower().replace("_", "-").replace(" ", "-")
    return _ALIASES.get(key, key) in PALETTES


def description(name: str) -> str:
    return {
        "tokyo-night": "deep indigo, the tinycode classic",
        "tokyo-night-storm": "softer blue-grey variant",
        "catppuccin-mocha": "warm pastels, very popular",
        "catppuccin-macchiato": "a touch lighter than mocha",
        "nord": "cool arctic blues",
        "gruvbox-dark": "retro warm earth tones",
        "dracula": "high contrast purple & pink",
        "one-dark": "the Atom editor classic",
        "kanagawa": "ink-wash Japanese muted tones",
        "everforest": "calm forest greens",
        "solarized-dark": "the precision low-strain classic",
        "ayu-mirage": "warm twilight blues",
        "material-ocean": "deep ocean blues & teals",
    }.get(name, "")


set_theme(DEFAULT_THEME)
