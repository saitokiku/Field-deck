#!/usr/bin/env bash
#
# FieldDeck installer — turns a Raspberry Pi OS Lite image into a FieldDeck unit.
#
# What this actually does, in one paragraph, because an installer that you cannot
# reason about is an installer you should not run as root: it installs apt
# packages, creates the unprivileged `fielddeck` system user that owns all
# hardware access, builds a virtualenv under --prefix, drops configuration into
# /etc/fielddeck without ever overwriting what is already there, installs udev
# rules so the daemon never needs root, and enables two systemd units — the
# daemon and (optionally) the kiosk. Nothing here touches /boot/firmware/config.txt
# and nothing here brings a CAN interface up: both are deliberate operator acts.
#
# Re-running is safe. Every step is either idempotent or explicitly skipped when
# it has already happened, and existing configuration is never clobbered.
#
# Run with --dry-run first. It prints every command it would run and changes
# nothing, and it works without root.

set -euo pipefail

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"

DEFAULT_PREFIX="/opt/fielddeck"
PREFIX="$DEFAULT_PREFIX"
FD_USER="fielddeck"          # the daemon's own account; not configurable, units name it
FD_GROUP="fielddeck"
OPERATOR="${SUDO_USER:-}"    # the human who will run fdctl and see the kiosk
CONFIG_DIR="/etc/fielddeck"
STATE_DIR="/var/lib/fielddeck"
RUNTIME_DIR="/run/fielddeck"
UNIT_DIR="/etc/systemd/system"
LIB_DIR="/usr/local/lib/fielddeck"
BIN_DIR="/usr/local/bin"
UDEV_RULES="/etc/udev/rules.d/99-fielddeck.rules"
XORG_CONF="/etc/X11/xorg.conf.d/10-fielddeck-touch.conf"

WITH_KIOSK=1
WITH_APT=1
FROM_LOCK=0
DRY_RUN=0
ASSUME_YES=0

STAGING=""   # temp dir for generated files; cleaned up on exit

# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

if [[ -t 1 ]]; then
  C_BOLD=$'\033[1m'; C_DIM=$'\033[2m'; C_WARN=$'\033[33m'; C_ERR=$'\033[31m'; C_OK=$'\033[32m'; C_OFF=$'\033[0m'
else
  C_BOLD=""; C_DIM=""; C_WARN=""; C_ERR=""; C_OK=""; C_OFF=""
fi

step()  { printf '\n%s==> %s%s\n' "$C_BOLD" "$*" "$C_OFF"; }
say()   { printf '    %s\n' "$*"; }
note()  { printf '    %s%s%s\n' "$C_DIM" "$*" "$C_OFF"; }
ok()    { printf '    %s✔%s %s\n' "$C_OK" "$C_OFF" "$*"; }
warn()  { printf '%s!! %s%s\n' "$C_WARN" "$*" "$C_OFF" >&2; }
die()   { printf '%s!! %s%s\n' "$C_ERR" "$*" "$C_OFF" >&2; exit 1; }

have()  { command -v "$1" >/dev/null 2>&1; }

# Every mutating command goes through run(), so --dry-run is honest rather than
# approximately honest.
run() {
  if (( DRY_RUN )); then
    printf '    %swould run:%s %s\n' "$C_DIM" "$C_OFF" "$(printf '%q ' "$@")"
    return 0
  fi
  "$@"
}

cleanup() { [[ -n "$STAGING" && -d "$STAGING" ]] && rm -rf "$STAGING"; }
trap cleanup EXIT

confirm() {
  # Used only for genuinely questionable situations (unsupported OS, foreign
  # architecture). Non-interactive callers must pass --assume-yes rather than
  # having the answer guessed for them.
  local prompt="$1"
  (( ASSUME_YES )) && { say "--assume-yes: continuing."; return 0; }
  if [[ ! -t 0 ]]; then
    die "$prompt — refusing to guess on a non-interactive run. Re-run with --assume-yes if you mean it."
  fi
  local reply
  read -r -p "    $prompt [y/N] " reply
  [[ "$reply" == [yY] || "$reply" == [yY][eE][sS] ]]
}

# ---------------------------------------------------------------------------
# Usage
# ---------------------------------------------------------------------------

