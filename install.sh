#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
#  tinycode installer — everything in one go:
#
#    curl -fsSL https://raw.githubusercontent.com/the-priest/tinycode/main/install.sh | bash
#
#  or from a clone:   ./install.sh
#
#  It installs (asking first for anything that needs sudo):
#    • Python 3.9+ and venv support      (if missing)
#    • tinycode in its own virtualenv    ~/.local/share/tinycode/venv
#    • the `tinycode` command            ~/.local/bin/tinycode
#    • Ollama                            (if missing)
#    • a model of your choice            (Ling-3.0-tiny by default, ~5 GB, one time)
#    • ripgrep (fast search), a desktop launcher, a default config
#
#  Options:
#    -y, --yes          accept all defaults, no questions
#    --no-model         don't download the model now (tinycode will on first run)
#    --no-ollama        don't install Ollama
#    --no-desktop       don't create the app-menu launcher
#    --model NAME       a preset (ling, qwen4b, qwen9b, ling-uncensored,
#                       qwen4b-uncensored, qwen9b-uncensored, lfm) or any Ollama tag
#    --uninstall        remove tinycode (keeps your config + sessions)
#    --purge            with --uninstall: also delete config + sessions
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

REPO="the-priest/tinycode"
REF="${TINYCODE_REF:-main}"
MODEL="${TINYCODE_MODEL:-}"   # empty = ask (default: ling)
PREFIX="${TINYCODE_HOME:-$HOME/.local/share/tinycode}"
VENV="$PREFIX/venv"
BIN_DIR="$HOME/.local/bin"
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/tinycode"

YES=0; WANT_MODEL=1; WANT_OLLAMA=1; WANT_DESKTOP=1; UNINSTALL=0; PURGE=0
while [ $# -gt 0 ]; do
  case "$1" in
    -y|--yes) YES=1 ;;
    --no-model) WANT_MODEL=0 ;;
    --no-ollama) WANT_OLLAMA=0 ;;
    --no-desktop) WANT_DESKTOP=0 ;;
    --model) shift; MODEL="${1:?--model needs a name}" ;;
    --uninstall) UNINSTALL=1 ;;
    --purge) PURGE=1 ;;
    -h|--help) sed -n '2,26p' "$0" 2>/dev/null | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done

# ── pretty output ────────────────────────────────────────────────────────────
if [ -t 2 ]; then
  B=$'\033[1m'; D=$'\033[2m'; R=$'\033[31m'; G=$'\033[32m'; Y=$'\033[33m'
  BL=$'\033[34m'; C=$'\033[36m'; N=$'\033[0m'
else B=; D=; R=; G=; Y=; BL=; C=; N=; fi
step() { printf '\n%s▸ %s%s\n' "$BL$B" "$*" "$N" >&2; }
ok()   { printf '  %s✓%s %s\n' "$G" "$N" "$*" >&2; }
warn() { printf '  %s!%s %s\n' "$Y" "$N" "$*" >&2; }
info() { printf '  %s%s%s\n' "$D" "$*" "$N" >&2; }
die()  { printf '\n%s✗ %s%s\n' "$R$B" "$*" "$N" >&2; exit 1; }

# read answers from the terminal even when piped through `curl | bash`
ask() {  # ask "question" default(y/n)
  local q="$1" def="${2:-y}" ans hint
  [ "$def" = y ] && hint="Y/n" || hint="y/N"
  if [ "$YES" = 1 ] || ! { : </dev/tty; } 2>/dev/null; then
    [ "$def" = y ]; return
  fi
  printf '  %s?%s %s [%s] ' "$C" "$N" "$q" "$hint" >&2
  read -r ans </dev/tty || ans=""
  ans="${ans:-$def}"
  case "$ans" in [Yy]*) return 0 ;; *) return 1 ;; esac
}

have() { command -v "$1" >/dev/null 2>&1; }

SUDO=""
if [ "$(id -u)" -ne 0 ]; then
  have sudo && SUDO="sudo"
