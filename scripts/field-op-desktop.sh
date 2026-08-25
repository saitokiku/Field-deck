#!/usr/bin/env bash
#
# FieldDeck "Field-Op" desktop — the pluggable side of the unit.
#
# FieldDeck proper is a headless daemon plus a 80x25 panel. That is the right
# shape when the Pi is wired into a bench. It is the wrong shape the moment you
# plug in an HDMI monitor, a keyboard and a mouse and want to read a datasheet,
# open a capture in Wireshark, or write up what you just found.
#
# This script installs that second face: a stacking window manager (Openbox), a
# taskbar (tint2), a file manager (PCManFM) and a field toolkit, as a session you
# pick at the login greeter. It does not replace or disturb the existing session
# choices, and it does not touch instrumentd.
#
# It is deliberately separate from scripts/install.sh. A bench unit does not need
# a desktop, and a desktop must never be a prerequisite for the daemon.
#
# Run with --dry-run first. It prints every command and changes nothing.

set -Eeuo pipefail

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

OPERATOR="${SUDO_USER:-$(id -un)}"
DRY_RUN=0
WITH_APT=1
WITH_ZRAM=1
ASSUME_YES=0

TERMINAL="lxterminal"        # X11. NOT foot: foot is Wayland-native and cannot
                             # run under an Openbox X11 session.
XSESSION="/usr/share/xsessions/field-op.desktop"

# ---------------------------------------------------------------------------
# Output — same vocabulary as scripts/install.sh
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

run() {
  if (( DRY_RUN )); then
    printf '    %swould run:%s %s\n' "$C_DIM" "$C_OFF" "$(printf '%q ' "$@")"
    return 0
  fi
  "$@"
}

# Write a file as $OPERATOR, honouring --dry-run. Content arrives on stdin.
write_user_file() {
  local path="$1" mode="${2:-0644}" content
  content="$(cat)"
  if (( DRY_RUN )); then
    printf '    %swould write:%s %s (%s bytes, mode %s)\n' "$C_DIM" "$C_OFF" "$path" "${#content}" "$mode"
    return 0
  fi
  install -d -o "$OPERATOR" -g "$OPERATOR" -m 0755 "$(dirname "$path")"
  printf '%s\n' "$content" > "$path"
  chown "$OPERATOR:$OPERATOR" "$path"
  chmod "$mode" "$path"
}

usage() {
  cat <<'USAGE'
FieldDeck Field-Op desktop installer

  sudo scripts/field-op-desktop.sh [options]

Options:
  --user NAME     Operator whose session is configured. Defaults to $SUDO_USER.
  --no-apt        Skip apt entirely; only write configuration.
  --no-zram       Do not install or enable zram swap.
  --dry-run       Print every command and file instead of applying. Needs no root.
  -y, --assume-yes  Do not prompt.
  -h, --help      This text.

What it installs, and why each group is here:
  UI            openbox tint2 rofi pcmanfm feh lxappearance obconf lxterminal
  productivity  firefox-esr vlc zathura geany libreoffice-writer/-calc
                pavucontrol network-manager-gnome
  field toolkit nmap wireshark tshark aircrack-ng tmux htop btop nnn fzf
                ripgrep mtr-tiny net-tools rsync
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --user) OPERATOR="${2:?--user needs a name}"; shift 2 ;;
    --no-apt) WITH_APT=0; shift ;;
    --no-zram) WITH_ZRAM=0; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    -y|--assume-yes) ASSUME_YES=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown option: $1 (try --help)" ;;
  esac
done

if (( DRY_RUN )); then
  say "dry run: skipping the root check"
elif [[ "$EUID" -ne 0 ]]; then
  die "run me with sudo (or pass --dry-run, which needs no root)"
fi

id -u "$OPERATOR" >/dev/null 2>&1 || die "no such user: $OPERATOR (pass --user NAME)"
HOME_DIR="$(getent passwd "$OPERATOR" | cut -d: -f6)"
[[ -d "$HOME_DIR" ]] || die "operator $OPERATOR has no home directory at ${HOME_DIR:-<unset>}"

CFG="$HOME_DIR/.config"

# ---------------------------------------------------------------------------