usage() {
  cat <<'USAGE'
FieldDeck installer

  sudo scripts/install.sh [options]

Options:
  --prefix DIR      Where the virtualenv and lockfile live (default /opt/fielddeck).
  --user NAME       Operator account added to the 'fielddeck' group and used to
                    run the kiosk. Defaults to $SUDO_USER.
  --no-kiosk        Daemon only: no Xorg, no terminal, no tmux, no HMI unit.
                    Use this for a headless unit driven over SSH with fdctl.
  --no-apt          Skip apt entirely. Assumes you have already provided the
                    packages the "Skipping apt" step lists.
  --from-lock       Install Python dependencies from PREFIX/requirements.lock
                    instead of resolving them fresh. Reproduces an earlier unit.
  --dry-run         Print every command instead of running it. Needs no root.
  -y, --assume-yes  Do not prompt, even on an unsupported OS or architecture.
  --uninstall-hint  Print how to remove FieldDeck, then exit.
  -h, --help        This text.

Run --dry-run first. Then run it for real, then run:

  sudo scripts/preflight.sh
USAGE
}

uninstall_hint() {
  cat <<HINT
To remove FieldDeck:

  sudo ${SOURCE_DIR}/scripts/uninstall.sh

That stops and disables the units, removes the venv, the units, the udev rules,
the kiosk assets and ${CONFIG_DIR} — and deliberately LEAVES your captured
sessions in ${STATE_DIR}/sessions. Deleting recorded evidence is not something
an uninstaller should do on your behalf; pass --purge-sessions if you truly
want them gone.
HINT
}

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

while (( $# )); do
  case "$1" in
    --prefix)          PREFIX="${2:?--prefix needs a directory}"; shift 2 ;;
    --prefix=*)        PREFIX="${1#*=}"; shift ;;
    --user)            OPERATOR="${2:?--user needs an account name}"; shift 2 ;;
    --user=*)          OPERATOR="${1#*=}"; shift ;;
    --no-kiosk)        WITH_KIOSK=0; shift ;;
    --no-apt)          WITH_APT=0; shift ;;
    --from-lock)       FROM_LOCK=1; shift ;;
    --dry-run)         DRY_RUN=1; shift ;;
    -y|--assume-yes)   ASSUME_YES=1; shift ;;
    --uninstall-hint)  uninstall_hint; exit 0 ;;
    -h|--help)         usage; exit 0 ;;
    *)                 usage >&2; die "unknown option: $1" ;;
  esac
done

PREFIX="${PREFIX%/}"
VENV="$PREFIX/venv"
LOCKFILE="$PREFIX/requirements.lock"

# ---------------------------------------------------------------------------
# Package list
#
# Grouped so that every package can be justified, and so the X group can be
# dropped whole for a headless install. A package nobody can explain is a
# package that should not be on a field device.
# ---------------------------------------------------------------------------

PKGS_BUILD=(
  python3-venv        # the virtualenv the daemon runs from
  python3-dev         # headers: some wheels still build from source on arm
  build-essential     # ditto; also what openocd/dfu users expect to find
  git                 # recipes and firmware are usually pulled from git
  tmux                # the top-level session manager for HMI/CLAUDE/SHELL/LOG.
                      # Not a nicety and not preinstalled on Pi OS Lite: without
                      # it the kiosk chain has no session manager and there is
                      # no 'fielddeck-session' on a headless unit either.
)
PKGS_BUS=(
  can-utils           # candump/cansend — the reference for "is the bus alive"
  i2c-tools           # i2cdetect, for confirming a bus before FieldDeck touches it
  usbutils            # lsusb: the first thing anyone runs when a probe vanishes
  ethtool             # link facts for the network transport
  tcpdump             # net.capture shells out to this
  libusb-1.0-0        # pyusb/openocd/dfu-util talk to USB through it
  fswebcam            # camera.snapshot needs a still-capture backend; without
                      # one the camera actions are registered and can never
                      # succeed. ~100 kB, and the alternatives are ffmpeg
                      # (large) or v4l-utils, both of which it also accepts.
)
PKGS_INSTRUMENT=(
  sigrok-cli          # logic analyzer capture (fielddeck/capture/sigrok.py)
  openocd             # SWD/JTAG (fielddeck/debug/flash.py)
  dfu-util            # USB DFU firmware load
)
# The kiosk group. Xorg exists here only to get pixels onto an SPI panel and
# touch events into a terminal — no desktop, no compositor, no panel.
PKGS_KIOSK=(
  xserver-xorg-core
  xserver-xorg-legacy         # Xorg.wrap: lets X start from a console session, not as root
  xserver-xorg-input-evdev    # resistive panels are usually evdev, not libinput
  xserver-xorg-input-libinput # ...but keep libinput for capacitive/USB pointers
  xserver-xorg-video-fbdev    # SPI panels present as /dev/fb1, not as a DRM device
  xinit                       # startx/xinit; the kiosk service runs xinit directly
  x11-xserver-utils           # xset (blanking off) and xrandr
  xinput                      # applying and checking the touch calibration matrix
  xterm                       # smallest capable terminal with a 6x12 bitmap font
  xfonts-base                 # provides the 6x12 font: 80x25 lands exactly on 480x300
)

