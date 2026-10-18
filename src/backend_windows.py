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
import tempfile
import sys
import time
import pathlib
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

# Built into executables there is no interpreter and no .py file: each
# program is an .exe sitting beside the others.
FROZEN = bool(getattr(sys, "frozen", False))
EXE    = ".exe" if FROZEN else ""


APP_EXE = "MonitorDimmer.exe"


def _program_dir():
    """Folder our own programs live in right now — the installer's payload
    while the downloadable installer is running, otherwise next to us."""
    if not FROZEN:
        return HERE
    payload = Path(getattr(sys, "_MEIPASS", sys.executable)) / "app"
    return payload if payload.is_dir() else Path(sys.executable).resolve().parent


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


def _set_dpi_aware():
    """Ask for Per-Monitor-V2 DPI awareness before the first window call.

    Without it Windows virtualises coordinates for this process: a monitor
    scaled to 150% reports 2560x1440 from GetMonitorInfo while GetWindowRect
    on a DPI-aware window (any modern game, and the Qt overlay) returns the
    real 3840x2160. Comparing the two then decides "fullscreen" on mismatched
    units. Declaring awareness puts everything in physical pixels.
    """
    user32 = ctypes.WinDLL("user32")
    try:                                        # Windows 10 1703+
        if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return
    except AttributeError:
        pass
    try:                                        # Windows 8.1: per-monitor v1
        ctypes.WinDLL("shcore").SetProcessDpiAwareness(2)
    except (AttributeError, OSError):
        user32.SetProcessDPIAware()             # last resort: system DPI


@functools.lru_cache(None)
def _user32():
    _set_dpi_aware()
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
    _declare(d, "CreateJobObjectW", W.HANDLE, ctypes.c_void_p, W.LPCWSTR)
    _declare(d, "SetInformationJobObject", W.BOOL, W.HANDLE, ctypes.c_int, ctypes.c_void_p, W.DWORD)
    _declare(d, "AssignProcessToJobObject", W.BOOL, W.HANDLE, W.HANDLE)
    _declare(d, "QueryInformationJobObject", W.BOOL, W.HANDLE, ctypes.c_int, ctypes.c_void_p,
             W.DWORD, ctypes.POINTER(W.DWORD))
    _declare(d, "TerminateJobObject", W.BOOL, W.HANDLE, W.UINT)
    _declare(d, "CreateToolhelp32Snapshot", W.HANDLE, W.DWORD, W.DWORD)
    _declare(d, "Thread32First", W.BOOL, W.HANDLE, ctypes.c_void_p)
    _declare(d, "Thread32Next", W.BOOL, W.HANDLE, ctypes.c_void_p)
    _declare(d, "OpenThread", W.HANDLE, W.DWORD, W.BOOL, W.DWORD)
    _declare(d, "ResumeThread", W.DWORD, W.HANDLE)
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
        # Handles are small integers and 0 is a valid one; ctypes hands back
        # None for it, which would later read as "no such monitor".
        handle = arr[0].hPhysicalMonitor or 0
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
    """Handle for `bus`, or None when no such monitor. Never confuse the two:
    handle 0 is a real monitor."""
    if bus not in _handles:
        ddc_displays()
    return _handles.get(bus)


def get_brightness(bus):
    """Return (current, max) brightness, or None."""
    h = _handle(bus)
    return _read_vcp(h) if h is not None else None


def set_brightness(bus, value):
    h = _handle(bus)
    return h is not None and bool(_dxva2().SetVCPFeature(h, 0x10, int(value)))


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
    """How to start the daemon from where we are now."""
    if FROZEN:
        return [str(_program_dir() / APP_EXE), "--role", "daemon"]
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


# ── Overlay children ──────────────────────────────────────────────────────────
#
# Each overlay runs inside its own job object. Two Windows facts make that
# worth the trouble:
#
#   * a virtualenv's python.exe is a stub that re-executes the real
#     interpreter, so the pid Popen reports is not the pid that owns the
#     overlay window. The daemon needs the real one to recognise its own
#     windows and not mistake them for a fullscreen game;
#   * nothing cleans up children when the daemon is killed, which would leave
#     a black window covering a screen with no obvious way to close it.
#
# A job object answers both: it lists every pid inside it, and it kills them
# when its last handle closes, which happens when the daemon dies.

JobObjectBasicProcessIdList        = 3
JobObjectExtendedLimitInformation  = 9
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
TH32CS_SNAPTHREAD                  = 0x0004
THREAD_SUSPEND_RESUME              = 0x0002
MAX_JOB_PIDS                       = 64
CREATE_SUSPENDED                   = 0x0004   # subprocess does not define it


