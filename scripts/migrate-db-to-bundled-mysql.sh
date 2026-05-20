#!/usr/bin/env bash
#
# migrate-db-to-bundled-mysql.sh
#
# Copy the database the app is currently pointed at (DB_HOST in .env — typically
# an external MySQL) into the bundled `mysql` Compose container (profile: db),
# which is wired to the same DB_USER / DB_PASSWORD / DB_NAME from .env.
#
# What it does, asking you to confirm at every step:
#   1. start the bundled `mysql` container
#   2. mysqldump the source database to a .sql file
#   3. load that .sql file into the bundled container's database
#   4. verify (table count + messages row count)
#   (5. optional: rewrite .env so the app uses the bundled container)
#
# Usage:
#   ./scripts/migrate-db-to-bundled-mysql.sh                 # interactive
#   ./scripts/migrate-db-to-bundled-mysql.sh --yes           # don't prompt
#
# Options:
#   --yes, -y          assume "yes" to every confirmation (non-interactive)
#   --from-host HOST   dump from HOST instead of DB_HOST in .env
#   --out FILE         write the dump to FILE (default: ./signalbot-db-export-<ts>.sql)
#   --dump-file FILE   skip mysqldump; load this existing .sql file instead
#   --skip-dump        only start the bundled container (schema.sql seeds it); no data copy
#   --cleanup          delete the generated dump file after a successful import
#   --update-env       after import, set DB_HOST=mysql and add `db` to COMPOSE_PROFILES
#                      in .env (a .env.bak backup is made; you'll be asked first)
#   -h | --help        show this header
#
# Notes:
#   * Run it from anywhere; it cd's to the repo root (where .env / docker-compose.yml live).
#   * mysqldump is used if it's on PATH; otherwise it runs inside the mysql:8.0 image
#     (`docker run --network host`), so a loopback DB_HOST works when the DB is on this host.
#   * Nothing here ever writes to the source database — it's read-only at the source.
#   * If the source MySQL refuses the connection, dump it where it does allow you
#     (e.g. `docker exec <its-mysql-container> mysqldump ...`) and re-run with --dump-file.
#
set -euo pipefail

# ── locate the repo root ──────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_DIR"
[[ -f .env ]]               || { echo "error: .env not found in $REPO_DIR" >&2; exit 1; }
[[ -f docker-compose.yml ]] || { echo "error: docker-compose.yml not found in $REPO_DIR" >&2; exit 1; }

# ── args ──────────────────────────────────────────────────────────────────────
ASSUME_YES=0; FROM_HOST=""; OUT_FILE=""; DUMP_FILE=""; SKIP_DUMP=0; CLEANUP=0; UPDATE_ENV=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    -y|--yes)     ASSUME_YES=1; shift ;;
    --from-host)  FROM_HOST="${2:?--from-host needs a value}"; shift 2 ;;
    --out)        OUT_FILE="${2:?--out needs a path}"; shift 2 ;;
    --dump-file)  DUMP_FILE="${2:?--dump-file needs a path}"; shift 2 ;;
    --skip-dump)  SKIP_DUMP=1; shift ;;
    --cleanup)    CLEANUP=1; shift ;;
    --update-env) UPDATE_ENV=1; shift ;;
    -h|--help)    awk 'NR>1 && /^#/{sub(/^#[[:space:]]?/,""); print; next} NR>1{exit}' "$0"; exit 0 ;;
    *) echo "unknown argument: $1  (try --help)" >&2; exit 2 ;;
  esac
done

# ── read a value from .env (last one wins; strips surrounding quotes / CR) ─────
env_get() {
  local v
  v="$(grep -E "^[[:space:]]*$1=" .env 2>/dev/null | tail -n1 | cut -d= -f2- || true)"
  v="${v%$'\r'}"
  v="${v#\"}"; v="${v%\"}"
  v="${v#\'}"; v="${v%\'}"
  printf '%s' "$v"
}

