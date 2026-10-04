"""Theme palettes, aliases and persistence."""

import tinycode.config as conf
from tinycode.config import load_config, save_setting
from tinycode.tui import theme as T


def test_palettes_are_complete():
    keys = {"BG", "BG_ALT", "PANEL", "BORDER", "MUTED", "DIM", "TEXT", "TEXT_SOFT",
            "BLUE", "CYAN", "GREEN", "YELLOW", "ORANGE", "RED", "PURPLE", "TEAL",
            "GREEN_BG", "RED_BG", "SYNTAX"}
    assert len(T.NAMES) >= 8
    for name in T.NAMES:
        assert set(T.PALETTES[name]) == keys, name
        assert T.description(name)


def test_set_theme_aliases_and_fallback():
    assert T.set_theme("catppuccin") == "catppuccin-mocha"
    assert T.CYAN == T.PALETTES["catppuccin-mocha"]["CYAN"]
    assert T.set_theme("tokyo") == "tokyo-night"
    assert T.set_theme("GRUVBOX") == "gruvbox-dark"
    assert T.set_theme("nonsense") == T.DEFAULT_THEME
    assert T.active() == T.DEFAULT_THEME
    assert T.is_theme("gruvbox") and not T.is_theme("banana")
    # derived styles track the palette
    assert T.MODE_STYLE["yolo"][0] == T.RED


def test_theme_persists_via_config():
    save_setting("theme", "dracula")
    assert 'theme = "dracula"' in conf.CONFIG_PATH.read_text()
    assert load_config().theme == "dracula"
    # explicit CLI/env overrides win over the saved choice
    assert load_config({"theme": "nord"}).theme == "nord"
    # a saved value that no longer exists falls back cleanly
    save_setting("theme", "does-not-exist")
    assert load_config().theme == T.DEFAULT_THEME
