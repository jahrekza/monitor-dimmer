#!/usr/bin/env python3
"""
Monitor Dimmer — Installer
Automatically dims your secondary monitor during gaming or fullscreen media.
https://github.com/YOUR_USERNAME/monitor-dimmer
"""

import os
import sys
import shutil
import subprocess
import re
import time
import json
from pathlib import Path

# ── Paths ────────────────────────────────────────────────────────────────────

HOME        = Path.home()
REPO_DIR    = Path(__file__).parent.resolve()
SRC_DIR     = REPO_DIR / "src"
BIN_DIR     = HOME / ".local/bin"
CONFIG_FILE = HOME / ".config/monitor-dimmer.conf"
SERVICE_DIR = HOME / ".config/systemd/user"
APP_DIR     = HOME / ".local/share/applications"
ICONS_DIR   = HOME / ".local/share/icons"
ICON_SRC    = REPO_DIR / "assets/dimmer.png"
ICON_DEST   = ICONS_DIR / "monitor-dimmer.png"
SVG_SRC     = REPO_DIR / "assets/monitor-dimmer-symbolic.svg"
SVG_DEST    = ICONS_DIR / "hicolor/scalable/apps/monitor-dimmer-symbolic.svg"
KDE_STATS   = HOME / ".config/kactivitymanagerd-statsrc"

SCRIPTS = ["monitor-dimmer", "monitor-dimmer-overlay", "monitor-dimmer-config"]

# ── Colors ───────────────────────────────────────────────────────────────────

R = "\033[0m"
B = "\033[1m"
C = "\033[96m"
G = "\033[92m"
Y = "\033[93m"
E = "\033[91m"
D = "\033[2m"

# ── Helpers ───────────────────────────────────────────────────────────────────

def banner():
    print(f"""
{B}{C}╔══════════════════════════════════════════════╗
║          Monitor Dimmer — Installer          ║
║  Auto-dim secondary screen during gaming     ║
║  or fullscreen media                         ║
╚══════════════════════════════════════════════╝{R}
""")


def step(text):
    print(f"\n{B}── {text}{R}")


def ok(text):
    print(f"  {G}✓{R}  {text}")


def warn(text):
    print(f"  {Y}!{R}  {text}")


def err(text):
    print(f"  {E}✗{R}  {text}")


def ask_yes_no(prompt, default=True):
    hint = f"{B}Y{R}/n" if default else f"y/{B}N{R}"
    while True:
        raw = input(f"  {prompt} [{hint}]: ").strip().lower()
        if raw == "":
            return default
        if raw in ("y", "yes"):
            return True
        if raw in ("n", "no"):
            return False


def ask_int(prompt, lo, hi, default):
    while True:
        raw = input(f"  {prompt} [{default}]: ").strip()
        if raw == "":
            return default
        try:
            v = int(raw)
            if lo <= v <= hi:
                return v
        except ValueError:
            pass
        print(f"  {E}Enter a number between {lo} and {hi}{R}")


def ask_choice(prompt, options):
    """options: list of (label, description)"""
    print(f"\n  {B}{prompt}{R}")
    for i, (label, desc) in enumerate(options, 1):
        print(f"    {B}{i}{R}  {label}  {D}{desc}{R}")
    while True:
        raw = input(f"\n  Choose (1–{len(options)}): ").strip()
        try:
            idx = int(raw) - 1
            if 0 <= idx < len(options):
                return idx
        except ValueError:
            pass


def run(cmd, **kwargs):
    return subprocess.run(cmd, capture_output=True, text=True, **kwargs)

# ── Dependency check ──────────────────────────────────────────────────────────

DISTRO_HINTS = {
    "arch":   "sudo pacman -S {pkg}",
    "debian": "sudo apt install {pkg}",
    "fedora": "sudo dnf install {pkg}",
}

