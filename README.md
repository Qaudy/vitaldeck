# vitaldeck

> **License: Personal Use Only — No Commercial Use**
> This software is free for personal, non-commercial homelab use.
> Selling, charging for, or profiting from this software in any form is strictly prohibited.
> See [LICENSE](LICENSE) for the full terms.

**A zero-dependency, single-file homelab dashboard for Linux hosts.**

vitaldeck reads `/proc` and `/sys` directly and shells out to `docker` for
container state, then streams a live JSON snapshot to your browser over
Server-Sent Events every couple of seconds. The backend is pure Python standard
library — nothing to `pip install` — and the entire UI is one `index.html` with
inline CSS and vanilla JS: no build step, no CDN, works fully offline.

Panels:

- **CPU** — live sparkline, per-core usage bars, and current clock speed
- **Temperature** — ring gauge (green → amber → red as it heats up)
- **Memory** — ring gauge of used RAM
- **Network** — per-interface rx/tx throughput sparklines
- **Disks** — usage bars for every real volume ≥ 1 GB
- **Docker** — container status chips, grouped into your own categories
- **Quick links** — a configurable grid of links to your services
- **Alerts** — optional Discord webhook notifications on threshold breaches and
  container start/stop

![screenshot](docs/screenshot.png)

> Drop your own screenshot at `docs/screenshot.png` and it will render here.

---

## Requirements

- **A Linux host.** Host networking is Linux-only — vitaldeck reports the host's
  real CPU, interfaces, and disks because the container shares the host network
  namespace. On Docker Desktop for Mac/Windows the stats reflect the Docker VM,
  not your physical machine.
- Either **Docker + Docker Compose**, or **Python 3.10+** for a bare-metal run.
- Read access to `/proc`, `/sys`, and (for the container panel) the Docker
  socket.

---

## Quick start

### Option A — No clone needed (easiest)

Create a folder anywhere on your Linux host, drop in three config files, and
point Docker Compose at the pre-built image:

```bash
mkdir vitaldeck && cd vitaldeck
cp /path/to/config.example.json  config.json   # or create from scratch
cp /path/to/links.example.json   links.json
cp /path/to/categories.example.json categories.json
```

Then create a `docker-compose.yaml` with the following contents and run it:

```yaml
services:
  vitaldeck:
    image: ghcr.io/qaudy/vitaldeck:latest
    container_name: vitaldeck
    restart: unless-stopped
    network_mode: host
    environment:
      HOST_PROC: /host/proc
      HOST_SYS: /host/sys
      HOST_ROOT: /host/root
    volumes:
      - /proc:/host/proc:ro
      - /sys:/host/sys:ro
      - /:/host/root:ro
      - /var/run/docker.sock:/var/run/docker.sock:ro
      - ./links.json:/app/links.json:ro
      - ./categories.json:/app/categories.json:ro
      - ./config.json:/app/config.json:ro
```

```bash
docker compose up -d
```

Then open **http://&lt;host-ip&gt;:8800**.

### Option B — Clone the repo

```bash
git clone https://github.com/Qaudy/vitaldeck.git && cd vitaldeck
cp config.example.json config.json
cp links.example.json links.json
cp categories.example.json categories.json
docker compose up -d
```

```bash
docker compose logs -f       # follow logs
docker compose down          # stop and remove
```

> To build the image locally from source instead of pulling it, edit
> `docker-compose.yaml` and swap `image:` for `build: .`.

The compose file mounts the host's `/proc`, `/sys`, `/` (all read-only) plus the
Docker socket, so the container reports **host** stats — not its own sandbox.

### Bare metal

```bash
python3 server.py            # http://localhost:8800
python3 server.py 9000       # custom port
```

No dependencies beyond the Python 3.10+ standard library.

---

## Configuration — `config.json`

```bash
cp config.example.json config.json
```

Edit it and refresh the page — `config.json` is re-read live on each request, so
there's no restart needed. **It is gitignored** (it can hold a secret webhook
URL) and must never be committed.

Full example:

```json
{
  "name": "",
  "alerts": {
    "temp": { "warn": 70, "crit": 85 },
    "cpu":  { "warn": 85, "crit": 95 },
    "mem":  { "warn": 80, "crit": 92 },
    "disk": { "warn": 80, "crit": 92 },
    "containers": { "enabled": true },
    "webhook": {
      "discord_url": "",
      "min_severity": "crit"
    }
  }
}
```

Every field:

| Field | Meaning |
|---|---|
| `name` | Dashboard title / brand, and the label used in alert messages. Empty = fall back to the host's hostname. |
| `alerts.temp.warn` / `.crit` | Temperature thresholds in **°C absolute**. |
| `alerts.cpu.warn` / `.crit` | CPU thresholds as a **percentage**. |
| `alerts.mem.warn` / `.crit` | Memory thresholds as a **percentage**. |
| `alerts.disk.warn` / `.crit` | Disk-usage thresholds as a **percentage**. |
| `alerts.containers.enabled` | `true`/`false` — toggle container start/stop notifications. |
| `alerts.webhook.discord_url` | Discord webhook URL (see below). |
| `alerts.webhook.min_severity` | `"warn"` or `"crit"` — the minimum severity that triggers a webhook post. Default `"crit"`. |

Set any individual threshold level to `null` to disable just that level.

---

## Configuration — Discord webhook (step by step)

1. In Discord, open **Server Settings → Integrations → Webhooks → New Webhook**.
2. Name it, pick the target channel, and click **Copy Webhook URL**.
3. Paste it into `config.json` as `alerts.webhook.discord_url`.
4. Set `min_severity` (`"crit"` is recommended to start, so you only get paged on
   serious breaches).
5. Refresh the dashboard. The next threshold breach — or container start/stop, if
   enabled — posts to your channel.

> **Security note:** the webhook URL is a *write-capability secret* — anyone who
> has it can post to your channel. It lives only in `config.json`, which is
> gitignored, so **never commit it**. The server strips the webhook URL out of
> the public `/api/config` endpoint automatically, so it never reaches the
> browser.

---

## Configuration — colors & theming

There is **no color option in `config.json`**. Theming is done by editing
`static/index.html` and rebuilding (`docker compose up -d --build`).

The palette lives in the `:root` block at the top of the file:

| Variable | Drives |
|---|---|
| `--bg` | Page background |
| `--card` / `--card-edge` | The frosted-glass cards |
| `--text` / `--dim` | Primary and muted text |
| `--cyan` / `--violet` / `--pink` | Accent gradient — logo, sparklines, bars |
| `--green` / `--amber` / `--red` | Gauge "fullness" ramp |
| `--mono` | Monospace font stack |

Two things are **not** driven by these variables and must be edited directly for
a full re-theme:

- The three aurora background blobs are hard-coded hex values in the
  `.aurora i:nth-child(...)` CSS rules.
- The sparkline line colors are hard-coded in the JS (`#38e1ff` for CPU; `#38e1ff`
  / `#ff5fa8` for network rx/tx).

The dashboard **name/brand** comes from `config.json` `name` at runtime — no
rebuild needed for that one.

---

## Configuration — other settings (tuning)

These are source-level knobs; most require a rebuild/restart.

| Setting | Where | Notes |
|---|---|---|
| **Port** | CLI arg → `PORT` env var → `8800` | In Docker (host networking, no port map) set the `PORT` env var in compose or the Dockerfile. |
| **Sample interval** | `TICK` in `server.py` | Default `2.0` seconds. |
| **Temperature sensor** | `server.py` reads `/sys/class/thermal/thermal_zone0/temp` | On non-Pi hardware you may need a different thermal zone. |
| **Temp gauge scale** | `static/index.html`, scaled to **85 °C** | 85 °C is the Raspberry Pi throttle point; change the scale for other hardware. |
| **Hidden interfaces** | `IFACE_SKIP` regex in `server.py` | Hides loopback, docker bridges, and veth pairs. |
| **Minimum disk size shown** | the `1 GB` filter in `disks()` in `server.py` | Smaller pseudo-volumes are skipped. |

---

## Quick links — `links.json`

```bash
cp links.example.json links.json
```

An array of `{ "name", "url", "desc", "icon" }` entries (`icon` is an emoji,
default 🔗):