fi

OS="$(uname -s)"
PM=""
for pm in apt-get dnf yum pacman zypper apk brew; do
  if have "$pm"; then PM="$pm"; break; fi
done

pkg_install() {  # pkg_install <generic-name>...
  [ -n "$PM" ] || return 1
  case "$PM" in
    apt-get) $SUDO apt-get update -qq && $SUDO DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "$@" ;;
    dnf|yum) $SUDO "$PM" install -y -q "$@" ;;
    pacman)  $SUDO pacman -S --noconfirm --needed "$@" ;;
    zypper)  $SUDO zypper --non-interactive install "$@" ;;
    apk)     $SUDO apk add "$@" ;;
    brew)    brew install "$@" ;;
  esac
}

banner() {
  printf '%s' "$BL$B" >&2
  cat >&2 <<'EOF'

  ▀█▀ █ █▄ █ █▄█ █▀▀ █▀█ █▀▄ █▀▀
   █  █ █ ▀█  █  █▄▄ █▄█ █▄▀ ██▄
EOF
  printf '%s  a fast, fully local coding agent%s\n' "$D" "$N" >&2
}

# ── uninstall ────────────────────────────────────────────────────────────────
if [ "$UNINSTALL" = 1 ]; then
  banner
  step "Removing tinycode"
  rm -rf "$VENV" && ok "removed $VENV"
  [ -L "$BIN_DIR/tinycode" ] || [ -f "$BIN_DIR/tinycode" ] && rm -f "$BIN_DIR/tinycode" && ok "removed $BIN_DIR/tinycode"
  rm -f "$HOME/.local/share/applications/tinycode.desktop" 2>/dev/null || true
  if [ "$PURGE" = 1 ]; then
    rm -rf "$CONFIG_DIR" "$PREFIX" "${XDG_CACHE_HOME:-$HOME/.cache}/tinycode"
    ok "removed config, sessions and logs"
  else
    info "kept config ($CONFIG_DIR) and sessions ($PREFIX/sessions); use --purge to delete"
  fi
  info "Ollama and downloaded models were left in place (see: ollama list / ollama rm NAME)"
  exit 0
fi

banner

# ── 0. pick a model ──────────────────────────────────────────────────────────
# preset key → Ollama tag and download size (same presets as `tinycode models`)
model_tag() {
  case "$1" in
    ling)              echo "hf.co/bloomer010/Ling-3.0-tiny-GGUF:Q4_K_XL" ;;
    ling-official)     echo "hf.co/inclusionAI/Ling-3.0-tiny-GGUF:Q4_K_M" ;;
    qwen4b)            echo "qwen3.5:4b" ;;
    qwen9b)            echo "qwen3.5:9b" ;;
    lfm)               echo "hf.co/LiquidAI/LFM2.5-8B-A1B-GGUF:Q4_K_M" ;;
    ling-uncensored)   echo "hf.co/mradermacher/Ling-3.0-tiny-uncensored-abliterated-GGUF:Q4_K_M" ;;
    qwen4b-uncensored) echo "huihui_ai/qwen3.5-abliterated:4b" ;;
    qwen9b-uncensored) echo "huihui_ai/qwen3.5-abliterated:9b" ;;
    *)                 echo "$1" ;;
  esac
}
model_size() {
  case "$1" in
    qwen4b*) echo "~3.4 GB" ;; qwen9b*) echo "~6.6 GB" ;; ling) echo "~5.3 GB" ;;
    ling*|lfm) echo "~5 GB" ;; *) echo "a few GB" ;;
  esac
}

