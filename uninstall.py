#!/usr/bin/env python3
"""Monitor Dimmer — Uninstaller

Run it from the download folder, or as `monitor-dimmer --uninstall` once it
is installed. What gets removed differs between systems, so the list comes
from the backend.
"""
import os
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
for candidate in (HERE / "src", HERE):          # download folder, or installed
    if (candidate / "dimmer_common.py").exists():
        sys.path.insert(0, str(candidate))
        break
import dimmer_common as dc  # noqa: E402

backend = dc.backend

R = "\033[0m"; B = "\033[1m"; G = "\033[92m"; Y = "\033[93m"; E = "\033[91m"


def ask_yes_no(prompt, default=True):
    hint = f"{B}Y{R}/n" if default else f"y/{B}N{R}"
    raw = input(f"  {prompt} [{hint}]: ").strip().lower()
    if raw == "":
        return default
    return raw in ("y", "yes")


def restore_brightness():
    """A monitor dimmed by DDC/CI must not stay dark once we are gone."""
    state = dc.load_state()
    if not state:
        return
    for entry in state:
        dc.set_brightness(entry["bus"], entry["brightness"])
    dc.save_state([])
    print(f"  {G}✓{R}  Restored the brightness of a dimmed monitor")


def main():
    backend.prepare_console()
    print(f"\n{B}Monitor Dimmer — Uninstaller{R}\n")

    files, dirs = backend.uninstall_items()
    if not ask_yes_no("Remove Monitor Dimmer and all its files?", default=False):
        print("  Cancelled.\n")
        return

    backend.stop_for_uninstall()
    restore_brightness()

    # Never stand inside a folder we are about to delete
    try:
        os.chdir(Path.home())
    except OSError:
        pass

    for f in files:
        try:
            if f.exists() or f.is_symlink():
                f.unlink()
                print(f"  {G}✓{R}  Removed {f}")
            else:
                print(f"  {Y}–{R}  Not found: {f}")
        except OSError as err:
            print(f"  {E}✗{R}  Could not remove {f}: {err}")

    for d in dirs:
        if not d.exists():
            print(f"  {Y}–{R}  Not found: {d}")
            continue
        try:
            shutil.rmtree(d)
            print(f"  {G}✓{R}  Removed {d}")
        except OSError as err:
            # On Windows the programs live where we are running from, so a
            # file we still hold open can survive this.
            print(f"  {E}✗{R}  Could not remove {d}: {err}")
            print(f"      Delete it by hand after closing this window.")

    for note in backend.uninstall_cleanup():
        print(f"  {G}✓{R}  {note}")

    print(f"\n{G}{B}✓ Uninstall complete.{R}\n")


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, EOFError):
        print("\n  Cancelled.\n")