PKG_MAP = {
    "arch":   {"ddcutil": "ddcutil", "PyQt6": "python-pyqt6",
               "PIL": "python-pillow", "pystray": None,
               "libappindicator": "libappindicator-gtk3"},
    "debian": {"ddcutil": "ddcutil", "PyQt6": "python3-pyqt6",
               "PIL": "python3-pil", "pystray": None,
               "libappindicator": "gir1.2-appindicator3-0.1"},
    "fedora": {"ddcutil": "ddcutil", "PyQt6": "python3-pyqt6",
               "PIL": "python3-pillow", "pystray": None,
               "libappindicator": "libappindicator-gtk3"},
}


def detect_distro():
    try:
        text = Path("/etc/os-release").read_text()
        if "arch" in text.lower() or "cachyos" in text.lower() or "manjaro" in text.lower():
            return "arch"
        if "debian" in text.lower() or "ubuntu" in text.lower():
            return "debian"
        if "fedora" in text.lower() or "bazzite" in text.lower() or "nobara" in text.lower():
            return "fedora"
    except FileNotFoundError:
        pass
    return "unknown"


def check_dependencies():
    step("Checking dependencies")
    distro  = detect_distro()
    missing = []

    # ddcutil
    if shutil.which("ddcutil"):
        ok("ddcutil")
    else:
        err("ddcutil — not found")
        missing.append("ddcutil")

    # Python packages
    for name, import_name in [("pystray", "pystray"), ("Pillow", "PIL"), ("PyQt6", "PyQt6")]:
        try:
            __import__(import_name)
            ok(f"python {name}")
        except ImportError:
            err(f"python {name} — not found")
            missing.append(name)

    # AppIndicator (for system tray)
    try:
        import gi
        gi.require_version("AppIndicator3", "0.1")
        from gi.repository import AppIndicator3  # noqa: F401
        ok("libappindicator3")
    except Exception:
        warn("libappindicator3 — tray icon may not work on some desktops")

    if missing:
        print(f"\n  {Y}Some dependencies are missing.{R}")

        pip_pkgs = [p for p in missing if p in ("pystray", "Pillow", "PyQt6")]
        sys_pkgs = [p for p in missing if p not in pip_pkgs]

        if pip_pkgs:
            if ask_yes_no(f"Install {', '.join(pip_pkgs)} via pip?"):
                subprocess.run([sys.executable, "-m", "pip", "install", "--user"] + pip_pkgs)

        if sys_pkgs:
            pkgs = PKG_MAP.get(distro, {})
            names = [pkgs.get(p, p) for p in sys_pkgs]
            hint  = DISTRO_HINTS.get(distro, "install {pkg}").format(pkg=" ".join(names))
            print(f"\n  {Y}Please install manually:{R}")
            print(f"    {B}{hint}{R}")
            if not ask_yes_no("Continue anyway?", default=False):
                sys.exit(1)

    return True

# ── Monitor detection ─────────────────────────────────────────────────────────

def detect_monitors():
    result = run(["ddcutil", "detect"])
    monitors = []
    current  = {}

    for line in result.stdout.splitlines():
        if re.match(r"^Display \d+", line):
            if current.get("bus") is not None:
                monitors.append(current)
            current = {}
        elif "I2C bus:" in line:
            m = re.search(r"/dev/i2c-(\d+)", line)
            if m:
                current["bus"] = int(m.group(1))
        elif "DRM connector:" in line:
            current["connector"] = line.split(":", 1)[-1].strip()
        elif "Model:" in line:
            current["model"] = line.split(":", 1)[-1].strip()
        elif "Mfg id:" in line:
            raw = line.split(":", 1)[-1].strip()
            current["mfg"] = raw.split(" - ")[-1].strip() if " - " in raw else raw

    if current.get("bus") is not None:
        monitors.append(current)

    return monitors


def monitor_label(m):
    name = f"{m.get('mfg', '')} {m.get('model', '')}".strip() or "Unknown Monitor"
    return f"{name}  {D}({m.get('connector', '?')}, i2c-{m['bus']}){R}"


