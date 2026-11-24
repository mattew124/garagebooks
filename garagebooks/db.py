"""All SQL lives here. One short-lived connection per request — traffic is one
person entering the odd invoice, a pool would be machinery for nothing."""

import logging
import os
import re
import time
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import psycopg
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row

from . import validation


def dsn():
    url = os.environ.get("DATABASE_URL")
    if url:
        return url
    return make_conninfo(
        host=os.environ.get("DATABASE_HOST", "database"),
        port=os.environ.get("DATABASE_PORT", "5432"),
        dbname=os.environ["DATABASE_NAME"],
        user=os.environ["DATABASE_USER"],
        password=os.environ["DATABASE_PASS"],
    )


DASHBOARD_UID = "garagebooks-tco"   # uid in grafana/cost-of-ownership.json


def default_urls(env=os.environ):
    """What to put on the Settings page the first time, from FQDN_TM: the same compose
    variable TeslaMate uses for its own address. (None, None) if it is unset or not a bare
    host name. Only ever used once; after that the Settings page is the source of truth."""
    fqdn = (env.get("FQDN_TM") or "").strip()
    if not fqdn or not re.fullmatch(r"[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?(:\d{1,5})?", fqdn):
        return None, None
    base = "/" + env.get("BASE_PATH", "").strip("/")
    graf = "/" + env.get("GRAFANA_PATH", "/grafana").strip("/")
    try:
        return (validation.parse_url_setting(f"https://{fqdn}{base.rstrip('/')}/"),
                validation.parse_url_setting(f"https://{fqdn}{graf}/d/{DASHBOARD_UID}"))
    except ValueError:
        return None, None


SCHEMA_SQL = Path(__file__).with_name("schema.sql")
log = logging.getLogger(__name__)   # garagebooks.db: inherits the app's level and handlers
MIGRATE_LOCK = 7_265_171   # arbitrary; stops two workers starting at once from racing


def migrate(wait=60):
    """Create or update the `garagebooks` schema. schema.sql is idempotent, so this is safe on
    every start. Waits for the database to come up, but
    gives up straight away on a permissions problem and says what to do about it."""
    deadline = time.monotonic() + wait
    while True:
        try:
            conn = psycopg.connect(dsn(), connect_timeout=5, autocommit=True)
            break
        except psycopg.OperationalError:
            if time.monotonic() > deadline:
                log.error("database still unreachable after %ss; giving up", wait)
                raise
            log.debug("database not ready yet, retrying")
            time.sleep(2)
    try:
        with conn:
            conn.execute("SELECT pg_advisory_lock(%s)", (MIGRATE_LOCK,))
            try:
                with conn.transaction():
                    fresh = conn.execute("SELECT to_regclass('garagebooks.setting') IS NULL "
                                         "AND to_regclass('expenses.setting') IS NULL").fetchone()[0]   # old name: upgraded, not new
                    # Up to 0.4.0-rc1 the schema was `expenses`: schema.sql renames it (see there).
                    renaming = conn.execute("SELECT to_regclass('expenses.setting') IS NOT NULL "
                                            "AND to_regnamespace('garagebooks') IS NULL").fetchone()[0]
                    conn.execute(SCHEMA_SQL.read_text())
                    log.info("schema %s", "created" if fresh else
                             "upgraded: renamed expenses to garagebooks" if renaming else "checked and up to date")
                    if renaming:
                        move_app_url(conn)
                    if fresh:   # first start on this database: pre-fill from FQDN_TM
                        app_url, grafana_url = default_urls()
                        conn.execute("UPDATE garagebooks.setting SET app_url = %s, grafana_url = %s "
                                     "WHERE id = 1", (app_url, grafana_url))
            finally:
                conn.execute("SELECT pg_advisory_unlock(%s)", (MIGRATE_LOCK,))
    except psycopg.errors.InsufficientPrivilege as exc:
        log.error("the database user cannot create the 'garagebooks' schema")
        raise RuntimeError(
            "The database user cannot create the 'garagebooks' schema. Use TeslaMate's own database "
            "user, or create the schema yourself (scripts/provision-db.sh) and set SKIP_MIGRATE=1. "
            f"({exc})") from exc


def move_app_url(conn, env=os.environ):
    """Once, while upgrading from the old schema: the same release usually moves the app from
    /expenses to /garagebooks, so the saved address (the dashboard's link back to the app)
    follows BASE_PATH, keeping the owner's scheme and host. Unchanged when BASE_PATH is unset
    or already matches, and never on a later start, when the address is the owner's alone."""
    base = env.get("BASE_PATH", "").strip().rstrip("/")
    url = conn.execute("SELECT app_url FROM garagebooks.setting WHERE id = 1").fetchone()[0]
    if not base or not url:
        return
    parts = urlsplit(url)
    if parts.path.rstrip("/") == base:
        return
    try:
        moved = validation.parse_url_setting(urlunsplit((parts.scheme, parts.netloc, base + "/", "", "")))
    except ValueError:
        return
    conn.execute("UPDATE garagebooks.setting SET app_url = %s WHERE id = 1", (moved,))
    log.info("upgraded: the app's address moved from %s to %s", url, moved)


def connect():
    return psycopg.connect(dsn(), row_factory=dict_row, connect_timeout=5)


CAR_KINDS = {"BEV": "Battery electric", "ICE": "Petrol or diesel", "PHEV": "Plug-in hybrid", "HEV": "Hybrid"}


def cars(conn):
    """The app's own car list: the cars TeslaMate tracks (sources.sync_cars adds them) and those added by hand."""
    return conn.execute("SELECT id, name, kind, make, model, teslamate_car_id, sold_at, sale_price FROM garagebooks.car ORDER BY id").fetchall()


_PURCHASES = ("FROM garagebooks.expense e JOIN garagebooks.category k ON k.id = e.category_id "
              "WHERE e.car_id = %s AND k.kind = 'purchase'")


def car_details(conn):
    """The cars as the Cars page shows them: with how many expenses and leases each has, what it cost to buy
    (the expenses in a purchase category: how many, their total, and the first one's date and odometer)
    and the final odometer entered with its sale."""
    return conn.execute(
        "SELECT c.id, c.name, c.kind, c.make, c.model, c.teslamate_car_id, c.sold_at, c.sale_price,"
        " c.vin, c.build_year, c.model_year, c.body_type,"
        " (SELECT count(*) FROM garagebooks.expense e WHERE e.car_id = c.id) AS expenses,"
        " (SELECT count(*) FROM garagebooks.lease l WHERE l.car_id = c.id) AS leases,"
        " p.purchases, p.purchase_price, p.purchased_at, p.purchase_km,"
        " (SELECT r.km FROM garagebooks.odometer_reading r WHERE r.car_id = c.id AND r.read_at = c.sold_at) AS sale_km "
        "FROM garagebooks.car c LEFT JOIN LATERAL ("
        " SELECT count(*) AS purchases, SUM(e.amount) AS purchase_price, MIN(e.occurred_at) AS purchased_at,"
        "  (ARRAY_AGG(e.odometer_km ORDER BY e.occurred_at, e.id))[1] AS purchase_km"
        " FROM garagebooks.expense e JOIN garagebooks.category k ON k.id = e.category_id"
        " WHERE e.car_id = c.id AND k.kind = 'purchase') p ON true ORDER BY c.id").fetchall()


def set_purchase(conn, car_id, v):
    """Record or change what a car cost to buy (`v`: amount, occurred_at, odometer_km). The purchase is an
    expense in a purchase category, as it always was, so the totals need nothing new: a car with none
    gets one, a car with one has it changed. Returns "ok", "several" (more than one purchase expense:
    not ours to merge) or "no_category"."""
    rows = conn.execute(f"SELECT e.id, e.amount, e.occurred_at, e.odometer_km {_PURCHASES} ORDER BY e.id FOR UPDATE OF e",
                        (car_id,)).fetchall()
    if len(rows) > 1:
        return "several"
    if rows:
        old, zone = rows[0], v["occurred_at"].tzinfo
        if (old["amount"], old["occurred_at"].astimezone(zone).date(),
                None if old["odometer_km"] is None else float(old["odometer_km"])) == (
                v["amount"], v["occurred_at"].date(), v["odometer_km"]):
            return "ok"       # saved unchanged with the car's other details: the expense keeps its time of day
        conn.execute("UPDATE garagebooks.expense SET amount = %(amount)s, occurred_at = %(occurred_at)s, "
                     "odometer_km = %(odometer_km)s WHERE id = %(id)s", {**v, "id": rows[0]["id"]})
        return "ok"
    category = conn.execute("SELECT id FROM garagebooks.category WHERE kind = 'purchase' ORDER BY sort_order, id LIMIT 1").fetchone()
    if category is None:
        return "no_category"
    conn.execute("INSERT INTO garagebooks.expense (car_id, category_id, occurred_at, amount, odometer_km) "
                 "VALUES (%(car)s, %(category)s, %(occurred_at)s, %(amount)s, %(odometer_km)s)",
                 {**v, "car": car_id, "category": category["id"]})
    return "ok"