step "Field-Op desktop plan"
say  "operator            $OPERATOR ($HOME_DIR)"
say  "terminal            $TERMINAL"
say  "session entry       $XSESSION"
say  "apt                 $( ((WITH_APT)) && echo yes || echo 'no (--no-apt)' )"
say  "zram swap           $( ((WITH_ZRAM)) && echo 'yes, 50% of RAM' || echo 'no (--no-zram)' )"
(( DRY_RUN )) && say "dry run             YES — nothing will be changed"

# ---------------------------------------------------------------------------

if (( WITH_APT )); then
  step "Installing packages"
  say "Answering the Wireshark debconf question up front, so a non-interactive"
  say "run cannot stall on it: yes, non-root members of group 'wireshark' may"
  say "capture. That is the whole point of installing it on a field unit."
  if (( ! DRY_RUN )); then
    echo "wireshark-common wireshark-common/install-setuid boolean true" | debconf-set-selections
  else
    note "would preseed wireshark-common/install-setuid=true"
  fi

  run env DEBIAN_FRONTEND=noninteractive apt-get update

  UI_PKGS=(xorg xinit openbox obconf tint2 rofi pcmanfm feh lightdm lxappearance lxterminal)
  APP_PKGS=(firefox-esr vlc zathura geany libreoffice-writer libreoffice-calc
            pavucontrol network-manager-gnome)
  TOOL_PKGS=(nmap wireshark tshark aircrack-ng build-essential python3-pip git tmux
             htop btop nnn fzf ripgrep net-tools iputils-ping curl wget rsync mtr-tiny)

  say "UI:        ${UI_PKGS[*]}"
  say "apps:      ${APP_PKGS[*]}"
  say "toolkit:   ${TOOL_PKGS[*]}"
  run env DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      "${UI_PKGS[@]}" "${APP_PKGS[@]}" "${TOOL_PKGS[@]}"
  ok "packages installed"
else
  step "Skipping apt (--no-apt)"
fi

# ---------------------------------------------------------------------------

step "Openbox menu"
say "The root element is <openbox_menu>, not <menu>. Openbox silently falls back"
say "to its stock menu when the root element is wrong, which looks exactly like"
say "'my menu did not apply' with no error anywhere."

write_user_file "$CFG/openbox/menu.xml" <<MENU_EOF
<?xml version="1.0" encoding="UTF-8"?>
<openbox_menu xmlns="http://openbox.org/">

<menu id="fieldops-menu" label="Field Tools">
  <item label="Network scan (nmap)">
    <action name="Execute"><command>${TERMINAL} -e "bash -lc 'nmap --help | less'"</command></action>
  </item>
  <item label="Packet capture (tshark)">
    <action name="Execute"><command>${TERMINAL} -e "bash -lc 'tshark -D; echo; read -p \"interface: \" i; tshark -i \\\$i'"</command></action>
  </item>
  <item label="Wireshark (GUI)">
    <action name="Execute"><command>wireshark</command></action>
  </item>
  <item label="System monitor (btop)">
    <action name="Execute"><command>${TERMINAL} -e btop</command></action>
  </item>
  <item label="Route trace (mtr)">
    <action name="Execute"><command>${TERMINAL} -e "bash -lc 'read -p \"host: \" h; mtr \\\$h'"</command></action>
  </item>
  <item label="File manager (nnn)">
    <action name="Execute"><command>${TERMINAL} -e nnn</command></action>
  </item>
</menu>

<menu id="fielddeck-menu" label="FieldDeck">
  <item label="Console (fdctl status)">
    <action name="Execute"><command>${TERMINAL} -e "bash -lc 'fdctl status; exec bash'"</command></action>
  </item>
  <item label="Panel (HMI)">
    <action name="Execute"><command>${TERMINAL} -e fielddeck-ui</command></action>
  </item>
  <item label="Full session (tmux)">
    <action name="Execute"><command>${TERMINAL} -e fielddeck-session</command></action>
  </item>
  <item label="Daemon log">
    <action name="Execute"><command>${TERMINAL} -e "bash -lc 'journalctl -u instrumentd -f'"</command></action>
  </item>
  <item label="Preflight check">
    <action name="Execute"><command>${TERMINAL} -e "bash -lc 'sudo fielddeck-preflight; exec bash'"</command></action>
  </item>