class IO_COUNTERS(ctypes.Structure):
    _fields_ = [(n, ctypes.c_ulonglong) for n in
                ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                 "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]


class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", wintypes.LARGE_INTEGER),
                ("PerJobUserTimeLimit", wintypes.LARGE_INTEGER),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD)]


class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
                ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t)]


class JOBOBJECT_BASIC_PROCESS_ID_LIST(ctypes.Structure):
    _fields_ = [("NumberOfAssignedProcesses", wintypes.DWORD),
                ("NumberOfProcessIdsInList", wintypes.DWORD),
                ("ProcessIdList", ctypes.c_size_t * MAX_JOB_PIDS)]


class THREADENTRY32(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                ("th32ThreadID", wintypes.DWORD), ("th32OwnerProcessID", wintypes.DWORD),
                ("tpBasePri", ctypes.c_long), ("tpDeltaPri", ctypes.c_long),
                ("dwFlags", wintypes.DWORD)]


def _kill_on_close_job():
    """A job that kills whatever is inside it once we let go of it."""
    k32 = _kernel32()
    job = k32.CreateJobObjectW(None, None)
    if not job:
        return None
    info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not k32.SetInformationJobObject(job, JobObjectExtendedLimitInformation,
                                       ctypes.byref(info), ctypes.sizeof(info)):
        k32.CloseHandle(job)
        return None
    return job


def _resume_process(pid):
    """Resume a process started suspended (we never get its thread handle)."""
    k32 = _kernel32()
    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0)
    if snap == wintypes.HANDLE(-1).value or not snap:
        return False
    try:
        entry = THREADENTRY32()
        entry.dwSize = ctypes.sizeof(entry)
        ok = k32.Thread32First(snap, ctypes.byref(entry))
        resumed = False
        while ok:
            if entry.th32OwnerProcessID == pid:
                th = k32.OpenThread(THREAD_SUSPEND_RESUME, False, entry.th32ThreadID)
                if th:
                    k32.ResumeThread(th)
                    k32.CloseHandle(th)
                    resumed = True
            ok = k32.Thread32Next(snap, ctypes.byref(entry))
        return resumed
    finally:
        k32.CloseHandle(snap)


class OverlayProcess:
    """One overlay window, held in a job object (see the note above)."""

    def __init__(self, cmd):
        k32 = _kernel32()
        self._job = _kill_on_close_job()
        # Start suspended so the stub cannot spawn the real interpreter before
        # the job is in place; without that the grandchild could escape it.
        flags = CREATE_SUSPENDED if self._job else 0
        self._p = subprocess.Popen(cmd, creationflags=flags)
        if self._job:
            if not k32.AssignProcessToJobObject(self._job, int(self._p._handle)):
                k32.CloseHandle(self._job)
                self._job = None
            if not _resume_process(self._p.pid):
                # Nothing would ever run: fall back to a plain child process.
                self._p.kill()
                if self._job:
                    k32.CloseHandle(self._job)
                self._job = None
                self._p = subprocess.Popen(cmd)

    @property
    def pids(self):
        """Every pid this overlay occupies — the stub and the real window."""
        if not self._job:
            return {self._p.pid}
        info = JOBOBJECT_BASIC_PROCESS_ID_LIST()
        got = wintypes.DWORD()
        if not _kernel32().QueryInformationJobObject(
                self._job, JobObjectBasicProcessIdList,
                ctypes.byref(info), ctypes.sizeof(info), ctypes.byref(got)):
            return {self._p.pid}
        n = min(info.NumberOfProcessIdsInList, MAX_JOB_PIDS)
        return {info.ProcessIdList[i] for i in range(n)} | {self._p.pid}

    def poll(self):
        return self._p.poll()

    def stop(self, timeout=2):
        """Terminate the whole job, so no part of the overlay survives."""
        k32 = _kernel32()
        if self._job:
            k32.TerminateJobObject(self._job, 0)
        elif self._p.poll() is None:
            self._p.terminate()
        try:
            self._p.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self._p.kill()
        if self._job:
            k32.CloseHandle(self._job)
            self._job = None


def spawn_overlay(cmd):
    return OverlayProcess(cmd)


# ── Console ───────────────────────────────────────────────────────────────────

ATTACH_PARENT_PROCESS = -1