DB_USER="$(env_get DB_USER)";     DB_USER="${DB_USER:-signalbot}"
DB_PASSWORD="$(env_get DB_PASSWORD)"
DB_NAME="$(env_get DB_NAME)";     DB_NAME="${DB_NAME:-messages_db}"
DB_PORT="$(env_get DB_PORT)";     DB_PORT="${DB_PORT:-3306}"
SRC_HOST="${FROM_HOST:-$(env_get DB_HOST)}"
MAX_PACKET="256M"

# ── docker compose CLI ────────────────────────────────────────────────────────
if docker compose version >/dev/null 2>&1;        then DC=(docker compose)
elif command -v docker-compose >/dev/null 2>&1;   then DC=(docker-compose)
else echo "error: neither 'docker compose' nor 'docker-compose' is available" >&2; exit 1; fi
DCP=("${DC[@]}" --profile db)

# ── interactive confirmation ──────────────────────────────────────────────────
STEP=0
_read_yn() {  # echoes the user's answer (lowercased); reads from the terminal
  local ans=""
  if [[ -r /dev/tty ]]; then printf '  Proceed? [Y/n] ' >/dev/tty; read -r ans </dev/tty || ans=""
  elif [[ -t 0 ]];      then printf '  Proceed? [Y/n] ';        read -r ans       || ans=""
  else echo "error: no terminal to confirm on — re-run with --yes for non-interactive mode" >&2; exit 1; fi
  printf '%s' "$ans" | tr '[:upper:]' '[:lower:]'
}
step_header() {  # $1 total-steps, $2 title
  STEP=$((STEP + 1))
  echo
  echo "────────────────────────────────────────────────────────────────────"
  echo " Step ${STEP}/${1} — ${2}"
  echo "────────────────────────────────────────────────────────────────────"
}
confirm() {  # call after printing the step details; aborts the script on "no"
  if [[ "$ASSUME_YES" -eq 1 ]]; then echo "  -> auto-confirmed (--yes)"; return 0; fi
  case "$(_read_yn)" in ""|y|yes) return 0 ;; *) echo "  Aborted by user. Nothing further was done."; exit 1 ;; esac
}
confirm_optional() {  # like confirm() but returns 1 (skip) instead of aborting
  if [[ "$ASSUME_YES" -eq 1 ]]; then echo "  -> auto-confirmed (--yes)"; return 0; fi
  case "$(_read_yn)" in ""|y|yes) return 0 ;; *) echo "  Skipped."; return 1 ;; esac
}

# ── how many steps will we run? ───────────────────────────────────────────────
# (start) [+ dump] + import + verify + point-.env-at-bundled  — except --skip-dump exits after step 1.
if   [[ "$SKIP_DUMP" -eq 1 ]]; then TOTAL=1
elif [[ -n "$DUMP_FILE" ]];    then TOTAL=4            # start, import, verify, .env
else                                TOTAL=5; fi       # start, dump, import, verify, .env

# ── helpers used by the steps ─────────────────────────────────────────────────
mysql_in_container() {  # run `mysql ...` inside the bundled container; stdin is forwarded
  "${DCP[@]}" exec -T -e MYSQL_PWD="$DB_PASSWORD" mysql \
    mysql --max-allowed-packet="$MAX_PACKET" -u"$DB_USER" "$@"
}
print_use_bundled() {
  cat <<EOF
  To make the app use it: in .env set
      DB_HOST=mysql
      COMPOSE_PROFILES=db,...        (keep your other profiles, just add 'db')
      DB_ROOT_PASSWORD=...           (the bundled container defaults it to 'changeme')
  then:  ${DC[*]} up -d
  (The app reaches it over the 'signalnet' Compose network — no host port needed.)
EOF
}

# ── intro ─────────────────────────────────────────────────────────────────────
echo "================================================================"
echo " Migrate database -> bundled 'mysql' Compose container"
echo "================================================================"
echo " Repo:        $REPO_DIR"
echo " Credentials: from .env  (user='${DB_USER}', database='${DB_NAME}')"
if [[ "$SKIP_DUMP" -eq 1 ]]; then
  echo " Action:      just create/start the bundled container (no data copy)"
