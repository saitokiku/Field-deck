#!/usr/bin/env bash
#
# FieldDeck preflight — "is this unit actually working?"
#
# Run it after an install, after a reboot, after plugging something in, and
# before you drive to site. Every line is one question with a PASS or a FAIL and
# a specific next step, because a checklist that says "something is wrong" is
# not a checklist.
#
#     fielddeck-preflight
#
# Run it as the operator, NOT as root. Half of what it checks is whether *you*
# can reach the daemon, and root can always reach everything. It says so if you
# run it with sudo anyway.
#
# Exit status: 0 if nothing FAILed. Warnings do not fail the run — a missing
# openocd is not a broken unit, it is a unit that cannot flash ARM targets.

set -uo pipefail   # deliberately no -e: a failing check is data, not an abort

# Overridable so this can be pointed at a development tree: everything below
# reads the layout from here rather than assuming it.
INSTALL_ENV="${FIELDDECK_INSTALL_ENV:-/etc/fielddeck/install.env}"
CONFIG_DIR="/etc/fielddeck"
STATE_DIR="/var/lib/fielddeck"
RUNTIME_DIR="/run/fielddeck"
SOCKET="$RUNTIME_DIR/instrumentd.sock"
AI_SOCKET="$RUNTIME_DIR/instrumentd-ai.sock"
FD_GROUP="fielddeck"

PASS_N=0; FAIL_N=0; WARN_N=0

if [[ -t 1 && "${NO_COLOR:-}" == "" ]]; then
  C_OK=$'\033[32m'; C_BAD=$'\033[31m'; C_WARN=$'\033[33m'; C_DIM=$'\033[2m'; C_BOLD=$'\033[1m'; C_OFF=$'\033[0m'
else
  C_OK=""; C_BAD=""; C_WARN=""; C_DIM=""; C_BOLD=""; C_OFF=""
fi

section() { printf '\n%s%s%s\n' "$C_BOLD" "$*" "$C_OFF"; }
pass() { PASS_N=$((PASS_N+1)); printf '  %s[PASS]%s %-34s %s\n' "$C_OK"   "$C_OFF" "$1" "${2:-}"; }
fail() { FAIL_N=$((FAIL_N+1)); printf '  %s[FAIL]%s %-34s %s\n' "$C_BAD"  "$C_OFF" "$1" "${2:-}"; }
warn() { WARN_N=$((WARN_N+1)); printf '  %s[WARN]%s %-34s %s\n' "$C_WARN" "$C_OFF" "$1" "${2:-}"; }
info() {                        printf '  %s[ .. ]%s %-34s %s\n' "$C_DIM" "$C_OFF" "$1" "${2:-}"; }
fixup(){ printf '         %s-> %s%s\n' "$C_DIM" "$*" "$C_OFF"; }

have() { command -v "$1" >/dev/null 2>&1; }

fd_env() {
  local key="$1" default="${2:-}" value=""
  [[ -r "$INSTALL_ENV" ]] && value="$(sed -n "s/^${key}=//p" "$INSTALL_ENV" | tail -n1)"
  printf '%s' "${value:-$default}"
}

# An exported FIELDDECK_PREFIX wins over the install record: someone who set it
# has a second environment in mind and this should check that one.
PREFIX="${FIELDDECK_PREFIX:-$(fd_env FIELDDECK_PREFIX /opt/fielddeck)}"
VENV="${FIELDDECK_VENV:-$(fd_env FIELDDECK_VENV "$PREFIX/venv")}"
PY="$VENV/bin/python"

# Group membership is a property of a login, so check the human's login, not
# sudo's. Getting this backwards is how an install looks fine to root and is
# unusable for the person who has to use it.
WHO="${SUDO_USER:-$(id -un)}"

# ---------------------------------------------------------------------------

printf '%sFieldDeck preflight%s   host=%s  user=%s  %s\n' \
  "$C_BOLD" "$C_OFF" "$(hostname 2>/dev/null || echo unknown)" "$WHO" "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
if [[ "$(id -u)" -eq 0 ]]; then
  printf '  %sRunning as root. Permission checks below are reported for %s, but\n' "$C_WARN" "$WHO"
  printf '  anything that "works" here may still fail for them. Re-run without sudo.%s\n' "$C_OFF"
fi

# ---------------------------------------------------------------------------
section "1. Installation"
# ---------------------------------------------------------------------------

