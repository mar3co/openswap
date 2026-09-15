#!/usr/bin/env bash
# OpenSwap installer for macOS.
#
#   curl -fsSL https://raw.githubusercontent.com/mar3co/openswap/main/install.sh | bash
#
# Installs uv if needed (uv brings its own Python), clones this repo to
# ~/.openswap (or installs from $OPENSWAP_DIR), installs the openswap tool,
# saves the Claude account you are logged into, and starts the menu bar extra.
# Safe to re-run: ~/.openswap is pulled instead of cloned.
#
# Everything lives in main() so a download that stops halfway runs nothing.
set -euo pipefail

REPO_URL="${OPENSWAP_REPO:-https://github.com/mar3co/openswap.git}"
INSTALL_DIR="${OPENSWAP_DIR:-$HOME/.openswap}"
UV_INSTALLER="https://astral.sh/uv/install.sh"

say()  { printf '\033[1m==>\033[0m %s\n' "$*"; }
fail() { printf 'error: %s\n' "$*" >&2; exit 1; }

# .git is a file in a worktree, so -e rather than -d.
is_checkout() { [ -e "$1/.git" ] && [ -f "$1/src/openswap/__init__.py" ]; }

main() {
  [ "$(uname -s)" = "Darwin" ] || fail "OpenSwap ships for macOS only."
  [ "$(id -u)" != 0 ] || fail "Run this installer as your normal user, not with sudo."

  # /usr/bin/git is a shim that opens the Command Line Tools dialog when run,
  # so a plain `command -v git` cannot tell whether git is really there.
  if ! xcode-select -p >/dev/null 2>&1; then
    say "Installing the Xcode Command Line Tools (they provide git)"
    if xcode-select --install >/dev/null; then
      fail "Finish the Command Line Tools dialog, then run this installer again."
    fi
    fail "The Command Line Tools install did not start. Try: sudo xcode-select --reset, then run this installer again."
  fi

  local original_path="$PATH"
  if ! command -v uv >/dev/null 2>&1; then
    say "Installing uv"
    curl -LsSf "$UV_INSTALLER" | sh
    # Same precedence as uv's installer.
    local uv_dir="$HOME/.local/bin"
    if [ -n "${UV_INSTALL_DIR:-}" ]; then uv_dir="$UV_INSTALL_DIR"
    elif [ -n "${XDG_BIN_HOME:-}" ]; then uv_dir="$XDG_BIN_HOME"
    elif [ -n "${XDG_DATA_HOME:-}" ]; then uv_dir="$XDG_DATA_HOME/../bin"
    fi
    export PATH="$uv_dir:$PATH"
    command -v uv >/dev/null 2>&1 || fail "uv is still not on PATH after its installer ran (expected $uv_dir/uv). Install it from https://docs.astral.sh/uv/ and run this installer again."
  fi

  local bin_dir
  bin_dir="$(uv tool dir --bin 2>/dev/null || true)"
  [ -n "$bin_dir" ] || fail "This uv is too old to report its tool directory. Run: uv self update, then run this installer again."

  if is_checkout "$INSTALL_DIR"; then
    if [ -n "${OPENSWAP_DIR:-}" ]; then
      say "Installing from $INSTALL_DIR (not pulling)"
    elif git -C "$INSTALL_DIR" pull --ff-only; then
      say "Updated $INSTALL_DIR"
    else
      say "Could not update $INSTALL_DIR (offline, or local changes). Installing it as-is."
    fi
  elif [ -e "$INSTALL_DIR" ]; then
    fail "$INSTALL_DIR exists but is not an OpenSwap checkout. Move it aside, or set OPENSWAP_DIR to another location."
  else
    say "Cloning OpenSwap into $INSTALL_DIR"
    git clone --quiet "$REPO_URL" "$INSTALL_DIR"
  fi

  say "Installing openswap"
  (cd "$INSTALL_DIR" && uv tool install --force --editable '.[menubar]')
  [ -x "$bin_dir/openswap" ] || fail "uv reported success but $bin_dir/openswap is missing."
  say "Installed $bin_dir/openswap"

  local need_new_shell=0
  case ":$original_path:" in
    *":$bin_dir:"*) ;;
    *)
      need_new_shell=1
      uv tool update-shell || say "Could not update your shell config. Add $bin_dir to PATH yourself."
      ;;
  esac

  say "Saving your Claude account and starting the menu bar extra"
  local status=0
  "$bin_dir/openswap" setup || status=$?

  [ "$need_new_shell" = 0 ] || say "Open a new terminal to use the openswap command."
  return "$status"
}

main "$@"
