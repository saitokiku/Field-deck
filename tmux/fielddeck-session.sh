#!/usr/bin/env bash
#
# FieldDeck tmux session bootstrap.
#
# Guarantees four windows, always in the same places, because those numbers are
# the device's permanent navigation:
#
#   1 HMI      the Textual panel      (fielddeck-ui)
#   2 CLAUDE   Claude Code in the repo
#   3 SHELL    a plain shell
#   4 LOG      journalctl -fu instrumentd
#
# Two invariants drive everything below:
#
#   * The session must never die. Not when the HMI crashes, not when Claude is
#     not installed, not when there is no journal to follow. A tmux session that
#     exits takes the panel with it and leaves an operator looking at a bare
#     console with no way back.
#   * Window indices and names must never move. They are touch targets on the
#     status line and they are in the documentation.
#
# Re-running is how you repair a session: existing windows are left alone, and
# any that are missing or dead are put back.
#
# Also useful over SSH — 'fielddeck-session' gives you the same four windows on
# a laptop as the panel has on the bench.

set -euo pipefail

SESSION="fielddeck"
SELF="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)/$(basename "${BASH_SOURCE[0]}")"
INSTALL_ENV="/etc/fielddeck/install.env"

# install.env is read key by key rather than sourced. It is a file written by
# root, but a bootstrap that executes its configuration is a bootstrap that can
# be turned into something else entirely by one bad edit.
fd_env() {
  local key="$1" default="${2:-}" value=""
  if [[ -r "$INSTALL_ENV" ]]; then
    value="$(sed -n "s/^${key}=//p" "$INSTALL_ENV" | tail -n1)"
  fi
  printf '%s' "${value:-$default}"
}

PREFIX="$(fd_env FIELDDECK_PREFIX /opt/fielddeck)"
VENV="$(fd_env FIELDDECK_VENV "$PREFIX/venv")"
# Where CLAUDE and SHELL start. The checkout if we know it, the prefix otherwise
# — 'claude' wants a repository, and /opt/fielddeck/src is the symlink to one
# that install.sh leaves behind.
SOURCE="$(fd_env FIELDDECK_SOURCE "")"
if [[ -z "$SOURCE" || ! -d "$SOURCE" ]]; then
  if [[ -d "$PREFIX/src" ]]; then SOURCE="$PREFIX/src"
  elif [[ -d "$PREFIX" ]]; then SOURCE="$PREFIX"
  else SOURCE="$(cd "$(dirname "$SELF")/.." && pwd -P)"
  fi
fi

# The installed config, falling back to the one next to this script so a git
# checkout works with nothing installed.
TMUX_CONF="/etc/fielddeck/tmux.conf"
[[ -r "$TMUX_CONF" ]] || TMUX_CONF="$(dirname "$SELF")/fielddeck.conf"

ATTACH=1

# Prefer the venv's entry points over whatever is on PATH: a stray pip install
# in a home directory must not decide which HMI the panel runs.
fd_bin() {
  local name="$1"
  if [[ -x "$VENV/bin/$name" ]]; then printf '%s' "$VENV/bin/$name"
  elif command -v "$name" >/dev/null 2>&1; then command -v "$name"
  else printf ''
  fi
}

banner() {
  # A framed message that survives on a 80-column panel. Used for every "this
  # is not working and here is why" case, so they all look the same.
  local line
  printf '\n'
  printf '  +%s+\n' "$(printf '%.0s-' {1..74})"
  for line in "$@"; do
    printf '  | %-72s |\n' "${line:0:72}"
  done
  printf '  +%s+\n\n' "$(printf '%.0s-' {1..74})"
}

# ---------------------------------------------------------------------------
# Window runners
#
# Each window runs one of these rather than a bare command, so that an exit is
# a visible event with a way forward instead of a window that vanishes.
# ---------------------------------------------------------------------------

# Restart with a backoff that resets after a healthy run. Not a synchronisation
# sleep: it exists so a program failing instantly (missing binary, bad config)
# does not spin the CPU and flood the scrollback with the same traceback.
supervise() {
  local label="$1"; shift
  local delay=1 started elapsed rc
  while true; do
    started="$SECONDS"
    rc=0
    "$@" || rc=$?
    elapsed=$(( SECONDS - started ))
    if (( elapsed >= 30 )); then
      delay=1
    fi
    banner \
      "$label exited (status $rc) after ${elapsed}s." \
      "" \
      "Restarting in ${delay}s. Ctrl-C to stop it restarting and get a shell." \
      "Other windows are unaffected: tap 1 HMI / 2 CLAUDE / 3 SHELL / 4 LOG."
    # Ctrl-C during the wait is the operator saying "stop trying"; drop to a
    # shell rather than fighting them.
    sleep "$delay" || { banner "$label supervision stopped. This window is now a shell."; exec "${SHELL:-/bin/bash}"; }
    (( delay < 10 )) && delay=$(( delay * 2 ))
  done
}

run_hmi() {
  local ui; ui="$(fd_bin fielddeck-ui)"
  if [[ -z "$ui" ]]; then
    banner \
      "fielddeck-ui is not installed." \
      "" \
      "Looked in $VENV/bin and on PATH." \
      "Install with:  sudo $SOURCE/scripts/install.sh" \
      "" \
      "This window is a shell so you can fix it without losing the session."
    exec "${SHELL:-/bin/bash}"
  fi
  cd "$SOURCE" 2>/dev/null || cd /
  supervise "HMI" "$ui"
}