apt_packages() {
  local -a pkgs=("${PKGS_BUILD[@]}" "${PKGS_BUS[@]}" "${PKGS_INSTRUMENT[@]}")
  (( WITH_KIOSK )) && pkgs+=("${PKGS_KIOSK[@]}")
  printf '%s\n' "${pkgs[@]}"
}

# ---------------------------------------------------------------------------
# Step 0 — explain, then validate the machine
# ---------------------------------------------------------------------------

announce_plan() {
  cat <<PLAN
${C_BOLD}FieldDeck install plan${C_OFF}

  source checkout      ${SOURCE_DIR}
  prefix               ${PREFIX}       (venv + requirements.lock)
  daemon account       ${FD_USER}:${FD_GROUP}   (system user, no login shell)
  operator account     ${OPERATOR:-<none: pass --user NAME>}
  configuration        ${CONFIG_DIR}       (existing files are never overwritten)
  state / sessions     ${STATE_DIR}
  udev rules           ${UDEV_RULES}
  systemd units        ${UNIT_DIR}/{instrumentd.service,fielddeck.target$( (( WITH_KIOSK )) && printf ',fielddeck-kiosk.service')}
  kiosk                $( (( WITH_KIOSK )) && echo "yes — Xorg + xterm + tmux + HMI on tty1" || echo "no (--no-kiosk): daemon only" )
  apt                  $( (( WITH_APT )) && echo "yes — $(apt_packages | wc -l) packages" || echo "no (--no-apt)" )
  dry run              $( (( DRY_RUN )) && echo "YES — nothing will be changed" || echo "no — this will modify the system" )

Not done by this script, on purpose:
  * /boot/firmware/config.txt is not touched. Your panel's dtoverlay is your
    decision and a bad edit there costs you a boot.
  * No CAN interface is brought up. 'ip link set canX up type can bitrate ...'
    energises a transceiver on someone's bus; that is an operator's act.
  * No firewall, no remote access, no cloud anything.

PLAN
}

check_root() {
  if (( DRY_RUN )); then
    note "dry run: skipping the root check"
    return
  fi
  [[ "$(id -u)" -eq 0 ]] || die "this needs root. Re-run with sudo, or add --dry-run to see what it would do."
}

