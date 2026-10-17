#!/usr/bin/env python3
"""Monitor Dimmer — Uninstaller"""
import shutil
import subprocess
import sys
from pathlib import Path

HOME    = Path.home()
R = "\033[0m"; B = "\033[1m"; G = "\033[92m"; Y = "\033[93m"; E = "\033[91m"

FILES = [
    HOME / ".local/bin/monitor-dimmer",
    HOME / ".local/bin/monitor-dimmer-overlay",
    HOME / ".local/bin/dimmer",
    HOME / ".config/systemd/user/monitor-dimmer.service",
    HOME / ".local/share/applications/dimmer.desktop",
    HOME / ".local/share/icons/monitor-dimmer.png",
    HOME / ".local/share/icons/hicolor/scalable/apps/monitor-dimmer-symbolic.svg",
    HOME / ".config/monitor-dimmer.conf",
]

DIRS = [
    HOME / ".local/share/monitor-dimmer",       # programs + private venv
    HOME / ".local/state/monitor-dimmer",
]

def ask_yes_no(prompt, default=True):
    hint = f"{B}Y{R}/n" if default else f"y/{B}N{R}"
    raw = input(f"  {prompt} [{hint}]: ").strip().lower()
    if raw == "": return default
    return raw in ("y", "yes")

def systemctl(*args):
    try:
        subprocess.run(["systemctl", "--user", *args], capture_output=True)
    except FileNotFoundError:
        pass

print(f"\n{B}Monitor Dimmer — Uninstaller{R}\n")

if not ask_yes_no("Remove Monitor Dimmer and all its files?", default=False):
    print("  Cancelled.\n")
    sys.exit(0)

# Stopping runs ExecStopPost, which restores a dimmed monitor's brightness
systemctl("stop",    "monitor-dimmer.service")
systemctl("disable", "monitor-dimmer.service")

for f in FILES:
    if f.exists() or f.is_symlink():
        f.unlink()
        print(f"  {G}✓{R}  Removed {f}")
    else:
        print(f"  {Y}–{R}  Not found: {f}")

for d in DIRS:
    if d.exists():
        shutil.rmtree(d)
        print(f"  {G}✓{R}  Removed {d}")

systemctl("daemon-reload")

# Remove from KDE favorites
kde_stats = HOME / ".config/kactivitymanagerd-statsrc"
if kde_stats.exists():
    content = kde_stats.read_text()
    if "dimmer.desktop" in content:
        content = content.replace(",applications:dimmer.desktop", "")
        content = content.replace("applications:dimmer.desktop,", "")
        kde_stats.write_text(content)
        print(f"  {G}✓{R}  Removed from KDE favorites")

print(f"\n{G}{B}✓ Uninstall complete.{R}\n")