CHOSEN=0
if [ -z "$MODEL" ]; then
  MODEL=ling
  if [ "$YES" != 1 ] && { : </dev/tty; } 2>/dev/null; then
    step "Choose a model (switch any time in the app with ctrl+p)"
    printf '\n' >&2
    printf '   %s1%s  Ling-3.0-tiny          %sfastest · 5.3 GB · good tool use · default%s\n' "$B" "$N" "$D" "$N" >&2
    printf '   %s2%s  Qwen3.5-4B             %sbetter at code · 3.4 GB · ~3x slower%s\n' "$B" "$N" "$D" "$N" >&2
    printf '   %s3%s  Qwen3.5-9B             %sbest coder under 10B · 6.6 GB · ~6x slower · 16 GB RAM%s\n' "$B" "$N" "$D" "$N" >&2
    printf '\n   %sunrestricted (refusals removed):%s\n' "$D" "$N" >&2
    printf '   %s4%s  Ling-3.0-tiny          %sfastest · ~5 GB%s\n' "$B" "$N" "$D" "$N" >&2
    printf '   %s5%s  Qwen3.5-4B             %sbetter at code · 3.4 GB%s\n' "$B" "$N" "$D" "$N" >&2
    printf '   %s6%s  Qwen3.5-9B             %sbest coder · 6.6 GB%s\n\n' "$B" "$N" "$D" "$N" >&2
    printf '  %s?%s Model [1]: ' "$C" "$N" >&2
    read -r pick </dev/tty || pick=""
    case "${pick:-1}" in
      2) MODEL=qwen4b ;; 3) MODEL=qwen9b ;; 4) MODEL=ling-uncensored ;;
      5) MODEL=qwen4b-uncensored ;; 6) MODEL=qwen9b-uncensored ;; *) MODEL=ling ;;
    esac
    CHOSEN=1
  fi
else
  CHOSEN=1
fi
MODEL_TAG="$(model_tag "$MODEL")"

# ── 1. Python ────────────────────────────────────────────────────────────────
step "Python"
PY=""
for cand in python3.13 python3.12 python3.11 python3.10 python3.9 python3; do
  if have "$cand" && "$cand" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null; then
    PY="$(command -v "$cand")"; break
  fi
done
if [ -z "$PY" ]; then
  warn "Python 3.9+ not found"
  if ask "Install Python 3 with $PM?" y; then
    case "$PM" in
      apt-get) pkg_install python3 python3-venv python3-pip ;;
      pacman) pkg_install python python-pip ;;
      brew) pkg_install python ;;
      *) pkg_install python3 python3-pip ;;
    esac || die "could not install Python — please install Python 3.9+ and re-run"
    PY="$(command -v python3)"
  else
    die "tinycode needs Python 3.9+"
  fi
fi
ok "$("$PY" --version 2>&1) ($PY)"

# venv support (Debian/Ubuntu ship Python without it)
if ! "$PY" -c 'import venv, ensurepip' >/dev/null 2>&1; then
  warn "Python venv support is missing"
  PYV="$("$PY" -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")')"
  if [ "$PM" = apt-get ] && ask "Install python$PYV-venv (needs sudo)?" y; then
    pkg_install "python$PYV-venv" || pkg_install python3-venv || die "could not install venv support"
  elif [ "$PM" != apt-get ]; then
    pkg_install python3-virtualenv 2>/dev/null || true
  else
    die "please install python3-venv and re-run"
  fi
fi

# ── 2. tinycode itself ───────────────────────────────────────────────────────
step "Installing tinycode"
mkdir -p "$PREFIX" "$BIN_DIR"
if [ -x "$VENV/bin/python" ] && "$VENV/bin/python" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null; then
  info "reusing virtualenv $VENV"
else
  rm -rf "$VENV"
  "$PY" -m venv "$VENV" || die "could not create a virtualenv at $VENV"
fi
"$VENV/bin/python" -m pip install -q --upgrade pip >/dev/null 2>&1 || true

SRC=""
SCRIPT_DIR=""
if [ -n "${BASH_SOURCE[0]:-}" ] && [ -f "${BASH_SOURCE[0]}" ]; then
  SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
