"""Windows backend: the same interface as backend_linux.py, on Win32 APIs.

DDC/CI through the Monitor Configuration API (dxva2), monitor identity from
the EDID in the registry, games from Steam's RunningAppID and the process
list, fullscreen windows by comparing window and monitor rectangles. No
service manager is used: the daemon runs as a normal background process,
started at login from the Run registry key.

Win32 calls are made lazily, so this module can be imported (and its pure
helpers tested) on other systems.
"""
import ctypes
import functools
import os
import shutil
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path

NAME = "windows"

# ── Paths ─────────────────────────────────────────────────────────────────────

CONFIG_FILE = Path(os.environ.get("APPDATA", Path.home() / "AppData/Roaming")) / "monitor-dimmer" / "monitor-dimmer.conf"
STATE_DIR   = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / "monitor-dimmer"
PID_FILE    = STATE_DIR / "daemon.pid"
STOP_FILE   = STATE_DIR / "stop"
HERE        = Path(__file__).resolve().parent

RUN_KEY   = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "MonitorDimmer"


# ── Structures ────────────────────────────────────────────────────────────────

class MONITORINFOEXW(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD),
                ("szDevice", wintypes.WCHAR * 32)]


class DISPLAY_DEVICEW(ctypes.Structure):
    _fields_ = [("cb", wintypes.DWORD), ("DeviceName", wintypes.WCHAR * 32),
                ("DeviceString", wintypes.WCHAR * 128), ("StateFlags", wintypes.DWORD),
                ("DeviceID", wintypes.WCHAR * 128), ("DeviceKey", wintypes.WCHAR * 128)]


class PHYSICAL_MONITOR(ctypes.Structure):
    _fields_ = [("hPhysicalMonitor", wintypes.HANDLE),
                ("szPhysicalMonitorDescription", wintypes.WCHAR * 128)]


_FUNCTYPE = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)   # CFUNCTYPE: importable elsewhere
MONITORENUMPROC = _FUNCTYPE(wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC,
                            ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)
WNDENUMPROC = _FUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

EDD_GET_DEVICE_INTERFACE_NAME = 0x1
MONITOR_DEFAULTTONEAREST = 2
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
STILL_ACTIVE = 259


# ── DLLs, with argument types declared: without them 64-bit handles get
#    truncated to a C int ───────────────────────────────────────────────────────

def _declare(dll, name, restype, *argtypes):
    f = getattr(dll, name)
    f.restype, f.argtypes = restype, list(argtypes)


@functools.lru_cache(None)
def _user32():
    d = ctypes.WinDLL("user32", use_last_error=True)
    W = wintypes
    _declare(d, "EnumDisplayMonitors", W.BOOL, W.HDC, ctypes.c_void_p, MONITORENUMPROC, W.LPARAM)
    _declare(d, "GetMonitorInfoW", W.BOOL, W.HMONITOR, ctypes.POINTER(MONITORINFOEXW))
    _declare(d, "EnumDisplayDevicesW", W.BOOL, W.LPCWSTR, W.DWORD, ctypes.POINTER(DISPLAY_DEVICEW), W.DWORD)
    _declare(d, "MonitorFromWindow", W.HMONITOR, W.HWND, W.DWORD)
    _declare(d, "EnumWindows", W.BOOL, WNDENUMPROC, W.LPARAM)
    _declare(d, "IsWindowVisible", W.BOOL, W.HWND)
    _declare(d, "IsIconic", W.BOOL, W.HWND)
    _declare(d, "GetClassNameW", ctypes.c_int, W.HWND, W.LPWSTR, ctypes.c_int)
    _declare(d, "GetWindowTextW", ctypes.c_int, W.HWND, W.LPWSTR, ctypes.c_int)
    _declare(d, "GetWindowRect", W.BOOL, W.HWND, ctypes.POINTER(W.RECT))
    _declare(d, "GetWindowThreadProcessId", W.DWORD, W.HWND, ctypes.POINTER(W.DWORD))
    return d


