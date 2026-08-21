#!/usr/bin/env bash
#
# FieldDeck uninstaller.
#
# Removes the services, the units, the virtualenv, the udev rules, the kiosk
# assets and /etc/fielddeck.
#
# It does NOT remove /var/lib/fielddeck/sessions. Captured traces are evidence:
# somebody recorded them because a device misbehaved, and an uninstaller that
# quietly deletes them has destroyed the only copy of a fault that may not
# reproduce. Pass --purge-sessions if you genuinely want them gone; you will be
# told exactly what is about to be deleted first.

set -euo pipefail

DEFAULT_PREFIX="/opt/fielddeck"
PREFIX="$DEFAULT_PREFIX"
CONFIG_DIR="/etc/fielddeck"
STATE_DIR="/var/lib/fielddeck"
RUNTIME_DIR="/run/fielddeck"
UNIT_DIR="/etc/systemd/system"
LIB_DIR="/usr/local/lib/fielddeck"
BIN_DIR="/usr/local/bin"
UDEV_RULES="/etc/udev/rules.d/99-fielddeck.rules"
XORG_CONF="/etc/X11/xorg.conf.d/10-fielddeck-touch.conf"
FD_USER="fielddeck"
FD_GROUP="fielddeck"

PURGE_SESSIONS=0
REMOVE_USER=0
KEEP_CONFIG=0
DRY_RUN=0
ASSUME_YES=0

if [[ -t 1 ]]; then
  C_BOLD=$'\033[1m'; C_DIM=$'\033[2m'; C_WARN=$'\033[33m'; C_ERR=$'\033[31m'; C_OK=$'\033[32m'; C_OFF=$'\033[0m'
else
  C_BOLD=""; C_DIM=""; C_WARN=""; C_ERR=""; C_OK=""; C_OFF=""
fi

step() { printf '\n%s==> %s%s\n' "$C_BOLD" "$*" "$C_OFF"; }
say()  { printf '    %s\n' "$*"; }
note() { printf '    %s%s%s\n' "$C_DIM" "$*" "$C_OFF"; }
ok()   { printf '    %s✔%s %s\n' "$C_OK" "$C_OFF" "$*"; }
warn() { printf '%s!! %s%s\n' "$C_WARN" "$*" "$C_OFF" >&2; }
die()  { printf '%s!! %s%s\n' "$C_ERR" "$*" "$C_OFF" >&2; exit 1; }

run() {
  if (( DRY_RUN )); then
    printf '    %swould run:%s %s\n' "$C_DIM" "$C_OFF" "$(printf '%q ' "$@")"
    return 0
  fi
  "$@"
}

# Removing a unit that was never installed is not a failure, it is the desired
# end state; the same goes for every path below.
run_ok() {
  if (( DRY_RUN )); then
    printf '    %swould run:%s %s\n' "$C_DIM" "$C_OFF" "$(printf '%q ' "$@")"
    return 0
  fi
  "$@" || true
}

usage() {
  cat <<'USAGE'
FieldDeck uninstaller

  sudo scripts/uninstall.sh [options]

Options:
  --purge-sessions  ALSO delete /var/lib/fielddeck/sessions and everything in it.
                    Captured CAN/serial/logic traces, screenshots and reports.
                    There is no undo and no backup.
  --remove-user     Also delete the 'fielddeck' system user and group. Left in
                    place by default, because it still owns the session files.
  --keep-config     Leave /etc/fielddeck alone (safety.yaml, aliases, presets).
  --prefix DIR      Where the venv was installed (default /opt/fielddeck; read
                    from /etc/fielddeck/install.env when present).
  --dry-run         Print what would be removed, remove nothing.
  -y, --assume-yes  Do not prompt. Required for the --purge-sessions prompt too.
  -h, --help        This text.
USAGE
}

