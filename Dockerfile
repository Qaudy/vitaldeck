FROM python:3.12-alpine

# docker-cli so the dash can show container status via the mounted socket;
# smartmontools (smartctl) powers the Disk Health panel. smartctl is harmless
# when the container has no device access — smart_data() just returns None and
# the panel hides, so this adds no requirement to the default deployment.
RUN apk add --no-cache docker-cli smartmontools

WORKDIR /app
COPY server.py ./
# baked-in seed defaults; seed_data() copies these into DATA_DIR on first run so
# a fresh install works with zero config files (they are NOT the live files).
COPY links.example.json config.example.json categories.example.json ./
COPY static ./static

# writable state (links/config/categories + the admin account) lives here, on a
# named volume so in-dashboard edits and the login survive container recreation.
RUN mkdir -p /app/data
ENV DATA_DIR=/app/data
VOLUME /app/data

ENV PORT=8800
EXPOSE 8800

# mark the container unhealthy if the server stops answering; pairs with
# `restart: unless-stopped` so a wedged process gets noticed in `docker ps`.
# /api/health is public (the gated /api/stats would 401 without a login).
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD wget -qO- "http://127.0.0.1:${PORT}/api/health" >/dev/null 2>&1 || exit 1

CMD ["python3", "server.py"]
