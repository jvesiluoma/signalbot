#!/usr/bin/env bash
#
# backup-db.sh — dump the signalbot MySQL database out of its container.
#
# Runs mysqldump *inside* the db container so the password never touches the
# host or the process list (it reuses the container's own $MYSQL_PASSWORD via
# MYSQL_PWD), and streams a timestamped gzip to the host.
#
# Usage:
#   scripts/backup-db.sh                    # full backup to ./db-backups/
#   OUTDIR=/mnt/backups scripts/backup-db.sh
#   RETAIN_DAYS=14 scripts/backup-db.sh     # prune dumps older than 14 days
#
#   # Lightweight backup — skip the multi-GB BLOB table *data* (screenshots /
#   # attachment bytes) but keep their schema. ~50 MB, seconds instead of
#   # minutes. Good for frequent/cron backups of messages + metadata:
#   IGNORE_TABLES="attachments" scripts/backup-db.sh
#
# Env overrides: CONTAINER, DB_NAME, DB_USER, OUTDIR, RETAIN_DAYS, IGNORE_TABLES
#   IGNORE_TABLES — comma/space separated table names whose DATA is skipped
#                   (schema is still included so a restore recreates them empty)
#
set -euo pipefail

CONTAINER="${CONTAINER:-signalbot-mysql-1}"
DB_NAME="${DB_NAME:-messages_db}"
DB_USER="${DB_USER:-signalbot}"
OUTDIR="${OUTDIR:-./db-backups}"
RETAIN_DAYS="${RETAIN_DAYS:-0}"          # 0 = keep everything
IGNORE_TABLES="${IGNORE_TABLES:-}"       # e.g. "attachments,page_snapshots"

if ! docker inspect -f '{{.State.Running}}' "$CONTAINER" >/dev/null 2>&1; then
    echo "error: container '$CONTAINER' is not running" >&2
    exit 1
fi

# --single-transaction: consistent InnoDB snapshot without locking writers.
# --no-tablespaces:     avoids needing the PROCESS privilege (signalbot user).
DUMP_OPTS="--single-transaction --quick --routines --triggers --events --no-tablespaces"
SCHEMA_ONLY=""        # tables whose data we skip; dumped --no-data afterwards
for t in ${IGNORE_TABLES//,/ }; do
    DUMP_OPTS="$DUMP_OPTS --ignore-table=$DB_NAME.$t"
    SCHEMA_ONLY="$SCHEMA_ONLY $t"
done

mkdir -p "$OUTDIR"
stamp="$(date +%Y%m%d-%H%M%S)"
suffix=""; [ -n "$SCHEMA_ONLY" ] && suffix="-lite"
out="$OUTDIR/${DB_NAME}-${stamp}${suffix}.sql.gz"
tmp="$out.partial"

# Pass options through to the in-container shell as positional args so nothing
# needs quoting gymnastics; MYSQL_PWD keeps the secret in the container.
docker exec "$CONTAINER" sh -c '
    set -e
    MYSQL_PWD="$MYSQL_PASSWORD"; export MYSQL_PWD
    mysqldump -u"$0" $1 "$2"
    if [ -n "$3" ]; then
        mysqldump -u"$0" --no-data --no-tablespaces "$2" $3
    fi
  ' "$DB_USER" "$DUMP_OPTS" "$DB_NAME" "$SCHEMA_ONLY" \
  | gzip -c > "$tmp"

# A truncated/failed dump must not masquerade as a good backup.
if ! gzip -t "$tmp" 2>/dev/null || [ ! -s "$tmp" ]; then
    rm -f "$tmp"
    echo "error: dump failed or produced an empty/corrupt file" >&2
    exit 1
fi
mv "$tmp" "$out"
echo "backup OK: $out ($(du -h "$out" | cut -f1))"
[ -n "$SCHEMA_ONLY" ] && echo "note: data skipped for:${SCHEMA_ONLY} (schema kept)"

if [ "$RETAIN_DAYS" -gt 0 ]; then
    find "$OUTDIR" -maxdepth 1 -name "${DB_NAME}-*.sql.gz" \
        -type f -mtime "+$RETAIN_DAYS" -print -delete
fi
