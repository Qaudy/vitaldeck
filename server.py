#!/usr/bin/env python3
"""vitaldeck — zero-dependency homelab dashboard for Raspberry Pi.

Pure stdlib: reads /proc and /sys directly, shells out to docker for
container state, and streams everything to the browser over SSE.

    python3 server.py            # http://0.0.0.0:8800
    python3 server.py 9000       # custom port
"""

import copy
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE = os.path.dirname(os.path.abspath(__file__))
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

# Built-in alert thresholds. config.json (bind-mounted, hot-editable) overlays
# these; missing keys fall back here. temp is °C absolute; cpu/mem/disk are %.
DEFAULTS = {
    # label shown in alert messages ("… on <name>"); empty -> use the hostname
    "name": "",
    "alerts": {
        "temp": {"warn": 70, "crit": 85},
        "cpu":  {"warn": 85, "crit": 95},
        "mem":  {"warn": 80, "crit": 92},
        "disk": {"warn": 80, "crit": 92},
        # notify when a container starts/stops; toggle off without losing config
        "containers": {"enabled": True},
        "webhook": {"discord_url": "", "min_severity": "crit"},
    },
}

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
        with open(os.path.join(BASE, "categories.json")) as f:
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


def load_config():
    """Merged config: config.json overlaid on DEFAULTS, read fresh each call.

    Same rail as links.json/categories.json — re-reading per use means a user
    can retune a threshold or paste a webhook URL and have it apply on the next
    tick, no restart. `alerts` is deep-merged one level so a one-line override
    like {"alerts": {"temp": {"crit": 80}}} keeps every other default intact.
    """
    cfg = copy.deepcopy(DEFAULTS)
    try:
        with open(os.path.join(BASE, "config.json")) as f:
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
    """Strip the merged config down to what's safe to hand a browser.

    /api/config is reachable by anyone who can load the dashboard — and via
    the cloudflared tunnel that can be the public internet. The full config
    holds the Discord webhook URL, which is a write-capability secret, so the
    raw config must never leave the server. Return a NEW dict containing only
    the fields the frontend actually consumes; never mutate `cfg`.

    Exposes the display name plus the alert thresholds (handy for the UI to
    colour gauges by the configured warn/crit one day) — but everything under
    alerts.webhook is dropped, since that's where the Discord secret lives.
    The return is an allow-list at the top level — only "name" and "alerts"
    are ever emitted, so a new top-level secret in config can't leak by
    default — and within "alerts" the webhook block is dropped explicitly.
    """
    alerts = cfg.get("alerts", {}) if isinstance(cfg, dict) else {}
    safe_alerts = {k: v for k, v in alerts.items() if k != "webhook"}
    return {"name": cfg.get("name", "") if isinstance(cfg, dict) else "",
            "alerts": safe_alerts}


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
        self.snapshot = {}
        self.lock = threading.Lock()
        self._prev_cpu = cpu_times()
        self._prev_net = net_bytes()
        self._prev_t = time.monotonic()
        self._docker = docker_ps()
        self._tick_n = 0
        # per-metric edge state for threshold alerts ("armed"|"alerting"),
        # carried across ticks so a breach fires once, not every 2 seconds
        self._alert_state = {}
        # baseline of container running-state, seeded from the first poll so a
        # fresh start doesn't alert for everything that's already up
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
            "disks": disks(),
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

    def get(self):
        with self.lock:
            return dict(self.snapshot)


SAMPLER = Sampler()


# ------------------------------------------------------------------ server

class Handler(BaseHTTPRequestHandler):
    """Routes: / (UI) · /api/stats · /api/links · /events (SSE stream).

    ThreadingHTTPServer gives each request its own thread, which is what
    lets long-lived /events connections coexist with normal page loads.
    """
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):  # quiet
        pass

    def _send(self, code, body, ctype="text/plain; charset=utf-8"):
        data = body if isinstance(body, bytes) else body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/":
            with open(os.path.join(BASE, "static", "index.html"), "rb") as f:
                self._send(200, f.read(), "text/html; charset=utf-8")
        elif path == "/api/stats":
            self._send(200, json.dumps(SAMPLER.get()), "application/json")
        elif path == "/api/links":
            # read from disk on every request so links.json edits apply on
            # page refresh without a restart
            try:
                with open(os.path.join(BASE, "links.json"), "rb") as f:
                    self._send(200, f.read(), "application/json")
            except OSError:
                self._send(200, "[]", "application/json")
        elif path == "/api/config":
            # merged config (defaults + config.json), re-read per request so
            # the browser picks up threshold edits on refresh. public_config()
            # strips secrets (the webhook URL) before it goes over the wire.
            self._send(200, json.dumps(public_config(load_config())),
                       "application/json")
        elif path == "/events":
            self._sse()
        else:
            self._send(404, "not found")

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
    # wait for the first sample so the page never renders empty
    while not SAMPLER.get():
        time.sleep(0.1)
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"vitaldeck on http://0.0.0.0:{PORT}")
    srv.serve_forever()


if __name__ == "__main__":
    main()