if [[ -r "$INSTALL_ENV" ]]; then
  pass "install record" "$INSTALL_ENV"
else
  warn "install record" "$INSTALL_ENV missing"
  fixup "this unit was not installed by scripts/install.sh, or /etc/fielddeck was removed"
fi

if [[ -x "$PY" ]]; then
  pass "python environment" "$("$PY" --version 2>&1)"
else
  fail "python environment" "$PY is missing"
  fixup "sudo scripts/install.sh"
fi

if [[ -x "$VENV/bin/instrumentd" ]]; then
  pass "fielddeck package" "$("$VENV/bin/fdctl" --version 2>/dev/null || echo installed)"
else
  fail "fielddeck package" "no instrumentd in $VENV/bin"
fi

if [[ -f "$PREFIX/requirements.lock" ]]; then
  pass "dependency lockfile" "$(grep -vc '^#' "$PREFIX/requirements.lock" 2>/dev/null || echo 0) pinned packages"
else
  warn "dependency lockfile" "$PREFIX/requirements.lock missing — this unit is not reproducible"
fi

for cmd in fdctl instrumentd fielddeck-ui; do
  if have "$cmd"; then
    pass "$cmd on PATH" "$(command -v "$cmd")"
  else
    fail "$cmd on PATH" "not found"
    fixup "sudo ln -sfn $VENV/bin/$cmd /usr/local/bin/$cmd"
  fi
done

# ---------------------------------------------------------------------------
section "2. Accounts and groups"
# ---------------------------------------------------------------------------

if getent passwd fielddeck >/dev/null 2>&1; then
  pass "fielddeck user exists" "$(getent passwd fielddeck | cut -d: -f7) (no login shell expected)"
else
  fail "fielddeck user exists" "the daemon has no account to run as"
  fixup "sudo scripts/install.sh"
fi

if getent group "$FD_GROUP" >/dev/null 2>&1; then
  pass "fielddeck group exists" "members: $(getent group "$FD_GROUP" | cut -d: -f4)"
else
  fail "fielddeck group exists" "nothing can open the control socket"
fi

if [[ "$WHO" == "root" ]]; then
  # root opens the socket regardless of group, which is exactly why checking
  # root tells you nothing about whether the unit is usable.
  info "group membership" "checked account is root; re-run as the operator to test this properly"
elif id -nG "$WHO" 2>/dev/null | tr ' ' '\n' | grep -qx "$FD_GROUP"; then
  pass "$WHO in $FD_GROUP" "fdctl works without sudo"
else
  fail "$WHO in $FD_GROUP" "every fdctl call will be permission-denied"
  fixup "sudo usermod -aG $FD_GROUP $WHO   then log out and back in"
fi

for g in dialout plugdev i2c spi gpio video; do
  if getent group "$g" >/dev/null 2>&1; then
    if id -nG fielddeck 2>/dev/null | tr ' ' '\n' | grep -qx "$g"; then
      info "daemon in $g" "on-board $g devices reachable"
    else
      warn "daemon in $g" "on-board $g devices will be permission-denied"
      fixup "sudo usermod -aG $g fielddeck && sudo systemctl restart instrumentd"
    fi
  fi
done

if [[ "$WHO" == "root" ]]; then
  info "journal access" "root reads everything; check this as the operator instead"
elif id -nG "$WHO" 2>/dev/null | tr ' ' '\n' | grep -qxE 'systemd-journal|adm'; then
  pass "$WHO can read the journal" "the tmux LOG window will work"
else
  warn "$WHO can read the journal" "journalctl -u instrumentd will show nothing"
  fixup "sudo usermod -aG systemd-journal $WHO   then log out and back in"
fi

# ---------------------------------------------------------------------------
section "3. Daemon"
# ---------------------------------------------------------------------------

if have systemctl; then
  if systemctl cat instrumentd.service >/dev/null 2>&1; then
    pass "instrumentd unit installed" ""
  else
    fail "instrumentd unit installed" "/etc/systemd/system/instrumentd.service missing"
  fi

  if systemctl is-enabled --quiet instrumentd.service 2>/dev/null; then
    pass "instrumentd enabled" "starts at boot"
  else
    warn "instrumentd enabled" "will NOT start at boot"
    fixup "sudo systemctl enable instrumentd"
  fi

  if systemctl is-active --quiet instrumentd.service 2>/dev/null; then
    pass "instrumentd running" "$(systemctl show -p ActiveEnterTimestamp --value instrumentd.service 2>/dev/null)"
  else
    fail "instrumentd running" "state: $(systemctl is-active instrumentd.service 2>/dev/null || echo unknown)"
    fixup "journalctl -u instrumentd -n 30 --no-pager    # an invalid safety.yaml stops it on purpose"
  fi