def add_car(conn, v):
    """A car entered by hand. Returns its id."""
    return conn.execute(
        "INSERT INTO garagebooks.car (name, kind, make, model, vin, build_year, model_year, body_type) "
        "VALUES (%(name)s, %(kind)s, %(make)s, %(model)s, %(vin)s, %(build_year)s, %(model_year)s, %(body_type)s) "
        "RETURNING id", v).fetchone()["id"]


def update_car(conn, car_id, v):
    """Returns False when there is no such car. A TeslaMate car renamed here keeps the new name."""
    return conn.execute(
        "UPDATE garagebooks.car SET kind = %(kind)s, make = %(make)s, model = %(model)s, vin = %(vin)s, "
        "build_year = %(build_year)s, model_year = %(model_year)s, body_type = %(body_type)s, "
        "name_from_teslamate = name_from_teslamate AND name = %(name)s, name = %(name)s WHERE id = %(id)s",
        {**v, "id": car_id}).rowcount == 1


CAR_DETAIL_FIELDS = ("vin", "build_year", "model_year", "body_type")


def car_blank_details(conn, car_id):
    """Which of the car's own details (CAR_DETAIL_FIELDS) are still empty, for the import to offer. A car that
    doesn't exist yet (id 0) has all of them blank."""
    if car_id == 0:
        return list(CAR_DETAIL_FIELDS)
    row = conn.execute("SELECT vin, build_year, model_year, body_type FROM garagebooks.car WHERE id = %s",
                       (car_id,)).fetchone()
    return [f for f in CAR_DETAIL_FIELDS if row and row[f] is None]


def fill_car_details(conn, car_id, v):
    """Put `v`'s details on the car where it has none. Nothing it already has is replaced, and nothing TeslaMate
    supplies (the name, make and model) is touched."""
    conn.execute(
        "UPDATE garagebooks.car SET vin = COALESCE(vin, %(vin)s), build_year = COALESCE(build_year, %(build_year)s), "
        "model_year = COALESCE(model_year, %(model_year)s), body_type = COALESCE(body_type, %(body_type)s) "
        "WHERE id = %(id)s", {"vin": None, "build_year": None, "model_year": None, "body_type": None, **v, "id": car_id})


def set_sale(conn, car_id, sold_at, sale_price, sale_km=None):
    """Record the sale of a car, or clear it (all None). The final odometer is kept as an odometer reading
    at the moment of the sale, so the distance and the per-km figures end there; the one from an earlier
    entry of the sale is replaced. Returns False when there is no such car."""
    old = conn.execute("SELECT sold_at FROM garagebooks.car WHERE id = %s FOR UPDATE", (car_id,)).fetchone()
    if old is None:
        return False
    if old["sold_at"] is not None:
        conn.execute("DELETE FROM garagebooks.odometer_reading WHERE car_id = %s AND read_at = %s", (car_id, old["sold_at"]))
    conn.execute("UPDATE garagebooks.car SET sold_at = %s, sale_price = %s WHERE id = %s", (sold_at, sale_price, car_id))
    if sold_at is not None and sale_km is not None:
        conn.execute("INSERT INTO garagebooks.odometer_reading (car_id, read_at, km) VALUES (%s, %s, %s) "
                     "ON CONFLICT (car_id, read_at) DO UPDATE SET km = EXCLUDED.km", (car_id, sold_at, sale_km))
    return True


def first_expense_at(conn, car_id):
    return conn.execute("SELECT MIN(occurred_at) AS at FROM garagebooks.expense WHERE car_id = %s", (car_id,)).fetchone()["at"]


def delete_car(conn, car_id):
    """Only a car added by hand that has no expenses. Returns whether it was deleted."""
    return conn.execute(
        "DELETE FROM garagebooks.car c WHERE c.id = %s AND c.teslamate_car_id IS NULL "
        "AND NOT EXISTS (SELECT 1 FROM garagebooks.expense e WHERE e.car_id = c.id)", (car_id,)).rowcount == 1


def has_schema(conn):
    """Whether the schema is this release's (SKIP_MIGRATE=1 leaves that to scripts/provision-db.sh)."""
    return conn.execute("SELECT to_regclass('garagebooks.setting') IS NULL "   # no schema at all: not ours to judge
                        "OR to_regclass('garagebooks.car') IS NOT NULL AS ok").fetchone()["ok"]


def build_ownership(conn, use_teslamate):
    """Put the ownership view in the form that fits: with TeslaMate's figures or without. Returns
    whether they are in it, or None when this database user may not rebuild it."""
    try:
        with conn.transaction():
            return conn.execute("SELECT garagebooks.build_ownership(%s) AS tm", (use_teslamate,)).fetchone()["tm"]
    except psycopg.Error as exc:
        log.warning("the ownership view could not be rebuilt: %s", exc)
        return None


def build_odometer_at(conn, use_teslamate):
    """The same for the SQL odometer function the dashboard's kilometre pace panel uses. Returns whether
    TeslaMate's positions are in it, or None when this database user may not rebuild it."""
    try:
        with conn.transaction():
            return conn.execute("SELECT garagebooks.build_odometer_at(%s) AS tm", (use_teslamate,)).fetchone()["tm"]
    except psycopg.Error as exc:
        log.warning("the odometer function could not be rebuilt: %s", exc)
        return None


def get_settings(conn):
    row = conn.execute("SELECT app_url, grafana_url, invoice_date_format, invoice_name_format, multi_amount, "
                       "lease_budgets FROM garagebooks.setting WHERE id = 1").fetchone()
    return row or {"app_url": None, "grafana_url": None, "invoice_date_format": None,
                   "invoice_name_format": None, "multi_amount": None, "lease_budgets": False}


def get_notify(conn):
    """The notification address and format (Settings), or None and 'ntfy'. Only the notifications job and
    the test button read the address; a page shows notify.host_of(address), never the address."""
    row = conn.execute("SELECT notify_url, notify_format FROM garagebooks.setting WHERE id = 1").fetchone()
    return (row["notify_url"], row["notify_format"] or "ntfy") if row else (None, "ntfy")


def save_notify(conn, url, fmt):
    conn.execute("UPDATE garagebooks.setting SET notify_url = %s, notify_format = %s WHERE id = 1", (url, fmt))


def get_notify_teslamate(conn):
    """Whether to send a notice when TeslaMate is newer than the tested release (Settings). Off by default."""
    row = conn.execute("SELECT notify_teslamate FROM garagebooks.setting WHERE id = 1").fetchone()
    return bool(row and row["notify_teslamate"])


def save_notify_teslamate(conn, on):
    conn.execute("UPDATE garagebooks.setting SET notify_teslamate = %s WHERE id = 1", (bool(on),))


def save_app_tz(conn, tz):
    """Keep APP_TZ in the settings, so the database's views (the reminders derived from a lease) judge "today" on
    the app's day, not the server's. Done on every start. False, and nothing saved, if the database does not know
    the zone or can't save it (it then uses its own day, as before)."""
    try:
        with conn.transaction():
            conn.execute("SELECT now() AT TIME ZONE %s", (tz,))
            conn.execute("UPDATE garagebooks.setting SET app_tz = %s WHERE id = 1 AND app_tz IS DISTINCT FROM %s",
                         (tz, tz))
        return True
    except psycopg.Error as exc:       # a zone the database doesn't know, or a schema from before this column
        log.warning("APP_TZ (%s) could not be saved for the database's views, which use the server's day: %s", tz, exc)
        return False


def lease_budgets_on(conn):
    """Whether the owner tracks the provider's yearly budget pools (Settings). Off unless turned on."""
    return bool(get_settings(conn)["lease_budgets"])


def save_settings(conn, app_url, grafana_url, invoice_date_format, invoice_name_format=None, multi_amount=None,
                  lease_budgets=False):
    conn.execute("UPDATE garagebooks.setting SET app_url = %s, grafana_url = %s, "
                 "invoice_date_format = %s, invoice_name_format = %s, multi_amount = %s, lease_budgets = %s WHERE id = 1",
                 (app_url, grafana_url, invoice_date_format, invoice_name_format, multi_amount, bool(lease_budgets)))


def tax_brackets(conn):
    """The ATO's income tax brackets, every year: rows of income_year, lower_income, threshold, base_tax, rate and
    checked_at, oldest year and lowest bracket first."""
    return conn.execute("SELECT * FROM garagebooks.tax_bracket ORDER BY income_year, lower_income").fetchall()


