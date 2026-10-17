"""Which windows are fullscreen right now, and what their titles are.

On KDE Plasma a small KWin script pushes the list of fullscreen windows to
the daemon over D-Bus whenever it changes; it sees native Wayland and X11
windows alike. Elsewhere X11/XWayland windows are inspected with xdotool +
xprop on demand (native Wayland windows are invisible there).
"""
import json
import re
import subprocess
import threading

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
