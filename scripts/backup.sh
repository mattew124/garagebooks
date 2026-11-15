#!/usr/bin/env bash
# =============================================================================
# backup.sh — back up, restore and check the `garagebooks` schema on its own.
#
#   backup.sh dump [FILE]              write the schema and its data to FILE
#                                      (default garagebooks-<date>-<time>.sql.gz here;
#                                      a name ending .gz is compressed). Read-only.
#   backup.sh restore [--replace] FILE load a dump made by `dump` (plain or .gz). Writes to
#                                      the database; refuses if the schema already has tables
#                                      unless --replace, which drops it first. All or nothing.
#   backup.sh check FILE               say whether an existing TeslaMate backup (plain SQL,
#                                      gzip or custom format) holds the schema. Read-only,
#                                      and needs no database (docker only for custom format).
#
# Only the garagebooks schema is ever dumped or restored; TeslaMate's own tables are never
# touched. The app's encryption key is not in the database and is never read here: keep it
# somewhere else, since the saved OneDrive sign-in in a dump can't be opened without it.
# Invoice files are not in the database either; back up their folder separately.
#
# Like provision-db.sh it runs pg_dump and psql *inside* the database container, so the host
# needs no PostgreSQL tools and the client always matches the server.
#
# Environment (same as provision-db.sh):
#   DB_CONTAINER       TeslaMate database container (default teslamate-database-1)
#   DB_USER / DB_NAME  superuser + database; default to the container's own
#                      POSTGRES_USER / POSTGRES_DB
#
# Exit status: 0 done (check: schema and data found), 1 refused or failed (check: not found, or
# structure only), 2 bad usage or an unreadable file.
# =============================================================================
set -euo pipefail

SCHEMA=garagebooks
DB_CONTAINER="${DB_CONTAINER:-teslamate-database-1}"

die() { echo "backup: $*" >&2; exit 1; }
usage() {
    echo "usage: backup.sh dump [FILE] | restore [--replace] FILE | check FILE" >&2
    exit 2
}

resolve_db() {
    DB_USER="${DB_USER:-$(docker exec "$DB_CONTAINER" printenv POSTGRES_USER 2>/dev/null || echo postgres)}"
    DB_NAME="${DB_NAME:-$(docker exec "$DB_CONTAINER" printenv POSTGRES_DB 2>/dev/null || echo "$DB_USER")}"
}

dbpsql() { docker exec -i "$DB_CONTAINER" psql -X -q -v ON_ERROR_STOP=1 -U "$DB_USER" -d "$DB_NAME" "$@"; }

need_file() {
    [[ -n "${1:-}" ]] || usage
    [[ -f "$1" && -r "$1" ]] || { echo "backup: $1 not found or not readable" >&2; exit 2; }
}

# The first bytes tell the format: gzip, PostgreSQL custom format, or plain SQL.
format_of() {
    local hex
    hex="$(head -c 5 "$1" | od -An -tx1 | tr -d ' \n')"
    case "$hex" in
        1f8b*) echo gzip ;;
        5047444d50) echo custom ;;   # "PGDMP"
        *) echo plain ;;
    esac
}

# Plain SQL on stdout, whether the file is plain or gzip.
sql_of() {
    if [[ "$(format_of "$1")" == gzip ]]; then gzip -dc -- "$1"; else cat -- "$1"; fi
}

# One pass over plain SQL: "<schema table?> <data?> <other tables?> <psql commands?>".
scan_sql() {
    awk '
        # Rows between COPY ... FROM stdin; and \. are data, whatever they say.
        incopy { if ($0 == "\\.") incopy = 0; next }
        /^COPY .* FROM stdin;$/ { incopy = 1 }
        /^CREATE TABLE garagebooks\./ { s = 1 }
        /^COPY garagebooks\./ || /^INSERT INTO garagebooks\./ { d = 1 }
        /^CREATE TABLE / && !/^CREATE TABLE garagebooks\./ { o = 1 }
        /^\\/ && !/^\\(un)?restrict / { m = 1 }
        END { print s + 0, d + 0, o + 0, m + 0 }'
}