elif [[ -n "$DUMP_FILE" ]]; then
  echo " Source:      existing dump file  '$DUMP_FILE'"
else
  echo " Source:      ${DB_USER}@${SRC_HOST:-<unset>}:${DB_PORT}/${DB_NAME}   (DB_HOST from .env)"
fi
echo " Steps:       $TOTAL (you'll be asked to confirm each one)"
[[ "$ASSUME_YES" -eq 1 ]] && echo " (--yes: every step will be auto-confirmed)"

# ── Step 1: start the bundled mysql container ─────────────────────────────────
step_header "$TOTAL" "Start the bundled 'mysql' container"
echo "  Command: ${DCP[*]} up -d mysql      (then wait until it answers)"
echo "  Effect:  creates database '${DB_NAME}' + user '${DB_USER}' and runs schema.sql"
echo "           on first init; reuses the existing container/volume otherwise."
confirm
echo "  Starting..."
"${DCP[@]}" up -d --wait mysql 2>/dev/null || "${DCP[@]}" up -d mysql
ready=0
for _ in $(seq 1 90); do
  if "${DCP[@]}" exec -T mysql mysqladmin ping -h 127.0.0.1 --silent >/dev/null 2>&1; then ready=1; break; fi
  sleep 2
done
[[ "$ready" -eq 1 ]] || { echo "  error: bundled mysql did not become ready in time" >&2; exit 1; }
echo "  OK — bundled mysql is up and accepting connections."

if [[ "$SKIP_DUMP" -eq 1 ]]; then
  echo
  echo "Done (--skip-dump): the bundled container is ready, seeded from schema.sql."
  print_use_bundled
  exit 0
fi

# ── Step 2: dump the source database to a file ────────────────────────────────
TMP_DUMP=""
if [[ -n "$DUMP_FILE" ]]; then
  [[ -f "$DUMP_FILE" ]] || { echo "error: --dump-file '$DUMP_FILE' not found" >&2; exit 1; }
  echo
  echo "  (Using existing dump file '$DUMP_FILE' — skipping the mysqldump step.)"
else
  case "$SRC_HOST" in
    ""|mysql)
      cat >&2 <<EOF

error: the source host resolves to '${SRC_HOST:-<unset>}' — that's the bundled
container itself (or unset), so there's nothing to copy from. Use:
  --from-host <ip-or-name>   to dump from the old external server
  --dump-file <path>         to load a dump you already have
  --skip-dump                to just create the empty container
