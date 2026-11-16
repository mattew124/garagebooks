"""GarageBooks: a small web UI for recording what the car costs to own.

Data lives in the `garagebooks` schema, in TeslaMate's database where there is a TeslaMate;
the Grafana dashboard in grafana/ turns it (plus TeslaMate's own charging costs, when it has
them) into total cost of ownership. TeslaMate is optional: see sources.py.
"""

import hashlib
import hmac
import logging
import os
from pathlib import Path
import time
from decimal import Decimal
from datetime import date, datetime, time as dtime, timedelta, timezone
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from flask import (Flask, abort, g, has_request_context, jsonify, make_response, redirect, render_template, request,
                   send_file, url_for)

from werkzeug.exceptions import RequestEntityTooLarge
from werkzeug.middleware.proxy_fix import ProxyFix

from . import dashboard, db, docinfo, icons, invoices, lease, logs, onedrive, reminders, sources, storage, updates, validation

log = logging.getLogger("garagebooks.app")

MESSAGES = {
    "saved": "Expense saved.",
    "updated": "Expense updated.",
    "deleted": "Expense deleted.",
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
    "reading_added": "Odometer reading saved.",
    "reading_deleted": "Odometer reading deleted.",
    "lease_added": "Lease added.",
    "lease_saved": "Lease saved.",
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
    "category_pool": "Budget pool saved.",
    "settings": "Settings saved.",
    "folders": "Folders saved.",
    "connected": "OneDrive connected.",
    "credentials": "App registration saved. Now connect OneDrive.",
    "disconnected": "OneDrive disconnected.",
    "imported": "Invoices imported.",
    "category_folder": "OneDrive folder saved.",
    "learned_deleted": "Removed.",
}

# PDFs read per page without JavaScript: each is downloaded and text-extracted (OCR for scans) while
# the request waits, so a big folder is worked through in rounds. The normal page reads them all, one request each.
TESLAMATE_RECHECK_SECONDS = 60   # how often the app looks again at whether TeslaMate is there and readable
TOKEN_MARGIN_SECONDS = 120   # a kept access token is dropped this long before it expires
IMPORT_BATCH = 10
# ...and stop reading more files once a round has taken this long (OCR can take 10+ seconds a file,
# and a request that outlasts gunicorn's 120 s timeout is killed), leaving the rest for the next round.
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