while (( $# )); do
  case "$1" in
    --purge-sessions) PURGE_SESSIONS=1; shift ;;
    --remove-user)    REMOVE_USER=1; shift ;;
    --keep-config)    KEEP_CONFIG=1; shift ;;
    --prefix)         PREFIX="${2:?--prefix needs a directory}"; shift 2 ;;
    --prefix=*)       PREFIX="${1#*=}"; shift ;;
    --dry-run)        DRY_RUN=1; shift ;;
    -y|--assume-yes)  ASSUME_YES=1; shift ;;
    -h|--help)        usage; exit 0 ;;
    *)                usage >&2; die "unknown option: $1" ;;
  esac
done

# The installer records where it put things; prefer that over the default.
if [[ -r "$CONFIG_DIR/install.env" ]]; then
  # shellcheck disable=SC1091  # written by install.sh as strict KEY=value
  recorded_prefix="$(sed -n 's/^FIELDDECK_PREFIX=//p' "$CONFIG_DIR/install.env" | tail -n1)"
  if [[ -n "${recorded_prefix:-}" && "$PREFIX" == "$DEFAULT_PREFIX" ]]; then
    PREFIX="$recorded_prefix"
    note "prefix ${PREFIX} read from ${CONFIG_DIR}/install.env"
  fi
fi
PREFIX="${PREFIX%/}"

(( DRY_RUN )) || [[ "$(id -u)" -eq 0 ]] || die "this needs root. Re-run with sudo, or add --dry-run."

# ---------------------------------------------------------------------------
# Say out loud what will and will not survive, before touching anything.
# ---------------------------------------------------------------------------

session_summary() {
  if [[ -d "$STATE_DIR/sessions" ]]; then
    local count size
    count="$(find "$STATE_DIR/sessions" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | wc -l)"
    size="$(du -sh "$STATE_DIR/sessions" 2>/dev/null | cut -f1)"
    printf '%s session directories, %s' "${count:-0}" "${size:-unknown size}"
  else
    printf 'none present'
  fi
}

step "What this will do"
cat <<PLAN
    remove   instrumentd.service, fielddeck-kiosk.service, fielddeck.target
    remove   ${PREFIX}/venv and ${PREFIX}/requirements.lock
    remove   ${UDEV_RULES}
    remove   ${LIB_DIR}, the ${BIN_DIR} symlinks, ${XORG_CONF}
    remove   $( (( KEEP_CONFIG )) && echo "(nothing — --keep-config)" || echo "${CONFIG_DIR} — safety.yaml, aliases, presets" )
    user     $( (( REMOVE_USER )) && echo "${FD_USER} and group ${FD_GROUP} DELETED" || echo "${FD_USER} kept (still owns the session files)" )
PLAN

if (( PURGE_SESSIONS )); then
  printf '\n%s!!  --purge-sessions: %s/sessions WILL BE DELETED  !!%s\n' "$C_ERR" "$STATE_DIR" "$C_OFF"
  printf '%s    Currently holding: %s%s\n' "$C_ERR" "$(session_summary)" "$C_OFF"
  printf '%s    Captured traces are evidence. There is no undo.%s\n\n' "$C_ERR" "$C_OFF"
  if (( ! ASSUME_YES )) && (( ! DRY_RUN )); then
    [[ -t 0 ]] || die "--purge-sessions on a non-interactive run needs --assume-yes as well"
    read -r -p "    Type DELETE to confirm: " reply
    [[ "$reply" == "DELETE" ]] || die "not confirmed; nothing was removed"
  fi
else
  printf '\n%s    KEEPING %s/sessions (%s).%s\n' "$C_BOLD" "$STATE_DIR" "$(session_summary)" "$C_OFF"
  printf '%s    Pass --purge-sessions if you want them deleted.%s\n' "$C_DIM" "$C_OFF"
fi

# ---------------------------------------------------------------------------
# Stop first. Never remove a venv out from under a running daemon: it would be
# killed mid-action rather than shutting down through its own safe-state path.
# ---------------------------------------------------------------------------

