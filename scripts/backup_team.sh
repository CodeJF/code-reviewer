#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="${REPO_DIR:-/opt/sl100-diagnosis}"
cd "$REPO_DIR"
docker compose --env-file deploy/.env -f deploy/docker-compose.yml run --rm -e BACKUP_ONCE=1 backup
