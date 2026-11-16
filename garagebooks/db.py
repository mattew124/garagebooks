"""All SQL lives here. One short-lived connection per request — traffic is one
person entering the odd invoice, a pool would be machinery for nothing."""

import logging
import os
import re
import time
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


def car_details(conn):
    """The cars as the Cars page shows them: with how many expenses each has and its readings entered by hand."""
    rows = conn.execute(
        "SELECT c.id, c.name, c.kind, c.make, c.model, c.teslamate_car_id, c.sold_at, c.sale_price,"
        " (SELECT count(*) FROM garagebooks.expense e WHERE e.car_id = c.id) AS expenses,"
        " (SELECT count(*) FROM garagebooks.lease l WHERE l.car_id = c.id) AS leases "
        "FROM garagebooks.car c ORDER BY c.id").fetchall()
    for row in rows:
        row["readings"] = conn.execute(
            "SELECT id, read_at, km FROM garagebooks.odometer_reading WHERE car_id = %s "
            "ORDER BY read_at DESC LIMIT 5", (row["id"],)).fetchall()
    return rows


def add_car(conn, v):
    """A car entered by hand. Returns its id."""
    return conn.execute(
        "INSERT INTO garagebooks.car (name, kind, make, model) VALUES (%(name)s, %(kind)s, %(make)s, %(model)s) "
        "RETURNING id", v).fetchone()["id"]


def update_car(conn, car_id, v):
    """Returns False when there is no such car. A TeslaMate car renamed here keeps the new name."""
    return conn.execute(
        "UPDATE garagebooks.car SET kind = %(kind)s, make = %(make)s, model = %(model)s, "
        "name_from_teslamate = name_from_teslamate AND name = %(name)s, name = %(name)s WHERE id = %(id)s",
        {**v, "id": car_id}).rowcount == 1


def set_sale(conn, car_id, sold_at, sale_price):
    """Record the sale of a car, or clear it (both None). Returns False when there is no such car."""
    return conn.execute("UPDATE garagebooks.car SET sold_at = %s, sale_price = %s WHERE id = %s",
                        (sold_at, sale_price, car_id)).rowcount == 1


def first_expense_at(conn, car_id):
    return conn.execute("SELECT MIN(occurred_at) AS at FROM garagebooks.expense WHERE car_id = %s", (car_id,)).fetchone()["at"]


def delete_car(conn, car_id):
    """Only a car added by hand that has no expenses. Returns whether it was deleted."""
    return conn.execute(
        "DELETE FROM garagebooks.car c WHERE c.id = %s AND c.teslamate_car_id IS NULL "
        "AND NOT EXISTS (SELECT 1 FROM garagebooks.expense e WHERE e.car_id = c.id)", (car_id,)).rowcount == 1


def add_reading(conn, car_id, read_at, km):
    """An odometer reading entered by hand; one at the same moment is replaced."""
    conn.execute("INSERT INTO garagebooks.odometer_reading (car_id, read_at, km) VALUES (%s, %s, %s) "
                 "ON CONFLICT (car_id, read_at) DO UPDATE SET km = EXCLUDED.km", (car_id, read_at, km))


def delete_reading(conn, car_id, reading_id):
    return conn.execute("DELETE FROM garagebooks.odometer_reading WHERE id = %s AND car_id = %s",
                        (reading_id, car_id)).rowcount == 1


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


def get_settings(conn):
    row = conn.execute("SELECT app_url, grafana_url, invoice_date_format, invoice_name_format, multi_amount "
                       "FROM garagebooks.setting WHERE id = 1").fetchone()
    return row or {"app_url": None, "grafana_url": None, "invoice_date_format": None,
                   "invoice_name_format": None, "multi_amount": None}


def save_settings(conn, app_url, grafana_url, invoice_date_format, invoice_name_format=None, multi_amount=None):
    conn.execute("UPDATE garagebooks.setting SET app_url = %s, grafana_url = %s, "
                 "invoice_date_format = %s, invoice_name_format = %s, multi_amount = %s WHERE id = 1",
                 (app_url, grafana_url, invoice_date_format, invoice_name_format, multi_amount))


