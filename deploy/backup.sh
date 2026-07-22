#!/bin/sh
set -eu

backup_once() {
  timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
  target="/backups/sl100_team_${timestamp}.dump"
  pg_dump --format=custom --no-owner --no-privileges --file="$target"
  pg_restore --list "$target" >/dev/null
  find /backups -type f -name 'sl100_team_*.dump' -mtime "+${BACKUP_RETENTION_DAYS:-14}" -delete
  echo "Backup verified: $target"
}

backup_once
if [ "${BACKUP_ONCE:-0}" = "1" ]; then
  exit 0
fi
while true; do
  sleep 86400
  backup_once
done