def _ensure_console():
    """Give a windowed build a console to talk in.

    The application is built without one so the daemon and the overlay never
    flash a black window. The settings wizard, the installer and the
    uninstaller do need somewhere to print and read: borrow the console of
    whoever started us, or open one of our own.
    """
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    if not k32.GetConsoleWindow():
        if not k32.AttachConsole(ATTACH_PARENT_PROCESS) and not k32.AllocConsole():
            return
    if sys.stdin and sys.stdout and sys.stderr:
        return                       # already wired up, leave the pipes alone
    try:
        sys.stdin  = open("CONIN$", "r", encoding="utf-8", errors="replace")
        sys.stdout = open("CONOUT$", "w", encoding="utf-8", errors="replace",
                          buffering=1)
        sys.stderr = open("CONOUT$", "w", encoding="utf-8", errors="replace",
                          buffering=1)
    except OSError:
        pass


def prepare_console():
    """Make this console able to show the settings wizard.

    The wizard draws its frame with box-drawing characters and colours it with
    ANSI escapes. A Windows console (or a redirected stream) defaults to the
    legacy code page, where those characters do not exist and printing them
    raises UnicodeEncodeError before the first screen is drawn. Switch the
    streams to UTF-8 and ask the console to interpret escapes.
    """
    if FROZEN:
        _ensure_console()
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError, ValueError):
            pass
    ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
    try:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        for handle_id in (-11, -12):                  # stdout, stderr
            h = k32.GetStdHandle(handle_id)
            mode = wintypes.DWORD()
            if h and k32.GetConsoleMode(h, ctypes.byref(mode)):
                k32.SetConsoleMode(h, mode.value | ENABLE_VIRTUAL_TERMINAL_PROCESSING)
    except (AttributeError, OSError):
        pass


# ── Terminal ──────────────────────────────────────────────────────────────────

def open_in_terminal(cmd):
    """Run `cmd` in a new console window (Windows Terminal when installed)."""
    if shutil.which("wt"):
        subprocess.Popen(["wt", *cmd])
    else:
        subprocess.Popen(cmd, creationflags=subprocess.CREATE_NEW_CONSOLE)
    return True


# ── Installation ──────────────────────────────────────────────────────────────
#
# Windows has no systemd and no ~/.local/bin, so an install is: a folder with
# the programs and a private virtualenv, .cmd launchers beside them, a Start
# menu shortcut, and the Run registry key for starting at login.

APP_HOME  = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / "Programs" / "monitor-dimmer"
VENV_DIR  = APP_HOME / "venv"
VENV_PY   = VENV_DIR / "Scripts" / "python.exe"
VENV_PYW  = VENV_DIR / "Scripts" / "pythonw.exe"
BIN_DIR   = APP_HOME / "bin"
ICON_DEST = APP_HOME / "monitor-dimmer.ico"
START_MENU = (Path(os.environ.get("APPDATA", Path.home() / "AppData/Roaming"))
              / "Microsoft/Windows/Start Menu/Programs/Monitor Dimmer.lnk")

# A system virtualenv would hand us the system's packages; on Windows there is
# no distro PyQt6 to borrow, so the environment is self-contained.
VENV_OPTIONS = []

# Wording the installer shows, where it depends on the system
GAME_PATHS_NOTE = "listed here counts as a game."
FULLSCREEN_NOTE = ("Only when fullscreen: the dimmer sees every window on "
                   "Windows, so this is safe to turn on")
# (package, import name, offer a system package first). Windows has no
# distro packages to borrow, so everything comes from pip.
PIP_PACKAGES = [("pystray", "pystray", False), ("Pillow", "PIL", False),
                ("PyQt6", "PyQt6", False)]


def check_tools():
    """[(name, purpose, found)] for optional tools, plus a fatal error or None.

    Everything the Windows backend needs is in the OS itself: DDC/CI through
    dxva2, windows through user32, no helper programs at all.
    """
    return [], None


def install_hint(keys):
    return None


def _daemon_argv():
    """How to start the *installed* daemon, windowless."""
    if FROZEN:
        return [str(APP_HOME / APP_EXE), "--role", "daemon"]
    py = VENV_PYW if VENV_PYW.exists() else VENV_PY
    return [str(py), str(APP_HOME / "monitor-dimmer")]