</menu>

<menu id="root-menu" label="Field-Op">
  <item label="Terminal">
    <action name="Execute"><command>${TERMINAL}</command></action>
  </item>
  <item label="Files">
    <action name="Execute"><command>pcmanfm</command></action>
  </item>
  <item label="Web browser">
    <action name="Execute"><command>firefox-esr</command></action>
  </item>
  <item label="Text editor">
    <action name="Execute"><command>geany</command></action>
  </item>
  <item label="Run… (rofi)">
    <action name="Execute"><command>rofi -show drun</command></action>
  </item>
  <separator />
  <menu id="fielddeck-menu" />
  <menu id="fieldops-menu" />
  <separator />
  <item label="Volume">
    <action name="Execute"><command>pavucontrol</command></action>
  </item>
  <item label="Network">
    <action name="Execute"><command>nm-connection-editor</command></action>
  </item>
  <item label="Appearance">
    <action name="Execute"><command>lxappearance</command></action>
  </item>
  <item label="Window manager settings">
    <action name="Execute"><command>obconf</command></action>
  </item>
  <separator />
  <item label="Reconfigure Openbox">
    <action name="Reconfigure" />
  </item>
  <separator />
  <item label="Log out">
    <action name="Exit" />
  </item>
  <item label="Reboot">
    <action name="Execute"><command>sudo systemctl reboot</command></action>
  </item>
  <item label="Shut down">
    <action name="Execute"><command>sudo systemctl poweroff</command></action>
  </item>
</menu>

</openbox_menu>
MENU_EOF
ok "$CFG/openbox/menu.xml"

# ---------------------------------------------------------------------------

step "Openbox rc.xml"
say "Started from the packaged default so every unspecified setting keeps its"
say "stock value, then the Field-Op keybindings are spliced into <keyboard>."

if (( DRY_RUN )); then
  note "would copy /etc/xdg/openbox/rc.xml and add W-Return / W-space / W-e / W-f keybinds"
else
  install -d -o "$OPERATOR" -g "$OPERATOR" -m 0755 "$CFG/openbox"
  if [[ ! -f "$CFG/openbox/rc.xml" ]]; then
    cp /etc/xdg/openbox/rc.xml "$CFG/openbox/rc.xml"
  fi
  python3 - "$CFG/openbox/rc.xml" "$TERMINAL" <<'PY'
import shutil, sys, xml.etree.ElementTree as ET

path, terminal = sys.argv[1], sys.argv[2]
NS = "http://openbox.org/3.4/rc"
ET.register_namespace("", NS)
tree = ET.parse(path)
root = tree.getroot()

kb = root.find(f"{{{NS}}}keyboard")
if kb is None:
    kb = ET.SubElement(root, f"{{{NS}}}keyboard")

# W-d is deliberately absent: Openbox ships it as ToggleShowDesktop, which is
# genuinely useful, so rofi gets W-space instead of fighting it.
BINDS = {
    "W-Return": terminal,
    "W-space":  "rofi -show drun",
    "W-e":      "pcmanfm",
    "W-f":      "firefox-esr",
}

# ShowMenu is not an Execute action, so it is handled separately below.
MENU_KEY = "W-a"

def live(cmd):
    """Is the binary this keybind invokes actually on this machine?"""
    return shutil.which(cmd.split()[0]) is not None

added, replaced, kept = [], [], []
by_key = {k.get("key"): k for k in kb.findall(f"{{{NS}}}keybind")}

for key, cmd in BINDS.items():
    existing = by_key.get(key)
    if existing is not None:
        cur = next((c.text for c in existing.iter(f"{{{NS}}}command") if c.text), None)
        # Leave a working bind alone; a bind onto a missing binary is dead weight
        # (Debian's stock rc.xml points W-e at kfmclient, which is KDE-only).
        if cur is None or live(cur):
            kept.append(f"{key} -> {cur or 'non-Execute action'}")
            continue
        kb.remove(existing)
        replaced.append(f"{key}: {cur} (missing) -> {cmd}")
    else:
        added.append(f"{key} -> {cmd}")
    b = ET.SubElement(kb, f"{{{NS}}}keybind", {"key": key})
    a = ET.SubElement(b, f"{{{NS}}}action", {"name": "Execute"})
    ET.SubElement(a, f"{{{NS}}}command").text = cmd

