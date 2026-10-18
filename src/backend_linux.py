"""Linux backend: everything Monitor Dimmer needs from the operating system.

The daemon, settings and installer talk to the system only through the
functions in this module; backend_windows.py offers the same interface.
"""
import json
import os
import re
import shutil
import sys
import subprocess
import threading
import time
from pathlib import Path

NAME = "linux"

# ── Paths ─────────────────────────────────────────────────────────────────────

CONFIG_FILE = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "monitor-dimmer.conf"
STATE_DIR   = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "monitor-dimmer"


# ── DDC/CI (ddcutil) ──────────────────────────────────────────────────────────
#
# What to call it when telling the user a monitor did not answer.
BRIGHTNESS_SOURCE = "ddcutil"

def ddc_displays():
    """Monitors that answer DDC/CI, as [{bus, connector, mfg, model, sn}]."""
    try:
        out = subprocess.run(["ddcutil", "detect"], capture_output=True,
                             text=True, timeout=30).stdout
    except (OSError, subprocess.TimeoutExpired):
        return []
    displays, current = [], None
    for line in out.splitlines():
        if re.match(r"^Display \d+", line):
            current = {}
            displays.append(current)
        elif re.match(r"^\S", line):
            current = None                       # "Invalid display": no DDC/CI
        elif current is None:
            continue
        elif "I2C bus:" in line:
            m = re.search(r"/dev/i2c-(\d+)", line)
            if m:
                current["bus"] = int(m.group(1))
        elif re.search(r"DRM[ _]connector:", line):   # "DRM_connector:" in full output
            # "card1-DP-3" -> "DP-3", the name compositors use
            current["connector"] = re.sub(r"^card\d+-", "", line.split(":", 1)[1].strip())
        elif "Mfg id:" in line:
            current["mfg"] = line.split(":", 1)[1].split(" - ")[0].strip()
        elif "Model:" in line:
            current["model"] = line.split(":", 1)[1].strip()
        elif "Serial number:" in line:
            current["sn"] = line.split(":", 1)[1].strip()
    return [d for d in displays if "bus" in d]


def ddc_label(bus):
    return f"i2c-{bus}"


