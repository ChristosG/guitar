#!/usr/bin/env bash
set -euo pipefail
docker network inspect platform-net >/dev/null 2>&1 || docker network create platform-net
docker compose up -d --build
echo "waiting for services…"; sleep 6
# Each curl is its own statement so a failure is fatal under `set -e`.
# (A `curl … && echo OK` chain would swallow a failed curl and still exit 0,
#  making the smoke unable to ever signal that a service is down.)
curl -sf http://localhost:8791/health/live >/dev/null
echo "api live: OK"
curl -sf http://localhost:8790/el >/dev/null
echo "web el:   OK"
echo "ready: $(curl -s http://localhost:8791/health/ready)"