# Any OTHER stock bind aimed at a missing binary is reported, not silently fixed:
# it is the operator's desktop, and a surprise rebinding is worse than a dead key.
dead = []
for k in kb.findall(f"{{{NS}}}keybind"):
    if k.get("key") in BINDS:
        continue
    for c in k.iter(f"{{{NS}}}command"):
        if c.text and not live(c.text):
            dead.append(f"{k.get('key')} -> {c.text.split()[0]}")

# A keyboard route to the root menu, so it stays reachable if any desktop
# manager ever takes the right-click again.
if MENU_KEY not in {k.get("key") for k in kb.findall(f"{{{NS}}}keybind")}:
    b = ET.SubElement(kb, f"{{{NS}}}keybind", {"key": MENU_KEY})
    a = ET.SubElement(b, f"{{{NS}}}action", {"name": "ShowMenu"})
    ET.SubElement(a, f"{{{NS}}}menu").text = "root-menu"
    added.append(f"{MENU_KEY} -> root-menu")

tree.write(path, encoding="UTF-8", xml_declaration=True)
for label, items in (("added", added), ("replaced", replaced), ("kept", kept)):
    if items:
        print(f"    {label}: " + "; ".join(items))
if dead:
    print(f"    note: {len(dead)} stock keybind(s) still point at software that is "
          f"not installed: " + "; ".join(dead))
PY
  chown "$OPERATOR:$OPERATOR" "$CFG/openbox/rc.xml"
fi
ok "$CFG/openbox/rc.xml"

# ---------------------------------------------------------------------------

step "Openbox autostart"
say "openbox-session sources this, so every entry point — the greeter session,"
say "startx from a tty — brings up the same desktop. One file, not three."

write_user_file "$CFG/openbox/autostart" 0755 <<'AUTOSTART_EOF'
# Field-Op session components.
#
# Each is guarded: a missing optional package must degrade the desktop, never
# prevent it from starting. A session that dies because nm-applet is absent is
# a session you cannot log in to fix.

command -v tint2      >/dev/null 2>&1 && tint2 &
command -v pcmanfm    >/dev/null 2>&1 && pcmanfm --desktop &
command -v nm-applet  >/dev/null 2>&1 && nm-applet &

# Blank the screen but never suspend it: a field unit mid-capture that DPMS-offs
# is fine, one that suspends is a lost session.
if command -v xset >/dev/null 2>&1; then
  xset s 600
  xset -dpms
fi
AUTOSTART_EOF
ok "$CFG/openbox/autostart"

# ---------------------------------------------------------------------------

step "tint2 taskbar"
say "Keys are the ones tint2 17 actually reads — 'task_font', not a 'font' key"
say "inside the taskbar block, which tint2 ignores while looking like it worked."

write_user_file "$CFG/tint2/tint2rc" <<'TINT_EOF'
# Field-Op panel: bottom, full width, classic grey.
#
# Two ordering rules that tint2 enforces silently, so getting them wrong looks
# like "tint2 ignored my config" rather than an error:
#
#  1. Background blocks must be declared BEFORE anything references them.
#     tint2 resolves *_background_id at parse time against the backgrounds it
#     has seen so far, so a reference above the definitions resolves to 0.
#  2. Background id 0 is built in and fully transparent. Declaring your own
#     transparent block first shifts every subsequent id by one.
#
# Hence: definitions first, and no hand-rolled transparent block.
# 1 = panel, 2 = idle task, 3 = active task

rounded = 0
border_width = 1
background_color = #c0c0c0 100
border_color = #808080 100

rounded = 0
border_width = 1
background_color = #d4d0c8 100
border_color = #dfdfdf 100

rounded = 0
border_width = 2
background_color = #a8a8a8 100
border_color = #404040 100

panel_items = LTSC
panel_monitor = all
panel_position = bottom center horizontal
panel_size = 100% 30
panel_margin = 0 0
panel_padding = 2 2 4
panel_background_id = 1
wm_menu = 1
panel_layer = bottom

