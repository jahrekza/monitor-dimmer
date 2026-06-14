# Monitor Dimmer

Automatically dims your secondary monitor when you launch a game or play fullscreen media — and restores it when you're done.

![License](https://img.shields.io/badge/license-MIT-blue)
![Platform](https://img.shields.io/badge/platform-Linux-lightgrey)
![Desktop](https://img.shields.io/badge/desktop-KDE%20Plasma-blue)

---

## Features

- **Auto-detection** — dims when a Steam or Heroic (GOG/Epic) game starts, or when VLC goes fullscreen
- **Two dimming modes**
  - **Black overlay** *(recommended)* — a borderless black window covers the screen, zero flicker, can go fully black
  - **Hardware brightness** — controls monitor brightness via DDC/CI (`ddcutil`)
- **Works with borderless windowed games** — detection is process-based, not window-state-based
- **System tray icon** — toggle auto-dim on/off, restore brightness, quit
- **Terminal config wizard** — change intensity, poll interval, dimming mode at any time
- **Starts automatically** at login via systemd user service
- **Interactive installer** — detects your monitors, game libraries, and walks you through setup

---

## Requirements

- Linux with **KDE Plasma** (Wayland or X11)
- Python 3.10+
- `ddcutil` (monitors must support DDC/CI)
- Python packages: `pystray`, `Pillow`, `PyQt6`
- `libappindicator-gtk3` (for system tray)

### Install dependencies

**Arch / CachyOS / Manjaro**
```bash
sudo pacman -S ddcutil python-pyqt6 python-pillow libappindicator-gtk3
pip install --user pystray
sudo usermod -aG i2c $USER   # then log out and back in
```

**Fedora / Bazzite / Nobara**
```bash
sudo dnf install ddcutil python3-pyqt6 python3-pillow libappindicator-gtk3
pip install --user pystray
sudo usermod -aG i2c $USER
```

**Debian / Ubuntu**
```bash
sudo apt install ddcutil python3-pyqt6 python3-pil gir1.2-appindicator3-0.1
pip install --user pystray
sudo usermod -aG i2c $USER
```

> **Note:** After adding yourself to the `i2c` group you must log out and back in for it to take effect.

---

## Installation

```bash
git clone https://github.com/jahrekza/monitor-dimmer
cd monitor-dimmer
python3 install.py
```

The installer will:
1. Check and offer to install missing Python dependencies
2. Detect your monitors via `ddcutil` and let you choose which one to dim
3. Detect Steam and Heroic game libraries automatically
4. Ask about dimming method, intensity, and poll interval
5. Install scripts, create a systemd user service, and add a desktop entry
6. Optionally add to KDE application launcher favorites
7. Run a quick dim test so you can see the effect immediately

---

## Usage

Once installed the daemon runs silently in the background.

| Situation | What happens |
|-----------|-------------|
| Steam game launches | Secondary monitor dims |
| Heroic game launches | Secondary monitor dims |
| VLC goes fullscreen | Secondary monitor dims |
| Game / VLC closes | Brightness restored |

### System tray

Look for the moon icon (🌙) in your taskbar. If it's hidden, click the `▲` arrow to expand hidden icons. Right-click for options:

- **Auto-dim** — toggle the feature on/off
- **Restore full brightness** — instant restore without disabling auto-dim
- **Quit** — stop the daemon

### Configuration wizard

```bash
monitor-dimmer-config
```

Options available:

| Setting | Description |
|---------|-------------|
| Dim intensity | 0 = no change · 50 = half brightness · 100 = fully black |
| Poll interval | How often to check if a game is running (seconds) |
| Dim method | Overlay (black window) or hardware (ddcutil) |
| VLC detection | Enable/disable fullscreen VLC triggering |

---

## How it works

```
┌─────────────────────────────────────────────────────┐
│  monitor-dimmer (daemon)                            │
│                                                     │
│  every N seconds:                                   │
│    scan /proc/*/exe for game paths  ──► game found  │
│    query VLC via MPRIS2 D-Bus       ──► fullscreen  │
│                                          │          │
│                                     dim secondary   │
│                                     monitor         │
│                                     (overlay or     │
│                                      ddcutil)       │
└─────────────────────────────────────────────────────┘
```

**Game detection** scans `/proc/*/exe` for processes running from configured game library paths. This works regardless of whether the game runs in fullscreen, borderless windowed, or windowed mode.

**VLC detection** queries VLC's MPRIS2 D-Bus interface (`org.mpris.MediaPlayer2.vlc`) and only triggers when VLC reports `Fullscreen = true`.

**Overlay mode** launches a borderless, always-on-top PyQt6 window covering the secondary screen at the configured opacity. Advantages over hardware dimming:
- No monitor flickering or power cycling
- Can reach 100% black
- Instant on/off

---

## File locations

| File | Purpose |
|------|---------|
| `~/.local/bin/monitor-dimmer` | Main daemon |
| `~/.local/bin/monitor-dimmer-overlay` | Overlay window process |
| `~/.local/bin/monitor-dimmer-config` | Configuration wizard |
| `~/.config/monitor-dimmer.conf` | Configuration file |
| `~/.config/systemd/user/monitor-dimmer.service` | Systemd user service |
| `~/.local/share/applications/monitor-dimmer-config.desktop` | Desktop entry |

### Configuration file format

```ini
# Monitor Dimmer configuration
dim_level=70
poll_interval=5
use_overlay=true
vlc_fullscreen=true
right_bus=10
game_paths=/home/user/.steam/steam/steamapps/common,/home/user/Games/Heroic
```

---

## Uninstall

```bash
python3 uninstall.py
```

Removes all installed files, disables the service, and removes the desktop entry.

---

## Troubleshooting

**No monitors detected by ddcutil**
```bash
sudo usermod -aG i2c $USER
# log out and back in, then:
ddcutil detect
```

**Tray icon not visible**
Click the `▲` arrow in your KDE taskbar to expand hidden icons. Then right-click the taskbar → *Configure System Tray* → find Monitor Dimmer → set to *Always visible*.

**Service not starting**
```bash
systemctl --user status monitor-dimmer.service
journalctl --user -u monitor-dimmer.service -n 50
```

**Dim not triggering for a game**
The game executable must be inside one of the configured `game_paths`. Run `monitor-dimmer-config` and check the path list, or add a custom path.

---

## License

MIT — do whatever you want with it.
