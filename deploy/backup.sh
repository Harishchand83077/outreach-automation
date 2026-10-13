#!/usr/bin/env bash
# Nightly database dump, kept 14 days (§19).
#
#   ./deploy/backup.sh                 # one dump now
#   crontab:  17 2 * * *  cd /opt/outreach && ./deploy/backup.sh >> /var/log/outreach-backup.log 2>&1
#
# ADR-001 put Postgres on this VM rather than on a managed service, which removed five problems
# and introduced exactly one: **the database dies with the box.** There is no point-in-time
# recovery here and no vendor holding a copy. These dumps are not belt-and-braces, they are the
# only copy -- and a copy on the same disk as the database is not a copy of anything, so the
# upload to S3 below is the step that actually makes this a backup.
#
# The upload uses the instance's IAM role (`outreach-ssm-role`), scoped to one bucket with
# `s3:PutObject`, `s3:GetObject` and `s3:ListBucket`. **No access key exists on this box** to
# be read out of a file or leaked in a log, and the role cannot touch any other bucket or
# delete what it has already written.
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
# Empty disables the upload, which is what a machine without the IAM role should do.
S3_BUCKET="${S3_BUCKET:-outreach-backups-918464541565}"
AWS_REGION="${AWS_REGION:-ap-southeast-2}"
COMPOSE=(docker compose --env-file "$ROOT/.env" -f "$ROOT/deploy/compose.prod.yml")

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

# The off-box copy. **This is the step that makes the previous forty lines a backup** -- until
# now every dump sat on the same EBS volume as the database it was protecting, so losing the
# instance lost both at once.
#
# A failed upload does not fail the script. The local dump has already been written and
# verified, and exiting non-zero here would turn "we could not reach S3 tonight" into a cron
# job that looks broken, which is how people start ignoring it. It is loud, and it is not fatal.
uploaded="no"
if [[ -n "$S3_BUCKET" ]]; then
	if command -v aws >/dev/null 2>&1; then
		echo "==> uploading to s3://$S3_BUCKET/"
		if aws s3 cp "$target" "s3://$S3_BUCKET/${target##*/}" 			--region "$AWS_REGION" --only-show-errors; then
			uploaded="yes"
			echo "==> uploaded ${target##*/}"
		else
			echo "WARNING: upload failed. The local dump is fine; this box still holds the" >&2
			echo "         only copy, so fix this before relying on it." >&2
		fi
	else
		echo "WARNING: the aws CLI is not installed, so nothing left this machine." >&2
	fi
else
	echo "==> S3_BUCKET is empty, so the dump stays on this machine only"
fi

cat <<NOTE

Done. Off-box copy: ${uploaded}.

Objects expire from the bucket after 30 days by a lifecycle rule, so it cannot grow without
bound; local dumps are kept ${KEEP_DAYS} days.

A restore drill is in docs/DEPLOY.md under "Restoring". Do it once, now, while nothing is
wrong — a backup nobody has ever restored is a hypothesis.
NOTE
