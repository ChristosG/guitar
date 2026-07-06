#!/usr/bin/env bash
set -euo pipefail
# Asserts postgres is up and the pgvector extension can be created.
docker compose exec -T postgres psql -U guitar -d guitar -c "CREATE EXTENSION IF NOT EXISTS vector;" \
  && docker compose exec -T postgres psql -U guitar -d guitar -c "SELECT '[1,2,3]'::vector;" \
  | grep -q '\[1,2,3\]'
echo "PGVECTOR_OK"
