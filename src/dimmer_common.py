"""Shared code for the Monitor Dimmer daemon, config wizard and installer."""
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

CONFIG_FILE = Path.home() / ".config/monitor-dimmer.conf"
STATE_DIR   = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "monitor-dimmer"
STATE_FILE  = STATE_DIR / "brightness.json"
HERE        = Path(__file__).resolve().parent
OVERLAY_BIN = HERE / "monitor-dimmer-overlay"

MODES        = ("off", "overlay", "ddc")
SCREEN_KEYS  = ("mode", "connector", "mfg", "model", "sn", "bus")
IDENTITY     = ("connector", "mfg", "model", "sn", "bus")

DEFAULTS = {
    "dim_level":          70,
    "poll_interval":      5,
    "auto_dim":           True,
    "detect_games":       True,
    "vlc_fullscreen":     True,
    "video_fullscreen":   True,
    "video_titles":       ["YouTube"],
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
        text = CONFIG_FILE.read_text()
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

    cfg["screens"] = [normalize_screen(screens[i]) for i in sorted(screens)]
    return cfg


def normalize_screen(raw):
    s = {k: (raw.get(k) or None) for k in IDENTITY}
    try:
        s["bus"] = int(s["bus"]) if s["bus"] is not None else None
    except ValueError:
        s["bus"] = None
    s["mode"] = raw.get("mode") if raw.get("mode") in MODES else "overlay"
    return s


def save_config(cfg):
    lines = ["# Monitor Dimmer configuration"]
    for k in DEFAULTS:
        v = cfg.get(k)
        if isinstance(v, bool):
            v = str(v).lower()
        elif isinstance(v, list):
            v = ",".join(v)
        lines.append(f"{k}={v}")
    for i, s in enumerate(cfg.get("screens", []), 1):
        for k in SCREEN_KEYS:
            if s.get(k) is not None:
                lines.append(f"screen{i}_{k}={s[k]}")
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text("\n".join(lines) + "\n")


def has_identity(screen):
    return any(screen.get(k) for k in ("model", "sn", "connector"))


def screen_label(s):
    name = " ".join(x for x in (s.get("mfg"), s.get("model")) if x) or "Unknown monitor"
    extra = [s["connector"]] if s.get("connector") else []
    if s.get("bus") is not None:
        extra.append(f"i2c-{s['bus']}")
    return f"{name} ({', '.join(extra)})" if extra else name


def active_screens(cfg):
    return [s for s in cfg["screens"] if s["mode"] != "off"]


def screens_label(cfg):
    names = [f"{screen_label(s)}: {s['mode']}" for s in active_screens(cfg)]
    return "; ".join(names) if names else "none"


# ── Monitors ──────────────────────────────────────────────────────────────────

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
        elif "DRM connector:" in line:
            # "card1-DP-3" -> "DP-3", the name compositors use
            current["connector"] = re.sub(r"^card\d+-", "", line.split(":", 1)[1].strip())
        elif "Mfg id:" in line:
            current["mfg"] = line.split(":", 1)[1].split(" - ")[0].strip()
        elif "Model:" in line:
            current["model"] = line.split(":", 1)[1].strip()
        elif "Serial number:" in line:
            current["sn"] = line.split(":", 1)[1].strip()
    return [d for d in displays if "bus" in d]


def qt_screens(python=None):
    """Screens as the compositor sees them, as [{connector, mfg, model, sn}]."""
    try:
        out = subprocess.run([python or sys.executable, str(OVERLAY_BIN), "--list"],
                             capture_output=True, text=True, timeout=15).stdout
        return json.loads(out)
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return []


def all_monitors(python=None):
    """Every screen, with its DDC bus when it has one.

    The compositor knows every screen (laptop panels and TVs too); ddcutil only
    sees those that speak DDC/CI. Without a graphical session fall back to the
    ddcutil list alone.
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


def dimmed_value(current, dim_level):
    return max(0, round(current * (100 - dim_level) / 100))


# Brightness to restore, per dimmed screen: [{"screen": identity, "bus": n,
# "brightness": v}]. Kept on disk until restored, so a crash or `systemctl
# stop` mid-game can still put it back.

def load_state():
    try:
        state = json.loads(STATE_FILE.read_text())
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
    STATE_FILE.write_text(json.dumps(state))


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