def get_brightness(bus):
    """Return (current, max) brightness, or None."""
    try:
        out = subprocess.run(["ddcutil", "getvcp", "10", "--brief", "--bus", str(bus)],
                             capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    m = re.search(r"VCP 10 C (\d+) (\d+)", out)
    return (int(m.group(1)), int(m.group(2))) if m else None


def set_brightness(bus, value):
    try:
        r = subprocess.run(["ddcutil", "setvcp", "10", str(value), "--bus", str(bus)],
                           capture_output=True, text=True, timeout=10)
        return r.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


# ── Games ─────────────────────────────────────────────────────────────────────

_STEAM_HELPER_PATHS = (
    "/ubuntu12_32/",
    "/ubuntu12_64/",
    "/.steam/root/",
    "/steam-runtime/",
    "/pressure-vessel/",
)

def _has_steam_game_id(pid_dir):
    """Check if process has SteamGameId env var set to a non-zero value (i.e. it's a game, not the Steam client)."""
    try:
        environ = (pid_dir / "environ").read_bytes()
        if b"SteamGameId=" not in environ:
            return False
        for entry in environ.split(b"\x00"):
            if entry.startswith(b"SteamGameId="):
                val = entry.split(b"=", 1)[1]
                if not (val.isdigit() and int(val) > 0):
                    return False
                try:
                    exe = os.readlink(pid_dir / "exe")
                    if any(p in exe for p in _STEAM_HELPER_PATHS):
                        return False
                except OSError:
                    pass
                return True
    except OSError:
        pass
    return False


def game_dirs(paths):
    # /proc/*/exe is fully resolved: on Fedora Atomic /home is a symlink to
    # /var/home, so match both the configured and the resolved spelling
    return sorted({d.rstrip("/") + "/" for p in paths for d in (p, os.path.realpath(p))})


def _is_game_launch_arg(arg, dirs):
    # Wine shows the game as "Z:\home\...\game.exe"; only count executables,
    # so a file manager or shell sitting in a game folder doesn't trigger
    arg = arg.replace("\\", "/")
    if arg[1:3] == ":/":
        arg = arg[2:]
    return arg.lower().endswith(".exe") and any(arg.startswith(d) for d in dirs)


def game_running(dirs):
    """True if a Steam game or a process from one of `dirs` is running."""
    for pid_dir in Path("/proc").iterdir():
        if not pid_dir.name.isdigit():
            continue
        try:
            if _has_steam_game_id(pid_dir):
                return True
            if dirs:
                exe = os.readlink(pid_dir / "exe")
                if any(exe.startswith(d) for d in dirs):
                    return True
                args = (pid_dir / "cmdline").read_bytes().decode("utf-8", errors="ignore")
                if any(_is_game_launch_arg(a, dirs) for a in args.split("\x00")):
                    return True
        except OSError:
            continue
    return False


# ── VLC ───────────────────────────────────────────────────────────────────────

def vlc_fullscreen(windows=None):
    """VLC reports fullscreen over MPRIS. (`windows`, a callable returning the
    fullscreen windows, is used by the Windows backend only.)"""
    try:
        names = subprocess.run(
            ["gdbus", "call", "--session", "--dest", "org.freedesktop.DBus",
             "--object-path", "/org/freedesktop/DBus",
             "--method", "org.freedesktop.DBus.ListNames"],
            capture_output=True, text=True, timeout=2).stdout
        # one name per VLC window: org.mpris.MediaPlayer2.vlc[.instanceNNN]
        for name in set(re.findall(r"org\.mpris\.MediaPlayer2\.vlc[\w.]*", names)):
            r = subprocess.run(
                ["gdbus", "call", "--session", "--dest", name,
                 "--object-path", "/org/mpris/MediaPlayer2",
                 "--method", "org.freedesktop.DBus.Properties.Get",
                 "org.mpris.MediaPlayer2", "Fullscreen"],
                capture_output=True, text=True, timeout=2)
            if "true" in r.stdout:
                return True
    except (OSError, subprocess.TimeoutExpired):
        pass
    return False


# ── Fullscreen windows ────────────────────────────────────────────────────────
#
# On KDE Plasma a small KWin script pushes the list of fullscreen windows to
# the daemon over D-Bus whenever it changes; it sees native Wayland and X11
# windows alike. Elsewhere X11/XWayland windows are inspected with xdotool +
# xprop on demand (native Wayland windows are invisible there).

BUS_NAME  = "io.github.jahrekza.MonitorDimmer"
OBJ_PATH  = "/io/github/jahrekza/MonitorDimmer"
IFACE     = "io.github.jahrekza.MonitorDimmer"
KWIN_NAME = "monitor-dimmer-fullscreen"

KWIN_SCRIPT = """
// Plasma 6 names first, Plasma 5 as fallback
function allWindows() {
    return workspace.windowList ? workspace.windowList() : workspace.clientList();
}
function report(gone) {
    var list = [];
    var ws = allWindows();
    for (var i = 0; i < ws.length; i++) {
        var w = ws[i];
        if (w.fullScreen && w !== gone)
            list.push({caption: String(w.caption), pid: w.pid,
                       cls: String(w.resourceClass)});
    }
    callDBus("%(bus)s", "%(path)s", "%(iface)s", "FullscreenWindows", JSON.stringify(list));
}
function watch(w) {
    w.fullScreenChanged.connect(function () { report(); });
    w.captionChanged.connect(function () { report(); });
}
allWindows().forEach(watch);
(workspace.windowAdded || workspace.clientAdded).connect(function (w) { watch(w); report(); });
// still listed while windowRemoved runs, so skip it explicitly
(workspace.windowRemoved || workspace.clientRemoved).connect(function (w) { report(w); });
report();
""" % {"bus": BUS_NAME, "path": OBJ_PATH, "iface": IFACE}

IFACE_XML = f"""<node><interface name="{IFACE}">
  <method name="FullscreenWindows"><arg type="s" direction="in"/></method>
</interface></node>"""


def _gdbus(*args, timeout=5):
    try:
        r = subprocess.run(["gdbus", "call", "--session", *args],
                           capture_output=True, text=True, timeout=timeout)
        return r.stdout if r.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def _kwin(method, *args):
    return _gdbus("--dest", "org.kde.KWin", "--object-path", "/Scripting",
                  "--method", f"org.kde.kwin.Scripting.{method}", *args)


class FullscreenWatcher:
    def __init__(self, script_path):
        self.script_path = script_path
        self.kwin = False
        self._windows = []
        self._lock = threading.Lock()

    # ── KWin push ─────────────────────────────────────────────────────────

    def start(self):
        """Try the KWin script; returns True if it is in use."""
        if _kwin("isScriptLoaded", KWIN_NAME) is None:
            return False                          # not KDE / no KWin scripting
        try:
            from gi.repository import Gio, GLib
        except ImportError:
            return False
        ready = threading.Event()
        threading.Thread(target=self._serve, args=(Gio, GLib, ready), daemon=True).start()
        if not ready.wait(5):
            return False
        self.script_path.parent.mkdir(parents=True, exist_ok=True)
        self.script_path.write_text(KWIN_SCRIPT)
        self.kwin = self._load_script()
        return self.kwin

    def _serve(self, Gio, GLib, ready):
        # Own main context: the tray backend may run its own GLib loop
        ctx = GLib.MainContext.new()
        ctx.push_thread_default()
        node = Gio.DBusNodeInfo.new_for_xml(IFACE_XML)

        def on_call(conn, sender, path, iface, method, params, invocation):
            try:
                data = json.loads(params.unpack()[0])
                windows = [{"caption": str(w.get("caption", "")),
                            "pid": int(w.get("pid") or 0),
                            "cls": str(w.get("cls", ""))} for w in data]
            except (ValueError, TypeError, AttributeError):
                windows = None
            if windows is not None:
                with self._lock:
                    self._windows = windows
            invocation.return_value(None)

        def on_bus(conn, name):
            conn.register_object(OBJ_PATH, node.interfaces[0], on_call, None, None)

        Gio.bus_own_name(Gio.BusType.SESSION, BUS_NAME, Gio.BusNameOwnerFlags.NONE,
                         on_bus, lambda *a: ready.set(), lambda *a: ready.set())
        GLib.MainLoop(ctx).run()

    def _load_script(self):
        _kwin("unloadScript", KWIN_NAME)          # a previous daemon's copy
        out = _kwin("loadScript", str(self.script_path), KWIN_NAME)
        m = re.search(r"-?\d+", out or "")
        if not m or int(m.group()) < 0:
            return False
        _gdbus("--dest", "org.kde.KWin", "--object-path", f"/Scripting/Script{m.group()}",
               "--method", "org.kde.kwin.Script.run")
        return True

    def check(self):
        """Reload the script if KWin lost it (e.g. KWin restarted)."""
        if self.kwin and "true" not in (_kwin("isScriptLoaded", KWIN_NAME) or "true"):
            self._load_script()

    def stop(self):
        if self.kwin:
            _kwin("unloadScript", KWIN_NAME)

    # ── Queries ───────────────────────────────────────────────────────────

    def windows(self):
        """Fullscreen windows as [{caption, pid, cls}]."""
        if self.kwin:
            with self._lock:
                return list(self._windows)
        return x11_fullscreen_windows()


def x11_fullscreen_windows():
    """Fullscreen X11/XWayland windows. None if xdotool/xprop are missing."""
    try:
        ids = subprocess.check_output(
            ["xdotool", "search", "--onlyvisible", "--name", ""],
            stderr=subprocess.DEVNULL, text=True, timeout=3).split()
    except FileNotFoundError:
        return None
    except (subprocess.SubprocessError, OSError):
        return []
    found = []
    for wid in ids:
        try:
            props = subprocess.check_output(
                ["xprop", "-id", wid, "_NET_WM_STATE", "_NET_WM_NAME", "_NET_WM_PID", "WM_CLASS"],
                stderr=subprocess.DEVNULL, text=True, timeout=2)
        except FileNotFoundError:
            return None
        except (subprocess.SubprocessError, OSError):
            continue
        if "_NET_WM_STATE_FULLSCREEN" not in props:
            continue
        name = re.search(r'_NET_WM_NAME\(\w+\) = "(.*)"', props)
        pid = re.search(r"_NET_WM_PID\(\w+\) = (\d+)", props)
        cls = re.search(r'WM_CLASS\(\w+\) = "[^"]*", "([^"]*)"', props)
        found.append({"caption": name.group(1) if name else "",
                      "pid": int(pid.group(1)) if pid else 0,
                      "cls": cls.group(1) if cls else ""})
    return found


# ── Service (systemd user unit) ───────────────────────────────────────────────

SERVICE = "monitor-dimmer.service"


def _systemctl(*args):
    try:
        return subprocess.run(["systemctl", "--user", *args, SERVICE],
                              capture_output=True, text=True)
    except OSError:
        return None


def service_status():
    """"active", "inactive", "failed", ... or "unknown"."""
    r = _systemctl("is-active")
    return r.stdout.strip() if r else "unknown"


def service_action(action):
    """start / stop / restart. Returns an error message, or None."""
    r = _systemctl(action)
    if r is None:
        return "systemctl not found"
    return None if r.returncode == 0 else (r.stderr.strip() or f"systemctl {action} failed")


def autostart_enabled():
    r = _systemctl("is-enabled")
    return bool(r) and r.stdout.strip() == "enabled"


def set_autostart(enabled):
    return service_action("enable" if enabled else "disable")


def register_daemon():
    """Only the Windows backend tracks the daemon itself (no service manager)."""


def unregister_daemon():
    pass


def stop_requested():
    """Windows has no SIGTERM; on Linux systemd stops us with a signal."""
    return False


# ── Terminal ──────────────────────────────────────────────────────────────────

# How each terminal takes a command to run
TERMINALS = [
    ("xdg-terminal-exec", []),
    ("konsole",           ["-e"]),
    ("ptyxis",            ["--"]),
    ("kgx",               ["--"]),
    ("gnome-terminal",    ["--"]),
    ("xfce4-terminal",    ["-x"]),
    ("alacritty",         ["-e"]),
    ("kitty",             []),
    ("foot",              []),
    ("wezterm",           ["start", "--"]),
    ("x-terminal-emulator", ["-e"]),
    ("xterm",             ["-e"]),
]


class OverlayProcess:
    """One overlay window. On Linux the process we start is the process whose
    window appears, and the service manager cleans up if the daemon dies, so
    a plain child process is all this needs."""

    def __init__(self, cmd):
        self._p = subprocess.Popen(cmd)

    @property
    def pids(self):
        return {self._p.pid}

    def poll(self):
        return self._p.poll()

    def stop(self, timeout=2):
        if self._p.poll() is None:
            self._p.terminate()
            try:
                self._p.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                self._p.kill()


def spawn_overlay(cmd):
    return OverlayProcess(cmd)


def prepare_console():
    """Nothing to do: a Linux terminal already speaks UTF-8 and ANSI."""


def open_in_terminal(cmd):
    """Run `cmd` (a list) in a new terminal window. Returns True on success."""
    candidates = list(TERMINALS)
    user = os.environ.get("TERMINAL")
    if user:
        candidates.insert(0, (user, dict(TERMINALS).get(Path(user).name, ["-e"])))
    for term, flag in candidates:
        if shutil.which(term):
            subprocess.Popen([term, *flag, *cmd], start_new_session=True)
            return True
    return False


# ── Installation ──────────────────────────────────────────────────────────────
#
# An install is: a folder with the programs and a private virtualenv, shell
# launchers in ~/.local/bin, a .desktop entry, and a systemd user service.

HOME        = Path.home()
APP_HOME    = HOME / ".local/share/monitor-dimmer"
VENV_DIR    = APP_HOME / "venv"
VENV_PY     = VENV_DIR / "bin/python"
BIN_DIR     = HOME / ".local/bin"
SERVICE_DIR = HOME / ".config/systemd/user"
SERVICE     = SERVICE_DIR / "monitor-dimmer.service"
APP_DIR     = HOME / ".local/share/applications"
DESKTOP     = APP_DIR / "dimmer.desktop"
ICONS_DIR   = HOME / ".local/share/icons"
ICON_DEST   = ICONS_DIR / "monitor-dimmer.png"
SVG_DEST    = ICONS_DIR / "hicolor/scalable/apps/monitor-dimmer-symbolic.svg"
KDE_STATS   = HOME / ".config/kactivitymanagerd-statsrc"

# PyGObject and PyQt6 are best taken from the distro, so the environment can
# see the system packages.
VENV_OPTIONS = ["--system-site-packages"]
# (package, import name, offer the distro package first)
PIP_PACKAGES = [("pystray", "pystray", False), ("Pillow", "PIL", False),
                ("PyQt6", "PyQt6", True)]

INSTALL_CMD = {
    "arch":   "sudo pacman -S {pkg}",
    "debian": "sudo apt install {pkg}",
    "fedora": "sudo dnf install {pkg}",
    "suse":   "sudo zypper install {pkg}",
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

# Wording the installer shows, where it depends on the system
GAME_PATHS_NOTE = "listed here (or a Windows .exe from it, via Wine) counts as a game."
FULLSCREEN_NOTE = ("Only when fullscreen: KDE sees all windows; "
                   "elsewhere only X11/XWayland games")

_indicator = False          # set by post_venv_check, used by install_startup


def _distro():
    fields = {}
    try:
        for line in Path("/etc/os-release").read_text(encoding="utf-8").splitlines():
            k, _, v = line.partition("=")
            fields[k] = v.strip().strip('"').lower()
    except OSError:
        return "unknown"
    ids = [fields.get("ID", "")] + fields.get("ID_LIKE", "").split()
    families = {"arch": "arch", "debian": "debian", "ubuntu": "debian",
                "fedora": "fedora", "rhel": "fedora", "suse": "suse"}
    for i in ids:
        for key, family in families.items():
            if key in i:
                return family
    return "unknown"


def install_hint(keys):
    distro = _distro()
    pkgs = [SYSTEM_PKGS.get(distro, {}).get(k, k) for k in keys]
    return INSTALL_CMD.get(distro, "install: {pkg}").format(pkg=" ".join(pkgs))


def check_tools():
    """[(key, purpose, found)] for optional tools, plus a fatal error or None."""
    optional = [
        ("ddcutil", "hardware brightness mode (DDC/CI)"),
        ("gdbus",   "VLC fullscreen detection"),
        ("xdotool", 'the "dim only while fullscreen" option'),
        ("xprop",   'the "dim only while fullscreen" option'),
    ]
    found = [(key, purpose, bool(shutil.which(key))) for key, purpose in optional]
    fatal = None
    if not shutil.which("systemctl"):
        fatal = "systemctl not found — the daemon runs as a systemd user service"
    return found, fatal


def post_venv_check(venv_py):
    """[(label, ok, hint)] for what pip cannot provide."""
    global _indicator
    _indicator = any(
        subprocess.run([str(venv_py), "-c",
                        "import gi; gi.require_version(%r, '0.1')" % lib],
                       capture_output=True).returncode == 0
        for lib in ("AppIndicator3", "AyatanaAppIndicator3"))
    hint = None if _indicator else install_hint(["appindicator"])
    return [("AppIndicator (tray icon)", _indicator, hint)]


def install_icons(assets_dir):
    """Returns (icon for the menu entry, messages)."""
    msgs, icon = [], None
    ICONS_DIR.mkdir(parents=True, exist_ok=True)
    png = assets_dir / "dimmer.png"
    if png.exists():
        shutil.copy2(png, ICON_DEST)
        icon = ICON_DEST
        msgs.append("App icon installed to %s" % ICON_DEST)
    svg = assets_dir / "monitor-dimmer-symbolic.svg"
    if svg.exists():
        SVG_DEST.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(svg, SVG_DEST)
        msgs.append("Symbolic tray icon installed to %s" % SVG_DEST)
    return icon, msgs


LAUNCHER = '#!/bin/sh\nexec "%s" "%s" "$@"\n'


def write_launchers(programs):
    """Shell launchers that run each program with the private virtualenv."""
    BIN_DIR.mkdir(parents=True, exist_ok=True)
    written = []
    for name in programs:
        (APP_HOME / name).chmod(0o755)
        launcher = BIN_DIR / name
        launcher.unlink(missing_ok=True)
        launcher.write_text(LAUNCHER % (VENV_PY, APP_HOME / name), encoding="utf-8")
        launcher.chmod(0o755)
        written.append(launcher)
    return written


def path_warning():
    if str(BIN_DIR) not in os.environ.get("PATH", "").split(":"):
        return "%s is not in your PATH — run the wizard as %s" % (BIN_DIR, BIN_DIR / "dimmer")
    return None


SERVICE_UNIT = """[Unit]
Description=Monitor Dimmer — auto-dim secondary screen during games/media
After=graphical-session.target
PartOf=graphical-session.target

[Service]
Type=simple
ExecStart=%(bin)s/monitor-dimmer
# Put the brightness back if the daemon died while a monitor was dimmed
ExecStopPost=%(bin)s/monitor-dimmer --restore
Restart=on-failure
RestartSec=5
%(env)s
[Install]
WantedBy=graphical-session.target
"""


def install_startup(autostart):
    SERVICE_DIR.mkdir(parents=True, exist_ok=True)
    env = "Environment=PYSTRAY_BACKEND=appindicator\n" if _indicator else ""
    SERVICE.write_text(SERVICE_UNIT % {"bin": BIN_DIR, "env": env}, encoding="utf-8")
    subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
    action = "enable" if autostart else "disable"
    subprocess.run(["systemctl", "--user", action, "monitor-dimmer.service"],
                   capture_output=True)
    if autostart:
        return "Service enabled (starts automatically at login): %s" % SERVICE
    return "Autostart disabled — start manually with: systemctl --user start monitor-dimmer"


DESKTOP_ENTRY = """[Desktop Entry]
Version=1.0
Type=Application
Name=Monitor Dimmer
Comment=Configure auto-dimming of secondary screen during games or fullscreen media
Exec=%(bin)s/dimmer
Icon=%(icon)s
Terminal=true
Categories=Settings;HardwareSettings;
Keywords=monitor;brightness;dim;gaming;
"""


def install_menu_entry(icon=None):
    APP_DIR.mkdir(parents=True, exist_ok=True)
    DESKTOP.write_text(DESKTOP_ENTRY % {"bin": BIN_DIR, "icon": icon or "video-display"},
                       encoding="utf-8")
    for tool in ("kbuildsycoca6", "kbuildsycoca5", "update-desktop-database"):
        if shutil.which(tool):
            cmd = [tool] if tool.startswith("kbuild") else [tool, str(APP_DIR)]
            subprocess.run(cmd, capture_output=True)
    return DESKTOP


def favorites_prompt():
    """The question to ask about pinning, or None when it makes no sense here."""
    if "KDE" in os.environ.get("XDG_CURRENT_DESKTOP", ""):
        return "Add to KDE application launcher favorites?"
    return None


def add_to_favorites():
    if not KDE_STATS.exists():
        return None
    content = KDE_STATS.read_text(encoding="utf-8")
    entry = "applications:dimmer.desktop"
    if entry in content:
        return "Already in KDE favorites"
    KDE_STATS.write_text(
        re.sub(r"(ordering=[^\n]+)", lambda m: m.group(0) + "," + entry, content),
        encoding="utf-8")
    return "Added to KDE application launcher favorites"


def start_daemon():
    subprocess.run(["systemctl", "--user", "restart", "monitor-dimmer.service"],
                   capture_output=True)
    time.sleep(2)
    return service_status() == "active"


# ── Where games live ──────────────────────────────────────────────────────────

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
            dirs += [Path(p) for p in
                     re.findall(r'"path"\s+"([^"]+)"', vdf.read_text(errors="ignore"))]
        for d in dirs:
            common = d / "steamapps/common"
            if common.is_dir() and common not in libs:
                libs.append(common)
    return libs


def other_game_paths():
    found = []
    for cfg in HEROIC_CONFIGS:
        try:
            data = json.loads(cfg.read_text(encoding="utf-8"))
            p = data.get("defaultSettings", {}).get("defaultInstallPath")
            if p:
                found.append(("Heroic (GOG/Epic)", str(Path(p).expanduser())))
        except (OSError, ValueError):
            pass
    found.append(("Heroic (GOG/Epic)", str(HOME / "Games/Heroic")))
    return found


# ── Uninstall ─────────────────────────────────────────────────────────────────

def uninstall_command():
    """How to run the uninstaller. The virtualenv lives inside the folder
    being deleted, but on Linux a running program does not stop its file
    from being removed."""
    return [sys.executable, str(APP_HOME / "uninstall.py")]


def stop_for_uninstall():
    # Stopping runs ExecStopPost, which restores a dimmed monitor's brightness
    for action in ("stop", "disable"):
        subprocess.run(["systemctl", "--user", action, "monitor-dimmer.service"],
                       capture_output=True)


def uninstall_items():
    """(files, dirs) to remove, in that order."""
    files = [BIN_DIR / name for name in
             ("monitor-dimmer", "monitor-dimmer-overlay", "dimmer")]
    files += [SERVICE, DESKTOP, ICON_DEST, SVG_DEST, CONFIG_FILE]
    return files, [APP_HOME, STATE_DIR]


def uninstall_cleanup():
    """Whatever is left once the files are gone."""
    notes = []
    subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
    if KDE_STATS.exists():
        content = KDE_STATS.read_text(encoding="utf-8")
        if "dimmer.desktop" in content:
            content = content.replace(",applications:dimmer.desktop", "")
            content = content.replace("applications:dimmer.desktop,", "")
            KDE_STATS.write_text(content, encoding="utf-8")
            notes.append("Removed from KDE favorites")
    return notes