fi
if [ -n "$SCRIPT_DIR" ] && [ -f "$SCRIPT_DIR/pyproject.toml" ] && grep -q 'name = "tinycode"' "$SCRIPT_DIR/pyproject.toml"; then
  SRC="$SCRIPT_DIR"
  info "installing from local checkout $SRC"
else
  SRC="https://github.com/$REPO/archive/refs/heads/$REF.tar.gz"
  info "installing from $SRC"
fi
"$VENV/bin/python" -m pip install -q --upgrade --disable-pip-version-check "$SRC" \
  || die "pip install failed (check your internet connection)"
ln -sf "$VENV/bin/tinycode" "$BIN_DIR/tinycode"
ok "tinycode $("$VENV/bin/tinycode" --version | awk '{print $2}') → $BIN_DIR/tinycode"

# PATH
case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *)
    LINE='export PATH="$HOME/.local/bin:$PATH"'
    for rc in "$HOME/.bashrc" "$HOME/.zshrc" "$HOME/.profile"; do
      if [ -f "$rc" ] && ! grep -qs '.local/bin' "$rc"; then
        printf '\n# added by tinycode installer\n%s\n' "$LINE" >> "$rc"
        info "added ~/.local/bin to PATH in $(basename "$rc")"
      fi
    done
    if [ -d "$HOME/.config/fish" ] && ! grep -qs '.local/bin' "$HOME/.config/fish/config.fish" 2>/dev/null; then
      mkdir -p "$HOME/.config/fish"
      echo 'fish_add_path $HOME/.local/bin' >> "$HOME/.config/fish/config.fish"
    fi
    NEED_NEW_SHELL=1
    ;;
esac

# config: create the default one; save the model if one was picked
if [ ! -f "$CONFIG_DIR/config.toml" ]; then
  "$VENV/bin/tinycode" config >/dev/null
  ok "config → $CONFIG_DIR/config.toml"
fi
if [ "$CHOSEN" = 1 ]; then
  "$VENV/bin/tinycode" models use "$MODEL" >/dev/null && ok "default model: $MODEL"
fi

# ── 3. ripgrep (optional, makes search fast) ─────────────────────────────────
if ! have rg && [ -n "$PM" ]; then
  step "ripgrep (optional, faster code search)"
  if ask "Install ripgrep?" y; then
    pkg_install ripgrep >/dev/null 2>&1 && ok "ripgrep installed" || warn "skipped (tinycode falls back to Python search)"
  fi
fi

# ── 3b. Node.js (optional: runs web apps the model builds + checks JavaScript) ─
if ! have node && [ -n "$PM" ]; then
  step "Node.js (optional — lets tinycode run and test the web apps it builds)"
  if ask "Install Node.js?" y; then
    case "$PM" in
      pacman) pkg_install nodejs ;;
      brew) pkg_install node ;;
      *) pkg_install nodejs ;;
    esac >/dev/null 2>&1 && ok "Node.js installed" || warn "skipped (app testing and JS checks will be off)"
  fi
fi

# ── 4. Ollama ────────────────────────────────────────────────────────────────
OLLAMA=""
find_ollama() {
  for c in "$(command -v ollama 2>/dev/null || true)" /usr/local/bin/ollama /usr/bin/ollama \
           /opt/homebrew/bin/ollama /Applications/Ollama.app/Contents/Resources/ollama; do
    [ -n "$c" ] && [ -x "$c" ] && { OLLAMA="$c"; return 0; }
  done
  return 1
}
step "Ollama"
if find_ollama; then
  ok "$("$OLLAMA" --version 2>/dev/null | grep -o '[0-9][0-9.]*' | head -1 | sed 's/^/ollama /') ($OLLAMA)"
elif [ "$WANT_OLLAMA" = 1 ]; then
  if ask "Ollama is not installed. Install it now (official installer)?" y; then
    if [ "$OS" = Darwin ]; then
      if have brew; then brew install ollama; else die "install Ollama from https://ollama.com/download and re-run"; fi
    else
      curl -fsSL https://ollama.com/install.sh | sh || die "Ollama install failed"
    fi
    find_ollama || die "Ollama installed but not found on PATH"
    ok "Ollama installed"
  else
    warn "skipped — install it later from https://ollama.com/download"
  fi
