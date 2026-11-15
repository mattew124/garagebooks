"""Which TeslaMate this release of the app has been tested against, and the check that
holds TeslaMate's figures back on a newer one.

TeslaMate has no stable database schema (its FAQ says so), so a newer TeslaMate may have
renamed or retyped something we read. Its database does not record the release, only its
Ecto migrations, so the check compares the newest migration in `public.schema_migrations`
with TESTED_MIGRATION: the newest one in TESTED_TESLAMATE. Both are updated together, by
`scripts/teslamate-compat.sh VERSION --record`, only after the test suite passes against
that TeslaMate.

The app itself never stops for this: TeslaMate is one optional source of figures
(sources.py), so on an untested version its figures are left out, with the reason, and
everything entered by hand carries on."""

import logging

import psycopg
from psycopg.rows import tuple_row

TESTED_TESLAMATE = "4.4.0"
TESTED_MIGRATION = 20261014120000

log = logging.getLogger(__name__)


def newest_migration(conn):
    """The newest TeslaMate migration applied, or None if it can't be read. Leaves the caller's
    transaction usable either way."""
    try:
        with conn.transaction(), conn.cursor(row_factory=tuple_row) as cur:   # whatever row factory the caller's connection has
            return cur.execute("SELECT max(version) FROM public.schema_migrations").fetchone()[0]
    except (psycopg.errors.UndefinedTable, psycopg.errors.InsufficientPrivilege) as exc:
        log.warning("Cannot read TeslaMate's schema_migrations, so its version is unchecked (%s). "
                    "Tested with TeslaMate %s.", type(exc).__name__, TESTED_TESLAMATE)
        return None


def untested(newest):
    """Why this TeslaMate's figures are held back: a sentence for the log and the page, or None
    when its newest migration is one the app was tested with (or couldn't be read)."""
    if newest is None or newest <= TESTED_MIGRATION:
        return None
    return (f"This TeslaMate database is newer than GarageBooks has been tested with "
            f"(TeslaMate {TESTED_TESLAMATE}, migration {TESTED_MIGRATION}; this database is at "
            f"{newest}). TeslaMate's tables can change without notice, so its odometer and charging "
            f"figures are left out until you upgrade GarageBooks to a release that lists a newer "
            f"TeslaMate, or set ALLOW_UNTESTED_TESLAMATE=1 to use them anyway.")