run_claude() {
  cd "$SOURCE" 2>/dev/null || cd /
  if ! command -v claude >/dev/null 2>&1; then
    banner \
      "Claude Code is not installed on this unit." \
      "" \
      "FieldDeck works completely without it. Everything Claude can do here is" \
      "also a deterministic command:  fdctl status, fdctl devices, fdctl arm," \
      "fdctl recipe run.  Nothing is gated behind the assistant." \
      "" \
      "To add it later, see https://claude.com/claude-code and then run:" \
      "    claude" \
      "from this window." \
      "" \
      "This window is a normal shell in $SOURCE."
    exec "${SHELL:-/bin/bash}"
  fi

  banner \
    "Claude Code, in $SOURCE" \
    "" \
    "Claude reaches hardware only through the restricted FieldDeck socket." \
    "It can look and it can stop; it cannot arm anything. Authorization comes" \
    "from the panel or from fdctl, never from this window."
  claude || true

  banner \
    "Claude exited. The session is fine." \
    "" \
    "Start it again with:  claude" \
    "This window is a shell in $SOURCE until you do."
  exec "${SHELL:-/bin/bash}"
}

run_shell() {
  cd "$SOURCE" 2>/dev/null || cd /
  exec "${SHELL:-/bin/bash}"
}

run_log() {
  if ! command -v journalctl >/dev/null 2>&1; then
    banner \
      "journalctl is not available on this system." \
      "" \
      "If you are running instrumentd by hand, its log is on its stderr." \
      "This window is a shell."
    exec "${SHELL:-/bin/bash}"
  fi
  # -n 200 so the window has context the moment it opens, rather than an empty
  # screen until the next event.
  supervise "LOG" journalctl -n 200 -f -u instrumentd
}

# ---------------------------------------------------------------------------
# Session construction
# ---------------------------------------------------------------------------

window_exists() {
  tmux list-windows -t "$SESSION" -F '#{window_index}' 2>/dev/null | grep -qx "$1"
}

# Create the window if it is missing; if something else has taken the index,
# take it back. Either way the operator ends up with the documented layout.
ensure_window() {
  local index="$1" name="$2" runner="$3" current
  if window_exists "$index"; then
    current="$(tmux display-message -p -t "$SESSION:$index" '#{window_name}')"
    if [[ "$current" != "$name" ]]; then
      tmux respawn-window -k -t "$SESSION:$index" "$SELF" --run "$runner"
      tmux rename-window -t "$SESSION:$index" "$name"
    fi
    return
  fi
  tmux new-window -d -t "$SESSION:$index" -n "$name" "$SELF" --run "$runner"
}

create_session() {
  # -x/-y matter: a detached session created with no client attached defaults to
  # 80x24, and the HMI is laid out for 80x25. Getting this wrong shows up as a
  # panel whose bottom row is missing rather than as an error.
  tmux -f "$TMUX_CONF" new-session -d -s "$SESSION" -x 80 -y 25 \
    -n HMI "$SELF" --run hmi
}

ensure_session() {
  command -v tmux >/dev/null 2>&1 || {
    printf 'fielddeck-session: tmux is not installed (sudo apt install tmux)\n' >&2
    exit 1
  }
  if ! tmux has-session -t "$SESSION" 2>/dev/null; then
    create_session
  fi
  ensure_window 1 HMI    hmi
  ensure_window 2 CLAUDE claude
  ensure_window 3 SHELL  shell
  ensure_window 4 LOG    log
  # Open on the panel, not on wherever the last person left it.
  tmux select-window -t "$SESSION:1"
}

usage() {
  cat <<'USAGE'
FieldDeck tmux session

  fielddeck-session [--no-attach] [--session NAME]

  Creates or repairs the 'fielddeck' session (windows 1 HMI, 2 CLAUDE,
  3 SHELL, 4 LOG) and attaches to it.

  --no-attach     Build the session and leave it detached. What the kiosk
                  uses before it hands the terminal over.
  --session NAME  Operate on a differently named session.
  --run WHICH     Internal: run one window's supervised process
                  (hmi | claude | shell | log).
USAGE
}

main() {
  while (( $# )); do
    case "$1" in
      --run)        shift; case "${1:-}" in
                      hmi)    run_hmi ;;
                      claude) run_claude ;;
                      shell)  run_shell ;;
                      log)    run_log ;;
                      *)      printf 'unknown window: %s\n' "${1:-}" >&2; exit 2 ;;
                    esac
                    return ;;
      --no-attach)  ATTACH=0; shift ;;
      --session)    SESSION="${2:?--session needs a name}"; shift 2 ;;
      -h|--help)    usage; exit 0 ;;
      *)            usage >&2; exit 2 ;;
    esac
  done

  ensure_session
  (( ATTACH )) || { printf 'session %s ready (detached)\n' "$SESSION"; return; }

  if [[ -n "${TMUX:-}" ]]; then
    # Already inside tmux — nesting a client inside itself is a mess of
    # prefix keys nobody wants on a touchscreen.
    tmux switch-client -t "$SESSION"
  else
    exec tmux attach-session -t "$SESSION"
  fi
}

main "$@"
