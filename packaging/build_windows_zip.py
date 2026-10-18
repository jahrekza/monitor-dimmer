#!/usr/bin/env python3
"""Build the downloadable Windows package.

    python packaging/build_windows_zip.py

Produces dist/monitor-dimmer-windows.zip: the programs, the Python packages
they need, and an official embeddable Python to run them with. Whoever
downloads it needs no git, no Python and no administrator rights — they
unpack it and double-click "Install Monitor Dimmer.cmd".

Why not a single built .exe: PyInstaller produces an unsigned executable,
and Smart App Control (on by default on a clean Windows 11) judges those by
Microsoft's reputation service and may refuse to run them. The embeddable
distribution's python.exe is signed by the Python Software Foundation, so
nothing here is unsigned.
"""
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

REPO   = Path(__file__).resolve().parent.parent
SRC    = REPO / "src"
BUILD  = REPO / "build"
BUNDLE = BUILD / "bundle"
DIST   = REPO / "dist"
NAME   = "monitor-dimmer-windows"

PROGRAMS = ["monitor-dimmer", "monitor-dimmer-overlay", "dimmer"]
MODULES  = ["dimmer_common.py", "backend_windows.py", "backend_linux.py"]
PACKAGES = ["PyQt6", "pystray", "Pillow"]

LAUNCHER = """@echo off
rem Runs the installer with the Python shipped alongside, so nothing has to
rem be installed first.
setlocal
cd /d "%~dp0"
"%~dp0python\\python.exe" "%~dp0install.py"
if errorlevel 1 pause
"""


def step(text):
    print(f"\n== {text}")


def embeddable_python():
    """Download the official embeddable build matching this interpreter."""
    version = "%d.%d.%d" % sys.version_info[:3]
    archive = BUILD / f"python-{version}-embed-amd64.zip"
    if not archive.exists():
        url = (f"https://www.python.org/ftp/python/{version}/"
               f"python-{version}-embed-amd64.zip")
        print(f"  downloading {url}")
        BUILD.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(url, timeout=300) as r:
            archive.write_bytes(r.read())
    print(f"  {archive.name} ({archive.stat().st_size / 1048576:.1f} MB)")
    return archive


def unpack_python(archive):
    target = BUNDLE / "python"
    target.mkdir(parents=True)
    with zipfile.ZipFile(archive) as z:
        z.extractall(target)
    # The embeddable build keeps imports to itself; point it at the programs
    # beside it and at the packages we are about to install.
    pth = next(target.glob("python*._pth"))
    stdlib = next((line for line in pth.read_text().splitlines()
                   if line.endswith(".zip")), "python3.zip")
    pth.write_text("\n".join([stdlib, ".", "..", "..\\lib", "import site"]) + "\n",
                   encoding="utf-8")
    print(f"  python unpacked, {pth.name} points at the programs and ..\\lib")
    return target


def install_packages():
    lib = BUNDLE / "lib"
    print(f"  pip install {' '.join(PACKAGES)} -> {lib}")
    r = subprocess.run([sys.executable, "-m", "pip", "install", "--quiet",
                        "--target", str(lib), *PACKAGES])
    if r.returncode != 0:
        sys.exit("pip install failed")
    prune(lib)
    size = sum(f.stat().st_size for f in lib.rglob("*") if f.is_file())
    print(f"  {size / 1048576:.0f} MB of packages")


# Shipped by PyQt6, never used by the overlay. The developer tools matter
# most: they are the only unsigned executables that would end up in the
# package, and Windows is happier when there are none.
UNUSED = ["bin",
          "PyQt6/Qt6/qml",
          "PyQt6/Qt6/translations",
          "PyQt6/Qt6/qsci"]


def prune(lib):
    for name in UNUSED:
        target = lib / name
        if not target.exists():
            continue
        freed = sum(f.stat().st_size for f in target.rglob("*") if f.is_file())
        shutil.rmtree(target)
        print(f"  dropped {name} ({freed / 1048576:.0f} MB)")


def copy_programs():
    for name in PROGRAMS + MODULES:
        shutil.copy2(SRC / name, BUNDLE / name)
    for name in ("install.py", "uninstall.py"):
        shutil.copy2(REPO / name, BUNDLE / name)
    assets = BUNDLE / "assets"
    assets.mkdir(exist_ok=True)
    for name in ("dimmer.png", "monitor-dimmer-symbolic.svg"):
        source = REPO / "assets" / name
        if source.exists():
            shutil.copy2(source, assets / name)
    (BUNDLE / "Install Monitor Dimmer.cmd").write_text(LAUNCHER, encoding="utf-8",
                                                       newline="\r\n")
    print(f"  programs, modules, assets and the launcher")


def make_zip():
    DIST.mkdir(parents=True, exist_ok=True)
    archive = DIST / f"{NAME}.zip"
    archive.unlink(missing_ok=True)
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for item in sorted(BUNDLE.rglob("*")):
            if item.is_file():
                z.write(item, Path(NAME) / item.relative_to(BUNDLE))
    print(f"  {archive} ({archive.stat().st_size / 1048576:.0f} MB)")
    return archive


def main():
    if sys.platform != "win32":
        sys.exit("This builds the Windows package; run it on Windows.")
    if BUNDLE.exists():
        shutil.rmtree(BUNDLE)
    BUNDLE.mkdir(parents=True)
    step("Embeddable Python")
    unpack_python(embeddable_python())
    step("Python packages")
    install_packages()
    step("Programs")
    copy_programs()
    step("Packing")
    archive = make_zip()
    step("Done")
    print(f"  Unpack it anywhere and run \"Install Monitor Dimmer.cmd\".")
    print(f"  {archive}")


if __name__ == "__main__":
    main()
