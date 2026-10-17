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

TARGET_KEYS = ("target_bus", "target_connector", "target_mfg", "target_model", "target_sn")

DEFAULTS = {
    "dim_level":          70,
    "poll_interval":      5,
    "use_overlay":        True,
    "vlc_fullscreen":     True,
    "require_fullscreen": False,
    "game_paths":         [],
    "target_bus":         None,
    "target_connector":   None,
    "target_mfg":         None,
    "target_model":       None,
    "target_sn":          None,
}

INT_KEYS  = {"dim_level": (0, 100), "poll_interval": (1, 3600), "target_bus": (0, 9999)}
BOOL_KEYS = {"use_overlay", "vlc_fullscreen", "require_fullscreen"}


def warn(msg):
    print(f"monitor-dimmer: {msg}", file=sys.stderr, flush=True)


# ── Config ────────────────────────────────────────────────────────────────────

def load_config():
    cfg = dict(DEFAULTS, game_paths=[])
    try:
        text = CONFIG_FILE.read_text()
    except FileNotFoundError:
        return cfg
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        k, _, v = line.partition("=")
        k, v = k.strip(), v.strip()
        if k.startswith("right_"):              # pre-1.1 names
            k = "target_" + k[len("right_"):]
        if k in INT_KEYS:
            lo, hi = INT_KEYS[k]
            try:
                cfg[k] = min(max(int(v), lo), hi)
            except ValueError:
                warn(f"ignoring invalid {k}={v!r} in {CONFIG_FILE}")
        elif k in BOOL_KEYS:
            cfg[k] = v.lower() in ("true", "yes", "1", "on")
        elif k == "game_paths":
            cfg[k] = [p.strip() for p in v.split(",") if p.strip()]
        elif k in DEFAULTS:
            cfg[k] = v or None
    return cfg


def save_config(cfg):
    lines = ["# Monitor Dimmer configuration"]
    for k in DEFAULTS:
        v = cfg.get(k)
        if v is None or v == []:
            continue
        if isinstance(v, bool):
            v = str(v).lower()
        elif isinstance(v, list):
            v = ",".join(v)
        lines.append(f"{k}={v}")
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text("\n".join(lines) + "\n")


def target_label(cfg):
    name = " ".join(x for x in (cfg.get("target_mfg"), cfg.get("target_model")) if x)
    if cfg.get("target_connector"):
        name = f"{name} ({cfg['target_connector']})" if name else cfg["target_connector"]
    if not name and cfg.get("target_bus") is not None:
        name = f"i2c-{cfg['target_bus']}"
    return name or "not set"


def has_target(cfg):
    return any(cfg.get(k) for k in ("target_model", "target_sn", "target_connector"))


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


def monitor_label(m):
    name = " ".join(x for x in (m.get("mfg"), m.get("model")) if x) or "Unknown monitor"
    extra = [m["connector"]] if m.get("connector") else []
    if m.get("bus") is not None:
        extra.append(f"i2c-{m['bus']}")
    return f"{name} ({', '.join(extra)})" if extra else name


def set_target(cfg, m):
    cfg["target_bus"]       = m.get("bus")
    cfg["target_connector"] = m.get("connector") or None
    cfg["target_mfg"]       = m.get("mfg") or None
    cfg["target_model"]     = m.get("model") or None
    cfg["target_sn"]        = m.get("sn") or None


def find_target(cfg, monitors):
    """Pick the configured monitor out of `monitors`, or None.

    Bus numbers and connector names follow the GPU port, so they change when
    cables are swapped; the serial number and model travel with the monitor.
    Match the serial first, then the model, and use the connector / bus only
    to tell identical monitors apart.
    """
    sn, model = cfg.get("target_sn"), cfg.get("target_model")
    if sn:
        hits = [m for m in monitors if m.get("sn") == sn]
        if len(hits) == 1:
            return hits[0]
    if model:
        hits = [m for m in monitors if m.get("model") == model]
        if len(hits) > 1:
            hits = ([m for m in hits if m.get("connector") == cfg.get("target_connector")]
                    or [m for m in hits if m.get("bus") == cfg.get("target_bus")])
        return hits[0] if len(hits) == 1 else None
    if not sn and cfg.get("target_connector"):
        hits = [m for m in monitors if m.get("connector") == cfg["target_connector"]]
        return hits[0] if len(hits) == 1 else None
    return None


def find_target_bus(cfg):
    if not has_target(cfg):
        return cfg.get("target_bus")             # old config: bus number only
    m = find_target(cfg, ddc_displays())
    return m["bus"] if m else None


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


def save_state(state):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state))


def load_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        return None


def clear_state():
    try:
        STATE_FILE.unlink()
    except FileNotFoundError:
        pass


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