EOF
      exit 1 ;;
    127.0.0.1|localhost|::1)
      : ;;  # ok — only works if mysqldump runs on the DB host itself; we'll note it below
  esac

  if [[ -n "$OUT_FILE" ]]; then DUMP_FILE="$OUT_FILE"; else
    TMP_DUMP="${REPO_DIR}/signalbot-db-export-$(date +%Y%m%d-%H%M%S).sql"; DUMP_FILE="$TMP_DUMP"
  fi

  step_header "$TOTAL" "Dump the source database to a file"
  echo "  Source:  ${DB_USER}@${SRC_HOST}:${DB_PORT}/${DB_NAME}   (read-only)"
  echo "  Output:  ${DUMP_FILE}"
  if command -v mysqldump >/dev/null 2>&1; then
    echo "  Command: MYSQL_PWD=*** mysqldump --single-transaction --routines --triggers"
    echo "           --events --no-tablespaces --add-drop-table --skip-lock-tables"
    echo "           --max-allowed-packet=${MAX_PACKET} -h ${SRC_HOST} -P ${DB_PORT} -u ${DB_USER} ${DB_NAME} > the file"
  else
    echo "  (mysqldump not on PATH — will run it inside the mysql:8.0 image, --network host)"
    echo "  Command: docker run --rm --network host -e MYSQL_PWD=*** mysql:8.0 mysqldump <same flags> > the file"
  fi
  case "$SRC_HOST" in 127.0.0.1|localhost|::1)
    echo "  Note:    source is a loopback address — this only works when run on the DB host." ;;
  esac
  confirm

  ERR_LOG="$(mktemp)"
  DUMP_ARGS=(--single-transaction --routines --triggers --events --no-tablespaces
             --add-drop-table --skip-lock-tables --max-allowed-packet="$MAX_PACKET"
             -h "$SRC_HOST" -P "$DB_PORT" -u "$DB_USER" "$DB_NAME")
  run_dump() {  # $@ = extra flags; uses MYSQL_PWD; writes SQL to stdout
    if command -v mysqldump >/dev/null 2>&1; then
      MYSQL_PWD="$DB_PASSWORD" mysqldump "$@" "${DUMP_ARGS[@]}"
    else
      docker run --rm --network host -e MYSQL_PWD="$DB_PASSWORD" mysql:8.0 mysqldump "$@" "${DUMP_ARGS[@]}"
    fi
  }
  echo "  Dumping..."
  if ! run_dump > "$DUMP_FILE" 2>"$ERR_LOG"; then
    if grep -qiE 'column.?statistics' "$ERR_LOG"; then
      echo "  (older source server — retrying with --column-statistics=0)"
      run_dump --column-statistics=0 > "$DUMP_FILE" 2>"$ERR_LOG" || { cat "$ERR_LOG" >&2; rm -f "$ERR_LOG" "$DUMP_FILE"; exit 1; }
    else
      echo "  --- mysqldump failed ---" >&2; cat "$ERR_LOG" >&2
      cat >&2 <<EOF
  ---
  The source MySQL refused the dump. If that's an "Access denied" error, it
  doesn't grant '${DB_USER}' from this machine. Fix the grant on it, e.g.:
      CREATE USER '${DB_USER}'@'%' IDENTIFIED BY '<your password>';
      GRANT ALL PRIVILEGES ON \`${DB_NAME}\`.* TO '${DB_USER}'@'%'; FLUSH PRIVILEGES;
  …or produce the dump where it IS allowed (on the DB host itself, or
  'docker exec <its-mysql-container> mysqldump -u${DB_USER} -p ${DB_NAME}') and re-run:
      $0 --dump-file your-dump.sql
EOF
      rm -f "$ERR_LOG" "$DUMP_FILE"; exit 1
    fi
  fi
  rm -f "$ERR_LOG"
  echo "  OK — wrote $(du -h "$DUMP_FILE" | cut -f1) to ${DUMP_FILE}"
fi

# ── Step 3: load the dump into the bundled container ──────────────────────────
step_header "$TOTAL" "Load the dump into the bundled container's database"
echo "  File:    ${DUMP_FILE}  ($(du -h "$DUMP_FILE" | cut -f1))"
echo "  Target:  bundled 'mysql' container -> database '${DB_NAME}' (user '${DB_USER}')"
echo "  Command: ${DCP[*]} exec -T -e MYSQL_PWD=*** mysql  mysql ${DB_NAME}   < the file"
echo "  Effect:  the dump's DROP/CREATE TABLE statements replace the schema.sql-seeded"
echo "           tables with the source data. The source DB is not touched."
confirm
echo "  Importing... (large dumps with screenshots/attachments can take a while)"
mysql_in_container "$DB_NAME" < "$DUMP_FILE"
echo "  OK — import complete."

# ── Step 4: verify ───────────────────────────────────────────────────────────
step_header "$TOTAL" "Verify the bundled database"
echo "  Command: SELECT counts from information_schema / messages in the bundled container"
confirm
TBL_COUNT="$(mysql_in_container -N -B -e "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='${DB_NAME}';" "$DB_NAME" 2>/dev/null | tr -d '[:space:]' || true)"
MSG_COUNT="$(mysql_in_container -N -B -e "SELECT COUNT(*) FROM messages;" "$DB_NAME" 2>/dev/null | tr -d '[:space:]' || true)"
echo "  tables in ${DB_NAME}: ${TBL_COUNT:-?}"
if [[ -n "$MSG_COUNT" ]]; then echo "  rows in 'messages':   ${MSG_COUNT}"
else echo "  (no 'messages' table yet — fine if the source was empty)"; fi

# ── temp-dump cleanup ─────────────────────────────────────────────────────────
if [[ -n "$TMP_DUMP" ]]; then
  if [[ "$CLEANUP" -eq 1 ]]; then
    rm -f "$TMP_DUMP"
    echo
    echo "Removed the temporary dump $TMP_DUMP (--cleanup)."
  else
    echo
    echo "Dump kept at: $TMP_DUMP   (pass --cleanup to auto-delete it next time)"
  fi
fi

do_env_update() {
  cp -f .env .env.bak
  if grep -qE '^[[:space:]]*DB_HOST=' .env; then
    sed -i.tmp -E 's|^[[:space:]]*DB_HOST=.*|DB_HOST=mysql|' .env && rm -f .env.tmp
  else
    printf '\nDB_HOST=mysql\n' >> .env
  fi
  if grep -qE '^[[:space:]]*COMPOSE_PROFILES=' .env; then
    cur="$(env_get COMPOSE_PROFILES)"
    case ",${cur}," in
      *",db,"*) : ;;                                                   # already there
      ,,|,) sed -i.tmp -E "s|^[[:space:]]*COMPOSE_PROFILES=.*|COMPOSE_PROFILES=db|" .env && rm -f .env.tmp ;;
      *)    sed -i.tmp -E "s|^[[:space:]]*COMPOSE_PROFILES=.*|COMPOSE_PROFILES=db,${cur}|" .env && rm -f .env.tmp ;;
    esac
  else
    # Only enable the `db` profile — the bundled signal/telegram/whatsapp services
    # are opt-in (and the bundled signal-api would clash with an already-running
    # one on port 8080). Add them yourself if you want them.
    printf 'COMPOSE_PROFILES=db\n' >> .env
  fi
  echo "  OK — .env updated: DB_HOST=mysql, COMPOSE_PROFILES includes 'db' (backup: .env.bak)."
}

# ── Step N (final): point .env at the bundled container ──────────────────────
step_header "$TOTAL" "Point .env at the bundled container (so the app uses it)"
CUR_DB_HOST="$(env_get DB_HOST)"
if [[ "$CUR_DB_HOST" == "mysql" ]]; then
  echo "  .env already has DB_HOST=mysql — nothing to change."
  echo "  (Make sure COMPOSE_PROFILES includes 'db' so the container is started.)"
  ENV_OK=1
else
  echo "  Current .env has DB_HOST=${CUR_DB_HOST:-<unset>}, which is NOT the bundled"
  echo "  container — the app would keep using that and ignore the data just imported."
  echo "  Change: DB_HOST=mysql ; ensure COMPOSE_PROFILES contains 'db'. (Backup -> .env.bak)"
  echo "  (Do NOT add 'signal' to COMPOSE_PROFILES if a 'signal-api' container already"
  echo "   runs on this host — it would collide on port 8080; keep SIGNAL_API_BASE as-is.)"
  if [[ "$UPDATE_ENV" -eq 1 ]]; then echo "  -> --update-env: applying."; do_env_update; ENV_OK=1
  elif confirm_optional;          then do_env_update; ENV_OK=1
  else ENV_OK=0; fi
fi

echo
echo "================================================================"
echo " Done. The bundled 'mysql' container holds a copy of '${DB_NAME}'."
echo "================================================================"
if [[ "${ENV_OK:-0}" -eq 1 ]]; then
  echo " Apply it now with:   ${DC[*]} up -d"
  echo "                      ${DC[*]} logs -f signalbot"
else
  echo " ACTION NEEDED — the app is NOT using the bundled DB yet. In .env set:"
  print_use_bundled
fi
