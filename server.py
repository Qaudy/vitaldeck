#!/usr/bin/env python3
"""vitaldeck — zero-dependency homelab dashboard for Raspberry Pi.

Pure stdlib: reads /proc and /sys directly, shells out to docker for
container state, and streams everything to the browser over SSE.

    python3 server.py            # http://0.0.0.0:8800
    python3 server.py 9000       # custom port
"""

import base64
import copy
import hashlib
import hmac
import http.cookies
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE = os.path.dirname(os.path.abspath(__file__))
# Writable state lives here (links/config/categories + auth.json), separate from
# the read-only app code in BASE. In Docker a named volume mounts at /app/data so
# in-dashboard edits and the admin account survive container recreation. Defaults
# to ./data for bare-metal runs. seed_data() fills it from the baked examples on
# first launch, so a fresh install works with zero config files (Jellyfin-style).
DATA = os.environ.get("DATA_DIR", os.path.join(BASE, "data"))
# precedence: argv (bare-metal `python3 server.py 9000`) > $PORT (container
# convention) > 8800. argv wins so the documented CLI usage still works.
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else int(os.environ.get("PORT", "8800"))
TICK = 2.0          # seconds between samples
DOCKER_EVERY = 2    # sample docker every N ticks → every 4s (it forks a
                    # process, so kept off the every-tick /proc hot path)

# Ceiling on simultaneous SSE streams (one per open dashboard tab). Each stream
# holds a thread for its whole lifetime, so an unbounded flood is a DoS surface
# if the dashboard is ever exposed. 32 is far above any realistic tab count.
MAX_SSE = 32
_sse_lock = threading.Lock()
_sse_count = 0      # streams currently open; guarded by _sse_lock

# Interfaces worth showing: skip loopback, docker bridges and veth pairs.
IFACE_SKIP = re.compile(r"^(lo|docker\d+|br-[0-9a-f]+|veth.*)$")

# Rate-limit for login/setup: 5 failures → 5-minute lockout.
# Keyed by client IP (client_address[0]). If vitaldeck is behind a reverse
# proxy this will always be 127.0.0.1 — X-Forwarded-For is not trusted here
# because it is attacker-controlled without a verified proxy in front.
_login_attempts = {}           # {ip: (fail_count, lockout_until_epoch)}
_login_attempts_lock = threading.Lock()
RATE_MAX  = 5                  # failures before lockout
RATE_LOCK = 5 * 60             # lockout duration in seconds

# Built-in alert thresholds. config.json (bind-mounted, hot-editable) overlays
# these; missing keys fall back here. temp is °C absolute; cpu/mem/disk are %.
DEFAULTS = {
    # label shown in alert messages ("… on <name>"); empty -> use the hostname
    "name": "",
    # <small> tagline beside the logo; empty -> "HOMELAB"
    "tagline": "HOMELAB",
    # temperature display unit shown in the ring gauge label ("C" or "F")
    "temp_unit": "C",
    # ring gauge 100% point in °C — Pi throttles at 85, tune for your hardware
    "temp_throttle_c": 85,
    # disk bar turns amber above this % (separate from alerts.disk.warn, which
    # triggers Discord; these are independent thresholds)
    "disk_warn_pct": 80,
    # --- tier 3: consumed by the server at startup; restart to apply ---
    "tick_seconds": 2.0,        # sampler + SSE push interval
    "disk_min_gb":  1,          # hide volumes smaller than this (in GB)
    "iface_skip":   r"^(lo|docker\d+|br-[0-9a-f]+|veth.*)$",
    "alerts": {
        "temp": {"warn": 70, "crit": 85},
        "cpu":  {"warn": 85, "crit": 95},
        "mem":  {"warn": 80, "crit": 92},
        "disk": {"warn": 80, "crit": 92},
        # notify when a container starts/stops; toggle off without losing config
        "containers": {"enabled": True},
        "webhook": {"discord_url": "", "min_severity": "crit"},
    },
    # UI colour palette, editable in the dashboard's Appearance settings. Each
    # value is a CSS hex colour the frontend applies to a --css-variable. card /
    # card_edge translucency stays fixed in the stylesheet (alpha isn't pickable
    # with <input type=color>); these are the solid accents worth theming.
    "theme": {
        "bg": "#07080f", "text": "#e8ecf4", "dim": "#8d96ad",
        "cyan": "#38e1ff", "violet": "#a06bff", "pink": "#ff5fa8",
        "green": "#3ddc84", "amber": "#ffb454", "red": "#ff5468",
        "aurora1": "#1450ff", "aurora2": "#8a2bff", "aurora3": "#00d4c0",
        "spark_cpu": "#38e1ff", "spark_tx": "#ff5fa8",
    },
}

# Allowed theme keys + the hex-colour shape the write path enforces, so a saved
# value can never be anything but a colour (it's injected as live CSS).
THEME_KEYS = set(DEFAULTS["theme"])
HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")

# none < warn < crit — lets the alert logic compare severities by rank.
RANK = {"none": 0, "warn": 1, "crit": 2}

# When containerised, mount the host's /proc, /sys and / and point these at
# the mountpoints (netdata convention) so stats reflect the host, not the box.
PROC = os.environ.get("HOST_PROC", "/proc")
SYS = os.environ.get("HOST_SYS", "/sys")
HOST_ROOT = os.environ.get("HOST_ROOT", "")  # prefix for statvfs paths


# ---------------------------------------------------------------- sampling

def read_file(path, default=""):
    """Read a whole (pseudo-)file, returning `default` if it doesn't exist."""
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return default


