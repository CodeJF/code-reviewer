#!/usr/bin/env bash
# Pull, back up, migrate and deploy a branch on the production server.
set -euo pipefail

REPO_DIR="${REPO_DIR:-/opt/sl100-diagnosis}"
BRANCH="${1:-main}"
COMPOSE_FILE="$REPO_DIR/deploy/docker-compose.yml"
ENV_FILE="$REPO_DIR/deploy/.env"
REVISION_LOG="${REVISION_LOG:-/var/lib/sl100-deploy/revisions.log}"

[[ "$BRANCH" =~ ^[A-Za-z0-9._/-]+$ ]] || { echo "Invalid branch: $BRANCH" >&2; exit 1; }
test -d "$REPO_DIR/.git" || { echo "Repository not found: $REPO_DIR" >&2; exit 1; }
test -f "$ENV_FILE" || { echo "Missing deployment config: $ENV_FILE" >&2; exit 1; }

set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

cd "$REPO_DIR"
test -z "$(git status --porcelain)" || { echo "Git worktree is not clean; deployment stopped." >&2; exit 1; }

OLD_SHA="$(git rev-parse HEAD)"
git fetch origin "$BRANCH"
if git show-ref --verify --quiet "refs/heads/$BRANCH"; then
  git switch "$BRANCH"
else
  git switch --create "$BRANCH" --track "origin/$BRANCH"
fi
git pull --ff-only origin "$BRANCH"
NEW_SHA="$(git rev-parse HEAD)"
echo "Deploying $OLD_SHA -> $NEW_SHA"

compose() {
  APP_VERSION="$1" docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" "${@:2}"
}

rollback() {
  exit_code=$?
  if [ "$exit_code" -eq 0 ]; then
    return
  fi
  echo "Deployment failed; attempting application image rollback to $OLD_SHA" >&2
  if docker image inspect "sl100-team:$OLD_SHA" >/dev/null 2>&1; then
    compose "$OLD_SHA" up -d --no-build api worker reconciler maintenance prometheus grafana caddy || true
  fi
  exit "$exit_code"
}
trap rollback EXIT

compose "$NEW_SHA" up -d postgres redis
if compose "$NEW_SHA" ps --status running --services | grep -qx postgres; then
  compose "$NEW_SHA" run --rm -e BACKUP_ONCE=1 backup
fi
compose "$NEW_SHA" build api worker reconciler maintenance
compose "$NEW_SHA" run --rm api uv run alembic upgrade head
compose "$NEW_SHA" up -d --no-build --remove-orphans

for _ in $(seq 1 30); do
  if curl --fail --silent --show-error "https://${APP_DOMAIN:-ops.example.invalid}/api/ready" >/dev/null; then
    break
  fi
  sleep 2
done
curl --fail --silent --show-error "https://${APP_DOMAIN:-ops.example.invalid}/api/ready"
running_services="$(compose "$NEW_SHA" ps --status running --services)"
for service in api worker reconciler maintenance postgres redis backup prometheus grafana caddy; do
  grep -qx "$service" <<<"$running_services" || { echo "Service is not running: $service" >&2; exit 1; }
done
compose "$NEW_SHA" ps

mkdir -p "$(dirname "$REVISION_LOG")"
printf '%s %s %s %s\n' "$(date -u +%FT%TZ)" "$OLD_SHA" "$NEW_SHA" "$BRANCH" >>"$REVISION_LOG"
trap - EXIT
echo "Deployment completed: $NEW_SHA"
