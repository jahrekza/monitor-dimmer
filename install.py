#!/usr/bin/env python3
"""
Monitor Dimmer — Installer
Automatically dims your secondary monitor during gaming or fullscreen media.
https://github.com/jahrekza/monitor-dimmer
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
APP_HOME    = HOME / ".local/share/monitor-dimmer"
VENV_DIR    = APP_HOME / "venv"
VENV_PY     = VENV_DIR / "bin/python"
SERVICE_DIR = HOME / ".config/systemd/user"
APP_DIR     = HOME / ".local/share/applications"
ICONS_DIR   = HOME / ".local/share/icons"
ICON_SRC    = REPO_DIR / "assets/dimmer.png"
ICON_DEST   = ICONS_DIR / "monitor-dimmer.png"
SVG_SRC     = REPO_DIR / "assets/monitor-dimmer-symbolic.svg"
SVG_DEST    = ICONS_DIR / "hicolor/scalable/apps/monitor-dimmer-symbolic.svg"
KDE_STATS   = HOME / ".config/kactivitymanagerd-statsrc"

PROGRAMS = ["monitor-dimmer", "monitor-dimmer-overlay", "dimmer"]
MODULES  = ["dimmer_common.py", "fullscreen_watch.py"]

sys.path.insert(0, str(SRC_DIR))
import dimmer_common as dc  # noqa: E402

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
    """subprocess.run that reports a missing program instead of crashing."""
    try:
        return subprocess.run(cmd, capture_output=True, text=True, **kwargs)
    except FileNotFoundError:
        return subprocess.CompletedProcess(cmd, 127, "", f"{cmd[0]}: not found")

# ── Dependency check ──────────────────────────────────────────────────────────

INSTALL_CMD = {
    "arch":     "sudo pacman -S {pkg}",
    "debian":   "sudo apt install {pkg}",
    "fedora":   "sudo dnf install {pkg}",
    "suse":     "sudo zypper install {pkg}",
}

SYSTEM_PKGS = {
    "arch":   {"ddcutil": "ddcutil", "xdotool": "xdotool", "xprop": "xorg-xprop",
               "gdbus": "glib2", "venv": "python", "PyQt6": "python-pyqt6",
               "appindicator": "libappindicator-gtk3"},
    "debian": {"ddcutil": "ddcutil", "xdotool": "xdotool", "xprop": "x11-utils",
               "gdbus": "libglib2.0-bin", "venv": "python3-venv", "PyQt6": "python3-pyqt6",
               "appindicator": "gir1.2-ayatanaappindicator3-0.1"},
    "fedora": {"ddcutil": "ddcutil", "xdotool": "xdotool", "xprop": "xprop",
               "gdbus": "glib2", "venv": "python3", "PyQt6": "python3-pyqt6",
               "appindicator": "libappindicator-gtk3"},
    "suse":   {"ddcutil": "ddcutil", "xdotool": "xdotool", "xprop": "xprop",
               "gdbus": "glib2-tools", "venv": "python3", "PyQt6": "python3-PyQt6",
               "appindicator": "typelib-1_0-AppIndicator3-0_1"},
}


def detect_distro():
    fields = {}
    try:
        for line in Path("/etc/os-release").read_text().splitlines():
            k, _, v = line.partition("=")
            fields[k] = v.strip().strip('"').lower()
    except FileNotFoundError:
        return "unknown"
    ids = [fields.get("ID", "")] + fields.get("ID_LIKE", "").split()
    families = {"arch": "arch", "debian": "debian", "ubuntu": "debian",
                "fedora": "fedora", "rhel": "fedora", "suse": "suse"}
    for i in ids:
        for key, family in families.items():
            if key in i:
                return family
    return "unknown"


def install_hint(distro, keys):
    pkgs = [SYSTEM_PKGS.get(distro, {}).get(k, k) for k in keys]
    return INSTALL_CMD.get(distro, "install: {pkg}").format(pkg=" ".join(pkgs))


def check_system_tools(distro):
    step("Checking system tools")
    optional = [
        ("ddcutil", "ddcutil", "hardware brightness mode (DDC/CI)"),
        ("gdbus",   "gdbus",   "VLC fullscreen detection"),
        ("xdotool", "xdotool", "\"dim only while fullscreen\" option"),
        ("xprop",   "xprop",   "\"dim only while fullscreen\" option"),
    ]
    missing = []
    for key, binary, purpose in optional:
        if shutil.which(binary):
            ok(binary)
        else:
            warn(f"{binary} — not found {D}(needed for {purpose}){R}")
            missing.append(key)
    if missing:
        print(f"\n  {D}Optional — install with:{R} {B}{install_hint(distro, missing)}{R}")
    if not shutil.which("systemctl"):
        err("systemctl not found — the daemon runs as a systemd user service")
        sys.exit(1)


def venv_has(module):
    return run([str(VENV_PY), "-c", f"import {module}"]).returncode == 0


def setup_venv(distro):
    """Private virtualenv for pystray/Pillow, so nothing touches the system
    Python (pip --user is refused on PEP 668 distros). It sees system site
    packages for PyGObject and PyQt6, which are best installed from the distro."""
    step("Setting up Python environment")
    if not VENV_PY.exists():
        APP_HOME.mkdir(parents=True, exist_ok=True)
        r = run([sys.executable, "-m", "venv", "--system-site-packages", str(VENV_DIR)])
        if r.returncode != 0:
            err("Could not create a Python virtual environment")
            print(f"  {D}{r.stderr.strip()}{R}")
            print(f"  Install venv support: {B}{install_hint(distro, ['venv'])}{R}")
            sys.exit(1)
    ok(f"Virtual environment: {VENV_DIR}")

    pip_pkgs = [pkg for pkg, mod in (("pystray", "pystray"), ("Pillow", "PIL"))
                if not venv_has(mod)]
    if not venv_has("PyQt6"):
        warn("PyQt6 not found (needed for the overlay and monitor list)")
        print(f"  {D}Best from your distro: {install_hint(distro, ['PyQt6'])}{R}")
        if ask_yes_no("Install PyQt6 into the environment with pip instead (~80 MB)?"):
            pip_pkgs.append("PyQt6")
    if pip_pkgs:
        print(f"  {D}Installing {', '.join(pip_pkgs)}...{R}")
        r = run([str(VENV_PY), "-m", "pip", "install", "--quiet", "--upgrade", *pip_pkgs])
        if r.returncode != 0:
            err("pip install failed")
            print(f"  {D}{r.stderr.strip()[-800:]}{R}")
            sys.exit(1)
    for name, mod in (("pystray", "pystray"), ("Pillow", "PIL"), ("PyQt6", "PyQt6")):
        if venv_has(mod):
            ok(f"python {name}")
        else:
            warn(f"python {name} — missing")

    indicator = any(run([str(VENV_PY), "-c",
                         f"import gi; gi.require_version('{lib}', '0.1')"]).returncode == 0
                    for lib in ("AppIndicator3", "AyatanaAppIndicator3"))
    if indicator:
        ok("AppIndicator (tray icon)")
    else:
        warn("AppIndicator not found — the tray icon may not show; the daemon works without it")
        print(f"  {D}{install_hint(distro, ['appindicator'])}{R}")
    return indicator

# ── Screens ───────────────────────────────────────────────────────────────────

def pick_screens():
    """Return the screens to dim, each with its mode."""
    step("Screens")
    monitors = dc.all_monitors(python=str(VENV_PY))
    if not monitors:
        err("No monitors detected — run the installer from your desktop session.")
        sys.exit(1)

    if len(monitors) == 1:
        warn("Only one monitor detected — dimming will affect your only screen.")
        if not ask_yes_no("Continue?", default=False):
            sys.exit(0)
        chosen = monitors
    else:
        print(f"\n  {B}Which screens should be dimmed? (not the ones you play on){R}")
        for i, m in enumerate(monitors, 1):
            ddc = "" if m.get("bus") is not None else f"  {D}(no DDC/CI: overlay only){R}"
            print(f"    {B}{i}{R}  {dc.screen_label(m)}{ddc}")
        while True:
            raw = input(f"\n  Numbers, separated by commas (e.g. 2 or 2,3): ").strip()
            try:
                picks = sorted({int(x) for x in raw.replace(" ", "").split(",") if x})
            except ValueError:
                continue
            if picks and all(1 <= i <= len(monitors) for i in picks):
                break
        chosen = [monitors[i - 1] for i in picks]

    screens = []
    for m in chosen:
        mode = "overlay"
        if m.get("bus") is not None:
            idx = ask_choice(
                f"How should {dc.screen_label(m)} be dimmed?",
                [("Overlay (black window)", "recommended — no flicker, can go fully black"),
                 ("Brightness (DDC/CI)",    "lowers the monitor's own brightness")])
            mode = "overlay" if idx == 0 else "ddc"
        screens.append(dict(dc.identity_of(m), mode=mode))
        ok(f"{dc.screen_label(m)} — {MODE_TEXT[mode]}")

    print()
    print(f"  {D}0 = no change · 50 = half · 100 = black{R}")
    dim_level = ask_int("Dim intensity (0–100)", 0, 100, 70)
    return screens, dim_level

# ── Detection ─────────────────────────────────────────────────────────────────

STEAM_ROOTS = [
    HOME / ".steam/steam",
    HOME / ".local/share/Steam",
    HOME / ".var/app/com.valvesoftware.Steam/.local/share/Steam",
]

HEROIC_CONFIGS = [
    HOME / ".config/heroic/config.json",
    HOME / ".var/app/com.heroicgameslauncher.hgl/config/heroic/config.json",
]


def steam_libraries():
    libs = []
    for root in STEAM_ROOTS:
        vdf = root / "steamapps/libraryfolders.vdf"
        dirs = [root]
        if vdf.exists():
            dirs += [Path(p) for p in re.findall(r'"path"\s+"([^"]+)"', vdf.read_text(errors="ignore"))]
        for d in dirs:
            common = d / "steamapps/common"
            if common.is_dir():
                libs.append(common)
    return libs


def detect_game_paths():
    found, seen = [], set()

    def add(label, path):
        real = os.path.realpath(path)
        if real not in seen and Path(path).is_dir():
            seen.add(real)
            found.append((label, str(path)))

    for lib in steam_libraries():
        add("Steam library", lib)

    for cfg in HEROIC_CONFIGS:
        try:
            data = json.loads(cfg.read_text())
            p = data.get("defaultSettings", {}).get("defaultInstallPath")
            if p:
                add("Heroic (GOG/Epic)", Path(p).expanduser())
        except (OSError, ValueError):
            pass
    add("Heroic (GOG/Epic)", HOME / "Games/Heroic")
    return found


def pick_game_paths():
    print(f"\n  {B}Game folders{R}")
    print(f"  {D}Steam games are found without folders. A process running from a folder{R}")
    print(f"  {D}listed here (or a Windows .exe from it, via Wine) counts as a game.{R}\n")
    chosen = []
    for label, path in detect_game_paths():
        ok(f"{label}: {D}{path}{R}")
        if ask_yes_no(f"Include {path}?"):
            chosen.append(path)

    while True:
        custom = input(f"\n  {D}Add another game folder? (leave empty to skip){R}: ").strip()
        if not custom:
            break
        path = os.path.abspath(os.path.expanduser(custom))
        if "," in custom:
            warn("Folders containing commas are not supported, skipping")
        elif not Path(path).is_dir():
            warn(f"Folder not found, skipping: {custom}")
        elif path not in chosen:
            chosen.append(path)
    return chosen


def pick_detection():
    step("Detection")
    games = ask_yes_no("Dim while a game runs?")
    fullscreen, game_paths = False, []
    if games:
        print(f"  {D}Only when fullscreen: KDE sees all windows; elsewhere only X11/XWayland games{R}")
        fullscreen = ask_yes_no("…only while a window is fullscreen?", default=False)
        game_paths = pick_game_paths()

    print()
    vlc = ask_yes_no("Dim while VLC is fullscreen?")

    print()
    print(f"  {D}Browser video: a fullscreen window whose title contains one of the sites{R}")
    video = ask_yes_no("Dim while YouTube is fullscreen in the browser?")
    titles = ["YouTube"]
    if video:
        extra = input(f"  {D}Other sites, separated by commas (e.g. Netflix,Twitch — empty to skip){R}: ")
        titles += [t.strip() for t in extra.split(",") if t.strip() and t.strip() not in titles]

    return {
        "detect_games":       games,
        "require_fullscreen": fullscreen,
        "game_paths":         game_paths,
        "vlc_fullscreen":     vlc,
        "video_fullscreen":   video,
        "video_titles":       titles,
    }


def pick_general():
    step("General")
    print(f"  {D}How often to check for games and video (lower = faster, a bit more CPU){R}")
    poll = ask_int("Poll interval in seconds (2–60)", 2, 60, 5)
    autostart = ask_yes_no("Start Monitor Dimmer at login?")
    return {"poll_interval": poll}, autostart

# ── Installation ──────────────────────────────────────────────────────────────

def write_config(cfg):
    dc.save_config(cfg)
    ok(f"Config written to {dc.CONFIG_FILE}")


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
    APP_HOME.mkdir(parents=True, exist_ok=True)
    BIN_DIR.mkdir(parents=True, exist_ok=True)
    for name in PROGRAMS + MODULES:
        shutil.copy2(SRC_DIR / name, APP_HOME / name)
    # Launchers in ~/.local/bin run the programs with the private venv
    for name in PROGRAMS:
        (APP_HOME / name).chmod(0o755)
        launcher = BIN_DIR / name
        launcher.unlink(missing_ok=True)
        launcher.write_text(f'#!/bin/sh\nexec "{VENV_PY}" "{APP_HOME / name}" "$@"\n')
        launcher.chmod(0o755)
        ok(f"Installed {launcher}")
    if str(BIN_DIR) not in os.environ.get("PATH", "").split(":"):
        warn(f"{BIN_DIR} is not in your PATH — run the wizard as {BIN_DIR / 'dimmer'}")


def install_service(autostart, indicator):
    SERVICE_DIR.mkdir(parents=True, exist_ok=True)
    service = SERVICE_DIR / "monitor-dimmer.service"
    backend = "Environment=PYSTRAY_BACKEND=appindicator\n" if indicator else ""
    service.write_text(f"""[Unit]
