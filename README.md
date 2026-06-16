# vitaldeck

> **License: Personal Use Only — No Commercial Use**
> This software is free for personal, non-commercial homelab use.
> Selling, charging for, or profiting from this software in any form is strictly prohibited.
> See [LICENSE](LICENSE) for the full terms.

**A zero-dependency homelab dashboard for Linux hosts.**

vitaldeck reads `/proc` and `/sys` directly and shells out to `docker` for
container state, then streams a live JSON snapshot to your browser over
Server-Sent Events every couple of seconds. The backend is pure Python standard
library — nothing to `pip install` — and the entire UI is one `index.html` with
inline CSS and vanilla JS: no build step, no CDN, works fully offline.

Panels:

- **CPU** — live sparkline, per-core usage bars, and current clock speed
- **Temperature** — ring gauge (green → amber → red), configurable unit (°C/°F) and throttle point
- **Memory** — ring gauge of used RAM
- **Network** — per-interface rx/tx throughput sparklines
- **Disks** — usage bars for every real volume above your configured size threshold
- **Docker** — container status chips, grouped into your own categories
- **Quick links** — a configurable grid of links to your services
- **Alerts** — Discord webhook notifications on threshold breaches and container start/stop
- **Full in-dashboard settings** — every knob, including the webhook URL, is editable in the browser

![screenshot](docs/screenshot.png)

> Drop your own screenshot at `docs/screenshot.png` and it will render here.

---

## Requirements

- **A Linux host.** Host networking is Linux-only — vitaldeck reports the host's
  real CPU, interfaces, and disks because the container shares the host network
  namespace. On Docker Desktop for Mac/Windows the stats reflect the Docker VM,
  not your physical machine.
- Either **Docker + Docker Compose**, or **Python 3.10+** for a bare-metal run.
- Read access to `/proc`, `/sys`, and (for the container panel) the Docker socket.

---

## Quick start

### Option A — No clone needed (easiest)

Create a folder anywhere on your Linux host, drop in this `docker-compose.yaml`, and start it:

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
      - vitaldeck-data:/app/data

volumes:
  vitaldeck-data:
```

```bash
docker compose up -d
```

Open **http://&lt;host-ip&gt;:8800**. On first launch you'll be asked to create an
admin account. After that, everything is configured right inside the dashboard
under the **⚙ settings** button — no files to touch, no restarts needed for
most changes.

### Option B — Clone the repo

```bash
git clone https://github.com/Qaudy/vitaldeck.git && cd vitaldeck
docker compose up -d
```

```bash
docker compose logs -f       # follow logs
docker compose down          # stop and remove
```

> To build the image locally instead of pulling it, edit `docker-compose.yaml`
> and swap `image:` for `build: .`.

### Bare metal

```bash
python3 server.py            # http://localhost:8800
python3 server.py 9000       # custom port
```

No dependencies beyond the Python 3.10+ standard library.

---

## Settings

Everything is edited in **⚙ settings** (top-right) and persisted to the data volume.

### General

| Setting | What it does |
|---|---|
| Dashboard name | Label used in the header, browser tab, and Discord alert messages. Blank = hostname. |
| Tagline | The small text beside the logo (default: `HOMELAB`). |
| Temperature unit | `°C` or `°F` — changes the ring gauge label and sub-text. |
| Throttle temp (°C) | The ring gauge's 100% point. Default 85 °C (Raspberry Pi throttle). Tune for your hardware. |
| Disk warn % | The disk bars turn amber above this percentage. Independent from the Discord alert threshold. |

**Server settings** (restart container to apply):

| Setting | What it does |
|---|---|
| Sample interval (s) | How often the backend reads `/proc` and pushes a snapshot. Default 2 s. |
| Hide disks smaller than (GB) | Volumes below this size are hidden from the Storage panel. Default 1 GB. |
| Interface skip regex | Interfaces matching this pattern are hidden from the Network panel. |

### Alerts

| Field | What it does |
|---|---|
| Temp / CPU / Mem / Disk warn & crit | Thresholds for each metric. Leave blank to disable a level. |
| Container notifications | Toggle start/stop alerts on or off. |
| Webhook minimum severity | Only post at `warn` or only at `crit` — applies to all platforms. |
| Discord webhook URL | Paste your `https://discord.com/api/webhooks/…` URL here. |
| Telegram bot token | Token from @BotFather (format: `1234567890:ABCDEF…`). |
| Telegram chat ID | Your chat or group ID. Message the bot then call `getUpdates` to find it. |

