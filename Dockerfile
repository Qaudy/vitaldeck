FROM python:3.12-alpine

# docker-cli so the dash can show container status via the mounted socket
RUN apk add --no-cache docker-cli

WORKDIR /app
COPY server.py ./
# generic defaults baked in; users override with a links.json bind mount
COPY links.example.json ./links.json
# default alert thresholds; users override with a config.json bind mount
COPY config.example.json ./config.json
COPY static ./static

ENV PORT=8800
EXPOSE 8800

# mark the container unhealthy if the server stops answering; pairs with
# `restart: unless-stopped` so a wedged process gets noticed in `docker ps`
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD wget -qO- "http://127.0.0.1:${PORT}/api/stats" >/dev/null 2>&1 || exit 1

CMD ["python3", "server.py"]
