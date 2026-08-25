# OSOYOO 3.5" SPI panel (ILI9486 + XPT2046)

480×320, 40-pin GPIO header, no HDMI. The panel FieldDeck's 80×25 HMI is laid
out for. These notes also cover the Waveshare 3.5" (A) and the many MPI3501
clones, which are the same two controllers on the same pins.

**Do not run OSOYOO's driver script**, or any `LCD-show` derivative. They are
written for 32-bit Raspbian: they write to `/boot/config.txt` (the wrong path
since Bookworm — it is `/boot/firmware/config.txt`), install the `fbturbo` X
driver that no longer exists, and replace config.txt wholesale, which drops
`vc4-kms-v3d` and costs you HDMI and often the boot. Two `dtoverlay` lines do
the whole job on a current kernel.

## config.txt

Append to `/boot/firmware/config.txt`. Leave `vc4-kms-v3d` alone: HDMI stays
live as `/dev/fb0` and the panel arrives alongside it as `/dev/fb1`, so a wrong
setting here costs you a blank panel and never a blind Pi.

```
dtoverlay=fbtft,spi0-0,ili9486,bgr,speed=16000000,fps=30,rotate=90,txbuflen=32768,dc_pin=24,reset_pin=25
dtoverlay=ads7846,cs=1,penirq=17,penirq_pull=2,speed=1000000,swapxy=1,pmax=255,xohms=150,xmin=200,xmax=3900,ymin=200,ymax=3900
```

Wiring these assume, and it is the standard for this board:

| Signal | Pin |
|---|---|
| Display CS | CE0 (GPIO8) |
| Display DC/RS | GPIO24 |
| Display RESET | GPIO25 |
| Touch CS | CE1 (GPIO7) |
| Touch IRQ | GPIO17 |

`fbtft` claims CE0, so `/dev/spidev0.0` disappears once this is live. That is
expected — the display is using that chip select.

**Test both overlays before rebooting.** `dtoverlay` applies them live, so a
mistake costs seconds instead of a boot:

```bash
sudo dtoverlay fbtft spi0-0 ili9486 bgr speed=16000000 fps=30 rotate=90 \
    txbuflen=32768 dc_pin=24 reset_pin=25
dmesg | tail -3      # expect: graphics fb1: fb_ili9486 frame buffer, 480x320
```

Beware that an **unknown parameter makes the firmware skip the entire overlay at
boot**, silently. `keep_vref_on`, which appears in many copies of this recipe,
is not a valid `ads7846` parameter on current Raspberry Pi OS — include it and
you get no touch at all, with nothing in the log saying why. `dtoverlay` run by
hand *does* report it (`* Unknown parameter`), which is the other reason to test
live first.

Tuning: `rotate=` is 0/90/180/270 if the image is sideways; drop `bgr` if red
and blue are swapped; `swapxy`/`invx`/`invy` if touch axes are wrong. Fine
calibration is a `TransformationMatrix` in the touch config — the method is
written out in `config/xorg/10-fielddeck-touch.conf`.

## Putting the HMI on it

`scripts/install.sh` (without `--no-kiosk`) installs `fielddeck-kiosk.service`,
which starts Xorg → xterm → tmux → HMI. Two extra pieces are needed to aim it at
an SPI panel:

1. **Pin X to the panel.** Copy `config/xorg/20-fielddeck-panel.conf` and
   `config/xorg/10-fielddeck-touch.conf` into `/etc/X11/fielddeck.conf.d/`, then
   point the kiosk at that directory. It must *not* go in
   `/etc/X11/xorg.conf.d`: that is shared with every X server on the machine,
   and `Device`/`Screen` sections naming `/dev/fb1` are not advice — they move
   any X server that reads them onto the panel.

2. **Let Xorg open the framebuffer.** `/etc/X11/Xwrapper.config`:

   ```
   allowed_users=anybody
   needs_root_rights=yes
   ```

   `fbdev` opens `/dev/fb1` directly rather than going through a DRM device
   carrying logind ACLs. And `allowed_users=console` is not enough for a
   service-started X — Xorg.wrap's console test asks logind for an active seat
   session, which a systemd unit does not reliably satisfy even with
   `PAMName=login`. The symptom is a flat
   `Only console users are allowed to run the X server`.

   Note the interaction: `needs_root_rights=yes` is exactly what makes Xorg
   reject an absolute `-configdir`, so the directory in step 1 is passed
   relative (`fielddeck.conf.d`) and resolved under `/etc/X11`.

### Alongside a desktop

If the unit also runs a desktop, the kiosk's default `DISPLAY :0` is already
taken — by the desktop's Xorg, or by labwc's Xwayland — and the kiosk dies with
`Server is already active for display 0`. Use the drop-in in
`config/systemd/fielddeck-kiosk-panel.conf.example` to move it to `:1`. VT1
stays free because the unit already declares `Conflicts=getty@tty1.service`, so
the panel and an HDMI desktop run side by side.

## Verifying

```bash
cat /sys/class/graphics/fb0/name /sys/class/graphics/fb1/name   # vc4drmfb, fb_ili9486
grep -i ads7846 /proc/bus/input/devices                          # ADS7846 Touchscreen
systemctl status fielddeck-kiosk
grep -E "FBDEV|\(EE\)" /var/log/Xorg.1.log
```

`journalctl -u fielddeck-kiosk` lists every framebuffer and input device it found
before it tried to use them, which is the log to read when the panel is black.