def replace_tax_year(conn, year, rows, checked_at):
    """Put one income year's brackets in place of what is there (rows of lower_income, threshold, base_tax, rate),
    noting when they were read. A year whose brackets are unchanged only has checked_at moved."""
    known = [(r["lower_income"], r["threshold"], r["base_tax"], r["rate"]) for r in conn.execute(
        "SELECT * FROM garagebooks.tax_bracket WHERE income_year = %s ORDER BY lower_income", (year,))]
    if known != [(r["lower_income"], r["threshold"], r["base_tax"], r["rate"]) for r in rows]:
        conn.execute("DELETE FROM garagebooks.tax_bracket WHERE income_year = %s", (year,))
        for r in rows:
            conn.execute("INSERT INTO garagebooks.tax_bracket (income_year, lower_income, threshold, base_tax, rate) "
                         "VALUES (%s, %s, %s, %s, %s)", (year, r["lower_income"], r["threshold"], r["base_tax"], r["rate"]))
    conn.execute("UPDATE garagebooks.tax_bracket SET checked_at = %s WHERE income_year = %s", (checked_at, year))
    return known != [(r["lower_income"], r["threshold"], r["base_tax"], r["rate"]) for r in rows]


def tax_choices(conn):
    """Each person's chosen bracket and Medicare status by income year: {(person, year): row}. Never a salary."""
    return {(r["person"], r["income_year"]): r for r in conn.execute("SELECT * FROM garagebooks.tax_choice")}


def set_tax_choice(conn, person, year, bracket_lower, medicare):
    """Choose a person's bracket (by its first dollar) and Medicare status for an income year; bracket_lower None
    removes the year's choice, so it follows the latest earlier one."""
    if bracket_lower is None:
        conn.execute("DELETE FROM garagebooks.tax_choice WHERE person = %s AND income_year = %s", (person, year))
        return
    conn.execute("INSERT INTO garagebooks.tax_choice (person, income_year, bracket_lower, medicare) VALUES (%s, %s, %s, %s) "
                 "ON CONFLICT (person, income_year) DO UPDATE SET bracket_lower = excluded.bracket_lower, medicare = excluded.medicare",
                 (person, year, bracket_lower, medicare))


def tax_positions(conn, person, years):
    """garagebooks.tax_position() for each year: {year: row or None}."""
    return {y: conn.execute("SELECT * FROM garagebooks.tax_position(%s, %s)", (person, y)).fetchone() for y in years}


def any_associate_lease(conn):
    return conn.execute("SELECT EXISTS (SELECT 1 FROM garagebooks.lease WHERE associate_lease) AS x").fetchone()["x"]


def lease_years(conn, car_id):
    """garagebooks.lease_income_year() for one car's leases, by lease and income year."""
    return conn.execute("SELECT * FROM garagebooks.lease_income_year() WHERE car_id = %s ORDER BY income_year, lease_id",
                        (car_id,)).fetchall()


def any_tax_choice(conn, person="employee"):
    return conn.execute("SELECT EXISTS (SELECT 1 FROM garagebooks.tax_choice WHERE person = %s) AS x", (person,)).fetchone()["x"]


def ev_charging_rates(conn):
    """The ATO's home charging rates: {FBT year: cents per km}."""
    return {r["fbt_year"]: r["cents_per_km"] for r in conn.execute(
        "SELECT fbt_year, cents_per_km FROM garagebooks.ev_charging_rate ORDER BY fbt_year")}


def ev_charging_table(conn):
    """The home charging rates with where they came from and when they were last checked, newest year first."""
    return conn.execute("SELECT * FROM garagebooks.ev_charging_rate ORDER BY fbt_year DESC").fetchall()


def set_ev_charging_rate(conn, year, cents_per_km, source_url=None, checked_at=None):
    """Add a year's rate, or change it (the ato_rates job, with where it read it and when)."""
    conn.execute("INSERT INTO garagebooks.ev_charging_rate (fbt_year, cents_per_km, source_url, checked_at) VALUES (%s, %s, %s, %s) "
                 "ON CONFLICT (fbt_year) DO UPDATE SET cents_per_km = excluded.cents_per_km, source_url = excluded.source_url,"
                 " checked_at = excluded.checked_at", (year, cents_per_km, source_url, checked_at))


def categories(conn):
    return conn.execute(
        "SELECT id, name, kind, upload_folder, budget_pool, gst_free, has_period FROM garagebooks.category ORDER BY sort_order, name"
    ).fetchall()


CATEGORY_FIELDS = ("upload_folder", "gst_free", "has_period", "budget_pool")


def update_category(conn, category_id, **fields):
    """Set some of a category's editable fields (CATEGORY_FIELDS). False if no such category."""
    if not fields or not set(fields) <= set(CATEGORY_FIELDS):
        raise ValueError(f"not editable category fields: {fields}")
    sets = ", ".join(f"{name} = %({name})s" for name in fields)
    return conn.execute(f"UPDATE garagebooks.category SET {sets} WHERE id = %(id)s",  # noqa: S608 - names checked above
                        {**fields, "id": category_id}).rowcount == 1


SERVICE_CATEGORY = "Service"


def service_items(conn):
    return conn.execute("SELECT id, name FROM garagebooks.service_item ORDER BY sort_order, lower(name)").fetchall()


def service_item(conn, item_id):
    return conn.execute("SELECT id, name FROM garagebooks.service_item WHERE id = %s", (item_id,)).fetchone()


def service_item_named(conn, name):
    return conn.execute("SELECT id, name FROM garagebooks.service_item WHERE lower(name) = lower(%s)", (name,)).fetchone()


def add_service_item(conn, name):
    """The new item, or None if the name exists (ignoring case)."""
    return conn.execute("INSERT INTO garagebooks.service_item (name) VALUES (%s) ON CONFLICT DO NOTHING "
                        "RETURNING id, name", (name,)).fetchone()


def rename_service_items(conn, names):
    """Rename items together: {item id: new name}. The names must already be checked as a set (no two the
    same, none another item's), so two items can swap names: each goes to a placeholder first, because the
    names are unique at every step."""
    if len(names) > 1:
        for item_id in names:
            conn.execute("UPDATE garagebooks.service_item SET name = %s WHERE id = %s", (f"~renaming {item_id}", item_id))
    for item_id, name in names.items():
        conn.execute("UPDATE garagebooks.service_item SET name = %s WHERE id = %s", (name, item_id))


def delete_service_item(conn, item_id):
    return conn.execute("DELETE FROM garagebooks.service_item WHERE id = %s", (item_id,)).rowcount == 1


def set_expense_services(conn, expense_id, item_ids):
    """Replace an expense's service items with `item_ids`."""
    conn.execute("DELETE FROM garagebooks.expense_service_item WHERE expense_id = %s", (expense_id,))
    for item_id in sorted(set(item_ids)):
        conn.execute("INSERT INTO garagebooks.expense_service_item (expense_id, item_id) VALUES (%s, %s) "
                     "ON CONFLICT DO NOTHING", (expense_id, item_id))


def expense_services(conn, expense_ids=None):
    """-> {expense_id: [item names in list order]}, for some expenses or all."""
    got = {}
    for r in conn.execute(
            "SELECT l.expense_id, i.name FROM garagebooks.expense_service_item l "
            "JOIN garagebooks.service_item i ON i.id = l.item_id "
            "WHERE (%(ids)s::bigint[] IS NULL OR l.expense_id = ANY(%(ids)s)) ORDER BY i.sort_order, lower(i.name)",
            {"ids": None if expense_ids is None else list(expense_ids)}):
        got.setdefault(r["expense_id"], []).append(r["name"])
    return got


def service_category_id(conn):
    row = conn.execute("SELECT id FROM garagebooks.category WHERE name = %s", (SERVICE_CATEGORY,)).fetchone()
    return row["id"] if row else None


def expense_service_ids(conn, expense_id):
    return [r["item_id"] for r in conn.execute(
        "SELECT item_id FROM garagebooks.expense_service_item WHERE expense_id = %s", (expense_id,))]


def add_category(conn, name):
    """Returns the id, or None if the name already exists (case-insensitive)."""
    existing = conn.execute(
        "SELECT id FROM garagebooks.category WHERE lower(name) = lower(%s)", (name,)
    ).fetchone()
    if existing:
        return None
    return conn.execute(
        "INSERT INTO garagebooks.category (name, kind) VALUES (%s, 'running') "
        "ON CONFLICT (name) DO NOTHING RETURNING id",
        (name,),
    ).fetchone()["id"]


_SELECT = """
    SELECT e.id, e.car_id, c.name AS car_name, e.category_id, cat.name AS category,
           cat.kind, e.occurred_at, e.amount, e.odometer_km, e.vendor, e.notes,
           e.invoice_url, e.invoice_path, e.packaged, e.period_start, e.period_end,
           e.gst_amount, e.card_payment_ref, e.bpay_biller_code, e.bpay_ref, cat.has_period, c.make AS car_make, c.model AS car_model, cat.budget_pool,
           cl.provider AS claim_provider, cl.provider_id AS claim_provider_id, cl.reference AS claim_reference,
           cl.status AS claim_status, cl.paid_date AS claim_paid_date, cl.approved_amount AS claim_approved,
           (EXISTS (SELECT 1 FROM garagebooks.provider_claim pc WHERE pc.expense_id = e.id)
            OR EXISTS (SELECT 1 FROM garagebooks.provider_transaction pt WHERE pt.expense_id = e.id AND pt.kind = 'direct_payment')) AS provider_matched
    FROM garagebooks.expense e
    JOIN garagebooks.car c ON c.id = e.car_id
    JOIN garagebooks.category cat ON cat.id = e.category_id
    LEFT JOIN garagebooks.expense_claim cl ON cl.expense_id = e.id
"""