@functools.lru_cache(None)
def _kernel32():
    d = ctypes.WinDLL("kernel32", use_last_error=True)
    W = wintypes
    _declare(d, "OpenProcess", W.HANDLE, W.DWORD, W.BOOL, W.DWORD)
    _declare(d, "QueryFullProcessImageNameW", W.BOOL, W.HANDLE, W.DWORD, W.LPWSTR, ctypes.POINTER(W.DWORD))
    _declare(d, "GetExitCodeProcess", W.BOOL, W.HANDLE, ctypes.POINTER(W.DWORD))
    _declare(d, "CloseHandle", W.BOOL, W.HANDLE)
    return d


@functools.lru_cache(None)
def _psapi():
    d = ctypes.WinDLL("psapi", use_last_error=True)
    W = wintypes
    _declare(d, "EnumProcesses", W.BOOL, ctypes.POINTER(W.DWORD), W.DWORD, ctypes.POINTER(W.DWORD))
    return d


@functools.lru_cache(None)
def _dxva2():
    d = ctypes.WinDLL("dxva2", use_last_error=True)
    W = wintypes
    _declare(d, "GetNumberOfPhysicalMonitorsFromHMONITOR", W.BOOL, W.HMONITOR, ctypes.POINTER(W.DWORD))
    _declare(d, "GetPhysicalMonitorsFromHMONITOR", W.BOOL, W.HMONITOR, W.DWORD, ctypes.POINTER(PHYSICAL_MONITOR))
    _declare(d, "DestroyPhysicalMonitor", W.BOOL, W.HANDLE)
    _declare(d, "GetVCPFeatureAndVCPFeatureReply", W.BOOL, W.HANDLE, ctypes.c_ubyte,
             ctypes.c_void_p, ctypes.POINTER(W.DWORD), ctypes.POINTER(W.DWORD))
    _declare(d, "SetVCPFeature", W.BOOL, W.HANDLE, ctypes.c_ubyte, W.DWORD)
    return d


# ── EDID ──────────────────────────────────────────────────────────────────────

def parse_edid(edid):
    """{mfg, model, sn} from raw EDID bytes (empty strings when absent)."""
    info = {"mfg": "", "model": "", "sn": ""}
    if len(edid) < 128 or edid[:8] != b"\x00\xff\xff\xff\xff\xff\xff\x00":
        return info
    word = (edid[8] << 8) | edid[9]
    info["mfg"] = "".join(chr(((word >> s) & 0x1F) + 64) for s in (10, 5, 0))
    for off in (54, 72, 90, 108):
        block = edid[off:off + 18]
        if block[:3] == b"\x00\x00\x00" and block[3] in (0xFC, 0xFF):
            text = block[5:18].split(b"\x0a")[0].decode("ascii", "replace").strip()
            info["model" if block[3] == 0xFC else "sn"] = text
    return info


def _edid_for_device(device_id):
    """EDID from the registry for an interface name like
    \\\\?\\DISPLAY#IVM6630#5&1f2c3&0&UID4352#{e6f07b5f-...}."""
    import winreg
    parts = device_id.split("#")
    if len(parts) < 3:
        return b""
    key = rf"SYSTEM\CurrentControlSet\Enum\DISPLAY\{parts[1]}\{parts[2]}\Device Parameters"
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key) as k:
            return bytes(winreg.QueryValueEx(k, "EDID")[0])
    except OSError:
        return b""


def _monitors():
    """[(hmonitor, device name like \\\\.\\DISPLAY1, rect)] for every monitor."""
    user32 = _user32()
    found = []

    def cb(hmon, hdc, rect, data):
        info = MONITORINFOEXW()
        info.cbSize = ctypes.sizeof(info)
        if user32.GetMonitorInfoW(hmon, ctypes.byref(info)):
            r = info.rcMonitor
            found.append((hmon, info.szDevice, (r.left, r.top, r.right, r.bottom)))
        return True

    user32.EnumDisplayMonitors(None, None, MONITORENUMPROC(cb), 0)
    return found


