"""Where a car's figures come from: which cars there are, what the odometer read at some
moment, what charging cost.

TeslaMate is one optional source. Where it is installed and readable it answers for the cars
it tracks, from the three tables the app has always read (cars, positions, charging_processes),
read-only. Everything else is answered from what was entered by hand: odometer readings,
the odometer on an expense, and expenses in an 'energy' category (charging and fuel).

A feature asks here and never queries TeslaMate's tables itself, and it must cope with "no
answer": every function returns None (or an Answer with no value and the reason) when the
figure isn't known, never a zero that stands in for it.

TeslaMate is in one of four states (TeslaMate.state):
  ok           installed, readable, and a version the app was tested with
  absent       not installed in this database: nothing to configure, nothing missing
  untested     installed, but newer than the tested version: its figures are held back
  unavailable  installed, but it can't be read (permissions, a table that changed)
"""

import logging
from dataclasses import dataclass
from decimal import Decimal

import psycopg

from . import compat

log = logging.getLogger(__name__)

OK, ABSENT, UNTESTED, UNAVAILABLE = "ok", "absent", "untested", "unavailable"


@dataclass(frozen=True)
class TeslaMate:
    state: str = ABSENT
    reason: str = ""          # why its figures are held back, for the log and the page
    charging: bool = False    # whether charging_processes can be read too (an older least-privilege role can't)

    @property
    def usable(self):
        return self.state == OK

    @property
    def installed(self):
        return self.state != ABSENT


@dataclass(frozen=True)
class Reading:
    km: float
    source: str               # "teslamate" or "manual"
    at: object = None         # when it was read (manual readings)


@dataclass(frozen=True)
class Answer:
    """A figure, or why there isn't one. `value` None means not known: never treat it as zero."""
    value: Decimal | None
    reason: str = ""


def _probe(conn, sql):
    """Run a query that returns nothing, in a savepoint. -> the error, or None when it ran."""
    try:
        with conn.transaction():
            conn.execute(sql)
        return None
    except psycopg.Error as exc:
        return exc


def detect(conn, allow_untested=False):
    """Look at this database and say what state TeslaMate is in. Cheap: four queries that return no rows."""
    cars = _probe(conn, "SELECT id, name, vin, model FROM public.cars LIMIT 0")
    if isinstance(cars, psycopg.errors.UndefinedTable | psycopg.errors.InvalidSchemaName):
        return TeslaMate(ABSENT)
    positions = cars or _probe(conn, "SELECT car_id, date, odometer FROM public.positions LIMIT 0")
    if positions is not None:
        reason = f"TeslaMate's tables can't be read ({type(positions).__name__}: {str(positions).splitlines()[0]})"
        return TeslaMate(UNAVAILABLE, reason)
    charging = _probe(conn, "SELECT car_id, start_date, cost FROM public.charging_processes LIMIT 0") is None
    held = compat.untested(compat.newest_migration(conn))
    if held and not allow_untested:
        return TeslaMate(UNTESTED, held, charging)
    return TeslaMate(OK, held or "", charging)


def sync_cars(conn, teslamate):
    """Give each TeslaMate car its row in our own car table. -> how many were added."""
    if not teslamate.usable:
        return 0
    try:
        with conn.transaction():
            return conn.execute("SELECT garagebooks.sync_teslamate_cars() AS n").fetchone()["n"]
    except psycopg.Error as exc:
        log.warning("TeslaMate's cars could not be read into the car list: %s", exc)
        return 0


_LINK = "(SELECT teslamate_car_id FROM garagebooks.car WHERE id = %(car)s)"