def write_launchers(programs):
    """A .cmd per program, so they can be run from a terminal if wanted.

    A frozen build needs none: the programs are already executables.
    """
    if FROZEN:
        return []
    BIN_DIR.mkdir(parents=True, exist_ok=True)
    written = []
    for name in programs:
        launcher = BIN_DIR / f"{name}.cmd"
        # %* forwards the arguments; @echo off keeps the console clean.
        # Text mode already turns a newline into CRLF: writing CRLF here
        # would put a stray carriage return on every line.
        launcher.write_text(
            "@echo off\n"
            f'"{VENV_PY}" "{APP_HOME / name}" %*\n',
            encoding="utf-8")
        written.append(launcher)
    return written


def install_icons(assets_dir):
    """Returns (icon for the shortcut, messages).

    Shortcuts want an .ico and the repo ships a .png, so convert it. Pillow
    lands in the virtualenv during setup, so this runs after it.
    """
    if FROZEN:
        # The build already placed the .ico beside the programs
        shipped = _program_dir() / ICON_DEST.name
        if shipped.exists() and shipped != ICON_DEST:
            shutil.copy2(shipped, ICON_DEST)
        return (ICON_DEST, ["App icon installed to %s" % ICON_DEST]) if ICON_DEST.exists() \
            else (None, [])
    png = assets_dir / "dimmer.png"
    if not png.exists():
        return None, []
    for python in (VENV_PY, Path(sys.executable)):
        if not Path(python).exists():
            continue
        code = ("from PIL import Image; "
                "Image.open(%r).convert('RGBA').save(%r, "
                "sizes=[(16,16),(32,32),(48,48),(64,64),(256,256)])"
                % (str(png), str(ICON_DEST)))
        r = subprocess.run([str(python), "-c", code], capture_output=True)
        if r.returncode == 0 and ICON_DEST.exists():
            return ICON_DEST, ["App icon installed to %s" % ICON_DEST]
    return None, ["Could not build the shortcut icon — using the default one"]


def post_venv_check(venv_py):
    """[(label, ok, hint)] for what pip cannot provide. Windows needs nothing
    beyond the packages themselves: the tray host is part of the shell."""
    return []


def path_warning():
    target = (APP_HOME / APP_EXE) if FROZEN else (BIN_DIR / "dimmer.cmd")
    return ("%s is not on your PATH — run the wizard from the Start menu, "
            "or as %s" % (target.parent, target))


def favorites_prompt():
    """Nothing to pin to: the Start menu shortcut is the whole story here."""
    return None


def add_to_favorites():
    return None


def uninstall_command():
    """How to run the uninstaller so that it can delete the install folder.

    Windows refuses to delete a running executable. Both the virtualenv's
    python.exe and, in a frozen build, the uninstaller itself live inside
    the folder being removed — so a frozen uninstaller is copied to the
    temporary folder first, and otherwise we use the interpreter the
    virtualenv was built from, which is elsewhere.
    """
    if FROZEN:
        return [str(_program_dir() / APP_EXE), "--role", "uninstall"]
    python = getattr(sys, "_base_executable", None) or sys.executable
    return [python, str(_program_dir() / "uninstall.py")]


def stop_for_uninstall():
    if _daemon_pid() == os.getpid():
        # `monitor-dimmer --uninstall` replaced the daemon with the
        # uninstaller, so we are the "daemon": asking it to stop would wait
        # ten seconds and then kill the uninstall halfway through.
        unregister_daemon()
        return
    service_action("stop")


def install_menu_entry(icon=None):
    """Start menu shortcut for the settings wizard, through WScript.Shell."""
    START_MENU.parent.mkdir(parents=True, exist_ok=True)
    target = str(APP_HOME / APP_EXE) if FROZEN else str(VENV_PY)
    arguments = "--role settings" if FROZEN else '"%s"' % (APP_HOME / "dimmer")
    ps = (
        "$s = (New-Object -ComObject WScript.Shell).CreateShortcut({lnk});"
        "$s.TargetPath = {target};"
        "$s.Arguments = {args};"
        "$s.WorkingDirectory = {cwd};"
        "$s.Description = 'Monitor Dimmer settings';"
        + ("$s.IconLocation = {icon};" if icon else "")
        + "$s.Save()"
    ).format(
        lnk=_ps_quote(str(START_MENU)),
        target=_ps_quote(target),
        args=_ps_quote(arguments),
        cwd=_ps_quote(str(APP_HOME)),
        icon=_ps_quote(str(icon)) if icon else "",
    )
    r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                       capture_output=True, text=True)
    return START_MENU if r.returncode == 0 and START_MENU.exists() else None


def _ps_quote(text):
    """A PowerShell single-quoted string."""
    return "'" + text.replace("'", "''") + "'"