def _identity(device_name):
    dd = DISPLAY_DEVICEW()
    dd.cb = ctypes.sizeof(dd)
    if not _user32().EnumDisplayDevicesW(device_name, 0, ctypes.byref(dd),
                                         EDD_GET_DEVICE_INTERFACE_NAME):
        return {"mfg": "", "model": "", "sn": ""}
    return parse_edid(_edid_for_device(dd.DeviceID))


# ── DDC/CI ────────────────────────────────────────────────────────────────────

_handles = {}          # bus number (our index) -> physical monitor handle


def ddc_displays():
    """Monitors that answer DDC/CI, as [{bus, connector, mfg, model, sn}].
    `bus` is an index into handles that stay valid until the next call."""
    dxva2 = _dxva2()
    for h in _handles.values():
        dxva2.DestroyPhysicalMonitor(h)
    _handles.clear()
    result = []
    for hmon, device, _rect in _monitors():
        n = wintypes.DWORD()
        if not dxva2.GetNumberOfPhysicalMonitorsFromHMONITOR(hmon, ctypes.byref(n)) or not n.value:
            continue
        arr = (PHYSICAL_MONITOR * n.value)()
        if not dxva2.GetPhysicalMonitorsFromHMONITOR(hmon, n.value, arr):
            continue
        handle = arr[0].hPhysicalMonitor
        for extra in arr[1:]:
            dxva2.DestroyPhysicalMonitor(extra.hPhysicalMonitor)
        bus = len(_handles)
        _handles[bus] = handle
        if _read_vcp(handle) is None:                 # no DDC/CI on this one
            continue
        result.append(dict(_identity(device), bus=bus, connector=device))
    return result


def ddc_label(bus):
    return f"DDC #{bus}"


def _read_vcp(handle):
    cur, mx = wintypes.DWORD(), wintypes.DWORD()
    if _dxva2().GetVCPFeatureAndVCPFeatureReply(handle, 0x10, None,
                                                ctypes.byref(cur), ctypes.byref(mx)):
        return cur.value, mx.value
    return None


def _handle(bus):
    if bus not in _handles:
        ddc_displays()
    return _handles.get(bus)


def get_brightness(bus):
    """Return (current, max) brightness, or None."""
    h = _handle(bus)
    return _read_vcp(h) if h else None


def set_brightness(bus, value):
    h = _handle(bus)
    return bool(h) and bool(_dxva2().SetVCPFeature(h, 0x10, int(value)))


# ── Processes ─────────────────────────────────────────────────────────────────

def _exe_of(pid):
    k32 = _kernel32()
    h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(32768)
        size = wintypes.DWORD(len(buf))
        if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return buf.value
        return ""
    finally:
        k32.CloseHandle(h)


