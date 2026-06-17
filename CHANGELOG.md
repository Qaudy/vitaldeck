# Changelog

All notable changes to vitaldeck are documented here.

---

## [Unreleased]

### Added — Phase 4

- **Disk health (SMART) panel** — a new dashboard card showing per-drive SMART status:
  overall health (PASSED / FAILED), temperature (in your configured °C/°F unit), power-on
  age, and the main wear indicator (NVMe life-used %, or ATA reallocated-sector count). It
  reads real SMART data by shelling out to `smartctl`, polled on a slow ~60 s cadence so it
  never stalls the live `/proc` sampler. The whole card hides itself when SMART can't be
  read (no `smartctl`, no device access, or disabled in settings), so hosts that can't
  expose SMART look exactly as before. Toggle it in Settings → General.
- **Mobile-responsive layout** — a phone-tier stylesheet (≤ 560 px) tightens the header,
  collapses the settings modal's two-column rows, lets the fixed-width category dropdowns go
  full width, and enlarges tap targets. The dashboard is now usable on a phone; verified at
  360–414 px widths.

### Changed — Phase 4

- The Docker image now bundles `smartmontools`, and `docker-compose.yaml` grants the
  container `SYS_RAWIO` plus a read-only `/dev` mount so `smartctl` can query the drives.
  This is the only privilege the dashboard requests beyond its read-only host mounts; remove
  the capability and the `/dev` mount to opt out (the panel then hides). See the README
  Security section.

### Added — Phase 3

- **5 built-in colour presets** in Settings → Appearance: Aurora Dark (default), Synthwave,
  Ocean, Terminal, and Ember. Clicking a preset fills all colour pickers and previews live.
- **Manual container assignments** in Settings → Categories. A new "Manual assignments"
  section lists every running container with a category dropdown, letting you override the
  automatic path-rule grouping per container. Overrides take precedence over path rules and
  are persisted in `categories.json` as an `"overrides"` object alongside `"rules"`.
- **Telegram alerts** — add a bot token and chat ID in Settings → Alerts to receive the same
  breach/recovery/container messages on Telegram alongside (or instead of) Discord. Both
  platforms share the same minimum-severity setting.
- **Interface skip regex documentation** — a short help note below the regex field in
  Settings → General lists what the default pattern skips (loopback, docker bridges, veth pairs).
- **Default example links** updated to Google and the vitaldeck GitHub page.

### Changed — Phase 3

- **Categories are now name-first, not path-first.** Settings → Categories leads with a
  "Your categories" box where you create category names directly, then an "Assign
  containers" list lets you drop each container into a category from a dropdown — no more
  typing compose paths. The old path-based auto-assignment still exists under a collapsed
  "Advanced" section. `categories.json` gains a top-level `categories` name list; the
  legacy plain-array and `{rules, overrides}` formats are still read and migrated.

### Fixed — Phase 3

- **Dropdown readability** — `<select>` controls now use a solid dark background and
  explicitly styled `<option>`s, fixing the white box with invisible (until hovered)
  text seen on some browsers.
- **Select element styling** — `appearance: none` + custom SVG chevron so dropdowns match
  the dark theme on all browsers instead of rendering with OS-default chrome.
- **Cursor inconsistencies** — text inputs now show the text cursor; checkboxes and their
  wrapper labels show the pointer cursor.

### Added

- **Everything is now configurable from the dashboard** — no config files to edit manually.
- **Discord webhook URL** is now editable in Settings → Alerts. Previously it required
  editing `config.json` inside the data volume directly.
- **Display settings** in Settings → General:
  - Temperature unit (°C / °F) — affects the ring gauge label and sub-text.
  - Throttle temperature (°C) — the ring gauge's 100% point (default 85 °C for Raspberry Pi; tune for other hardware).
  - Disk warn % — the threshold at which disk bars turn amber, independent of the Discord alert threshold.
  - Tagline — the small text beside the logo (default: `HOMELAB`).
- **Server tuning settings** in Settings → General → Server settings (restart to apply):
  - Sample interval in seconds (replaces the hardcoded `TICK = 2.0`).
  - Minimum disk size to display in GB (replaces the hardcoded 1 GB filter).
  - Interface skip regex (replaces the hardcoded `IFACE_SKIP` pattern).
- **Password change** in Settings → General — change the admin password without touching
  any files. Changing the password rotates the session signing secret, immediately
  invalidating all other active sessions.
- **Login rate limiting** — 5 failed attempts locks the IP out for 5 minutes. Applies to
  both the login and the first-run setup endpoints.
- **Alert state persistence** — alert and container states are written to the data volume
  after each transition. A server restart no longer re-fires Discord alerts for thresholds
  that were already breached before the restart.
- **Stale data indicator** — if the live SSE feed goes quiet for more than 5 seconds, the
  header shows "last seen Xs ago". After 10 seconds the dashboard grid dims to 60% opacity
  so it's obvious the data is not live.
- **SSE exponential backoff** — reconnect delay doubles on each failure (3 s → 6 s →
  12 s → … → 60 s cap) rather than retrying at a fixed 3-second interval. Resets to 3 s
  on the first successful message after reconnection.

### Changed

- `public_config()` now returns the full config (including the webhook URL and tier-3
  server settings) to authenticated sessions, since all endpoints are behind the login gate.
- The General tab in Settings now covers branding, display preferences, server tuning, and
  password management — previously it only had the dashboard name field.
- The Alerts tab now includes a Discord webhook URL input — the old note directing users to
  edit `config.json` manually has been removed.
- `config.example.json` updated to include all new keys with their defaults.
- README rewritten to reflect the current feature set.

### Fixed

- Tier-3 config values (`tick_seconds`, `disk_min_gb`, `iface_skip`) are now applied before
  the first sample is taken. Previously, `seed_data()` ran after `Sampler()` was constructed,
  so on a fresh install these values were ignored until the next restart.
- `_login_attempts` dict is lazily evicted on access rather than growing without bound when
  many IPs fail authentication.

---

## [1.0.0] — Phase 1

Initial public release.

### Added

- Live dashboard over Server-Sent Events — CPU, temperature, memory, network, disk, Docker
  containers, quick links.
- Zero-dependency Python backend (`server.py`, pure stdlib).
- Single-file frontend (`static/index.html`, inline CSS + vanilla JS).
- Full in-dashboard settings: alert thresholds, quick links, container categories, colour theme.
- PBKDF2-SHA256 password auth with HMAC-signed session cookies.
- First-run setup wizard.
- Discord webhook alerts — edge-triggered per metric (fires once per episode, recovery on clear).
- Docker Compose setup with host networking, read-only mounts, and a named data volume.
- Optional Cloudflare tunnel service (off by default, behind a compose profile).
- Multi-arch Docker image published to `ghcr.io/qaudy/vitaldeck`.
- `HEALTHCHECK` on `/api/health`.
- SSE connection cap (`MAX_SSE = 32`) to limit DoS exposure.
- HTML escaping (`esc()` / `safeUrl()`) on all dynamic DOM content.
- Atomic JSON writes (`write-then-rename`) so a crash never leaves a half-written config.
