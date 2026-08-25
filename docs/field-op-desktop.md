# The Field-Op desktop

FieldDeck proper is a daemon and an 80×25 panel. That is the right shape when
the Pi is wired into a bench and the only interface is a 3.5" screen.

It is the wrong shape the moment you plug in an HDMI monitor, a keyboard and a
mouse — which is exactly what you do when you carry the unit back to a desk and
want to read a datasheet, open a capture in Wireshark, or write up what you
found. `scripts/field-op-desktop.sh` installs that second face.

```bash
sudo scripts/field-op-desktop.sh --dry-run    # read what it intends to do
sudo scripts/field-op-desktop.sh --user pi    # then let it
```

It is deliberately **not** part of `scripts/install.sh`. A bench unit does not
need a desktop, and a desktop must never become a prerequisite for the daemon.

---

## What you get

A stacking window manager that behaves the way a mouse-and-keyboard user
expects: draggable windows, a taskbar along the bottom, a Start button.

| | |
|---|---|
| **Window manager** | Openbox — stacking, decorated windows, near-zero CPU |
| **Taskbar** | tint2 — Start button, task list, system tray, clock |
| **Start menu** | rofi, searchable: click Start or press `Super`+`space` |
| **Desktop** | PCManFM — wallpaper and icons |
| **Terminal** | lxterminal |
| **Field toolkit** | nmap, wireshark/tshark, aircrack-ng, mtr, btop, nnn, fzf, ripgrep, tmux |
| **Productivity** | Firefox ESR, VLC, Zathura, Geany, LibreOffice Writer/Calc |

Log out and pick **Field-Op** at the greeter, or run `startx` from a tty. Both
routes end in `openbox-session`, which sources `~/.config/openbox/autostart`, so
there is one place that defines what the session runs.

### Keys

| Key | Action |
|---|---|
| `Super`+`a` | Field-Op menu (also has FieldDeck and Field Tools submenus) |
| `Super`+`space` | Start menu / run anything |
| `Super`+`Return` | Terminal |
| `Super`+`e` | Files |
| `Super`+`f` | Browser |
| `Super`+`d` | Show desktop |

The menu carries a **FieldDeck** submenu — `fdctl status`, the HMI, the tmux
session, the daemon log, and preflight — so the bench side is reachable without
remembering command names.

---

## Things that look obvious and are wrong

Four of these cost real time, so they are written down rather than rediscovered.

**Right-click on the desktop is not the Start menu.** `pcmanfm --desktop` owns
the root window and answers that click itself. PCManFM has a `show_wm_menu`
option that is supposed to hand the click to the window manager; the Raspberry
Pi build (1.4.0-1+rpt9) accepts the key and ignores it. The script sets it
anyway — harmless where ignored, correct where honoured — and puts the menu on
a Start button and on `Super`+`a`, neither of which depends on PCManFM. Desktop
right-click stays PCManFM's, which is useful in its own right.

**`foot` cannot be the terminal.** It is a Wayland-native terminal and this is
an X11 session. A menu entry calling `foot` fails silently.

**tint2 background ids are positional and order-sensitive.** Id 0 is built in and
fully transparent, so declaring your own transparent block first shifts every
other id by one. And `*_background_id` is resolved at parse time against the
backgrounds seen *so far*, so definitions must appear above every reference. Get
either wrong and the panel renders transparent-on-black, which looks exactly
like "tint2 did not start". Similarly the taskbar font key is `task_font`; a
`font` key inside the `taskbar` block is ignored.

**Openbox menu files need `<openbox_menu>` as the root element**, not `<menu>`.
Openbox falls back to its stock menu when the root element is wrong, and prints
nothing.

---

## ZRAM

Raspberry Pi OS already ships zram via the `rpi-swap` package's systemd
generator — check with `swapon --show` before adding anything. The widely-copied
`zram-tools` recipe is actively harmful on such a system: `zramswap.service`
tries to `mkswap` a `/dev/zram0` that is already mounted, fails, and leaves a red
unit on every boot while the working zram sits next to it. The script detects an
existing zram device and leaves it alone.

That recipe also names the wrong files. On Debian the package config is
`/etc/default/zramswap` and the unit is `zramswap.service` — there is no
`/etc/zram.conf` and no `zram-config` service, so following that advice edits a
file nothing reads.

---

## Capture permissions

`tshark` and `wireshark` capture as a non-root user only through the `wireshark`
group. The script preseeds the debconf question and adds the operator to the
group; **it takes effect at the next login**, not in the shell that ran the
installer. Until then Wireshark opens and lists no interfaces, which reads as a
broken install.