check_platform() {
  step "Checking the machine"

  local arch; arch="$(uname -m)"
  case "$arch" in
    aarch64|arm64)
      ok "architecture $arch — the supported target" ;;
    armv7l|armv6l)
      warn "architecture $arch is a 32-bit userland."
      say "FieldDeck targets Raspberry Pi OS Lite 64-bit. On 32-bit you will build"
      say "numpy and friends from source (slow), and some wheels do not exist at all."
      confirm "Continue on 32-bit anyway?" || die "stopped at your request"
      ;;
    x86_64|amd64)
      warn "architecture $arch — this is not a Raspberry Pi."
      say "The daemon and CLI are fine here; the kiosk assumes a 480x320 SPI panel"
      say "on tty1 and will not be useful on a desktop."
      confirm "Continue on $arch?" || die "stopped at your request"
      ;;
    *)
      warn "unrecognised architecture $arch"
      confirm "Continue anyway?" || die "stopped at your request"
      ;;
  esac

  if [[ -r /etc/os-release ]]; then
    # shellcheck disable=SC1091  # sourced at runtime on the target, not resolvable here
    . /etc/os-release
    local id="${ID:-unknown}" like="${ID_LIKE:-}" codename="${VERSION_CODENAME:-unknown}"
    if [[ "$id" == "debian" || "$id" == "raspbian" || "$like" == *debian* ]]; then
      if [[ "$codename" == "bookworm" ]]; then
        ok "OS ${PRETTY_NAME:-$id} — the supported target"
      else
        warn "OS ${PRETTY_NAME:-$id} (${codename}) is Debian-like but not Bookworm."
        say "Package names below are Bookworm's. Expect at least one apt failure."
        confirm "Continue on ${codename}?" || die "stopped at your request"
      fi
    else
      warn "OS ${PRETTY_NAME:-$id} is not Debian-based."
      say "The apt step will not work. Install the equivalent packages yourself and"
      say "re-run with --no-apt."
      confirm "Continue on a non-Debian system?" || die "stopped at your request"
    fi
  else
    warn "no /etc/os-release; cannot identify this OS"
    confirm "Continue anyway?" || die "stopped at your request"
  fi

  if [[ -r /proc/device-tree/model ]]; then
    note "board: $(tr -d '\0' < /proc/device-tree/model)"
  fi

  [[ -f "$SOURCE_DIR/pyproject.toml" ]] || die "$SOURCE_DIR does not look like the FieldDeck checkout (no pyproject.toml)"
  ok "source checkout looks right: $SOURCE_DIR"

  if [[ -z "$OPERATOR" ]]; then
    warn "no operator account chosen (\$SUDO_USER is empty and --user was not given)."
    say "Nobody will be added to the 'fielddeck' group, so fdctl will need sudo and"
    say "the kiosk unit will have no account to run as. Re-run with --user NAME to fix."
  elif ! id "$OPERATOR" >/dev/null 2>&1; then
    die "operator account '$OPERATOR' does not exist"
  else
    ok "operator account: $OPERATOR"
  fi

  case "$SOURCE_DIR" in
    /root/*|/home/*)
      note "the checkout lives under a home directory. That is fine — the daemon"
      note "installs into the venv and never reads it — but for a permanent unit"
      note "consider cloning to ${PREFIX}/src so root-owned services and the kiosk"
      note "user see the same tree."
      ;;
  esac
}

# ---------------------------------------------------------------------------
# Step 1 — apt
# ---------------------------------------------------------------------------

install_apt() {
  if (( ! WITH_APT )); then
    step "Skipping apt (--no-apt)"
    say "Make sure these are present, or things will fail later in confusing ways:"
    apt_packages | tr '\n' ' ' | fold -s -w 76 | sed 's/^/      /'
    return
  fi

  step "Installing apt packages"
  have apt-get || die "apt-get not found; re-run with --no-apt and install the packages yourself"

  local -a pkgs; mapfile -t pkgs < <(apt_packages)
  say "core build + git + tmux:   ${PKGS_BUILD[*]}"
  say "bus and capture tools:     ${PKGS_BUS[*]}"
  say "instrument/debug tools:    ${PKGS_INSTRUMENT[*]}"
  if (( WITH_KIOSK )); then
    say "kiosk (X, no desktop):     ${PKGS_KIOSK[*]}"
  else
    say "kiosk packages skipped (--no-kiosk)"
  fi

  run env DEBIAN_FRONTEND=noninteractive apt-get update
  run env DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "${pkgs[@]}"
  ok "apt packages installed"
}

# ---------------------------------------------------------------------------
# Step 2 — the fielddeck user and group
# ---------------------------------------------------------------------------

add_to_group_if_exists() {
  # Adding a user to a group that does not exist on this image is not an error,
  # it just means that class of hardware is not present.
  local user="$1" group="$2"
  if ! getent group "$group" >/dev/null 2>&1; then
    note "group '$group' does not exist on this system; skipping"
    return
  fi
  if id -nG "$user" 2>/dev/null | tr ' ' '\n' | grep -qx "$group"; then
    note "$user is already in '$group'"
    return
  fi
  run usermod -aG "$group" "$user"
  ok "$user added to '$group'"
}

install_user() {
  step "Creating the ${FD_USER} system account"
  say "The daemon runs as this account. It owns hardware access; it has no login"
  say "shell and no password, and it cannot write its own safety policy."

  if getent group "$FD_GROUP" >/dev/null 2>&1; then
    note "group '$FD_GROUP' already exists"
  else
    run groupadd --system "$FD_GROUP"
    ok "group '$FD_GROUP' created"
  fi

  if id "$FD_USER" >/dev/null 2>&1; then
    note "user '$FD_USER' already exists"
  else
    run useradd --system --gid "$FD_GROUP" \
      --home-dir "$STATE_DIR" --no-create-home \
      --shell /usr/sbin/nologin \
      --comment "FieldDeck instrument daemon" "$FD_USER"
    ok "user '$FD_USER' created"
  fi

  # Kernel-assigned device groups. udev rules cover USB adapters and probes;
  # these cover the on-board peripherals whose group Raspberry Pi OS already
  # sets (gpio/i2c/spi via its own 99-com.rules, dialout for /dev/ttyAMA*).
  say "Adding ${FD_USER} to the device groups Raspberry Pi OS already assigns:"
  local group
  for group in dialout plugdev i2c spi gpio video netdev; do
    add_to_group_if_exists "$FD_USER" "$group"
  done

  if [[ -n "$OPERATOR" ]]; then
    say "Adding ${OPERATOR} to the groups a human operator needs:"
    # 'fielddeck' is what makes the 0660 control socket openable without sudo.
    add_to_group_if_exists "$OPERATOR" "$FD_GROUP"
    # ...and this is what makes the tmux LOG window able to follow a *service*
    # journal rather than only the operator's own messages.
    add_to_group_if_exists "$OPERATOR" "systemd-journal"
    note "group membership only takes effect on a new login. 'newgrp ${FD_GROUP}'"
    note "gets you a shell with it immediately."
  fi
}

# ---------------------------------------------------------------------------
# Step 3 — the Python environment
# ---------------------------------------------------------------------------

install_python() {
  step "Building the Python environment at ${VENV}"

  run install -d -m 0755 "$PREFIX"

  if [[ -x "$VENV/bin/python" ]]; then
    note "venv already exists; upgrading in place"
  else
    say "python3 -m venv ${VENV}"
    run python3 -m venv "$VENV"
  fi

  run "$VENV/bin/python" -m pip install --upgrade pip setuptools wheel

  if (( FROM_LOCK )); then
    [[ -f "$LOCKFILE" ]] || die "--from-lock given but $LOCKFILE does not exist"
    say "Installing pinned dependencies from ${LOCKFILE} (reproducing an earlier unit)."
    run "$VENV/bin/python" -m pip install --require-virtualenv -r "$LOCKFILE"
    say "Installing FieldDeck itself without re-resolving dependencies."
    run "$VENV/bin/python" -m pip install --require-virtualenv --no-deps "${SOURCE_DIR}[hardware]"
  else
    say "Installing FieldDeck with the [hardware] extra: pyserial, python-can,"
    say "cantools, pymodbus, pyvisa, pyusb, numpy, zstandard."
    say "This needs network access and can take several minutes on a Pi."
    run "$VENV/bin/python" -m pip install --require-virtualenv --upgrade "${SOURCE_DIR}[hardware]"
  fi

  # The lockfile is what makes a second unit identical to this one. FieldDeck
  # itself is excluded: it is installed from the checkout, and a 'fielddeck @
  # file:///...' line would only be true on this machine.
  step "Writing the dependency snapshot to ${LOCKFILE}"
  if (( DRY_RUN )); then
    note "would run: ${VENV}/bin/python -m pip freeze > ${LOCKFILE}"
  else
    {
      echo "# FieldDeck dependency snapshot"
      echo "# Written by scripts/install.sh on $(date -u +%Y-%m-%dT%H:%M:%SZ) from ${SOURCE_DIR}"
      echo "# Reproduce this exact environment elsewhere with:"
      echo "#   sudo scripts/install.sh --from-lock"
      echo "# FieldDeck itself is intentionally absent: it is installed from the checkout."
      "$VENV/bin/python" -m pip freeze --exclude-editable | grep -v -i '^fielddeck' || true
    } > "$LOCKFILE"
    chmod 0644 "$LOCKFILE"
    ok "$(grep -c . "$LOCKFILE") lines written"
  fi

  step "Linking the FieldDeck commands into ${BIN_DIR}"
  say "So fdctl works from any shell without activating the venv."
  local cmd
  for cmd in fdctl instrumentd fielddeck-ui fielddeck-mcp; do
    if (( DRY_RUN )) || [[ -x "$VENV/bin/$cmd" ]]; then
      run ln -sfn "$VENV/bin/$cmd" "$BIN_DIR/$cmd"
    else
      warn "$VENV/bin/$cmd was not installed; skipping the symlink"
    fi
  done
  run install -m 0755 "$SOURCE_DIR/scripts/preflight.sh" "$BIN_DIR/fielddeck-preflight"
  ok "fdctl, instrumentd, fielddeck-ui, fielddeck-mcp, fielddeck-preflight"

  # The CLAUDE and SHELL tmux windows cd here, and 'claude' wants a repository.
  if [[ "$SOURCE_DIR" != "$PREFIX"* ]]; then
    run ln -sfn "$SOURCE_DIR" "$PREFIX/src"
    ok "$PREFIX/src -> $SOURCE_DIR"
  fi
}

# ---------------------------------------------------------------------------
# Step 4 — directories and configuration
# ---------------------------------------------------------------------------

# install_config_file SRC DST — never overwrites, always explains.
install_config_file() {
  local src="$1" dst="$2" owner="$3" mode="$4"
  if [[ -e "$dst" ]]; then
    note "$dst exists — keeping your version (the example is at $src)"
    return
  fi
  run install -o "${owner%%:*}" -g "${owner##*:}" -m "$mode" "$src" "$dst"
  ok "$dst installed from the example"
}

install_directories() {
  step "Creating directories"
  say "Configuration is root-owned: the daemon reads its safety policy and cannot"
  say "rewrite it. State is fielddeck-owned and mode 0750 — group members can"
  say "reach the control socket, but they cannot rummage in the session store."

  run install -d -o root -g "$FD_GROUP" -m 0755 "$CONFIG_DIR"
  run install -d -o root -g "$FD_GROUP" -m 0755 "$CONFIG_DIR/instruments"
  # Recipe search order includes <config>/recipes and <state>/recipes.
  run install -d -o root -g "$FD_GROUP" -m 0755 "$CONFIG_DIR/recipes"

  run install -d -o "$FD_USER" -g "$FD_GROUP" -m 0750 "$STATE_DIR"
  run install -d -o "$FD_USER" -g "$FD_GROUP" -m 0750 "$STATE_DIR/sessions"
  run install -d -o "$FD_USER" -g "$FD_GROUP" -m 0750 "$STATE_DIR/logs"
  run install -d -o "$FD_USER" -g "$FD_GROUP" -m 0750 "$STATE_DIR/recipes"
  # firmware/ is one of the few roots a flash action will read an image from.
  run install -d -o "$FD_USER" -g "$FD_GROUP" -m 0750 "$STATE_DIR/firmware"

  # systemd recreates this every boot from RuntimeDirectory=; creating it now
  # only matters if you start instrumentd by hand before the first boot.
  run install -d -o "$FD_USER" -g "$FD_GROUP" -m 0750 "$RUNTIME_DIR"
  ok "directories in place"

  step "Installing example configuration (never overwriting yours)"
  install_config_file "$SOURCE_DIR/config/fielddeck.example.yaml" "$CONFIG_DIR/fielddeck.yaml" "root:$FD_GROUP" 0644
  install_config_file "$SOURCE_DIR/config/safety.example.yaml"    "$CONFIG_DIR/safety.yaml"    "root:$FD_GROUP" 0644
  install_config_file "$SOURCE_DIR/config/instruments/README.md"  "$CONFIG_DIR/instruments/README.md" "root:$FD_GROUP" 0644
  note "config/ui.example.yaml is NOT installed: no loader reads ui.yaml yet, and"
  note "a config file that silently does nothing reads as a promise. See the file."

  # Consumed by the kiosk and tmux scripts, and by the systemd units as an
  # EnvironmentFile. Deliberately restricted to KEY=value so it is valid in both
  # systemd's parser and 'set -a; . file'.
  step "Recording the install layout in ${CONFIG_DIR}/install.env"
  say "The kiosk and tmux scripts read it instead of hard-coding ${PREFIX}."
  local envfile="$STAGING/install.env"
  cat > "$envfile" <<ENVEOF
# Written by scripts/install.sh. Safe to edit; re-running the installer rewrites it.
# Strict KEY=value only: systemd reads this as an EnvironmentFile.
FIELDDECK_PREFIX=${PREFIX}
FIELDDECK_VENV=${VENV}
FIELDDECK_SOURCE=${SOURCE_DIR}
FIELDDECK_USER=${FD_USER}
FIELDDECK_GROUP=${FD_GROUP}
FIELDDECK_OPERATOR=${OPERATOR}
FIELDDECK_CONFIG_DIR=${CONFIG_DIR}
FIELDDECK_STATE_DIR=${STATE_DIR}
FIELDDECK_KIOSK=$( (( WITH_KIOSK )) && echo 1 || echo 0 )
ENVEOF
  run install -o root -g "$FD_GROUP" -m 0644 "$envfile" "$CONFIG_DIR/install.env"
  ok "$CONFIG_DIR/install.env"
}

# ---------------------------------------------------------------------------
# Step 5 — udev
# ---------------------------------------------------------------------------

install_udev() {
  step "Installing udev rules"
  say "These give the '${FD_GROUP}' group access to USB serial adapters, USBTMC"
  say "instruments, SWD/JTAG probes and logic analyzers — which is what lets the"
  say "daemon run unprivileged. Read the file: it explains every rule."
  run install -o root -g root -m 0644 "$SOURCE_DIR/config/udev/99-fielddeck.rules" "$UDEV_RULES"

  if have udevadm; then
    run udevadm control --reload-rules
    # Re-tags devices that are already plugged in, so you do not have to unplug
    # everything after an install.
    run udevadm trigger --subsystem-match=usb --subsystem-match=tty --action=change
    ok "udev rules installed and applied to already-attached devices"
  else
    warn "udevadm not found; rules installed but not reloaded. Reboot to apply."
  fi
}

# ---------------------------------------------------------------------------
# Step 6 — kiosk assets
# ---------------------------------------------------------------------------

install_kiosk_assets() {
  step "Installing tmux configuration and the kiosk launcher"
  run install -d -m 0755 "$LIB_DIR"
  run install -o root -g root -m 0644 "$SOURCE_DIR/tmux/fielddeck.conf" "$CONFIG_DIR/tmux.conf"
  run install -o root -g root -m 0755 "$SOURCE_DIR/tmux/fielddeck-session.sh" "$LIB_DIR/fielddeck-session.sh"
  ok "$CONFIG_DIR/tmux.conf and $LIB_DIR/fielddeck-session.sh"
  say "tmux is installed for every install, kiosk or not: 'fielddeck-session'"
  say "over SSH gives you the same four windows as the panel."
  run ln -sfn "$LIB_DIR/fielddeck-session.sh" "$BIN_DIR/fielddeck-session"

  if (( ! WITH_KIOSK )); then
    note "--no-kiosk: skipping Xorg, the kiosk launcher and the kiosk unit"
    return
  fi

  run install -o root -g root -m 0755 "$SOURCE_DIR/scripts/fielddeck-kiosk.sh" "$LIB_DIR/fielddeck-kiosk.sh"
  install_config_file "$SOURCE_DIR/config/.xinitrc.example" "$CONFIG_DIR/xinitrc" "root:root" 0755

  step "Installing the Xorg touch configuration"
  say "Input only: device sections and the calibration matrix placeholder. No"
  say "compositor, no panel, no screensaver."
  run install -d -m 0755 /etc/X11/xorg.conf.d
  install_config_file "$SOURCE_DIR/config/xorg/10-fielddeck-touch.conf" "$XORG_CONF" "root:root" 0644

  # Xorg.wrap decides whether a non-root user may start X. Debian ships
  # allowed_users=console, which is exactly what we need — but only if the
  # kiosk service gets a real login session on tty1, which is why the unit uses
  # PAMName=login. Write the file only if it is absent: an operator who has
  # already tuned it knows more about their panel than this script does.
  if [[ ! -e /etc/X11/Xwrapper.config ]]; then
    local wrapper="$STAGING/Xwrapper.config"
    cat > "$wrapper" <<'WRAPEOF'
# Written by FieldDeck's installer because the file was absent.
#
# 'console' means: only a user with an active session on a virtual terminal may
# start X. The kiosk unit gets one via PAMName=login on tty1.
allowed_users=console
# 'auto' lets X drop privileges when the kernel gives it a DRM device. SPI
# panels driven through fbdev (/dev/fb1) usually need real root rights instead;
# if Xorg exits with "cannot open /dev/fb1" or "no screens found", set this to
# yes and try again.
needs_root_rights=auto
WRAPEOF
    run install -o root -g root -m 0644 "$wrapper" /etc/X11/Xwrapper.config
    ok "/etc/X11/Xwrapper.config written (allowed_users=console)"
  else
    note "/etc/X11/Xwrapper.config exists — leaving it alone"
  fi
}

# ---------------------------------------------------------------------------
# Step 7 — systemd
# ---------------------------------------------------------------------------

install_units() {
  step "Installing systemd units"
  run install -o root -g root -m 0644 "$SOURCE_DIR/systemd/instrumentd.service" "$UNIT_DIR/instrumentd.service"
  run install -o root -g root -m 0644 "$SOURCE_DIR/systemd/fielddeck.target"    "$UNIT_DIR/fielddeck.target"
  ok "instrumentd.service, fielddeck.target"

  # The units name /opt/fielddeck literally, because a unit full of variables is
  # a unit nobody can read. A non-default --prefix is handled with a drop-in.
  if [[ "$PREFIX" != "$DEFAULT_PREFIX" ]]; then
    say "Non-default prefix: writing a drop-in so ExecStart points at ${VENV}."
    local dropin_dir="$UNIT_DIR/instrumentd.service.d"
    local dropin="$STAGING/10-prefix.conf"
    cat > "$dropin" <<DROPEOF
# Written by scripts/install.sh --prefix ${PREFIX}
[Service]
ExecStart=
ExecStart=${VENV}/bin/instrumentd
DROPEOF
    run install -d -m 0755 "$dropin_dir"
    run install -o root -g root -m 0644 "$dropin" "$dropin_dir/10-prefix.conf"
    ok "$dropin_dir/10-prefix.conf"
  fi

  if (( WITH_KIOSK )); then
    run install -o root -g root -m 0644 "$SOURCE_DIR/systemd/fielddeck-kiosk.service" "$UNIT_DIR/fielddeck-kiosk.service"
    if [[ -n "$OPERATOR" ]]; then
      # The shipped unit runs as 'fielddeck', which has no login shell, so a
      # missing drop-in fails the kiosk loudly instead of quietly running X as
      # root. This is the drop-in that makes it work.
      local kdir="$UNIT_DIR/fielddeck-kiosk.service.d"
      local kconf="$STAGING/10-operator.conf"
      cat > "$kconf" <<KIOSKEOF
# Written by scripts/install.sh --user ${OPERATOR}
# The kiosk runs as a human account: it needs a login session on tty1 for the
# logind ACLs that grant access to /dev/input/* and /dev/fb*.
[Service]
User=${OPERATOR}
Group=$(id -gn "$OPERATOR")
KIOSKEOF
      run install -d -m 0755 "$kdir"
      run install -o root -g root -m 0644 "$kconf" "$kdir/10-operator.conf"
      ok "fielddeck-kiosk.service (running as $OPERATOR)"
    else
      warn "no operator account: the kiosk unit is installed but will fail to start."
      say "Re-run with --user NAME, or write ${UNIT_DIR}/fielddeck-kiosk.service.d/10-operator.conf yourself."
    fi
  fi

  run systemctl daemon-reload

  step "Enabling services"
  say "fielddeck.target is what boot pulls in; instrumentd is wanted by it."
  say "The kiosk only Wants= the daemon, so restarting the HMI never restarts"
  say "instrumentd — and the panel keeps running if the daemon is down."
  run systemctl enable fielddeck.target
  run systemctl enable instrumentd.service
  (( WITH_KIOSK )) && [[ -n "$OPERATOR" ]] && run systemctl enable fielddeck-kiosk.service

  say "Starting instrumentd now."
  run systemctl restart instrumentd.service
  ok "instrumentd enabled and started"
  if (( WITH_KIOSK )); then
    note "The kiosk is enabled but not started: it takes over tty1, which would"
    note "pull the console out from under this shell. It comes up on next boot,"
    note "or start it deliberately with: sudo systemctl start fielddeck-kiosk"
  fi
}

# ---------------------------------------------------------------------------
# Step 8 — what to check
# ---------------------------------------------------------------------------

print_verification() {
  cat <<VERIFY

${C_BOLD}Installed.${C_OFF} Verify in this order — each command answers one question.

${C_BOLD}1. Is the daemon running?${C_OFF}
     systemctl status instrumentd --no-pager
     journalctl -u instrumentd -n 40 --no-pager
   A daemon that refuses to start on a bad ${CONFIG_DIR}/safety.yaml is
   working as designed. The journal line will name the field.

${C_BOLD}2. Is the socket permissioned so you do not need sudo?${C_OFF}
     ls -l ${RUNTIME_DIR}/
   Expect srw-rw---- ${FD_USER} ${FD_GROUP} instrumentd.sock

${C_BOLD}3. Are you in the group?${C_OFF}
     id -nG ${OPERATOR:-\$USER} | tr ' ' '\\n' | grep ${FD_GROUP}
   Group changes need a fresh login. If it is missing here but you just ran the
   installer, log out and back in.

${C_BOLD}4. Does the CLI reach the daemon?${C_OFF}
     fdctl status
     fdctl devices
     fdctl limits          # the safety policy this unit actually loaded

${C_BOLD}5. Does it work with nothing attached?${C_OFF}
     FIELDDECK_SIM=1 fdctl --help
   And with simulated hardware, in a private instance that leaves the installed
   daemon running and touches nothing under /run, /etc or /var:
     export FIELDDECK_HOME=~/.fielddeck-sim
     FIELDDECK_SIM=1 ${VENV}/bin/instrumentd &
     FIELDDECK_HOME=~/.fielddeck-sim ${VENV}/bin/fdctl devices
   Stop it with 'kill %1' and remove ~/.fielddeck-sim when you are done.

   Do NOT expect 'systemctl stop instrumentd' followed by running instrumentd as
   ${FD_USER} to work: /run/fielddeck is created by the unit's RuntimeDirectory=
   and is removed the moment the unit stops, so the daemon then has nowhere to
   put its socket and exits with a ConfigurationError.

${C_BOLD}6. Everything at once${C_OFF}
     sudo fielddeck-preflight

${C_BOLD}7. The panel${C_OFF}
$( (( WITH_KIOSK )) && cat <<KIOSKV
     sudo systemctl start fielddeck-kiosk    # takes over tty1
     journalctl -u fielddeck-kiosk -n 40 --no-pager
   Or without X, over SSH, for the same four windows:
     fielddeck-session
KIOSKV
)
$( (( ! WITH_KIOSK )) && printf '     skipped (--no-kiosk). Over SSH: fielddeck-session\n' )

${C_BOLD}Deliberately still your job${C_OFF}
  * CAN interfaces stay down until you bring them up:
      sudo ip link set can0 up type can bitrate 500000 listen-only on
    Drop 'listen-only on' only when you intend to transmit.
  * Panel dtoverlay in /boot/firmware/config.txt.
  * Touch calibration: see ${XORG_CONF}.

To remove: sudo ${SOURCE_DIR}/scripts/uninstall.sh   (sessions are kept)

VERIFY
}

# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

main() {
  STAGING="$(mktemp -d)"
  announce_plan
  check_root
  check_platform
  install_apt
  install_user
  install_python
  install_directories
  install_udev
  install_kiosk_assets
  install_units
  if (( DRY_RUN )); then
    printf '\n%sDry run complete. Nothing was changed.%s\n\n' "$C_BOLD" "$C_OFF"
  else
    print_verification
  fi
}

main "$@"