def odometer_at(conn, teslamate, car_id, when, manual=True):
    """The odometer (km) at `when`: TeslaMate's last position at or before it for a car it
    tracks, else (unless manual=False) the latest reading entered by hand at or before it.
    -> Reading, or None when nothing is known.

    positions.date is a naive UTC timestamp, so the aware `when` is converted to UTC first:
    comparing it as local time would be off by the UTC offset.
    """
    if teslamate.usable:
        try:
            with conn.transaction():
                row = conn.execute(
                    "SELECT odometer FROM public.positions "
                    f"WHERE car_id = {_LINK} AND odometer IS NOT NULL AND date <= (%(when)s AT TIME ZONE 'UTC') "
                    "ORDER BY date DESC LIMIT 1", {"car": car_id, "when": when}).fetchone()
            if row is not None:
                return Reading(round(row["odometer"], 1), "teslamate")
        except psycopg.Error as exc:
            log.warning("TeslaMate's odometer could not be read: %s", exc)
    if not manual:
        return None
    row = conn.execute(
        "SELECT km, at FROM ("
        " SELECT km, read_at AS at FROM garagebooks.odometer_reading WHERE car_id = %(car)s AND read_at <= %(when)s"
        " UNION ALL"
        " SELECT odometer_km, occurred_at FROM garagebooks.expense"
        "  WHERE car_id = %(car)s AND odometer_km IS NOT NULL AND occurred_at <= %(when)s"
        ") r ORDER BY at DESC, km DESC LIMIT 1", {"car": car_id, "when": when}).fetchone()
    return None if row is None else Reading(float(row["km"]), "manual", row["at"])


def charging_cost(conn, teslamate, car_id, start=None, end=None):
    """What charging and fuel cost a car, optionally between two moments (start inclusive, end
    exclusive): 'energy' expenses, plus TeslaMate's charging costs for a car it tracks.
    -> Answer. No value, with the reason, when the car is TeslaMate's and TeslaMate can't answer."""
    args = {"car": car_id, "start": start, "end": end}
    manual = conn.execute(
        "SELECT COALESCE(SUM(e.amount), 0) AS total FROM garagebooks.expense e "
        "JOIN garagebooks.category c ON c.id = e.category_id "
        "WHERE e.car_id = %(car)s AND c.kind = 'energy' "
        "AND (%(start)s::timestamptz IS NULL OR e.occurred_at >= %(start)s) "
        "AND (%(end)s::timestamptz IS NULL OR e.occurred_at < %(end)s)", args).fetchone()["total"]
    linked = conn.execute(f"SELECT {_LINK} AS id", args).fetchone()["id"]
    if linked is None:
        return Answer(manual)
    if not teslamate.usable:
        return Answer(None, teslamate.reason or "TeslaMate is not installed in this database.")
    if not teslamate.charging:
        return Answer(None, "The app's database user may not read TeslaMate's charging costs.")
    try:
        with conn.transaction():
            row = conn.execute(
                "SELECT COALESCE(SUM(cost), 0) AS total FROM public.charging_processes "
                "WHERE car_id = %(linked)s "
                "AND (%(start)s::timestamptz IS NULL OR start_date >= (%(start)s::timestamptz AT TIME ZONE 'UTC')) "
                "AND (%(end)s::timestamptz IS NULL OR start_date < (%(end)s::timestamptz AT TIME ZONE 'UTC'))",
                {**args, "linked": linked}).fetchone()
    except psycopg.Error as exc:
        log.warning("TeslaMate's charging costs could not be read: %s", exc)
        return Answer(None, "TeslaMate's charging costs could not be read.")
    return Answer(manual + Decimal(str(row["total"])))


def first_activity(conn, teslamate):
    """The earliest thing the dashboard can show: the first expense (the purchase, usually), odometer
    reading or TeslaMate position. None when there is nothing yet."""
    first = conn.execute(
        "SELECT LEAST((SELECT MIN(occurred_at) FROM garagebooks.expense),"
        " (SELECT MIN(read_at) FROM garagebooks.odometer_reading)) AS first").fetchone()["first"]
    if teslamate.usable:
        try:
            with conn.transaction():
                tm = conn.execute("SELECT MIN(date) AT TIME ZONE 'UTC' AS first FROM public.positions").fetchone()["first"]
            if tm is not None and (first is None or tm < first):
                first = tm
        except psycopg.Error as exc:
            log.warning("TeslaMate's first position could not be read: %s", exc)
    return first