def categories(conn):
    return conn.execute(
        "SELECT id, name, kind, upload_folder, budget_pool FROM garagebooks.category ORDER BY sort_order, name"
    ).fetchall()


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


def set_upload_folder(conn, category_id, folder):
    """Returns False when there is no such category."""
    return conn.execute("UPDATE garagebooks.category SET upload_folder = %s WHERE id = %s",
                        (folder, category_id)).rowcount == 1


_SELECT = """
    SELECT e.id, e.car_id, c.name AS car_name, e.category_id, cat.name AS category,
           cat.kind, e.occurred_at, e.amount, e.odometer_km, e.vendor, e.notes,
           e.invoice_url, e.invoice_path
    FROM garagebooks.expense e
    JOIN garagebooks.car c ON c.id = e.car_id
    JOIN garagebooks.category cat ON cat.id = e.category_id
"""


# What the list can be sorted by: the name in the address -> the SQL it means. Never built from user text.
SORTS = {"date": "e.occurred_at", "category": "cat.name", "vendor": "NULLIF(lower(e.vendor), '')",
         "amount": "e.amount", "odometer": "e.odometer_km"}


def list_expenses(conn, category_id=None, limit=1000, sort="date", direction="desc"):
    """Sorted either way. Blank vendors and odometers always come last, whichever way round."""
    order = SORTS.get(sort, SORTS["date"])
    way = "ASC" if direction == "asc" else "DESC"
    where, params = "", []
    if category_id:
        where = "WHERE e.category_id = %s"
        params.append(category_id)
    params.append(limit)
    return conn.execute(
        f"{_SELECT} {where} ORDER BY {order} {way} NULLS LAST, e.occurred_at {way}, e.id {way} LIMIT %s", params
    ).fetchall()


def totals(conn, category_id=None):
    where, params = "", []
    if category_id:
        where = "WHERE category_id = %s"
        params.append(category_id)
    return conn.execute(
        f"SELECT COALESCE(SUM(amount), 0) AS total, COUNT(*) AS n FROM garagebooks.expense {where}",
        params,
    ).fetchone()


def get_expense(conn, expense_id):
    return conn.execute(f"{_SELECT} WHERE e.id = %s", (expense_id,)).fetchone()


_FIELDS = ("car_id", "category_id", "occurred_at", "amount", "odometer_km",
           "vendor", "notes", "invoice_url", "invoice_path")


def insert_expense(conn, v):
    return conn.execute(
        f"INSERT INTO garagebooks.expense ({', '.join(_FIELDS)}) "
        f"VALUES ({', '.join('%(' + f + ')s' for f in _FIELDS)}) RETURNING id",
        {"invoice_path": None, **v},
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
                        "UNION SELECT invoice_path FROM garagebooks.expense WHERE invoice_path = ANY(%(refs)s)",
                        {"refs": list(refs)}).fetchall()
    return {r["ref"] for r in rows}


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
        {"invoice_path": None, **v, "id": expense_id},
    )
    return cur.rowcount == 1


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



# ---- leases (roadmap L1) -------------------------------------------------------------

LEASE_FIELDS = ("package_name", "financier", "start_date", "end_date", "refinance_date", "term_months",
                "pay_frequency", "pretax_payment", "posttax_payment", "admin_fee", "car_cost",
                "residual_amount", "residual_percent", "fbt_base_value", "fbt_method", "statutory_percent",
                "ev_exempt", "tax_treatment", "tax_treatment_note", "estimated_km_per_year",
                "rego_expiry", "rego_renewed_by_provider")


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
    _save_budgets(conn, lease_id, v["start_date"], v["budgets"])
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


def set_budget_pool(conn, category_id, pool):
    """Which of the provider's budget pools a category's expenses come from (None: none). False if no such category."""
    return conn.execute("UPDATE garagebooks.category SET budget_pool = %s WHERE id = %s", (pool, category_id)).rowcount == 1