else
  info "skipped (--no-ollama)"
fi

# tinycode starts/stops Ollama itself; an always-on system service just eats RAM
if [ -n "$OLLAMA" ] && [ "$OS" = Linux ] && have systemctl \
   && systemctl is-enabled ollama >/dev/null 2>&1; then
  info "Ollama is set up as an always-on system service."
  info "tinycode can start it on launch and stop it on exit instead, so the model"
  info "only uses RAM while you're coding."
  if ask "Let tinycode manage Ollama (disable the always-on service)?" y; then
    $SUDO systemctl disable --now ollama >/dev/null 2>&1 && ok "service disabled — tinycode will run Ollama on demand" \
      || warn "could not disable the service (tinycode still works with it)"
  fi
fi

# ── 5. the model ─────────────────────────────────────────────────────────────
if [ -n "$OLLAMA" ] && [ "$WANT_MODEL" = 1 ]; then
  step "Model: $MODEL_TAG"
  STARTED=""
  if ! curl -fsS --max-time 2 http://127.0.0.1:11434/api/version >/dev/null 2>&1; then
    "$OLLAMA" serve >/dev/null 2>&1 &
    STARTED=$!
    for _ in $(seq 1 50); do
      curl -fsS --max-time 1 http://127.0.0.1:11434/api/version >/dev/null 2>&1 && break
      sleep 0.2
    done
  fi
  if "$OLLAMA" list 2>/dev/null | awk '{print $1}' | grep -qxF "$MODEL_TAG"; then
    ok "already downloaded"
  elif ask "Download the model now ($(model_size "$MODEL"), one time)?" y; then
    "$OLLAMA" pull "$MODEL_TAG" && ok "model ready" || warn "download failed — tinycode will retry on first run"
  else
    info "tinycode will download it on first run"
  fi
  if [ -n "$STARTED" ]; then kill "$STARTED" 2>/dev/null || true; wait "$STARTED" 2>/dev/null || true; fi
fi

# ── 6. desktop launcher ──────────────────────────────────────────────────────
if [ "$OS" = Linux ] && [ "$WANT_DESKTOP" = 1 ]; then
  APPS="$HOME/.local/share/applications"
  mkdir -p "$APPS"
  cat > "$APPS/tinycode.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=tinycode
GenericName=Local coding agent
Comment=Fast, fully local AI coding agent (Ollama)
Exec=$BIN_DIR/tinycode
Path=$HOME
Terminal=true
Categories=Development;Utility;
Keywords=ai;code;agent;ollama;llm;
Icon=utilities-terminal
EOF
  ok "app-menu launcher created"
fi

# ── 7. check ─────────────────────────────────────────────────────────────────
step "Checking the installation"
"$VENV/bin/tinycode" doctor || true

printf '\n%s✓ tinycode is installed!%s\n\n' "$G$B" "$N" >&2
printf '  %scd your-project && tinycode%s      start coding\n' "$B" "$N" >&2
printf '  %stinycode -p "explain this repo"%s  one-shot, prints the answer\n' "$B" "$N" >&2
printf '  %sctrl+p%s inside the app            settings, switch model, unrestricted mode\n' "$B" "$N" >&2
printf '  %stinycode models%s                  recommended models\n' "$B" "$N" >&2
printf '  %stinycode doctor%s                  diagnose problems\n' "$B" "$N" >&2
printf '  %stinycode update%s                  upgrade\n\n' "$B" "$N" >&2
if [ "${NEED_NEW_SHELL:-0}" = 1 ]; then
  printf '  %sopen a new terminal (or run: export PATH="$HOME/.local/bin:$PATH") first%s\n\n' "$Y" "$N" >&2
fi
