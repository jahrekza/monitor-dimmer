"""Shared, OS-independent code for the Monitor Dimmer daemon, settings and
installer. Everything that talks to the operating system lives in the
backend module picked below (backend_linux.py / backend_windows.py)."""
import json
import re
import subprocess
import sys
from pathlib import Path

if sys.platform == "win32":
    import backend_windows as backend
else:
    import backend_linux as backend

CONFIG_FILE = backend.CONFIG_FILE
STATE_DIR   = backend.STATE_DIR
STATE_FILE  = STATE_DIR / "brightness.json"
HERE        = Path(__file__).resolve().parent
OVERLAY_BIN = HERE / "monitor-dimmer-overlay"

# Frozen into executables, our programs are .exe files sitting next to each
# other with no interpreter and no .py files in sight.
FROZEN  = bool(getattr(sys, "frozen", False))
APP_EXE = "MonitorDimmer.exe" if sys.platform == "win32" else "MonitorDimmer"

# Which program the one executable should act as
ROLES = {"monitor-dimmer":         "daemon",
         "monitor-dimmer-overlay": "overlay",
         "dimmer":                 "settings"}


def _program_dir():
    """Where our programs are right now.

    The downloadable installer is a single file carrying the application
    folder inside it; while it runs, that payload is what we launch.
    """
    if not FROZEN:
        return HERE
    payload = Path(getattr(sys, "_MEIPASS", sys.executable)) / "app"
    return payload if payload.is_dir() else Path(sys.executable).resolve().parent


PROGRAM_DIR = _program_dir()


def program_command(name, *args):
    """The argv that runs one of our own programs."""
    args = [str(a) for a in args]
    if FROZEN:
        return [str(PROGRAM_DIR / APP_EXE), "--role", ROLES[name], *args]
    return [sys.executable, str(HERE / name), *args]

MODES        = ("off", "overlay", "ddc")
SCREEN_KEYS  = ("mode", "level", "connector", "mfg", "model", "sn", "bus")
LEVELS       = (30, 50, 70, 85, 100)          # offered in the tray
IDENTITY     = ("connector", "mfg", "model", "sn", "bus")

# Video sites recognised in a fullscreen browser window's title. Matched as
# whole words, case-insensitive, so "Plex" doesn't hit "complex".
VIDEO_SITES = [
    # global
    "YouTube", "Netflix", "Twitch", "Prime Video", "Disney+", "HBO Max", "Max",
    "Apple TV", "Vimeo", "Dailymotion", "Kick", "Crunchyroll", "SkyShowtime",
    "Paramount+", "Plex", "Jellyfin",
    # Poland
    "Player.pl", "Polsat Box Go", "TVP VOD", "CDA", "CANAL+",
    # Ireland
    "RTÉ Player", "RTE Player", "Virgin Media Player", "TG4",
]

DEFAULTS = {
    "dim_level":          70,
    "poll_interval":      5,
    "auto_dim":           True,
    "detect_games":       True,
    "vlc_fullscreen":     True,
    "video_fullscreen":   True,
    "video_titles":       VIDEO_SITES,
    "require_fullscreen": False,
    "game_paths":         [],
}

INT_KEYS  = {"dim_level": (0, 100), "poll_interval": (1, 3600)}
BOOL_KEYS = {"auto_dim", "detect_games", "vlc_fullscreen", "video_fullscreen",
             "require_fullscreen"}
LIST_KEYS = {"game_paths", "video_titles"}


def warn(msg):
    print(f"monitor-dimmer: {msg}", file=sys.stderr, flush=True)


# ── Config ────────────────────────────────────────────────────────────────────
#
# Screens to dim are numbered entries:  screen1_mode=overlay, screen1_model=...
# mode is off / overlay / ddc. Screens not listed are never touched.