# What the cost after tax needs of an expense (lease.effective_cost works it out): its own choice, its
# category's pool, kind and GST status, the payments of the lease covering its date in time zone %(tz)s, and the
# marginal rate for the income year of that date (None when no tax bracket is chosen for it or before it).
_AFTER_TAX_SELECT = """
    SELECT e.id, e.car_id, e.category_id, cat.name AS category, cat.kind, cat.budget_pool, cat.gst_free, e.amount,
           e.gst_amount, e.occurred_at, e.packaged, l.id AS lease_id, l.pretax_payment, l.posttax_payment,
           garagebooks.marginal_rate('employee', (e.occurred_at AT TIME ZONE %(tz)s)::date) AS marginal_rate
    FROM garagebooks.expense e
    JOIN garagebooks.category cat ON cat.id = e.category_id
    LEFT JOIN LATERAL (SELECT * FROM garagebooks.lease l WHERE l.car_id = e.car_id
                       AND (e.occurred_at AT TIME ZONE %(tz)s)::date BETWEEN l.start_date AND l.end_date
                       ORDER BY l.start_date LIMIT 1) l ON true
"""


def after_tax_facts(conn, tz, car_id=None, category_id=None, year=None, expense_ids=None):
    """Expenses with what lease.effective_cost() needs, newest first. Narrowed to a car, a category, an FBT
    year (by each expense's date in `tz`) or some expense ids."""
    if any(v is not None and not 0 <= v <= 32767 for v in (car_id, category_id)):
        return []                   # ids are smallint: nothing can match one outside it
    where = ["(%(car)s::bigint IS NULL OR e.car_id = %(car)s)", "(%(cat)s::bigint IS NULL OR e.category_id = %(cat)s)",
             "(%(year)s::integer IS NULL OR garagebooks.fbt_year((e.occurred_at AT TIME ZONE %(tz)s)::date) = %(year)s)",
             "(%(ids)s::bigint[] IS NULL OR e.id = ANY(%(ids)s))"]
    return conn.execute(f"{_AFTER_TAX_SELECT} WHERE {' AND '.join(where)} ORDER BY e.occurred_at DESC, e.id DESC",
                        {"tz": tz, "car": car_id, "cat": category_id, "year": year,
                         "ids": None if expense_ids is None else list(expense_ids)}).fetchall()


def lease_car_ids(conn):
    """The cars that have at least one lease."""
    return {r["car_id"] for r in conn.execute("SELECT DISTINCT car_id FROM garagebooks.lease")}


# What the list can be sorted by: the name in the address -> the SQL it means. Never built from user text.
SORTS = {"date": "e.occurred_at", "category": "cat.name", "vendor": "NULLIF(lower(e.vendor), '')",
         "amount": "e.amount", "odometer": "e.odometer_km"}


def _filters(category_id, car_id, start, end, tz):
    where, params = [], []
    if category_id:
        where.append("e.category_id = %s")
        params.append(category_id)
    if car_id:
        where.append("e.car_id = %s")
        params.append(car_id)
    if start:
        where.append("(e.occurred_at AT TIME ZONE %s)::date >= %s")
        params += [tz, start]
    if end:
        where.append("(e.occurred_at AT TIME ZONE %s)::date <= %s")
        params += [tz, end]
    return ("WHERE " + " AND ".join(where)) if where else "", params


def list_expenses(conn, category_id=None, limit=1000, sort="date", direction="desc", car_id=None, start=None, end=None,
                  tz="UTC"):
    """Sorted either way, optionally one category's or one car's and between two local dates. Blank vendors and
    odometers always come last, whichever way round."""
    order = SORTS.get(sort, SORTS["date"])
    way = "ASC" if direction == "asc" else "DESC"
    where, params = _filters(category_id, car_id, start, end, tz)
    params.append(limit)
    return conn.execute(
        f"{_SELECT} {where} ORDER BY {order} {way} NULLS LAST, e.occurred_at {way}, e.id {way} LIMIT %s", params
    ).fetchall()


def totals(conn, category_id=None, car_id=None, start=None, end=None, tz="UTC"):
    where, params = _filters(category_id, car_id, start, end, tz)
    return conn.execute(
        f"SELECT COALESCE(SUM(e.amount), 0) AS total, COUNT(*) AS n FROM garagebooks.expense e {where}", params,
    ).fetchone()


def get_expense(conn, expense_id):
    return conn.execute(f"{_SELECT} WHERE e.id = %s", (expense_id,)).fetchone()


_FIELDS = ("car_id", "category_id", "occurred_at", "amount", "odometer_km",
           "vendor", "notes", "invoice_url", "invoice_path", "packaged", "period_start", "period_end",
           "gst_amount", "card_payment_ref", "bpay_biller_code", "bpay_ref")

_DEFAULTS = {"invoice_path": None, "packaged": None, "period_start": None, "period_end": None, "gst_amount": None,
             "card_payment_ref": None, "bpay_biller_code": None, "bpay_ref": None}


def insert_expense(conn, v):
    return conn.execute(
        f"INSERT INTO garagebooks.expense ({', '.join(_FIELDS)}) "
        f"VALUES ({', '.join('%(' + f + ')s' for f in _FIELDS)}) RETURNING id",
        {**_DEFAULTS, **v},
    ).fetchone()["id"]


def learned(conn, after):
    """What the importer trusts: labels confirmed `after`+ times, and for each vendor
    its clear-cut category. -> {"labels": {field: {label}}, "vendors": {vendor: category}}."""
    labels = {}
    for r in conn.execute("SELECT field, label FROM garagebooks.learned_label WHERE hits >= %s", (after,)):
        labels.setdefault(r["field"], set()).add(r["label"])
    vendors, seen = {}, {}
    for r in conn.execute(
            "SELECT l.vendor, c.name, l.hits FROM garagebooks.learned_vendor l "
            "JOIN garagebooks.category c ON c.id = l.category_id WHERE l.hits >= %s "
            "ORDER BY l.vendor, l.hits DESC", (after,)):
        if r["vendor"] not in seen:
            seen[r["vendor"]] = r["hits"]
            vendors[r["vendor"]] = r["name"]
        elif seen[r["vendor"]] == r["hits"]:
            vendors.pop(r["vendor"], None)   # two categories tied: no clear answer
    return {"labels": labels, "vendors": vendors}


def record_label(conn, field, label):
    conn.execute("INSERT INTO garagebooks.learned_label (field, label) VALUES (%s, %s) "
                 "ON CONFLICT (field, label) DO UPDATE SET hits = garagebooks.learned_label.hits + 1",
                 (field, label))


def record_vendor(conn, vendor, category_id):
    conn.execute("INSERT INTO garagebooks.learned_vendor (vendor, category_id) VALUES (%s, %s) "
                 "ON CONFLICT (vendor, category_id) DO UPDATE SET hits = garagebooks.learned_vendor.hits + 1",
                 (vendor, category_id))


def learned_rows(conn):
    return (conn.execute("SELECT field, label, hits FROM garagebooks.learned_label ORDER BY field, label").fetchall(),
            conn.execute("SELECT l.vendor, c.name AS category, l.hits FROM garagebooks.learned_vendor l "
                         "JOIN garagebooks.category c ON c.id = l.category_id ORDER BY l.vendor, c.name").fetchall())


def delete_label(conn, field, label):
    conn.execute("DELETE FROM garagebooks.learned_label WHERE field = %s AND label = %s", (field, label))


def delete_vendor(conn, vendor):
    conn.execute("DELETE FROM garagebooks.learned_vendor WHERE vendor = %s", (vendor,))


DUPLICATE_WINDOW_DAYS = 14


def find_duplicates(conn, car_id, occurred_at, amount, window_days=DUPLICATE_WINDOW_DAYS, exclude_id=None):
    """Expenses that look like this one: same car, exactly the same amount, within `window_days`
    either side. Closest first, at most five. Category and vendor are shown, not required to match:
    the same bill is often filed under a different category or vendor spelling."""
    return conn.execute(
        f"{_SELECT} WHERE e.car_id = %(car)s AND e.amount = %(amount)s "
        "AND e.occurred_at BETWEEN %(when)s - make_interval(days => %(days)s) AND %(when)s + make_interval(days => %(days)s) "
        "AND (%(skip)s::bigint IS NULL OR e.id <> %(skip)s) "
        "ORDER BY abs(extract(epoch FROM e.occurred_at - %(when)s)), e.id LIMIT 5",
        {"car": car_id, "amount": amount, "when": occurred_at, "days": window_days, "skip": exclude_id}).fetchall()


