<div align="center">

```
▀█▀ █ █▄ █ █▄█ █▀▀ █▀█ █▀▄ █▀▀
 █  █ █ ▀█  █  █▄▄ █▄█ █▄▀ ██▄
```

**A fast, reliable, fully local coding agent for your terminal.**

Talk to it in plain language. It reads your code, edits files, runs commands and tests, and **proves its work runs** before it says it's done.
It runs on your machine through [Ollama](https://ollama.com) with **Ling-3.0-tiny**, and nothing leaves your computer.

</div>

![tinycode](docs/screenshot.png)

## Install (one command)

```bash
curl -fsSL https://raw.githubusercontent.com/the-priest/tinycode/main/install.sh | bash
```

The installer does everything and asks before anything that needs `sudo`:

| | |
|---|---|
| ✓ Python 3.9+ and venv | installed if missing (apt / dnf / pacman / zypper / apk / brew) |
| ✓ tinycode | in its own virtualenv, with a `tinycode` command in `~/.local/bin` |
| ✓ Ollama | official installer if missing. It also offers to switch Ollama from always-on to on-demand. |
| ✓ the model | Ling-3.0-tiny, about 5.3 GB, downloaded once with a progress bar |
| ✓ extras | Node.js to run and test the web apps it builds, ripgrep for fast search, an app-menu launcher, a default config, and a `doctor` self-check |

Options: `-y` (no questions), `--no-model`, `--no-ollama`, `--model TAG`, `--uninstall [--purge]`.
From a clone, run `./install.sh`.

## Use

```bash
cd ~/code/my-project
tinycode
```

Then just ask:

```
❯ the tests in test_api.py fail, find out why and fix it
❯ add a --verbose flag to cli.py and document it in the README
❯ explain how @src/auth.py handles token refresh
❯ !git status
```

| Command | What it does |
|---|---|
| `tinycode [DIR]` | interactive TUI in DIR (default: current directory) |
| `tinycode -c` | continue the last conversation in this directory |
| `tinycode -p "…"` | headless: run the task, print the answer (scripts, CI, pipes) |
| `tinycode --yolo` | auto-approve everything (or `--mode auto-edit`) |
| `tinycode --no-think` | turn off reasoning for faster replies |
| `tinycode doctor` | check Python, Ollama, the model, tool support and RAM |
| `tinycode update` | upgrade in place |

## What makes it good

**It checks that the code actually works, not just that it looks right.** Small models write code that looks plausible and doesn't run. tinycode closes that gap with automatic checks that need no extra model time and feed exact errors back to the model:

1. **After every edit:** a fast syntax and bug check on that file (table below).
2. **Web apps get run.** Any HTML page the model builds or changes is loaded with its scripts in a small simulated browser. It needs only Node.js, with no browser or download. tinycode clicks every button, types into inputs, presses keys, runs the game loop, and reports:
   - crashes with file and line, e.g. `clicking "=": TypeError … script.js line 42`, plus a hint like "getElementById('displayy') returned null"
   - broken wiring: `onclick` calling a function that doesn't exist, ids the JavaScript looks up that the HTML doesn't have, `<script src>` files that are missing
   - invalid output such as `NaN`, `undefined` or `[object Object]` appearing on the page
   - wrong behaviour in common apps: a calculator really gets `2 + 3 =` and must show 5, and a todo app must show the item you add
3. **Your tests get run.** If code changed and the project has tests (pytest, npm test, cargo test, go test…), they run before the model is allowed to finish, and failures go back to it.
4. **The model can test its own work** with the `test_app` tool, writing its own scenario: click 7, ×, 6, = and expect the display to show 42.
5. **Finish gate:** when the model says it's done, all of the above runs on what it changed. Anything broken goes back to it to fix, for a few rounds at most. Problems that were already there before its change are left out, so it isn't pushed into edits nobody asked for.

**Catches its own bugs while it works.** Every time the model writes or edits a file, tinycode runs a fast local check on it and puts the result straight into the tool output, with the line number and the offending line, so the model fixes it in its next step:

| Language | Check |
|---|---|
| Python | syntax (built-in compiler), undefined names and similar real bugs (`ruff`, or `pyflakes`, if installed) |
| JavaScript | `node --check` |
| HTML | unclosed and mismatched tags, plus `node --check` on every inline `<script>`, using the file's own line numbers |
| TypeScript | syntax errors (`tsc`) |
| CSS · JSON · TOML · YAML | braces, comments and parse errors |
| Shell · C/C++ · Go · PHP · Ruby | `bash -n`, `gcc -fsyntax-only`, `gofmt -e`, `php -l`, `ruby -c` |

A file that is still being written in chunks is reported as "unfinished, keep going", not as an error. When the model says it's done, every file it changed is checked again. If anything is still broken, the errors go back to it to fix, for a few rounds at most. The sidebar's **PROBLEMS** panel shows what's open, and the model can also run the `check` tool itself on a file or the whole project. A checker whose tool isn't installed is skipped. tinycode also detects the project type and its test command (pytest, npm test, cargo test, go test…) and tells the model.

**Built for a small local model.** Small models make small mistakes. tinycode catches them so a task doesn't fail on one bad tool call:

- **Forgiving edits.** If `old_string` doesn't match exactly, edits still land when the only problem is indentation, whitespace or line numbers copied from `read_file`. The fix is re-indented to fit the file. An edit that is ambiguous or can't be found is never guessed. Instead the model gets back the closest matching region of the file so it can try again.
- **Tool-call repair.** Misspelled tool names (`run_bash`, `str_replace`, `cat`) and argument names (`file_path`, `cmd`, `old`) are mapped to the right ones, and values are converted to the right types. Tool calls the model writes as text (`<tool_call>{…}`, JSON blocks) are recovered.
- **No runaway output.** Repetition loops are detected while the model is still writing and cut off, and each step has a cap on reasoning length. The step is then retried with a focused prompt. Repeating the exact same tool call gets a nudge.
- **Self-correcting.** Errors go back to the model with a hint (for example "Did you mean src/util.py?") instead of crashing.
- **Context that doesn't overflow.** Old tool output is pruned automatically as the context window fills, the token estimate is calibrated against the model's real token counts, and `/compact` summarizes the conversation on demand.
- **Fast.** The system prompt stays fixed for the whole session, so Ollama can reuse its cache between steps. Reasoning can be switched off with `ctrl+t`. The only dependency is Textual.

**You see the work.** The model says in one line what it's doing before each step, and it writes files in chunks of about 80 lines, so you watch a file grow instead of waiting for all of it. Edits show a side-by-side before/after diff, new files show a syntax-highlighted preview, and command output streams live. While Ollama holds back a tool call that is still being written, such as a whole file, the status line shows how many tokens have been generated so far. An experimental `tool_mode = "stream"` setting shows files as they are written, but it depends on the model using a tool-call format tinycode can read.

**Safe by default.**
- Every file edit shows a coloured diff before it is applied. You can answer *Yes*, *Yes, don't ask again*, or *No, and tell it what to do instead*.
- Read-only commands (`ls`, `cat`, `git status`, `grep`, …) run without asking.
- Dangerous commands (`rm -rf ~`, `sudo`, `git push --force`, `curl | sh`, …) always ask, even in yolo mode. So do writes outside the project.
- `/undo` reverts all the file changes from the last turn.

**Nice to use.**
- Streaming markdown answers, collapsible reasoning, and live command output.
- A sidebar with the plan, a context gauge, token speed, and the files changed (`+12 -3`).
- `@file` to attach files (with autocomplete), `!cmd` to run a shell command yourself, `/` for the command menu, message history with ↑/↓, and queued messages while the agent is working.
- Sessions are saved automatically; resume one with `/resume` or `tinycode -c`.
- Project memory: tinycode reads `TINYCODE.md` (or `AGENTS.md` / `CLAUDE.md`). Run `/init` to generate one for your project.

**Ollama comes and goes with the app.** tinycode starts `ollama serve` when it launches (or adopts one that is already running), loads the model, and waits until it is in memory. On exit it unloads the model and stops the server, so the RAM is freed. This also happens on `ctrl+c`, when you close the terminal window, on SIGTERM, and on a crash. A server tinycode started is tied to it with `PR_SET_PDEATHSIG`, so the kernel stops it even if tinycode is `kill -9`ed.

## Keys and commands

| Key | | Command | |
|---|---|---|---|
| `enter` | send | `/help` | all commands and keys |
| `shift+enter` / `ctrl+j` / `\`+enter | new line | `/new` | fresh conversation |
| `esc` | interrupt the agent | `/resume` | pick an earlier conversation |
| `shift+tab` | mode: ask → auto-edit → yolo | `/undo` | revert the last turn's file changes |
| `ctrl+t` | reasoning on/off | `/compact` | summarize to free context |
| `ctrl+o` | expand tool output and reasoning | `/init` | write a TINYCODE.md for this project |
| `ctrl+b` | sidebar | `/diff` | files changed this session |
| `ctrl+n` | new conversation | `/model` | switch model |
| `ctrl+l` | clear screen | `/cost` | token usage |
| `ctrl+c` ×2 / `ctrl+q` | quit | `/export` | save the conversation as markdown |

## Tools the model can use

`read_file` · `edit_file` · `write_file` · `bash` · `test_app` · `check` · `glob` · `grep` · `list_dir` · `todowrite` · `fetch_url`

`glob`, `grep` and `list_dir` skip `.git`, `node_modules`, virtualenvs and build folders. `grep` uses ripgrep when it is installed.

## Configuration

`tinycode config` creates `~/.config/tinycode/config.toml`:

```toml
[model]
model = "hf.co/bloomer010/Ling-3.0-tiny-GGUF:Q4_K_XL"
num_ctx = 32768        # lower if you're short on RAM
num_predict = 16384
temperature = 0.6
think = true           # reasoning: slower but more accurate
tool_mode = "native"   # native: Ollama tool calling · stream: experimental live view

[agent]
mode = "ask"           # ask | auto-edit | yolo
max_steps = 40
auto_check = true     # check every edited file, and again before finishing
app_check = true      # run changed web pages in a simulated browser before finishing
run_tests = true      # run the project's tests before finishing, when code changed

[ollama]
manage_server = true
stop_server_on_exit = true
unload_on_exit = true
```

Every key can also be set with an environment variable, for example `TINYCODE_NUM_CTX=32768` or `TINYCODE_MODE=auto-edit`. `OLLAMA_HOST` is respected. Personal instructions for every project go in `~/.config/tinycode/TINYCODE.md`.

Any Ollama model with tool support works: `tinycode --model qwen3:8b`.

## The model

**Ling-3.0-tiny** by InclusionAI (MIT licence) is a sparse mixture-of-experts model with about 7.9B total and 1.3B active parameters, quantized to Q4_K_XL (about 5.3 GB). It supports tool calling and reasoning in Ollama. It runs on a CPU, faster with a GPU, and needs 8 GB of RAM or more.

## Troubleshooting

- Run `tinycode doctor` first.
- The log is at `~/.cache/tinycode/tinycode.log`.
- **`ollama` not found**: re-run the installer, or install it from https://ollama.com/download.
- **Out of memory**: lower `num_ctx` in the config, or close other models (`ollama ps`).
- **Slow replies**: press `ctrl+t` to turn reasoning off.
- **Uninstall**: `curl -fsSL https://raw.githubusercontent.com/the-priest/tinycode/main/install.sh | bash -s -- --uninstall`

## Development

```bash
git clone https://github.com/the-priest/tinycode && cd tinycode
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest -q        # the tests use a fake Ollama server, so no model is needed
.venv/bin/tinycode --no-boot         # try the UI without Ollama
```

MIT licensed.