def pick_monitors():
    step("Detecting monitors")
    monitors = detect_monitors()

    if not monitors:
        err("No monitors detected via ddcutil.")
        print(f"  {D}Make sure you are in the 'i2c' group: sudo usermod -aG i2c $USER{R}")
        sys.exit(1)

    if len(monitors) == 1:
        warn("Only one monitor detected — dimming will affect your only screen.")
        if not ask_yes_no("Continue?", default=False):
            sys.exit(0)
        return None, monitors[0]["bus"]

    print(f"  Found {len(monitors)} monitors:\n")
    for i, m in enumerate(monitors, 1):
        print(f"    {B}{i}{R}  {monitor_label(m)}")

    print()
    primary_idx = ask_choice(
        "Which is your PRIMARY monitor? (gaming/media — the one you look at)",
        [(monitor_label(m), "") for m in monitors]
    )

    secondary_idx = ask_choice(
        "Which monitor should be AUTO-DIMMED?",
        [(monitor_label(m), "← will be dimmed during games/media")
         if i != primary_idx else (monitor_label(m), "← primary (selected above)")
         for i, m in enumerate(monitors)]
    )

    dim_bus = monitors[secondary_idx]["bus"]
    ok(f"Primary:  {monitor_label(monitors[primary_idx])}")
    ok(f"Dim target: {monitor_label(monitors[secondary_idx])}")
    return monitors[primary_idx], dim_bus

# ── Game path detection ───────────────────────────────────────────────────────

def detect_game_paths():
    paths = []

    steam = HOME / ".steam/steam/steamapps/common"
    if steam.exists():
        paths.append(("Steam", str(steam)))

    heroic_cfg = HOME / ".var/app/com.heroicgameslauncher.hgl/config/heroic/config.json"
    if heroic_cfg.exists():
        try:
            data = json.loads(heroic_cfg.read_text())
            p = data.get("defaultSettings", {}).get("defaultInstallPath")
            if p and Path(p).exists():
                paths.append(("Heroic (GOG/Epic)", p))
        except Exception:
            pass

    heroic_default = HOME / "Games/Heroic"
    if heroic_default.exists() and not any("Heroic" in label for label, _ in paths):
        paths.append(("Heroic (GOG/Epic)", str(heroic_default)))

    return paths


def pick_game_paths():
    step("Detecting game libraries")
    found = detect_game_paths()
    chosen = []

    if not found:
        warn("No game libraries detected automatically.")
        custom = input("  Enter custom path (or leave empty to skip): ").strip()
        if custom and Path(custom).exists():
            chosen.append(custom)
        return chosen

    for label, path in found:
        ok(f"{label}: {D}{path}{R}")
        if ask_yes_no(f"Include {label}?"):
            chosen.append(path)

    custom = input(f"\n  {D}Add a custom game path? (leave empty to skip){R}: ").strip()
    if custom and Path(custom).exists():
        chosen.append(custom)
    elif custom:
        warn(f"Path not found, skipping: {custom}")

    return chosen

# ── Settings wizard ───────────────────────────────────────────────────────────

def pick_settings():
    step("Dimming settings")

    method_idx = ask_choice(
        "How should the screen be dimmed?",
        [
            ("Black overlay",        "recommended — no flicker, can go fully black"),
            ("Hardware brightness",  "changes monitor brightness via DDC/CI"),
        ]
    )
    use_overlay = method_idx == 0

    print()
    if use_overlay:
        print(f"  {D}0 = invisible, 50 = semi-transparent, 100 = fully black{R}")
    else:
        print(f"  {D}0 = no change, 50 = half brightness, 100 = pitch black{R}")
    dim_level = ask_int("Dim intensity (0–100)", 0, 100, 70)

    print()
    print(f"  {D}How often to check if a game/media is running{R}")
    poll = ask_int("Poll interval in seconds (2–60)", 2, 60, 5)

    print()
    vlc = ask_yes_no("Detect VLC in fullscreen?")

    return {
        "use_overlay":    use_overlay,
        "dim_level":      dim_level,
        "poll_interval":  poll,
        "vlc_fullscreen": vlc,
    }

