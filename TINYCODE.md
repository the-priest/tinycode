# tinycode — project guide

A fully local terminal coding agent (Python + Textual, talks to Ollama).

## Commands
- Dev install: `python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"`
- Run: `.venv/bin/tinycode [DIR]` (`--no-boot` to try the UI without Ollama)
- Tests: `.venv/bin/python -m pytest -q`; one test: `pytest tests/test_edits.py::test_exact`
- Installer check: `bash -n install.sh` (its preset map must match `models.PRESETS`; a test checks this)

## Layout
- `tinycode/agent.py` — the agent loop (UI-agnostic, `AgentUI` callbacks)
- `tinycode/tools.py` — tool implementations, schemas, permission rules, and the project sandbox (every path is confined to the workdir; `escape_reason` blocks shell escapes)
- `tinycode/edits.py` — forgiving `edit_file` matcher + diffs
- `tinycode/checks.py` — per-language syntax/error checkers run after every edit and before finishing
- `tinycode/webcheck.py` + `tinycode/assets/domsim.js` — runs web pages in a simulated browser (Node vm + fake DOM): clicks, keys, probes, model-written scenarios
- `tinycode/parsing.py` — tool-call normalization, text tool-call recovery, loop detection
- `tinycode/context.py` — system prompt, project memory, context pruning, @mentions
- `tinycode/ollama.py` — Ollama HTTP client and server/model lifecycle
- `tinycode/models.py` — model presets (incl. unrestricted builds) and per-family sampling
- `tinycode/config.py` — `Config`, `load_config` (file → env → CLI), `save_setting` (used by the ctrl+p settings)
- `tinycode/tui/` — Textual app (`app.py`), widgets/modals incl. the ctrl+p `Palette` (`widgets.py`), colours (`theme.py`)
- `tinycode/cli.py` — argument parsing, headless `-p` mode, `doctor`, `models`
- `tests/fake_ollama.py` — scripted fake Ollama server used by the tests

## Style
- Standard library only, except `textual` (keeps installs small and fast).
- `from __future__ import annotations`; keep Python 3.9 compatible.
- Tools never raise: they return `ToolResult(ok=False, output="ERROR: ...")` so the model can recover.
