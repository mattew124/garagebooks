#!/usr/bin/env bash
# =============================================================================
# teslamate-compat.sh — test this project against a real TeslaMate release.
#
#   scripts/teslamate-compat.sh [VERSION|latest] [--record] [--if-new]
#
# Starts a throwaway Postgres, lets the real teslamate/teslamate:VERSION image migrate it
# (the image's own entrypoint does that), then:
#   1. runs the whole test suite against that database (TEST_TESLAMATE_IMAGE);
#   2. compares TeslaMate's newest migration with garagebooks/compat.py.
# compat.py is changed while the suite runs (as if VERSION were recorded) and put back, unless --record.
# Never touches a real TeslaMate database. Needs docker and .venv (scripts/dev.sh setup).
#
# Exit status: 0 = tested and nothing to change; 1 = the tests failed; 2 = the tests pass but
# TeslaMate is newer than compat.py records (with --record, also after it has recorded it).
# --record, after a pass, updates compat.py, the README line and the CHANGELOG (an entry at the top of
# Unreleased) to VERSION, so only the review and the release are left.
# --if-new exits 0 at once, running nothing, when VERSION is the release already recorded.
# =============================================================================
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

version="" record=0 if_new=0
for arg in "$@"; do
    case "$arg" in
        --record) record=1 ;;
        --if-new) if_new=1 ;;
        *) version="$arg" ;;
    esac
done
tested="$(.venv/bin/python -c 'from garagebooks import compat; print(compat.TESTED_TESLAMATE)')"
recorded="$(.venv/bin/python -c 'from garagebooks import compat; print(compat.TESTED_MIGRATION)')"
version="${version:-$tested}"
if [ "$version" = latest ]; then
    version="$(curl -fsSL https://api.github.com/repos/teslamate-org/teslamate/releases/latest \
        | .venv/bin/python -c 'import json,sys; print(json.load(sys.stdin)["tag_name"].lstrip("v"))')"
fi
if [ "$if_new" = 1 ] && [ "$version" = "$tested" ]; then
    echo "compat: TeslaMate $version is already the recorded release; nothing new."
    exit 0
fi
image="teslamate/teslamate:$version"
echo "compat: TeslaMate $version (this release was tested with $tested)"

name="garagebooks-compat-$$"
trap 'docker rm -fv "$name" >/dev/null 2>&1 || true' EXIT
pass="$(openssl rand -hex 16)"
# --tmpfs: the postgres image's data dir is a VOLUME; without it every run leaks an anonymous volume.
docker run -d --rm --name "$name" --tmpfs /var/lib/postgresql -e POSTGRES_USER=teslamate -e POSTGRES_DB=teslamate \
    -e "POSTGRES_PASSWORD=$pass" postgres:18-trixie >/dev/null
until docker exec "$name" pg_isready -q -U teslamate -d teslamate; do sleep 1; done
sleep 2   # initdb restarts the server once

docker pull -q "$image" >/dev/null
# The entrypoint waits for the database and applies TeslaMate's migrations, then runs the
# command: a no-op here, so TeslaMate itself never starts (no Tesla login, no MQTT).
docker run --rm --network "container:$name" -e DATABASE_HOST=127.0.0.1 -e DATABASE_USER=teslamate \
    -e "DATABASE_PASS=$pass" -e DATABASE_NAME=teslamate -e ENCRYPTION_KEY=compat-test-only -e DISABLE_MQTT=true \
    "$image" /bin/dash -c true

newest="$(docker exec "$name" psql -X -At -U teslamate -d teslamate -c 'SELECT max(version) FROM schema_migrations')"
echo "compat: TeslaMate $version's newest migration is $newest (recorded: $recorded)"

status=0

# A TeslaMate newer than compat.py records is held back by the app, and the suite's tests of the
# TeslaMate-backed figures would fail for that reason alone. So the suite runs as the release will be
# once recorded: compat.py is changed for the run, and put back unless --record keeps it.
backup="$(mktemp)"
cp garagebooks/compat.py "$backup"
trap 'cp "$backup" garagebooks/compat.py; rm -f "$backup"; docker rm -f "$name" >/dev/null 2>&1 || true' EXIT
sed -i -E "s/^TESTED_TESLAMATE = .*/TESTED_TESLAMATE = \"$version\"/; s/^TESTED_MIGRATION = .*/TESTED_MIGRATION = $newest/" \
    garagebooks/compat.py

echo "compat: running the test suite against TeslaMate $version's own schema..."
TEST_TESLAMATE_IMAGE="$image" .venv/bin/python -m pytest -q -n auto -p no:cacheprovider || status=1

if [ "$status" = 0 ] && [ "$newest" -gt "$recorded" ]; then
    echo "compat: tests pass, but TeslaMate $version is newer than the recorded $tested."
    echo "compat: run again with --record to make this release claim it." >&2
    status=2
fi
if [ "$record" = 1 ] && { [ "$status" = 0 ] || [ "$status" = 2 ]; }; then
    cp garagebooks/compat.py "$backup"      # keep the recorded values past the restore on exit
    sed -i -E "s/^Tested with TeslaMate [0-9][0-9.]*\./Tested with TeslaMate $version./" README.md
    .venv/bin/python scripts/auto_rc.py record "$version" "$tested"
    echo "compat: recorded TeslaMate $version (migration $newest). Review 'git diff'."
    # Exit 2 stays: "passed, and newer than recorded" is how a caller (the workflow) learns something was recorded.
    [ "$status" = 2 ] || status=0
fi
exit "$status"