def create_app(connect=None, onedrive_transport=None, release_fetch=None, icon_fetch=None):
    """`connect` is injectable so tests can hand in a connection to a scratch DB,
    `onedrive_transport` so they can stand in for Microsoft, `release_fetch` for GitHub and `icon_fetch` for the provider icon's source."""
    app = Flask(__name__)
    logs.configure(app.logger, os.environ.get("LOG_LEVEL"))
    log = app.logger
    tz = os.environ.get("APP_TZ", "UTC")
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
            wrote = dashboard.export(dashboard_dir, tz, os.environ.get("BASE_PATH", "").rstrip("/"), start=start,
                                     teslamate=known["teslamate"])
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
    icon_cache = icons.IconCache(fetch=icon_fetch, enabled=os.environ.get("PROVIDER_ICONS", "1").strip() != "0")
    base_path = os.environ.get("BASE_PATH", "")
    if base_path:
        app.wsgi_app = PrefixMiddleware(app.wsgi_app, base_path)
    # The proxy's scheme and host, so the OneDrive callback address is the
    # public one. The app is only reachable through the proxy (or on loopback).
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=0, x_proto=1, x_host=1)
    log.info("started: version=%s tz=%s base_path=%s onedrive=%s invoice_folder=%s update_check=%s", version or "dev",
             tz, base_path or "/", "on" if drive else "off", "on" if local else "off",
             "on" if update_check.active else "off")
    update_check.status()    # starts the first check now, so the footer knows by the first page

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

    @app.template_filter("isodate")
    def isodate(value):
        return value.astimezone(ZoneInfo(tz)).date().isoformat()

    @app.template_filter("day")
    def day(value):
        return value.astimezone(ZoneInfo(tz)).strftime("%d %b %Y")

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
        return {"onedrive_enabled": drive is not None, "local_enabled": local is not None,
                "uploads_enabled": bool(drive or local), "storage": provider, "icon_credit": icons.CREDIT}

    @app.context_processor
    def footer():
        """The docs link, the running version and whether a newer release exists (never waits for GitHub)."""
        return {"docs_url": updates.DOCS_URL, "about": update_check.status()}

    @app.errorhandler(RequestEntityTooLarge)
    def too_large(_exc):
        return render_template("error.html", title="File too large",
                               text="Invoice PDF is too large (max 20 MB). Go back and choose a smaller file."), 413

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

    def form_context(values=None, errors=None, expense_id=None):
        with connect() as conn:
            account = db.onedrive_account(conn) if drive else None
            folders = db.car_folders(conn)
            return {
                # Which storage each car's invoices go to, for the file picker: only where it can be used.
                "car_storage": {car_id: f["backend"] for car_id, f in folders.items() if store_for(f["backend"])},
                "default_storage": "onedrive" if drive else "local",
                "cars": sorted(db.cars(conn), key=lambda c: c["sold_at"] is not None),   # sold cars last (the sort is stable)
                "categories": db.categories(conn),
                "values": values or {},
                "errors": errors or {},
                "view": list_view(),
                "expense_id": expense_id,
                "onedrive_connected": bool(account and account["refresh_token"]),
            }

    def dup_view(matches):
        """Possible duplicates as the page and the JSON endpoint show them."""
        return [{"id": m["id"], "date": m["occurred_at"].astimezone(ZoneInfo(tz)).strftime("%d %b %Y"),
                 "category": m["category"], "vendor": m["vendor"] or "", "amount": f"{m['amount']:.2f}",
                 "amount_text": money(m["amount"]), "edit_url": url_for("edit", expense_id=m["id"])}
                for m in matches]

    def parse(form, check_duplicates=False):
        """-> (values, errors). With check_duplicates, a likely duplicate is reported as
        errors["_duplicates"] (before anything is uploaded) unless the owner has said to save anyway."""
        with connect() as conn:
            car_ids = {c["id"] for c in db.cars(conn)}
            cat_ids = {c["id"] for c in db.categories(conn)}
        values, errors = validation.parse_expense_form(form, tz, car_ids, cat_ids)
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
            return storage.OneDriveStore(drive, access_token)
        return local if kind == "local" else None

    def car_folder(conn, car_id, kind):
        """The car's folder, if it is in that storage."""
        folder = db.car_folders(conn).get(car_id)
        return folder if folder and folder["backend"] == kind else None

    def connected(conn, kind):
        """Ready to use? OneDrive needs a sign-in; the folder on the server needs nothing."""
        return kind == "local" or bool((db.onedrive_account(conn) or {}).get("refresh_token"))

    kept = {}   # this worker's access token: {"key": the stored refresh token it came with, "token", "until"}

    def access_token():
        """An access token for Graph. Microsoft's last about an hour, so one is kept and reused until
        shortly before it expires (a refresh costs most of a second). It is only reused while the stored
        refresh token is the one it came with: disconnecting, signing in again or new credentials all
        change that, so none of them leaves a kept token in use. Otherwise: spend the stored refresh token
        and store the one Microsoft hands back."""
        with connect() as conn:
            account = db.onedrive_account(conn, lock=True)
            if not account or not account["refresh_token"]:
                kept.clear()
                raise onedrive.NotConnected()
            if kept.get("key") == account["refresh_token"] and kept["until"] > time.monotonic():
                return kept["token"]
            tokens = drive.refresh(account["client_id"], drive.decrypt(account["client_secret"]),
                                   drive.decrypt(account["refresh_token"]))
            key = account["refresh_token"]
            if tokens.get("refresh_token"):
                key = drive.encrypt(tokens["refresh_token"])
                db.save_onedrive_account(conn, refresh_token=key)
        lifetime = tokens.get("expires_in", 0) - TOKEN_MARGIN_SECONDS
        kept.clear()
        if lifetime > 0:
            kept.update(key=key, token=tokens["access_token"], until=time.monotonic() + lifetime)
        return tokens["access_token"]

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

    def form_values(form):
        """Echo the raw submission back into the form after a validation error."""
        return {k: form.get(k, "") for k in (
            "car_id", "category_id", "date", "time", "amount", "odometer_km",
            "vendor", "notes", "invoice_url", "invoice_path")}

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
        return view

    def sort_link(column):
        """Address of the list ordered by this column: the other way round if it already is."""
        view = list_view()
        current = (view.get("sort", "date"), view.get("dir", "desc"))
        if current[0] == column:
            way = "asc" if current[1] == "desc" else "desc"
        else:
            way = "desc" if column in ("date", "amount", "odometer") else "asc"
        return url_for("index", **{**view, "sort": column, "dir": way})

    @app.get("/")
    def index():
        view = list_view()
        category_id = view.get("category")
        with connect() as conn:
            ctx = {
                "view": view, "sort_link": sort_link,
                "sorts": {"date": "Date", "category": "Category", "vendor": "Vendor",
                          "amount": "Amount", "odometer": "Odometer"},
                "expenses": db.list_expenses(conn, category_id, sort=view.get("sort", "date"),
                                             direction=view.get("dir", "desc")),
                "totals": db.totals(conn, category_id),
                "categories": db.categories(conn),
                "category_id": category_id,
                "message": MESSAGES.get(request.args.get("msg", "")),
            }
        if request.args.get("msg") == "imported":
            ctx["message"] = import_message()
        return render_template("index.html", **ctx)

    def import_message():
        n, renamed, failed = (request.args.get(k, 0, type=int) for k in ("n", "renamed", "rename_failed"))
        text = f"Imported {n} invoice{'' if n == 1 else 's'}." if n else MESSAGES["imported"]
        if renamed:
            text += f" Renamed {renamed}{'' if request.args.get('store') == 'local' else ' in OneDrive'}."
        if failed:
            text += f" Couldn't rename {failed} (a file with that name may already exist): imported with the old name."
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
        }
        with connect() as conn:
            offer = bool(db.stored_open_reminders(conn, row["car_id"]))
        return render_template("form.html", **form_context(values, expense_id=expense_id), reminder_offer=offer)

    @app.post("/<int:expense_id>/edit")
    def update(expense_id):
        values, errors = parse(request.form)
        if errors:
            ctx = form_context(form_values(request.form), errors, expense_id)
            return render_template("form.html", **ctx), 400
        with connect() as conn:
            if not db.update_expense(conn, expense_id, values):
                abort(404)
        refresh_dashboard()
        log.info("expense %s updated", expense_id)
        return redirect(url_for("index", **list_view(), msg=saved_msg("updated")), 303)

    @app.post("/<int:expense_id>/delete")
    def delete(expense_id):
        with connect() as conn:
            if not db.delete_expense(conn, expense_id):
                abort(404)
        refresh_dashboard()
        log.info("expense %s deleted", expense_id)
        return redirect(url_for("index", **list_view(), msg="deleted"), 303)

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
        with connect() as conn:
            cats = db.categories(conn)
        return render_template("categories.html", categories=cats, error=error, pools=lease.POOLS,
                               message=MESSAGES.get(request.args.get("msg", ""))), (400 if error else 200)

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

    @app.post("/cars")
    def car_add():
        values, errors = validation.parse_car_form(request.form, db.CAR_KINDS)
        if errors:
            return render_cars({"new": errors}, {"new": request.form}, 400)
        with connect() as conn:
            car_id = db.add_car(conn, values)
        log.info("car %s added by hand", car_id)
        return redirect(url_for("cars_page", msg="car_added"), 303)

    @app.post("/cars/<int:car_id>")
    def car_update(car_id):
        car_or_404(car_id)
        values, errors = validation.parse_car_form(request.form, db.CAR_KINDS)
        if errors:
            return render_cars({car_id: errors}, {car_id: request.form}, 400)
        with connect() as conn:
            db.update_car(conn, car_id, values)
        log.info("car %s updated", car_id)
        return redirect(url_for("cars_page", msg="car_saved"), 303)

    @app.post("/cars/<int:car_id>/sale")
    def car_sale(car_id):
        car_or_404(car_id)
        now = datetime.now(ZoneInfo(tz))
        if request.form.get("action") == "clear":
            values, errors = {"sold_at": None, "sale_price": None}, {}
        else:
            values, errors = validation.parse_sale(request.form, tz, now)
        if errors:
            return render_cars({car_id: errors}, {car_id: request.form}, 400)
        with connect() as conn:
            db.set_sale(conn, car_id, values["sold_at"], values["sale_price"])
            first = db.first_expense_at(conn, car_id)
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

    @app.post("/cars/<int:car_id>/readings")
    def reading_add(car_id):
        car_or_404(car_id)
        errors = {}
        try:
            # A reading is for a day: noon, or this moment for today's (noon may still be ahead).
            read_at = min(validation.parse_local_datetime(request.form.get("date"), "12:00", tz),
                          datetime.now(ZoneInfo(tz)).replace(microsecond=0)) \
                if request.form.get("date") == datetime.now(ZoneInfo(tz)).date().isoformat() \
                else validation.parse_local_datetime(request.form.get("date"), "12:00", tz)
        except ValueError as exc:
            errors["date"] = str(exc)
        try:
            km = validation.parse_odometer(request.form.get("km"))
            if km is None:
                raise ValueError("Enter the odometer reading.")
        except ValueError as exc:
            errors["km"] = str(exc)
        if errors:
            return render_cars({car_id: errors}, {car_id: request.form}, 400)
        with connect() as conn:
            db.add_reading(conn, car_id, read_at, km)
        refresh_dashboard()
        log.info("odometer reading added for car %s", car_id)
        return redirect(url_for("cars_page", msg="reading_added"), 303)

    @app.post("/cars/<int:car_id>/readings/<int:reading_id>/delete")
    def reading_delete(car_id, reading_id):
        with connect() as conn:
            if not db.delete_reading(conn, car_id, reading_id):
                abort(404)
        return redirect(url_for("cars_page", msg="reading_deleted"), 303)

    # ---- leases (roadmap L1) -------------------------------------------------------

    def today_here():
        return datetime.now(ZoneInfo(tz)).date()

    def lease_context(car, **extra):
        return dict(car=car, pools=lease.POOLS, frequencies={k: v[0] for k, v in lease.FREQUENCIES.items()},
                    fbt_methods=lease.FBT_METHODS, tax_treatments=lease.TAX_TREATMENTS,
                    message=MESSAGES.get(request.args.get("msg", "")), **extra)

    def lease_view(conn, row, today):
        """A lease with its budgets and the read-only figures worked out from them."""
        budgets = db.lease_budgets(conn, row["id"])
        return {**row, "budgets": {b["pool"]: b["yearly_amount"] for b in lease.initial_budgets(row, budgets)},
                "later_budgets": [b for b in budgets if b["effective_from"] != row["start_date"]],
                "figures": lease.derived(row, budgets, today)}

    def render_lease(car, status=200):
        today = today_here()
        with connect() as conn:
            current = db.current_lease(conn, car["id"], today)
            views = [lease_view(conn, row, today) for row in db.car_leases(conn, car["id"])]
        shown = next((v for v in views if current and v["id"] == current["id"]), None)
        others = [v for v in views if v is not shown]
        return render_template("lease.html", **lease_context(car, shown=shown, others=others, today=today)), status

    def render_lease_form(car, lease_id, values, errors, status=200):
        return render_template("lease_form.html", **lease_context(car, lease_id=lease_id, values=values, errors=errors)), status

    def lease_form_values(row, budgets):
        """A saved lease as the form's text fields."""
        values = {k: ("" if v is None else v) for k, v in row.items()}
        for pool, amount in budgets.items():
            values[f"budget_{pool}"] = amount
        return values

    def parse_lease(car_id, lease_id=None):
        values, errors = validation.parse_lease_form(request.form, lease.POOLS, lease.FREQUENCIES,
                                                     lease.FBT_METHODS, lease.TAX_TREATMENTS)
        if not errors:
            with connect() as conn:
                clash = db.overlapping_lease(conn, car_id, values["start_date"], values["end_date"], lease_id)
            if clash:
                errors["start_date"] = (f"These dates overlap the lease from {clash['start_date']:%d %b %Y} "
                                        f"to {clash['end_date']:%d %b %Y}. A car has one current lease: "
                                        "end that one first, or change these dates.")
        return values, errors

    @app.get("/cars/<int:car_id>/lease")
    def car_lease(car_id):
        return render_lease(car_or_404(car_id))

    @app.get("/cars/<int:car_id>/lease/new")
    def car_lease_new(car_id):
        car = car_or_404(car_id)
        return render_lease_form(car, None, {"fbt_method": "statutory", "statutory_percent": "20",
                                             "tax_treatment": "employee_contribution", "start_date": today_here().isoformat()}, {})

    @app.post("/cars/<int:car_id>/lease/new")
    def car_lease_create(car_id):
        car = car_or_404(car_id)
        values, errors = parse_lease(car_id)
        if errors:
            return render_lease_form(car, None, request.form, errors, 400)
        with connect() as conn:
            lease_id = db.add_lease(conn, car_id, values)
        if lease_id is None:      # another lease was added between the check and the save
            return render_lease_form(car, None, request.form, {"start_date": "These dates overlap another lease of this car."}, 400)
        log.info("lease %s added to car %s", lease_id, car_id)
        return redirect(url_for("car_lease", car_id=car_id, msg="lease_added"), 303)

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
        values, errors = parse_lease(car_id, lease_id)
        if errors:
            return render_lease_form(car, lease_id, request.form, errors, 400)
        with connect() as conn:
            outcome = db.update_lease(conn, car_id, lease_id, values)
        if outcome == "missing":
            abort(404)
        if outcome == "overlap":
            return render_lease_form(car, lease_id, request.form, {"start_date": "These dates overlap another lease of this car."}, 400)
        log.info("lease %s of car %s updated", lease_id, car_id)
        return redirect(url_for("car_lease", car_id=car_id, msg="lease_saved"), 303)

    @app.post("/cars/<int:car_id>/lease/<int:lease_id>/delete")
    def car_lease_delete(car_id, lease_id):
        car_or_404(car_id)
        with connect() as conn:
            if not db.delete_lease(conn, car_id, lease_id):
                abort(404)
        log.info("lease %s of car %s deleted", lease_id, car_id)
        return redirect(url_for("car_lease", car_id=car_id, msg="lease_deleted"), 303)

    # ---- the FBT year report (roadmap L2) -------------------------------------------

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
            leases = [(row, db.lease_budgets(conn, row["id"])) for row in rows]
            costs = db.fbt_costs(conn, car_id, year, tz)
            years = lease.fbt_years(rows, db.fbt_years_with_expenses(conn, car_id, tz))
            # TeslaMate's charging for the year (only its part: what was entered by hand is already in the costs)
            charging = sources.charging_cost(conn, teslamate(conn), car_id, datetime(year, 4, 1, tzinfo=zone),
                                             datetime(year + 1, 4, 1, tzinfo=zone), include_manual=False)
        report = lease.fbt_report(leases, costs, charging, year, today)
        earlier, later = [y for y in years if y < year], [y for y in years if y > year]
        return render_template("fbt.html", car=car, report=report, today=today, frequencies={k: v[0] for k, v in lease.FREQUENCIES.items()},
                               previous=earlier[-1] if earlier else None, following=later[0] if later else None,
                               label=lease.fbt_label)
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
        return dict(cars=db.cars(conn), categories=db.categories(conn), lead_days=reminders.DEFAULT_LEAD_DAYS,
                    lead_km=reminders.DEFAULT_LEAD_KM)

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
        return validation.parse_reminder_form(request.form, car_ids, category_ids, car_id)

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
        return render_template("expense_reminders.html", expense=expense, open_ones=open_ones, errors=errors or {},
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

    @app.post("/categories/<int:category_id>/pool")
    def category_pool(category_id):
        pool = (request.form.get("budget_pool") or "").strip() or None
        if pool is not None and pool not in lease.POOLS:
            abort(400)
        with connect() as conn:
            if not db.set_budget_pool(conn, category_id, pool):
                abort(404)
        return redirect(url_for("categories", msg="category_pool"), 303)
    # ---- change history ---------------------------------------------------------

    # Field -> label, in the order they are shown. id and the created/updated stamps are never shown.
    history_fields = {
        "expense": (("car_id", "Car"), ("category_id", "Category"), ("occurred_at", "Date and time"),
                    ("amount", "Amount"), ("odometer_km", "Odometer"), ("vendor", "Vendor"), ("notes", "Notes"),
                    ("invoice_url", "Invoice link"), ("invoice_path", "Invoice file")),
        "category": (("name", "Name"), ("kind", "Kind"), ("sort_order", "Sort order"),
                     ("upload_folder", "Invoice subfolder")),
    }

    def history_entries(entries, car_names, category_names):
        def show(field, value):
            if value is None or value == "":
                return "—"
            if field == "car_id":
                return car_names.get(value, f"car {value}")
            if field == "category_id":
                return category_names.get(value, f"category {value}")
            if field == "amount":
                return money(Decimal(str(value)))
            if field == "odometer_km":
                return km(value)
            if field == "occurred_at":
                try:
                    return when(datetime.fromisoformat(value))
                except (TypeError, ValueError):
                    return str(value)
            return str(value)

        out = []
        for e in entries:
            before, after = e["before_row"] or {}, e["after_row"] or {}
            row = after or before
            fields = history_fields[e["table_name"]]
            if e["operation"] == "update":
                changed = [(label, show(f, before.get(f)), show(f, after.get(f)))
                           for f, label in fields if before.get(f) != after.get(f)]
            else:
                changed = [(label, None, show(f, row.get(f))) for f, label in fields if row.get(f) not in (None, "")]
            if e["table_name"] == "expense":
                title = "Expense: " + " · ".join(
                    x for x in (category_names.get(row.get("category_id")), car_names.get(row.get("car_id")),
                                row.get("vendor")) if x)
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

    @app.post("/categories/<int:category_id>/folder")
    def category_folder(category_id):
        try:
            folder = validation.parse_upload_folder(request.form.get("upload_folder"))
        except ValueError as exc:
            with connect() as conn:
                cats = db.categories(conn)
            return render_template("categories.html", categories=cats, error=None, pools=lease.POOLS,
                                   folder_error=(category_id, str(exc)), message=None), 400
        with connect() as conn:
            if not db.set_upload_folder(conn, category_id, folder):
                abort(404)
        return redirect(url_for("categories", msg="category_folder"), 303)

    # ---- OneDrive ---------------------------------------------------------------

    STATE_COOKIE = "onedrive_sign_in"
    SIGN_IN_MINUTES = 10

    def need_drive():
        if drive is None:
            abort(404)

    def callback_url():
        return url_for("onedrive_callback", _external=True)

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
            "connected": ready["onedrive"], "callback_url": callback_url() if drive is not None else None,
            "errors": errors or {}, "values": request.form if errors else {}, "notice": notice,
            "message": MESSAGES.get(request.args.get("msg", "")), "name_example": name_example(settings_row),
            "secret_days_left": (expires - date.today()).days if expires else None,
            "available": ready["local"], "root": str(local.root) if local else None,
            "local_shown": local_shown if local else None,
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
                                     account=None, auth_state=None, auth_expires_at=None, **fields)
        return redirect(url_for("storage_page", msg="credentials"), 303)

    @app.post("/onedrive/connect")
    def onedrive_connect():
        need_drive()
        with connect() as conn:
            account = db.onedrive_account(conn)
        if not account or not account["client_id"]:
            return redirect(url_for("storage_page"), 303)
        state, verifier, challenge = onedrive.new_pkce()
        with connect() as conn:
            db.save_onedrive_account(
                conn, auth_state=drive.encrypt(f"{state} {verifier}"),
                auth_expires_at=datetime.now(timezone.utc) + timedelta(minutes=SIGN_IN_MINUTES))
        # Not a redirect: Chrome applies form-action to a form POST's redirect.
        # A link (followed by app.js) is a plain navigation, as n8n does it.
        resp = make_response(render_template(
            "connecting.html",
            sign_in_url=drive.authorize_url(account["client_id"], callback_url(), state, challenge)))
        # Ties the sign-in to this browser: the callback must come back to the one that started it.
        resp.set_cookie(STATE_COOKIE, state, max_age=SIGN_IN_MINUTES * 60, httponly=True,
                        samesite="Lax", secure=request.is_secure, path=request.script_root or "/")
        return resp

    @app.get("/onedrive/callback")
    def onedrive_callback():
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
        if request.args.get("error"):
            log.warning("Microsoft didn't connect OneDrive: %s", docinfo.quote(request.args["error"]))
            return render_storage(status=400, notice="Microsoft didn't connect OneDrive: "
                                   + (request.args.get("error_description") or request.args["error"]))
        try:
            tokens = drive.redeem_code(account["client_id"], drive.decrypt(account["client_secret"]),
                                       request.args.get("code", ""), callback_url(), pending[1])
        except onedrive.OneDriveError as exc:
            log.warning("OneDrive sign-in failed: %s", exc)
            return render_storage(status=400, notice=exc.user_message)
        name = drive.account_name(tokens["access_token"]) or "your Microsoft account"
        with connect() as conn:
            db.save_onedrive_account(conn, refresh_token=drive.encrypt(tokens["refresh_token"]), account=name)
        log.info("OneDrive connected")
        resp = redirect(url_for("storage_page", msg="connected"), 303)
        resp.delete_cookie(STATE_COOKIE, path=request.script_root or "/")
        return resp

    @app.post("/onedrive/disconnect")
    def onedrive_disconnect():
        need_drive()
        with connect() as conn:
            db.save_onedrive_account(conn, refresh_token=None, account=None)
        log.info("OneDrive disconnected")
        return redirect(url_for("storage_page", msg="disconnected"), 303)

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

    @app.get("/local/file")
    def local_file():
        """A PDF in the folder that has no expense yet (the import page links to these)."""
        return send_invoice(request.args.get("path", ""))

    # ---- what both storages do: a folder per car, importing, the file picker -------

    def render_browse(store, car, folder_id, error=None, status=200):
        kind = store.kind
        try:
            here = store.folder(folder_id)
            children = store.subfolders(folder_id)
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
                "note": "", "id": "", "guess": {}, "new_name": "", "rename": False, "odo_note": "", "duplicates": [], **fields}

    def import_page(store, car, **ctx):
        """The import page's template with what every version of it needs. A row's `url` is the file's
        reference (what the expense will store); `link` turns it into an address to open."""
        return render_template("import.html", car=car, kind=store.kind, info=STORES[store.kind], link=store.link,
                               learn_after=invoices.LEARN_AFTER, **ctx)

    def render_import(store, car, rows, remaining=0, errors=None, status=200, notice=None, skipped=None):
        with connect() as conn:
            categories = db.categories(conn)
            if skipped is None:
                skipped = len(db.skipped_import_urls(conn, car["id"]))
        return import_page(store, car, rows=rows, remaining=remaining, errors=errors or {}, categories=categories,
                           notice=notice, skipped=skipped), status

    def import_context(car_id, kind):
        """What reading a file needs: the folder, category names, learned labels and settings."""
        with connect() as conn:
            return {"folder": car_folder(conn, car_id, kind), "categories": db.categories(conn),
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

    def read_file(store, car, ctx, n, f):
        """Read one PDF and propose its expense: an import row. A file that can't be fetched
        becomes a row saying why, never an error for the whole page."""
        by_name = {c["name"]: c["id"] for c in ctx["categories"]}
        row = import_row(n, url=f["url"], name=f["name"], folder=f["folder"], id=f["id"])
        timer = docinfo.Timer()
        try:
            pdf = store.download(f["id"])
            guess = invoices.propose(f["folder"], f["name"], invoices.extract_text(pdf), list(by_name), ctx["learned"],
                                     ctx["settings"]["multi_amount"])
        except storage.StorageError as exc:
            log.warning("import file=%s folder=%s skipped: %s", docinfo.quote(f["name"]), docinfo.quote(f["folder"]), exc)
            row["note"] = exc.user_message
            return row
        have, missing = docinfo.found(guess)
        # The bytes actually read, not the size the page sent back: that is only what the listing said.
        log.debug("import file=%s folder=%s bytes=%d sha256=%s found=%s missing=%s ms=%d", docinfo.quote(f["name"]),
                  docinfo.quote(f["folder"]), len(pdf), docinfo.sha12(pdf), have, missing, timer.ms)
        row.update(
            date=guess["date"].isoformat() if guess["date"] else "",
            category_id=str(by_name.get(guess["category"], "")),
            vendor=guess["vendor"] or "", odometer_km=guess["odometer_km"] or "",
            amount=f"{guess['amount']:.2f}" if guess["amount"] is not None else "",
            include=not guess["missing"],
            note=("Couldn't read the " + ", ".join(guess["missing"]) + ": fill in to import."
                  if guess["missing"] else ""))
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
        if guess["date"] and guess["category"]:
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
                    text = invoices.extract_text(store.download(row["id"]))
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
        the invoices already in it, and the Add form's file picker. The same code serves both."""
        info = STORES[kind]
        page = "storage_page"

        def need_store():
            store = store_for(kind)
            if store is None:
                abort(404)
            return store

        @app.get(f"/{kind}/browse/<int:car_id>", endpoint=f"{kind}_browse")
        def browse(car_id):
            store = need_store()
            return render_browse(store, car_or_404(car_id), request.args.get("folder") or None)

        @app.post(f"/{kind}/browse/<int:car_id>/new-folder", endpoint=f"{kind}_new_folder")
        def new_folder(car_id):
            store = need_store()
            car = car_or_404(car_id)
            parent_id = request.form.get("parent_id") or None
            try:
                name = validation.parse_folder_name(request.form.get("name"))
                created = store.create_folder(parent_id, name)
            except onedrive.NotConnected:
                return redirect(url_for(page), 303)
            except (ValueError, storage.StorageError) as exc:
                said = exc.user_message if isinstance(exc, storage.StorageError) else str(exc)   # or the name's ValueError
                return render_browse(store, car, parent_id, error=said, status=400)
            return redirect(url_for(f"{kind}_browse", car_id=car_id, folder=created["id"]), 303)

        @app.post(f"/{kind}/folders/<int:car_id>", endpoint=f"{kind}_choose_folder")
        def choose_folder(car_id):
            store = need_store()
            car_or_404(car_id)
            try:
                chosen = store.folder(request.form.get("folder_id") or None)
                if kind == "local":
                    local.claim()          # from now on, the folder must carry its marker (storage.py)
            except storage.StorageError as exc:
                log.warning("%s folder choice failed: %s", kind, exc)
                return render_storage(status=400, notice=f"Couldn't use that folder: {exc}")
            with connect() as conn:
                db.set_car_folder(conn, car_id, chosen["id"], chosen["path"], kind)
            return redirect(url_for(page, msg="folders"), 303)

        @app.post(f"/{kind}/folders/<int:car_id>/clear", endpoint=f"{kind}_clear_folder")
        def clear_folder(car_id):
            need_store()
            car_or_404(car_id)
            with connect() as conn:
                db.clear_car_folder(conn, car_id, kind)
            return redirect(url_for(page, msg="folders"), 303)

        # ---- import the invoices already in the car's folder ----------------------

        @app.get(f"/{kind}/import/<int:car_id>", endpoint=f"{kind}_import")
        def import_start(car_id):
            """The page comes up at once, with a progress bar: its script then asks for the file list
            (/scan) and for each file in turn (/read), so nothing waits on the storage or OCR. With
            ?sync=1 (no JavaScript) everything is read before the page is sent."""
            store = need_store()
            car = car_or_404(car_id)
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
                                 notice=None if files else "No PDFs found in that folder.")

        @app.get(f"/{kind}/import/<int:car_id>/scan", endpoint=f"{kind}_import_scan")
        def import_scan(car_id):
            """The files still to import, listed but not read: {listed, already_imported, remaining, files}."""
            store = need_store()
            car_or_404(car_id)
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
            car = car_or_404(car_id)
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
                                   kind=kind, link=store.link)

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
            car = car_or_404(car_id)
            ref_field = store.ref_field
            with connect() as conn:
                car_ids = {c["id"] for c in db.cars(conn)}
                cat_rows = db.categories(conn)
                cat_ids = {c["id"] for c in cat_rows}
                settings_row = db.get_settings(conn)
            cat_names = {c["id"]: c["name"] for c in cat_rows}
            count = min(request.form.get("count", type=int) or 0, 200)
            rows, chosen, errors, skip_urls = [], [], {}, []
            for n in range(count):
                def f(key, n=n):
                    return request.form.get(f"{key}_{n}", "")

                row = import_row(n, include=bool(f("include")), skip=bool(f("skip")), rename=bool(f("rename")), date=f("date"), category_id=f("category_id"),
                                 vendor=f("vendor"), amount=f("amount"), odometer_km=f("odometer_km"),
                                 url=f("url"), name=f("name"), folder=f("folder"), id=f("id"),
                                 guess={k: f(f"guess_{k}") for k in LEARN_FIELDS})
                rows.append(row)
                if row["skip"] and row["url"]:
                    skip_urls.append(row["url"])
                    continue
                if not row["include"]:
                    continue
                values, row_errors = validation.parse_expense_form({
                    "car_id": car_id, "category_id": row["category_id"], "date": row["date"], "time": "12:00",
                    "amount": row["amount"], "odometer_km": row["odometer_km"], "vendor": row["vendor"],
                    ref_field: row["url"]}, tz, car_ids, cat_ids)
                if not row["url"]:
                    row_errors[ref_field] = "Missing link to the PDF."
                if row_errors:
                    errors[n] = " ".join(row_errors.values())
                else:
                    chosen.append((row, values))
            if errors:
                return render_import(store, car, rows, errors=errors, status=400)
            if skip_urls:
                with connect() as conn:
                    db.skip_imports(conn, car_id, skip_urls)
            if not chosen:
                if skip_urls:
                    return redirect(url_for(f"{kind}_import", car_id=car_id), 303)
                return render_import(store, car, rows, status=400, notice="Tick at least one invoice to import.")
            imported, renamed, rename_failed = [], 0, 0
            with connect() as conn:
                known = db.known_invoices(conn, [v[ref_field] for _, v in chosen])
            for row, values in chosen:
                if values[ref_field] in known:   # a double submit imports once
                    continue
                target = validation.invoice_filename(
                    values["occurred_at"].astimezone(ZoneInfo(tz)), cat_names[values["category_id"]], values["vendor"],
                    settings_row["invoice_date_format"], settings_row["invoice_name_format"],
                    car=car["name"], amount=values["amount"])
                if row["rename"] and row["id"] and target != row["name"]:
                    try:
                        values[ref_field] = store.rename(row["id"], target)
                        if kind == "local":
                            row["id"] = values[ref_field]    # there the reference is where the file is
                        renamed += 1
                    except storage.StorageError as exc:
                        rename_failed += 1           # import it anyway, still pointing at the old name
                        log.warning("import rename item=%s to=%s failed: %s", row["id"], docinfo.quote(target), exc)
                with connect() as conn:
                    db.insert_expense(conn, values)
                imported.append((row, values))
            log.debug("import confirm car=%d rows=%d imported=%d skipped_known=%d renamed=%d rename_failed=%d", car_id,
                      len(chosen), len(imported), len(chosen) - len(imported), renamed, rename_failed)
            refresh_dashboard()
            learn_from(store, imported)
            more = {"store": kind} if kind == "local" else {}
            return redirect(url_for("index", msg="imported", n=len(imported), renamed=renamed,
                                    rename_failed=rename_failed, **more), 303)

        @app.get(f"/api/{kind}/browse/<int:car_id>", endpoint=f"api_{kind}_browse")
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
                here, folders, files = store.contents(folder_id)
            except onedrive.SignInExpired as exc:
                return jsonify(error=exc.user_message, url=url_for(page)), 409
            except storage.StorageError as exc:
                log.warning("%s file picker failed: %s", kind, exc)
                return jsonify(error=exc.user_message), 502
            return jsonify(id=here["id"], path=here["path"], parent_id=here["parent_id"], is_root=here["is_root"],
                           folders=folders, files=files)

    for kind in STORES:
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
                                     values["multi_amount"])
                log.info("settings saved")
                return redirect(url_for("settings", msg="settings"), 303)
        else:
            with connect() as conn:
                values = db.get_settings(conn)
        return render_template("settings.html", values=values, errors=errors, aliases=validation.NAME_ALIASES,
                               date_default=validation.DEFAULT_DATE_PATTERN, name_default=validation.DEFAULT_NAME_FORMAT,
                               multi_choices=validation.MULTI_AMOUNT_CHOICES, example=name_example(values),
                               message=MESSAGES.get(request.args.get("msg", ""))), (400 if errors else 200)

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
        return jsonify(
            date=guess["date"].isoformat() if guess["date"] else None,
            category_id=by_name.get(guess["category"]), vendor=guess["vendor"],
            amount=f"{guess['amount']:.2f}" if guess["amount"] is not None else None,
            odometer_km=guess["odometer_km"])

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
