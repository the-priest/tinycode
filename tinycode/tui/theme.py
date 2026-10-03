"""Colour palette (Tokyo Night inspired)."""

BG = "#0f1117"
BG_ALT = "#13151d"
PANEL = "#161821"
BORDER = "#2a2e42"
MUTED = "#565f89"
DIM = "#3b4261"
TEXT = "#c0caf5"
TEXT_SOFT = "#a9b1d6"
BLUE = "#7aa2f7"
CYAN = "#7dcfff"
GREEN = "#9ece6a"
YELLOW = "#e0af68"
ORANGE = "#ff9e64"
RED = "#f7768e"
PURPLE = "#bb9af7"
TEAL = "#73daca"

TOOL_LABELS = {
    "read_file": "Read", "write_file": "Write", "edit_file": "Edit", "bash": "Bash",
    "glob": "Glob", "grep": "Grep", "list_dir": "List", "todowrite": "Plan",
    "fetch_url": "Fetch",
}

MODE_STYLE = {
    "ask": (MUTED, "ask before edits & commands"),
    "auto-edit": (PURPLE, "auto-accept edits"),
    "yolo": (RED, "yolo — everything auto-approved"),
}
