# The claude-bridge container: Node + the Claude Code CLI + `bridge.py`, and nothing else.
#
# A DEDICATED container, not a package added to apps/api. That separation is the
# security story, and it is the whole reason this is safe to do at all:
#
#   - The API container runs web-facing code — it parses uploads, serves a chat box,
#     and drives an agent loop over text a user typed. It is the thing an attacker
#     would aim at. It gets NO credentials.
#   - This container's only job is to exec `claude -p` with every tool disabled. It
#     runs no app code, has no database, and publishes no port to the host (see
#     docker-compose.yml — it is reachable only on the internal `appnet`).
#
# `~/.claude` is bind-mounted in from the host at RUN time, never baked in. Nothing
# secret is in this image.

FROM node:22-slim

# python3 for bridge.py (stdlib only — no pip, no venv, nothing to drift).
# ca-certificates so the CLI can do TLS to api.anthropic.com.
RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# PINNED to the version on the host (`claude --version` → 2.1.209). Not `latest`,
# deliberately: this container and your host `claude` share one credentials file, and
# an image that silently upgrades itself underneath a shared credential store is a
# thing you want to opt into, not discover. Bump it here when you bump the host.
RUN npm install -g @anthropic-ai/claude-code@2.1.209 \
 && npm cache clean --force

# uid 1000 — the same as the human on the host (`id -u` → 1000), which is what keeps
# files the CLI writes into the mounted ~/.claude owned by YOU and not by root. Get
# this wrong and the first OAuth refresh inside the container leaves a root-owned
# .credentials.json that your own terminal `claude` can no longer write.
# `node:22-slim` ships exactly this user, already at 1000.
USER node
ENV HOME=/home/node

COPY bridge.py /app/bridge.py

# HOST/PORT/TOKEN/CONCURRENCY all come from the environment (docker-compose.yml).
CMD ["python3", "/app/bridge.py"]
