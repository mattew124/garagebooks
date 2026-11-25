"""GarageBooks: a small web UI for recording what the car costs to own.

Data lives in the `garagebooks` schema, in TeslaMate's database where there is a TeslaMate;
the Grafana dashboard in grafana/ turns it (plus TeslaMate's own charging costs, when it has
them) into total cost of ownership. TeslaMate is optional: see sources.py.
"""

import functools
import io
import hashlib
import hmac
import json
import logging
import os
import re
import threading
from pathlib import Path
import time
from decimal import Decimal
from datetime import date, datetime, time as dtime, timedelta, timezone
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from flask import (Flask, abort, g, has_request_context, jsonify, make_response, redirect, render_template, request,
                   send_file, url_for)

from psycopg import errors as pg_errors
from werkzeug.exceptions import RequestEntityTooLarge
from werkzeug.middleware.proxy_fix import ProxyFix

from . import ato, claims as claim_lib, dashboard, db, docinfo, fuel, icons, invoices, jobs, jobtext, lease, leaseform, logs, notices, notify, onedrive, outlook, reminders, sources, storage, tax, updates, validation

log = logging.getLogger("garagebooks.app")

MESSAGES = {
    "saved": "Expense saved.",
    "updated": "Expense updated.",
    "deleted": "Expense deleted.",
    "notify_saved": "Notification settings saved.",
    "saved_uploaded": "Expense saved. The invoice was saved in the car's folder and linked.",
    "updated_uploaded": "Expense updated. The invoice was saved in the car's folder and linked.",
    "updated_replaced": "Expense updated. It now points to the new invoice; the previous file is still "
                        "where it was.",
    "saved_not_uploaded": "Expense saved, but the invoice PDF was not uploaded: nowhere is set up for this "
                          "car's invoices. Choose its folder on the Invoice storage page, then edit the expense "
                          "to upload it.",
    "updated_not_uploaded": "Expense updated, but the invoice PDF was not uploaded: nowhere is set up for "
                            "this car's invoices. Choose its folder on the Invoice storage page, then upload "
                            "it again.",
    "category": "Category added.",
    "car_added": "Car added.",
    "car_saved": "Car saved.",
    "car_deleted": "Car deleted.",
    "car_sold": "Sale saved.",
    "car_sold_early": "Sale saved. The sale date is before the car's first expense, so its figures may look odd.",
    "car_unsold": "Sale cleared.",
    "lease_added": "Lease added.",
    "lease_saved": "Lease saved.",
    "lease_added_not_uploaded": "Lease added. The PDF wasn't kept: choose a folder for this car on the Invoice storage page first.",
    "lease_saved_not_uploaded": "Lease saved. The PDF wasn't kept: choose a folder for this car on the Invoice storage page first.",
    "lease_deleted": "Lease deleted.",
    "reminder_added": "Reminder added.",
    "reminder_saved": "Reminder saved.",
    "reminder_deleted": "Reminder deleted.",
    "reminder_resolved": "Reminder resolved.",
    "reminder_resolved_next": "Reminder resolved, and the next one is added.",
    "reminder_resolved_exists": "Reminder resolved. The next one already exists, so no other was added.",
    "reminder_resolved_no_odometer": "Reminder resolved. No odometer is known to count the next one from, so it "
                                     "wasn't created: add it yourself.",
    "reminder_reopened": "Reminder reopened. The next one it had added was removed.",
    "reminder_reopened_kept": "Reminder reopened. The next one had been edited or resolved, so it was left as it is.",
    "categories_saved": "Changes saved.",
    "service_added": "Service item added.",
    "lease_document_kept": "Kept the file with the lease. It stays where it is, with its own name.",
    "service_deleted": "Service item removed. Expenses that had it keep their other items.",
    "settings": "Settings saved.",
    "folders": "Folders saved.",
    "connected": "OneDrive connected.",
    "credentials": "App registration saved. Now connect OneDrive or the mailbox.",
    "disconnected": "OneDrive disconnected.",
    "mail_connected": "Mailbox connected. Now choose a mailbox folder for each car.",
    "mail_disconnected": "Mailbox disconnected.",
    "imported": "Invoices imported.",
    "learned_deleted": "Removed.",
    "renewal_requested": "Renewal request sent to the provider. It pays the registration: check it has before it's due.",
    "claim_forgotten": "Claim record removed. The provider still has the claim, if one was made there.",
    "claims_refreshed": "Claim statuses refreshed.",
    "claims_synced": "Provider history synced.",
    "claims_sync_running": "A sync is already running.",
    "claims_signin": "Sign in to the provider first, then sync again.",
    "claims_none": "No claims are waiting on the provider: nothing to refresh.",
}

# PDFs read per page without JavaScript: each is downloaded and text-extracted (OCR for scans) while
# the request waits, so a big folder is worked through in rounds. The normal page reads them all, one request each.
TESLAMATE_RECHECK_SECONDS = 60   # how often the app looks again at whether TeslaMate is there and readable
TOKEN_MARGIN_SECONDS = 120   # a kept access token is dropped this long before it expires
IMPORT_BATCH = 10
TAX_PEOPLE = ("employee", "associate")   # whose tax bracket Settings can take, by income year (the associate's only
                                         # shows once a car has an associate lease)
LEASE_FOLDER = "Lease"       # the subfolder of a car's folder that its lease documents are saved in
# ...and stop reading more files once a round has taken this long (OCR can take 10+ seconds a file,
# and a request that outlasts gunicorn's timeout (180 s, in gunicorn.conf.py) is killed), leaving the rest for the next round.
IMPORT_BUDGET_SECONDS = 75
# Fields of an import row compared with what the owner submits, to learn from corrections.
LEARN_FIELDS = ("date", "category_id", "vendor", "amount", "odometer_km")
# The places invoices can be kept, as the pages name them. All are set up on the one Invoice storage
# page, each in its own card (templates/_storage_<kind>.html); another provider is another entry here,
# a store for it in storage.py and its card.
STORES = {
    "local": {"label": "the invoice folder", "title": "Folder on this server", "short": "server folder",
              "where": "the car's invoice folder",
              "choose_first": "Choose a folder for this car on the Invoice storage page first."},
    "onedrive": {"label": "OneDrive", "title": "OneDrive", "short": "OneDrive folder",
                 "where": "the car's OneDrive folder",
                 "choose_first": "Choose a OneDrive folder for this car on the Invoice storage page first."},
}
# Where invoices can be imported from: every storage, and an Outlook mailbox folder (outlook.py), which
# is only read. A car's mailbox folder is chosen on the same page but kept apart from its storage folder.
SOURCES = {**STORES, "mail": {
    "label": "Outlook", "title": "Outlook mailbox", "short": "mailbox folder", "where": "the car's mailbox folder",
    "choose_first": "Choose a mailbox folder for this car on the Invoice storage page first."}}


class PrefixMiddleware:
    """Serve under BASE_PATH (e.g. /garagebooks) without the proxy stripping it.
    /healthz stays reachable unprefixed for the container healthcheck."""

    def __init__(self, wsgi, prefix):
        self.wsgi, self.prefix = wsgi, prefix.rstrip("/")

    def __call__(self, environ, start_response):
        path = environ.get("PATH_INFO", "")
        if not self.prefix or path == "/healthz":
            return self.wsgi(environ, start_response)
        if path == self.prefix or path.startswith(self.prefix + "/"):
            environ["SCRIPT_NAME"] = self.prefix
            environ["PATH_INFO"] = path[len(self.prefix):] or "/"
            return self.wsgi(environ, start_response)
        start_response("404 Not Found", [("Content-Type", "text/plain")])
        return [b"Not found\n"]


def ref_category_ids(categories):
    """The categories whose expenses keep a notice's payment references: those paid from the registration pool."""
    return {c["id"] for c in categories if c["budget_pool"] == claim_lib.RENEWAL_POOL}