launcher_padding = 4 2 4
launcher_background_id = 0
launcher_icon_size = 22
launcher_icon_theme = PiXflat
launcher_tooltip = 1
launcher_item_app = ~/.local/share/applications/field-op-start.desktop

taskbar_mode = single_desktop
taskbar_padding = 2 0 4
taskbar_background_id = 0
taskbar_active_background_id = 0

task_text = 1
task_icon = 1
task_centered = 0
task_maximum_size = 220 26
task_padding = 4 2
task_font = Sans 10
task_font_color = #101010 100
task_background_id = 2
task_active_background_id = 3

systray_padding = 4 2 4
systray_background_id = 0
systray_icon_size = 20

time1_format = %H:%M
time1_font = Sans 10
time2_format = %Y-%m-%d
time2_font = Sans 7
clock_font_color = #101010 100
clock_padding = 6 0
clock_background_id = 0
TINT_EOF
ok "$CFG/tint2/tint2rc"

# ---------------------------------------------------------------------------

step "Session entry and .xinitrc"
say "A named 'Field-Op' entry at the greeter, alongside — not instead of — the"
say "sessions already installed. The stock Openbox and labwc entries are untouched."

if (( DRY_RUN )); then
  note "would write $XSESSION"
else
  install -d -m 0755 /usr/share/xsessions
  cat > "$XSESSION" <<'DESKTOP_EOF'
[Desktop Entry]
Name=Field-Op
Comment=Openbox desktop with the FieldDeck field toolkit
Exec=openbox-session
TryExec=openbox-session
Icon=openbox
Type=Application
DesktopNames=Openbox
DESKTOP_EOF
  chmod 0644 "$XSESSION"
fi
ok "$XSESSION"

write_user_file "$HOME_DIR/.xinitrc" 0755 <<'XINIT_EOF'
#!/bin/sh
# startx from a tty lands in exactly the same session as the greeter does,
# because both end up in openbox-session, which sources ~/.config/openbox/autostart.
exec openbox-session
XINIT_EOF
ok "$HOME_DIR/.xinitrc"

# ---------------------------------------------------------------------------

step "Start menu"
say "A right-click on the desktop cannot be the Start menu here: pcmanfm owns"
say "the root window and answers that click itself. Its show_wm_menu option is"
say "supposed to hand the click to the window manager, and this Raspberry Pi"
say "build of pcmanfm (1.4.0-1+rpt9) accepts the key and ignores it."
say ""
say "So the menu gets two routes that do not depend on pcmanfm at all:"
say "  * a Start button at the left of the taskbar, opening a searchable list"
say "  * Super+a, which opens the Openbox root menu anywhere"
say "Right-click on the desktop stays PCManFM's — New Folder, Desktop"
say "Preferences and friends — which is useful in its own right."

write_user_file "$HOME_DIR/.local/share/applications/field-op-start.desktop" 0644 <<'START_EOF'
[Desktop Entry]
Type=Application
Name=Start
Comment=Search and launch anything installed
Exec=rofi -show drun -show-icons
Icon=start-here
Terminal=false
Categories=System;
START_EOF
ok "$HOME_DIR/.local/share/applications/field-op-start.desktop"

# show_wm_menu is still set: harmless where ignored, and correct on any build
# that does honour it.
PCMANFM_PROFILE="$CFG/pcmanfm/default"
if (( DRY_RUN )); then
  note "would set show_wm_menu=1 in $PCMANFM_PROFILE/desktop-items-*.conf"