def load_config():
    cfg = {k: list(v) if isinstance(v, list) else v for k, v in DEFAULTS.items()}
    screens, legacy = {}, {}
    try:
        # Always UTF-8: the locale encoding differs between systems (and
        # cannot even represent "z with a dot" on a Western Windows), so a
        # config would not survive moving between machines.
        text = CONFIG_FILE.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        text = ""
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        k, _, v = line.partition("=")
        k, v = k.strip(), v.strip()
        m = re.match(r"^screen(\d+)_(\w+)$", k)
        if m and m.group(2) in SCREEN_KEYS:
            screens.setdefault(int(m.group(1)), {})[m.group(2)] = v
        elif re.match(r"^(right|target)_\w+$", k) or k == "use_overlay":
            legacy[k.split("_", 1)[1] if k != "use_overlay" else k] = v
        elif k in INT_KEYS:
            lo, hi = INT_KEYS[k]
            try:
                cfg[k] = min(max(int(v), lo), hi)
            except ValueError:
                warn(f"ignoring invalid {k}={v!r} in {CONFIG_FILE}")
        elif k in BOOL_KEYS:
            cfg[k] = v.lower() in ("true", "yes", "1", "on")
        elif k in LIST_KEYS:
            cfg[k] = [p.strip() for p in v.split(",") if p.strip()]

    if not screens and any(legacy.get(k) for k in IDENTITY):
        # one-screen configs from older versions (right_* / target_* keys)
        overlay = legacy.get("use_overlay", "true").lower() in ("true", "yes", "1", "on")
        screens[1] = dict(legacy, mode="overlay" if overlay else "ddc")

    if cfg["video_titles"] == ["YouTube"]:         # the 2.0 default: upgrade to the full list
        cfg["video_titles"] = list(VIDEO_SITES)
    cfg["screens"] = [normalize_screen(screens[i]) for i in sorted(screens)]
    return cfg


def title_matches(caption, titles):
    """True if one of `titles` appears in `caption` as a whole word."""
    for t in titles:
        if re.search(r"(?<!\w)" + re.escape(t) + r"(?!\w)", caption, re.IGNORECASE):
            return True
    return False


def normalize_screen(raw):
    s = {k: (raw.get(k) or None) for k in IDENTITY}
    try:
        s["bus"] = int(s["bus"]) if s["bus"] is not None else None
    except ValueError:
        s["bus"] = None
    s["mode"] = raw.get("mode") if raw.get("mode") in MODES else "overlay"
    try:                                          # per-screen dim level, else dim_level
        s["level"] = min(max(int(raw["level"]), 0), 100) if raw.get("level") not in (None, "") else None
    except ValueError:
        s["level"] = None
    return s


def screen_level(cfg, screen):
    return screen["level"] if screen.get("level") is not None else cfg["dim_level"]


def config_mtime():
    try:
        return CONFIG_FILE.stat().st_mtime_ns
    except FileNotFoundError:
        return None


def save_config(cfg):
    lines = ["# Monitor Dimmer configuration"]
    for k in DEFAULTS:
        v = cfg.get(k)
        if isinstance(v, bool):
            v = str(v).lower()
        elif isinstance(v, list):
            v = ",".join(v)
        lines.append(f"{k}={v}")
    # screens set to "off" are the same as unlisted ones: drop them
    for i, s in enumerate([s for s in cfg.get("screens", []) if s["mode"] != "off"], 1):
        for k in SCREEN_KEYS:
            if s.get(k) is not None:
                lines.append(f"screen{i}_{k}={s[k]}")
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")


def save_changes(cfg, keys, screens):
    """Write only what the caller changed on top of the file's current
    content, so changes made meanwhile (e.g. from the tray) survive.
    `keys`: changed option names; `screens`: screens whose mode changed."""
    current = load_config()
    for k in keys:
        current[k] = cfg[k]
    for s in screens:
        hit = find_monitor(s, current["screens"])
        if hit is not None:
            hit["mode"], hit["level"] = s["mode"], s.get("level")
        else:
            current["screens"].append(dict(s))
    save_config(current)
    return current


def screen_key(s):
    """Stable key for a screen while the daemon runs (survives config reloads)."""
    return (s.get("sn"), s.get("model"), s.get("connector"))


def has_identity(screen):
    return any(screen.get(k) for k in ("model", "sn", "connector"))


def screen_label(s):
    name = " ".join(x for x in (s.get("mfg"), s.get("model")) if x) or "Unknown monitor"
    extra = [s["connector"]] if s.get("connector") else []
    if s.get("bus") is not None:
        extra.append(backend.ddc_label(s["bus"]))
    return f"{name} ({', '.join(extra)})" if extra else name


def active_screens(cfg):
    return [s for s in cfg["screens"] if s["mode"] != "off"]