# ── Installation ──────────────────────────────────────────────────────────────

def write_config(cfg):
    lines = [
        "# Monitor Dimmer configuration — generated by install.py",
        f"dim_level={cfg['dim_level']}",
        f"poll_interval={cfg['poll_interval']}",
        f"use_overlay={str(cfg['use_overlay']).lower()}",
        f"vlc_fullscreen={str(cfg['vlc_fullscreen']).lower()}",
    ]
    if cfg.get("right_bus") is not None:
        lines.append(f"right_bus={cfg['right_bus']}")
    if cfg.get("game_paths"):
        lines.append(f"game_paths={','.join(cfg['game_paths'])}")
    CONFIG_FILE.write_text("\n".join(lines) + "\n")
    ok(f"Config written to {CONFIG_FILE}")


def install_icon():
    ICONS_DIR.mkdir(parents=True, exist_ok=True)
    if ICON_SRC.exists():
        shutil.copy2(ICON_SRC, ICON_DEST)
        ok(f"App icon installed to {ICON_DEST}")
    else:
        warn("App icon not found in assets/ — skipping")
    if SVG_SRC.exists():
        SVG_DEST.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(SVG_SRC, SVG_DEST)
        ok(f"Symbolic tray icon installed to {SVG_DEST}")
    else:
        warn("Symbolic icon not found in assets/ — skipping")


def install_scripts():
    BIN_DIR.mkdir(parents=True, exist_ok=True)
    for name in SCRIPTS:
        src  = SRC_DIR / name
        dest = BIN_DIR / name
        shutil.copy2(src, dest)
        dest.chmod(0o755)
        ok(f"Installed {dest}")


def install_service(autostart):
    SERVICE_DIR.mkdir(parents=True, exist_ok=True)
    service = SERVICE_DIR / "monitor-dimmer.service"
    service.write_text(f"""[Unit]
Description=Monitor Dimmer — auto-dim secondary screen during games/media
After=graphical-session.target
PartOf=graphical-session.target

[Service]
Type=simple
ExecStart={BIN_DIR}/monitor-dimmer
Restart=on-failure
RestartSec=5
Environment=PYSTRAY_BACKEND=appindicator

[Install]
WantedBy=graphical-session.target
""")
    ok(f"Service file written to {service}")
    run(["systemctl", "--user", "daemon-reload"])
    if autostart:
        run(["systemctl", "--user", "enable", "monitor-dimmer.service"])
        ok("Service enabled (starts automatically at login)")
    else:
        run(["systemctl", "--user", "disable", "monitor-dimmer.service"])
        ok("Autostart disabled — start manually with: systemctl --user start monitor-dimmer")


def install_desktop_entry():
    APP_DIR.mkdir(parents=True, exist_ok=True)
    icon = str(ICON_DEST) if ICON_DEST.exists() else "weather-clear-night"
    entry = APP_DIR / "monitor-dimmer-config.desktop"
    entry.write_text(f"""[Desktop Entry]
Version=1.0
Type=Application
Name=Monitor Dimmer
Comment=Configure auto-dimming of secondary screen during games or fullscreen media
Exec=konsole --noclose -e monitor-dimmer-config
Icon={icon}
Terminal=false
Categories=Settings;HardwareSettings;
Keywords=monitor;brightness;dim;gaming;
""")
    ok(f"Desktop entry written to {entry}")
    run(["kbuildsycoca6", "--noincremental"])