```json
{ "name": "Router", "url": "http://192.168.1.1", "desc": "router admin", "icon": "📡" }
```

Bind-mounted into the container and re-read on page refresh — no rebuild.
Gitignored.

---

## Container grouping — `categories.json`

```bash
cp categories.example.json categories.json
```

An array of `{ "name", "path" }` rules:

```json
{ "name": "Media Stack", "path": "/home/user/containers/media" }
```

A container is grouped under a rule when its Docker Compose working directory
**equals or sits under** that path. Anything unmatched falls under "Other". New
services added beneath a configured path are picked up automatically. Like the
other config files, it's live-reloaded and gitignored.

---

## Deployment notes

- **`network_mode: host` is required** so `/proc/net/dev` sees the host's real
  interfaces (and so the dashboard is reachable on `:8800` without a port map).
- The host's `/proc`, `/sys`, `/`, and the Docker socket are mounted **read-only**.
- `restart: unless-stopped` brings the dashboard back after reboots.
- A `HEALTHCHECK` in the Dockerfile lets the runtime know the server is alive.

---

## Cloudflare tunnel (optional)

A `cloudflared` service ships in the compose file, gated behind a profile so it
is **off by default**.

```bash
cp .env.example .env          # paste your tunnel token into CLOUDFLARE_TUNNEL_TOKEN
docker compose --profile tunnel up -d
```

> **⚠️ Warning:** this exposes an **unauthenticated** dashboard to the public
> internet. Read the Security section first. The image tag is pinned — bump it to
> a current release from
> [Docker Hub](https://hub.docker.com/r/cloudflare/cloudflared/tags) as needed.

---

## Security

vitaldeck serves **raw host telemetry and container names with no
authentication.** Treat it accordingly:

- Keep it on a trusted LAN or a private overlay (e.g. Tailscale), **or** put
  authentication in front of any public tunnel.
- The **Docker socket is root-equivalent.** Mounting it `:ro` only protects the
  socket *file*, not the Docker API behind it — anyone who can reach the API can
  effectively control the host.

**Hardening checklist for deployers:**

- Front the dashboard with a reverse proxy that enforces authentication.
- Use a read-only Docker-socket proxy (e.g. Tecnativa's `docker-socket-proxy`)
  instead of mounting the raw socket.
- Optionally run the container as a non-root user that belongs to the correct
  `docker` group. (Not done by default: the container must read the socket, and a
  wrong GID silently breaks the container panel.)
- Cap concurrent SSE connections if the dashboard is exposed — the server streams
  one long-lived connection per open tab, which is a potential DoS surface on an
  untrusted network.

---

## Contributing

PRs welcome. A few conventions:

- The repo ships **templates** — `config.example.json`, `links.example.json`,
  `categories.example.json`, and `.env.example`. The real `config.json`,
  `links.json`, `categories.json`, and `.env` are **gitignored and must never be
  committed** (they hold secrets and personal paths).
- To run locally for testing: `python3 server.py` and open
  `http://localhost:8800`. No dependencies to install.
- The frontend is a single file with no build step — edit `static/index.html`
  directly. All values coming from config or the host are HTML-escaped via the
  `esc()` / `safeUrl()` helpers before they touch `innerHTML`; keep new dynamic
  output behind them.

---

## License

vitaldeck is released under a **Personal Use Only** license. See [LICENSE](LICENSE) for the full terms.

The short version:

- ✅ **Use it** on your own homelab, Raspberry Pi, or personal server — completely free.
- ✅ **Modify it** for your own needs.
- ✅ **Share it** with others, as long as it remains free and this license is included.
- ❌ **Do not sell it**, charge for it, bundle it into a paid product, or profit from it in any way.

This is a personal project shared with the community. It is not for commercial distribution.

---

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| **Containers panel says docker unavailable** | The Docker socket isn't mounted, or `docker` isn't installed/running on the host. |
| **No temperature shown** | This hardware has no `/sys/class/thermal/thermal_zone0` — point the reader at a different thermal zone in `server.py`. |
| **Can't change the port in Docker** | Host networking has no port map; set the `PORT` env var instead of mapping ports. |
| **Brand flashes the default name first** | Expected — `config.json` is fetched after the first paint, so the title updates a moment later. |