def screens_label(cfg):
    names = [f"{screen_label(s)}: {s['mode']}" for s in active_screens(cfg)]
    return "; ".join(names) if names else "none"


# ── Monitors ──────────────────────────────────────────────────────────────────

ddc_displays   = backend.ddc_displays
get_brightness = backend.get_brightness
set_brightness = backend.set_brightness


def qt_screens(python=None):
    """Screens as the compositor sees them, as [{connector, mfg, model, sn}]."""
    try:
        cmd = (program_command("monitor-dimmer-overlay", "--list") if FROZEN or not python
               else [python, str(OVERLAY_BIN), "--list"])
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=15).stdout
        return json.loads(out)
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return []


def all_monitors(python=None):
    """Every screen, with its DDC bus when it has one.

    The compositor knows every screen (laptop panels and TVs too); only the
    ones that speak DDC/CI answer the backend. Without a graphical session
    fall back to that answer alone.
    """
    ddc = ddc_displays()
    screens = qt_screens(python)
    if not screens:
        return ddc
    for s in screens:
        match = [d for d in ddc if d.get("connector") == s["connector"]]
        if not match:
            match = [d for d in ddc if d.get("model") and d.get("model") == s.get("model")]
        if len(match) == 1:
            d = match[0]
            s["bus"] = d["bus"]
            s["sn"] = s.get("sn") or d.get("sn", "")
            s["mfg"] = d.get("mfg") or s.get("mfg", "")
    return screens


def identity_of(m):
    return {k: (m.get(k) if m.get(k) not in ("", None) else None) for k in IDENTITY}


def find_monitor(screen, monitors):
    """Pick the configured screen out of `monitors`, or None.

    Bus numbers and connector names follow the GPU port, so they change when
    cables are swapped; the serial number and model travel with the monitor.
    Match the serial first, then the model, and use the connector / bus only
    to tell identical monitors apart.
    """
    sn, model = screen.get("sn"), screen.get("model")
    if sn:
        hits = [m for m in monitors if m.get("sn") == sn]
        if len(hits) == 1:
            return hits[0]
    if model:
        hits = [m for m in monitors if m.get("model") == model]
        if len(hits) > 1:
            hits = ([m for m in hits if m.get("connector") == screen.get("connector")]
                    or [m for m in hits if m.get("bus") == screen.get("bus")])
        return hits[0] if len(hits) == 1 else None
    if not sn and screen.get("connector"):
        hits = [m for m in monitors if m.get("connector") == screen["connector"]]
        return hits[0] if len(hits) == 1 else None
    return None


def find_bus(screen, displays=None):
    if not has_identity(screen):
        return screen.get("bus")                 # bus number only
    m = find_monitor(screen, ddc_displays() if displays is None else displays)
    return m["bus"] if m else None


def merge_screens(cfg, monitors):
    """Configured screens plus every connected monitor not configured yet,
    each as (screen_dict, connected). New monitors start with mode off."""
    result, seen = [], []
    for s in cfg["screens"]:
        m = find_monitor(s, monitors)
        if m is not None:
            seen.append(id(m))
            # refresh port data (connector / bus move with the cable)
            s.update({k: v for k, v in identity_of(m).items() if v is not None})
        result.append((s, m is not None))
    for m in monitors:
        if id(m) not in seen:
            result.append((dict(identity_of(m), mode="off"), True))
    return result


def overlay_args(screen):
    args = []
    for flag, key in (("--model", "model"), ("--sn", "sn"), ("--connector", "connector")):
        if screen.get(key):
            args += [flag, screen[key]]
    return args


# ── DDC brightness ────────────────────────────────────────────────────────────

def dimmed_value(current, dim_level):
    return max(0, round(current * (100 - dim_level) / 100))


# Brightness to restore, per dimmed screen: [{"screen": identity, "bus": n,
# "brightness": v}]. Kept on disk until restored, so a crash or `systemctl
# stop` mid-game can still put it back.

def load_state():
    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if isinstance(state, dict):                  # 1.1: single screen
        state = [{"screen": {}, **state}]
    return state if isinstance(state, list) else []


def save_state(state):
    if not state:
        try:
            STATE_FILE.unlink()
        except FileNotFoundError:
            pass
        return
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state), encoding="utf-8")
