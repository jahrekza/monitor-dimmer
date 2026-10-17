"""Linux backend: everything Monitor Dimmer needs from the operating system.

The daemon, settings and installer talk to the system only through the
functions in this module; backend_windows.py offers the same interface.
"""
import json
import os
import re
import shutil
import subprocess
import threading
from pathlib import Path

NAME = "linux"

# ── Paths ─────────────────────────────────────────────────────────────────────

CONFIG_FILE = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "monitor-dimmer.conf"
STATE_DIR   = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "monitor-dimmer"


# ── DDC/CI (ddcutil) ──────────────────────────────────────────────────────────

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
