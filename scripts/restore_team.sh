#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="${REPO_DIR:-/opt/sl100-diagnosis}"
BACKUP_NAME="${1:-}"
test -n "$BACKUP_NAME" || { echo "Usage: $0 sl100_team_YYYYMMDDTHHMMSSZ.dump" >&2; exit 1; }
[[ "$BACKUP_NAME" =~ ^sl100_team_[0-9TZ]+\.dump$ ]] || { echo "Invalid backup name" >&2; exit 1; }
cd "$REPO_DIR"

compose=(docker compose --env-file deploy/.env -f deploy/docker-compose.yml)
"${compose[@]}" run --rm --entrypoint sh backup -c "test -f /backups/$BACKUP_NAME && pg_restore --list /backups/$BACKUP_NAME >/dev/null"
"${compose[@]}" stop api worker reconciler maintenance
"${compose[@]}" exec -T postgres dropdb --username sl100 --if-exists sl100_team
"${compose[@]}" exec -T postgres createdb --username sl100 sl100_team
"${compose[@]}" run --rm --entrypoint sh backup -c "pg_restore --clean --if-exists --no-owner --no-privileges --dbname=sl100_team /backups/$BACKUP_NAME"
"${compose[@]}" start api
"${compose[@]}" exec -T api uv run alembic upgrade head
"${compose[@]}" start worker reconciler maintenance