Description=Monitor Dimmer — auto-dim secondary screen during games/media
After=graphical-session.target
PartOf=graphical-session.target

[Service]
Type=simple
ExecStart={BIN_DIR}/monitor-dimmer
# Put the brightness back if the daemon died while a monitor was dimmed
ExecStopPost={BIN_DIR}/monitor-dimmer --restore
Restart=on-failure
RestartSec=5
{backend}
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
    icon = str(ICON_DEST) if ICON_DEST.exists() else "video-display"
    entry = APP_DIR / "dimmer.desktop"
    entry.write_text(f"""[Desktop Entry]
Version=1.0
Type=Application
Name=Monitor Dimmer
Comment=Configure auto-dimming of secondary screen during games or fullscreen media
Exec={BIN_DIR}/dimmer
Icon={icon}
Terminal=true
Categories=Settings;HardwareSettings;
Keywords=monitor;brightness;dim;gaming;
""")
    ok(f"Desktop entry written to {entry}")
    for tool in ("kbuildsycoca6", "kbuildsycoca5", "update-desktop-database"):
        if shutil.which(tool):
            run([tool] if tool.startswith("kbuild") else [tool, str(APP_DIR)])


def add_to_kde_favorites():
    if not KDE_STATS.exists():
        return
    content = KDE_STATS.read_text()
    entry   = "applications:dimmer.desktop"
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
    run(["systemctl", "--user", "restart", "monitor-dimmer.service"])
    time.sleep(2)
    r = run(["systemctl", "--user", "is-active", "monitor-dimmer.service"])
    if r.stdout.strip() == "active":
        ok("Service is running")
    else:
        warn("Service may not have started — check: systemctl --user status monitor-dimmer")