else
  warn "systemd" "not available; skipping unit checks"
fi

# ---------------------------------------------------------------------------
section "4. Control socket"
# ---------------------------------------------------------------------------

if [[ -d "$RUNTIME_DIR" ]]; then
  mode="$(stat -c '%a' "$RUNTIME_DIR" 2>/dev/null)"
  owner="$(stat -c '%U:%G' "$RUNTIME_DIR" 2>/dev/null)"
  if [[ "$mode" == "750" && "$owner" == "fielddeck:$FD_GROUP" ]]; then
    pass "runtime directory" "$RUNTIME_DIR $owner $mode"
  else
    warn "runtime directory" "$RUNTIME_DIR is $owner $mode, expected fielddeck:$FD_GROUP 750"
  fi
else
  fail "runtime directory" "$RUNTIME_DIR does not exist"
  fixup "the daemon has never started; systemd creates it via RuntimeDirectory="
fi

if [[ -S "$SOCKET" ]]; then
  mode="$(stat -c '%a' "$SOCKET" 2>/dev/null)"
  owner="$(stat -c '%U:%G' "$SOCKET" 2>/dev/null)"
  if [[ "$mode" == "660" && "$owner" == "fielddeck:$FD_GROUP" ]]; then
    pass "control socket" "$owner $mode"
  else
    fail "control socket" "$owner $mode, expected fielddeck:$FD_GROUP 660"
    fixup "world-writable or wrongly grouped means anyone or no one can drive the hardware"
  fi
  if [[ -r "$SOCKET" && -w "$SOCKET" ]]; then
    pass "socket reachable by $WHO" ""
  else
    fail "socket reachable by $WHO" "no read/write access"
    fixup "group membership needs a fresh login: try 'newgrp $FD_GROUP'"
  fi
else
  fail "control socket" "$SOCKET is missing"
  fixup "sudo systemctl start instrumentd"
fi

if [[ -S "$AI_SOCKET" ]]; then
  info "restricted (AI) socket" "$(stat -c '%U:%G %a' "$AI_SOCKET" 2>/dev/null) — cannot arm anything"
fi

# ---------------------------------------------------------------------------
section "5. Does the CLI actually work?"
# ---------------------------------------------------------------------------

if have fdctl && [[ -S "$SOCKET" ]]; then
  out="$(timeout 15 fdctl status 2>&1)"
  rc=$?
  if (( rc == 0 )); then
    pass "fdctl status" "the daemon answered"
    printf '%s\n' "$out" | sed 's/^/         /' | head -14
  elif (( rc == 124 )); then
    fail "fdctl status" "timed out after 15s"
    fixup "the daemon is up but not answering; journalctl -u instrumentd -n 50"
  else
    fail "fdctl status" "exit $rc"
    printf '%s\n' "$out" | sed 's/^/         /' | head -6
  fi
else
  warn "fdctl status" "skipped: no fdctl or no socket"
fi

# ---------------------------------------------------------------------------
section "6. Configuration"
# ---------------------------------------------------------------------------

if [[ -f "$CONFIG_DIR/fielddeck.yaml" ]]; then
  pass "fielddeck.yaml present" "$(stat -c '%U:%G %a' "$CONFIG_DIR/fielddeck.yaml")"
else
  warn "fielddeck.yaml present" "using built-in defaults"
fi

if [[ -f "$CONFIG_DIR/safety.yaml" ]]; then
  pass "safety.yaml present" "$(stat -c '%U:%G %a' "$CONFIG_DIR/safety.yaml")"
  if [[ "$(stat -c '%U' "$CONFIG_DIR/safety.yaml")" != "root" ]]; then
    fail "safety.yaml ownership" "not root-owned — the daemon could rewrite its own limits"
    fixup "sudo chown root:$FD_GROUP $CONFIG_DIR/safety.yaml"
  fi
else
  warn "safety.yaml present" "using the built-in conservative limits"
  fixup "cp config/safety.example.yaml $CONFIG_DIR/safety.yaml and edit it deliberately"
fi