def known_invoices(conn, refs):
    """Which of these invoices an expense already has: links, or paths in the folder on the server
    (a path starts with '/', a link with https://, so one can't be taken for the other)."""
    if not refs:
        return set()
    rows = conn.execute("SELECT invoice_url AS ref FROM garagebooks.expense WHERE invoice_url = ANY(%(refs)s) "
                        "UNION SELECT invoice_path FROM garagebooks.expense WHERE invoice_path = ANY(%(refs)s) "
                        # An email whose PDF was copied into the car's storage: its expense links the copy.
                        "UNION SELECT ref FROM garagebooks.mail_import WHERE ref = ANY(%(refs)s) "
                        "UNION SELECT invoice_url FROM garagebooks.expense_document WHERE invoice_url = ANY(%(refs)s) "
                        "UNION SELECT invoice_path FROM garagebooks.expense_document WHERE invoice_path = ANY(%(refs)s)",
                        {"refs": list(refs)}).fetchall()
    return {r["ref"] for r in rows}


def add_document(conn, expense_id, name, ref_field, ref):
    """Attach a PDF to an expense. `ref_field` is 'invoice_url' or 'invoice_path'; one already attached is left alone."""
    column = "invoice_path" if ref_field == "invoice_path" else "invoice_url"
    conn.execute(f"INSERT INTO garagebooks.expense_document (expense_id, name, {column}) VALUES (%s, %s, %s) "
                 "ON CONFLICT DO NOTHING", (expense_id, name[:300], ref))


def documents(conn, expense_id):
    return conn.execute("SELECT id, name, invoice_url, invoice_path FROM garagebooks.expense_document "
                        "WHERE expense_id = %s ORDER BY id", (expense_id,)).fetchall()


def get_document(conn, document_id):
    return conn.execute("SELECT id, expense_id, name, invoice_url, invoice_path FROM garagebooks.expense_document "
                        "WHERE id = %s", (document_id,)).fetchone()


def delete_document(conn, expense_id, document_id):
    return conn.execute("DELETE FROM garagebooks.expense_document WHERE id = %s AND expense_id = %s",
                        (document_id, expense_id)).rowcount == 1


def skipped_import_urls(conn, car_id):
    return {r["url"] for r in conn.execute("SELECT url FROM garagebooks.import_skip WHERE car_id = %s", (car_id,))}


