#!/usr/bin/env python3
"""Monitor Dimmer — Uninstaller"""
import subprocess
import sys
from pathlib import Path

HOME    = Path.home()
R = "\033[0m"; B = "\033[1m"; G = "\033[92m"; Y = "\033[93m"; E = "\033[91m"

FILES = [
    HOME / ".local/bin/monitor-dimmer",
    HOME / ".local/bin/monitor-dimmer-overlay",
    HOME / ".local/bin/monitor-dimmer-config",
    HOME / ".config/systemd/user/monitor-dimmer.service",
    HOME / ".local/share/applications/monitor-dimmer-config.desktop",
    HOME / ".config/monitor-dimmer.conf",
]

def ask_yes_no(prompt, default=True):
    hint = f"{B}Y{R}/n" if default else f"y/{B}N{R}"
    raw = input(f"  {prompt} [{hint}]: ").strip().lower()
    if raw == "": return default
    return raw in ("y", "yes")

print(f"\n{B}Monitor Dimmer — Uninstaller{R}\n")

if not ask_yes_no("Remove Monitor Dimmer and all its files?", default=False):
    print("  Cancelled.\n")
    sys.exit(0)

subprocess.run(["systemctl", "--user", "stop",    "monitor-dimmer.service"], capture_output=True)
subprocess.run(["systemctl", "--user", "disable", "monitor-dimmer.service"], capture_output=True)
subprocess.run(["systemctl", "--user", "daemon-reload"],                     capture_output=True)

for f in FILES:
    if f.exists():
        f.unlink()
        print(f"  {G}✓{R}  Removed {f}")
    else:
        print(f"  {Y}–{R}  Not found: {f}")

# Remove from KDE favorites
kde_stats = HOME / ".config/kactivitymanagerd-statsrc"
if kde_stats.exists():
    content = kde_stats.read_text()
    if "monitor-dimmer-config.desktop" in content:
        content = content.replace(",applications:monitor-dimmer-config.desktop", "")
        content = content.replace("applications:monitor-dimmer-config.desktop,", "")
        kde_stats.write_text(content)
        print(f"  {G}✓{R}  Removed from KDE favorites")

print(f"\n{G}{B}✓ Uninstall complete.{R}\n")
