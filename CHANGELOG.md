# Changelog

All notable changes to vitaldeck are documented here.

---

## [Unreleased]

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