else
  install -d -o "$OPERATOR" -g "$OPERATOR" -m 0755 "$PCMANFM_PROFILE"
  shopt -s nullglob
  ITEM_CONFS=("$PCMANFM_PROFILE"/desktop-items-*.conf)
  shopt -u nullglob
  for conf in "${ITEM_CONFS[@]}"; do
    if grep -qE '^\s*show_wm_menu\s*=' "$conf"; then
      sed -i -E 's|^\s*show_wm_menu\s*=.*|show_wm_menu=1|' "$conf"
    elif grep -qE '^\[\*\]' "$conf"; then
      sed -i '0,/^\[\*\]/s//[*]\nshow_wm_menu=1/' "$conf"
    else
      printf '[*]\nshow_wm_menu=1\n' >> "$conf"
    fi
  done
  (( ${#ITEM_CONFS[@]} )) && ok "show_wm_menu=1 set on ${#ITEM_CONFS[@]} monitor profile(s)"
fi

# ---------------------------------------------------------------------------

step "Capture permissions"
say "tshark and wireshark capture as a non-root user only via group 'wireshark'."
say "Without this the GUI opens and shows no interfaces, which reads as broken."
if getent group wireshark >/dev/null 2>&1; then
  if id -nG "$OPERATOR" | tr ' ' '\n' | grep -qx wireshark; then
    ok "$OPERATOR is already in 'wireshark'"
  else
    run usermod -aG wireshark "$OPERATOR"
    ok "$OPERATOR added to 'wireshark' (takes effect at next login)"
  fi
else
  warn "group 'wireshark' does not exist — is wireshark-common installed?"
fi

# ---------------------------------------------------------------------------

if (( WITH_ZRAM )); then
  step "ZRAM swap"

  # Raspberry Pi OS (Bookworm onward) ships the `rpi-swap` package, whose
  # systemd generator already creates a zram swap device with writeback. On such
  # a system the popular `zram-tools` advice is not just redundant, it is
  # harmful: zramswap.service tries to mkswap a /dev/zram0 that is already
  # mounted, fails, and leaves a red unit on every single boot while the working
  # zram sits right next to it looking identical.
  #
  # So: measure first, and only provide zram if nothing already does.

  EXISTING_ZRAM="$(swapon --noheadings --show=NAME,SIZE 2>/dev/null | awk '$1 ~ /zram/ {print $1" "$2}')"

  if [[ -n "$EXISTING_ZRAM" ]]; then
    ok "zram swap is already active: $EXISTING_ZRAM"
    if [[ -e /usr/lib/systemd/system-generators/rpi-swap-generator ]]; then
      say "Provided by rpi-swap's generator — see rpi-swap-generator(8)."
    elif [[ -e /usr/lib/systemd/system-generators/zram-generator ]]; then
      say "Provided by systemd-zram-generator — see zram-generator.conf(5)."
    fi
    say "Leaving it alone. Installing zram-tools on top of this creates a"
    say "zramswap.service that fails at every boot and changes nothing."
    if dpkg -s zram-tools >/dev/null 2>&1; then
      warn "zram-tools IS installed and will conflict with the above."
      say "Remove it with:  sudo systemctl disable --now zramswap.service &&"
      say "                 sudo apt-get purge -y zram-tools"
    fi
  else
    say "No zram swap found. Installing zram-tools to provide it."
    run env DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends zram-tools
    if (( DRY_RUN )); then
      note "would set PERCENT=50 / ALGO=zstd in /etc/default/zramswap and enable zramswap.service"
    elif [[ -f /etc/default/zramswap ]]; then
      # Debian ships /etc/default/zramswap and zramswap.service. There is no
      # /etc/zram.conf and no zram-config service on Debian; writing those does
      # nothing at all.
      sed -i -E 's|^\s*#?\s*PERCENT=.*|PERCENT=50|' /etc/default/zramswap
      grep -qE '^PERCENT=' /etc/default/zramswap || printf 'PERCENT=50\n' >> /etc/default/zramswap
      if grep -q zstd /sys/block/zram0/comp_algorithm 2>/dev/null; then
        sed -i -E 's|^\s*#?\s*ALGO=.*|ALGO=zstd|' /etc/default/zramswap
      fi
      systemctl enable --now zramswap.service && ok "zramswap enabled at 50% of RAM"
    else
      warn "/etc/default/zramswap missing after install — skipping"
    fi
  fi
fi

# ---------------------------------------------------------------------------

step "Done"
if (( DRY_RUN )); then
  say "Dry run complete. Nothing was changed."
else
  say "Log out and pick 'Field-Op' at the greeter, or run 'startx' from a tty."
  say "Start button at the far left of the taskbar. Super+a menu, Super+Return terminal, Super+space run,"
  say "Super+e files, Super+f browser, Super+a menu, Super+d show desktop."
  note "Group changes (wireshark) apply at your next login, not to this shell."
fi
