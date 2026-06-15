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
- **Login + in-dashboard settings** — a first-run setup wizard creates an admin
  account; links, alert thresholds, container categories, and a full colour theme
  are all edited in the browser and persisted (no config files to touch)

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

No config files to prepare. Create a folder anywhere on your Linux host, drop in
this `docker-compose.yaml`, and start it:

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
      DATA_DIR: /app/data
    volumes:
      - /proc:/host/proc:ro
      - /sys:/host/sys:ro
      - /:/host/root:ro
      - /var/run/docker.sock:/var/run/docker.sock:ro
      - vitaldeck-data:/app/data      # settings + your login persist here

volumes:
  vitaldeck-data:
```

```bash
docker compose up -d
```

Then open **http://&lt;host-ip&gt;:8800**. On first launch you'll be asked to
**create an admin account** (setup wizard); after that you log in, and
everything — links, alert thresholds, container categories, and colours — is
edited right in the dashboard under the **⚙ settings** button. No files to
touch.

### Option B — Clone the repo

```bash
git clone https://github.com/Qaudy/vitaldeck.git && cd vitaldeck
docker compose up -d
```

```bash
docker compose logs -f       # follow logs
docker compose down          # stop and remove
```

> To build the image locally from source instead of pulling it, edit
> `docker-compose.yaml` and swap `image:` for `build: .`.

The compose file mounts the host's `/proc`, `/sys`, `/` (all read-only) plus the
Docker socket, so the container reports **host** stats — not its own sandbox. The
`vitaldeck-data` named volume holds all your settings and the admin account, so
they survive `docker compose up -d --force-recreate` and image updates.

### Bare metal

```bash
python3 server.py            # http://localhost:8800
python3 server.py 9000       # custom port
```

No dependencies beyond the Python 3.10+ standard library.

---

## Configuration — in the dashboard

Everything is edited under the **⚙ settings** button (top-right) and saved to the
`vitaldeck-data` volume — no files, no restart. The panel has five tabs:

| Tab | What you set |
|---|---|
| **General** | Dashboard name / brand (the label in alert messages too; blank = hostname). |
| **Alerts** | `warn`/`crit` thresholds for temp (°C), CPU/memory/disk (%); container start-stop toggle; webhook minimum severity. Leave a threshold blank to disable that level. |
| **Links** | The quick-links grid — name, URL, description, emoji icon. Add/remove rows. |
| **Categories** | Container grouping rules — a display name mapped to a host path. |
| **Appearance** | The full colour theme — see below. |

Under the hood these are stored as `config.json`, `links.json`, and
`categories.json` inside the data volume. You *can* edit those files directly
(e.g. `docker compose exec vitaldeck vi /app/data/config.json`) but the dashboard
is the intended path.

### Discord webhook

The webhook URL is the one setting **not** editable in the dashboard — it's a
write-capability secret (anyone with it can post to your channel), and the
dashboard has no per-user trust, so exposing it in the UI would let anyone who
can open the page read it. Set it directly in the data volume:

```bash
docker compose exec vitaldeck vi /app/data/config.json
# set alerts.webhook.discord_url to your "https://discord.com/api/webhooks/…" URL
```

To get the URL: in Discord, **Server Settings → Integrations → Webhooks → New
Webhook → Copy Webhook URL**. Set `alerts.webhook.min_severity` (`"warn"` or
`"crit"`) in the Alerts tab. The next threshold breach — or container start/stop,
if enabled — posts to your channel.

> The server strips the webhook URL out of the `/api/config` response, so it
> never reaches the browser; only its `min_severity` is exposed.

---

## Configuration — colors & theming

Open **⚙ settings → Appearance** and use the colour pickers. Changes preview
live; **Save changes** persists them, **Reset to defaults** restores the shipped
palette. Closing without saving reverts the preview.

The theme covers every coloured element:

| Picker | Drives |
|---|---|
| `bg` | Page background |
| `text` / `dim` | Primary and muted text |
| `cyan` / `violet` / `pink` | Accent gradient — logo, bars |
| `green` / `amber` / `red` | Gauge "fullness" ramp |
| `aurora1` / `aurora2` / `aurora3` | The three drifting background blobs |
| `spark_cpu` / `spark_tx` | The sparkline line colours |

Themes are stored as hex colours under `theme` in the data volume's
`config.json` and validated server-side. The frosted-glass card translucency and
the monospace font stay fixed. The dashboard **name/brand** is set in the General
tab.

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

## Quick links & container grouping

Both are edited in **⚙ settings** (the **Links** and **Categories** tabs) and
persist to the data volume.

- **Links** — each entry is a name, URL, description, and emoji icon (default
  🔗). URLs are scheme-checked (only `http(s)`/`ssh`/relative allowed).
- **Categories** — each rule maps a display name to a host path. A container is
  grouped under a rule when its Docker Compose working directory **equals or sits
  under** that path; anything unmatched falls under "Other". New services added
  beneath a configured path are picked up automatically.

---

## Deployment notes

- **`network_mode: host` is required** so `/proc/net/dev` sees the host's real
  interfaces (and so the dashboard is reachable on `:8800` without a port map).
- The host's `/proc`, `/sys`, `/`, and the Docker socket are mounted **read-only**.
- The `vitaldeck-data` named volume holds your settings + admin account; back it
  up to keep them. Delete it to reset to a fresh setup wizard.
- `restart: unless-stopped` brings the dashboard back after reboots.
- A `HEALTHCHECK` hits the public `/api/health` endpoint so the runtime knows the
  server is alive.

---

## Cloudflare tunnel (optional)

A `cloudflared` service ships in the compose file, gated behind a profile so it
is **off by default**.

```bash
cp .env.example .env          # paste your tunnel token into CLOUDFLARE_TUNNEL_TOKEN
docker compose --profile tunnel up -d
```

> **⚠️ Warning:** even with the login, exposing the dashboard to the public
> internet widens its attack surface. Read the Security section first. The image
> tag is pinned — bump it to a current release from
> [Docker Hub](https://hub.docker.com/r/cloudflare/cloudflared/tags) as needed.

---

## Security

vitaldeck has a built-in login: a first-run wizard creates one admin account
(salted PBKDF2 hash stored in the data volume), and all telemetry and settings
endpoints require a signed-cookie session. That's a real gate — but understand
its limits:

- The auth is a small, hand-rolled stdlib implementation (to keep the
  zero-dependency promise). It's appropriate for a trusted LAN or a private
  overlay (e.g. Tailscale). For **direct internet exposure**, still put a
  battle-tested reverse proxy with its own auth in front.
- The **Docker socket is root-equivalent.** Mounting it `:ro` only protects the
  socket *file*, not the Docker API behind it — anyone who reaches the API (i.e.
  anyone who gets a session) can effectively control the host.
- There's no rate-limiting on login attempts; rely on the network boundary or a
  proxy for brute-force protection on exposed deployments.

**Hardening checklist for deployers:**

- Front the dashboard with a reverse proxy (TLS + its own auth) for anything
  public; the dashboard sets no `Secure` cookie flag itself since it commonly
  runs over plain HTTP on a LAN.
- Use a read-only Docker-socket proxy (e.g. Tecnativa's `docker-socket-proxy`)
  instead of mounting the raw socket.
- Optionally run the container as a non-root user that belongs to the correct
  `docker` group. (Not done by default: the container must read the socket, and a
  wrong GID silently breaks the container panel.)
- The server caps concurrent SSE streams (`MAX_SSE` in `server.py`) to blunt a
  connection-flood DoS.

---

## Contributing

PRs welcome. A few conventions:

- The repo ships **seed templates** — `config.example.json`, `links.example.json`,
  `categories.example.json` — which the server copies into the data dir on first
  run. Live runtime files (and the admin `auth.json`) live in the data volume,
  never in the repo. `.env` (tunnel token only) stays gitignored.
- To run locally for testing: `python3 server.py` then open
  `http://localhost:8800` (writes to `./data/`). No dependencies to install.
- The frontend is a single file with no build step — edit `static/index.html`
  directly. All values coming from config or the host are HTML-escaped via the
  `esc()` / `safeUrl()` helpers before they touch `innerHTML`; keep new dynamic
  output behind them. New write endpoints must re-validate input server-side
  (see `sanitize_*` in `server.py`).

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
| **Forgot the admin password** | Delete the data volume (`docker compose down && docker volume rm <project>_vitaldeck-data`) to reset to the setup wizard — note this clears all settings too. Or `docker compose exec vitaldeck rm /app/data/auth.json` and restart to re-run setup while keeping settings. |
| **Setup wizard reappears after recreate** | The `vitaldeck-data` volume isn't persisting — check the `volumes:` block is present and you're not running with `--volumes`/an anonymous mount. |
| **Logged out after an image update** | Only if the data volume was recreated — sessions are signed by a secret stored in that volume. |