def install_startup(autostart):
    """Start at login, pointing at the installed copy (not this installer)."""
    import winreg
    command = " ".join('"%s"' % a for a in _daemon_argv())
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
            if autostart:
                winreg.SetValueEx(k, RUN_VALUE, 0, winreg.REG_SZ, command)
                return f"Starts at login (registry Run key, {RUN_VALUE})"
            try:
                winreg.DeleteValue(k, RUN_VALUE)
            except FileNotFoundError:
                pass
            return "Autostart disabled — start it from the Start menu or the launcher"
    except OSError as e:
        return f"Could not write the Run key: {e}"


def start_daemon():
    """Restart the installed daemon. Returns True once it reports itself."""
    service_action("stop")
    flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    subprocess.Popen(_daemon_argv(), creationflags=flags, close_fds=True)
    for _ in range(20):
        if _daemon_pid():
            return True
        time.sleep(0.5)
    return False


# ── Where games live ──────────────────────────────────────────────────────────

def steam_libraries():
    """Every steamapps/common Steam knows about, found through the registry."""
    import winreg
    roots = []
    for hive, key, value in ((winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath"),
                             (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam",
                              "InstallPath")):
        try:
            with winreg.OpenKey(hive, key) as k:
                roots.append(Path(winreg.QueryValueEx(k, value)[0]))
        except OSError:
            pass
    libs = []
    for root in roots:
        dirs = [root]
        vdf = root / "steamapps/libraryfolders.vdf"
        if vdf.exists():
            import re
            dirs += [Path(p.replace("\\\\", "\\"))
                     for p in re.findall(r'"path"\s+"([^"]+)"', vdf.read_text(errors="ignore"))]
        for d in dirs:
            common = d / "steamapps/common"
            if common.is_dir() and common not in libs:
                libs.append(common)
    return libs


def other_game_paths():
    """[(label, path)] for launchers other than Steam."""
    appdata = Path(os.environ.get("APPDATA", Path.home() / "AppData/Roaming"))
    found = []
    cfg = appdata / "heroic" / "config.json"
    try:
        import json
        data = json.loads(cfg.read_text(encoding="utf-8"))
        p = data.get("defaultSettings", {}).get("defaultInstallPath")
        if p:
            found.append(("Heroic (GOG/Epic)", str(Path(p))))
    except (OSError, ValueError):
        pass
    for label, p in (("Epic Games", Path(os.environ.get("ProgramW6432", r"C:\Program Files"))
                                    / "Epic Games"),
                     ("GOG Galaxy", Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))
                                    / "GOG Galaxy" / "Games"),
                     ("Xbox / Game Pass", Path(os.environ.get("SystemDrive", "C:") + "\\") / "XboxGames")):
        if p.is_dir():
            found.append((label, str(p)))
    return found


# ── Uninstall ─────────────────────────────────────────────────────────────────

def uninstall_items():
    """(files, dirs) to remove, in that order.

    CONFIG_FILE's folder is ours alone here, unlike ~/.config on Linux, so it
    goes too and leaves nothing behind. The program folder is left out of a
    frozen build's list: we are running from inside it, and Windows will not
    delete a running executable. uninstall_cleanup() sees to it afterwards.
    """
    dirs = [STATE_DIR, CONFIG_FILE.parent]
    if not FROZEN:
        dirs.insert(0, APP_HOME)
    return ([START_MENU, CONFIG_FILE], dirs)


def _delete_after_exit(folder):
    """Have the shell remove `folder` once we are no longer running inside it.

    The work goes into a small script. Handing `cmd /c` a compound command
    with a quoted path runs into its own quoting rules and fails with a
    syntax error, so write the steps down instead.
    """
    script = pathlib.Path(tempfile.gettempdir()) / "monitor-dimmer-cleanup.cmd"
    lines = ["@echo off",
             "ping -n 4 127.0.0.1 >nul",
             'rmdir /s /q "%s"' % folder,
             'del "%~f0"']
    script.write_text("\r\n".join(lines) + "\r\n", encoding="utf-8", newline="")
    flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NO_WINDOW
    subprocess.Popen(["cmd", "/c", str(script)], creationflags=flags, close_fds=True)


def uninstall_cleanup():
    """Whatever is left once the files are gone."""
    import winreg
    notes = []
    if FROZEN and APP_HOME.exists():
        _delete_after_exit(APP_HOME)
        notes.append("%s goes as soon as this window closes" % APP_HOME)
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
            winreg.DeleteValue(k, RUN_VALUE)
            notes.append("Removed the start-at-login registry entry")
    except OSError:
        pass
    return notes
