#!/usr/bin/env bash
# Nightly database dump, kept 14 days (§19).
#
#   ./deploy/backup.sh                 # one dump now
#   crontab:  17 2 * * *  cd /opt/outreach && ./deploy/backup.sh >> /var/log/outreach-backup.log 2>&1
#
# ADR-001 put Postgres on this VM rather than on a managed service, which removed five problems
# and introduced exactly one: **the database dies with the box.** There is no point-in-time
# recovery here and no vendor holding a copy. These dumps are not belt-and-braces, they are the
# only copy, so the weekly off-box step at the bottom is the part that actually matters.
#
# The dump runs inside the Compose network, so it works even though Postgres publishes no port
# to the host — which is the configuration that keeps the database off the internet.
#
# `pg_dump` is run with `--no-password`: the password comes from the container's environment and
# is never put on a command line, where it would be visible in `ps` to every user on the box.

set -euo pipefail

cd "$(dirname "$0")/.."
ROOT="$(pwd)"

BACKUP_DIR="${BACKUP_DIR:-$ROOT/backups}"
KEEP_DAYS="${KEEP_DAYS:-14}"
COMPOSE=(docker compose --project-directory "$ROOT" --env-file "$ROOT/.env" -f "$ROOT/deploy/compose.prod.yml")

[[ -f "$ROOT/.env" ]] || { echo "ERROR: no .env in $ROOT" >&2; exit 1; }

mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"

stamp="$(date -u +%Y%m%dT%H%M%SZ)"
target="$BACKUP_DIR/outreach-$stamp.sql.gz"

# `--clean --if-exists` so the dump can be restored over an existing database without a manual
# drop first, which is the state you are in at 3am when you need it.
#
# Written to a temporary name and renamed only on success: a half-written file with the right
# name is a backup that looks fine until the day it is needed.
echo "==> dumping to $target"
"${COMPOSE[@]}" exec -T postgres sh -c \
	'exec pg_dump --no-password --clean --if-exists -U "$POSTGRES_USER" -d "$POSTGRES_DB"' \
	| gzip -9 > "$target.part"
mv "$target.part" "$target"
chmod 600 "$target"

size="$(wc -c < "$target")"
# A dump this small means pg_dump wrote an error page or an empty schema. Better to fail the
# backup loudly than to keep fourteen days of nothing.
if (( size < 2048 )); then
	echo "ERROR: dump is only ${size} bytes; keeping it for inspection but something is wrong" >&2
	exit 1
fi
echo "==> wrote ${size} bytes"

echo "==> removing dumps older than ${KEEP_DAYS} days"
find "$BACKUP_DIR" -name 'outreach-*.sql.gz' -type f -mtime "+${KEEP_DAYS}" -print -delete

# Verify the newest dump is readable, rather than assuming. gzip -t catches the truncation that
# a full disk produces, which is the most likely way these files go bad.
gzip -t "$target" && echo "==> ${target##*/} verified readable"

cat <<'NOTE'

Done. The weekly off-box copy is manual and is the part that matters:

    scp -r <vm>:/opt/outreach/backups/ ~/outreach-backups/

A restore drill is in docs/DEPLOY.md under "Restoring". Do it once, now, while nothing is
wrong — a backup nobody has ever restored is a hypothesis.
NOTE