step "Stopping and disabling services"
for unit in fielddeck-kiosk.service instrumentd.service fielddeck.target; do
  if systemctl list-unit-files "$unit" >/dev/null 2>&1 && systemctl cat "$unit" >/dev/null 2>&1; then
    say "stopping $unit (instrumentd drives outputs to a safe state on the way down)"
    run_ok systemctl stop "$unit"
    run_ok systemctl disable "$unit"
  else
    note "$unit not installed"
  fi
done

step "Removing systemd units"
for unit in instrumentd.service fielddeck-kiosk.service fielddeck.target; do
  run_ok rm -f "$UNIT_DIR/$unit"
  run_ok rm -rf "$UNIT_DIR/$unit.d"
done
run_ok systemctl daemon-reload
run_ok systemctl reset-failed
ok "units removed"

step "Removing udev rules"
run_ok rm -f "$UDEV_RULES"
if command -v udevadm >/dev/null 2>&1; then
  run_ok udevadm control --reload-rules
  say "device nodes keep their current group until they are re-plugged or the Pi reboots"
fi
ok "udev rules removed"

step "Removing the Python environment"
run_ok rm -rf "$PREFIX/venv"
run_ok rm -f "$PREFIX/requirements.lock"
run_ok rm -f "$PREFIX/src"
# Only if it is now empty: --prefix may be a directory that predates FieldDeck.
if [[ -d "$PREFIX" ]] && (( ! DRY_RUN )); then
  rmdir "$PREFIX" 2>/dev/null && ok "$PREFIX removed (it was empty)" || note "$PREFIX kept: not empty"
fi
for cmd in fdctl instrumentd fielddeck-ui fielddeck-mcp fielddeck-preflight fielddeck-session; do
  run_ok rm -f "$BIN_DIR/$cmd"
done
ok "venv, lockfile and command symlinks removed"

step "Removing kiosk assets"
run_ok rm -rf "$LIB_DIR"
run_ok rm -f "$XORG_CONF"
note "/etc/X11/Xwrapper.config is left alone: other software may rely on it."
ok "kiosk assets removed"

if (( KEEP_CONFIG )); then
  step "Keeping ${CONFIG_DIR} (--keep-config)"
else
  step "Removing ${CONFIG_DIR}"
  say "This includes safety.yaml. If you tuned limits for a specific bench, copy"
  say "it somewhere first — that file is the record of a deliberate decision."
  run_ok rm -rf "$CONFIG_DIR"
  ok "$CONFIG_DIR removed"
fi

step "Runtime directory"
run_ok rm -rf "$RUNTIME_DIR"
ok "$RUNTIME_DIR removed (systemd recreates it only while the unit runs)"

if (( PURGE_SESSIONS )); then
  step "Deleting captured sessions"
  run_ok rm -rf "$STATE_DIR"
  ok "$STATE_DIR removed"
else
  step "Session store"
  say "KEPT: $STATE_DIR"
  say "  sessions:  $STATE_DIR/sessions"
  say "  firmware:  $STATE_DIR/firmware"
  say "  recipes:   $STATE_DIR/recipes"
  say "Remove it yourself with: sudo rm -rf $STATE_DIR"
fi

if (( REMOVE_USER )); then
  step "Removing the ${FD_USER} account"
  if (( ! PURGE_SESSIONS )) && [[ -d "$STATE_DIR" ]]; then
    warn "$STATE_DIR is still owned by ${FD_USER}; after this it will show a bare UID."
  fi
  run_ok userdel "$FD_USER"
  run_ok groupdel "$FD_GROUP"
  ok "account removed"
else
  step "Accounts"
  say "KEPT: user '${FD_USER}' and group '${FD_GROUP}'."
  say "They still own the session store. Remove with --remove-user, or:"
  say "  sudo userdel ${FD_USER} && sudo groupdel ${FD_GROUP}"
  say "Human accounts are not touched; they simply stay in a group nothing uses."
fi

if (( DRY_RUN )); then
  printf '\n%sDry run complete. Nothing was changed.%s\n\n' "$C_BOLD" "$C_OFF"
else
  printf '\n%sFieldDeck removed.%s\n\n' "$C_BOLD" "$C_OFF"
fi
