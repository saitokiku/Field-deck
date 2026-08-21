#!/usr/bin/env bash
#
# FieldDeck kiosk launcher — what fielddeck-kiosk.service execs on tty1.
#
# The whole job is: get an X server onto the panel, put one terminal on it, and
# hand that terminal to tmux. There is no window manager, no desktop and no
# session manager, because every one of those is another thing that can be
# between an operator and the E-STOP button.
#
# It is deliberately noisy in the journal. When a panel stays black at the top
# of a ladder, the only debugging you have is 'journalctl -u fielddeck-kiosk',
# so this logs what it found before it tries to use it.
#
# Not started directly. Use:  sudo systemctl start fielddeck-kiosk

set -euo pipefail

INSTALL_ENV="/etc/fielddeck/install.env"
XINITRC="${FIELDDECK_XINITRC:-/etc/fielddeck/xinitrc}"
VT="${FIELDDECK_KIOSK_VT:-1}"
DISPLAY_NUM="${FIELDDECK_KIOSK_DISPLAY:-:0}"

log()  { printf 'fielddeck-kiosk: %s\n' "$*"; }
warn() { printf 'fielddeck-kiosk: WARNING: %s\n' "$*" >&2; }
die()  { printf 'fielddeck-kiosk: ERROR: %s\n' "$*" >&2; exit 1; }

fd_env() {
  local key="$1" default="${2:-}" value=""
  if [[ -r "$INSTALL_ENV" ]]; then
    value="$(sed -n "s/^${key}=//p" "$INSTALL_ENV" | tail -n1)"
  fi
  printf '%s' "${value:-$default}"
}

PREFIX="$(fd_env FIELDDECK_PREFIX /opt/fielddeck)"
SESSION_SH="/usr/local/lib/fielddeck/fielddeck-session.sh"
[[ -x "$SESSION_SH" ]] || SESSION_SH="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)/tmux/fielddeck-session.sh"

# ---------------------------------------------------------------------------
# Report the hardware we are about to draw on. None of this is fatal — a panel
# that is missing here is usually a dtoverlay that was never added to
# /boot/firmware/config.txt, and saying so beats a black screen.
# ---------------------------------------------------------------------------

report_hardware() {
  log "user=$(id -un) uid=$(id -u) tty=${XDG_VTNR:-unknown} display=${DISPLAY_NUM}"

  local fb
  local -a fbs=()
  for fb in /dev/fb*; do [[ -e "$fb" ]] && fbs+=("$fb"); done
  if (( ${#fbs[@]} )); then
    log "framebuffers: ${fbs[*]}"
    local sysfs name size
    for fb in "${fbs[@]}"; do
      sysfs="/sys/class/graphics/$(basename "$fb")"
      [[ -r "$sysfs/name" ]] || continue
      name="$(cat "$sysfs/name")"
      size="unknown size"
      [[ -r "$sysfs/virtual_size" ]] && size="$(cat "$sysfs/virtual_size")"
      log "  $fb: $name ($size)"
    done
  else
    warn "no /dev/fb* device. An SPI panel needs its dtoverlay in"
    warn "/boot/firmware/config.txt; FieldDeck's installer does not add it,"
    warn "because a wrong overlay there costs you the next boot."
  fi

  if [[ -d /dev/input ]]; then
    local event
    local -a inputs=()
    for event in /dev/input/event*; do [[ -e "$event" ]] && inputs+=("$(basename "$event")"); done
    if (( ${#inputs[@]} )); then
      log "input devices: ${inputs[*]}"
      # The names are what tell you whether the touch controller enumerated at
      # all, or whether X is about to come up with a keyboard and nothing else.
      local name_file
      for name_file in /sys/class/input/event*/device/name; do
        [[ -r "$name_file" ]] || continue
        log "  $(basename "$(dirname "$(dirname "$name_file")")"): $(cat "$name_file")"
      done
    else
      warn "no /dev/input/event* devices: X will start with no touch and no keyboard."
    fi
  fi
}

# ---------------------------------------------------------------------------
# Fallback: no X. Still give the operator a panel.
#
# The HMI is a terminal application; X exists here only for pixels and touch.
# If X is missing or broken, running the session straight on the console is a
# strictly better outcome than a black screen — you lose touch, not FieldDeck.
# ---------------------------------------------------------------------------

run_on_console() {
  warn "starting the tmux session directly on the console instead of under X."
  warn "You will have keyboard control but no touch. Fix X and restart with:"
  warn "  sudo systemctl restart fielddeck-kiosk"
  exec "$SESSION_SH"
}

main() {
  log "starting (prefix=${PREFIX})"
  report_hardware

  if ! command -v xinit >/dev/null 2>&1; then
    warn "xinit is not installed (apt install xinit), so there is no X to start."
    run_on_console
  fi
  if ! command -v Xorg >/dev/null 2>&1 && ! command -v X >/dev/null 2>&1; then
    warn "no X server found (apt install xserver-xorg-core)."
    run_on_console
  fi
  [[ -x "$SESSION_SH" ]] || die "cannot find fielddeck-session.sh (looked at $SESSION_SH)"

  if [[ ! -r "$XINITRC" ]]; then
    warn "$XINITRC is missing; install it from config/.xinitrc.example."
    run_on_console
  fi

  log "exec xinit $XINITRC -- $DISPLAY_NUM vt$VT"
  # -keeptty: the service already owns /dev/tty1 (TTYPath= in the unit). Without
  #   it X opens a tty of its own, fights the one systemd gave us, and usually
  #   fails with "Cannot open virtual console".
  # -nolisten tcp: no X network listener. There is nothing on the far side of
  #   this device that should be drawing on the panel.
  # vt$VT: pin the console. Letting X pick means it sometimes lands on a VT the
  #   operator cannot get back from.
  exec xinit "$XINITRC" -- "$DISPLAY_NUM" "vt${VT}" -keeptty -nolisten tcp
}

main "$@"
