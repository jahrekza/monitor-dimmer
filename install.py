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
from pathlib import Path

# ── Paths ────────────────────────────────────────────────────────────────────

REPO_DIR = Path(__file__).parent.resolve()
SRC_DIR  = REPO_DIR / "src"
ASSETS   = REPO_DIR / "assets"

PROGRAMS = ["monitor-dimmer", "monitor-dimmer-overlay", "dimmer"]
MODULES  = ["dimmer_common.py", "backend_linux.py", "backend_windows.py"]
OBSOLETE = ["fullscreen_watch.py"]          # merged into backend_linux.py in 2.2

sys.path.insert(0, str(SRC_DIR))
import dimmer_common as dc  # noqa: E402

# Where things go, how the daemon is started, where games live: all of that
# differs between systems and lives in the backend dimmer_common picked.
backend  = dc.backend
APP_HOME = backend.APP_HOME
VENV_DIR = backend.VENV_DIR
VENV_PY  = backend.VENV_PY

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
        # Our own programs speak UTF-8; the locale encoding would choke
        return subprocess.run(cmd, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", **kwargs)
    except FileNotFoundError:
        return subprocess.CompletedProcess(cmd, 127, "", f"{cmd[0]}: not found")

# ── Dependency check ──────────────────────────────────────────────────────────

def check_system_tools():
    """Optional helper programs, as the backend defines them."""
    step("Checking system tools")
    tools, fatal = backend.check_tools()
    missing = []
    for key, purpose, found in tools:
        if found:
            ok(key)
        else:
            warn(f"{key} — not found {D}(needed for {purpose}){R}")
            missing.append(key)
    if missing:
        hint = backend.install_hint(missing)
        if hint:
            print(f"\n  {D}Optional — install with:{R} {B}{hint}{R}")
    if not tools and not fatal:
        ok("nothing else needed on this system")
    if fatal:
        err(fatal)
        sys.exit(1)


def venv_has(module):
    return run([str(VENV_PY), "-c", f"import {module}"]).returncode == 0


def setup_venv():
    """Private virtualenv for the Python packages, so nothing touches the
    system Python (pip --user is refused on PEP 668 distros). Which packages
    may come from the system instead is the backend's call."""
    if dc.FROZEN:
        # Built into executables: Python, the virtualenv and pip are all
        # baked in, so there is nothing to set up.
        return
    step("Setting up Python environment")
    if not VENV_PY.exists():
        APP_HOME.mkdir(parents=True, exist_ok=True)
        r = run([sys.executable, "-m", "venv", *backend.VENV_OPTIONS, str(VENV_DIR)])
        if r.returncode != 0:
            err("Could not create a Python virtual environment")
            print(f"  {D}{r.stderr.strip()}{R}")
            hint = backend.install_hint(["venv"])
            if hint:
                print(f"  Install venv support: {B}{hint}{R}")
            sys.exit(1)
    ok(f"Virtual environment: {VENV_DIR}")

    pip_pkgs = []
    for pkg, module, offer_system in backend.PIP_PACKAGES:
        if venv_has(module):
            continue
        if not offer_system:
            pip_pkgs.append(pkg)
            continue
        warn(f"{pkg} not found (needed for the overlay and monitor list)")
        hint = backend.install_hint([pkg])
        if hint:
            print(f"  {D}Best from your system: {hint}{R}")
        if ask_yes_no(f"Install {pkg} into the environment with pip instead (~80 MB)?"):
            pip_pkgs.append(pkg)
    if pip_pkgs:
        print(f"  {D}Installing {', '.join(pip_pkgs)}...{R}")
        r = run([str(VENV_PY), "-m", "pip", "install", "--quiet", "--upgrade", *pip_pkgs])
        if r.returncode != 0:
            err("pip install failed")
            print(f"  {D}{r.stderr.strip()[-800:]}{R}")
            sys.exit(1)
    for pkg, module, _ in backend.PIP_PACKAGES:
        if venv_has(module):
            ok(f"python {pkg}")
        else:
            warn(f"python {pkg} — missing")

    for label, present, hint in backend.post_venv_check(VENV_PY):
        if present:
            ok(label)
        else:
            warn(f"{label} — not found; the daemon works without it")
            if hint:
                print(f"  {D}{hint}{R}")


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

def detect_game_paths():
    """[(label, folder)] worth offering, from the backend's launchers."""
    found, seen = [], set()

    def add(label, path):
        real = os.path.normcase(os.path.realpath(path))
        if real not in seen and Path(path).is_dir():
            seen.add(real)
            found.append((label, str(path)))

    for lib in backend.steam_libraries():
        add("Steam library", lib)
    for label, path in backend.other_game_paths():
        add(label, path)
    return found


def pick_game_paths():
    print(f"\n  {B}Game folders{R}")
    print(f"  {D}Steam games are found without folders. A process running from a folder{R}")
    print(f"  {D}{backend.GAME_PATHS_NOTE}{R}\n")
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
        print(f"  {D}{backend.FULLSCREEN_NOTE}{R}")
        fullscreen = ask_yes_no("…only while a window is fullscreen?", default=False)
        game_paths = pick_game_paths()

    print()
    vlc = ask_yes_no("Dim while VLC is fullscreen?")

    print()
    print(f"  {D}Video sites: YouTube, Netflix, Twitch, Prime Video, Disney+, Max, Vimeo,{R}")
    print(f"  {D}Crunchyroll, Player.pl, TVP VOD, CDA, RTÉ Player and more ({len(dc.VIDEO_SITES)} in total){R}")
    video = ask_yes_no("Dim while one of these plays fullscreen in the browser?")
    titles = list(dc.VIDEO_SITES)
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


def install_icons():
    icon, messages = backend.install_icons(ASSETS)
    for m in messages:
        ok(m)
    if icon is None and not messages:
        warn("App icon not found in assets/ — skipping")
    return icon


def install_scripts():
    APP_HOME.mkdir(parents=True, exist_ok=True)
    if dc.FROZEN:
        source = dc.PROGRAM_DIR
        if source != APP_HOME:
            for item in source.iterdir():
                dest = APP_HOME / item.name
                if item.is_dir():
                    shutil.copytree(item, dest, dirs_exist_ok=True)
                else:
                    shutil.copy2(item, dest)
        ok(f"Programs installed to {APP_HOME}")
        for launcher in backend.write_launchers(PROGRAMS):
            ok(f"Installed {launcher}")
        note = backend.path_warning()
        if note:
            warn(note)
        return
    for name in PROGRAMS + MODULES:
        shutil.copy2(SRC_DIR / name, APP_HOME / name)
    for name in OBSOLETE:
        (APP_HOME / name).unlink(missing_ok=True)
    # so `monitor-dimmer --uninstall` works after the download folder is gone
    shutil.copy2(REPO_DIR / "uninstall.py", APP_HOME / "uninstall.py")
    ok(f"Programs installed to {APP_HOME}")
    for launcher in backend.write_launchers(PROGRAMS):
        ok(f"Installed {launcher}")
    note = backend.path_warning()
    if note:
        warn(note)


def install_service(autostart):
    ok(backend.install_startup(autostart))


def install_desktop_entry(icon):
    entry = backend.install_menu_entry(icon)
    if entry:
        ok(f"Menu entry written to {entry}")


def add_to_favorites():
    note = backend.add_to_favorites()
    if note:
        ok(note)


def start_service():
    if backend.start_daemon():
        ok("Monitor Dimmer is running")
    else:
        warn("It may not have started — run `dimmer` and check Service")


def installed_command(name, *args):
    """Run one of the programs we have just installed."""
    args = [str(a) for a in args]
    if dc.FROZEN:
        return [str(APP_HOME / dc.APP_EXE), "--role", dc.ROLES[name], *args]
    return [str(VENV_PY), str(APP_HOME / name), *args]


def test_dim():
    print(f"\n  {Y}Testing dim for 3 seconds — look at the chosen screens...{R}")
    r = run(installed_command("dimmer", "--test"))
    print(r.stdout, end="")
    if r.returncode != 0:
        warn("Test failed — run `dimmer` and use \"Test dim\" to investigate")

# ── Summary & main ────────────────────────────────────────────────────────────

MODE_TEXT = {"off": "not dimmed", "overlay": "overlay (black window)",
             "ddc": "brightness (DDC/CI)"}


def on_off(v):
    return f"{G}on{R}" if v else f"{D}off{R}"


def how_to_run():
    """(settings, uninstall, leftover): how the user reaches us afterwards."""
    if dc.FROZEN:
        return ("Monitor Dimmer in the Start menu",
                "%s --role uninstall" % (APP_HOME / dc.APP_EXE),
                "This installer file is no longer needed and can be deleted")
    return ("run dimmer",
            "run monitor-dimmer --uninstall",
            "This download folder is no longer needed and can be deleted")


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
    Video sites: {on_off(cfg['video_fullscreen'])} · {len(cfg['video_titles'])} sites
  {D}GENERAL{R}
    Poll interval: {cfg['poll_interval']} s · start at login: {on_off(autostart)}
""")


def main():
    backend.prepare_console()
    banner()

    # 1. Dependencies
    check_system_tools()
    setup_venv()

    # 2. Settings — an update can keep the existing ones
    cfg = dc.load_config()
    keep = False
    if dc.CONFIG_FILE.exists() and dc.active_screens(cfg):
        step("Existing settings found")
        print(f"  {D}Screens: {dc.screens_label(cfg)}{R}")
        keep = ask_yes_no("Keep your current settings and just update the program?")
    if keep:
        # keep the user's choice; a first install defaults to starting at login
        autostart = backend.autostart_enabled() or not (APP_HOME / "monitor-dimmer").exists()
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
    # Windows will not let us overwrite the running program
    if backend.service_status() == "active":
        backend.service_action("stop")
        ok("Stopped the running Monitor Dimmer")
    write_config(cfg)
    install_scripts()
    icon = install_icons()
    install_service(autostart)
    install_desktop_entry(icon)

    # 4. Pin it, where that means anything
    prompt = backend.favorites_prompt()
    if prompt and not keep and ask_yes_no("\n" + prompt):
        add_to_favorites()

    # 5. Start
    step("Starting service")
    start_service()

    # 6. Test
    if ask_yes_no("\nRun a quick dim test now?"):
        test_dim()

    settings_hint, uninstall_hint, leftover_hint = how_to_run()
    print(f"""
{G}{B}✓ Installation complete!{R}

  • System tray icon: auto-dim, dim now, screens and detection
    (it may be hidden — click ▲ in the taskbar)
  • All settings: {B}{settings_hint}{R}
  • To update:    run this installer again
  • To uninstall: {B}{uninstall_hint}{R}
  • {leftover_hint}
""")


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, EOFError):
        print(f"\n\n  {D}Installation cancelled.{R}\n")
        sys.exit(0)