**Discord:** **Server Settings → Integrations → Webhooks → New Webhook → Copy Webhook URL**.

**Telegram:** Message [@BotFather](https://t.me/BotFather) → `/newbot` → copy the token. Send any message to your new bot, then open `https://api.telegram.org/bot{TOKEN}/getUpdates` in a browser to find your `chat.id`.

Both platforms fire on the same events; leaving a platform's fields blank disables it independently.

### Links

Add, edit, or remove entries in the quick-links grid. Each entry has a name,
URL, description, and emoji icon (default 🔗). URLs are scheme-checked —
only `http(s)`, `ssh`, and relative paths are accepted.

### Categories

Containers are grouped in two ways — both configured here:

**Path rules** — map a display name to a host path. Any container whose Docker
Compose working directory equals or sits under that path is automatically placed
under that category. Anything unmatched falls under "Other".

**Manual assignments** — override or extend path rules per container. The
Categories tab shows every currently-running container with a dropdown; pick a
category name (sourced from your path rules) or leave it on **— auto —** to
let path matching decide. Manual assignments take precedence over path rules.

**Interface skip regex** — in **General → Server settings**, this pattern hides
interfaces from the Network panel. The default skips:
- `lo` — loopback
- `docker\d+` — Docker bridge interfaces
- `br-*` — bridge interfaces
- `veth*` — virtual ethernet pairs

### Appearance

Five built-in colour presets (**Aurora Dark**, **Synthwave**, **Ocean**,
**Terminal**, **Ember**) are available at the top of the tab — clicking one
fills all colour pickers and previews live. You can then fine-tune individual
swatches. **Save** persists the result; **Reset** restores the Aurora Dark
defaults.

---

## Password management

To change the admin password, open **⚙ settings → General**, scroll to
**Change password**, and fill in your current and new passwords. The session
is re-issued immediately so you stay logged in; all other open tabs or devices
are logged out on their next request.

To fully reset the account (e.g. forgotten password):

```bash
docker compose exec vitaldeck rm /app/data/auth.json
docker compose restart vitaldeck
```

The setup wizard will reappear on the next page load. All other settings
(links, categories, config) are preserved.

---

## Deployment notes

- **`network_mode: host` is required** so `/proc/net/dev` sees the host's real
  interfaces and so the dashboard is reachable on `:8800` without a port map.
- The host's `/proc`, `/sys`, `/`, and the Docker socket are all mounted
  **read-only**.
- The `vitaldeck-data` named volume holds your settings and admin account.
  Back it up to preserve them; delete it to reset everything to the setup wizard.
- `restart: unless-stopped` brings the dashboard back after reboots automatically.
- A `HEALTHCHECK` polls `/api/health` so the runtime reports the container as
  unhealthy if the server wedges.
- The `PORT` env var (or a CLI arg to `server.py`) overrides the default `8800`.

---

## Cloudflare tunnel (optional)

A `cloudflared` service ships in the compose file, gated behind a profile so
it is **off by default**.

```bash
cp .env.example .env          # paste your tunnel token into CLOUDFLARE_TUNNEL_TOKEN
docker compose --profile tunnel up -d
```

> **⚠️ Warning:** exposing the dashboard to the public internet widens its
> attack surface even with the login gate. Read the Security section below
> before enabling the tunnel.

---

## Security

vitaldeck has a built-in login gate: a first-run wizard creates one admin
account (PBKDF2-SHA256 hash stored in the data volume), and all data and
settings endpoints require a valid signed-cookie session. Understand the limits:

- The auth is a hand-rolled stdlib implementation to preserve the
  zero-dependency promise. It is appropriate for a trusted LAN or private
  overlay network (e.g. Tailscale). For **direct internet exposure**, still
  put a battle-tested reverse proxy with its own auth in front.
- Login attempts are rate-limited: 5 failures triggers a 5-minute lockout per
  IP. If vitaldeck is behind a reverse proxy, the IP is always the proxy's
  address — network-level protection matters more in that case.
- The **Docker socket is root-equivalent.** Mounting it `:ro` only protects
  the socket file, not the Docker API behind it. Anyone with a valid session
  can read container metadata.
- Sessions are stateless HMAC-signed cookies. Changing your password rotates
  the signing secret, immediately invalidating all other sessions.
- The server caps concurrent SSE connections (`MAX_SSE = 32`) to blunt a
  connection-flood DoS.

**Hardening checklist:**

- Front the dashboard with a reverse proxy (TLS + its own auth layer) for
  anything public. vitaldeck sets no `Secure` cookie flag since it commonly
  runs over plain HTTP on a LAN.
- Use a read-only Docker-socket proxy (e.g. Tecnativa's `docker-socket-proxy`)
  instead of mounting the raw socket — lets the Containers panel work while
  blocking all management operations.
- Optionally run the container as a non-root user that belongs to the `docker`
  group. (Not done by default since an incorrect GID silently breaks the
  Containers panel.)

---

## Contributing

PRs welcome. A few conventions:

- The repo ships seed templates — `config.example.json`, `links.example.json`,
  `categories.example.json` — which the server copies into the data directory
  on first run. Live runtime files and `auth.json` live in the data volume,
  never in the repo. `.env` (tunnel token only) stays gitignored.
- To run locally: `python3 server.py` then open `http://localhost:8800`
  (writes to `./data/`). No dependencies to install.
- The frontend is a single file with no build step — edit `static/index.html`
  directly. All values from config or the host are HTML-escaped via `esc()` /
  `safeUrl()` before touching `innerHTML`; keep new dynamic output behind them.
  New write endpoints must re-validate input server-side (see `sanitize_*` in
  `server.py`).

---

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| **Containers panel says docker unavailable** | The Docker socket isn't mounted, or `docker` isn't installed/running on the host. |
| **No temperature shown** | This hardware has no `/sys/class/thermal/thermal_zone0` sensor. Try a different thermal zone in `server.py`. |
| **Can't change the port in Docker** | Host networking has no port map; set the `PORT` env var in the compose file instead. |
| **Forgot the admin password** | Open Settings → General → Change password if you're still logged in. Otherwise: `docker compose exec vitaldeck rm /app/data/auth.json && docker compose restart vitaldeck` — this re-runs setup while keeping all other settings. |
| **Setup wizard reappears after recreate** | The `vitaldeck-data` volume isn't persisting — check the `volumes:` block is present and you're not running with `--volumes` or an anonymous mount. |
| **Logged out on all devices unexpectedly** | Password was changed, which rotates the session signing secret. Log back in. |
| **Network panel shows wrong interfaces** | Adjust the **Interface skip regex** in Settings → General → Server settings (restart required). |
| **Disk panel shows too many or too few volumes** | Raise **Hide disks smaller than** in Settings → General → Server settings (restart required). |

---

## License

vitaldeck is released under a **Personal Use Only** license. See [LICENSE](LICENSE) for the full terms.

- ✅ Use it on your own homelab, Raspberry Pi, or personal server — free.
- ✅ Modify it for your own needs.
- ✅ Share it with others, as long as it remains free and this license is included.
- ❌ Do not sell it, charge for it, bundle it into a paid product, or profit from it in any way.