# The only check that matters: does the code load it? A YAML file that parses
# and a safety policy that loads are different questions.
if [[ -x "$PY" ]]; then
  out="$("$PY" - <<'EOF' 2>&1
from fielddeck.common.config import load_config, load_safety_config
from fielddeck.common.paths import default_paths
p = default_paths()
c = load_config(p)
s = load_safety_config(p)
print(f"OK {len(s.global_limits)} limits, {len(c.serial_presets)} serial presets, "
      f"{len(c.can_presets)} CAN presets, arm ttl {s.default_arm_ttl_s:g}s, "
      f"lease ttl {s.default_lease_ttl_s:g}s")
EOF
)"
  if [[ "$out" == OK* ]]; then
    pass "configuration loads" "${out#OK }"
  else
    fail "configuration loads" "the daemon will refuse to start"
    printf '%s\n' "$out" | sed 's/^/         /' | head -8
  fi
fi

# ---------------------------------------------------------------------------
section "7. Storage"
# ---------------------------------------------------------------------------

for d in "$STATE_DIR" "$STATE_DIR/sessions" "$STATE_DIR/firmware"; do
  if [[ -d "$d" ]]; then
    pass "$(basename "$d") directory" "$(stat -c '%U:%G %a' "$d") $d"
  else
    warn "$(basename "$d") directory" "$d missing"
  fi
done

if [[ -d "$STATE_DIR/sessions" ]]; then
  free_mb="$(df -Pm "$STATE_DIR/sessions" 2>/dev/null | awk 'NR==2 {print $4}')"
  floor="$( [[ -x "$PY" ]] && "$PY" -c "
from fielddeck.common.config import load_config
from fielddeck.common.paths import default_paths
print(load_config(default_paths()).storage.min_free_mb)" 2>/dev/null || echo 256)"
  if [[ -n "${free_mb:-}" ]] && (( free_mb > floor )); then
    pass "free space" "${free_mb} MB free, floor ${floor} MB"
  else
    fail "free space" "${free_mb:-?} MB free, below the ${floor} MB floor — new captures are refused"
    fixup "delete old sessions, or point storage.sessions_dir at an external disk"
  fi
fi

# ---------------------------------------------------------------------------
section "8. Device access (udev)"
# ---------------------------------------------------------------------------

if [[ -f /etc/udev/rules.d/99-fielddeck.rules ]]; then
  pass "udev rules installed" "$(grep -cvE '^\s*(#|$)' /etc/udev/rules.d/99-fielddeck.rules) rules"
else
  warn "udev rules installed" "USB adapters may be root-only"
  fixup "sudo install -m 0644 config/udev/99-fielddeck.rules /etc/udev/rules.d/ && sudo udevadm control --reload-rules"
fi

found_tty=0
for dev in /dev/ttyUSB* /dev/ttyACM* /dev/ttyAMA*; do
  [[ -e "$dev" ]] || continue
  found_tty=1
  grp="$(stat -c '%G' "$dev" 2>/dev/null)"
  if [[ "$grp" == "$FD_GROUP" || "$grp" == "dialout" ]]; then
    pass "serial $dev" "group $grp $(stat -c '%a' "$dev")"
  else
    warn "serial $dev" "group $grp — the daemon probably cannot open it"
    fixup "re-plug it, or: sudo udevadm trigger --subsystem-match=tty --action=change"
  fi
done
(( found_tty )) || info "serial ports" "none attached"