def test_dim():
    print(f"\n  {Y}Testing dim for 3 seconds — look at the chosen screens...{R}")
    r = run([str(BIN_DIR / "dimmer"), "--test"])
    print(r.stdout, end="")
    if r.returncode != 0:
        warn("Test failed — run `dimmer` and use \"Test dim\" to investigate")

# ── Summary & main ────────────────────────────────────────────────────────────

MODE_TEXT = {"off": "not dimmed", "overlay": "overlay (black window)",
             "ddc": "brightness (DDC/CI)"}


def on_off(v):
    return f"{G}on{R}" if v else f"{D}off{R}"


def print_summary(cfg, autostart):
    screens = "\n".join(f"    • {dc.screen_label(s)} — {MODE_TEXT[s['mode']]}"
                        for s in dc.active_screens(cfg)) or "    (none)"
    paths   = "\n".join(f"      • {p}" for p in cfg["game_paths"]) or "      (none)"
    print(f"""
{B}Summary{R}
  {D}SCREENS{R}
{screens}
    Dim intensity: {cfg['dim_level']}%
  {D}DETECTION{R}
    Games: {on_off(cfg['detect_games'])} · only when fullscreen: {on_off(cfg['require_fullscreen'])}
    Game folders:
{paths}
    VLC fullscreen: {on_off(cfg['vlc_fullscreen'])}
    Browser video: {on_off(cfg['video_fullscreen'])} · {', '.join(cfg['video_titles'])}
  {D}GENERAL{R}
    Poll interval: {cfg['poll_interval']} s · start at login: {on_off(autostart)}
""")