def create_app(connect=None, onedrive_transport=None, release_fetch=None, icon_fetch=None, background_jobs=True,
               notify_send=None, ato_fetch=None):
    """`connect` is injectable so tests can hand in a connection to a scratch DB,
    `onedrive_transport` so they can stand in for Microsoft, `release_fetch` for GitHub, `icon_fetch` for the provider icon's source, `notify_send` for the notification
    service (notify.send's signature) and `ato_fetch` for the ATO's pages (ato.get's signature).
    `background_jobs=False` is for the worker (`python -m garagebooks worker`), which runs the scheduled jobs itself."""
    app = Flask(__name__)
    logs.configure(app.logger, os.environ.get("LOG_LEVEL"))
    log = app.logger
    tz = os.environ.get("APP_TZ", "UTC")
    real_start = connect is None      # not a test handing in its own database
    if connect is None:
        connect = db.connect
        if os.environ.get("SKIP_MIGRATE") != "1":
            db.migrate()
        else:
            log.info("SKIP_MIGRATE=1: not touching the schema")
            with db.connect() as conn:
                if not db.has_schema(conn):
                    raise RuntimeError("SKIP_MIGRATE=1, but the 'garagebooks' schema in this database is from an "
                                       "older release. Run scripts/provision-db.sh again, then start the app.")
        with db.connect() as conn:
            db.save_app_tz(conn, tz)
    # Who made a change, for the change history. The sign-in gate in front of the app names the user in
    # a request header; AUTH_USER_HEADER says which. Unset (the default) the header is never read: it is
    # only trustworthy when the proxy strips it from client requests. Every database connection the
    # app opens passes it on, so each write path is covered, and it lasts for that transaction only.
    actor_header = (os.environ.get("AUTH_USER_HEADER") or "").strip()
    if actor_header:
        open_connection = connect

        def connect():
            conn = open_connection()
            actor = validation.parse_actor(request.headers.get(actor_header)) if has_request_context() else None
            if conn is not None and actor:
                conn.execute("SELECT set_config('garagebooks.actor', %s, true)", (actor,))
            return conn
    dashboard_dir = os.environ.get("DASHBOARD_DIR")
    allow_untested = os.environ.get("ALLOW_UNTESTED_TESLAMATE") == "1"
    # What is known about TeslaMate, looked at again every so often: it may be installed, upgraded
    # or repaired while the app runs. "view" is the form the ownership view was last put in.
    known = {"teslamate": sources.TeslaMate(sources.UNAVAILABLE, "The database has not been looked at yet."),
             "at": None, "view": None, "logged": None}

    def teslamate(conn=None, fresh=False):
        """TeslaMate's state (sources.TeslaMate). With a connection, and when it was last looked at over
        a minute ago (or fresh=True), look again: add its new cars to ours, and put the ownership view
        in the form that fits."""
        now = time.monotonic()
        if conn is None or not (fresh or known["at"] is None or now - known["at"] > TESLAMATE_RECHECK_SECONDS):
            return known["teslamate"]
        found = sources.detect(conn, allow_untested)
        known["teslamate"], known["at"] = found, now
        if (found.state, found.reason) != known["logged"]:
            known["logged"] = (found.state, found.reason)
            if found.state == sources.OK and found.reason:
                log.error("ALLOW_UNTESTED_TESLAMATE=1: using TeslaMate's figures anyway. %s", found.reason)
            elif found.state == sources.OK:
                log.info("TeslaMate: installed and within what was tested; its odometer and charging figures are used")
            elif found.state == sources.ABSENT:
                log.info("TeslaMate: not installed in this database; everything comes from what is entered by hand")
            else:
                log.warning("TeslaMate: %s; its figures are held back. %s", found.state, found.reason)
        sources.sync_cars(conn, found)
        if known["view"] != found.usable and db.build_ownership(conn, found.usable) is not None:
            known["view"] = found.usable
            db.build_odometer_at(conn, found.usable)
        return found

    def refresh_dashboard():
        """Write the dashboard with a time range that starts at the earliest data. Run on start and
        after every expense change, so a purchase dated before TeslaMate's first record shows at once."""
        if not dashboard_dir:
            return
        try:
            try:
                with connect() as conn:
                    start = sources.first_activity(conn, teslamate(conn))
            except Exception as exc:    # no data yet, or no database: Grafana's own default range then
                log.info("Dashboard time range left at its default (%s)", exc)
                start = None
            try:
                with connect() as conn:
                    sold = any(car["sold_at"] is not None for car in db.cars(conn))
            except Exception:           # noqa: BLE001 - no database yet: the panel is left out until a sale is known
                sold = False
            try:
                with connect() as conn:
                    # The cost-after-tax panels have nothing to show without a tax bracket chosen and a lease.
                    after_tax = db.any_tax_choice(conn) and bool(db.lease_car_ids(conn))
            except Exception:           # noqa: BLE001 - no database yet: left out until there is something to show
                after_tax = False
            try:
                with connect() as conn:
                    pace = db.any_estimated_km(conn)
            except Exception:           # noqa: BLE001 - no database yet: the panel is left out until a lease has an estimate
                pace = False
            try:
                with connect() as conn:
                    fuel_used = db.any_fuel(conn)
            except Exception:           # noqa: BLE001 - no database yet: the Fuel dashboard waits until a car uses fuel
                fuel_used = False
            wrote = dashboard.export(dashboard_dir, tz, os.environ.get("BASE_PATH", "").rstrip("/"), start=start,
                                     teslamate=known["teslamate"], sold=sold, pace=pace, after_tax=after_tax, fuel=fuel_used)
            log.info("Grafana dashboard %s in %s", "written" if wrote else "already up to date", dashboard_dir)
        except OSError as exc:
            log.warning("Could not write the Grafana dashboard to %s: %s", dashboard_dir, exc)

    try:
        with connect() as conn:
            teslamate(conn, fresh=True)
    except Exception as exc:    # noqa: BLE001 - no database yet: looked at again on the first request
        log.info("TeslaMate not looked for at start: %s", exc)
    refresh_dashboard()
    # Room for the largest PDF plus the rest of the form.
    app.config["MAX_CONTENT_LENGTH"] = validation.MAX_PDF_BYTES + 1024 * 1024
    # GARAGEBOOKS_ENCRYPTION_KEY turns OneDrive on; everything else is set up in the UI. Its name up
    # to 0.4.0-rc1, EXPENSES_ENCRYPTION_KEY, still works, so an upgrade doesn't turn OneDrive off.
    drive = None
    key = os.environ.get("GARAGEBOOKS_ENCRYPTION_KEY") or os.environ.get("EXPENSES_ENCRYPTION_KEY")
    if key:
        drive = onedrive.OneDrive(key, onedrive_transport)
    elif os.environ.get("ONEDRIVE_TOKEN_KEY"):
        log.warning("ONEDRIVE_TOKEN_KEY was renamed GARAGEBOOKS_ENCRYPTION_KEY; OneDrive stays off until it is set.")
    # The mailbox uses OneDrive's app registration and encryption key, with a sign-in of its own.
    mailbox = outlook.Mailbox(drive) if drive else None
    # INVOICES_DIR turns on keeping invoices in a folder on the server: a host folder (a NAS share,
    # say) mounted into the container. The app serves those PDFs itself, at /invoice/<id>.
    local = None
    invoices_dir = os.environ.get("INVOICES_DIR", "").strip()
    # INVOICES_REQUIRED=1: don't run without the folder. The app won't start when it can't be used, and
    # /healthz fails if it stops being usable, so a share that didn't mount is seen, not worked around.
    invoices_required = os.environ.get("INVOICES_REQUIRED", "0").strip() == "1"
    if invoices_dir:
        def local_in_use():
            """Does any car keep its invoices in the folder? Then the folder must carry its marker."""
            with connect() as conn:
                return any(f["backend"] == "local" for f in db.car_folders(conn).values())
        local = storage.LocalFolder(invoices_dir, link=lambda ref: url_for("local_file", path=ref),
                                    in_use=local_in_use)
        # Only for showing: where that folder is on the server itself, which the container can't see.
        local_shown = (os.environ.get("INVOICES_HOST_DIR", "").strip() or invoices_dir).rstrip("/")
        try:
            problem = local.problem()
        except Exception as exc:   # noqa: BLE001 - the database isn't up yet; the folder is checked again on every use
            log.info("invoice folder not fully checked at start: %s", exc)
            problem = None
        if problem and invoices_required:
            # Stopping here makes gunicorn exit, so the container stops; with a restart policy it tries again,
            # and starts once the share is mounted.
            log.error("INVOICES_DIR %s can't be used, and INVOICES_REQUIRED=1, so the app won't start: %s",
                      invoices_dir, problem)
            raise RuntimeError(f"INVOICES_REQUIRED=1 and the invoice folder can't be used: {problem}")
        if problem:
            log.warning("INVOICES_DIR %s can't be used; the app runs without it until it can (INVOICES_REQUIRED=1 "
                        "would stop it instead): %s", invoices_dir, problem)
    # The image is built with its release version in APP_VERSION; anything else is a development build.
    version = os.environ.get("APP_VERSION", "")
    update_check = updates.UpdateCheck(version, fetch=release_fetch,
                                       enabled=os.environ.get("UPDATE_CHECK", "1").strip() != "0")
    app.extensions["update_check"] = update_check
    # CLAIMS_CONNECTOR_URL turns on claiming an expense from the salary packaging provider, through a
    # separate service that knows how to talk to it (docs/claims-connector.md). Unset, nothing shows.
    connector = claim_lib.from_env()
    icon_cache = icons.IconCache(fetch=icon_fetch, enabled=os.environ.get("PROVIDER_ICONS", "1").strip() != "0")
    base_path = os.environ.get("BASE_PATH", "")
    if base_path:
        app.wsgi_app = PrefixMiddleware(app.wsgi_app, base_path)
    # The proxy's scheme and host, so the OneDrive callback address is the
    # public one. The app is only reachable through the proxy (or on loopback).
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=0, x_proto=1, x_host=1)
    log.info("started: version=%s tz=%s base_path=%s onedrive=%s invoice_folder=%s update_check=%s claims=%s",
             version or "dev", tz, base_path or "/", "on" if drive else "off", "on" if local else "off",
             "on" if update_check.active else "off", "on" if connector else "off")
    update_check.status()    # starts the first check now, so the footer knows by the first page

    # Scheduled jobs (jobs.py). Add a job here, beside the things it needs. Where they run is SCHEDULER:
    # in this process (the default), in a worker container, or nowhere.
    scheduler = jobs.Scheduler(connect, log)
    scheduler.add("teslamate", timedelta(hours=1), lambda conn: f"TeslaMate: {teslamate(conn, fresh=True).state}",
                  "Looks at TeslaMate again: adds its new cars, and holds its figures back or lets them through.")
    scheduler.add("dashboard", timedelta(hours=6), lambda _conn: refresh_dashboard(),
                  "Rewrites the Grafana dashboard, so its time range and panels follow the data.")
    scheduler.add("ato_rates", timedelta(days=7), lambda conn: tax.refresh(conn, ato_fetch or ato.get, datetime.now(timezone.utc)),
                  "Reads the income tax brackets and the home charging rate for an electric car from the ATO's pages. If a page "
                  "can't be read or has changed, the rates already kept stay as they are.")
    send_notice = notify_send or notify.send

    def notifications(conn):
        return notices.run(conn, send_notice, teslamate(conn), datetime.now(ZoneInfo(tz)),
                           onedrive=drive is not None, signin_check=check_signin)
    scheduler.add("notifications", timedelta(minutes=15), notifications,
                  "Sends the notices you have asked for (Settings, Notifications): a reminder coming due, the OneDrive "
                  "client secret nearing its expiry, the OneDrive sign-in lost. Each is sent once.")

    def check_mail(conn):
        """Count what is waiting in each car's mailbox folder, for the storage page and the notifications job.
        Nothing is imported or changed: that is the owner's to do, on the import page."""
        if mailbox is None:
            return "off"
        account = db.onedrive_account(conn)
        if not (account and account["mail_refresh_token"]):
            return "no mailbox connected"
        store, total, failed = store_for("mail"), 0, []
        for car_id, folder in db.car_mail_folders(conn).items():
            try:
                items = store.pdfs(folder["folder_id"])
            except onedrive.SignInExpired as exc:
                raise jobs.JobError(exc.user_message) from None
            except storage.StorageError as exc:
                log.warning("mail folder of car %d not looked in: %s", car_id, exc)
                failed.append(exc.user_message)
                continue
            known = db.known_invoices(conn, [f["web_url"] for f in items])
            skipped = db.skipped_import_urls(conn, car_id)
            waiting = [f for f in items if f["web_url"] not in known and f["web_url"] not in skipped]
            emails = len({outlook.split_id(f["id"])[0] for f in waiting})
            db.note_mail_waiting(conn, car_id, emails, max((f["received"] for f in waiting if f["received"]), default=None))
            total += emails
        said = f"{total} email{'' if total == 1 else 's'} waiting"
        if failed:
            raise jobs.JobError(f"{said}; a folder couldn't be read: {failed[0]}")
        return said
    scheduler.add("mail", timedelta(minutes=30), check_mail,
                  "Looks in each car's mailbox folder for emails not yet imported, for the Invoice storage page and "
                  "a notice. It only reads: importing is done on the import page.")
    app.extensions["scheduler"] = scheduler
    try:
        jobs_mode = jobs.mode(os.environ)
    except ValueError as exc:
        log.error("%s Running the scheduled jobs in the app, as if it were unset.", exc)
        jobs_mode = "app"
    app.extensions["scheduler_mode"] = jobs_mode
    if real_start and background_jobs and jobs_mode == "app":
        scheduler.start_thread()
    log.info("scheduled jobs: %s", {"app": "run by this process", "off": "switched off (SCHEDULER=off)",
                                    "worker": "left to a worker (SCHEDULER=worker)"}[jobs_mode]
             if background_jobs else "this is the worker")

    # ---- helpers --------------------------------------------------------------

    @app.template_filter("money")
    def money(value):
        return f"-${-value:,.2f}" if value < 0 else f"${value:,.2f}"

    @app.template_filter("km")
    def km(value):
        return "—" if value is None else f"{float(value):,.0f} km"

    @app.template_filter("percent")
    def percent(value):
        return "—" if value is None else f"{Decimal(value).normalize():f}%"

    @app.template_filter("when")
    def when(value):
        return value.astimezone(ZoneInfo(tz)).strftime("%d %b %Y %H:%M")

    app.jinja_env.globals["applies"] = db.applies
    app.jinja_env.filters["every"] = jobtext.every_in_words
    app.jinja_env.filters["job_state"] = jobtext.state

    @app.template_filter("isodate")
    def isodate(value):
        return value.astimezone(ZoneInfo(tz)).date().isoformat()

    @app.template_filter("day")
    def day(value):
        """A date as the lists show it: a timestamp in the app's time zone, or a plain date as it is."""
        if isinstance(value, datetime):
            value = value.astimezone(ZoneInfo(tz))
        return value.strftime("%d %b %Y")

    static_versions = {}

    @app.url_defaults
    def version_static(endpoint, values):
        # A browser holding the previous release's app.js/style.css must not mix them with the new page,
        # so each static URL carries a hash of its file: a changed file is a new URL, refetched for certain.
        if endpoint != "static" or "v" in values or not values.get("filename"):
            return
        path = Path(app.static_folder) / values["filename"]
        try:
            stat = path.stat()
        except OSError:
            return
        key = (values["filename"], stat.st_mtime_ns, stat.st_size)
        if key not in static_versions:
            static_versions[key] = hashlib.sha256(path.read_bytes()).hexdigest()[:10]
        values["v"] = static_versions[key]

    @app.after_request
    def security_headers(resp):
        resp.headers["Content-Security-Policy"] = (
            "default-src 'none'; style-src 'self'; script-src 'self'; "
            "img-src 'self'; connect-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
        )
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "same-origin"
        resp.headers.setdefault("Cache-Control", "no-store")
        return resp

    @app.context_processor
    def onedrive_flag():
        # `storage` is the provider the app is set up with: its name and icon for the pages. Another
        # provider (Google Drive, say) is another entry here, not another edit to every template.
        provider = {"name": "OneDrive", "icon": url_for("provider_icon", name="onedrive"),
                    "credit": icons.CREDIT} if drive else None
        return {"claims_enabled": connector is not None,
                "onedrive_enabled": drive is not None, "local_enabled": local is not None,
                "uploads_enabled": bool(drive or local), "storage": provider, "icon_credit": icons.CREDIT,
                # The Microsoft account's and Outlook's marks, for their cards and buttons (with OneDrive only).
                "marks": {n: url_for("provider_icon", name=n) for n in ("microsoft", "outlook")} if drive else {}}

    @app.context_processor
    def footer():
        """The docs link, the running version and whether a newer release exists (never waits for GitHub)."""
        return {"docs_url": updates.DOCS_URL, "about": update_check.status()}

    @app.errorhandler(RequestEntityTooLarge)
    def too_large(_exc):
        return render_template("error.html", title="File too large",
                               text="Invoice PDF is too large (max 20 MB). Go back and choose a smaller file."), 413

    @app.before_request
    def refuse_nul_bytes():
        """Postgres text can't hold a NUL (and the file system won't take one in a path), so one in any
        field or query value is refused here with a 400, rather than failing later as a server error."""
        for source in (request.args, request.form):
            if any("\x00" in key or "\x00" in value for key, value in source.items(multi=True)):
                text = "That request contains a character the app can't store. Go back and remove it."
                if request.path.startswith("/api/"):
                    return jsonify(error=text), 400
                return render_template("error.html", title="Invalid request", text=text), 400
        return None

    @app.before_request
    def same_origin_posts():
        """CSRF guard: a state-changing request must come from this site's own
        pages. Behind a forward-auth gate the browser's cookies are sent
        automatically, so without this any other site could POST on the user's
        behalf."""
        if request.method in ("GET", "HEAD", "OPTIONS"):
            return
        source = request.headers.get("Origin") or request.headers.get("Referer")
        if not source or urlparse(source).netloc != request.host:
            abort(403)

    @app.before_request
    def look_for_teslamate():
        """Once a minute at most: has TeslaMate appeared, gone or changed? The dashboard follows."""
        if request.endpoint in (None, "static", "healthz", "provider_icon"):
            return
        if known["at"] is not None and time.monotonic() - known["at"] <= TESLAMATE_RECHECK_SECONDS:
            return
        before = known["teslamate"]
        try:
            with connect() as conn:
                teslamate(conn)
        except Exception as exc:    # noqa: BLE001 - the page itself will say the database is down
            log.debug("TeslaMate not looked for: %s", exc)
            return
        if known["teslamate"] != before:
            refresh_dashboard()

    @app.context_processor
    def dashboards_link():
        """The header's link to Grafana. A failure to read it must not take the page down."""
        try:
            with connect() as conn:
                return {"grafana_url": db.get_settings(conn)["grafana_url"]}
        except Exception:
            return {"grafana_url": None}

    def after_tax_view(rows, rate_set):
        """What the list shows of the cost after tax, from db.after_tax_facts rows: each packaged expense's cost
        ({id: cost}), how many are packaged, and the total of every expense after tax (None while a packaged one
        can't be worked out, which is whenever no tax bracket is chosen for its income year or an earlier one).
        `rate_set` is whether any bracket is chosen at all."""
        costs, packaged, total, needs_gst = {}, 0, Decimal(0), set()
        for row in rows:
            paid_from_package = lease.is_packaged(row["packaged"], row["lease_id"] is not None, row["budget_pool"], row["kind"])
            cost = lease.effective_cost(row["amount"], paid_from_package, row["gst_free"], row["pretax_payment"],
                                        row["posttax_payment"], row["marginal_rate"], gst_amount=row["gst_amount"])
            if paid_from_package:
                packaged += 1
                costs[row["id"]] = cost
                if row["gst_free"] and row["gst_amount"] is None:       # GST only partly applies: it must be entered
                    needs_gst.add(row["id"])
            total = None if cost is None or total is None else total + cost
        return {"costs": costs, "packaged": packaged, "total": total, "rate_set": rate_set, "needs_gst": needs_gst}

    def form_context(values=None, errors=None, expense_id=None):
        with connect() as conn:
            account = db.onedrive_account(conn) if drive else None
            folders = db.car_folders(conn)
            return {
                "leased_cars": db.lease_car_ids(conn),
                # Which storage each car's invoices go to, for the file picker: only where it can be used.
                "car_storage": {car_id: f["backend"] for car_id, f in folders.items() if store_for(f["backend"])},
                "default_storage": "onedrive" if drive else "local",
                "cars": sorted(db.cars(conn), key=lambda c: c["sold_at"] is not None),   # sold cars last (the sort is stable)
                # The cars "Fill from TeslaMate" can answer for: ones TeslaMate tracks, while its figures are usable.
                "teslamate_cars": {c["id"] for c in db.cars(conn) if c["teslamate_car_id"] is not None}
                                  if teslamate(conn).usable else set(),
                "categories": db.categories(conn),
                "values": values or {},
                "errors": errors or {},
                "view": list_view(),
                "expense_id": expense_id,
                "documents": db.documents(conn, expense_id) if expense_id else [],
                "onedrive_connected": bool(account and account["refresh_token"]),
            }

    def dup_view(matches):
        """Possible duplicates as the page and the JSON endpoint show them."""
        return [{"id": m["id"], "date": m["occurred_at"].astimezone(ZoneInfo(tz)).strftime("%d %b %Y"),
                 "category": m["category"], "vendor": m["vendor"] or "", "amount": f"{m['amount']:.2f}",
                 "amount_text": money(m["amount"]), "edit_url": url_for("edit", expense_id=m["id"])}
                for m in matches]

    def powertrain_errors(car_kind, category_id, raw_items, categories, items, stored=None):
        """What a category or service item that doesn't suit the car's powertrain (db.applies) is refused with, as
        {field: text}. `stored` is (car_id, category_id, item ids, car id now) of an expense being edited: what it
        already has is kept even when it no longer applies, so only a change is checked."""
        errors, category = {}, categories.get(category_id)
        same_car = stored is not None and stored[0] == stored[3]
        if category and not db.applies(category["applies_to"], car_kind) and not (same_car and stored[1] == category_id):
            errors["category_id"] = f"{category['name']} does not apply to this kind of car."
        if category and category["name"] == db.SERVICE_CATEGORY:
            kept = set(stored[2]) if same_car else set()
            bad = [i["name"] for i in items if str(i["id"]) in {str(r) for r in raw_items}
                   and not db.applies(i["applies_to"], car_kind) and i["id"] not in kept]
            if bad:
                errors["service_item"] = ", ".join(bad) + (" does not" if len(bad) == 1 else " do not") + " apply to this kind of car."
        return errors

    def parse(form, check_duplicates=False, stored=None):
        """-> (values, errors). With check_duplicates, a likely duplicate is reported as
        errors["_duplicates"] (before anything is uploaded) unless the owner has said to save anyway.
        `stored` is (car id, category id, service item ids) of the expense being edited."""
        with connect() as conn:
            cars = {c["id"]: c for c in db.cars(conn)}
            cats = {c["id"]: c for c in db.categories(conn)}
            items = db.service_items(conn)
        car_ids, cat_ids = set(cars), set(cats)
        values, errors = validation.parse_expense_form(form, tz, car_ids, cat_ids, ref_category_ids(cats.values()))
        if not errors:
            car_now = values["car_id"]
            errors.update(powertrain_errors(cars[car_now].get("kind"), values["category_id"], form.getlist("service_item"),
                                            cats, items, stored and (*stored, car_now)))
        if check_duplicates and not errors and not form.get("confirm_duplicate"):
            with connect() as conn:
                matches = db.find_duplicates(conn, values["car_id"], values["occurred_at"], values["amount"])
            if matches:
                log.debug("duplicate check: %d possible duplicate(s) of car=%d, held for confirmation", len(matches),
                          values["car_id"])
                return values, {"_duplicates": dup_view(matches)}
        if form.get("remove_invoice"):
            values["invoice_path"] = None       # the file itself stays in the folder
        if not (drive or local):
            return values, errors
        upload = request.files.get("invoice_file")
        try:
            pdf = validation.parse_invoice_pdf(upload and upload.filename, upload.read() if upload else b"")
        except ValueError as exc:
            errors["invoice_file"] = str(exc)
            return values, errors
        if pdf is None:
            return values, errors
        if errors:
            errors["invoice_file"] = "Fix the fields above, then choose the PDF again."
            return values, errors
        if not upload_ready(values["car_id"]):
            # No folder for this car, or its storage isn't connected: that must not stop the expense being saved.
            log.info("invoice upload skipped: no invoice storage is set up for car %s", values["car_id"])
            g.invoice_note = "not_uploaded"
            return values, errors
        had_invoice = bool(values["invoice_url"] or values["invoice_path"])
        try:
            store, ref = upload_invoice(values, pdf)
        except storage.StorageError as exc:
            log.warning("invoice upload failed: %s", exc)
            errors["invoice_file"] = exc.user_message
            return values, errors
        # The new file is the expense's invoice, wherever the one before it was.
        values["invoice_url"] = values["invoice_path"] = None
        values[store.ref_field] = ref
        g.invoice_note = "replaced" if had_invoice else "uploaded"
        log.info("invoice uploaded to %s (%d bytes)", store.name, len(pdf))
        return values, errors

    def store_for(kind):
        """The storage of that kind, or None when this installation doesn't have it."""
        if kind == "onedrive" and drive is not None:
            return storage.OneDriveStore(drive, access_token, drive_tree, drive_sign_in)
        if kind == "mail" and mailbox is not None:
            return storage.MailStore(mailbox, lambda: access_token(mail=True))
        return local if kind == "local" else None

    drive_tree = onedrive.TreeCache(drive) if drive is not None else None   # this worker's, for browsing

    def drive_sign_in():
        """What the drive tree belongs to: a new sign-in or registration starts it again."""
        with connect() as conn:
            account = db.onedrive_account(conn)
        return account["client_id"], account["signed_in_at"]

    def car_folder(conn, car_id, kind):
        """The car's folder, if it is in that storage (for "mail", its mailbox folder)."""
        if kind == "mail":
            return db.car_mail_folders(conn).get(car_id)
        folder = db.car_folders(conn).get(car_id)
        return folder if folder and folder["backend"] == kind else None

    def connected(conn, kind):
        """Ready to use? OneDrive and the mailbox need their sign-ins; the folder on the server needs nothing."""
        return kind == "local" or bool((db.onedrive_account(conn) or {}).get(
            "mail_refresh_token" if kind == "mail" else "refresh_token"))

    kept = {"drive": {}, "mail": {}}   # this worker's access tokens, per sign-in: {"key": the stored refresh token it came with, "token", "until"}

    def access_token(mail=False):
        """An access token for Graph: OneDrive's, or with mail=True the mailbox's (its own sign-in on the same
        app registration, outlook.py). Microsoft's last about an hour, so one is kept and reused until
        shortly before it expires (a refresh costs most of a second). It is only reused while the stored
        refresh token is the one it came with: disconnecting, signing in again or new credentials all
        change that, so none of them leaves a kept token in use. Otherwise: spend the stored refresh token
        and store the one Microsoft hands back. A sign-in Microsoft refuses is noted (signin_lost_at), so
        the notifications job can say so; a refresh that works clears it."""
        p = "mail_" if mail else ""
        mine = kept["mail" if mail else "drive"]
        lost = None
        with connect() as conn:
            account = db.onedrive_account(conn, lock=True)
            if not account or not account[f"{p}refresh_token"]:
                mine.clear()
                if mail:
                    raise onedrive.NotConnected.mailbox()
                raise onedrive.NotConnected()
            if mine.get("key") == account[f"{p}refresh_token"] and mine["until"] > time.monotonic():
                return mine["token"]
            try:
                tokens = drive.refresh(account["client_id"], drive.decrypt(account["client_secret"]),
                                       drive.decrypt(account[f"{p}refresh_token"]),
                                       scopes=onedrive.MAIL_SCOPES if mail else onedrive.SCOPES,
                                       what="mailbox" if mail else "OneDrive")
            except onedrive.SignInExpired as exc:
                lost = exc
            else:
                key = account[f"{p}refresh_token"]
                if tokens.get("refresh_token"):
                    key = drive.encrypt(tokens["refresh_token"])
                db.save_onedrive_account(conn, **{f"{p}refresh_token": key, f"{p}signin_lost_at": None,
                                                  f"{p}signin_checked_at": datetime.now(timezone.utc)})
        if lost is not None:        # after the transaction above, which holds the row
            mine.clear()
            with connect() as conn:
                db.note_signin_lost(conn, mail=mail)
            raise lost
        lifetime = tokens.get("expires_in", 0) - TOKEN_MARGIN_SECONDS
        mine.clear()
        if lifetime > 0:
            mine.update(key=key, token=tokens["access_token"], until=time.monotonic() + lifetime)
        return tokens["access_token"]

    def check_signin():
        """Does Microsoft still accept the saved sign-in? Asked by the notifications job at most once a day
        (a refresh is the only way to know). The answer is recorded by access_token; this just asks."""
        try:
            access_token()
        except onedrive.OneDriveError as exc:      # lost (recorded), not connected, or Microsoft out of reach: tried again later
            log.info("OneDrive sign-in check: %s", exc)

    def upload_ready(car_id):
        """Has this car a folder, in a storage this installation has and can use right now?"""
        with connect() as conn:
            folder = db.car_folders(conn).get(car_id)
            return bool(folder and store_for(folder["backend"]) and connected(conn, folder["backend"]))

    def saved_msg(base):
        """The notice after saving: says what happened to an uploaded invoice, if one was chosen."""
        note = getattr(g, "invoice_note", None)
        if note == "not_uploaded":
            return base + "_not_uploaded"
        if note == "replaced" and base == "updated":
            return "updated_replaced"
        return base + "_uploaded" if note else base

    def upload_invoice(values, pdf):
        """Save the PDF in the car's folder, in its category's subfolder. -> (the storage it went to,
        the file's reference there)."""
        with connect() as conn:
            folder = db.car_folders(conn).get(values["car_id"])
            store = store_for(folder["backend"]) if folder else None
            category_row = next(c for c in db.categories(conn) if c["id"] == values["category_id"])
            category = category_row["name"]
            settings_row = db.get_settings(conn)
            car_name = next((c["name"] for c in db.cars(conn) if c["id"] == values["car_id"]), None)
        if store is None:      # parse() checks upload_ready first; the folder was cleared in between
            raise storage.StorageError("Choose a folder for this car on the Invoice storage page first.")
        name = validation.invoice_filename(
            values["occurred_at"], category, values["vendor"], settings_row["invoice_date_format"],
            settings_row["invoice_name_format"], car=car_name, amount=values["amount"])
        timer = docinfo.Timer()
        subfolder = validation.safe_folder_name(category_row["upload_folder"] or category)
        ref = store.upload(store.ensure_subfolder(folder["folder_id"], subfolder), name, pdf)
        log.debug("upload name=%s to=%s subfolder=%s bytes=%d sha256=%s category=%r ms=%d", docinfo.quote(name),
                  store.kind, subfolder, len(pdf), docinfo.sha12(pdf), category, timer.ms)
        return store, ref

    @app.context_processor
    def service_items_for_templates():
        """The service items, for the forms that offer them: read once per request, and only if a page asks."""
        def service_items():
            if "service_items" not in g:
                with connect() as conn:
                    g.service_items = db.service_items(conn)
            return g.service_items
        return {"service_items": service_items}

    def save_services(conn, expense_id, category_id, raw_ids):
        """Keep the service items ticked for an expense: only for the Service category, and only real items."""
        ids = []
        if category_id == db.service_category_id(conn):
            ids = validation.parse_service_ids(raw_ids, {i["id"] for i in db.service_items(conn)})
        db.set_expense_services(conn, expense_id, ids)

    def form_values(form):
        """Echo the raw submission back into the form after a validation error."""
        return {"service_item": form.getlist("service_item"), **{k: form.get(k, "") for k in (
            "car_id", "category_id", "date", "time", "amount", "odometer_km",
            "vendor", "notes", "invoice_url", "invoice_path", "packaged", "period_start", "period_end", "gst_amount",
        "card_payment_ref", "bpay_biller_code", "bpay_ref", "litres", "partial", "missed")}}

    # ---- routes ---------------------------------------------------------------

    @app.get("/provider-icon/<name>")
    def provider_icon(name):
        """Another product's logo (the storage provider's, Grafana's): fetched once from its public source
        and kept (see icons.py), or the bundled drawing when that isn't possible. Browsers may keep it,
        unlike the pages. The storage provider's is only served when there is one."""
        source = icons.SOURCES.get(name)
        if source is None or (source["storage"] and drive is None):
            abort(404)
        fetched = icon_cache.get(name)
        body = fetched or (Path(app.static_folder) / f"provider-{name}.svg").read_bytes()
        resp = make_response(body)
        resp.mimetype = "image/svg+xml"
        # The bundled drawing is a stand-in: look again soon, so the real one shows once it can be fetched.
        resp.headers["Cache-Control"] = "public, max-age=86400" if fetched else "public, max-age=300"
        return resp

    @app.get("/healthz")
    def healthz():
        """The container's health check. With INVOICES_REQUIRED=1, an invoice folder that can't be used
        (the NAS went away, say) makes the container unhealthy, with the reason."""
        if local is not None and invoices_required:
            try:
                problem = local.problem()
            except Exception:      # noqa: BLE001 - the database is down: nothing to say about the folder
                problem = None
            if problem:
                log.warning("health check: invoice folder can't be used: %s", problem)
                resp = make_response(f"invoice folder can't be used: {problem}\n", 503)
                resp.mimetype = "text/plain"
                return resp
        return "ok\n"

    def list_view():
        """How the list was being looked at: its category filter and sort order, read from the address or
        the form. Only what differs from the default is kept, so plain links stay plain. It rides along
        to the edit page and back, so saving or cancelling returns to the same view."""
        view = {}
        category = request.values.get("category", type=int)
        if category:
            view["category"] = category
        sort = request.values.get("sort")
        way = request.values.get("dir")
        sort = sort if sort in db.SORTS else "date"
        way = way if way in ("asc", "desc") else "desc"
        if (sort, way) != ("date", "desc"):
            view["sort"], view["dir"] = sort, way
        car = request.values.get("car", type=int)
        if car:
            view["car"] = car
        for key in ("from", "to"):
            try:
                view[key] = date.fromisoformat(request.values.get(key, "")).isoformat()
            except ValueError:
                pass
        if connector is not None and request.values.get("state") in LIST_STATES:
            view["state"] = request.values["state"]
        return view

    LIST_STATES = ("ready", "claimed", "unpaid", "blocked")

    def list_context(conn, view):
        """Everything the expense list needs, for the All page and the Claims page alike: the filtered, sorted
        expenses (with where each stands on claiming, when that is on), their total, the categories and cars."""
        category_id, car_id = view.get("category"), view.get("car")
        start = date.fromisoformat(view["from"]) if view.get("from") else None
        end = date.fromisoformat(view["to"]) if view.get("to") else None
        rows = db.list_expenses(conn, category_id, sort=view.get("sort", "date"), direction=view.get("dir", "desc"),
                                car_id=car_id, start=start, end=end, tz=tz)
        claim_open = set()
        if connector is not None:
            spans, paid, direct = db.lease_spans(conn), db.reimbursements(conn), db.direct_payments(conn)
            annotated = []
            for e in rows:
                state, reason = claim_lib.expense_state(
                    e["budget_pool"], e["amount"], claim_lib.covered(spans, e["car_id"], local_day(e)),
                    e["claim_provider_id"] is not None, e["provider_matched"])
                pay = None
                if e["id"] in paid:
                    p = paid[e["id"]]
                    pstate, gap = claim_lib.reimbursement(p["expected"], p["received"])
                    pay = {"state": pstate, "gap": gap, "reference": p["reference"]}
                annotated.append({**e, "state": state, "pay": pay, "direct": direct.get(e["id"]), "reason": reason and reason[0].upper() + reason[1:]})
            wanted = view.get("state")
            rows = [e for e in annotated if wanted is None or e["state"] == wanted
                    or (wanted == "unpaid" and e["state"] == "claimed" and (e["pay"] is None or e["pay"]["state"] != "paid")
                        # A renewal the provider has paid itself owes the owner nothing.
                        and not (e["claim_method"] == "renewal" and e["direct"]))]
            claim_open = {e["id"] for e in rows if e["state"] == "ready"}
        if view.get("state"):
            totals = {"n": len(rows), "total": sum((e["amount"] for e in rows), Decimal(0))}
        else:
            totals = db.totals(conn, category_id, car_id, start, end, tz)
        after = after_tax_view(db.after_tax_facts(conn, tz, expense_ids=[e["id"] for e in rows]), db.any_tax_choice(conn))
        services = db.expense_services(conn, [e["id"] for e in rows])
        return {"view": view, "sort_link": sort_link, "expenses": rows, "after": after, "services": services,
                "history_synced": connector is not None and db.last_synced(conn) is not None, "totals": totals, "claim_open": claim_open,
                "sorts": {"date": "Date", "category": "Category", "vendor": "Vendor", "amount": "Amount",
                          "odometer": "Odometer"},
                "categories": db.categories(conn), "cars": db.cars(conn), "category_id": category_id}

    def sort_link(column):
        """Address of the list ordered by this column: the other way round if it already is."""
        view = list_view()
        current = (view.get("sort", "date"), view.get("dir", "desc"))
        if current[0] == column:
            way = "asc" if current[1] == "desc" else "desc"
        else:
            way = "desc" if column in ("date", "amount", "odometer") else "asc"
        return url_for(request.endpoint if request.endpoint in ("index", "claims_page") else "index",
                       **{**view, "sort": column, "dir": way})

    @app.get("/")
    def index():
        view = list_view()
        with connect() as conn:
            ctx = {**list_context(conn, view), "message": MESSAGES.get(request.args.get("msg", ""))}
        if request.args.get("msg") == "imported":
            ctx["message"] = import_message()
        if request.args.get("msg") == "claimed":
            ctx["message"] = ("Claim submitted. The provider's reference is "
                              f"{request.args.get('ref', '')[:100] or 'not known yet'}.")
        return render_template("index.html", **ctx)

    def import_message():
        n, renamed, failed = (request.args.get(k, 0, type=int) for k in ("n", "renamed", "rename_failed"))
        text = f"Imported {n} invoice{'' if n == 1 else 's'}." if n else MESSAGES["imported"]
        if renamed:
            text += f" Renamed {renamed}{'' if request.args.get('store') == 'local' else ' in OneDrive'}."
        if failed:
            text += f" Couldn't rename {failed} (a file with that name may already exist): imported with the old name."
        kept = request.args.get("leases", 0, type=int)
        if kept:
            text += f" Kept {kept} lease document{'' if kept == 1 else 's'} with {'its' if kept == 1 else 'their'} lease."
        copied, not_copied = (request.args.get(k, 0, type=int) for k in ("copied", "copy_failed"))
        if copied:
            text += f" Saved {copied} attached PDF{'' if copied == 1 else 's'} in the car's invoice storage."
        if not_copied:
            text += (f" Couldn't save {not_copied} attached PDF{'' if not_copied == 1 else 's'} in the car's invoice "
                     "storage: those expenses link to the email instead.")
        return text

    @app.get("/new")
    def new():
        now = datetime.now(ZoneInfo(tz))
        values = {"date": now.strftime("%Y-%m-%d"), "time": now.strftime("%H:%M")}
        ctx = form_context(values)
        if len(ctx["cars"]) == 1:
            values["car_id"] = str(ctx["cars"][0]["id"])
        return render_template("form.html", **ctx)

    @app.post("/new")
    def create():
        values, errors = parse(request.form, check_duplicates=True)
        if "_duplicates" in errors:
            upload = request.files.get("invoice_file")
            ctx = form_context(form_values(request.form), {})
            return render_template("form.html", **ctx, duplicates=errors["_duplicates"],
                                   had_file=bool(upload and upload.filename)), 409
        if errors:
            return render_template("form.html", **form_context(form_values(request.form), errors)), 400
        with connect() as conn:
            expense_id = db.insert_expense(conn, values)
            save_services(conn, expense_id, values["category_id"], request.form.getlist("service_item"))
        refresh_dashboard()
        log.info("expense %s saved (car %s, category %s)", expense_id, values["car_id"], values["category_id"])
        with connect() as conn:
            offer = bool(db.stored_open_reminders(conn, values["car_id"]))
        if offer:       # the car has open reminders: offer to resolve them with this expense
            return redirect(url_for("expense_reminders", expense_id=expense_id, msg=saved_msg("saved")), 303)
        return redirect(url_for("index", **list_view(), msg=saved_msg("saved")), 303)

    def load_or_404(conn, expense_id):
        row = db.get_expense(conn, expense_id)
        if row is None:
            abort(404)
        return row

    @app.get("/<int:expense_id>/edit")
    def edit(expense_id):
        with connect() as conn:
            row = load_or_404(conn, expense_id)
        local = row["occurred_at"].astimezone(ZoneInfo(tz))
        values = {
            "car_id": str(row["car_id"]), "category_id": str(row["category_id"]),
            "date": local.strftime("%Y-%m-%d"), "time": local.strftime("%H:%M"),
            "amount": f"{row['amount']:.2f}",
            "odometer_km": "" if row["odometer_km"] is None else f"{row['odometer_km']:.1f}",
            "vendor": row["vendor"] or "", "notes": row["notes"] or "",
            "invoice_url": row["invoice_url"] or "", "invoice_path": row["invoice_path"] or "",
            "packaged": {None: "auto", True: "package", False: "personal"}[row["packaged"]],
            "period_start": row["period_start"].isoformat() if row["period_start"] else "",
            "period_end": row["period_end"].isoformat() if row["period_end"] else "",
            "gst_amount": "" if row["gst_amount"] is None else f"{row['gst_amount']:.2f}",
            "card_payment_ref": row["card_payment_ref"] or "", "bpay_biller_code": row["bpay_biller_code"] or "",
            "bpay_ref": row["bpay_ref"] or "",
            "litres": "" if row["litres"] is None else f"{row['litres']:.2f}",
            "partial": "" if row["full_tank"] else "1", "missed": "1" if row["missed_fill"] else "",
        }
        with connect() as conn:
            offer = bool(db.stored_open_reminders(conn, row["car_id"]))
            claim = claim_summary(conn, row)
            values["service_item"] = [str(i) for i in db.expense_service_ids(conn, expense_id)]
        return render_template("form.html", **form_context(values, expense_id=expense_id), reminder_offer=offer,
                               claim=claim)

    @app.post("/<int:expense_id>/edit")
    def update(expense_id):
        with connect() as conn:
            current = db.get_expense(conn, expense_id)
            stored = current and (current["car_id"], current["category_id"], db.expense_service_ids(conn, expense_id))
        values, errors = parse(request.form, stored=stored)
        if errors:
            ctx = form_context(form_values(request.form), errors, expense_id)
            return render_template("form.html", **ctx), 400
        with connect() as conn:
            if not db.update_expense(conn, expense_id, values):
                abort(404)
            save_services(conn, expense_id, values["category_id"], request.form.getlist("service_item"))
        refresh_dashboard()
        log.info("expense %s updated", expense_id)
        return redirect(url_for("index", **list_view(), msg=saved_msg("updated")), 303)

    @app.post("/<int:expense_id>/delete")
    def delete(expense_id):
        try:
            with connect() as conn:
                if not db.delete_expense(conn, expense_id):
                    abort(404)
        except (pg_errors.RestrictViolation, pg_errors.ForeignKeyViolation):
            # A claim points at it (expense_claim.expense_id is ON DELETE RESTRICT): say so, don't fail.
            return render_template("error.html", title="This expense has been claimed",
                                   text="This expense has been claimed from the provider, so it can't be deleted "
                                        "while the claim is recorded. Open it with Edit and use Remove claim "
                                        "record first, then delete it."), 409
        refresh_dashboard()
        log.info("expense %s deleted", expense_id)
        return redirect(url_for("index", **list_view(), msg="deleted"), 303)

    def render_categories_page(row_errors=None, overlay=None, error=None, item_error=None, status=200, message=None):
        """The Categories page. After a refused save, `overlay` holds what was typed ({"c<id>": {field: value}},
        {"s<id>": name}) so it is shown again, and `row_errors` says which row is wrong ({"c<id>": text})."""
        overlay = overlay or {}
        with connect() as conn:
            cats = [{**c, **overlay.get(f"c{c['id']}", {})} for c in db.categories(conn)]
            service = [{**i, "name": overlay.get(f"s{i['id']}", i["name"]),
                        "applies_to": overlay.get(f"a{i['id']}", i["applies_to"])} for i in db.service_items(conn)]
            budgets_on = db.lease_budgets_on(conn)
        return render_template("categories.html", categories=cats, items=service, error=error, item_error=item_error,
                               row_errors=row_errors or {}, pools=lease.POOLS, budgets_on=budgets_on, applies_to=db.APPLIES_TO,
                               message=message), status

    @app.route("/categories", methods=["GET", "POST"])
    def categories():
        error = None
        if request.method == "POST":
            try:
                name = validation.parse_category_name(request.form.get("name"))
                with connect() as conn:
                    if db.add_category(conn, name) is None:
                        raise ValueError(f"“{name}” already exists.")
                log.info("category added")
                return redirect(url_for("categories", msg="category"), 303)
            except ValueError as exc:
                error = str(exc)
        return render_categories_page(error=error, status=400 if error else 200,
                                      message=MESSAGES.get(request.args.get("msg", "")))

    def posted_ids(name):
        """The ids a form lists under `name`, once each; anything that isn't a number is ignored."""
        got = []
        for raw in request.form.getlist(name):
            if raw.strip().isdigit() and int(raw) not in got and int(raw) < 2 ** 31:
                got.append(int(raw))
        return got

    @app.post("/categories/save")
    def categories_save():
        """Every category and service item on the page, saved together: each changed row, or nothing if any row
        is wrong. A field the form doesn't carry (the folder box without uploads, the pool without budgets) is
        left alone, and so is a row that is no longer there."""
        form = request.form
        with connect() as conn:
            cats = {c["id"]: c for c in db.categories(conn)}
            service_rows = db.service_items(conn)
        service = {i["id"]: i["name"] for i in service_rows}
        item_applies = {i["id"]: i["applies_to"] for i in service_rows}
        errors, overlay, category_changes, renames = {}, {}, {}, {}
        for cid in (i for i in posted_ids("category_id") if i in cats):
            typed = {"gst_free": form.get(f"gst_partial_{cid}") == "1", "has_period": form.get(f"has_period_{cid}") == "1",
                     "has_litres": form.get(f"has_litres_{cid}") == "1"}
            if f"upload_folder_{cid}" in form:
                typed["upload_folder"] = form[f"upload_folder_{cid}"]
            if f"budget_pool_{cid}" in form:
                typed["budget_pool"] = form[f"budget_pool_{cid}"]
            if f"applies_to_{cid}" in form:
                typed["applies_to"] = form[f"applies_to_{cid}"]
            overlay[f"c{cid}"] = typed
            try:
                if "upload_folder" in typed:
                    typed["upload_folder"] = validation.parse_upload_folder(typed["upload_folder"])
                if "budget_pool" in typed:
                    typed["budget_pool"] = typed["budget_pool"].strip() or None
                    if typed["budget_pool"] is not None and typed["budget_pool"] not in lease.POOLS:
                        raise ValueError("Choose one of the pools listed.")
                if typed.get("applies_to", "all") not in db.APPLIES_TO:
                    raise ValueError("Choose which cars it applies to from the list.")
            except ValueError as exc:
                errors[f"c{cid}"] = str(exc)
                continue
            changed = {k: v for k, v in typed.items() if cats[cid][k] != v}
            if changed:
                category_changes[cid] = changed
        item_ids = [i for i in posted_ids("item_id") if i in service]
        names, applies_changes = {}, {}
        for iid in item_ids:
            overlay[f"s{iid}"] = form.get(f"item_name_{iid}", "")
            if f"item_applies_to_{iid}" in form:
                value = form[f"item_applies_to_{iid}"]
                overlay[f"a{iid}"] = value
                if value not in db.APPLIES_TO:
                    errors[f"s{iid}"] = "Choose which cars it applies to from the list."
                    continue
                if value != item_applies[iid]:
                    applies_changes[iid] = value
            try:
                names[iid] = validation.parse_service_item_name(form.get(f"item_name_{iid}"))
            except ValueError as exc:
                errors[f"s{iid}"] = str(exc)
        taken = {}
        for iid, name in {**{i: n for i, n in service.items() if i not in names}, **names}.items():
            other = taken.setdefault(name.lower(), iid)
            if other != iid:
                later = iid if iid in names else other           # blame a row that was edited, not one left alone
                errors.setdefault(f"s{later}", f"“{name}” already exists.")
        renames = {i: n for i, n in names.items() if n != service[i] and f"s{i}" not in errors}
        if errors:
            return render_categories_page(row_errors=errors, overlay=overlay, status=400)
        with connect() as conn:
            for cid, changed in category_changes.items():
                db.update_category(conn, cid, **changed)
            db.rename_service_items(conn, renames)
            db.set_service_items_applies(conn, applies_changes)
        log.info("categories saved: %d categories, %d service items changed", len(category_changes),
                 len(set(renames) | set(applies_changes)))
        return redirect(url_for("categories", msg="categories_saved"), 303)

    # ---- cars ----------------------------------------------------------------

    def render_cars(errors=None, values=None, status=200):
        now = datetime.now(ZoneInfo(tz))
        with connect() as conn:
            state = teslamate(conn)
            cars = db.car_details(conn)
            for car in cars:
                car["odometer"] = sources.odometer_at(conn, state, car["id"], min(car["sold_at"] or now, now))
        return render_template("cars.html", cars=cars, kinds=db.CAR_KINDS, teslamate=state, errors=errors or {},
                               values=values or {}, today=now.date().isoformat(),
                               message=MESSAGES.get(request.args.get("msg", ""))), status

    @app.get("/cars")
    def cars_page():
        return render_cars()

    def parse_car():
        """The car's own details and, when a price is entered, its purchase. -> (values, purchase, errors)."""
        values, errors = validation.parse_car_form(request.form, db.CAR_KINDS)
        purchase, more = validation.parse_purchase(request.form, tz, datetime.now(ZoneInfo(tz)))
        return values, purchase, {**errors, **more}

    def save_purchase(conn, car_id, purchase):
        """-> an error for the price box, or None. Nothing entered leaves the purchase as it is."""
        if purchase is None:
            return None
        outcome = db.set_purchase(conn, car_id, purchase)
        if outcome == "several":
            return "This car has more than one purchase expense, so the total can't be changed here: edit them in the expense list."
        if outcome == "no_category":
            return "There is no purchase category to record it in."
        return None

    @app.post("/cars")
    def car_add():
        values, purchase, errors = parse_car()
        if errors:
            return render_cars({"new": errors}, {"new": request.form}, 400)
        with connect() as conn:
            car_id = db.add_car(conn, values)
            save_purchase(conn, car_id, purchase)
        refresh_dashboard()
        log.info("car %s added by hand", car_id)
        return redirect(url_for("cars_page", msg="car_added"), 303)

    @app.post("/cars/<int:car_id>")
    def car_update(car_id):
        car_or_404(car_id)
        values, purchase, errors = parse_car()
        if not errors:
            try:
                with connect() as conn:
                    db.update_car(conn, car_id, values)
                    problem = save_purchase(conn, car_id, purchase)
                    if problem:
                        raise ValueError(problem)       # nothing is saved: the car's details wait with the price
            except ValueError as exc:
                errors["purchase_price"] = str(exc)
        if errors:
            return render_cars({car_id: errors}, {car_id: request.form}, 400)
        refresh_dashboard()
        log.info("car %s updated", car_id)
        return redirect(url_for("cars_page", msg="car_saved"), 303)

    @app.post("/cars/<int:car_id>/sale")
    def car_sale(car_id):
        car = car_or_404(car_id)
        now = datetime.now(ZoneInfo(tz))
        if request.form.get("action") == "clear":
            values, errors = {"sold_at": None, "sale_price": None, "sale_km": None}, {}
        else:
            # The final odometer is asked for; a car TeslaMate tracks may leave it to TeslaMate.
            values, errors = validation.parse_sale(request.form, tz, now,
                                                   km_required=car["teslamate_car_id"] is None)
        if errors:
            return render_cars({car_id: errors}, {car_id: request.form}, 400)
        with connect() as conn:
            db.set_sale(conn, car_id, values["sold_at"], values["sale_price"], values["sale_km"])
            first = db.first_expense_at(conn, car_id)
        refresh_dashboard()
        log.info("car %s sale %s", car_id, "recorded" if values["sold_at"] else "cleared")
        if values["sold_at"] is None:
            msg = "car_unsold"
        elif first is not None and values["sold_at"] < first:
            msg = "car_sold_early"
        else:
            msg = "car_sold"
        return redirect(url_for("cars_page", msg=msg), 303)

    @app.post("/cars/<int:car_id>/delete")
    def car_delete(car_id):
        car_or_404(car_id)
        with connect() as conn:
            deleted = db.delete_car(conn, car_id)
        if not deleted:
            return render_cars({car_id: {"delete": "Only a car added by hand that has no expenses can be deleted."}},
                               status=400)
        log.info("car %s deleted", car_id)
        return redirect(url_for("cars_page", msg="car_deleted"), 303)

    # ---- leases (roadmap L1) -------------------------------------------------------

    def today_here():
        return datetime.now(ZoneInfo(tz)).date()

    def lease_context(car, **extra):
        with connect() as conn:
            budgets_on = db.lease_budgets_on(conn)
            folder = db.car_folders(conn).get(car["id"])
            kind = folder["backend"] if folder and store_for(folder["backend"]) and connected(conn, folder["backend"]) else None
        return dict(car=car, pools=lease.POOLS, frequencies={k: v[0] for k, v in lease.FREQUENCIES.items()},
                    fbt_methods=lease.FBT_METHODS, tax_treatments=lease.TAX_TREATMENTS, budgets_on=budgets_on,
                    documents=validation.LEASE_DOCUMENTS, storage_kind=kind, doc_link=lease_doc_link,
                    message=MESSAGES.get(request.args.get("msg", "")), **extra)

    def lease_doc_link(row, doc):
        """Where a lease's kept document opens: its link, or the app's own address for a file in the folder."""
        if row.get(f"{doc}_url"):
            return row[f"{doc}_url"]
        if row.get(f"{doc}_path") and local is not None:
            return url_for("local_file", path=row[f"{doc}_path"])
        return None

    def lease_view(conn, row, today):
        """A lease with its budgets and the read-only figures worked out from them."""
        budgets = db.lease_budgets(conn, row["id"])
        return {**row, "budgets": {b["pool"]: b["yearly_amount"] for b in lease.initial_budgets(row, budgets)},
                "later_budgets": [b for b in budgets if b["effective_from"] != row["start_date"]],
                "figures": lease.derived(row, budgets, today)}

    def km_pace_for(conn, car, row, year, today):
        """The kilometre pace of one lease in one FBT year (lease.km_pace) with the odometers it needs, read
        through sources.odometer_at: at the start of the days held, and now, or at their end once that is
        past. Moments are in APP_TZ. `reasons` says why an odometer isn't known; the readings say where
        each came from."""
        zone = ZoneInfo(tz)
        found = lease.km_pace(row, year, today, None, None)
        found.update(start_reading=None, current_reading=None, reasons=[])
        if found["state"] not in ("current", "past") or found["estimate"] is None:
            return found
        state = teslamate(conn)
        first, last = found["first"], found["last"]
        start = sources.odometer_at(conn, state, car["id"], datetime.combine(first, dtime.min, zone))
        end = min(datetime.now(zone), datetime.combine(last + timedelta(days=1), dtime.min, zone))
        now = sources.odometer_at(conn, state, car["id"], end)
        found = lease.km_pace(row, year, today, start and start.km, now and now.km)
        held_back = car["teslamate_car_id"] is not None and state.state in (sources.UNAVAILABLE, sources.UNTESTED)
        reasons = []
        for reading, day in ((start, first), (now, min(today, last))):
            if reading is None:
                reasons.append(f"TeslaMate's figures are held back ({state.reason}), so only readings entered by hand count, "
                               f"and none is on or before {day.day} {day:%b %Y}." if held_back else
                               f"No odometer reading is on or before {day.day} {day:%b %Y}.")
        found.update(start_reading=start, current_reading=now, reasons=list(dict.fromkeys(reasons)))
        return found

    def km_pace_year(paces, rows, year, today):
        """The FBT year's kilometre pace across its leases (lease.km_pace_year), from each lease's km_pace_for: None
        unless two or more leases with an estimate are held in the year (one lease's own figures are the year's)."""
        parts = [(row, p["start_reading"] and p["start_reading"].km, p["current_reading"] and p["current_reading"].km)
                 for row, p in zip(rows, paces, strict=True)]
        found = lease.km_pace_year(parts, year, today)
        if found is None or found["leases"] < 2:
            return None
        found["reasons"] = list(dict.fromkeys(r for p in paces for r in p["reasons"]))
        return found

    def render_lease(car, status=200):
        today = today_here()
        with connect() as conn:
            current = db.current_lease(conn, car["id"], today)
            views = [lease_view(conn, row, today) for row in db.car_leases(conn, car["id"])]
            shown = next((v for v in views if current and v["id"] == current["id"]), None)
            if shown and shown["figures"]["status"] == "current":
                year = lease.fbt_year(today)
                shown["pace"] = km_pace_for(conn, car, shown, year, today)
                # The FBT year can hold an earlier (or a later) lease too: the short version then counts the whole year.
                held = [v for v in views if lease.days_held(v, year)]
                if len(held) > 1:
                    paces = [shown["pace"] if v is shown else km_pace_for(conn, car, v, year, today) for v in held]
                    shown["pace_year"] = km_pace_year(paces, held, year, today)
        others = [v for v in views if v is not shown]
        return render_template("lease.html", **lease_context(car, shown=shown, others=others, today=today, fbt_label=lease.fbt_label,
                                                            fbt_start=lease.fbt_start)), status

    def render_lease_form(car, lease_id, values, errors, status=200):
        return render_template("lease_form.html", **lease_context(car, lease_id=lease_id, values=values, errors=errors)), status

    def lease_form_values(row, budgets):
        """A saved lease as the form's text fields."""
        values = {k: ("" if v is None else v) for k, v in row.items()}
        values["associate_lease"] = "on" if row["associate_lease"] else ""
        values["charging_reimbursed"] = "on" if row["charging_reimbursed"] else ""
        for pool, amount in budgets.items():
            values[f"budget_{pool}"] = amount
        return values

    def parse_lease(car, lease_id=None):
        """-> (values, errors). A PDF chosen for the lease form or the agreement is saved to the car's folder
        once everything else is right; with no folder to save to, the lease is still saved (g.lease_note)."""
        car_id = car["id"]
        with connect() as conn:
            budgets_on = db.lease_budgets_on(conn)
        values, errors = validation.parse_lease_form(request.form, lease.POOLS, lease.FREQUENCIES,
                                                     lease.FBT_METHODS, lease.TAX_TREATMENTS, budgets_on,
                                                     electric=car["kind"] == "BEV")
        if not errors:
            with connect() as conn:
                clash = db.overlapping_lease(conn, car_id, values["start_date"], values["end_date"], lease_id)
            if clash:
                errors["start_date"] = (f"These dates overlap the lease from {clash['start_date']:%d %b %Y} "
                                        f"to {clash['end_date']:%d %b %Y}. A car has one current lease: "
                                        "end that one first, or change these dates.")
        chosen = {}
        for doc in validation.LEASE_DOCUMENTS:
            upload = request.files.get(f"{doc}_file")
            try:
                pdf = validation.parse_invoice_pdf(upload and upload.filename, upload.read() if upload else b"")
            except ValueError as exc:
                errors[f"{doc}_file"] = str(exc)
                continue
            if pdf is not None:
                chosen[doc] = pdf
        if errors:
            for doc in chosen:
                errors.setdefault(f"{doc}_file", "Fix the details below, then choose the PDF again.")
            return values, errors
        if chosen and not upload_ready(car_id):
            log.info("lease documents not saved: no invoice storage is set up for car %s", car_id)
            g.lease_note = "not_uploaded"
            return values, errors
        for doc, pdf in chosen.items():
            try:
                store, ref = upload_lease_document(car, values, doc, pdf)
            except storage.StorageError as exc:
                log.warning("lease document upload failed: %s", exc)
                errors[f"{doc}_file"] = exc.user_message
                return values, errors
            values[f"{doc}_url"] = values[f"{doc}_path"] = None
            values[f"{doc}_{'path' if store.ref_field == 'invoice_path' else 'url'}"] = ref
            log.info("lease document (%s) saved to %s (%d bytes)", doc, store.name, len(pdf))
        return values, errors

    def upload_lease_document(car, values, doc, pdf):
        """Save a lease document in the "Lease" subfolder of the car's folder. -> (the storage, the file's reference)."""
        with connect() as conn:
            folder = db.car_folders(conn).get(car["id"])
            store = store_for(folder["backend"]) if folder else None
            settings_row = db.get_settings(conn)
        if store is None:
            raise storage.StorageError("Choose a folder for this car on the Invoice storage page first.")
        start = values["start_date"]
        name = validation.invoice_filename(
            datetime(start.year, start.month, start.day), validation.LEASE_DOCUMENTS[doc],
            values.get("package_name") or values.get("financier"), settings_row["invoice_date_format"],
            settings_row["invoice_name_format"], car=car["name"])
        return store, store.upload(store.ensure_subfolder(folder["folder_id"], LEASE_FOLDER), name, pdf)

    def lease_msg(base):
        return base + "_not_uploaded" if getattr(g, "lease_note", None) else base

    @app.get("/cars/<int:car_id>/lease")
    def car_lease(car_id):
        return render_lease(car_or_404(car_id))

    @app.get("/cars/<int:car_id>/lease/new")
    def car_lease_new(car_id):
        car = car_or_404(car_id)
        return render_lease_form(car, None, {"fbt_method": "statutory", "statutory_percent": "20",
                                             "tax_treatment": "employee_contribution"}, {})

    @app.post("/cars/<int:car_id>/lease/new")
    def car_lease_create(car_id):
        car = car_or_404(car_id)
        values, errors = parse_lease(car)
        if errors:
            return render_lease_form(car, None, request.form, errors, 400)
        with connect() as conn:
            lease_id = db.add_lease(conn, car_id, values)
        if lease_id is None:      # another lease was added between the check and the save
            return render_lease_form(car, None, request.form, {"start_date": "These dates overlap another lease of this car."}, 400)
        log.info("lease %s added to car %s", lease_id, car_id)
        refresh_dashboard()
        return redirect(url_for("car_lease", car_id=car_id, msg=lease_msg("lease_added")), 303)

    def lease_or_404(car_id, lease_id):
        with connect() as conn:
            row = db.get_lease(conn, lease_id, car_id)
            budgets = db.lease_budgets(conn, lease_id) if row else []
        if row is None:
            abort(404)
        return row, lease.initial_budgets(row, budgets)

    @app.get("/cars/<int:car_id>/lease/<int:lease_id>/edit")
    def car_lease_edit(car_id, lease_id):
        car = car_or_404(car_id)
        row, first = lease_or_404(car_id, lease_id)
        values = lease_form_values(row, {b["pool"]: b["yearly_amount"] for b in first})
        return render_lease_form(car, lease_id, values, {})

    @app.post("/cars/<int:car_id>/lease/<int:lease_id>/edit")
    def car_lease_update(car_id, lease_id):
        car = car_or_404(car_id)
        lease_or_404(car_id, lease_id)
        values, errors = parse_lease(car, lease_id)
        if errors:
            return render_lease_form(car, lease_id, request.form, errors, 400)
        with connect() as conn:
            outcome = db.update_lease(conn, car_id, lease_id, values)
        if outcome == "missing":
            abort(404)
        if outcome == "overlap":
            return render_lease_form(car, lease_id, request.form, {"start_date": "These dates overlap another lease of this car."}, 400)
        log.info("lease %s of car %s updated", lease_id, car_id)
        refresh_dashboard()
        return redirect(url_for("car_lease", car_id=car_id, msg=lease_msg("lease_saved")), 303)

    @app.post("/cars/<int:car_id>/lease/<int:lease_id>/delete")
    def car_lease_delete(car_id, lease_id):
        car_or_404(car_id)
        with connect() as conn:
            if not db.delete_lease(conn, car_id, lease_id):
                abort(404)
        log.info("lease %s of car %s deleted", lease_id, car_id)
        refresh_dashboard()
        return redirect(url_for("car_lease", car_id=car_id, msg="lease_deleted"), 303)

    @app.post("/api/lease-prefill")
    def api_lease_prefill():
        """Read a provider's lease form and propose the Add a lease page's fields: the PDF just chosen on the
        computer, or one already in a storage (onedrive_id or local_id). Only the lease's own figures are
        read (leaseform.py); nothing is saved here."""
        upload = request.files.get("form_file")
        filename = upload and upload.filename
        data = upload.read() if upload else b""
        kind = next((k for k in STORES if request.form.get(f"{k}_id") and store_for(k)), None)
        if not upload and kind:
            filename = request.form.get("name", "")
            try:
                data = store_for(kind).download(request.form[f"{kind}_id"])
            except storage.StorageError as exc:
                log.warning("lease prefill from %s failed: %s", STORES[kind]["label"], exc)
                return jsonify(error=exc.user_message), 502
        try:
            pdf = validation.parse_invoice_pdf(filename, data)
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
        if pdf is None:
            return jsonify(error="Choose a PDF first."), 400
        found = leaseform.read(invoices.extract_text(pdf))
        with connect() as conn:
            if not db.lease_budgets_on(conn):
                found = {k: v for k, v in found.items() if not k.startswith("budget_")}
        log.debug("lease prefill file=%s bytes=%d found=%s", docinfo.quote(filename), len(pdf), sorted(found))
        return jsonify(fields=found)

    # ---- the FBT year report (roadmap L2) -------------------------------------------

    def home_charging(conn, car, leases, year, today):
        """The home charging reimbursement of an FBT year, for an electric car on a lease that has it ticked:
        None when there is nothing to work out, else {"km", "rate", "result"}. The distance is the odometer at the
        end of the days the car was held in the year (or now, if earlier) less the odometer at their start,
        from sources.odometer_at() only: km is None when either reading isn't known, and then there is no result.
        `rate` is (the FBT year whose cents per km it is, cents per km), or None before the first year with one.
        The tax saved is at the marginal rate of each income year the days fall in, weighted by days."""
        if car["kind"] != "BEV":
            return None
        spans = lease.reimbursed_spans([row for row, _ in leases], year, today)
        if not spans:
            return None
        zone, state, now = ZoneInfo(tz), teslamate(conn), datetime.now(ZoneInfo(tz))
        km = Decimal(0)
        for first, last in spans:
            begin = datetime.combine(first, dtime.min, zone)
            end = min(datetime.combine(last + timedelta(days=1), dtime.min, zone), now)
            at_start, at_end = (sources.odometer_at(conn, state, car["id"], when) for when in (begin, end))
            if at_start is None or at_end is None:
                km = None
                break
            km += (Decimal(str(at_end.km)) - Decimal(str(at_start.km))).quantize(Decimal("0.1"))
        ev = lease.ev_rate(db.ev_charging_rates(conn), year)
        positions = db.tax_positions(conn, "employee", {tax.income_year(d) for span in spans for d in span})
        rate = lease.rate_over_days(spans, lambda y: positions[y] and positions[y]["marginal_rate"])
        return {"km": km, "rate": ev, "result": lease.charging_reimbursement(km, ev and ev[1], rate)}

    def fbt_after_tax(conn, car, leases, report, year, today, budgets_on):
        """The report's "After tax" part: each line the report has with its invoice total and what it costs after
        tax, TeslaMate's charging (paid personally), the reimbursement, and the year's after-tax cost. With no
        tax bracket chosen there are no figures. A total that depends on something unknown is None, never a number
        standing in for it. Purchases are left out, as in the running costs."""
        if not db.any_tax_choice(conn):
            return {"rate_set": False}
        rows = [r for r in db.after_tax_facts(conn, tz, car_id=car["id"], year=year) if r["kind"] != "purchase"]
        if budgets_on:
            names = {**lease.POOLS, lease.NO_POOL: "Not in a pool"}
            found = lease.after_tax_lines(rows, lambda r: r["budget_pool"] or lease.NO_POOL)
            order = [k for k in names if k in found]
        else:
            names = {}
            found = lease.after_tax_lines(rows, lambda r: r["category"])
            order = sorted(found)
        lines = [{"key": k, "label": names.get(k, k), **found[k]} for k in order]
        invoice = sum((line["invoice"] for line in lines), Decimal(0))
        effective = None if any(line["effective"] is None for line in lines) else sum((line["effective"] for line in lines), Decimal(0))
        charging = report["teslamate_charging"]
        if charging:
            if charging["value"] is None:
                invoice = effective = None
            else:
                invoice = None if invoice is None else invoice + Decimal(charging["value"])
                effective = None if effective is None else effective + Decimal(charging["value"])
        reimbursement = home_charging(conn, car, leases, year, today)
        total = effective
        if reimbursement is not None:
            total = None if reimbursement["result"] is None or effective is None else effective - reimbursement["result"]["tax_saved"]
        return {"rate_set": True, "lines": lines, "charging": charging, "invoice": invoice, "effective": effective,
                "reimbursement": reimbursement, "total": total}

    @app.get("/cars/<int:car_id>/fbt")
    def car_fbt(car_id):
        car = car_or_404(car_id)
        today = today_here()
        try:
            year = int(request.args["year"]) if "year" in request.args else lease.fbt_year(today)
        except ValueError:
            abort(404)
        if not 1990 <= year <= 2200:
            abort(404)
        zone = ZoneInfo(tz)
        with connect() as conn:
            rows = db.car_leases(conn, car_id)
            if not rows:
                return render_template("fbt.html", car=car, report=None, today=today)
            budgets_on = db.lease_budgets_on(conn)
            leases = [(row, db.lease_budgets(conn, row["id"]) if budgets_on else []) for row in rows]
            costs = db.fbt_costs(conn, car_id, year, tz)
            years = lease.fbt_years(rows, db.fbt_years_with_expenses(conn, car_id, tz))
            # TeslaMate's charging for the year (only its part: what was entered by hand is already in the costs)
            charging = sources.charging_cost(conn, teslamate(conn), car_id, datetime(year, 4, 1, tzinfo=zone),
                                             datetime(year + 1, 4, 1, tzinfo=zone), include_manual=False)
            report = lease.fbt_report(leases, costs, charging, year, today)
            after = fbt_after_tax(conn, car, leases, report, year, today, budgets_on)
            # An associate lease's household figures for the income years this FBT year runs across (L7).
            associate = [r for r in db.lease_years(conn, car_id) if r["associate"] and r["income_year"] in (year - 1, year)]
            paces = [km_pace_for(conn, car, row, year, today) for row, _budgets, _figures in report["leases"]]
            pace_year = km_pace_year(paces, [row for row, _b, _f in report["leases"]], year, today)
        earlier, later = [y for y in years if y < year], [y for y in years if y > year]
        return render_template("fbt.html", car=car, report=report, paces=paces, today=today, budgets_on=budgets_on, frequencies={k: v[0] for k, v in lease.FREQUENCIES.items()},
                               previous=earlier[-1] if earlier else None, following=later[0] if later else None,
                               label=lease.fbt_label, after=after, associate=associate, tax_label=tax.year_label, pace_year=pace_year)
    # ---- reminders (roadmap item 4) ------------------------------------------------

    def open_items(conn, car_id=None):
        """Every open reminder with its status. Worked out when asked, once per request for all cars."""
        if car_id is None and "reminder_items" in g:
            return g.reminder_items
        now = datetime.now(ZoneInfo(tz))
        items = reminders.due_items(conn, teslamate(conn), now.date(), now, car_id)
        if car_id is None:
            g.reminder_items = items
        return items

    @app.context_processor
    def reminder_badge():
        """What the header's Reminders link and the list's notice say: how many are overdue or need a look.
        A failure to work it out must not take the page down."""
        try:
            with connect() as conn:
                overdue, attention = reminders.attention(open_items(conn))
        except Exception:      # noqa: BLE001 - the page itself will say the database is down
            return {"reminder_attention": None}
        return {"reminder_attention": {"overdue": overdue, "total": attention}}

    def reminder_form_context(conn):
        now = datetime.now(ZoneInfo(tz))
        cars = db.cars(conn)
        state = teslamate(conn)
        # Each car's odometer now, so a standard reminder's first due reading can be worked out on the page.
        odometers = {c["id"]: sources.odometer_at(conn, state, c["id"], now) for c in cars}
        return dict(cars=cars, categories=db.categories(conn), item_choices=db.service_items(conn), lead_days=reminders.DEFAULT_LEAD_DAYS,
                    lead_km=reminders.DEFAULT_LEAD_KM, standard=reminders.STANDARD,
                    odometers={k: v.km for k, v in odometers.items() if v}, today_iso=now.date().isoformat())

    def render_reminders(errors=None, values=None, status=200):
        page = max(request.args.get("page", 1, type=int) or 1, 1)
        with connect() as conn:
            items = open_items(conn)
            done, total = db.resolved_reminders(conn, reminders.RESOLVED_PAGE, (page - 1) * reminders.RESOLVED_PAGE)
            choices = {i["car_id"]: db.recent_expenses(conn, i["car_id"]) for i in items if i["reminder_id"]}
            ctx = reminder_form_context(conn)
        return render_template("reminders.html", items=items, resolved=done, page=page,
                               pages=-(-total // reminders.RESOLVED_PAGE) or 1, resolved_total=total, choices=choices,
                               errors=errors or {}, values=values or {}, today=today_here().isoformat(),
                               message=MESSAGES.get(request.args.get("msg", "")), **ctx), status

    @app.get("/reminders")
    def reminders_page():
        return render_reminders()

    def parse_reminder(car_id=None):
        with connect() as conn:
            car_ids = {c["id"] for c in db.cars(conn)}
            category_ids = {c["id"] for c in db.categories(conn)}
            item_ids = {i["id"] for i in db.service_items(conn)}
        return validation.parse_reminder_form(request.form, car_ids, category_ids, car_id, item_ids)

    @app.post("/reminders")
    def reminder_add():
        values, errors = parse_reminder()
        if errors:
            return render_reminders({"new": errors}, {"new": request.form}, 400)
        with connect() as conn:
            reminder_id = db.add_reminder(conn, values)
        log.info("reminder %s added to car %s", reminder_id, values["car_id"])
        return redirect(url_for("reminders_page", msg="reminder_added"), 303)

    def open_reminder_or_404(reminder_id):
        with connect() as conn:
            row = db.get_reminder(conn, reminder_id)
        if row is None or row["resolved_at"] is not None:
            abort(404)
        return row

    def render_reminder_edit(row, values, errors, status=200):
        with connect() as conn:
            ctx = reminder_form_context(conn)
        car = next(c for c in ctx["cars"] if c["id"] == row["car_id"])
        return render_template("reminder_edit.html", reminder=row, car=car, values=values, errors=errors, **ctx), status

    @app.get("/reminders/<int:reminder_id>/edit")
    def reminder_edit(reminder_id):
        row = open_reminder_or_404(reminder_id)
        values = {k: ("" if v is None else v) for k, v in row.items()}
        return render_reminder_edit(row, values, {})

    @app.post("/reminders/<int:reminder_id>/edit")
    def reminder_update(reminder_id):
        row = open_reminder_or_404(reminder_id)
        values, errors = parse_reminder(row["car_id"])
        if errors:
            return render_reminder_edit(row, request.form, errors, 400)
        with connect() as conn:
            if not db.update_reminder(conn, reminder_id, values):
                abort(404)
        log.info("reminder %s updated", reminder_id)
        return redirect(url_for("reminders_page", msg="reminder_saved"), 303)

    @app.post("/reminders/<int:reminder_id>/delete")
    def reminder_delete(reminder_id):
        with connect() as conn:
            if not db.delete_reminder(conn, reminder_id):
                abort(404)
        log.info("reminder %s deleted", reminder_id)
        return redirect(url_for("reminders_page", msg="reminder_deleted"), 303)

    def resolved_message(results):
        if any(r.next_id for r in results):
            return "reminder_resolved_next"
        notes = sorted({r.next_note for r in results if r.next_note})
        return f"reminder_resolved_{notes[0]}" if notes else "reminder_resolved"

    @app.post("/reminders/<int:reminder_id>/resolve")
    def reminder_resolve(reminder_id):
        row = open_reminder_or_404(reminder_id)
        now = datetime.now(ZoneInfo(tz))
        values, errors = validation.parse_resolve_form(request.form, now.date())
        expense = None
        if values["expense_id"] is not None:
            with connect() as conn:
                expense = db.get_expense(conn, values["expense_id"])
            if expense is None or expense["car_id"] != row["car_id"]:
                errors["expense_id"] = "That expense is not one of this car's."
        if errors:
            return render_reminders({reminder_id: errors}, {reminder_id: request.form}, 400)
        moment = None if expense else reminders.resolution_moment(values["date"], ZoneInfo(tz), now)
        with connect() as conn:
            result = reminders.resolve(conn, teslamate(conn), reminder_id, ZoneInfo(tz), expense, moment, values["km"])
        if result.outcome != "resolved":      # resolved by someone else meanwhile
            abort(404)
        log.info("reminder %s resolved%s", reminder_id, f", next {result.next_id}" if result.next_id else "")
        return redirect(url_for("reminders_page", msg=resolved_message([result])), 303)

    @app.post("/reminders/<int:reminder_id>/unresolve")
    def reminder_unresolve(reminder_id):
        with connect() as conn:
            outcome = reminders.unresolve(conn, reminder_id)
        if outcome is None:
            abort(404)
        log.info("reminder %s reopened", reminder_id)
        return redirect(url_for("reminders_page", msg="reminder_reopened_kept" if outcome[1] else "reminder_reopened"), 303)

    def render_expense_reminders(expense_id, errors=None, status=200):
        with connect() as conn:
            expense = load_or_404(conn, expense_id)
            open_ones = db.stored_open_reminders(conn, expense["car_id"])
            items = set(db.expense_service_ids(conn, expense_id))
        return render_template("expense_reminders.html", expense=expense, open_ones=open_ones, errors=errors or {},
                               expense_items=items,
                               message=MESSAGES.get(request.args.get("msg", ""))), status

    @app.get("/<int:expense_id>/reminders")
    def expense_reminders(expense_id):
        return render_expense_reminders(expense_id)

    @app.post("/<int:expense_id>/reminders")
    def expense_reminders_resolve(expense_id):
        with connect() as conn:
            expense = load_or_404(conn, expense_id)
            ids = request.form.getlist("reminder_id")
            rows = [db.get_reminder(conn, int(i)) if i.isdigit() and int(i) < 2 ** 31 else None for i in ids]
        if not ids:
            return redirect(url_for("index"), 303)
        if any(r is None or r["car_id"] != expense["car_id"] or r["resolved_at"] is not None for r in rows):
            return render_expense_reminders(expense_id, {"reminder_id": "Choose from this car's open reminders."}, 400)
        with connect() as conn:
            results = [reminders.resolve(conn, teslamate(conn), r["id"], ZoneInfo(tz), expense) for r in rows]
        log.info("expense %s resolved %d reminder(s)", expense_id, len(results))
        return redirect(url_for("reminders_page", msg=resolved_message(results)), 303)

    # ---- claims: sending an expense to the salary packaging provider ----------------------------

    def need_connector():
        if connector is None:
            abort(404)

    def local_day(row):
        return row["occurred_at"].astimezone(ZoneInfo(tz)).date()

    def claim_open_ids(conn, rows):
        """Ids of the listed expenses that can be claimed now (none when claiming is off)."""
        if connector is None:
            return set()
        spans = db.lease_spans(conn)
        return {r["id"] for r in rows
                if claim_lib.claimable(r["budget_pool"], r["amount"], claim_lib.covered(spans, r["car_id"], local_day(r)),
                                       r["claim_provider_id"] is not None or r["provider_matched"])}

    def claim_summary(conn, row):
        """What the edit page says about claiming this expense: the claim recorded, or whether it can be claimed."""
        if connector is None:
            return None
        return {"claim": row if row["claim_provider_id"] else None,
                "open": row["id"] in claim_open_ids(conn, [row])}

    def claim_problem(conn, row):
        """Why this expense can't be claimed now (a sentence for the page), or None."""
        if row["claim_provider_id"]:
            ref = f" (reference {row['claim_reference']})" if row["claim_reference"] else ""
            return f"This expense has already been claimed{ref}."
        if row["provider_matched"]:
            return "This expense has already been claimed: a payment from the provider matches it."
        reason = claim_lib.block_reason(row["budget_pool"], row["amount"],
                                        claim_lib.covered(db.lease_spans(conn), row["car_id"], local_day(row)))
        if reason:
            return f"This expense can't be claimed: {reason}."
        return None

    def claim_invoice(row):
        """-> (INVOICE object or None, blocking message or None, notice or None) for an expense's invoice file:
        read from the invoice folder, or downloaded from OneDrive (with OneDrive's own sign-in) when the expense
        keeps the file's OneDrive address. Any other link (an email in the mailbox, a web page) isn't a file this
        app can send."""
        too_large = "The invoice file is too large to send (the limit is 10 MB)."
        if row["invoice_url"] and onedrive.is_drive_url(row["invoice_url"]):
            store = store_for("onedrive")
            if store is None:
                return None, ("This expense's invoice is in OneDrive, which isn't set up here "
                              "(GARAGEBOOKS_ENCRYPTION_KEY), so it can't be sent."), None
            try:
                name, content = store.fetch(row["invoice_url"], max_bytes=claim_lib.MAX_INVOICE_BYTES)
            except onedrive.TooLarge:
                return None, too_large, None
            except storage.StorageError as exc:
                log.warning("claim: the OneDrive invoice of expense %s couldn't be read: %s", row["id"], exc.user_message)
                return None, exc.user_message, None
            return claim_lib.invoice_payload(name, content), None, None
        if row["invoice_path"] and local is not None:
            try:
                content = local.download(row["invoice_path"])
            except storage.StorageError as exc:
                return None, exc.user_message, None
            if len(content) > claim_lib.MAX_INVOICE_BYTES:
                return None, too_large, None
            name = row["invoice_path"].rsplit("/", 1)[-1]
            return claim_lib.invoice_payload(name, content), None, None
        if row["invoice_path"] or row["invoice_url"]:
            return None, ("This expense's invoice is only a link, which can't be sent from here: attach the "
                          "invoice file first (Edit, then choose the PDF), then claim it."), None
        return None, None, "No invoice is attached to this expense. The provider will probably want one."

    def render_claim(row, fields, method, status=200, **extra):
        context = {"errors": [], "preview": None, "needs": [], "signin": False, "error": None, "blocked": None,
                   "notice": None, **extra}
        return render_template("claim.html", expense=row, fields=fields, method=method, view=list_view(),
                               renewable=row["budget_pool"] == claim_lib.RENEWAL_POOL,
                               renewal_labels=claim_lib.RENEWAL_FIELD_LABELS, **context), status

    def claim_method(row, asked):
        """How this expense goes to the provider. Only a registration has the choice: what was chosen on the page,
        else what its lease says (the provider renews it, or the owner pays and claims it back)."""
        if row["budget_pool"] != claim_lib.RENEWAL_POOL:
            return "reimburse"
        if asked in claim_lib.METHODS:
            return asked
        day = local_day(row)
        with connect() as conn:
            renewed = any(x["rego_renewed_by_provider"] and x["start_date"] <= day <= x["end_date"]
                          for x in db.car_leases(conn, row["car_id"]))
        return "renewal" if renewed else "reimburse"

    def clean_for(method, form):
        return claim_lib.clean_renewal_fields(form) if method == "renewal" else claim_lib.clean_fields(form)

    def refusal(method, reply):
        if method == "renewal" and reply.status == 404:
            return ("This claims connector can't send a registration renewal request: update it, or choose to claim "
                    "the registration back instead. Nothing was sent.")
        return reply.error

    def clean_preview(body):
        """What the preview page shows of the connector's answer, as plain, bounded values."""
        as_list = lambda key: [t for t in (claim_lib.text(x) for x in (body.get(key) or [])[:20]) if t]  # noqa: E731
        return {"provider": claim_lib.text(body.get("provider"), 100), "kind": claim_lib.text(body.get("kind"), 100),
                "car": claim_lib.text(body.get("car"), 200), "amount": claim_lib.number(body.get("amount")),
                "gst": claim_lib.number(body.get("gst")), "ex_gst": claim_lib.number(body.get("ex_gst")),
                "summary": as_list("summary"), "warnings": as_list("warnings"),
                "declaration": claim_lib.declaration_text(body.get("declaration"))}

    def claim_context(expense_id):
        """The expense, or a page explaining why it can't be claimed: -> (row, None) or (None, response)."""
        with connect() as conn:
            row = load_or_404(conn, expense_id)
            why = claim_problem(conn, row)
        if why:
            return None, (render_template("error.html", title="Can't claim this expense", text=why), 400)
        return row, None

    def preview_page(expense_id, asked, fields=None, errors=(), first_visit=False, status=200):
        row, refused = claim_context(expense_id)
        if refused:
            return refused
        method = claim_method(row, asked)
        fields = fields or {}
        if first_visit:
            # What the expense already holds starts the claim's fields. Only the first time the page opens: after
            # that the form's own values count (an owner who clears the GST leaves it to the provider).
            fields = {**claim_lib.stored_fields(row, method), **fields}
        invoice, blocked, notice = claim_invoice(row)
        if blocked:
            return render_claim(row, fields, method, blocked=blocked, errors=errors)
        ask = connector.renewal_preview if method == "renewal" else connector.preview
        show = functools.partial(render_claim, row, fields, method, status=status, notice=notice, errors=errors)
        try:
            reply = ask(claim_lib.expense_payload(row, local_day(row), fields), invoice)
        except claim_lib.ConnectorError as exc:
            return show(error=exc.user_message)
        if reply.ok:
            return show(preview=clean_preview(reply.body))
        if reply.needs_signin:
            return show(signin=True)
        if reply.needs_fields:
            return show(needs=reply.needs_fields)
        return show(error=refusal(method, reply))

    @app.get("/<int:expense_id>/claim")
    def claim_page(expense_id):
        need_connector()
        return preview_page(expense_id, request.args.get("method"), first_visit=True)

    @app.post("/<int:expense_id>/claim")
    def claim_preview(expense_id):
        need_connector()
        row, refused = claim_context(expense_id)
        if refused:
            return refused
        method = claim_method(row, request.form.get("method"))
        fields, errors = clean_for(method, request.form)
        if errors:
            return render_claim(row, fields, method, errors=errors, status=400)
        return preview_page(expense_id, method, fields)

    @app.post("/<int:expense_id>/claim/submit")
    def claim_submit(expense_id):
        need_connector()
        row, refused = claim_context(expense_id)
        if refused:
            return refused
        method = claim_method(row, request.form.get("method"))
        if request.form.get("method", method) != method:      # a renewal asked of an expense that can't have one
            abort(400)
        fields, errors = clean_for(method, request.form)
        if errors:
            return render_claim(row, fields, method, errors=errors, status=400)
        if method == "renewal" and request.form.get("accept_declaration") != "1":
            return preview_page(expense_id, method, fields, status=400,
                                errors=["Nothing was sent: read and accept the declaration first."])
        invoice, blocked, notice = claim_invoice(row)
        if blocked:
            return render_claim(row, fields, method, blocked=blocked)
        send = connector.renewal_submit if method == "renewal" else connector.submit
        try:
            reply = send(claim_lib.expense_payload(row, local_day(row), fields), invoice)
        except claim_lib.ConnectorError as exc:
            return render_claim(row, fields, method, error=exc.user_message, notice=notice)
        if reply.needs_signin:
            return render_claim(row, fields, method, signin=True, notice=notice)
        if reply.needs_fields:
            return render_claim(row, fields, method, needs=reply.needs_fields, notice=notice)
        provider_id = claim_lib.text(reply.body.get("provider_id"), 200)
        if not reply.ok:
            return render_claim(row, fields, method, error=refusal(method, reply), notice=notice)
        if not provider_id:
            log.error("claims connector accepted expense %s without a provider_id", expense_id)
            return render_claim(row, fields, method, error="The connector accepted the claim but didn't say which one it "
                                "is, so it couldn't be recorded. Check the provider's own site before trying again.",
                                notice=notice, status=502)
        reference = claim_lib.text(reply.body.get("reference"), 200)
        health = {}
        try:
            health = connector.health().body
        except claim_lib.ConnectorError:
            pass
        provider = claim_lib.text(health.get("provider"), 100) or claim_lib.text(reply.body.get("provider"), 100) or "provider"
        # A renewal's fields (the licence number, the payment references) are sent and never kept: only how it went.
        kept_fields = {"method": "renewal"} if method == "renewal" else fields
        with connect() as conn:
            stored = db.add_claim(conn, expense_id, {
                "provider": provider, "provider_id": provider_id, "reference": reference,
                "status": claim_lib.text(reply.body.get("status"), 200)
                or ("Renewal requested" if method == "renewal" else None),
                "amount": claim_lib.number(reply.body.get("amount")) or row["amount"],
                "gst": claim_lib.number(kept_fields.get("gst")),
                "fields": json.dumps(kept_fields) if kept_fields else None})
            db.rematch(conn, tz, claim_lib.match_by_figures)
        log.info("expense %s claimed (provider reference recorded: %s)", expense_id, bool(reference))
        if not stored:
            log.warning("expense %s was claimed twice at once; the first record was kept", expense_id)
        said = "renewal_requested" if method == "renewal" else "claimed"
        return redirect(url_for("index", **list_view(), msg=said, ref=reference or ""), 303)

    @app.post("/<int:expense_id>/claim/forget")
    def claim_forget(expense_id):
        need_connector()
        with connect() as conn:
            if not db.delete_claim(conn, expense_id):
                abort(404)
            db.rematch(conn, tz, claim_lib.match_by_figures)
        log.info("claim record for expense %s removed", expense_id)
        return redirect(url_for("index", **list_view(), msg="claim_forgotten"), 303)

    def provider_status():
        """-> {state: 'signed_in'|'signed_out'|'unreachable', provider, message}: asked of the connector now, briefly."""
        try:
            reply = connector.health(timeout=5)
        except claim_lib.ConnectorError as exc:
            return {"state": "unreachable", "provider": None, "message": exc.user_message}
        if not reply.ok:
            return {"state": "unreachable", "provider": None, "message": reply.error}
        saved = reply.body.get("session_saved") is True
        return {"state": "signed_in" if saved else "signed_out", "message": None,
                "provider": claim_lib.text(reply.body.get("provider"), 100)}

    @app.get("/claims")
    def claims_page():
        """The claims ledger: the All page's expense list with where each expense stands on claiming, and the
        provider's transaction history beside it."""
        need_connector()
        view = list_view()
        tx = request.args.get("tx") if request.args.get("tx") in ("matched", "unmatched", "per_km", "other") else None
        with connect() as conn:
            ctx = list_context(conn, view)
            start = date.fromisoformat(view["from"]) if view.get("from") else None
            end = date.fromisoformat(view["to"]) if view.get("to") else None
            transactions = db.transaction_rows(conn, view.get("car"), start, end, tx)
            synced = db.last_synced(conn)
            sync = db.sync_state(conn)
        status = provider_status()
        problem = request.args.get("problem", "")[:300] if request.args.get("msg") == "claims_problem" else None
        return render_template("claims.html", **ctx, ledger=True, transactions=transactions, tx=tx, synced=synced,
                               status=status, sync=sync, message=MESSAGES.get(request.args.get("msg", "")), problem=problem,
                               tx_link=lambda key: url_for("claims_page", **{**view, "tx": key}),
                               signin=status["state"] == "signed_out" or request.args.get("msg") == "claims_signin"
                               or (not sync["running"] and sync["outcome"] == "signin"))

    def do_sync(since, cars):
        """Fetch the provider's history from `since` through the connector, keep it, and line it up with the
        expenses. -> (outcome, message): 'synced', 'signin' or 'problem' with why."""
        try:
            reply = connector.transactions(cars, since=since)
        except claim_lib.ConnectorError as exc:
            return "problem", exc.user_message
        if reply.needs_signin:
            return "signin", None
        if not reply.ok:
            return "problem", reply.error
        car_ids = {c["id"] for c in cars}
        items = reply.body.get("transactions")
        rows = [r for r in (claim_lib.clean_transaction(i, car_ids)
                            for i in (items if isinstance(items, list) else [])[:claim_lib.MAX_TRANSACTIONS]) if r]
        claims = reply.body.get("claims")
        lines = [r for r in (claim_lib.clean_claim_line(i, car_ids)
                             for i in (claims if isinstance(claims, list) else [])[:claim_lib.MAX_TRANSACTIONS]) if r]
        provider = claim_lib.text(reply.body.get("provider"), 100) or "Claims connector"
        with connect() as conn:
            db.save_transactions(conn, provider, rows, since)
            db.save_claim_lines(conn, provider, lines, since)
            db.rematch(conn, tz, claim_lib.match_by_figures)
        return "synced", None

    def finish_sync(since, cars):
        try:
            outcome, message = do_sync(since, cars)
        except Exception:  # noqa: BLE001 - this may be a thread: whatever went wrong is recorded, not lost
            log.exception("syncing the provider's history failed")
            outcome, message = "problem", "The sync failed. The app's log says why."
        with connect() as conn:
            db.end_sync(conn, outcome, message)
        return outcome, message

    @app.post("/claims/sync")
    def claims_sync():
        """Fetch the provider's history. The provider's site is slow, so this starts the sync and returns: the
        page shows it running and updates itself. Only what is new since the last sync is fetched, unless asked
        for everything (full=1)."""
        need_connector()
        with connect() as conn:
            cars = [{"id": c["id"], "name": c["name"], "make": c["make"], "model": c["model"]} for c in db.cars(conn)]
            since = db.history_since(conn, tz, datetime.now(ZoneInfo(tz)).date(), full=request.form.get("full") == "1")
            if not db.begin_sync(conn, since):
                return redirect(url_for("claims_page", msg="claims_sync_running"), 303)
        if app.testing:                       # the tests want the answer now; the real app never makes a person wait
            outcome, message = finish_sync(since, cars)
            if outcome == "synced":
                return redirect(url_for("claims_page", msg="claims_synced"), 303)
            if outcome == "signin":
                return redirect(url_for("claims_page", msg="claims_signin"), 303)
            return redirect(url_for("claims_page", msg="claims_problem", problem=message), 303)
        threading.Thread(target=finish_sync, args=(since, cars), daemon=True, name="claims-sync").start()
        return redirect(url_for("claims_page", msg="claims_sync_started"), 303)   # no message: the running notice says it

    @app.get("/claims/sync/status")
    def claims_sync_status():
        need_connector()
        with connect() as conn:
            state = db.sync_state(conn)
        return jsonify(running=bool(state["running"]), outcome=state["outcome"], message=state["message"])

    @app.post("/claims/refresh")
    def claims_refresh():
        """Ask the connector where each unpaid claim has got to, and keep what it says."""
        need_connector()
        with connect() as conn:
            waiting = [r["provider_id"] for r in db.claim_rows(conn) if r["paid_date"] is None]
        if not waiting:
            return redirect(url_for("claims_page", msg="claims_none"), 303)
        try:
            reply = connector.status(waiting)
        except claim_lib.ConnectorError as exc:
            return redirect(url_for("claims_page", msg="claims_problem", problem=exc.user_message), 303)
        if reply.needs_signin:
            return redirect(url_for("claims_page", msg="claims_problem", problem="Sign in to the provider first: "
                                    "open a claim and use Sign in there, then refresh again."), 303)
        if not reply.ok:
            return redirect(url_for("claims_page", msg="claims_problem", problem=reply.error), 303)
        with connect() as conn:
            for item in reply.body.get("claims") or []:
                if not isinstance(item, dict) or not claim_lib.text(item.get("provider_id"), 200):
                    continue
                provider_id = claim_lib.text(item["provider_id"], 200)
                status = claim_lib.text(item.get("status"), 200)
                if status is None:           # not found, or an error for this one: keep what we know
                    db.touch_claim_check(conn, provider_id)
                    continue
                try:
                    paid = date.fromisoformat(item["paid_date"]) if item.get("paid_date") else None
                except (TypeError, ValueError):
                    paid = None
                db.save_claim_status(conn, provider_id, status, claim_lib.text(item.get("reference"), 200),
                                     claim_lib.number(item.get("approved_amount")), paid)
        return redirect(url_for("claims_page", msg="claims_refreshed"), 303)

    def signin_view(body):
        """The sign-in state of a /v1/health answer, cleaned: only what the page shows."""
        signin = body.get("signin") if isinstance(body.get("signin"), dict) else {}
        state = signin.get("state") if signin.get("state") in claim_lib.SIGNIN_STATES else "idle"
        out = {"state": state}
        for key in ("message", "error"):
            if claim_lib.text(signin.get(key), 500):
                out[key] = claim_lib.text(signin[key], 500)
        return out

    def connector_json(call):
        """Run a connector call for the sign-in script: its answer, or the reason it didn't come."""
        try:
            reply = call()
        except claim_lib.ConnectorError as exc:
            return None, (jsonify(ok=False, error=exc.user_message), 502)
        if reply.status != 200:
            return None, (jsonify(ok=False, error=reply.error), 502)
        return reply, None

    @app.get("/claims/signin/status")
    def claims_signin_status():
        need_connector()
        reply, failed = connector_json(connector.health)
        if failed:
            return failed
        return jsonify(ok=True, provider=claim_lib.text(reply.body.get("provider"), 100), signin=signin_view(reply.body))

    @app.post("/claims/signin")
    def claims_signin():
        need_connector()
        reply, failed = connector_json(connector.signin)
        if failed:
            return failed
        return jsonify(ok=True, state=signin_view({"signin": {"state": reply.body.get("state")}})["state"])

    @app.post("/claims/signin/code")
    def claims_signin_code():
        need_connector()
        data = request.get_json(silent=True)
        code = (data if isinstance(data, dict) else request.form).get("code")
        if not isinstance(code, str) or not re.fullmatch(r"[0-9A-Za-z -]{1,20}", code.strip()):
            return jsonify(ok=False, error="Enter the code exactly as the provider sent it."), 400
        reply, failed = connector_json(lambda: connector.signin_code(code.strip()))
        if failed:
            return failed
        return jsonify(ok=True, state=signin_view({"signin": {"state": reply.body.get("state")}})["state"])

    @app.post("/categories/services")
    def service_item_add():
        try:
            name = validation.parse_service_item_name(request.form.get("name"))
            with connect() as conn:
                if db.add_service_item(conn, name) is None:
                    raise ValueError(f"“{name}” already exists.")
        except ValueError as exc:
            return render_categories_page(item_error=str(exc), status=400)
        return redirect(url_for("categories", msg="service_added"), 303)

    @app.post("/categories/services/<int:item_id>/delete")
    def service_item_delete(item_id):
        with connect() as conn:
            if not db.delete_service_item(conn, item_id):
                abort(404)
        return redirect(url_for("categories", msg="service_deleted"), 303)

    @app.post("/api/service-items")
    def api_service_items():
        """Add a service item from the import page's dialog, so nothing typed there is lost. An item that exists
        (ignoring case) is returned as it is, marked `existing`, so the page just ticks it."""
        body = request.get_json(silent=True)
        raw = body.get("name") if isinstance(body, dict) else None
        try:
            name = validation.parse_service_item_name(raw if isinstance(raw, str) else "")
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
        with connect() as conn:
            item = db.add_service_item(conn, name)
            if item is not None:
                return jsonify(id=item["id"], name=item["name"]), 201
            item = db.service_item_named(conn, name)
        return jsonify(id=item["id"], name=item["name"], existing=True)

    # ---- change history ---------------------------------------------------------

    # Field -> label, in the order they are shown. id and the created/updated stamps are never shown.
    history_fields = {
        "expense": (("car_id", "Car"), ("category_id", "Category"), ("occurred_at", "Date and time"),
                    ("amount", "Amount"), ("odometer_km", "Odometer"), ("vendor", "Vendor"), ("notes", "Notes"),
                    ("invoice_url", "Invoice link"), ("invoice_path", "Invoice file"), ("packaged", "Paid from"),
                    ("period_start", "Period start"), ("period_end", "Period end"), ("gst_amount", "GST"),
                    ("litres", "Litres"), ("full_tank", "Filled the tank"), ("missed_fill", "Missed a fuel-up")),
        "category": (("name", "Name"), ("kind", "Kind"), ("sort_order", "Sort order"),
                     ("upload_folder", "Invoice subfolder"), ("gst_free", "GST only partly applies"), ("has_period", "Has a period"),
                     ("has_litres", "Records litres")),
        "expense_claim": (("provider", "Provider"), ("reference", "Reference"), ("status", "Status"),
                          ("amount", "Amount"), ("gst", "GST"), ("approved_amount", "Approved amount"),
                          ("paid_date", "Paid on")),
    }

    def history_entries(entries, car_names, category_names):
        def show(field, value):
            if field == "packaged":
                return {None: "Automatic", True: "The package", False: "Personally"}.get(value, str(value))
            if field in ("gst_free", "full_tank", "missed_fill", "has_litres"):
                return "Yes" if value else "No"
            if value is None or value == "":
                return "—"
            if field == "car_id":
                return car_names.get(value, f"car {value}")
            if field == "category_id":
                return category_names.get(value, f"category {value}")
            if field in ("amount", "gst", "approved_amount", "gst_amount"):
                return money(Decimal(str(value)))
            if field == "odometer_km":
                return km(value)
            if field == "occurred_at":
                try:
                    return when(datetime.fromisoformat(value))
                except (TypeError, ValueError):
                    return str(value)
            return str(value)

        quiet = {"full_tank": True, "missed_fill": False, "has_litres": False}      # not worth a line when added
        out = []
        for e in entries:
            before, after = e["before_row"] or {}, e["after_row"] or {}
            row = after or before
            fields = history_fields[e["table_name"]]
            if e["operation"] == "update":
                # A row logged before a column existed has no value for it: that is the column's default.
                changed = [(label, show(f, before.get(f, quiet.get(f))), show(f, after.get(f, quiet.get(f))))
                           for f, label in fields if before.get(f, quiet.get(f)) != after.get(f, quiet.get(f))]
            else:
                changed = [(label, None, show(f, row.get(f))) for f, label in fields
                           if row.get(f) not in (None, "") and not (f == "gst_free" and row.get(f) is False)
                           and not (f in quiet and row.get(f) == quiet[f])]
            if e["table_name"] == "expense":
                title = "Expense: " + " · ".join(
                    x for x in (category_names.get(row.get("category_id")), car_names.get(row.get("car_id")),
                                row.get("vendor")) if x)
            elif e["table_name"] == "expense_claim":
                title = f"Claim: {row.get('reference') or row.get('provider_id', '')}"
            else:
                title = f"Category: {row.get('name', '')}"
            out.append({"when": e["changed_at"], "actor": e["actor"], "operation": e["operation"],
                        "label": {"insert": "Added", "update": "Edited", "delete": "Deleted"}[e["operation"]],
                        "title": title, "row_id": e["row_id"], "table": e["table_name"], "fields": changed})
        return out

    @app.get("/history")
    def history_page():
        filters = {}
        car = request.args.get("car", type=int)
        if car:
            filters["car"] = car
        if request.args.get("table") in db.HISTORY_TABLES:
            filters["table"] = request.args["table"]
        if request.args.get("operation") in db.HISTORY_OPERATIONS:
            filters["operation"] = request.args["operation"]
        with connect() as conn:
            entries, page, pages = db.history(conn, car, filters.get("table"), filters.get("operation"),
                                              request.args.get("page", 1, type=int))
            cars = db.cars(conn)
            cats = db.categories(conn)
        shown = history_entries(entries, {c["id"]: c["name"] for c in cars}, {c["id"]: c["name"] for c in cats})
        return render_template("history.html", entries=shown, cars=cars, filters=filters, page=page, pages=pages,
                               page_link=lambda n: url_for("history_page", **filters, **({"page": n} if n > 1 else {})))

    @app.get("/learned")
    def learned_page():
        with connect() as conn:
            label_rows, vendor_rows = db.learned_rows(conn)
        return render_template("learned.html", labels=label_rows, vendors=vendor_rows, after=invoices.LEARN_AFTER,
                               message=MESSAGES.get(request.args.get("msg", "")))

    @app.post("/learned/label/delete")
    def learned_label_delete():
        with connect() as conn:
            db.delete_label(conn, request.form.get("field", ""), request.form.get("label", ""))
        return redirect(url_for("learned_page", msg="learned_deleted"), 303)

    @app.post("/learned/vendor/delete")
    def learned_vendor_delete():
        with connect() as conn:
            db.delete_vendor(conn, request.form.get("vendor", ""))
        return redirect(url_for("learned_page", msg="learned_deleted"), 303)

    # ---- OneDrive ---------------------------------------------------------------

    STATE_COOKIE = "onedrive_sign_in"
    SIGN_IN_MINUTES = 10

    def need_drive():
        if drive is None:
            abort(404)

    def callback_url(account=None, new=False):
        """Where Microsoft sends the browser back after a sign-in. A registration saved since the address
        moved (callback = 'microsoft') uses /microsoft/callback; one saved before keeps /onedrive/callback,
        which is what its installer registered. new=True: the address a registration saved now would use."""
        legacy = not new and account is not None and account["client_id"] and account["callback"] is None
        return url_for("onedrive_callback" if legacy else "microsoft_callback", _external=True)

    def car_or_404(car_id):
        with connect() as conn:
            car = next((c for c in db.cars(conn) if c["id"] == car_id), None)
        if car is None:
            abort(404)
        return car

    @app.get("/storage")
    def storage_page():
        return render_storage()

    @app.get("/onedrive")
    def onedrive_page():
        """Where OneDrive was set up before every storage shared one page: old links still arrive."""
        return redirect(url_for("storage_page"), 302)

    def render_storage(errors=None, status=200, notice=None):
        """The one page for everywhere invoices can be kept: a card to set up each place, then the
        folder each car uses. `providers` is what the page loops over, in STORES' order."""
        account = None
        with connect() as conn:
            cars, folders = db.cars(conn), db.car_folders(conn)
            mail_folders = db.car_mail_folders(conn)
            settings_row = db.get_settings(conn)
            if drive is not None:
                account = db.onedrive_account(conn)
        expires = account and account["secret_expires"]
        # ready: a car's folder can be chosen there now (the folder is mounted; OneDrive is signed in).
        ready = {"local": local is not None and local.available(),
                 "onedrive": bool(account and account["refresh_token"])}
        providers = [{"kind": kind, **info, "enabled": store_for(kind) is not None, "ready": ready[kind]}
                     for kind, info in STORES.items()]
        ctx = {
            "providers": providers, "cars": cars, "folders": folders, "account": account,
            "connected": ready["onedrive"], "callback_url": callback_url(account) if drive is not None else None,
            "new_callback_url": callback_url(new=True) if drive is not None else None,
            "errors": errors or {}, "values": request.form if errors else {}, "notice": notice,
            "message": MESSAGES.get(request.args.get("msg", "")), "name_example": name_example(settings_row),
            "secret_days_left": (expires - date.today()).days if expires else None,
            "available": ready["local"], "root": str(local.root) if local else None,
            "local_shown": local_shown if local else None,
            "mail_folders": mail_folders, "mail_connected": bool(account and account["mail_refresh_token"]),
        }
        return render_template("storage.html", **ctx), status

    @app.post("/onedrive/credentials")
    def onedrive_credentials():
        need_drive()
        errors, fields = {}, {}
        for field, fn in (("client_id", validation.parse_client_id),
                          ("secret_expires", validation.parse_optional_date)):
            try:
                fields[field] = fn(request.form.get(field))
            except ValueError as exc:
                errors[field] = str(exc)
        secret = (request.form.get("client_secret") or "").strip()
        if not secret:
            errors["client_secret"] = "Paste the client secret's Value (not its Secret ID)."
        if errors:
            return render_storage(errors, 400)
        with connect() as conn:
            # A new registration can't use the old one's sign-in.
            db.save_onedrive_account(conn, client_secret=drive.encrypt(secret), refresh_token=None,
                                     account=None, auth_state=None, auth_expires_at=None,
                                     signin_lost_at=None, signin_checked_at=None, mail_refresh_token=None,
                                     mail_account=None, mail_signin_lost_at=None, mail_signin_checked_at=None,
                                     callback="microsoft", **fields)
        return redirect(url_for("storage_page", msg="credentials"), 303)

    @app.post("/onedrive/connect")
    def onedrive_connect():
        return start_sign_in("drive")

    @app.post("/mail/connect")
    def mail_connect():
        return start_sign_in("mail")

    def start_sign_in(purpose):
        """Send the browser to Microsoft to sign in, for OneDrive ("drive") or the mailbox ("mail"). Both use
        the one app registration and come back to the one callback address; the pending sign-in remembers
        which it was for."""
        need_drive()
        with connect() as conn:
            account = db.onedrive_account(conn)
        if not account or not account["client_id"]:
            return redirect(url_for("storage_page"), 303)
        state, verifier, challenge = onedrive.new_pkce()
        with connect() as conn:
            db.save_onedrive_account(
                conn, auth_state=drive.encrypt(f"{state} {verifier} {purpose}"),
                auth_expires_at=datetime.now(timezone.utc) + timedelta(minutes=SIGN_IN_MINUTES))
        scopes = onedrive.MAIL_SCOPES if purpose == "mail" else onedrive.SCOPES
        # Not a redirect: Chrome applies form-action to a form POST's redirect.
        # A link (followed by app.js) is a plain navigation, as n8n does it.
        resp = make_response(render_template(
            "connecting.html",
            sign_in_url=drive.authorize_url(account["client_id"], callback_url(account), state, challenge, scopes)))
        # Ties the sign-in to this browser: the callback must come back to the one that started it.
        resp.set_cookie(STATE_COOKIE, state, max_age=SIGN_IN_MINUTES * 60, httponly=True,
                        samesite="Lax", secure=request.is_secure, path=request.script_root or "/")
        return resp

    @app.get("/microsoft/callback", endpoint="microsoft_callback")
    @app.get("/onedrive/callback")
    def onedrive_callback():
        """Microsoft sends the browser back here after a sign-in, for OneDrive or the mailbox. Both
        addresses work; which one the sign-in asked for is callback_url(account)."""
        need_drive()
        with connect() as conn:
            account = db.onedrive_account(conn)
        state = request.args.get("state", "")
        try:
            pending = account and account["auth_state"] and drive.decrypt(account["auth_state"]).split(" ")
        except onedrive.SignInExpired:
            pending = None
        if (not pending or account["auth_expires_at"] < datetime.now(timezone.utc)
                or not hmac.compare_digest(pending[0], state)
                or not hmac.compare_digest(request.cookies.get(STATE_COOKIE, ""), state)):
            return render_storage(status=400, notice=(
                "This sign-in wasn't started from this browser, or took longer than "
                f"{SIGN_IN_MINUTES} minutes. Press Connect OneDrive to try again."))
        with connect() as conn:
            db.save_onedrive_account(conn, auth_state=None, auth_expires_at=None)
        mail = pending[2:] == ["mail"]       # a sign-in started before the mailbox existed has no third part
        what = "the mailbox" if mail else "OneDrive"
        if request.args.get("error"):
            log.warning("Microsoft didn't connect %s: %s", what, docinfo.quote(request.args["error"]))
            return render_storage(status=400, notice=f"Microsoft didn't connect {what}: "
                                   + (request.args.get("error_description") or request.args["error"]))
        try:
            tokens = drive.redeem_code(account["client_id"], drive.decrypt(account["client_secret"]),
                                       request.args.get("code", ""), callback_url(account), pending[1],
                                       onedrive.MAIL_SCOPES if mail else onedrive.SCOPES)
        except onedrive.OneDriveError as exc:
            log.warning("%s sign-in failed: %s", what, exc)
            return render_storage(status=400, notice=exc.user_message)
        p = "mail_" if mail else ""
        name = (mailbox if mail else drive).account_name(tokens["access_token"]) or "your Microsoft account"
        with connect() as conn:
            db.save_onedrive_account(conn, **{f"{p}refresh_token": drive.encrypt(tokens["refresh_token"]),
                                              f"{p}account": name, f"{p}signin_lost_at": None,
                                              f"{p}signin_checked_at": None,
                                              **({} if mail else {"signed_in_at": datetime.now(timezone.utc)})})
        log.info("%s connected", what)
        resp = redirect(url_for("storage_page", msg="mail_connected" if mail else "connected"), 303)
        resp.delete_cookie(STATE_COOKIE, path=request.script_root or "/")
        return resp

    @app.post("/onedrive/disconnect")
    def onedrive_disconnect():
        need_drive()
        with connect() as conn:
            db.save_onedrive_account(conn, refresh_token=None, account=None, signin_lost_at=None, signin_checked_at=None)
        log.info("OneDrive disconnected")
        return redirect(url_for("storage_page", msg="disconnected"), 303)

    @app.post("/mail/disconnect")
    def mail_disconnect():
        """Forget the mailbox's sign-in. Each car's mailbox folder is kept, for when it is connected again."""
        need_drive()
        with connect() as conn:
            db.save_onedrive_account(conn, mail_refresh_token=None, mail_account=None, mail_signin_lost_at=None,
                                     mail_signin_checked_at=None)
        log.info("mailbox disconnected")
        return redirect(url_for("storage_page", msg="mail_disconnected"), 303)

    # ---- the invoice folder on the server ---------------------------------------

    def send_invoice(ref):
        """A PDF from the invoice folder, shown in the browser's own viewer (?download=1 saves it
        instead). The folder is behind the same forward-auth as every other page."""
        if local is None:
            abort(404)
        try:
            path = local.file(ref)
        except storage.StorageError as exc:
            log.warning("invoice not served: %s", exc)
            return render_template("error.html", title="Invoice not found", text=exc.user_message), 404
        resp = send_file(path, mimetype="application/pdf", download_name=path.name,
                         as_attachment=bool(request.args.get("download")))
        resp.headers["Cache-Control"] = "private, no-cache"   # the browser may keep it, but asks each time
        return resp

    @app.get("/invoice/<int:expense_id>")
    def invoice(expense_id):
        """The expense's stored invoice: what the list and the Grafana dashboard link to."""
        with connect() as conn:
            row = load_or_404(conn, expense_id)
        if not row["invoice_path"]:
            abort(404)
        return send_invoice(row["invoice_path"])

    @app.get("/document/<int:document_id>")
    def document(document_id):
        """A PDF merged into an expense on the import page: from the invoice folder, or the stored link."""
        with connect() as conn:
            doc = db.get_document(conn, document_id)
        if doc is None:
            abort(404)
        if doc["invoice_path"]:
            return send_invoice(doc["invoice_path"])
        return redirect(doc["invoice_url"], 302)        # https only: the table refuses anything else

    @app.post("/<int:expense_id>/documents/<int:document_id>/remove")
    def document_remove(expense_id, document_id):
        """Forget an attached document (the file stays where it is, and is offered for import again)."""
        with connect() as conn:
            if not db.delete_document(conn, expense_id, document_id):
                abort(404)
        log.info("document %s removed from expense %s", document_id, expense_id)
        return redirect(url_for("edit", expense_id=expense_id, **list_view()), 303)

    @app.get("/local/file")
    def local_file():
        """A PDF in the folder that has no expense yet (the import page links to these)."""
        return send_invoice(request.args.get("path", ""))

    # ---- what both storages do: a folder per car, importing, the file picker -------

    # A folder chosen before its car exists (the import then builds the car): car id 0 in the storage routes.
    NEW_CAR = {"id": 0, "name": "", "kind": "BEV", "make": None, "model": None, "teslamate_car_id": None}
    CAR_KEYS = ("make", "model") + db.CAR_DETAIL_FIELDS

    def attach_rows(rows, count, ticked, ref_field, errors):
        """Which rows are merged into which expense: {main row n: [the rows attached to it]}. A row attached to a
        row that is itself attached follows the chain to the one that makes the expense. Problems go into `errors`
        (by row number): a row to attach to that doesn't exist, itself, a loop, a main row that isn't ticked, or a
        bad link."""
        pointing = {}
        for row in rows:
            if row["attach"] == "":
                continue
            try:
                target = int(row["attach"])
            except ValueError:
                target = -1
            if not 0 <= target < count or target == row["n"]:
                errors[row["n"]] = "Choose another row to attach this one to."
            else:
                pointing[row["n"]] = target
        attachments = {}
        for n, target in pointing.items():
            if n in errors:
                continue
            seen = {n}
            while target in pointing and target not in seen:
                seen.add(target)
                target = pointing[target]
            row = rows[n]
            try:
                ref = validation.parse_invoice_path(row["url"]) if ref_field == "invoice_path" else validation.parse_invoice_url(row["url"])
            except ValueError as exc:
                errors[n] = str(exc)
                continue
            if target in seen or target in pointing:
                errors[n] = "These rows are attached to each other in a loop."
            elif not ref or not row["name"]:
                errors[n] = "Missing link to the PDF."
            elif target not in ticked:
                errors[n] = "The row it is attached to isn't ticked for import."
            else:
                attachments.setdefault(target, []).append({"name": row["name"], "url": ref})
        return attachments

    def ctx_folder(kind):
        return new_car_folder(kind)

    def new_car_spec(fresh, cat_kinds, folder):
        """The new car an import builds -> (the values for db.add_car, None), or (None, errors) when the dialog's
        fields are needed. The purchase invoice, when one is ticked, gives the make, model and details; what the
        dialog was given on top of that wins. A car with no make and model is called by its folder's name."""
        form = request.form
        purchase = next((r for r, v in fresh if cat_kinds.get(v["category_id"]) == "purchase" and r["car_fill"]), None) \
            or next((r for r, v in fresh if cat_kinds.get(v["category_id"]) == "purchase"), None)
        typed = {k: (form.get(f"new_car_{k}") or "").strip() for k in ("name", "kind", "make", "model") + db.CAR_DETAIL_FIELDS}
        if purchase is None and not typed["name"]:
            return None, {"name": "Enter the car's name (or its make and model)."} if form.get("new_car_asked") else {}
        base = {k: purchase.get(f"car_{k}", "") if purchase and purchase["car_fill"] else "" for k in CAR_KEYS}
        data = {**base, **{k: v for k, v in typed.items() if v}}
        data.setdefault("kind", "BEV")
        if not data.get("name") and not (data.get("make") or data.get("model")):
            data["name"] = (folder["folder_path"].rstrip("/").rsplit("/", 1)[-1] if folder else "") or "New car"
        values, errors = validation.parse_car_form(data, db.CAR_KINDS)
        return (values, None) if not errors else (None, errors)

    def car_or_new(car_id):
        return dict(NEW_CAR) if car_id == 0 else car_or_404(car_id)

    def render_browse(store, car, folder_id, error=None, status=200):
        kind = store.kind
        try:
            if kind == "mail":
                here, children = store.folder(folder_id), store.subfolders(folder_id)
            else:       # no ?folder: browsing has just started, so see what has changed
                here, children, _ = store.browse(folder_id, fresh=folder_id is None)
        except onedrive.NotConnected:
            return redirect(url_for("storage_page"), 303)
        except onedrive.SignInExpired as exc:
            log.warning("OneDrive sign-in has expired: %s", exc)
            return render_storage(status=400, notice=exc.user_message)
        except storage.StorageError as exc:
            log.warning("%s folder browse failed: %s", kind, exc)
            return render_storage(status=502, notice=exc.user_message)
        return render_template("browse.html", kind=kind, car=car, here=here, children=children,
                               is_root=folder_id is None or here["is_root"], error=error,
                               new_name=request.form.get("name", "") if error else ""), status

    def import_row(n, **fields):
        return {"n": n, "include": False, "skip": False, "date": "", "category_id": "", "vendor": "",
                "amount": "", "odometer_km": "", "url": "", "name": "", "folder": "",
                "note": "", "id": "", "attach": "", "car_fill": False, "car_fields": [], "services": [], "notes": "", "gst_amount": "", "card_payment_ref": "",
                "bpay_biller_code": "", "bpay_ref": "", "car_vin": "", "car_build_year": "", "car_model_year": "",
                "car_body_type": "", "car_make": "", "car_model": "", "period_start": "", "period_end": "", "guess": {}, "new_name": "", "rename": False, "odo_note": "", "duplicates": [], "lease": None, **fields}

    def import_page(store, car, **ctx):
        """The import page's template with what every version of it needs. A row's `url` is the file's
        reference (what the expense will store); `link` turns it into an address to open."""
        # For a car that doesn't exist yet, every request of the page carries the folder it was chosen for.
        link_args = {"folder": request.values.get("folder", "")} if car["id"] == 0 else {}
        return render_template("import.html", car=car, kind=store.kind, info=SOURCES[store.kind], link=store.link,
                               learn_after=invoices.LEARN_AFTER, link_args=link_args, kinds=db.CAR_KINDS, **ctx)

    def render_import(store, car, rows, remaining=0, errors=None, status=200, notice=None, skipped=None, **extra):
        with connect() as conn:
            categories = db.categories(conn)
            if skipped is None:
                skipped = len(db.skipped_import_urls(conn, car["id"]))
        return import_page(store, car, rows=rows, remaining=remaining, errors=errors or {}, categories=categories,
                           notice=notice, skipped=skipped, **extra), status

    def new_car_folder(kind):
        """The folder chosen for a car that doesn't exist yet (the request's `folder`), or None."""
        store = store_for(kind)
        wanted = request.values.get("folder")
        if store is None or not wanted:
            return None
        try:
            here = store.folder(wanted)
        except (storage.StorageError, onedrive.NotConnected, onedrive.SignInExpired):
            return None
        return {"folder_id": here["id"], "folder_path": here["path"], "backend": kind}

    def import_context(car_id, kind):
        """What reading a file needs: the folder, category names, learned labels and settings."""
        with connect() as conn:
            return {"folder": car_folder(conn, car_id, kind) if car_id else new_car_folder(kind),
                    "categories": db.categories(conn),
                    "learned": db.learned(conn, invoices.LEARN_AFTER), "settings": db.get_settings(conn),
                    "connected": connected(conn, kind)}

    def pending_files(store, ctx, car_id):
        """-> (all PDFs in the folder, those with no expense yet and not skipped, how many were skipped)."""
        files = store.pdfs(ctx["folder"]["folder_id"])
        with connect() as conn:
            known = db.known_invoices(conn, [f["web_url"] for f in files])
            skipped = db.skipped_import_urls(conn, car_id)
        left = [f for f in files if f["web_url"] not in known]
        pending = [f for f in left if f["web_url"] not in skipped]
        return files, pending, len(left) - len(pending)

    def read_document(store, item_id):
        """-> (text, the PDF's bytes or None, the email or None) of one thing to import. From a mailbox, an
        attached PDF is read with its email (for the refund words and what to fall back on), and an email
        with none is read from its subject and text."""
        if store.kind != "mail":
            pdf = store.download(item_id)
            return invoices.extract_text(pdf), pdf, None
        message_id, attachment_id = outlook.split_id(item_id)
        email = store.message(message_id)
        if attachment_id is None:
            return f"{email['subject']}\n\n{email['text']}", None, email
        pdf = store.download(item_id)
        return invoices.extract_text(pdf), pdf, email

    def from_email(guess, email, text):
        """What an email adds to the guesses from its text or PDF: a refund is a negative amount (the figure
        beside "refunded" first), and the day it arrived and who sent it stand in for a date and vendor
        that couldn't be read."""
        if invoices.is_refund(email["subject"], text):
            amount = invoices.refund_amount(text)
            amount = amount if amount is not None else guess["amount"]
            guess["amount"] = -abs(amount) if amount is not None else None
        if guess["date"] is None and email["received"]:
            guess["date"] = email["received"].astimezone(ZoneInfo(tz)).date()
        guess["vendor"] = guess["vendor"] or email["sender"] or None
        guess["missing"] = [f for f in ("date", "category", "amount") if guess[f] is None]
        return guess

    def lease_document_row(car, text):
        """What the import offers for a PDF that is a lease form or agreement, or None for any other PDF:
        {doc, label, lease_id (the lease it can be kept with, or None), add_url, note}. A lease matches by the
        start date its form states (an agreement states none: it matches when the car has one lease)."""
        doc = leaseform.document_kind(text)
        if doc is None or car["id"] == 0:
            return None
        start = leaseform.parse_date(leaseform.read(text).get("start_date"))
        label = validation.LEASE_DOCUMENTS[doc]
        with connect() as conn:
            leases = db.car_leases(conn, car["id"])
        match = next((x for x in leases if start and x["start_date"] == start), None)
        if match is None and doc == "agreement" and len(leases) == 1:
            match = leases[0]
        found = {"doc": doc, "label": label, "lease_id": None, "add_url": url_for("car_lease_new", car_id=car["id"]),
                 "kept": False, "lease_start": ""}
        if match is not None:
            started = f"{match['start_date']:%d %b %Y}".lstrip("0")
            kept = bool(match[f"{doc}_url"] or match[f"{doc}_path"])
            found.update(lease_id=None if kept else match["id"], kept=kept, lease_start=started)
        found["note"] = (
            f"This looks like a {label.lower()}, not an invoice, so it isn't offered as an expense. "
            + (f"The lease from {found['lease_start']} already has one." if found["kept"] else
               f"Tick it to keep it with the lease from {found['lease_start']}; the file isn't renamed or moved."
               if found["lease_id"] else "No lease matches it yet: add one from it first."))
        return found

    def read_file(store, car, ctx, n, f):
        """Read one PDF (or email) and propose its expense: an import row. A file that can't be fetched
        becomes a row saying why, never an error for the whole page."""
        by_name = {c["name"]: c["id"] for c in ctx["categories"]}
        row = import_row(n, url=f["url"], name=f["name"], folder=f["folder"], id=f["id"])
        timer = docinfo.Timer()
        try:
            text, pdf, email = read_document(store, f["id"])
            lease_doc = lease_document_row(car, text) if email is None else None
            if lease_doc:
                # A lease form or agreement is kept with its lease, not imported as an expense, and the file is
                # never renamed or moved: the row has no rename option at all.
                row.update(lease=lease_doc, note=lease_doc["note"])
                return row
            guess = invoices.propose(f["folder"], f["name"], text, list(by_name), ctx["learned"],
                                     ctx["settings"]["multi_amount"])
            if email is not None:
                guess = from_email(guess, email, text)
        except storage.StorageError as exc:
            log.warning("import file=%s folder=%s skipped: %s", docinfo.quote(f["name"]), docinfo.quote(f["folder"]), exc)
            row["note"] = exc.user_message
            return row
        have, missing = docinfo.found(guess)
        # The bytes actually read, not the size the page sent back: that is only what the listing said.
        content = pdf if pdf is not None else text.encode()
        log.debug("import file=%s folder=%s bytes=%d sha256=%s found=%s missing=%s ms=%d", docinfo.quote(f["name"]),
                  docinfo.quote(f["folder"]), len(content), docinfo.sha12(content), have, missing, timer.ms)
        guessed = next((c for c in ctx["categories"] if c["name"] == guess["category"]), None)
        if guessed and not db.applies(guessed["applies_to"], car.get("kind") if car["id"] else None):
            guess = {**guess, "category": None}       # a guess that doesn't suit this car (fuel for an EV) isn't offered
        row.update(
            date=guess["date"].isoformat() if guess["date"] else "",
            category_id=str(by_name.get(guess["category"], "")),
            vendor=guess["vendor"] or "", odometer_km=guess["odometer_km"] or "",
            amount=f"{guess['amount']:.2f}" if guess["amount"] is not None else "",
            include=not guess["missing"], notes=guess["notes"],
            gst_amount=f"{guess['gst_amount']:.2f}" if guess["gst_amount"] is not None else "",
            card_payment_ref=guess["card_payment_ref"] or "", bpay_biller_code=guess["bpay_biller_code"] or "",
            bpay_ref=guess["bpay_ref"] or "",
            period_start=guess["period_start"].isoformat() if guess["period_start"] else "",
            period_end=guess["period_end"].isoformat() if guess["period_end"] else "",
            note=("Couldn't read the " + ", ".join(guess["missing"]) + ": fill in to import."
                  if guess["missing"] else ""))
        kinds = {c["name"]: c["kind"] for c in ctx["categories"]}
        if guess["car_details"] and kinds.get(guess["category"]) == "purchase":
            with connect() as conn:           # only what the car doesn't have yet (a new car has nothing)
                blank = db.car_blank_details(conn, car["id"])
            offered = [k for k in guess["car_details"] if k in blank or (car["id"] == 0 and k in ("make", "model"))]
            if offered:
                row["car_fill"], row["car_fields"] = True, offered
                row.update({f"car_{k}": str(guess["car_details"][k]) for k in offered})
        with connect() as conn:         # the service items this PDF names: shown when the category is Service
            named = {i["name"]: str(i["id"]) for i in db.service_items(conn)}
        row["services"] = [named[n] for n in invoices.service_items_in(text, list(named))]
        if guess["suggest_skip"]:        # a brochure or letter: not offered again unless the owner un-ticks Skip
            row.update(skip=True, include=False,
                       note="Doesn't look like an invoice (no amount, no invoice words): skipped. Untick Skip to import it.")
        if guess["date"] and not row["odometer_km"]:
            with connect() as conn:
                reading = sources.odometer_at(conn, teslamate(), car["id"],
                                              datetime.combine(guess["date"], dtime(12), ZoneInfo(tz)), manual=False)
            if reading is not None:
                row["odometer_km"] = f"{reading.km:.1f}".rstrip("0").rstrip(".")
                row["odo_note"] = "from TeslaMate"
        if guess["date"] and guess["amount"] is not None:
            with connect() as conn:
                same = db.find_duplicates(conn, car["id"], datetime.combine(guess["date"], dtime(12), ZoneInfo(tz)),
                                          guess["amount"])
            if same:
                row["duplicates"] = dup_view(same)
                row["include"] = False          # the owner decides, not the default
        if guess["date"] and guess["category"] and store.kind != "mail":    # an email's PDF is copied, not renamed
            target = validation.invoice_filename(
                datetime.combine(guess["date"], dtime(12)), guess["category"], guess["vendor"],
                ctx["settings"]["invoice_date_format"], ctx["settings"]["invoice_name_format"],
                car=car["name"], amount=guess["amount"])
            row["new_name"] = "" if target == f["name"] else target
        # What was proposed, sent back with the form so confirming can tell what the owner changed.
        row["guess"] = {k: row[k] for k in LEARN_FIELDS}
        return row

    def file_entry(f):
        return {"id": f["id"], "name": f["name"], "folder": f["folder"], "url": f["web_url"], "size": f["size"]}

    def learn_from(store, imported):
        """Remember what the owner's corrections show: the label printed beside a value they
        changed, and which category a vendor's invoices get. Best effort: whatever goes
        wrong here must never undo or block the import that has just been saved."""
        try:
            with connect() as conn:
                for row, values in imported:
                    if row["vendor"]:
                        db.record_vendor(conn, row["vendor"].strip().lower()[:100], values["category_id"])
                    typed = {"amount": values["amount"], "odometer_km": values["odometer_km"],
                             "date": values["occurred_at"].astimezone(ZoneInfo(tz)).date()}
                    guess = row["guess"]
                    changed = [f for f in invoices.LEARNABLE if typed[f] is not None and not same_value(f, typed[f], guess.get(f))]
                    if not changed or not row["id"]:
                        continue
                    text = read_document(store, row["id"])[0]
                    for field in changed:
                        for label in invoices.find_labels(text, field, typed[field]):
                            db.record_label(conn, field, label)
                            log.debug("learned field=%s label=%r file=%s", field, label, docinfo.quote(row["name"]))
        except Exception:
            log.warning("learning from the import failed", exc_info=True)

    def same_value(field, typed, guessed):
        """Did the owner keep what was proposed? (guess is the form text it was shown as)"""
        try:
            if field == "amount":
                return Decimal(guessed) == typed
            if field == "date":
                return guessed == typed.isoformat()
            return float(guessed) == float(typed)
        except (ArithmeticError, ValueError, TypeError):
            return False

    def storage_routes(kind):
        """One storage's routes, at /onedrive/... or /local/...: choosing each car's folder, importing
        the invoices already in it, and the Add form's file picker. The same code serves both, and the
        mailbox (/mail/...), which is only read: its folder is chosen and imported from the same way,
        but it has no new folder and no file picker, and its folder is not where a car's invoices go."""
        info = SOURCES[kind]
        page = "storage_page"

        def need_store():
            store = store_for(kind)
            if store is None:
                abort(404)
            return store

        @app.get(f"/{kind}/browse/<int:car_id>", endpoint=f"{kind}_browse")
        def browse(car_id):
            store = need_store()
            return render_browse(store, car_or_new(car_id), request.args.get("folder") or None)

        def new_folder(car_id):
            store = need_store()
            car = car_or_new(car_id)
            parent_id = request.form.get("parent_id") or None
            try:
                name = validation.parse_folder_name(request.form.get("name"))
                created = store.create_folder(parent_id, name)
            except onedrive.NotConnected:
                return redirect(url_for(page), 303)
            except (ValueError, storage.StorageError) as exc:
                said = exc.user_message if isinstance(exc, storage.StorageError) else str(exc)   # or the name's ValueError
                return render_browse(store, car, parent_id, error=said, status=400)
            if kind == "onedrive":
                drive_tree.expire()
            return redirect(url_for(f"{kind}_browse", car_id=car_id, folder=created["id"]), 303)

        if kind != "mail":      # the mailbox is only read (Mail.Read can't make a folder)
            app.post(f"/{kind}/browse/<int:car_id>/new-folder", endpoint=f"{kind}_new_folder")(new_folder)

        def mail_file(car_id):
            """A PDF attached to an email in the car's mailbox folder, shown in the browser's own viewer: the
            import page links to it beside the email, which Outlook on the web opens. Only a PDF is served."""
            store = need_store()
            car_or_404(car_id)
            item_id = request.args.get("id", "")
            try:
                if outlook.split_id(item_id)[1] is None:
                    abort(404)
                name = store.attachment_name(item_id)
                pdf = validation.parse_invoice_pdf("attachment.pdf", store.download(item_id))
            except (ValueError, storage.StorageError) as exc:
                log.warning("emailed PDF not served: %s", exc)
                abort(404)
            name = name if name.lower().endswith(".pdf") and validation.safe_folder_name(name) == name else "attachment.pdf"
            resp = send_file(io.BytesIO(pdf), mimetype="application/pdf", download_name=name, as_attachment=False)
            resp.headers["Cache-Control"] = "private, no-cache"
            return resp

        if kind == "mail":
            app.get("/mail/file/<int:car_id>", endpoint="mail_file")(mail_file)

        @app.post(f"/{kind}/folders/<int:car_id>", endpoint=f"{kind}_choose_folder")
        def choose_folder(car_id):
            store = need_store()
            car_or_new(car_id)
            try:
                chosen = store.folder(request.form.get("folder_id") or None)
                if kind == "mail" and chosen["is_root"]:
                    raise storage.StorageError("Choose a folder in the mailbox, not the top of it.")
                if kind == "local":
                    local.claim()          # from now on, the folder must carry its marker (storage.py)
            except storage.StorageError as exc:
                log.warning("%s folder choice failed: %s", kind, exc)
                return render_storage(status=400, notice=f"Couldn't use that folder: {exc.user_message}")
            if car_id == 0:       # a car that doesn't exist yet: its import builds it, and saves the folder with it
                return redirect(url_for(f"{kind}_import", car_id=0, folder=chosen["id"]), 303)
            with connect() as conn:
                if kind == "mail":
                    db.set_car_mail_folder(conn, car_id, chosen["id"], chosen["path"])
                else:
                    db.set_car_folder(conn, car_id, chosen["id"], chosen["path"], kind)
            return redirect(url_for(page, msg="folders"), 303)

        @app.post(f"/{kind}/folders/<int:car_id>/clear", endpoint=f"{kind}_clear_folder")
        def clear_folder(car_id):
            need_store()
            car_or_404(car_id)
            with connect() as conn:
                if kind == "mail":
                    db.clear_car_mail_folder(conn, car_id)
                else:
                    db.clear_car_folder(conn, car_id, kind)
            return redirect(url_for(page, msg="folders"), 303)

        # ---- import the invoices already in the car's folder ----------------------

        @app.get(f"/{kind}/import/<int:car_id>", endpoint=f"{kind}_import")
        def import_start(car_id):
            """The page comes up at once, with a progress bar: its script then asks for the file list
            (/scan) and for each file in turn (/read), so nothing waits on the storage or OCR. With
            ?sync=1 (no JavaScript) everything is read before the page is sent."""
            store = need_store()
            car = car_or_new(car_id)
            ctx = import_context(car_id, kind)
            if not ctx["folder"] or not ctx["connected"]:
                return redirect(url_for(page), 303)
            if not request.args.get("sync"):
                with connect() as conn:
                    skipped = len(db.skipped_import_urls(conn, car_id))
                return import_page(store, car, rows=[], remaining=0, errors={}, scanning=True, skipped=skipped,
                                   categories=ctx["categories"], notice=None)
            try:
                files, pending, skipped = pending_files(store, ctx, car_id)
                log.debug("import scan car=%d listed=%d already_imported=%d pending=%d batch=%d", car_id, len(files),
                          len(files) - len(pending), len(pending), min(len(pending), IMPORT_BATCH))
                rows, round_timer = [], docinfo.Timer()
                for n, f in enumerate(pending[:IMPORT_BATCH]):
                    if n and round_timer.ms > IMPORT_BUDGET_SECONDS * 1000:
                        log.debug("import scan stopped after %d files: over the %ds budget", n, IMPORT_BUDGET_SECONDS)
                        break
                    rows.append(read_file(store, car, ctx, n, file_entry(f)))
            except onedrive.NotConnected:
                return redirect(url_for(page), 303)
            except onedrive.SignInExpired as exc:
                return render_storage(status=400, notice=exc.user_message)
            except storage.StorageError as exc:
                return render_storage(status=502, notice=exc.user_message)
            return render_import(store, car, rows, remaining=len(pending) - len(rows), skipped=skipped, status=200,
                                 notice=None if files else ("No emails found in that folder." if kind == "mail"
                                                            else "No PDFs found in that folder."))

        @app.get(f"/{kind}/import/<int:car_id>/scan", endpoint=f"{kind}_import_scan")
        def import_scan(car_id):
            """The files still to import, listed but not read: {listed, already_imported, remaining, files}."""
            store = need_store()
            car_or_new(car_id)
            ctx = import_context(car_id, kind)
            if not ctx["folder"]:
                return jsonify(error=info["choose_first"], url=url_for(page)), 409
            try:
                files, pending, skipped = pending_files(store, ctx, car_id)
            except onedrive.NotConnected as exc:
                return jsonify(error=exc.user_message, url=url_for(page)), 409
            except onedrive.SignInExpired as exc:
                return jsonify(error=exc.user_message, url=url_for(page)), 400
            except storage.StorageError as exc:
                log.warning("import scan failed: %s", exc)
                return jsonify(error=exc.user_message), 502
            # Every file, not a batch: the page reads them one request at a time, so none waits on the others.
            log.debug("import scan car=%d listed=%d already_imported=%d skipped=%d pending=%d", car_id, len(files),
                      len(files) - len(pending) - skipped, skipped, len(pending))
            return jsonify(listed=len(files), already_imported=len(files) - len(pending) - skipped, skipped=skipped,
                           remaining=0, files=[file_entry(f) for f in pending])

        @app.post(f"/{kind}/import/<int:car_id>/read", endpoint=f"{kind}_import_read")
        def import_read(car_id):
            """One file's row (an HTML fragment) for the page to add to the table."""
            store = need_store()
            car = car_or_new(car_id)
            n = request.form.get("n", type=int)
            f = {k: request.form.get(k, "") for k in ("id", "name", "folder", "url")}
            f["size"] = request.form.get("size", type=int)    # only logged; a number, so it can't forge a line
            if n is None or n < 0 or not f["id"] or not f["name"]:
                abort(400)
            ctx = import_context(car_id, kind)
            try:
                row = read_file(store, car, ctx, n, f)
            except storage.StorageError as exc:
                row = import_row(n, name=f["name"], folder=f["folder"], url=f["url"], note=exc.user_message)
            return render_template("_import_row.html", r=row, n=n, categories=ctx["categories"], errors={},
                                   kind=kind, link=store.link, car=car)

        @app.post(f"/{kind}/import/<int:car_id>/unskip", endpoint=f"{kind}_import_unskip")
        def import_unskip(car_id):
            """Offer the skipped files again."""
            need_store()
            car_or_404(car_id)
            with connect() as conn:
                db.clear_skipped_imports(conn, car_id)
            return redirect(url_for(f"{kind}_import", car_id=car_id), 303)

        @app.post(f"/{kind}/import/<int:car_id>", endpoint=f"{kind}_import_confirm")
        def import_confirm(car_id):
            store = need_store()
            car = car_or_new(car_id)
            ref_field = store.ref_field
            with connect() as conn:
                car_ids = {c["id"] for c in db.cars(conn)} | ({0} if car_id == 0 else set())
                cat_rows = db.categories(conn)
                cat_ids = {c["id"] for c in cat_rows}
                settings_row = db.get_settings(conn)
            cat_names = {c["id"]: c["name"] for c in cat_rows}
            cat_kinds = {c["id"]: c["kind"] for c in cat_rows}
            cats_by_id = {c["id"]: c for c in cat_rows}
            with connect() as conn:
                db_items = db.service_items(conn)
            count = min(request.form.get("count", type=int) or 0, 200)
            rows, chosen, errors, skip_urls, lease_picks = [], [], {}, [], []
            for n in range(count):
                def f(key, n=n):
                    return request.form.get(f"{key}_{n}", "")

                row = import_row(n, include=bool(f("include")), skip=bool(f("skip")), rename=bool(f("rename")), date=f("date"), category_id=f("category_id"),
                                 vendor=f("vendor"), amount=f("amount"), odometer_km=f("odometer_km"),
                                 notes=f("notes"), period_start=f("period_start"), period_end=f("period_end"),
                                 services=request.form.getlist(f"service_item_{n}"), gst_amount=f("gst_amount"),
                                 card_payment_ref=f("card_payment_ref"), bpay_biller_code=f("bpay_biller_code"),
                                 bpay_ref=f("bpay_ref"), car_fill=bool(f("car_fill")), attach=f("attach"),
                                 car_fields=[k for k in CAR_KEYS if f"car_{k}_{n}" in request.form],
                                 **{f"car_{k}": f(f"car_{k}") for k in CAR_KEYS},
                                 url=f("url"), name=f("name"), folder=f("folder"), id=f("id"),
                                 guess={k: f(f"guess_{k}") for k in LEARN_FIELDS})
                rows.append(row)
                if row["attach"] != "":          # merged into another row's expense: sorted out below
                    continue
                if row["skip"] and row["url"]:
                    skip_urls.append(row["url"])
                    continue
                if f("lease"):          # a lease form or agreement, kept with its lease: no expense, no rename
                    lease_picks.append((f("lease_id"), f("lease_doc"), row))
                    continue
                if not row["include"]:
                    continue
                # A hidden period (the category has none) isn't saved, or checked.
                dated = any(str(c["id"]) == row["category_id"] and c["has_period"] for c in cat_rows)
                values, row_errors = validation.parse_expense_form({
                    "car_id": car_id, "category_id": row["category_id"], "date": row["date"], "time": "12:00",
                    "amount": row["amount"], "odometer_km": row["odometer_km"], "vendor": row["vendor"],
                    "notes": row["notes"], "period_start": row["period_start"] if dated else "",
                    "period_end": row["period_end"] if dated else "",
                    "gst_amount": row["gst_amount"], "card_payment_ref": row["card_payment_ref"],
                    "bpay_biller_code": row["bpay_biller_code"], "bpay_ref": row["bpay_ref"],
                    ref_field: row["url"]}, tz, car_ids, cat_ids, ref_category_ids(cat_rows))
                if not row["url"]:
                    row_errors[ref_field] = "Missing link to the PDF."
                if not row_errors:
                    row_errors.update(powertrain_errors(car.get("kind") if car_id else None, values["category_id"],
                                                        row["services"], cats_by_id, db_items))
                if row["car_fill"]:             # the car's details, as confirmed on the row (checked with the row)
                    details, detail_errors = validation.parse_car_extras(lambda k, row=row: row[f"car_{k}"])
                    row["car_details"] = {k: v for k, v in details.items() if v is not None and f"car_{k}_{n}" in request.form}
                    row_errors.update(detail_errors)
                if row_errors:
                    errors[n] = " ".join(row_errors.values())
                else:
                    chosen.append((row, values))
            attachments = attach_rows(rows, count, {r["n"] for r, _ in chosen}, ref_field, errors)
            if errors:
                return render_import(store, car, rows, errors=errors, status=400)
            with connect() as conn:
                known = db.known_invoices(conn, [v[ref_field] for _, v in chosen])
            fresh = [row_values for row_values in chosen if row_values[1][ref_field] not in known]
            if car_id == 0:
                if not fresh:
                    return render_import(store, car, rows, status=400, notice="Tick at least one invoice to import."
                                         if not chosen else "Those invoices were already imported.")
                spec, car_errors = new_car_spec(fresh, cat_kinds, ctx_folder(kind))
                if car_errors is not None:
                    return render_import(store, car, rows, status=400, needs_car=True, new_car=request.form,
                                         car_errors=car_errors, notice=None)
                with connect() as conn:
                    car_id = db.add_car(conn, spec)
                    where = ctx_folder(kind)
                    if kind == "mail":
                        db.set_car_mail_folder(conn, car_id, where["folder_id"], where["folder_path"])
                    else:
                        db.set_car_folder(conn, car_id, where["folder_id"], where["folder_path"], kind)
                car = {**car, "id": car_id, "name": spec["name"]}
                for _, values in chosen:
                    values["car_id"] = car_id
                log.info("car %s made from an import", car_id)
            if skip_urls:
                with connect() as conn:
                    db.skip_imports(conn, car_id, skip_urls)
            kept = 0
            for lease_id, doc, row in lease_picks:
                try:
                    ref = (validation.parse_invoice_path if ref_field == "invoice_path" else validation.parse_invoice_url)(row["url"])
                except ValueError:
                    continue
                if car_id and ref and lease_id.isdigit() and doc in validation.LEASE_DOCUMENTS:
                    with connect() as conn:
                        kept += db.set_lease_document(conn, car_id, int(lease_id), doc, ref_field, ref)
            if kept:
                log.info("import kept %d file(s) as lease documents of car %s", kept, car_id)
            if not chosen:
                if kept and len(lease_picks) == kept and not skip_urls:
                    return redirect(url_for("car_lease", car_id=car_id, msg="lease_document_kept"), 303)
                if skip_urls or kept:
                    return redirect(url_for(f"{kind}_import", car_id=car_id), 303)
                return render_import(store, car, rows, status=400, notice="Tick at least one invoice to import.")
            imported, renamed, rename_failed, copied, copy_failed = [], 0, 0, 0, 0
            for row, values in chosen:
                if values[ref_field] in known:   # a double submit imports once
                    continue
                if kind == "mail":      # an email's PDF is copied into the car's storage, not renamed
                    outcome = keep_attachment(store, row, values)
                    copied += outcome == "copied"
                    copy_failed += outcome == "failed"
                target = validation.invoice_filename(
                    values["occurred_at"].astimezone(ZoneInfo(tz)), cat_names[values["category_id"]], values["vendor"],
                    settings_row["invoice_date_format"], settings_row["invoice_name_format"],
                    car=car["name"], amount=values["amount"])
                if kind != "mail" and row["rename"] and row["id"] and target != row["name"]:
                    try:
                        values[ref_field] = store.rename(row["id"], target)
                        if kind == "local":
                            row["id"] = values[ref_field]    # there the reference is where the file is
                        renamed += 1
                    except storage.StorageError as exc:
                        rename_failed += 1           # import it anyway, still pointing at the old name
                        log.warning("import rename item=%s to=%s failed: %s", row["id"], docinfo.quote(target), exc)
                with connect() as conn:
                    expense_id = db.insert_expense(conn, values)
                    if kind == "mail":
                        db.record_mail_import(conn, car_id, row["url"], expense_id)
                    save_services(conn, expense_id, values["category_id"], row["services"])
                    if row["car_fill"] and row.get("car_details"):      # blanks only: never replaces what the car has
                        db.fill_car_details(conn, car_id, row["car_details"])
                    for extra in attachments.get(row["n"], []):         # the other PDFs of this expense
                        db.add_document(conn, expense_id, extra["name"], ref_field, extra["url"])
                imported.append((row, values))
            log.debug("import confirm car=%d rows=%d imported=%d skipped_known=%d renamed=%d rename_failed=%d", car_id,
                      len(chosen), len(imported), len(chosen) - len(imported), renamed, rename_failed)
            refresh_dashboard()
            learn_from(store, imported)
            more = {"store": kind} if kind == "local" else {}
            if copied or copy_failed:
                more.update(copied=copied, copy_failed=copy_failed)
            if kept:
                more.update(leases=kept)
            return redirect(url_for("index", msg="imported", n=len(imported), renamed=renamed,
                                    rename_failed=rename_failed, **more), 303)

        def api_browse(car_id):
            """One folder for the Add form's file picker. Starts in the car's folder; ?folder=<id> goes
            elsewhere (up included) and ?folder=root is the top of the storage."""
            store = need_store()
            car_or_404(car_id)
            wanted = request.args.get("folder")
            with connect() as conn:
                folder = car_folder(conn, car_id, kind)
                ready = connected(conn, kind)
            if not ready:
                return jsonify(error=onedrive.NotConnected().user_message, url=url_for(page)), 409
            if not wanted and not folder:
                return jsonify(error=info["choose_first"], url=url_for(page)), 409
            folder_id = None if wanted == "root" else (wanted or folder["folder_id"])
            try:
                here, folders, files = store.browse(folder_id, fresh=not wanted)
            except onedrive.SignInExpired as exc:
                return jsonify(error=exc.user_message, url=url_for(page)), 409
            except storage.StorageError as exc:
                log.warning("%s file picker failed: %s", kind, exc)
                return jsonify(error=exc.user_message), 502
            return jsonify(id=here["id"], path=here["path"], parent_id=here["parent_id"], is_root=here["is_root"],
                           folders=folders, files=files)

        if kind != "mail":      # the Add form's file picker: an email's PDF is imported, not picked
            app.get(f"/api/{kind}/browse/<int:car_id>", endpoint=f"api_{kind}_browse")(api_browse)

    def keep_attachment(store, row, values):
        """An email's PDF goes into the car's invoice storage like an uploaded one (category subfolder, the
        file name template), and the expense links that copy. -> "copied", "failed" (it links the email
        instead), or None when there is nothing to copy (no PDF, or no storage folder for the car)."""
        if outlook.split_id(row["id"])[1] is None or not upload_ready(values["car_id"]):
            return None
        try:
            pdf = validation.parse_invoice_pdf(row["name"] if row["name"].lower().endswith(".pdf") else "attachment.pdf",
                                               store.download(row["id"]))
            kept_in, ref = upload_invoice(values, pdf)
        except (ValueError, storage.StorageError) as exc:
            log.warning("emailed PDF %s not saved in the car's storage: %s", docinfo.quote(row["name"]), exc)
            return "failed"
        values["invoice_url"] = values["invoice_path"] = None
        values[kept_in.ref_field] = ref
        return "copied"

    for kind in SOURCES:
        storage_routes(kind)

    def name_example(values):
        """What an uploaded file would be called under the saved (or just-typed, if valid) settings."""
        try:
            date_pattern = validation.parse_date_pattern(values.get("invoice_date_format"))
            template = validation.parse_name_format(values.get("invoice_name_format"))
        except ValueError:
            date_pattern = template = None
        return validation.invoice_filename(datetime(2026, 11, 7, 12, 0), "Tyres", "Tyre Shop", date_pattern, template,
                                           car="Model Y", amount=Decimal("1234.50"))

    @app.route("/settings", methods=["GET", "POST"])
    def settings():
        values, errors = {}, {}
        tax_values = {}
        if request.method == "POST":
            for field in ("app_url", "grafana_url"):
                raw = request.form.get(field, "")
                values[field] = raw
                try:
                    values[field] = validation.parse_url_setting(raw)
                except ValueError as exc:
                    errors[field] = str(exc)
            raw = request.form.get("invoice_date_format", "")
            values["invoice_date_format"] = raw
            try:
                values["invoice_date_format"] = validation.parse_date_pattern(raw)
            except ValueError as exc:
                errors["invoice_date_format"] = str(exc)
            raw = request.form.get("invoice_name_format", "")
            values["invoice_name_format"] = raw
            try:
                values["invoice_name_format"] = validation.parse_name_format(raw)
            except ValueError as exc:
                errors["invoice_name_format"] = str(exc)
            with connect() as conn:
                tax_changes, tax_values, tax_errors = tax.parse_choices(request.form, db.tax_brackets(conn), today_here(),
                                                                         TAX_PEOPLE)
            errors.update(tax_errors)
            values["lease_budgets"] = bool(request.form.get("lease_budgets"))
            raw = request.form.get("multi_amount", "max")
            values["multi_amount"] = raw
            try:
                values["multi_amount"] = validation.parse_multi_amount(raw)
            except ValueError as exc:
                errors["multi_amount"] = str(exc)
            if not errors:
                # The fields show the defaults as real text, so saving them unchanged means "default".
                date_pattern = values["invoice_date_format"]
                name_format = values["invoice_name_format"]
                with connect() as conn:
                    db.save_settings(conn, values["app_url"], values["grafana_url"],
                                     None if date_pattern == validation.DEFAULT_DATE_PATTERN else date_pattern,
                                     None if name_format == validation.DEFAULT_NAME_FORMAT else name_format,
                                     values["multi_amount"], values["lease_budgets"])
                    for (person, year), (lower, medicare) in tax_changes.items():
                        db.set_tax_choice(conn, person, year, lower, medicare)
                refresh_dashboard()         # the cost-after-tax panels appear and go with the tax brackets
                log.info("settings saved")
                return redirect(url_for("settings", msg="settings"), 303)
        else:
            with connect() as conn:
                values = db.get_settings(conn)
        return render_settings(values, errors, tax_values=tax_values)

    def render_settings(values, errors, status=None, notify_errors=None, notify_format=None, test=None, tax_values=None):
        """The Settings page. The notification address is a secret: the page is only told its host. `tax_values` is
        what a refused form sent for the tax table, shown back as it was."""
        with connect() as conn:
            address, saved_format = db.get_notify(conn)
            notify_teslamate = db.get_notify_teslamate(conn)
            job_list = scheduler.status(conn)
            ev_rates = db.ev_charging_table(conn)
            rows, today = db.tax_brackets(conn), today_here()
            years = tax.settings_years(rows, today)
            people = TAX_PEOPLE if db.any_associate_lease(conn) else TAX_PEOPLE[:1]
            tax_rows = tax.settings_rows(rows, db.tax_choices(conn), {p: db.tax_positions(conn, p, years) for p in people},
                                         today, people)
        return render_template("settings.html", values=values, errors=errors, aliases=validation.NAME_ALIASES,
                               job_list=job_list, jobs_mode=jobs_mode, ev_rates=ev_rates, ev_source=ato.URLS["ev_charging"],
                               tax_rows=tax_rows, tax_people={p: tax.PEOPLE[p] for p in people}, tax_values=tax_values or {},
                               medicare=tax.MEDICARE, tax_field=tax.field, fbt_label=lease.fbt_label,
                               date_default=validation.DEFAULT_DATE_PATTERN, name_default=validation.DEFAULT_NAME_FORMAT,
                               multi_choices=validation.MULTI_AMOUNT_CHOICES, example=name_example(values),
                               message=MESSAGES.get(request.args.get("msg", "")),
                               notify={"host": notify.host_of(address) if address else None, "errors": notify_errors or {},
                                       "format": notify_format or saved_format, "formats": notify.FORMATS, "test": test,
                                       "teslamate": notify_teslamate},
                               ), status or (400 if errors or notify_errors else 200)

    @app.post("/settings/notifications")
    def notifications_save():
        """Save the notification address and format. A blank address keeps the one saved (it is never shown
        back); the Remove box clears it."""
        errors = {}
        raw_format = request.form.get("notify_format", "")
        try:
            fmt = validation.parse_notify_format(raw_format)
        except ValueError as exc:
            fmt = None
            errors["notify_format"] = str(exc)
        try:
            address = validation.parse_notify_url(request.form.get("notify_url", ""))
        except ValueError as exc:
            address = None
            errors["notify_url"] = str(exc)
        if errors:
            with connect() as conn:
                values = db.get_settings(conn)
            return render_settings(values, {}, notify_errors=errors, notify_format=fmt or "ntfy")
        with connect() as conn:
            if request.form.get("notify_clear"):
                address = None
            elif address is None:
                address = db.get_notify(conn)[0]
            db.save_notify(conn, address, fmt)
            db.save_notify_teslamate(conn, request.form.get("notify_teslamate"))
        log.info("notification settings saved: %s", "on (" + notify.host_of(address) + ")" if address else "off")
        return redirect(url_for("settings", msg="notify_saved"), 303)

    @app.post("/settings/notifications/test")
    def notifications_test():
        """Send one notice now to the saved address, and say how it went, in words that never hold the address."""
        with connect() as conn:
            address, fmt = db.get_notify(conn)
            values = db.get_settings(conn)
        if not address:
            test = {"ok": False, "text": "Save an address first: there is nowhere to send it yet."}
        else:
            try:
                send_notice(address, fmt, "GarageBooks test notice",
                            "If you can read this, notifications are working.", tags=("white_check_mark",))
                test = {"ok": True, "text": f"A test notice was sent to {notify.host_of(address)}. Check that it arrived."}
            except jobs.JobError as exc:
                test = {"ok": False, "text": str(exc)}
        return render_settings(values, {}, status=200, test=test)

    @app.post("/api/prefill")
    def api_prefill():
        """Read an invoice PDF and propose the form's fields, for the Add page: the one just chosen
        on the computer, or one already in a storage (onedrive_id or local_id, and name)."""
        timer = docinfo.Timer()
        upload = request.files.get("invoice_file")
        filename = upload and upload.filename
        data = upload.read() if upload else b""
        kind = next((k for k in STORES if request.form.get(f"{k}_id") and store_for(k)), None)
        if not upload and kind:
            filename = request.form.get("name", "")
            try:
                data = store_for(kind).download(request.form[f"{kind}_id"])
            except storage.StorageError as exc:
                log.warning("prefill from %s failed: %s", STORES[kind]["label"], exc)
                return jsonify(error=exc.user_message), 502
        try:
            pdf = validation.parse_invoice_pdf(filename, data)
        except ValueError as exc:
            log.debug("prefill file=%s refused: %s", docinfo.quote(filename), exc)
            return jsonify(error=str(exc)), 400
        if pdf is None:
            log.debug("prefill refused: no file sent")
            return jsonify(error="Choose a PDF first."), 400
        with connect() as conn:
            categories = db.categories(conn)
            learned = db.learned(conn, invoices.LEARN_AFTER)
            multi_amount = db.get_settings(conn)["multi_amount"]
        guess = invoices.propose("", filename, invoices.extract_text(pdf), [c["name"] for c in categories], learned,
                                 multi_amount)
        have, missing = docinfo.found(guess)
        log.debug("prefill file=%s bytes=%d sha256=%s found=%s missing=%s ms=%d", docinfo.quote(filename), len(pdf),
                  docinfo.sha12(pdf), have, missing, timer.ms)
        by_name = {c["name"]: c["id"] for c in categories}
        # The form shows any period it holds, so it is only given one its category has.
        if not any(c["name"] == guess["category"] and c["has_period"] for c in categories):
            guess.update(period_start=None, period_end=None)
        if not any(c["name"] == guess["category"] and c["budget_pool"] == claim_lib.RENEWAL_POOL for c in categories):
            guess.update(card_payment_ref=None, bpay_biller_code=None, bpay_ref=None)
        return jsonify(
            date=guess["date"].isoformat() if guess["date"] else None,
            category_id=by_name.get(guess["category"]), vendor=guess["vendor"],
            amount=f"{guess['amount']:.2f}" if guess["amount"] is not None else None,
            odometer_km=guess["odometer_km"], notes=guess["notes"] or None,
            gst_amount=f"{guess['gst_amount']:.2f}" if guess["gst_amount"] is not None else None,
            card_payment_ref=guess["card_payment_ref"], bpay_biller_code=guess["bpay_biller_code"],
            bpay_ref=guess["bpay_ref"],
            period_start=guess["period_start"].isoformat() if guess["period_start"] else None,
            period_end=guess["period_end"].isoformat() if guess["period_end"] else None)

    @app.get("/api/duplicates")
    def api_duplicates():
        """Likely duplicates of the expense being entered: the Add page asks before it submits."""
        car_id = request.args.get("car_id", type=int)
        if car_id is None:
            return jsonify(error="car_id required"), 400
        try:
            when = validation.parse_local_datetime(request.args.get("date"), request.args.get("time"), tz)
            amount = validation.parse_amount(request.args.get("amount"))
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
        with connect() as conn:
            return jsonify(matches=dup_view(db.find_duplicates(conn, car_id, when, amount)))

    @app.get("/api/fuel")
    def api_fuel():
        """What a fill-up would show once saved, for the form to display while it is being filled in: the distance
        since the last fuel-up, then L/100 km of the tank. The same view the list reads, run on the fill-up and
        undone (see db.fuel_preview), so the two can't disagree. Nothing is saved."""
        with connect() as conn:
            car_ids = {c["id"] for c in db.cars(conn)}
            cat_ids = {c["id"] for c in db.categories(conn)}
        values, errors = validation.parse_expense_form({"amount": "0", **request.args.to_dict()}, tz, car_ids, cat_ids)
        if errors:
            return jsonify(error=" ".join(errors.values())), 400
        expense_id = request.args.get("expense_id", type=int)
        with connect() as conn:
            row = db.fuel_preview(conn, values, expense_id)
        if row is None:
            return jsonify(error="No such expense."), 404
        row = {**row, "litres": values.get("litres"), "odometer_km": values.get("odometer_km")}
        figure = row["l_per_100km"] if values.get("litres") is not None else None
        return jsonify(
            distance_km=None if row["distance_km"] is None else float(row["distance_km"]),
            l_per_100km=None if figure is None else float(figure),
            lines=fuel.describe(row))

    @app.get("/api/odometer")
    def api_odometer():
        """Odometer at a given local date/time — drives the form's auto-fill."""
        car_id = request.args.get("car_id", type=int)
        if car_id is None:
            return jsonify(error="car_id required"), 400
        try:
            when = validation.parse_local_datetime(
                request.args.get("date"), request.args.get("time"), tz)
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
        # Only TeslaMate's: an older reading entered by hand is not the odometer on this day.
        with connect() as conn:
            reading = sources.odometer_at(conn, teslamate(), car_id, when, manual=False)
        return jsonify(odometer_km=reading.km if reading else None)

    return app
