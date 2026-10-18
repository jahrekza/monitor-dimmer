#!/usr/bin/env python3
"""Build the Windows executables.

    python packaging/build_windows.py

Produces two things in dist/:

  MonitorDimmer/            the application — one folder, one MonitorDimmer.exe
                            that is the daemon, the overlay, the settings
                            wizard and the uninstaller, told apart by --role
  monitor-dimmer-setup.exe  a single file carrying that folder inside it;
                            this is what someone downloads and double-clicks

Needs PyInstaller and Pillow:  pip install pyinstaller pillow
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO    = Path(__file__).resolve().parent.parent
SRC     = REPO / "src"
ASSETS  = REPO / "assets"
BUILD   = REPO / "build"
STAGE   = BUILD / "stage"
DIST    = REPO / "dist"
APP_DIR = DIST / "MonitorDimmer"
ICON    = BUILD / "monitor-dimmer.ico"

# staged name -> the program it is a copy of
PROGRAMS = {
    "daemon_main.py":    SRC / "monitor-dimmer",
    "overlay_main.py":   SRC / "monitor-dimmer-overlay",
    "wizard_main.py":    SRC / "dimmer",
    "setup_main.py":     REPO / "install.py",
    "uninstall_main.py": REPO / "uninstall.py",
}
MODULES = ["dimmer_common.py", "backend_windows.py", "backend_linux.py"]


def step(text):
    print(f"\n== {text}")


def stage():
    """PyInstaller imports its inputs, so give the programs importable names."""
    if STAGE.exists():
        shutil.rmtree(STAGE)
    STAGE.mkdir(parents=True)
    for name, source in PROGRAMS.items():
        shutil.copy2(source, STAGE / name)
    for name in MODULES:
        shutil.copy2(SRC / name, STAGE / name)
    shutil.copy2(REPO / "packaging" / "app.py", STAGE / "app.py")
    print(f"  staged {len(PROGRAMS) + len(MODULES) + 1} files in {STAGE}")


def make_icon():
    png = ASSETS / "dimmer.png"
    if not png.exists():
        print("  no assets/dimmer.png — building without an icon")
        return None
    try:
        from PIL import Image
    except ImportError:
        print("  Pillow missing — building without an icon")
        return None
    Image.open(png).convert("RGBA").save(
        ICON, sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (256, 256)])
    print(f"  icon: {ICON}")
    return ICON


def pyinstaller(*args):
    cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm",
           "--workpath", str(BUILD / "work"), "--distpath", str(DIST), *args]
    print("  " + " ".join(cmd[3:]))
    r = subprocess.run(cmd)
    if r.returncode != 0:
        sys.exit("PyInstaller failed")


def build_app(icon):
    """The application itself: one folder, one executable, no console."""
    args = ["--name", "MonitorDimmer", "--onedir", "--windowed",
            "--paths", str(STAGE),
            "--specpath", str(BUILD)]
    for module in ("dimmer_common", "backend_windows", "daemon_main",
                   "overlay_main", "wizard_main", "setup_main", "uninstall_main"):
        args += ["--hidden-import", module]
    if icon:
        args += ["--icon", str(icon)]
    pyinstaller(*args, str(STAGE / "app.py"))
    if icon:
        shutil.copy2(icon, APP_DIR / "monitor-dimmer.ico")


def build_setup(icon):
    """One downloadable file carrying the application inside it."""
    sep = ";" if os.name == "nt" else ":"
    args = ["--name", "monitor-dimmer-setup", "--onefile", "--console",
            "--paths", str(STAGE), "--specpath", str(BUILD),
            "--add-data", f"{APP_DIR}{sep}app"]
    for module in ("dimmer_common", "backend_windows", "setup_main"):
        args += ["--hidden-import", module]
    if icon:
        args += ["--icon", str(icon)]
    pyinstaller(*args, str(STAGE / "app.py"))


def main():
    if sys.platform != "win32":
        sys.exit("This build script makes the Windows executables; run it on Windows.")
    step("Staging the programs")
    stage()
    step("Icon")
    icon = make_icon()
    step("Building the application (dist/MonitorDimmer)")
    build_app(icon)
    step("Building the downloadable installer (dist/monitor-dimmer-setup.exe)")
    build_setup(icon)
    step("Done")
    print(f"  application: {APP_DIR / 'MonitorDimmer.exe'}")
    print(f"  installer:   {DIST / 'monitor-dimmer-setup.exe'}")


if __name__ == "__main__":
    main()
