#!/usr/bin/env bash
# Deploy an immutable Git revision to the team server. Run on the server only.
set -euo pipefail

REPO_DIR="${REPO_DIR:-/opt/sl100-diagnosis}"
REF="${1:-origin/main}"
COMPOSE_FILE="$REPO_DIR/deploy/docker-compose.yml"

test -d "$REPO_DIR/.git" || { echo "Repository not found: $REPO_DIR" >&2; exit 1; }
test -f "$REPO_DIR/deploy/.env" || { echo "Missing deployment-only config: $REPO_DIR/deploy/.env" >&2; exit 1; }

cd "$REPO_DIR"
git fetch --prune --tags origin
git checkout --detach "$REF"
git rev-parse --verify HEAD
docker compose --env-file deploy/.env -f "$COMPOSE_FILE" up -d --build --remove-orphans
docker compose --env-file deploy/.env -f "$COMPOSE_FILE" ps