def add_to_kde_favorites():
    if not KDE_STATS.exists():
        return
    content = KDE_STATS.read_text()
    entry   = "applications:monitor-dimmer-config.desktop"
    if entry in content:
        ok("Already in KDE favorites")
        return
    new = re.sub(
        r"(ordering=[^\n]+)",
        lambda m: m.group(0) + f",{entry}",
        content
    )
    KDE_STATS.write_text(new)
    ok("Added to KDE application launcher favorites")


def start_service():
    run(["systemctl", "--user", "start", "monitor-dimmer.service"])
    time.sleep(2)
    r = run(["systemctl", "--user", "is-active", "monitor-dimmer.service"])
    if r.stdout.strip() == "active":
        ok("Service is running")
    else:
        warn("Service may not have started — check: systemctl --user status monitor-dimmer")


def test_dim(cfg):
    print(f"\n  {Y}Testing dim for 3 seconds — look at your secondary screen...{R}")
    if cfg["use_overlay"]:
        proc = subprocess.Popen(
            ["python3", str(BIN_DIR / "monitor-dimmer-overlay"),
             str(cfg["dim_level"] / 100)]
        )
        time.sleep(3)
        proc.terminate()
    else:
        bus    = cfg.get("right_bus")
        target = 100 - cfg["dim_level"]
        if bus:
            run(["ddcutil", "setvcp", "10", str(target), "--bus", str(bus)])
            time.sleep(3)
            run(["ddcutil", "setvcp", "10", "100", "--bus", str(bus)])
    print(f"  {G}Done.{R}")

# ── Summary & main ────────────────────────────────────────────────────────────

def print_summary(cfg):
    method = "Black overlay" if cfg["use_overlay"] else "Hardware (ddcutil)"
    paths  = "\n".join(f"    • {p}" for p in cfg.get("game_paths", [])) or "    (none)"
    vlc    = "yes (fullscreen only)" if cfg["vlc_fullscreen"] else "no"

    print(f"""
{B}Installation summary:{R}
  Dim method:      {method}
  Dim intensity:   {cfg['dim_level']}%
  Poll interval:   {cfg['poll_interval']}s
  VLC detection:   {vlc}
  Secondary bus:   i2c-{cfg.get('right_bus', '?')}
  Game paths:
{paths}
""")


def main():
    banner()

    # 1. Dependencies
    check_dependencies()

    # 2. Monitor selection
    _primary, dim_bus = pick_monitors()

    # 3. Game paths
    game_paths = pick_game_paths()

    # 4. Settings
    settings = pick_settings()

    # 5. Build final config
    cfg = {
        **settings,
        "right_bus":  dim_bus,
        "game_paths": game_paths,
    }

    # 6. Summary + confirm
    print_summary(cfg)
    if not ask_yes_no("Proceed with installation?"):
        print(f"\n  {D}Installation cancelled.{R}\n")
        sys.exit(0)

    # 7. Autostart
    print()
    autostart = ask_yes_no("Start Monitor Dimmer automatically at login?")

    # 8. Install
    step("Installing")
    install_icon()
    write_config(cfg)
    install_scripts()
    install_service(autostart)
    install_desktop_entry()

    # 9. KDE favorites (optional)
    desktop = os.environ.get("XDG_CURRENT_DESKTOP", "")
    if "KDE" in desktop:
        if ask_yes_no("\nAdd to KDE application launcher favorites?"):
            add_to_kde_favorites()

    # 10. Start
    step("Starting service")
    start_service()

    # 11. Test
    if ask_yes_no("\nRun a quick dim test now?"):
        test_dim(cfg)

    print(f"""
{G}{B}✓ Installation complete!{R}

  • The service starts automatically at login
  • System tray icon: look for it in your taskbar (may be hidden — click ▲)
  • To reconfigure: run {B}monitor-dimmer-config{R}
  • To uninstall:   run {B}python3 {REPO_DIR}/uninstall.py{R}
""")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print(f"\n\n  {D}Installation cancelled.{R}\n")
        sys.exit(0)