def cpu_times():
    """Per-core (busy, total) jiffies from /proc/stat.

    Each cpuN line is cumulative time split into columns:
        user nice system idle iowait irq softirq steal ...
    A single reading is meaningless — usage % comes from the delta between
    two samples: (busy1-busy0)/(total1-total0). The Sampler does that math.
    idle + iowait both count as "not busy" (iowait is idle-waiting-on-IO).
    """
    cores = {}
    for line in read_file(f"{PROC}/stat").splitlines():
        # "cpu0".."cpuN" only — skip the aggregate "cpu" summary line
        if line.startswith("cpu") and line[3:4].isdigit():
            parts = line.split()
            vals = [int(v) for v in parts[1:]]
            idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
            cores[parts[0]] = (sum(vals) - idle, sum(vals))
    return cores


def net_bytes():
    """{iface: (rx_bytes, tx_bytes)} cumulative counters from /proc/net/dev.

    Format: two header lines, then "iface: rx_bytes ... [8 cols] tx_bytes ...".
    Counters only ever grow; throughput = delta / elapsed time (Sampler).
    """
    out = {}
    for line in read_file(f"{PROC}/net/dev").splitlines()[2:]:
        name, rest = line.split(":", 1)
        name = name.strip()
        if IFACE_SKIP.match(name):
            continue
        f = rest.split()
        out[name] = (int(f[0]), int(f[8]))  # col 0 = rx bytes, col 8 = tx bytes
    return out


def meminfo():
    """Parse /proc/meminfo into {key: bytes} (file reports kB)."""
    mi = {}
    for line in read_file(f"{PROC}/meminfo").splitlines():
        k, v = line.split(":", 1)
        mi[k] = int(v.split()[0]) * 1024  # kB -> bytes
    return mi


def disks():
    """Usage for every real block-device mount ≥ 1 GB.

    Filters: device must live under /dev/ (drops tmpfs/overlay/proc etc.),
    each device counted once (bind mounts share a device), and volumes under
    1 GB are noise (boot partitions, squashfs snaps).
    """
    out = []
    seen = set()
    # /proc/mounts symlinks to /proc/self/mounts, which follows the *reader's*
    # mount namespace even through a bind mount — /proc/1/mounts does not.
    mounts = read_file(f"{PROC}/1/mounts") or read_file(f"{PROC}/mounts")
    for line in mounts.splitlines():
        dev, mnt, fstype = line.split()[:3]
        if not dev.startswith("/dev/") or dev in seen:
            continue
        seen.add(dev)
        try:
            # in a container the host's "/" is visible at HOST_ROOT, so the
            # host path "/mnt/storage" is stat'd as "/host/root/mnt/storage"
            st = os.statvfs(HOST_ROOT + mnt if HOST_ROOT else mnt)
        except OSError:
            continue
        total = st.f_blocks * st.f_frsize
        if total < 1 << 30:  # skip tiny pseudo-volumes
            continue
        free = st.f_bavail * st.f_frsize
        out.append({"mount": mnt, "total": total, "used": total - free})
    return out


def load_categories():
    """Category rules from categories.json: [{"name": ..., "path": ...}, ...].

    Each rule maps a display name to a host path; containers whose compose
    project lives under that path are grouped beneath the name. Read fresh
    on every docker poll so edits apply without a restart (same idea as
    links.json).
    """
    try:
        with open(data_path("categories.json")) as f:
            rules = json.load(f)
        return rules if isinstance(rules, list) else []
    except (OSError, ValueError):
        return []


def categorize(workdir, rules):
    """Pick the category name for a container, or None for uncategorized.

    `workdir` is the host directory of the compose file that launched the
    container (the com.docker.compose.project.working_dir label), e.g.
    "/home/user/containers/nova_tv/jellyfin". Empty string for containers
    started outside compose (docker run).
    """
    if not workdir:
        return None
    for rule in rules:
        path = rule.get("path", "").rstrip("/")
        # exact match or true subdirectory — the "/" suffix stops
        # /x/nova_tv2 from matching a /x/nova_tv rule
        if path and (workdir == path or workdir.startswith(path + "/")):
            return rule.get("name")
    return None


def docker_ps():
    """All containers via the docker CLI, or None if docker is unusable.

    Tab-separated --format keeps parsing trivial; the 4s timeout stops a
    hung docker daemon from stalling the sampler thread. The compose
    working_dir label tells us which stack directory a container came
    from, which drives the category grouping in the UI.
    """
    if not shutil.which("docker"):
        return None
    try:
        r = subprocess.run(
            ["docker", "ps", "-a", "--format",
             "{{.Names}}\t{{.State}}\t{{.Status}}\t{{.Image}}"
             "\t{{.Label \"com.docker.compose.project.working_dir\"}}"],
            capture_output=True, text=True, timeout=4)
        if r.returncode != 0:
            return None
        rules = load_categories()
        rows = []
        for line in r.stdout.splitlines():
            name, state, status, image, workdir = (line.split("\t") + [""] * 5)[:5]
            rows.append({"name": name, "state": state,
                         "status": status, "image": image,
                         "category": categorize(workdir, rules)})
        return rows
    except (subprocess.TimeoutExpired, OSError):
        return None


# ------------------------------------------------------------ data dir / seed

# Runtime files that live in DATA, each seeded from a baked example in BASE.
SEED_FILES = {
    "links.json": "links.example.json",
    "config.json": "config.example.json",
    "categories.json": "categories.example.json",
}


def data_path(name):
    """Absolute path to a runtime file inside the writable DATA dir."""
    return os.path.join(DATA, name)


def seed_data():
    """Ensure DATA exists and is populated so a fresh install just works.

    Copies each baked *.example.json into DATA only when the target is absent,
    so it never clobbers a user's edits on later boots. This is what makes the
    container zero-config: with the old per-file bind mounts a missing file
    crashed startup, but a named volume + seeding gives working defaults that
    then persist. auth.json is NOT seeded — it's created by the setup wizard.
    """
    os.makedirs(DATA, exist_ok=True)
    for target, example in SEED_FILES.items():
        dest, src = data_path(target), os.path.join(BASE, example)
        if not os.path.exists(dest) and os.path.exists(src):
            shutil.copyfile(src, dest)