def skip_imports(conn, car_id, urls):
    for url in urls:
        conn.execute("INSERT INTO garagebooks.import_skip (car_id, url) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                     (car_id, url))


def clear_skipped_imports(conn, car_id):
    conn.execute("DELETE FROM garagebooks.import_skip WHERE car_id = %s", (car_id,))


def update_expense(conn, expense_id, v):
    sets = ", ".join(f"{f} = %({f})s" for f in _FIELDS)
    cur = conn.execute(
        f"UPDATE garagebooks.expense SET {sets}, updated_at = now() WHERE id = %(id)s",
        {**_DEFAULTS, **v, "id": expense_id},
    )
    return cur.rowcount == 1


def is_claimed(conn, expense_id):
    return conn.execute("SELECT 1 FROM garagebooks.expense_claim WHERE expense_id = %s", (expense_id,)).fetchone() is not None


def delete_expense(conn, expense_id):
    return conn.execute("DELETE FROM garagebooks.expense WHERE id = %s", (expense_id,)).rowcount == 1


# ---- invoice storage -----------------------------------------------------------

def car_folders(conn):
    """-> {car_id: {folder_id, folder_path, backend}}. backend is 'onedrive' or 'local'."""
    return {r["car_id"]: r for r in
            conn.execute("SELECT car_id, folder_id, folder_path, backend FROM garagebooks.car_folder")}


def set_car_folder(conn, car_id, folder_id, folder_path, backend="onedrive"):
    """A car has one folder: choosing one in the other storage replaces it."""
    conn.execute(
        "INSERT INTO garagebooks.car_folder (car_id, folder_id, folder_path, backend) VALUES (%s, %s, %s, %s) "
        "ON CONFLICT (car_id) DO UPDATE SET folder_id = EXCLUDED.folder_id, "
        "folder_path = EXCLUDED.folder_path, backend = EXCLUDED.backend",
        (car_id, folder_id, folder_path, backend))


def clear_car_folder(conn, car_id, backend):
    conn.execute("DELETE FROM garagebooks.car_folder WHERE car_id = %s AND backend = %s", (car_id, backend))


def onedrive_account(conn, lock=False):
    """The single sign-in row, or None. lock=True holds it until commit, so two
    uploads at once can't both spend the same (single-use) refresh token."""
    return conn.execute(
        "SELECT * FROM garagebooks.onedrive_account" + (" FOR UPDATE" if lock else "")
    ).fetchone()


def save_onedrive_account(conn, **fields):
    """Upsert the given columns of the sign-in row."""
    cols = ", ".join(fields)
    vals = ", ".join(f"%({f})s" for f in fields)
    sets = ", ".join(f"{f} = EXCLUDED.{f}" for f in fields)
    conn.execute(
        f"INSERT INTO garagebooks.onedrive_account (id, {cols}) VALUES (true, {vals}) "
        f"ON CONFLICT (id) DO UPDATE SET {sets}, updated_at = now()",
        fields)



def note_signin_lost(conn, mail=False):
    """Microsoft refused the saved sign-in (the mailbox's, with mail=True): remember when it was first
    noticed (the notifications job tells the owner once). Cleared by connecting again, new credentials,
    disconnecting or a refresh that works."""
    p = "mail_" if mail else ""
    conn.execute(f"UPDATE garagebooks.onedrive_account SET {p}signin_lost_at = COALESCE({p}signin_lost_at, now()), "
                 f"{p}signin_checked_at = now() WHERE {p}refresh_token IS NOT NULL")


# ---- invoices by email -----------------------------------------------------------

def car_mail_folders(conn):
    """-> {car_id: {folder_id, folder_path, waiting, newest_at, checked_at}}."""
    return {r["car_id"]: r for r in conn.execute("SELECT * FROM garagebooks.car_mail_folder")}


def set_car_mail_folder(conn, car_id, folder_id, folder_path):
    """A new folder starts with nothing counted: the mail job counts it on its next run."""
    conn.execute(
        "INSERT INTO garagebooks.car_mail_folder (car_id, folder_id, folder_path) VALUES (%s, %s, %s) "
        "ON CONFLICT (car_id) DO UPDATE SET folder_id = EXCLUDED.folder_id, folder_path = EXCLUDED.folder_path, "
        "waiting = NULL, newest_at = NULL, checked_at = NULL", (car_id, folder_id, folder_path))


def clear_car_mail_folder(conn, car_id):
    conn.execute("DELETE FROM garagebooks.car_mail_folder WHERE car_id = %s", (car_id,))


def note_mail_waiting(conn, car_id, waiting, newest_at):
    conn.execute("UPDATE garagebooks.car_mail_folder SET waiting = %s, newest_at = %s, checked_at = now() "
                 "WHERE car_id = %s", (waiting, newest_at, car_id))


def record_mail_import(conn, car_id, ref, expense_id):
    conn.execute("INSERT INTO garagebooks.mail_import (ref, car_id, expense_id) VALUES (%s, %s, %s) "
                 "ON CONFLICT (ref) DO NOTHING", (ref, car_id, expense_id))


# ---- leases (roadmap L1) -------------------------------------------------------------

LEASE_FIELDS = ("package_name", "financier", "start_date", "end_date", "refinance_date", "term_months",
                "pay_frequency", "pretax_payment", "posttax_payment", "admin_fee", "car_cost",
                "residual_amount", "residual_percent", "fbt_base_value", "fbt_method", "statutory_percent",
                "ev_exempt", "tax_treatment", "tax_treatment_note", "estimated_km_per_year",
                "rego_expiry", "rego_renewed_by_provider", "associate_lease", "charging_reimbursed", "depreciation_value",
                "form_url", "form_path", "agreement_url", "agreement_path")


def car_leases(conn, car_id):
    """Every lease of a car, newest first. A row per lease with all its columns."""
    return conn.execute("SELECT * FROM garagebooks.lease WHERE car_id = %s ORDER BY start_date DESC, id DESC",
                        (car_id,)).fetchall()


def get_lease(conn, lease_id, car_id=None):
    """One lease, or None. With `car_id`, only if it is that car's."""
    return conn.execute("SELECT * FROM garagebooks.lease WHERE id = %s AND (%s::smallint IS NULL OR car_id = %s)",
                        (lease_id, car_id, car_id)).fetchone()


def current_lease(conn, car_id, on):
    """The lease that covers the date `on` (both ends included), else the next one to start, else
    None. At most one lease of a car covers a day: the app refuses overlapping ranges."""
    return conn.execute(
        "SELECT * FROM garagebooks.lease WHERE car_id = %(car)s AND end_date >= %(on)s "
        "ORDER BY start_date LIMIT 1", {"car": car_id, "on": on}).fetchone()


def any_estimated_km(conn):
    """Whether any lease has an estimated km per year: only then does the dashboard have a kilometre pace to show."""
    return conn.execute("SELECT EXISTS (SELECT 1 FROM garagebooks.lease WHERE estimated_km_per_year IS NOT NULL) AS found").fetchone()["found"]


def lease_budgets(conn, lease_id):
    """Every yearly budget of a lease: rows of pool, yearly_amount, effective_from (earliest first).
    A later re-budget is another row for the pool; lease.budget_on() picks the one in force on a date."""
    return conn.execute("SELECT pool, yearly_amount, effective_from FROM garagebooks.lease_budget "
                        "WHERE lease_id = %s ORDER BY effective_from, pool", (lease_id,)).fetchall()


def overlapping_lease(conn, car_id, start, end, exclude_id=None):
    """A lease of this car whose dates share a day with start..end, or None."""
    return conn.execute(
        "SELECT * FROM garagebooks.lease WHERE car_id = %(car)s AND start_date <= %(end)s AND end_date >= %(start)s "
        "AND (%(skip)s::integer IS NULL OR id <> %(skip)s) ORDER BY start_date LIMIT 1",
        {"car": car_id, "start": start, "end": end, "skip": exclude_id}).fetchone()


def _save_budgets(conn, lease_id, start, budgets):
    for pool, amount in budgets.items():
        conn.execute("INSERT INTO garagebooks.lease_budget (lease_id, pool, yearly_amount, effective_from) "
                     "VALUES (%s, %s, %s, %s)", (lease_id, pool, amount, start))


def add_lease(conn, car_id, v):
    """Add a lease with its first budgets (effective from its start). Returns its id, or None when its dates
    overlap another lease of the car (checked while the car's row is held, so two at once can't both pass)."""
    conn.execute("SELECT id FROM garagebooks.car WHERE id = %s FOR UPDATE", (car_id,))
    if overlapping_lease(conn, car_id, v["start_date"], v["end_date"]):
        return None
    cols = ", ".join(LEASE_FIELDS)
    marks = ", ".join(f"%({f})s" for f in LEASE_FIELDS)
    lease_id = conn.execute(f"INSERT INTO garagebooks.lease (car_id, {cols}) VALUES (%(car_id)s, {marks}) RETURNING id",
                            {**v, "car_id": car_id}).fetchone()["id"]
    _save_budgets(conn, lease_id, v["start_date"], v["budgets"] or {})
    return lease_id


def update_lease(conn, car_id, lease_id, v):
    """Change a lease and its first budgets (the rows effective from its start; a later re-budget stays).
    Returns "missing" (no such lease for this car), "overlap" or "ok"."""
    conn.execute("SELECT id FROM garagebooks.car WHERE id = %s FOR UPDATE", (car_id,))
    old = get_lease(conn, lease_id, car_id)
    if old is None:
        return "missing"
    if overlapping_lease(conn, car_id, v["start_date"], v["end_date"], exclude_id=lease_id):
        return "overlap"
    sets = ", ".join(f"{f} = %({f})s" for f in LEASE_FIELDS)
    conn.execute(f"UPDATE garagebooks.lease SET {sets} WHERE id = %(id)s", {**v, "id": lease_id})
    if v["budgets"] is None:      # lease budgets are turned off: the first set is kept, and follows the start date
        conn.execute("UPDATE garagebooks.lease_budget SET effective_from = %s WHERE lease_id = %s AND effective_from = %s",
                     (v["start_date"], lease_id, old["start_date"]))
        return "ok"
    conn.execute("DELETE FROM garagebooks.lease_budget WHERE lease_id = %s AND effective_from = %s",
                 (lease_id, old["start_date"]))
    _save_budgets(conn, lease_id, v["start_date"], v["budgets"])
    return "ok"


def delete_lease(conn, car_id, lease_id):
    return conn.execute("DELETE FROM garagebooks.lease WHERE id = %s AND car_id = %s", (lease_id, car_id)).rowcount == 1


# ---- the FBT year report (roadmap L2) ---------------------------------------------------

def fbt_costs(conn, car_id, year, tz):
    """A car's expenses in one FBT year, by category: rows of category, kind, budget_pool, total and count.
    The year is decided by each expense's date in time zone `tz`, through garagebooks.fbt_year()."""
    return conn.execute(
        "SELECT c.name AS category, c.kind, c.budget_pool, c.sort_order, SUM(e.amount) AS total, count(*) AS n "
        "FROM garagebooks.expense e JOIN garagebooks.category c ON c.id = e.category_id "
        "WHERE e.car_id = %(car)s AND garagebooks.fbt_year((e.occurred_at AT TIME ZONE %(tz)s)::date) = %(year)s "
        "GROUP BY c.id ORDER BY c.sort_order, c.name", {"car": car_id, "year": year, "tz": tz}).fetchall()


def fbt_years_with_expenses(conn, car_id, tz):
    """The FBT years (the year each starts in) in which this car has an expense, oldest first."""
    return [r["y"] for r in conn.execute(
        "SELECT DISTINCT garagebooks.fbt_year((occurred_at AT TIME ZONE %(tz)s)::date) AS y "
        "FROM garagebooks.expense WHERE car_id = %(car)s ORDER BY 1", {"car": car_id, "tz": tz}).fetchall()]


# ---- reminders (roadmap item 4) --------------------------------------------------------

REMINDER_FIELDS = ("car_id", "title", "category_id", "service_item_id", "due_date", "due_km", "lead_days", "lead_km",
                   "every_months", "every_km", "notes")


def open_reminders(conn, car_id=None):
    """Every open reminder, stored and derived from a lease (see the reminder_due view), with the car's
    name, the category's name and, for a stored one, its notes and recurrence. Not sorted."""
    return conn.execute(
        "SELECT d.*, c.name AS car_name, cat.name AS category, si.name AS service_item, r.notes, r.every_months, "
        "       r.every_km, r.edited_at, NULL::timestamptz AS resolved_at "
        "FROM garagebooks.reminder_due d JOIN garagebooks.car c ON c.id = d.car_id "
        "LEFT JOIN garagebooks.category cat ON cat.id = d.category_id "
        "LEFT JOIN garagebooks.service_item si ON si.id = d.service_item_id "
        "LEFT JOIN garagebooks.reminder r ON r.id = d.reminder_id "
        "WHERE (%(car)s::bigint IS NULL OR d.car_id = %(car)s)", {"car": car_id}).fetchall()


def get_reminder(conn, reminder_id, lock=False):
    """One stored reminder, or None. lock=True holds it until commit, so two resolutions at once can't both succeed."""
    return conn.execute("SELECT * FROM garagebooks.reminder WHERE id = %s" + (" FOR UPDATE" if lock else ""),
                        (reminder_id,)).fetchone()


def add_reminder(conn, v):
    """Add a reminder. `v` has the REMINDER_FIELDS (and optionally previous_id). Returns its id."""
    cols = REMINDER_FIELDS + ("previous_id",)
    return conn.execute(
        f"INSERT INTO garagebooks.reminder ({', '.join(cols)}) VALUES ({', '.join('%(' + c + ')s' for c in cols)}) RETURNING id",
        {"previous_id": None, **v}).fetchone()["id"]


def update_reminder(conn, reminder_id, v):
    """Change an open reminder (its car stays). False if there is none."""
    sets = ", ".join(f"{f} = %({f})s" for f in REMINDER_FIELDS if f != "car_id")
    return conn.execute(f"UPDATE garagebooks.reminder SET {sets}, edited_at = now() "
                        "WHERE id = %(id)s AND resolved_at IS NULL", {**v, "id": reminder_id}).rowcount == 1


def delete_reminder(conn, reminder_id):
    """Delete a reminder that is still open: a resolved one is kept. False if there is none."""
    return conn.execute("DELETE FROM garagebooks.reminder WHERE id = %s AND resolved_at IS NULL",
                        (reminder_id,)).rowcount == 1


def mark_resolved(conn, reminder_id, resolved_at, expense_id, km):
    conn.execute("UPDATE garagebooks.reminder SET resolved_at = %s, resolved_expense_id = %s, resolved_km = %s WHERE id = %s",
                 (resolved_at, expense_id, km, reminder_id))


def mark_open(conn, reminder_id):
    conn.execute("UPDATE garagebooks.reminder SET resolved_at = NULL, resolved_expense_id = NULL, resolved_km = NULL "
                 "WHERE id = %s", (reminder_id,))


def following_reminder(conn, reminder_id):
    """The reminder created when this one was resolved, or None."""
    return conn.execute("SELECT * FROM garagebooks.reminder WHERE previous_id = %s ORDER BY id LIMIT 1",
                        (reminder_id,)).fetchone()


def resolved_reminders(conn, limit, offset=0):
    """-> (rows, total): resolved reminders, newest resolution first, with the expense that did the job."""
    rows = conn.execute(
        "SELECT r.id, r.title, r.car_id, c.name AS car_name, r.resolved_at, r.resolved_km, r.resolved_expense_id, "
        "       e.occurred_at AS expense_at, e.vendor, e.amount, cat.name AS expense_category "
        "FROM garagebooks.reminder r JOIN garagebooks.car c ON c.id = r.car_id "
        "LEFT JOIN garagebooks.expense e ON e.id = r.resolved_expense_id "
        "LEFT JOIN garagebooks.category cat ON cat.id = e.category_id "
        "WHERE r.resolved_at IS NOT NULL ORDER BY r.resolved_at DESC, r.id DESC LIMIT %s OFFSET %s", (limit, offset)).fetchall()
    total = conn.execute("SELECT count(*) AS n FROM garagebooks.reminder WHERE resolved_at IS NOT NULL").fetchone()["n"]
    return rows, total


def stored_open_reminders(conn, car_id):
    """A car's open reminders that can be resolved (not the ones derived from its lease)."""
    return conn.execute("SELECT r.id, r.title, r.category_id, r.service_item_id, r.due_date, r.due_km FROM garagebooks.reminder r "
                        "WHERE r.car_id = %s AND r.resolved_at IS NULL ORDER BY r.due_date NULLS LAST, r.id",
                        (car_id,)).fetchall()


def recent_expenses(conn, car_id, limit=20):
    """A car's latest expenses, for choosing the one that resolves a reminder."""
    return conn.execute(f"{_SELECT} WHERE e.car_id = %s ORDER BY e.occurred_at DESC, e.id DESC LIMIT %s",
                        (car_id, limit)).fetchall()
# ---- change history ------------------------------------------------------------

HISTORY_PAGE = 50
HISTORY_TABLES = ("expense", "category", "expense_claim")
HISTORY_OPERATIONS = ("insert", "update", "delete")


def history(conn, car_id=None, table=None, operation=None, page=1):
    """One page of the change log, newest first, filtered. -> (entries, page, pages). A car filter
    means that car's expenses (a category belongs to no car). The page is clamped to the last one."""
    where, params = [], {}
    if car_id:
        where.append("table_name = 'expense' AND COALESCE(after_row, before_row) ->> 'car_id' = %(car)s")
        params["car"] = str(car_id)
    if table in HISTORY_TABLES:
        where.append("table_name = %(table)s")
        params["table"] = table
    if operation in HISTORY_OPERATIONS:
        where.append("operation = %(operation)s")
        params["operation"] = operation
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    total = conn.execute(f"SELECT count(*) AS n FROM garagebooks.change_log {clause}", params).fetchone()["n"]
    pages = max(1, -(-total // HISTORY_PAGE))
    page = min(max(page, 1), pages)
    entries = conn.execute(
        f"SELECT id, changed_at, actor, table_name, row_id, operation, before_row, after_row "
        f"FROM garagebooks.change_log {clause} ORDER BY changed_at DESC, id DESC LIMIT %(limit)s OFFSET %(offset)s",
        {**params, "limit": HISTORY_PAGE, "offset": (page - 1) * HISTORY_PAGE}).fetchall()
    return entries, page, pages


# ---- claims (roadmap L3: what was sent to the salary packaging provider) ------------------------------

def lease_spans(conn):
    """-> {car_id: [(start_date, end_date)]} of every lease, both ends included: which days a lease covers."""
    spans = {}
    for row in conn.execute("SELECT car_id, start_date, end_date FROM garagebooks.lease"):
        spans.setdefault(row["car_id"], []).append((row["start_date"], row["end_date"]))
    return spans


CLAIM_FIELDS = ("provider", "provider_id", "reference", "status", "amount", "gst", "fields")


def add_claim(conn, expense_id, v):
    """Record that an expense was claimed. False if it already has a claim (nothing is changed then)."""
    cur = conn.execute(
        f"INSERT INTO garagebooks.expense_claim (expense_id, {', '.join(CLAIM_FIELDS)}) "
        f"VALUES (%(expense_id)s, {', '.join('%(' + f + ')s' for f in CLAIM_FIELDS)}) ON CONFLICT DO NOTHING",
        {"gst": None, "fields": None, **v, "expense_id": expense_id})
    return cur.rowcount == 1


def claim_rows(conn):
    """Every recorded claim with its expense, newest first, for the claims page."""
    return conn.execute(
        "SELECT cl.*, e.occurred_at, e.amount AS expense_amount, e.vendor, e.car_id, c.name AS car_name,"
        " cat.name AS category FROM garagebooks.expense_claim cl"
        " JOIN garagebooks.expense e ON e.id = cl.expense_id JOIN garagebooks.car c ON c.id = e.car_id"
        " JOIN garagebooks.category cat ON cat.id = e.category_id ORDER BY cl.submitted_at DESC, cl.expense_id DESC"
    ).fetchall()


def save_claim_status(conn, provider_id, status, reference=None, approved_amount=None, paid_date=None):
    """What a status refresh learned about a claim. The provider's reference is only filled in when it
    was missing. -> the number of rows changed."""
    return conn.execute(
        "UPDATE garagebooks.expense_claim SET status = %s, reference = COALESCE(reference, %s),"
        " approved_amount = %s, paid_date = %s, status_checked_at = now() WHERE provider_id = %s",
        (status, reference, approved_amount, paid_date, provider_id)).rowcount


def touch_claim_check(conn, provider_id):
    conn.execute("UPDATE garagebooks.expense_claim SET status_checked_at = now() WHERE provider_id = %s",
                 (provider_id,))


def delete_claim(conn, expense_id):
    return conn.execute("DELETE FROM garagebooks.expense_claim WHERE expense_id = %s", (expense_id,)).rowcount == 1


# ---- the provider's transaction history (a cache of what the claims connector says) ---------------------------

TRANSACTION_COLUMNS = ("provider_id", "occurred_on", "amount", "gst", "type", "status", "description", "payee",
                       "kind", "car_id", "claim_provider_id", "reference")


def save_transactions(conn, provider, rows, since=None):
    """Make the cache the connector's current answer for the period asked about (from `since`, or all of it): add
    or refresh these rows (its key is what makes a row the same one) and drop any in that period it no longer
    reports, or reported under another name. -> how many."""
    conn.execute("DELETE FROM garagebooks.provider_transaction WHERE provider <> %s"
                 " OR (occurred_on >= COALESCE(%s::date, '-infinity') AND NOT (provider_id = ANY(%s)))",
                 (provider, since, [r["provider_id"] for r in rows]))
    for row in rows:
        conn.execute(
            f"INSERT INTO garagebooks.provider_transaction (provider, {', '.join(TRANSACTION_COLUMNS)}) "
            f"VALUES (%(provider)s, {', '.join('%(' + c + ')s' for c in TRANSACTION_COLUMNS)}) "
            "ON CONFLICT (provider, provider_id) DO UPDATE SET "
            + ", ".join(f"{c} = EXCLUDED.{c}" for c in TRANSACTION_COLUMNS[1:]) + ", synced_at = now()",
            {**row, "provider": provider})
    return len(rows)


CLAIM_LINE_COLUMNS = ("reference", "occurred_on", "amount", "approved_amount", "status", "paid_date", "description", "car_id")


def save_claim_lines(conn, provider, rows, since=None):
    """Make the cached claim lines the connector's current answer for the period (like save_transactions)."""
    conn.execute("DELETE FROM garagebooks.provider_claim WHERE provider <> %s"
                 " OR (occurred_on >= COALESCE(%s::date, '-infinity')"
                 "     AND NOT ((provider_id, line_id) IN (SELECT * FROM unnest(%s::text[], %s::text[]))))",
                 (provider, since, [r["provider_id"] for r in rows], [r["line_id"] for r in rows]))
    for row in rows:
        conn.execute(
            f"INSERT INTO garagebooks.provider_claim (provider, provider_id, line_id, {', '.join(CLAIM_LINE_COLUMNS)}) "
            f"VALUES (%(provider)s, %(provider_id)s, %(line_id)s, {', '.join('%(' + c + ')s' for c in CLAIM_LINE_COLUMNS)}) "
            "ON CONFLICT (provider, provider_id, line_id) DO UPDATE SET "
            + ", ".join(f"{c} = EXCLUDED.{c}" for c in CLAIM_LINE_COLUMNS) + ", synced_at = now()",
            {**row, "provider": provider})
    return len(rows)


# How closely a provider's claim line, or a payment it made itself, must agree with an expense to be taken as the same
# thing, tried in this order (each only pairs what is unambiguous): the same day and amount; the same amount within a
# month (the provider dates a claim from its own form, not from the invoice); a direct payment within two weeks and
# 15% (its amount carries the provider's own fees).
CLAIM_TIERS = ((0, "0"), (30, "0"))
DIRECT_TIER = (14, "0.15")


def rematch(conn, tz, match_by_figures):
    """Line the provider's history up with the expenses again. A claim line belongs to the expense claimed from
    here with its claim id, else to the one expense that fits its car, day and amount (see CLAIM_TIERS). A payment
    follows its claim to that expense, so a part payment is no problem. A payment the provider made itself with no
    claim belongs to the one expense that fits it (DIRECT_TIER). Per-km charging and other rows never match: they
    have no expense."""
    conn.execute("UPDATE garagebooks.provider_claim SET expense_id = NULL WHERE expense_id IS NOT NULL")
    conn.execute("UPDATE garagebooks.provider_transaction SET expense_id = NULL WHERE expense_id IS NOT NULL")
    conn.execute(
        "UPDATE garagebooks.provider_claim pc SET expense_id = cl.expense_id FROM garagebooks.expense_claim cl"
        " WHERE pc.provider_id = cl.provider_id")

    def free_expenses():
        return [(r["id"], r["car_id"], r["day"], r["amount"]) for r in conn.execute(
            "SELECT e.id, e.car_id, (e.occurred_at AT TIME ZONE %s)::date AS day, e.amount FROM garagebooks.expense e"
            " WHERE NOT EXISTS (SELECT 1 FROM garagebooks.expense_claim cl WHERE cl.expense_id = e.id)"
            " AND NOT EXISTS (SELECT 1 FROM garagebooks.provider_claim pc WHERE pc.expense_id = e.id)"
            " AND NOT EXISTS (SELECT 1 FROM garagebooks.provider_transaction pt WHERE pt.expense_id = e.id)", (tz,))]

    from decimal import Decimal
    for days, tolerance in CLAIM_TIERS:
        lines = [(r["id"], r["car_id"], r["occurred_on"], r["amount"]) for r in conn.execute(
            "SELECT id, car_id, occurred_on, amount FROM garagebooks.provider_claim WHERE expense_id IS NULL")]
        for lid, eid in match_by_figures(lines, free_expenses(), days, Decimal(tolerance)).items():
            conn.execute("UPDATE garagebooks.provider_claim SET expense_id = %s WHERE id = %s", (eid, lid))
    payments = [(r["id"], r["car_id"], r["occurred_on"], r["amount"]) for r in conn.execute(
        "SELECT id, car_id, occurred_on, amount FROM garagebooks.provider_transaction"
        " WHERE kind = 'direct_payment' AND claim_provider_id IS NULL")]
    for pid, eid in match_by_figures(payments, free_expenses(), DIRECT_TIER[0], Decimal(DIRECT_TIER[1])).items():
        conn.execute("UPDATE garagebooks.provider_transaction SET expense_id = %s WHERE id = %s", (eid, pid))
    conn.execute(
        "UPDATE garagebooks.provider_transaction pt SET expense_id = x.expense_id FROM"
        " (SELECT provider_id, min(expense_id) AS expense_id FROM garagebooks.provider_claim"
        "  WHERE expense_id IS NOT NULL GROUP BY provider_id) x"
        " WHERE x.provider_id = pt.claim_provider_id AND pt.kind = 'claim_payment'")
    conn.execute(
        "UPDATE garagebooks.provider_transaction pt SET expense_id = cl.expense_id FROM garagebooks.expense_claim cl"
        " WHERE pt.expense_id IS NULL AND cl.provider_id = pt.claim_provider_id AND pt.kind = 'claim_payment'")


def direct_payments(conn):
    """-> {expense_id: row}: the payments the provider made itself that were matched to an expense."""
    return {r["expense_id"]: r for r in conn.execute(
        "SELECT expense_id, occurred_on, amount, description FROM garagebooks.provider_transaction"
        " WHERE kind = 'direct_payment' AND expense_id IS NOT NULL ORDER BY occurred_on").fetchall()}


def reimbursements(conn):
    """-> {expense_id: {claim, reference, expected, received}}: for each expense with a claim in the provider's
    history, what the whole claim should pay (the approved amounts, else the amounts claimed) and what the
    payments in the history say was paid for it."""
    rows = conn.execute(
        "SELECT l.expense_id, c.provider_id AS claim, c.reference, c.expected, COALESCE(p.received, 0) AS received"
        " FROM (SELECT DISTINCT expense_id, provider_id FROM garagebooks.provider_claim WHERE expense_id IS NOT NULL) l"
        " JOIN (SELECT provider_id, min(reference) AS reference, sum(COALESCE(approved_amount, amount)) AS expected"
        "       FROM garagebooks.provider_claim GROUP BY provider_id) c ON c.provider_id = l.provider_id"
        " LEFT JOIN (SELECT claim_provider_id, sum(abs(amount)) AS received FROM garagebooks.provider_transaction"
        "            WHERE kind = 'claim_payment' AND claim_provider_id IS NOT NULL GROUP BY claim_provider_id) p"
        "   ON p.claim_provider_id = l.provider_id").fetchall()
    return {r["expense_id"]: r for r in rows}


def transaction_rows(conn, car_id=None, start=None, end=None, match=None):
    """The cached transactions, newest first, with the expense each belongs to. `match`: 'matched', 'unmatched'
    (a claim payment or direct payment with no expense), 'per_km' or 'other' (deductions and the like, which belong to
    no expense). With none of them the list leaves out 'other'."""
    where, params = [], []
    if car_id:
        where.append("pt.car_id = %s")
        params.append(car_id)
    if start:
        where.append("pt.occurred_on >= %s")
        params.append(start)
    if end:
        where.append("pt.occurred_on <= %s")
        params.append(end)
    where.append({"matched": "pt.expense_id IS NOT NULL",
                  "unmatched": "pt.expense_id IS NULL AND pt.kind IN ('claim_payment', 'direct_payment')",
                  "per_km": "pt.kind = 'per_km_charging'",
                  "other": "pt.kind = 'other'"}.get(match, "pt.kind <> 'other'"))
    return conn.execute(
        "SELECT pt.*, c.name AS car_name, cat.name AS expense_category, e.vendor AS expense_vendor"
        " FROM garagebooks.provider_transaction pt LEFT JOIN garagebooks.car c ON c.id = pt.car_id"
        " LEFT JOIN garagebooks.expense e ON e.id = pt.expense_id"
        " LEFT JOIN garagebooks.category cat ON cat.id = e.category_id"
        f" WHERE {' AND '.join(where)} ORDER BY pt.occurred_on DESC, pt.id DESC", params).fetchall()


def last_synced(conn):
    """When the provider's history was last fetched, or None if it never has been (or it was empty)."""
    return conn.execute(
        "SELECT max(at) AS at FROM (SELECT synced_at AS at FROM garagebooks.provider_transaction"
        " UNION ALL SELECT synced_at FROM garagebooks.provider_claim) x").fetchone()["at"]


def history_since(conn, tz, today, margin_days=90, full=False):
    """The date to fetch the provider's history from. The first time, back to the earliest expense (a month
    before it); after that only the last few months, which is what a sync needs to catch up and is quick."""
    latest = conn.execute(
        "SELECT max(d) AS d FROM (SELECT max(occurred_on) AS d FROM garagebooks.provider_transaction"
        " UNION ALL SELECT max(occurred_on) FROM garagebooks.provider_claim) x").fetchone()["d"]
    if latest and not full:
        return latest - timedelta(days=margin_days)
    first = conn.execute("SELECT (min(occurred_at) AT TIME ZONE %s)::date AS d FROM garagebooks.expense", (tz,)).fetchone()["d"]
    return (first - timedelta(days=30)) if first else today - timedelta(days=365)


def sync_state(conn):
    """-> the one provider_sync row (state, started_at, finished_at, outcome, message, since). 'running' for more
    than half an hour counts as dead."""
    row = conn.execute(
        "SELECT state, started_at, finished_at, outcome, message, since,"
        " (state = 'running' AND started_at > now() - interval '30 minutes') AS running"
        " FROM garagebooks.provider_sync WHERE id = 1").fetchone()
    return row


def begin_sync(conn, since):
    """Claim the right to run a sync. False if one is already running."""
    return conn.execute(
        "UPDATE garagebooks.provider_sync SET state = 'running', started_at = now(), finished_at = NULL,"
        " outcome = NULL, message = NULL, since = %s"
        " WHERE state <> 'running' OR started_at < now() - interval '30 minutes'", (since,)).rowcount == 1


def end_sync(conn, outcome, message=None):
    conn.execute("UPDATE garagebooks.provider_sync SET state = 'idle', finished_at = now(), outcome = %s, message = %s"
                 " WHERE id = 1", (outcome, message))
