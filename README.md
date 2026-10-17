# Monitor Dimmer

Automatically dims your other monitors when you launch a game or play fullscreen media — and restores them when you're done.

![License](https://img.shields.io/badge/license-MIT-blue)
![Platform](https://img.shields.io/badge/platform-Linux-lightgrey)
![Desktop](https://img.shields.io/badge/desktop-KDE%20Plasma-blue)

---

## Features

- **Auto-detection** — dims when a Steam, Heroic (GOG/Epic) or any game from your library folders starts, when VLC goes fullscreen, or when YouTube (or another site you list) plays fullscreen in the browser
- **Any number of screens** — choose which screens get dimmed and which stay untouched, each with its own mode and dim level
- **Manual control** — "Dim now" works regardless of detection; auto-dim can be switched off
- **Two dimming modes**, chosen per screen
  - **Black overlay** *(recommended)* — a borderless black window covers the screen, zero flicker, can go fully black, works on any screen (laptop panels and TVs too); clicks pass through it
  - **Hardware brightness** — lowers the monitor's own brightness via DDC/CI (`ddcutil`), relative to the brightness you use, and puts it back afterwards
- **Finds your monitor by identity** — serial number / model, not the GPU port, so swapping cables doesn't make it dim the wrong screen; two identical monitors are told apart by connector
- **Works with borderless windowed games** — detection is process-based; optionally dim only while a game is fullscreen (on KDE this sees native Wayland windows too)
- **Safe restore** — if the daemon is stopped or crashes while a monitor is dimmed, the brightness is restored
- **System tray icon** — auto-dim on/off, dim now, per-screen mode, what to detect, settings (the daemon also runs without a tray)
- **Terminal config wizard** — change intensity, screens, poll interval and detection at any time
- **Starts automatically** at login via systemd user service
- **Interactive installer** — detects your monitors, game libraries, and walks you through setup

---

## Requirements

- Linux with a systemd user session — developed on **KDE Plasma** (Wayland or X11)
- Python 3.10+ with `venv` support
- `PyQt6` — overlay window and monitor list (best from your distro; the installer can pip-install it otherwise)
- Optional:
  - `ddcutil` — hardware brightness mode (monitors must support DDC/CI)
  - `gdbus` (glib2) — VLC fullscreen detection, KWin window watcher
  - `xdotool` + `xprop` — fullscreen windows on desktops other than KDE (browser video, "dim only while fullscreen")
  - AppIndicator (`libappindicator-gtk3` / `gir1.2-ayatanaappindicator3-0.1`) — tray icon

`pystray` and `Pillow` are installed by the installer into a private virtual environment (`~/.local/share/monitor-dimmer/venv`), so nothing is pip-installed into your system Python.

### Install dependencies

**Arch / CachyOS / Manjaro**
```bash
sudo pacman -S python-pyqt6 ddcutil glib2 xdotool xorg-xprop libappindicator-gtk3
```

**Fedora / Bazzite / Nobara**
```bash
sudo dnf install python3-pyqt6 ddcutil glib2 xdotool xprop libappindicator-gtk3
```

**Debian / Ubuntu**
```bash
sudo apt install python3-venv python3-pyqt6 ddcutil libglib2.0-bin xdotool x11-utils gir1.2-ayatanaappindicator3-0.1
```

For hardware brightness your user needs access to `/dev/i2c-*`. On many distros:
```bash
sudo usermod -aG i2c $USER   # then log out and back in
```

---

## Installation

Run it from your desktop session (the installer asks the compositor for the list of screens):

```bash
git clone https://github.com/jahrekza/monitor-dimmer
cd monitor-dimmer
python3 install.py
```

The installer will:
1. Check optional system tools and create the private Python environment
2. Ask for the dimming method, then let you pick the screens to dim
3. Detect Steam libraries (native and Flatpak) and Heroic install folders
4. Ask about intensity, poll interval, VLC, YouTube and fullscreen-only detection
5. Install the programs, create a systemd user service, and add a desktop entry
6. Optionally add to KDE application launcher favorites
7. Run a quick dim test so you can see the effect immediately

### Updating

```bash
cd monitor-dimmer
git pull
python3 install.py
```

The installer offers to keep your current settings and only update the program.

---

## Usage

Once installed the daemon runs silently in the background.

| Situation | What happens |
|-----------|-------------|
| Steam game launches | Chosen screens dim |
| Game from a library folder launches (Heroic, native, …) | Chosen screens dim |
| VLC goes fullscreen | Chosen screens dim |
| YouTube goes fullscreen in the browser | Chosen screens dim |
| Game / video closes | Brightness restored |

### System tray

Look for the monitor icon in your taskbar. If it's hidden, click the `▲` arrow to expand hidden icons. Right-click for options:

- **Status** — what is happening right now (watching, dimmed by a game / VLC / video, manual)
- **Auto-dim** — turn automatic dimming on or off
- **Dim now** — dim the chosen screens right away, whatever is running
- **Screens** — every connected screen with *Not dimmed / Overlay / Brightness (DDC/CI)* and its *Dim level* (default, 30–100%); brightness is greyed out for screens without DDC/CI. *Refresh screen list* after plugging in a monitor
- **Detect** — games, games only when fullscreen, VLC fullscreen, fullscreen video in the browser
- **Settings…** — open the configuration wizard in a terminal
- **Quit** — stop the daemon (restores everything)

Changes made in the tray are saved to the config.

### Configuration wizard

```bash
dimmer
```

```
  SCREENS
   1  Screens to dim           per screen: overlay / brightness, own dim level
   2  Default dim intensity    used by screens without their own level
   3  Test dim (3 s)
  DETECTION
   4  Games                    on/off · only when fullscreen
   5  Game folders             list, add, remove
   6  VLC fullscreen           on/off
   7  Browser video            on/off · sites (window titles)
  GENERAL
   8  Auto-dim                 on/off
   9  Poll interval            seconds
  10  Service                  start / stop / restart / start at login
   s  Save    q  Quit
```

Saving writes only what you changed, so changes made from the tray in the meantime are kept, and the running daemon picks the new settings up within a few seconds — no restart.

---------|-------------|
| Dim intensity | 0 = no change · 50 = half brightness · 100 = fully black |
| Poll interval | How often to check if a game is running (seconds) |
| Test dim | Dim the chosen screens for 3 seconds |
| Screens | Pick which screens are dimmed and how (overlay / hardware brightness) |
| Detection | Games, games only when fullscreen, VLC fullscreen, browser video |
| Start / Stop service | Control the systemd user service |

---

## How it works

```
┌─────────────────────────────────────────────────────┐
│  monitor-dimmer (daemon)                            │
│                                                     │
│  every N seconds:                                   │
│    scan /proc for Steam games and   ──► game found  │
│    processes from game_paths                        │
│    query VLC via MPRIS2 D-Bus       ──► fullscreen  │
│    fullscreen window titled YouTube ──► video       │
│                                          │          │
│                                     dim each chosen │
│                                     screen (overlay │
│                                     or ddcutil)     │
└─────────────────────────────────────────────────────┘
```

**Game detection** looks for processes with a `SteamGameId` (Steam and Proton games, Steam helpers excluded) and for processes whose executable — or Windows `.exe` started through Wine — lives inside one of the configured `game_paths`. This works whether the game runs fullscreen, borderless windowed, or windowed. With *fullscreen only* enabled, some window also has to be fullscreen.

**Fullscreen windows.** On KDE Plasma the daemon loads a small KWin script that reports fullscreen windows (title, process) over D-Bus whenever they change — native Wayland and X11 windows alike. On other desktops X11/XWayland windows are read with `xdotool` + `xprop`; native Wayland windows are invisible there, so leave *fullscreen only* off for native Wayland games.

**Browser video.** Browsers don't tell other programs whether a video is fullscreen or which site is playing, so the daemon looks for a fullscreen window whose title contains one of `video_titles` (default `YouTube`; add e.g. `Netflix,Twitch` in the config).

**VLC detection** queries every VLC instance's MPRIS2 D-Bus interface and only triggers when VLC reports `Fullscreen = true`.

**Overlay mode** opens a borderless, always-on-top, click-through PyQt6 window covering each overlay screen at the configured opacity. It doesn't take keyboard focus from the game. Advantages over hardware dimming:
- No monitor flickering or power cycling
- Can reach 100% black
- Instant on/off
- Works on screens without DDC/CI

Placing a window on a chosen screen is up to the compositor: KDE Plasma and X11 desktops honour it; on other Wayland desktops use hardware brightness if the overlay lands on the wrong screen.

**Hardware brightness** reads the monitor's current brightness, lowers it by the dim intensity (70% dim of brightness 60 → 18) and restores the saved value afterwards. The saved value is kept in `~/.local/state/monitor-dimmer/` until it has been restored, so a crash or `systemctl stop` mid-game doesn't leave the screen dark.

---

## File locations

| File | Purpose |
|------|---------|
| `~/.local/bin/monitor-dimmer` | Daemon launcher (`--restore` undoes a leftover dim) |
| `~/.local/bin/monitor-dimmer-overlay` | Overlay window launcher (`--list` prints the screens) |
| `~/.local/bin/dimmer` | Configuration wizard launcher |
| `~/.local/share/monitor-dimmer/` | Programs and their private Python environment |
| `~/.local/state/monitor-dimmer/` | Brightness to restore while a DDC dim is active |
| `~/.config/monitor-dimmer.conf` | Configuration file |
| `~/.config/systemd/user/monitor-dimmer.service` | Systemd user service |
| `~/.local/share/applications/dimmer.desktop` | Desktop entry |

### Configuration file format

```ini
# Monitor Dimmer configuration
dim_level=70
poll_interval=5
auto_dim=true
detect_games=true
vlc_fullscreen=true
video_fullscreen=true
video_titles=YouTube,Netflix
require_fullscreen=false
game_paths=/home/user/.steam/steam/steamapps/common,/home/user/Games/Heroic
screen1_mode=ddc
screen1_level=50
screen1_connector=DP-3
screen1_mfg=IVM
screen1_model=PL3494WQ
screen1_sn=1187021300709
screen1_bus=11
screen2_mode=overlay
screen2_connector=HDMI-A-1
screen2_model=LG TV
```

`screenN_mode` is `off`, `overlay` or `ddc`; `screenN_level` (0–100) overrides `dim_level` for that screen. Screens that aren't listed are never touched.

**How screens are found.** Bus numbers and connector names belong to the GPU port, not the monitor, so they change when cables are swapped. The daemon looks for each screen by `sn`, then by `model`, and uses `connector` / `bus` only to tell identical monitors apart. A screen that is not found — switched off or unplugged — is skipped. Configs from older versions (`right_*` / `target_*` keys, or only a bus number) are read as `screen1`.

---

## Uninstall

```bash
python3 uninstall.py
```

Stops the service (restoring brightness if a monitor is dimmed), removes all installed files, the private Python environment, the config and the desktop entry.

---

## Troubleshooting

**No monitors listed for hardware brightness**
```bash
sudo usermod -aG i2c $USER
# log out and back in, then:
ddcutil detect
```
Monitors listed as *Invalid display* don't support DDC/CI — use overlay mode for them.

**Tray icon not visible**
Click the `▲` arrow in your taskbar to expand hidden icons. On KDE: right-click the taskbar → *Configure System Tray* → find Monitor Dimmer → set to *Always visible*. Without an AppIndicator/tray host the daemon keeps working; use `dimmer` to configure it.

**Service not starting**
```bash
systemctl --user status monitor-dimmer.service
journalctl --user -u monitor-dimmer.service -n 50
```
The service starts with `graphical-session.target`. Desktops that don't activate that target need `systemctl --user start monitor-dimmer` from their autostart.

**The wrong monitor dims, or none at all**
Tray → *Screens*: set the right screen to *Overlay* or *Brightness* and the others to *Not dimmed*. After plugging in a monitor use *Refresh screen list*.

**YouTube not detected**
The browser window must be fullscreen (video fullscreen, `F` on YouTube) and its title must contain "YouTube". On desktops other than KDE install `xdotool` and `xprop`; native Wayland browser windows can only be seen on KDE.

**Dim not triggering for a game**
Steam games are found automatically. Other games must run from inside one of the configured `game_paths` — re-run the installer or edit `game_paths` in the config. If *fullscreen only* is on, try turning it off.

---

## License

MIT — do whatever you want with it.
