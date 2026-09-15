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
# Everything lives in main(), invoked inside a group whose closing brace is
# the last token, so a download that stops anywhere short of that runs nothing.
set -euo pipefail

REPO_URL="${OPENSWAP_REPO:-https://github.com/mar3co/openswap.git}"
INSTALL_DIR="${OPENSWAP_DIR:-$HOME/.openswap}"
UV_INSTALLER="https://astral.sh/uv/install.sh"

say()  { printf '\033[1m==>\033[0m %s\n' "$*"; }
warn() { printf 'warning: %s\n' "$*" >&2; }
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
    fail "The Command Line Tools install did not start. If a download is already running, wait for it to finish; otherwise try: sudo xcode-select --reset. Then run this installer again."
  fi

  local original_path="$PATH"
  if ! command -v uv >/dev/null 2>&1; then
    say "Installing uv"
    curl -LsSf "$UV_INSTALLER" | sh || fail "The uv installer failed (see above). Install uv from https://docs.astral.sh/uv/ and run this installer again."
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
  bin_dir="$(uv tool dir --bin || true)"
  [ -n "$bin_dir" ] || fail "uv could not report its tool directory (see above). Update uv with: uv self update, or through whatever installed it, then run this installer again."

  local update_note=""
  if is_checkout "$INSTALL_DIR"; then
    if [ -n "${OPENSWAP_DIR:-}" ]; then
      say "Installing from $INSTALL_DIR (not pulling)"
    elif git -C "$INSTALL_DIR" pull --ff-only; then
      say "Updated $INSTALL_DIR"
    else
      update_note="$INSTALL_DIR was NOT updated (see git's message above); the existing checkout was reinstalled as-is."
      warn "$update_note"
    fi
  elif [ -e "$INSTALL_DIR" ]; then
    fail "$INSTALL_DIR exists but is not an OpenSwap checkout. If it is a leftover from an interrupted install, delete it; otherwise move it aside or set OPENSWAP_DIR to another location."
  else
    say "Cloning OpenSwap into $INSTALL_DIR"
    git clone --quiet "$REPO_URL" "$INSTALL_DIR" || fail "Cloning $REPO_URL failed (see above). Check your network, then run this installer again."
  fi

  say "Installing openswap"
  (cd "$INSTALL_DIR" && uv tool install --force --editable '.[menubar]') || fail "Installing the openswap tool failed (see above). Fix that, then run this installer again; until then the openswap command may be out of step with $INSTALL_DIR."
  [ -x "$bin_dir/openswap" ] || fail "uv reported success but $bin_dir/openswap is missing."
  say "Installed $bin_dir/openswap"

  local path_note=""
  case ":$original_path:" in
    *":$bin_dir:"*) ;;
    *)
      if uv tool update-shell; then
        path_note="Open a new terminal to use the openswap command."
      else
        path_note="Add $bin_dir to your PATH to use the openswap command (the shell config update failed, see above)."
      fi
      ;;
  esac

  say "Saving your Claude account and starting the menu bar extra"
  local status=0
  "$bin_dir/openswap" setup || status=$?

  [ -z "$update_note" ] || warn "$update_note"
  [ -z "$path_note" ] || say "$path_note"
  return "$status"
}

{
  main "$@"
  exit
}