def load_alert_state():
    """Load persisted alert + container state, or return empty dicts on failure."""
    try:
        with open(data_path("alert_state.json")) as f:
            d = json.load(f)
        if not isinstance(d, dict):
            return {}, {}
        return d.get("alert_state") or {}, d.get("ctr_state") or {}
    except (OSError, ValueError):
        return {}, {}


def save_alert_state(alert_state, ctr_state):
    """Persist alert + container state so restarts don't re-fire existing alerts."""
    try:
        write_json("alert_state.json", {"alert_state": alert_state,
                                         "ctr_state":   ctr_state})
    except OSError as e:
        print(f"alert state persist error: {e}", file=sys.stderr)


def load_config():
    """Merged config: config.json overlaid on DEFAULTS, read fresh each call.

    Same rail as links.json/categories.json — re-reading per use means a user
    can retune a threshold or paste a webhook URL and have it apply on the next
    tick, no restart. `alerts` is deep-merged one level so a one-line override
    like {"alerts": {"temp": {"crit": 80}}} keeps every other default intact.
    """
    cfg = copy.deepcopy(DEFAULTS)
    try:
        with open(data_path("config.json")) as f:
            user = json.load(f)
    except (OSError, ValueError):
        user = {}
    if not isinstance(user, dict):
        return cfg
    for key, val in user.items():
        if key == "alerts" and isinstance(val, dict):
            for metric, sub in val.items():
                if isinstance(sub, dict) and isinstance(cfg["alerts"].get(metric), dict):
                    cfg["alerts"][metric].update(sub)
                else:
                    cfg["alerts"][metric] = sub
        else:
            cfg[key] = val
    return cfg


def public_config(cfg):
    """Return the full merged config for an authenticated browser session.

    Now that /api/config is behind the login gate, every field — including the
    Discord webhook URL — can be surfaced so the settings UI can show and edit
    it. The endpoint is still auth-gated in do_GET/do_POST, so unauthenticated
    requests never reach this function.
    """
    c = cfg if isinstance(cfg, dict) else {}
    alerts = c.get("alerts", {}) if isinstance(c, dict) else {}
    wh = alerts.get("webhook", {}) if isinstance(alerts, dict) else {}
    full_alerts = {k: v for k, v in alerts.items() if k != "webhook"}
    full_alerts["webhook"] = {
        "discord_url":  wh.get("discord_url", ""),
        "min_severity": wh.get("min_severity", "crit"),
    }
    return {
        "name":           c.get("name", ""),
        "tagline":        c.get("tagline", DEFAULTS["tagline"]),
        "temp_unit":      c.get("temp_unit", DEFAULTS["temp_unit"]),
        "temp_throttle_c": c.get("temp_throttle_c", DEFAULTS["temp_throttle_c"]),
        "disk_warn_pct":  c.get("disk_warn_pct", DEFAULTS["disk_warn_pct"]),
        "tick_seconds":   c.get("tick_seconds", DEFAULTS["tick_seconds"]),
        "disk_min_gb":    c.get("disk_min_gb", DEFAULTS["disk_min_gb"]),
        "iface_skip":     c.get("iface_skip", DEFAULTS["iface_skip"]),
        "alerts":         full_alerts,
        "theme":          c.get("theme", {}),
    }


# ------------------------------------------------------------------- auth
#
# Jellyfin/Sonarr-style local auth, pure stdlib: a first-run wizard creates one
# admin account whose salted PBKDF2 hash lives in DATA/auth.json. Sessions are
# stateless, signed cookies — an HMAC over "user|expiry" keyed by a random secret
# that's persisted alongside the hash, so logins survive restarts without any
# server-side session store. No password ever touches an env var or the repo.

PBKDF2_ITER = 200_000
SESSION_TTL = 7 * 24 * 3600   # seconds a login cookie stays valid
COOKIE_NAME = "vd_session"


def _pbkdf2(password, salt, iterations):
    """Derive the raw password hash. Single source of truth so the setup path
    and the verify path always agree on the algorithm and parameters."""
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)


def load_auth():
    """The admin record dict, or None if no account has been created yet."""
    try:
        with open(data_path("auth.json")) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def save_auth(record):
    """Persist the admin record. 0o600 because it holds the password hash and
    the cookie-signing secret — both sensitive even at rest."""
    path = data_path("auth.json")
    with open(path, "w") as f:
        json.dump(record, f)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass  # best-effort (e.g. odd filesystems); the volume is already private


def is_configured():
    """True once an admin account exists (drives setup-vs-login in the UI)."""
    return load_auth() is not None


def create_admin(user, password):
    """Build and persist the one admin account: a salted PBKDF2 hash plus a
    fresh random HMAC secret used to sign session cookies."""
    salt = secrets.token_bytes(16)
    record = {
        "user": user,
        "salt": salt.hex(),
        "hash": _pbkdf2(password, salt, PBKDF2_ITER).hex(),
        "iterations": PBKDF2_ITER,
        "secret": secrets.token_hex(32),
    }
    save_auth(record)
    return record


def change_password(record, new_password):
    """Re-hash the password and rotate the session secret.

    Rotating the secret immediately invalidates every existing session token
    (they are signed with the old key) — stateless revocation at no extra cost.
    Open SSE streams are auth-checked only at connect time so they keep pushing
    until the next non-SSE authenticated request fails.
    """
    salt = secrets.token_bytes(16)
    updated = dict(record)
    updated["salt"]       = salt.hex()
    updated["hash"]       = _pbkdf2(new_password, salt, PBKDF2_ITER).hex()
    updated["iterations"] = PBKDF2_ITER
    updated["secret"]     = secrets.token_hex(32)
    save_auth(updated)
    return updated