cmd_dump() {
    local out="${1:-garagebooks-$(date +%Y%m%d-%H%M%S).sql.gz}" tmp
    [[ $# -le 1 ]] || usage
    [[ ! -e "$out" ]] || die "$out already exists; choose another name"
    resolve_db
    [[ "$(dbpsql -tA -c "SELECT 1 FROM pg_namespace WHERE nspname = '$SCHEMA'")" == 1 ]] \
        || die "no $SCHEMA schema in $DB_NAME@$DB_CONTAINER (has the app started once?)"
    umask 077
    tmp="$(mktemp "$out.partial.XXXXXX")"
    trap 'rm -f -- "$tmp"' EXIT
    # --no-owner/--no-privileges: the dump loads under whichever role runs it, and the
    # restricted role's grants come back from provision-db.sh.
    if [[ "$out" == *.gz ]]; then
        docker exec "$DB_CONTAINER" pg_dump -U "$DB_USER" -d "$DB_NAME" --schema="$SCHEMA" \
            --no-owner --no-privileges | gzip >"$tmp"
    else
        docker exec "$DB_CONTAINER" pg_dump -U "$DB_USER" -d "$DB_NAME" --schema="$SCHEMA" \
            --no-owner --no-privileges >"$tmp"
    fi
    mv -n -- "$tmp" "$out"
    trap - EXIT
    echo "backup: wrote $out (the $SCHEMA schema only; keep it private, and keep the encryption key elsewhere)"
}

cmd_restore() {
    local replace=0 file flags tables drop
    if [[ "${1:-}" == --replace ]]; then replace=1; shift; fi
    [[ $# -eq 1 ]] || usage
    file="$1"
    need_file "$file"
    [[ "$(format_of "$file")" != custom ]] || die "custom-format files can't be restored here; use a file made by 'dump'"
    flags="$(sql_of "$file" | scan_sql)"
    read -r has_schema _ has_other has_meta <<<"$flags"
    [[ "$has_schema" == 1 ]] || die "$file is not a $SCHEMA dump (no $SCHEMA tables in it)"
    [[ "$has_other" == 0 ]] || die "$file is a whole-database backup, not a $SCHEMA dump; restore it with TeslaMate's own steps"
    [[ "$has_meta" == 0 ]] || die "$file has psql commands in it; refusing"
    resolve_db
    tables="$(dbpsql -tA -c "SELECT count(*) FROM pg_tables WHERE schemaname = '$SCHEMA'")"
    if [[ "$tables" != 0 && "$replace" == 0 ]]; then
        die "$DB_NAME already has a $SCHEMA schema with $tables tables; nothing changed. Add --replace to drop it and load the backup"
    fi
    drop="DROP SCHEMA IF EXISTS $SCHEMA;"
    [[ "$replace" == 0 ]] || drop="DROP SCHEMA IF EXISTS $SCHEMA CASCADE;"
    # -1: one transaction, so a bad file leaves the existing schema as it was.
    { echo "$drop"; sql_of "$file"; } | dbpsql -1 >/dev/null
    echo "backup: restored the $SCHEMA schema from $file into $DB_NAME@$DB_CONTAINER"
}

cmd_check() {
    local file="${1:-}" has_schema has_data
    [[ $# -eq 1 ]] || usage
    need_file "$file"
    if [[ "$(format_of "$file")" == custom ]]; then
        local toc
        toc="$(docker exec -i "$DB_CONTAINER" pg_restore -l <"$file")" \
            || { echo "backup: pg_restore could not read $file (is the database container running?)" >&2; exit 2; }
        has_schema=0 has_data=0
        grep -q " TABLE $SCHEMA " <<<"$toc" && has_schema=1
        grep -q " TABLE DATA $SCHEMA " <<<"$toc" && has_data=1
    else
        read -r has_schema has_data _ <<<"$(sql_of "$file" | scan_sql)"
    fi
    if [[ "$has_schema" != 1 ]]; then
        echo "$file does not include the $SCHEMA schema: back it up with 'backup.sh dump', or change the backup to include it"
        exit 1
    fi
    if [[ "$has_data" != 1 ]]; then
        echo "$file includes the $SCHEMA schema, structure only: the data is not in it"
        exit 1
    fi
    echo "$file includes the $SCHEMA schema, with its data"
}

[[ $# -ge 1 ]] || usage
command="$1"
shift
case "$command" in
    dump) cmd_dump "$@" ;;
    restore) cmd_restore "$@" ;;
    check) cmd_check "$@" ;;
    *) usage ;;
esac