def main():
    banner()
    distro = detect_distro()

    # 1. Dependencies
    check_system_tools(distro)
    indicator = setup_venv(distro)

    # 2. Settings — an update can keep the existing ones
    cfg = dc.load_config()
    keep = False
    if dc.CONFIG_FILE.exists() and dc.active_screens(cfg):
        step("Existing settings found")
        print(f"  {D}Screens: {dc.screens_label(cfg)}{R}")
        keep = ask_yes_no("Keep your current settings and just update the program?")
    if keep:
        # keep the user's choice; a first install of the service defaults to on
        state = run(["systemctl", "--user", "is-enabled", "monitor-dimmer.service"]).stdout.strip()
        autostart = state in ("enabled", "") or "not-found" in state
    else:
        screens, dim_level = pick_screens()
        detection = pick_detection()
        general, autostart = pick_general()
        cfg.update(detection, **general, dim_level=dim_level, screens=screens)

        print_summary(cfg, autostart)
        if not ask_yes_no("Proceed with installation?"):
            print(f"\n  {D}Installation cancelled.{R}\n")
            sys.exit(0)

    # 3. Install
    step("Installing")
    install_icon()
    write_config(cfg)
    install_scripts()
    install_service(autostart, indicator)
    install_desktop_entry()

    # 4. KDE favorites (optional)
    desktop = os.environ.get("XDG_CURRENT_DESKTOP", "")
    if "KDE" in desktop and not keep:
        if ask_yes_no("\nAdd to KDE application launcher favorites?"):
            add_to_kde_favorites()

    # 5. Start
    step("Starting service")
    start_service()

    # 6. Test
    if ask_yes_no("\nRun a quick dim test now?"):
        test_dim()

    print(f"""
{G}{B}✓ Installation complete!{R}

  • System tray icon: auto-dim, dim now, screens and detection
    (it may be hidden — click ▲ in the taskbar)
  • All settings: run {B}dimmer{R}
  • To update:    git pull, then run this installer again
  • To uninstall: run {B}python3 {REPO_DIR}/uninstall.py{R}
""")


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, EOFError):
        print(f"\n\n  {D}Installation cancelled.{R}\n")
        sys.exit(0)