def verify_password(password, record):
    """Return True iff `password` matches the stored admin hash in `record`.

    `record` is a dict from load_auth() with hex-encoded "salt" and "hash" and
    an int "iterations". Re-derive the hash from the supplied password using the
    SAME salt/iterations (helper: `_pbkdf2(password, salt_bytes, iterations)`),
    then compare it to the stored hash.

    Convert the hex "salt"/"hash" back to bytes, re-derive the candidate hash
    with the SAME salt and iteration count, and compare with hmac.compare_digest
    — a plain `==` on secret material leaks length/prefix info through timing,
    exactly the side channel constant-time comparison exists to close.
    """
    if not isinstance(record, dict):
        return False
    try:
        salt = bytes.fromhex(record["salt"])
        stored = bytes.fromhex(record["hash"])
        iterations = int(record["iterations"])
    except (KeyError, ValueError, TypeError):
        return False
    candidate = _pbkdf2(password, salt, iterations)
    return hmac.compare_digest(candidate, stored)


def _secret_bytes(record):
    return bytes.fromhex(record["secret"])


def make_session(record):
    """Mint a signed session token: base64url("user|expiry") + "." + HMAC sig."""
    expiry = int(time.time()) + SESSION_TTL
    payload = base64.urlsafe_b64encode(
        f"{record['user']}|{expiry}".encode()).decode().rstrip("=")
    sig = hmac.new(_secret_bytes(record), payload.encode(),
                   hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def check_session(token):
    """Validate a session token; return the username or None.

    Stateless: verify the HMAC signature against the persisted secret, then the
    embedded expiry. Constant-time signature compare so a forged cookie can't be
    brute-forced byte-by-byte via timing.
    """
    record = load_auth()
    if not record or not token or "." not in token:
        return None
    payload, _, sig = token.partition(".")
    expected = hmac.new(_secret_bytes(record), payload.encode(),
                        hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        raw = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
        user, _, exp = raw.decode().partition("|")
    except (ValueError, UnicodeDecodeError):
        return None
    if not exp.isdigit() or int(exp) < time.time():
        return None
    return user if user == record.get("user") else None


# ------------------------------------------------------ validated writes
#
# Everything below persists browser-supplied data into DATA. The dashboard has
# no per-field trust, so each writer re-validates: links get URL-scheme checks,
# config coerces numeric thresholds and PRESERVES the webhook secret (never
# accepting one from the client), and theme values must be hex colours.

URL_OK = re.compile(r"^(https?:|ssh:|/|#)", re.I)


def write_json(name, obj):
    """Atomically replace DATA/<name> with obj (write-temp-then-rename), so a
    crash mid-write can't leave a half-written config the loaders would reject."""
    path = data_path(name)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=2)
    os.replace(tmp, path)


def load_raw_config():
    """The on-disk config.json as-is (NOT merged with DEFAULTS), so writers can
    preserve fields the UI never sends — above all alerts.webhook."""
    try:
        with open(data_path("config.json")) as f:
            c = json.load(f)
        return c if isinstance(c, dict) else {}
    except (OSError, ValueError):
        return {}


def safe_url(u):
    """Mirror the frontend's safeUrl: keep http(s)/ssh/relative/anchor, else #."""
    s = str(u or "").strip()
    return s if URL_OK.match(s) else "#"


def sanitize_links(incoming):
    """Validate a links payload into a clean list, or return (None, error)."""
    if not isinstance(incoming, list):
        return None, "links must be a list"
    out = []
    for item in incoming:
        if not isinstance(item, dict):
            return None, "each link must be an object"
        name = str(item.get("name", "")).strip()[:80]
        if not name:
            continue  # drop blank rows rather than error
        out.append({
            "name": name,
            "url": safe_url(item.get("url", "")),
            "desc": str(item.get("desc", "")).strip()[:120],
            "icon": str(item.get("icon", "")).strip()[:8] or "🔗",
        })
    return out, None


def sanitize_categories(incoming):
    """Validate a categories payload into a clean list, or (None, error)."""
    if not isinstance(incoming, list):
        return None, "categories must be a list"
    out = []
    for item in incoming:
        if not isinstance(item, dict):
            return None, "each category must be an object"
        name = str(item.get("name", "")).strip()[:80]
        path = str(item.get("path", "")).strip()[:255]
        if name and path:
            out.append({"name": name, "path": path})
    return out, None


def _num_or_none(v):
    """A threshold is a non-negative number, or None to disable that level."""
    if v is None:
        return True, None
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return False, None
    return (0 <= v <= 1000), v


def sanitize_config(incoming):
    """Merge a validated settings payload onto the on-disk config and return
    (config_to_persist, None), or (None, error).
    """
    if not isinstance(incoming, dict):
        return None, "config must be an object"
    cfg = copy.deepcopy(load_raw_config())
    alerts = cfg.setdefault("alerts", {})

    if "name" in incoming:
        cfg["name"] = str(incoming.get("name") or "").strip()[:64]

    if "tagline" in incoming:
        cfg["tagline"] = str(incoming.get("tagline") or "").strip()[:32]

    if "temp_unit" in incoming:
        val = str(incoming.get("temp_unit", "C")).upper()
        if val not in ("C", "F"):
            return None, "temp_unit must be 'C' or 'F'"
        cfg["temp_unit"] = val

    if "temp_throttle_c" in incoming:
        v = incoming.get("temp_throttle_c")
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not (0 < v <= 200):
            return None, "temp_throttle_c must be a number between 0 and 200"
        cfg["temp_throttle_c"] = float(v)

    if "disk_warn_pct" in incoming:
        v = incoming.get("disk_warn_pct")
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not (0 <= v <= 100):
            return None, "disk_warn_pct must be a number between 0 and 100"
        cfg["disk_warn_pct"] = float(v)

    if "tick_seconds" in incoming:
        v = incoming.get("tick_seconds")
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not (0.5 <= v <= 60):
            return None, "tick_seconds must be a number between 0.5 and 60"
        cfg["tick_seconds"] = float(v)

    if "disk_min_gb" in incoming:
        v = incoming.get("disk_min_gb")
        if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0:
            return None, "disk_min_gb must be a non-negative number"
        cfg["disk_min_gb"] = float(v)

    if "iface_skip" in incoming:
        s = str(incoming.get("iface_skip", ""))
        try:
            re.compile(s)
        except re.error:
            return None, "iface_skip must be a valid regular expression"
        cfg["iface_skip"] = s

    a = incoming.get("alerts")
    if isinstance(a, dict):
        for metric in ("temp", "cpu", "mem", "disk"):
            sub = a.get(metric)
            if not isinstance(sub, dict):
                continue
            cur = alerts.get(metric) if isinstance(alerts.get(metric), dict) else {}
            for level in ("warn", "crit"):
                if level in sub:
                    ok, val = _num_or_none(sub[level])
                    if not ok:
                        return None, f"alerts.{metric}.{level} must be a number or null"
                    cur[level] = val
            alerts[metric] = cur
        if isinstance(a.get("containers"), dict) and "enabled" in a["containers"]:
            alerts.setdefault("containers", {})["enabled"] = bool(a["containers"]["enabled"])
        wh_in = a.get("webhook")
        if isinstance(wh_in, dict):
            wh = alerts.setdefault("webhook", {})
            if "discord_url" in wh_in:
                url = str(wh_in["discord_url"] or "").strip()[:512]
                wh["discord_url"] = url
            if "min_severity" in wh_in:
                if wh_in["min_severity"] not in ("warn", "crit"):
                    return None, "min_severity must be 'warn' or 'crit'"
                wh["min_severity"] = wh_in["min_severity"]
        # legacy flat field (older payloads sent min_severity at the alerts level)
        elif "min_severity" in a:
            if a["min_severity"] not in ("warn", "crit"):
                return None, "min_severity must be 'warn' or 'crit'"
            alerts.setdefault("webhook", {})["min_severity"] = a["min_severity"]

    t = incoming.get("theme")
    if isinstance(t, dict):
        theme = cfg.setdefault("theme", {})
        for key, val in t.items():
            if key not in THEME_KEYS:
                continue  # ignore unknown keys silently
            if not (isinstance(val, str) and HEX_RE.match(val)):
                return None, f"theme.{key} must be a #rrggbb hex colour"
            theme[key] = val

    return cfg, None


def severity_of(value, warn, crit):
    """Bucket a value into 'none' < 'warn' < 'crit' against its two thresholds.

    A threshold left as None is simply never crossed, so a user can disable one
    level (e.g. warn-only or crit-only) by omitting it from config.json.
    """
    if crit is not None and value >= crit:
        return "crit"
    if warn is not None and value >= warn:
        return "warn"
    return "none"


def step_alert(metric, sev, min_sev, state):
    """Edge-triggered alert decision for ONE metric — the heart of the
    "fire once per episode" behaviour.

    Args:
        metric:  "temp" | "cpu" | "mem" | "disk".
        sev:     this tick's severity from severity_of(): "none"|"warn"|"crit".
        min_sev: webhook.min_severity from config: "warn" or "crit".
        state:   mutable {metric: "armed"|"alerting"}, persisted across ticks.
                 A metric not present yet should be treated as "armed".
                 READ and UPDATE state[metric] in here.

    Return an event dict to send to Discord, or None to stay quiet:
        breach:   {"metric": metric, "kind": "breach", "severity": sev}
        recovery: {"metric": metric, "kind": "recovery", "severity": "none"}

    Locked rules:
        - ARMED and RANK[sev] >= RANK[min_sev]  -> emit breach, become "alerting".
        - ALERTING and sev == "none" (dropped below warn) -> emit recovery,
          become "armed".
        - One fire per episode: while "alerting", a warn->crit escalation does
          NOT emit again (no second alert until it recovers and re-breaches).
    """
    status = state.get(metric, "armed")
    if status == "armed":
        if RANK[sev] >= RANK[min_sev]:
            state[metric] = "alerting"
            return {"metric": metric, "kind": "breach", "severity": sev}
    else:  # "alerting" — wait for a full recovery before we can fire again
        if sev == "none":
            state[metric] = "armed"
            return {"metric": metric, "kind": "recovery", "severity": "none"}
    return None


def container_events(docker, state):
    """Edge-trigger container start/stop notifications.

    Diffs the current docker list's running-state against the previous poll.
    `docker` is the docker_ps() list, or None when docker is unreachable — in
    which case we emit nothing (a transient daemon hiccup must not look like
    every container stopping). `state` is mutable {name: is_running}, carried
    across ticks and seeded at startup so a fresh boot doesn't alert for
    containers that were already up.

    Returns a list of {"kind": "ctr_start"|"ctr_stop", "name": ...} events.
    """
    if docker is None:
        return []
    events = []
    now = {c["name"]: (c["state"] == "running") for c in docker}
    for name, running in now.items():
        was = state.get(name, False)
        if running and not was:
            events.append({"kind": "ctr_start", "name": name})
        elif not running and was:
            events.append({"kind": "ctr_stop", "name": name})
    # a container that disappeared while running counts as a stop
    for name in [n for n in state if n not in now]:
        if state[name]:
            events.append({"kind": "ctr_stop", "name": name})
        del state[name]
    state.update(now)  # commit this poll as the new baseline
    return events


def format_container(ev, label):
    """Render one container start/stop event as Discord message text."""
    if ev["kind"] == "ctr_start":
        return f"🟢 **container started** on {label} — `{ev['name']}`"
    return f"🔴 **container stopped** on {label} — `{ev['name']}`"


def format_alert(ev):
    """Render one alert event as Discord message text."""
    unit = "°C" if ev["metric"] == "temp" else "%"
    name = ev["metric"].upper()
    host = ev.get("host") or "host"
    if ev["kind"] == "recovery":
        return f"✅ **{name} recovered** on {host} — now {ev['value']:.0f}{unit}"
    sev = ev["severity"]
    icon = "🔴" if sev == "crit" else "🟠"
    thr = ev.get("crit") if sev == "crit" else ev.get("warn")
    return (f"{icon} **{name} {sev.upper()}** on {host} — "
            f"{ev['value']:.0f}{unit} (≥ {thr}{unit})")


def post_discord(url, content):
    """Fire-and-forget Discord webhook POST (stdlib urllib, zero deps).

    Runs on its own daemon thread so a slow or unreachable Discord never
    stalls the sampler tick — the dashboard keeps updating regardless.
    """
    def _send():
        try:
            data = json.dumps({"content": content}).encode()
            # Discord 403s the default Python-urllib User-Agent; any custom one
            # is accepted, so identify ourselves explicitly.
            req = urllib.request.Request(
                url, data=data, headers={"Content-Type": "application/json",
                                         "User-Agent": "vitaldeck/1.0"})
            urllib.request.urlopen(req, timeout=5)
        except Exception as e:
            print(f"discord webhook error: {e}", file=sys.stderr)
    threading.Thread(target=_send, daemon=True).start()


class Sampler:
    """Background thread that keeps the latest stats snapshot in memory.

    One sampler serves every connected browser: each HTTP/SSE handler just
    reads the shared snapshot, so ten open tabs cost the same as one.
    Rates (CPU %, net B/s) need two readings, so previous values are kept
    between ticks and deltas computed against wall-clock-independent
    time.monotonic().
    """

    def __init__(self):
        # --- tier 3: apply config-driven constants before the first sample ---
        startup_cfg = load_config()
        global TICK, IFACE_SKIP
        tick = startup_cfg.get("tick_seconds", DEFAULTS["tick_seconds"])
        if not isinstance(tick, bool) and isinstance(tick, (int, float)) and 0.5 <= tick <= 60:
            TICK = float(tick)
        iface_pat = startup_cfg.get("iface_skip", DEFAULTS["iface_skip"])
        try:
            IFACE_SKIP = re.compile(str(iface_pat))
        except re.error:
            pass  # keep the compiled default if stored value is invalid
        raw_min_gb = startup_cfg.get("disk_min_gb", DEFAULTS["disk_min_gb"])
        self._disk_min_gb = float(raw_min_gb) if (
            not isinstance(raw_min_gb, bool) and
            isinstance(raw_min_gb, (int, float)) and raw_min_gb >= 0) else 1

        self.snapshot = {}
        self.lock = threading.Lock()
        self._prev_cpu = cpu_times()
        self._prev_net = net_bytes()
        self._prev_t = time.monotonic()
        self._docker = docker_ps()
        self._tick_n = 0

        # restore persisted alert state so a restart doesn't re-fire alerts
        # that were already in flight; fall back to in-memory defaults if absent
        saved_alert, saved_ctr = load_alert_state()
        self._alert_state = saved_alert if saved_alert else {}
        if saved_ctr:
            self._ctr_state = saved_ctr
        else:
            # seed baseline from current docker state so a fresh boot doesn't
            # alert for containers that were already up
            self._ctr_state = {c["name"]: (c["state"] == "running")
                               for c in (self._docker or [])}
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while True:
            time.sleep(TICK)
            try:
                self._sample()
            except Exception as e:  # keep the loop alive no matter what
                print(f"sampler error: {e}", file=sys.stderr)

    def _sample(self):
        now = time.monotonic()
        dt = max(now - self._prev_t, 0.001)

        cur_cpu = cpu_times()
        cores = []
        for name in sorted(cur_cpu, key=lambda n: int(n[3:])):
            busy0, tot0 = self._prev_cpu.get(name, (0, 0))
            busy1, tot1 = cur_cpu[name]
            d_tot = tot1 - tot0
            cores.append(round(100 * (busy1 - busy0) / d_tot, 1) if d_tot else 0.0)
        self._prev_cpu = cur_cpu

        cur_net = net_bytes()
        net = {}
        for name, (rx, tx) in cur_net.items():
            prx, ptx = self._prev_net.get(name, (rx, tx))
            # max(...,0) guards against counter resets (iface bounced)
            net[name] = {"rx": max(rx - prx, 0) / dt, "tx": max(tx - ptx, 0) / dt}
        self._prev_net = cur_net
        self._prev_t = now

        # docker forks a process, so poll it less often than the cheap files
        self._tick_n += 1
        if self._tick_n % DOCKER_EVERY == 0:
            self._docker = docker_ps()

        mi = meminfo()
        mem_total = mi.get("MemTotal", 1)
        # MemAvailable is the kernel's "actually usable" estimate — it counts
        # reclaimable cache as free, unlike naive total-minus-free
        mem_used = mem_total - mi.get("MemAvailable", 0)

        temp_raw = read_file(f"{SYS}/class/thermal/thermal_zone0/temp", "0")  # millidegrees C
        freq_raw = read_file(
            f"{SYS}/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq", "0")  # kHz

        raw_disks = disks()
        # disk_min_gb post-filter: disks() already drops volumes < 1 GB;
        # this lets users raise the floor further without touching disks()
        if self._disk_min_gb > 1:
            min_bytes = self._disk_min_gb * (1 << 30)
            raw_disks = [d for d in raw_disks if d["total"] >= min_bytes]

        snap = {
            "t": time.time(),
            # the container's own hostname is a random ID, so prefer the
            # host's /etc/hostname when running containerised
            "host": (read_file(f"{HOST_ROOT}/etc/hostname") if HOST_ROOT else "")
                    or socket.gethostname(),
            "uptime": float(read_file(f"{PROC}/uptime", "0 0").split()[0]),
            "load": list(os.getloadavg()),
            "cpu": {"cores": cores,
                    "avg": round(sum(cores) / len(cores), 1) if cores else 0,
                    "freq_mhz": int(freq_raw) // 1000 if freq_raw.isdigit() else 0},
            "temp_c": int(temp_raw) / 1000 if temp_raw.isdigit() else None,
            "mem": {"total": mem_total, "used": mem_used,
                    "swap_total": mi.get("SwapTotal", 0),
                    "swap_used": mi.get("SwapTotal", 0) - mi.get("SwapFree", 0)},
            "disks": raw_disks,
            "net": net,
            "docker": self._docker,
        }
        with self.lock:
            self.snapshot = snap

        # threshold alerts → Discord; wrapped so an alert bug never kills the
        # sampler. config re-read per tick so threshold edits apply live.
        try:
            self._check_alerts(snap, load_config())
        except Exception as e:
            print(f"alert error: {e}", file=sys.stderr)

    def _check_alerts(self, snap, cfg):
        """Reduce the snapshot to one value per metric, then let step_alert
        decide what (if anything) to send. Disk uses the fullest volume."""
        al = cfg.get("alerts", {}) if isinstance(cfg, dict) else {}
        wh = al.get("webhook", {}) if isinstance(al, dict) else {}
        url = wh.get("discord_url", "")
        min_sev = wh.get("min_severity", "crit")
        # config "name" overrides the hostname as the label in every message
        label = (cfg.get("name") or snap.get("host", "")) if isinstance(cfg, dict) \
            else snap.get("host", "")

        # snapshot state before the loop so we can detect changes and persist
        prev_alert = dict(self._alert_state)
        prev_ctr   = dict(self._ctr_state)

        mem = snap["mem"]
        disk_pct = max((d["used"] / d["total"] * 100
                        for d in snap["disks"] if d["total"]), default=0.0)
        values = {
            "temp": snap["temp_c"],
            "cpu": snap["cpu"]["avg"],
            "mem": mem["used"] / mem["total"] * 100 if mem["total"] else 0.0,
            "disk": disk_pct,
        }
        for metric, value in values.items():
            if value is None:  # e.g. no thermal sensor
                continue
            th = al.get(metric, {}) if isinstance(al, dict) else {}
            sev = severity_of(value, th.get("warn"), th.get("crit"))
            event = step_alert(metric, sev, min_sev, self._alert_state)
            if not event:
                continue
            event.update(value=value, host=label,
                         warn=th.get("warn"), crit=th.get("crit"))
            if url:
                post_discord(url, format_alert(event))

        # container start/stop alerts. Diff every tick to keep the baseline
        # fresh; the state only changes when docker is actually repolled, so
        # this is quiet between polls. Only posts when enabled and a url is set.
        ctr_evs = container_events(snap.get("docker"), self._ctr_state)
        ctr_cfg = al.get("containers", {}) if isinstance(al, dict) else {}
        if url and ctr_cfg.get("enabled", True):
            for ev in ctr_evs:
                post_discord(url, format_container(ev, label))

        # persist state when anything changed (only on transitions, not every tick)
        if self._alert_state != prev_alert or self._ctr_state != prev_ctr:
            save_alert_state(self._alert_state, self._ctr_state)

    def get(self):
        with self.lock:
            return dict(self.snapshot)


# Seed before constructing the Sampler so load_config() in __init__ sees
# config.json on a fresh install (seed_data is called again in main() but
# that runs after the Sampler is already alive).
seed_data()
SAMPLER = Sampler()


# ------------------------------------------------------------------ server

def _rate_check(ip):
    """Return (allowed, retry_after_seconds). Lazy-evicts expired entries."""
    with _login_attempts_lock:
        count, until = _login_attempts.get(ip, (0, 0))
        if until and time.time() < until:
            return False, int(until - time.time())
        if until:  # lockout expired — clean up the entry
            del _login_attempts[ip]
        return True, 0


def _rate_fail(ip):
    """Record a failed attempt; apply lockout if threshold crossed."""
    with _login_attempts_lock:
        count, until = _login_attempts.get(ip, (0, 0))
        count += 1
        new_until = time.time() + RATE_LOCK if count >= RATE_MAX else 0
        _login_attempts[ip] = (count, new_until)


def _rate_clear(ip):
    """Clear the attempt counter after a successful auth."""
    with _login_attempts_lock:
        _login_attempts.pop(ip, None)

class Handler(BaseHTTPRequestHandler):
    """Routes — public: / (UI shell) · /api/health · /api/auth/status ·
    POST /api/setup|login|logout. Auth-gated: /api/stats · /api/links ·
    /api/categories · /api/config (GET+POST) · /events (SSE).

    ThreadingHTTPServer gives each request its own thread, which is what
    lets long-lived /events connections coexist with normal page loads.
    """
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):  # quiet
        pass

    def _send(self, code, body, ctype="text/plain; charset=utf-8", headers=()):
        data = body if isinstance(body, bytes) else body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for name, value in headers:
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(data)

    def _json(self, code, obj, headers=()):
        self._send(code, json.dumps(obj), "application/json", headers)

    def _body(self):
        """Parse a JSON request body, or None if absent/oversized/malformed."""
        try:
            n = int(self.headers.get("Content-Length", 0))
        except ValueError:
            return None
        if n <= 0 or n > 1_000_000:  # 1 MB ceiling — these are tiny config blobs
            return None
        try:
            return json.loads(self.rfile.read(n).decode())
        except (ValueError, UnicodeDecodeError):
            return None

    def _authed(self):
        """Username from a valid session cookie, or None."""
        raw = self.headers.get("Cookie")
        if not raw:
            return None
        try:
            jar = http.cookies.SimpleCookie(raw)
        except http.cookies.CookieError:
            return None
        morsel = jar.get(COOKIE_NAME)
        return check_session(morsel.value) if morsel else None

    def _cookie(self, token, max_age):
        # HttpOnly: JS can't read it (blunts XSS cookie theft). SameSite=Strict:
        # not sent on cross-site requests (CSRF defence). No Secure flag — the
        # dashboard commonly runs over plain HTTP on a LAN; a TLS reverse proxy
        # can add it. max_age=0 with an empty token clears the cookie (logout).
        return ("Set-Cookie",
                f"{COOKIE_NAME}={token}; Path=/; HttpOnly; SameSite=Strict; "
                f"Max-Age={max_age}")

    def do_GET(self):
        path = self.path.split("?")[0]
        # --- public: the page shell, the healthcheck, and auth state ---
        if path == "/":
            with open(os.path.join(BASE, "static", "index.html"), "rb") as f:
                self._send(200, f.read(), "text/html; charset=utf-8")
            return
        if path == "/api/health":          # liveness probe — no auth (the Docker
            self._json(200, {"ok": True})  # HEALTHCHECK can't log in)
            return
        if path == "/api/auth/status":
            self._json(200, {"configured": is_configured(),
                             "authed": bool(self._authed())})
            return
        # --- everything below requires a valid session ---
        if not self._authed():
            self._json(401, {"error": "auth required"})
            return
        if path == "/api/stats":
            self._json(200, SAMPLER.get())
        elif path == "/api/links":
            try:
                with open(data_path("links.json"), "rb") as f:
                    self._send(200, f.read(), "application/json")
            except OSError:
                self._json(200, [])
        elif path == "/api/categories":
            self._json(200, load_categories())
        elif path == "/api/config":
            # public_config() strips the webhook URL before it goes over the wire
            self._json(200, public_config(load_config()))
        elif path == "/events":
            self._sse()
        else:
            self._send(404, "not found")

    def do_POST(self):
        path = self.path.split("?")[0]
        # --- public auth endpoints ---
        if path == "/api/setup":
            return self._do_setup()
        if path == "/api/login":
            return self._do_login()
        if path == "/api/logout":
            return self._json(200, {"ok": True}, [self._cookie("", 0)])
        # --- writes require a valid session ---
        if not self._authed():
            return self._json(401, {"error": "auth required"})
        if path == "/api/auth/change-password":
            return self._do_change_password()
        body = self._body()
        if body is None:
            return self._json(400, {"error": "invalid JSON body"})
        if path == "/api/links":
            data, err = sanitize_links(body)
            fname = "links.json"
        elif path == "/api/categories":
            data, err = sanitize_categories(body)
            fname = "categories.json"
        elif path == "/api/config":
            data, err = sanitize_config(body)
            fname = "config.json"
        else:
            return self._send(404, "not found")
        if err:
            return self._json(400, {"error": err})
        write_json(fname, data)
        # echo the (secret-stripped) config back so the UI re-syncs; ok for rest
        if path == "/api/config":
            return self._json(200, public_config(load_config()))
        self._json(200, {"ok": True})

    def _do_setup(self):
        """First-run only: create the single admin account and log them in."""
        if is_configured():
            return self._json(409, {"error": "already configured"})
        ip = self.client_address[0]
        allowed, retry = _rate_check(ip)
        if not allowed:
            return self._json(429, {"error": f"Too many attempts. Try again in {retry}s."})
        body = self._body() or {}
        user = str(body.get("user", "")).strip()
        pw = str(body.get("password", ""))
        if not user or len(pw) < 8:
            _rate_fail(ip)
            return self._json(400, {"error": "username required; password "
                                             "must be at least 8 characters"})
        record = create_admin(user, pw)
        _rate_clear(ip)
        self._json(200, {"ok": True}, [self._cookie(make_session(record),
                                                    SESSION_TTL)])

    def _do_login(self):
        ip = self.client_address[0]
        allowed, retry = _rate_check(ip)
        if not allowed:
            return self._json(429, {"error": f"Too many attempts. Try again in {retry}s."})
        body = self._body() or {}
        record = load_auth()
        user = str(body.get("user", "")).strip()
        pw = str(body.get("password", ""))
        if record and user == record.get("user") and verify_password(pw, record):
            _rate_clear(ip)
            return self._json(200, {"ok": True},
                              [self._cookie(make_session(record), SESSION_TTL)])
        _rate_fail(ip)
        self._json(401, {"error": "invalid username or password"})

    def _do_change_password(self):
        """Change the admin password; rotates the session secret to invalidate
        all existing cookies. Issues a fresh cookie for the calling session."""
        body = self._body() or {}
        current_pw = str(body.get("current_password", ""))
        new_pw     = str(body.get("new_password", ""))
        record = load_auth()
        if not record or not verify_password(current_pw, record):
            return self._json(401, {"error": "current password incorrect"})
        if len(new_pw) < 8:
            return self._json(400, {"error": "new password must be at least 8 characters"})
        new_record = change_password(record, new_pw)
        self._json(200, {"ok": True},
                   [self._cookie(make_session(new_record), SESSION_TTL)])

    def _sse(self):
        """Server-Sent Events: push the snapshot every TICK seconds, forever.

        The wire format is just "data: <json>\\n\\n" per message — the
        browser's EventSource handles parsing and auto-reconnect natively,
        no websocket library needed on either end.
        """
        if not self._sse_admit():
            return  # over the cap — _sse_admit already sent the rejection

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        try:
            while True:
                payload = json.dumps(SAMPLER.get())
                self.wfile.write(f"data: {payload}\n\n".encode())
                self.wfile.flush()
                time.sleep(TICK)
        except (BrokenPipeError, ConnectionResetError):
            pass  # tab closed — thread ends quietly
        finally:
            with _sse_lock:
                global _sse_count
                _sse_count -= 1

    def _sse_admit(self):
        """Reserve a slot for a new SSE stream under the MAX_SSE cap.

        Returns True if admitted (count incremented — caller's finally decrements).
        Returns False if at capacity (sends 503 itself; caller must not decrement).
        """
        global _sse_count
        with _sse_lock:
            if _sse_count < MAX_SSE:
                _sse_count += 1
                return True
        self._send(503, "too many connections")
        return False


def main():
    # populate the writable data dir from the baked defaults before anything
    # reads it — this is what lets a fresh install boot with no config files
    seed_data()
    # wait for the first sample so the page never renders empty
    while not SAMPLER.get():
        time.sleep(0.1)
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"vitaldeck on http://0.0.0.0:{PORT}")
    srv.serve_forever()


if __name__ == "__main__":
    main()