can_found=0
for iface in /sys/class/net/*; do
  [[ -r "$iface/type" ]] || continue
  [[ "$(cat "$iface/type" 2>/dev/null)" == "280" ]] || continue   # ARPHRD_CAN
  can_found=1
  name="$(basename "$iface")"
  state="$(cat "$iface/operstate" 2>/dev/null)"
  bitrate="$(cat "$iface/can_bittiming/bitrate" 2>/dev/null || echo unset)"
  if [[ "$state" == "up" ]]; then
    info "CAN $name" "up, bitrate $bitrate"
  else
    info "CAN $name" "$state — this is normal; FieldDeck never brings a link up"
    fixup "sudo ip link set $name up type can bitrate 500000 listen-only on"
  fi
done
(( can_found )) || info "CAN interfaces" "none present"

if have lsusb; then
  n="$(lsusb 2>/dev/null | wc -l)"
  info "USB devices" "$n enumerated (lsusb for detail)"
fi

# ---------------------------------------------------------------------------
section "9. Panel and touch"
# ---------------------------------------------------------------------------

fb_found=0
for fb in /dev/fb*; do
  [[ -e "$fb" ]] || continue
  fb_found=1
  name="$(cat "/sys/class/graphics/$(basename "$fb")/name" 2>/dev/null || echo unknown)"
  size="$(cat "/sys/class/graphics/$(basename "$fb")/virtual_size" 2>/dev/null || echo '?')"
  pass "framebuffer $fb" "$name ${size}"
done
if (( ! fb_found )); then
  warn "framebuffer" "no /dev/fb* — nothing can draw on a panel"
  fixup "add your panel's dtoverlay to /boot/firmware/config.txt (FieldDeck does not edit it)"
fi

# A touchscreen is an input device that reports absolute X/Y. /proc/bus/input
# is the cheapest place to see that without opening anything.
if [[ -r /proc/bus/input/devices ]]; then
  # Paragraph mode: /proc/bus/input/devices separates devices with a blank
  # line. A touch panel is the device that reports absolute axes AND has an
  # event handler; that is also true of a joystick, hence the cautious label.
  touch_names="$(awk 'BEGIN{RS="";FS="\n"}
    /B: ABS=/ && /Handlers=.*event/ {
      for (i = 1; i <= NF; i++)
        if ($i ~ /^N: Name=/) { n = $i; sub(/^N: Name="/, "", n); sub(/"$/, "", n); print n }
    }' /proc/bus/input/devices 2>/dev/null | head -5)"
  if [[ -n "$touch_names" ]]; then
    pass "absolute-position input" "$(printf '%s' "$touch_names" | tr '\n' ';')"
  else
    warn "absolute-position input" "no touchscreen-like device found"
    fixup "check the panel's touch controller overlay; evtest lists what the kernel sees"
  fi
fi

# ---------------------------------------------------------------------------
section "10. Kiosk"
# ---------------------------------------------------------------------------

if have systemctl && systemctl cat fielddeck-kiosk.service >/dev/null 2>&1; then
  if systemctl is-enabled --quiet fielddeck-kiosk.service 2>/dev/null; then
    pass "kiosk enabled" "the panel comes up at boot"
  else
    warn "kiosk enabled" "installed but not enabled"
  fi
  if systemctl is-active --quiet fielddeck-kiosk.service 2>/dev/null; then
    pass "kiosk running" ""
  else
    warn "kiosk running" "state: $(systemctl is-active fielddeck-kiosk.service 2>/dev/null)"
    fixup "journalctl -u fielddeck-kiosk -n 40 --no-pager"
  fi
  if [[ -f /etc/systemd/system/fielddeck-kiosk.service.d/10-operator.conf ]]; then
    pass "kiosk operator drop-in" "$(sed -n 's/^User=//p' /etc/systemd/system/fielddeck-kiosk.service.d/10-operator.conf)"
  else
    fail "kiosk operator drop-in" "the unit would try to run X as the nologin 'fielddeck' account"
    fixup "sudo scripts/install.sh --user $WHO"
  fi
  for f in /etc/fielddeck/xinitrc /etc/X11/xorg.conf.d/10-fielddeck-touch.conf /usr/local/lib/fielddeck/fielddeck-kiosk.sh; do
    if [[ -e "$f" ]]; then
      pass "$(basename "$f")" "$f"
    else
      fail "$(basename "$f")" "$f missing"
    fi
  done
  for b in Xorg xinit xterm xset; do
    if have "$b"; then
      pass "$b installed" "$(command -v "$b")"
    else
      fail "$b installed" "the kiosk cannot start without it"
    fi
  done
  if fc-list >/dev/null 2>&1 || [[ -d /usr/share/fonts/X11/misc ]]; then
    info "6x12 bitmap font" "check with: xlsfonts | grep -x 6x12   (package: xfonts-base)"
  fi
else
  info "kiosk" "not installed (--no-kiosk), or systemd unavailable"
fi

if have tmux; then
  pass "tmux installed" "$(tmux -V)"
else
  fail "tmux installed" "no session manager, no panel navigation"
fi
if [[ -f /etc/fielddeck/tmux.conf ]]; then
  pass "tmux.conf" "/etc/fielddeck/tmux.conf"
else
  warn "tmux.conf" "missing — tmux falls back to its defaults and the status bar is not touchable"
fi
if [[ -x /usr/local/lib/fielddeck/fielddeck-session.sh ]]; then
  pass "session bootstrap" "/usr/local/lib/fielddeck/fielddeck-session.sh"
else
  warn "session bootstrap" "missing — no guaranteed HMI/CLAUDE/SHELL/LOG windows"
fi

# ---------------------------------------------------------------------------
section "11. Optional tools"
#
# Each missing tool costs exactly one capability. None of them stop FieldDeck
# from running, and the actions that need them say so at the time.
# ---------------------------------------------------------------------------

check_tool() {
  local cmd="$1" what="$2" pkg="$3"
  if have "$cmd"; then
    pass "$cmd" "$what"
  else
    warn "$cmd" "missing — no $what"
    fixup "sudo apt install $pkg"
  fi
}

check_tool sigrok-cli "logic analyzer capture"        sigrok-cli
check_tool openocd    "SWD/JTAG flash and debug"      openocd
check_tool dfu-util   "USB DFU firmware load"         dfu-util
check_tool candump    "CAN cross-check from the shell" can-utils
check_tool i2cdetect  "I2C bus scan"                  i2c-tools
check_tool tcpdump    "packet capture"                tcpdump
check_tool ethtool    "ethernet link facts"           ethtool
check_tool lsusb      "USB enumeration from the shell" usbutils
have picotool  || info "picotool"  "absent — no RP2040 BOOTSEL flashing"
have pyocd     || info "pyocd"     "absent — CMSIS-DAP/J-Link falls back to openocd"
have esptool.py|| info "esptool.py" "absent — no ESP32/ESP8266 flashing"
have avrdude   || info "avrdude"   "absent — no AVR flashing"
have claude    || info "claude"    "absent — the CLAUDE tmux window shows an offline notice and stays open"

if have tcpdump; then
  caps="$(getcap "$(command -v tcpdump)" 2>/dev/null)"
  systemd_caps="$(systemctl show -p AmbientCapabilities --value instrumentd.service 2>/dev/null)"
  if [[ -z "$caps" && "$systemd_caps" != *net_raw* ]]; then
    info "net.capture" "will fail: the daemon has no CAP_NET_RAW (deliberate default)"
    fixup "systemctl edit instrumentd  ->  [Service] AmbientCapabilities=CAP_NET_RAW"
  fi
fi

# ---------------------------------------------------------------------------
section "12. Optional Python hardware libraries"
# ---------------------------------------------------------------------------

if [[ -x "$PY" ]]; then
  # Collected first, then looped over in this shell. Piping into 'while' would
  # put the counters in a subshell and every result would be silently lost from
  # the summary at the bottom.
  modules="$("$PY" - <<'EOF' 2>/dev/null
import importlib

for module, what in (
    ("serial", "serial / RS-232 / RS-485"),
    ("can", "SocketCAN and CAN FD"),
    ("cantools", "DBC decoding"),
    ("pymodbus", "Modbus RTU and TCP"),
    ("pyvisa", "SCPI bench instruments"),
    ("usb", "USBTMC over libusb"),
    ("numpy", "timing and signal analysis"),
    ("zstandard", "compressed event logs (gzip otherwise)"),
):
    try:
        importlib.import_module(module)
    except ImportError:
        print(f"{module} MISSING {what}")
    else:
        print(f"{module} OK {what}")
EOF
)"
  while read -r name state detail; do
    [[ -n "$name" ]] || continue
    if [[ "$state" == "OK" ]]; then
      pass "python: $name" "$detail"
    else
      warn "python: $name" "missing — no $detail"
      fixup "sudo $VENV/bin/pip install 'fielddeck[hardware]'"
    fi
  done <<< "$modules"
fi

# ---------------------------------------------------------------------------

printf '\n%s%d passed, %d warnings, %d failed%s\n' \
  "$C_BOLD" "$PASS_N" "$WARN_N" "$FAIL_N" "$C_OFF"

if (( FAIL_N )); then
  printf '%sThis unit is not ready.%s Fix the FAIL lines above, then run this again.\n\n' "$C_BAD" "$C_OFF"
  exit 1
fi
if (( WARN_N )); then
  printf '%sUsable, with the gaps listed above.%s Each warning costs one capability.\n\n' "$C_WARN" "$C_OFF"
else
  printf '%sReady.%s\n\n' "$C_OK" "$C_OFF"
fi
exit 0