def _pids():
    arr = (wintypes.DWORD * 8192)()
    needed = wintypes.DWORD()
    if not _psapi().EnumProcesses(arr, ctypes.sizeof(arr), ctypes.byref(needed)):
        return []
    return [p for p in arr[:needed.value // ctypes.sizeof(wintypes.DWORD)] if p]


def _alive(pid):
    k32 = _kernel32()
    h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return False
    try:
        code = wintypes.DWORD()
        return bool(k32.GetExitCodeProcess(h, ctypes.byref(code))) and code.value == STILL_ACTIVE
    finally:
        k32.CloseHandle(h)


# ── Games ─────────────────────────────────────────────────────────────────────

def game_dirs(paths):
    return sorted({os.path.normcase(os.path.realpath(p)).rstrip("\\/") + "\\" for p in paths})


def _steam_game_running():
    """Steam stores the id of the running game in the registry."""
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as k:
            return int(winreg.QueryValueEx(k, "RunningAppID")[0]) > 0
    except (OSError, ValueError):
        return False


def game_running(dirs):
    """True if a Steam game or a process from one of `dirs` is running."""
    if _steam_game_running():
        return True
    if not dirs:
        return False
    for pid in _pids():
        exe = os.path.normcase(_exe_of(pid))
        if exe and any(exe.startswith(d) for d in dirs):
            return True
    return False


def vlc_fullscreen(windows=None):
    """No MPRIS on Windows: a fullscreen window owned by vlc.exe."""
    found = windows() if callable(windows) else windows
    return any(os.path.basename(_exe_of(w["pid"])).lower() == "vlc.exe" for w in found or [])


# ── Fullscreen windows ────────────────────────────────────────────────────────

_SHELL_CLASSES = {"Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"}


class FullscreenWatcher:
    """Polls the window list (no push mechanism needed on Windows)."""

    def __init__(self, script_path=None):
        pass

    def start(self):
        return False

    def check(self):
        pass

    def stop(self):
        pass

    def windows(self):
        """Visible windows that cover their whole monitor, as [{caption, pid, cls}]."""
        user32 = _user32()
        found = []

        def cb(hwnd, data):
            if not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
                return True
            cls = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, cls, 256)
            if cls.value in _SHELL_CLASSES:
                return True
            r = wintypes.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(r))
            info = MONITORINFOEXW()
            info.cbSize = ctypes.sizeof(info)
            hmon = user32.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST)
            if not user32.GetMonitorInfoW(hmon, ctypes.byref(info)):
                return True
            m = info.rcMonitor
            if r.left <= m.left and r.top <= m.top and r.right >= m.right and r.bottom >= m.bottom:
                title = ctypes.create_unicode_buffer(512)
                user32.GetWindowTextW(hwnd, title, 512)
                pid = wintypes.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                found.append({"caption": title.value, "pid": pid.value, "cls": cls.value})
            return True

        user32.EnumWindows(WNDENUMPROC(cb), 0)
        return found


# ── Background process instead of a service ──────────────────────────────────

def _pythonw():
    w = Path(sys.executable).with_name("pythonw.exe")
    return str(w if w.exists() else sys.executable)


def _daemon_cmd():
    return [_pythonw(), str(HERE / "monitor-dimmer")]


def register_daemon():
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STOP_FILE.unlink(missing_ok=True)
    PID_FILE.write_text(str(os.getpid()))


def unregister_daemon():
    PID_FILE.unlink(missing_ok=True)


def _daemon_pid():
    try:
        pid = int(PID_FILE.read_text())
    except (OSError, ValueError):
        return None
    return pid if _alive(pid) else None


def service_status():
    return "active" if _daemon_pid() else "inactive"


def service_action(action):
    """start / stop / restart. Returns an error message, or None."""
    if action in ("stop", "restart"):
        pid = _daemon_pid()
        if pid:
            # ask nicely, so the daemon restores brightness; then insist
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            STOP_FILE.write_text("")
            for _ in range(40):
                if not _alive(pid):
                    break
                time.sleep(0.25)
            else:
                subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True)
    if action in ("start", "restart") and not _daemon_pid():
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        subprocess.Popen(_daemon_cmd(), creationflags=flags, close_fds=True)
    return None


def stop_requested():
    if STOP_FILE.exists():
        STOP_FILE.unlink(missing_ok=True)
        return True
    return False


def autostart_enabled():
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, RUN_VALUE)
            return True
    except OSError:
        return False


def set_autostart(enabled):
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
            if enabled:
                cmd = " ".join(f'"{c}"' for c in _daemon_cmd())
                winreg.SetValueEx(k, RUN_VALUE, 0, winreg.REG_SZ, cmd)
            else:
                try:
                    winreg.DeleteValue(k, RUN_VALUE)
                except FileNotFoundError:
                    pass
    except OSError as e:
        return str(e)
    return None


# ── Terminal ──────────────────────────────────────────────────────────────────

def open_in_terminal(cmd):
    """Run `cmd` in a new console window (Windows Terminal when installed)."""
    if shutil.which("wt"):
        subprocess.Popen(["wt", *cmd])
    else:
        subprocess.Popen(cmd, creationflags=subprocess.CREATE_NEW_CONSOLE)
    return True
