"""Property-based tests (Hypothesis) for every route that takes input: whatever is sent, the app
answers (a redirect, a re-rendered form, a 4xx), never a 500, and nothing invalid reaches the database.

app.testing is on, so an exception inside a view is raised here instead of becoming a 500 page:
either way the example fails, and Hypothesis shrinks it to the smallest request that does it.

One app serves the whole module, with both invoice stores on (a folder on disk, and OneDrive through a
stand-in for Microsoft), car 1 signed in and pointed at a folder in each. `world.prepare()` returns a
client on a clean slate; each property calls it once, and `world.reset()` between examples that save.
"""

import io
from decimal import Decimal

import psycopg
import pytest
from hypothesis import given, settings, strategies as st
from psycopg.rows import dict_row

from garagebooks import create_app, validation as v
from tests import test_onedrive as od
from tests.test_app import ORIGIN
from tests.test_validation import PDF

# Any answer but a crash. The app deliberately answers 502 when a storage (OneDrive, the folder) fails.
OK = set(range(200, 500)) | {502}
EXAMPLES = settings(max_examples=40)        # each example is a real request on a real database


# ---- the world under test ---------------------------------------------------------------------

class TolerantMicrosoft(od.FakeMicrosoft):
    """The stand-in for Microsoft, answering 404 where the fixed fake would trip on an id it never
    made up: a fuzzed request must reach the app's own handling, not the test double's assertions."""

    def __call__(self, method, url, data=None, headers=None, raw=False):
        try:
            return super().__call__(method, url, data, headers, raw)
        except (AssertionError, KeyError, IndexError, ValueError):
            return 404, {"error": {"code": "itemNotFound"}}


class World:
    def __init__(self, pg, app, ms, root):
        self.pg, self.app, self.ms, self.root = pg, app, ms, root
        self.db = pg.admin()
        self.client = None
        self.folders = self.db.execute("SELECT id, upload_folder FROM garagebooks.category").fetchall()

    def reset(self):
        """The tables the properties write to, empty (as the admin fixture leaves them)."""
        self.db.execute("TRUNCATE garagebooks.expense, garagebooks.learned_label, garagebooks.learned_vendor,"
                        " garagebooks.import_skip RESTART IDENTITY CASCADE")
        self.db.execute("DELETE FROM garagebooks.category WHERE sort_order = 100")
        for row in self.folders:        # the standard categories' upload folders, which a property may have changed
            self.db.execute("UPDATE garagebooks.category SET upload_folder = %s WHERE id = %s", (row["upload_folder"], row["id"]))
        self.db.execute("UPDATE garagebooks.setting SET app_url = NULL, grafana_url = NULL, invoice_date_format = NULL,"
                        " invoice_name_format = NULL, multi_amount = NULL, marginal_tax_rate = NULL, notify_url = NULL,"
                        " notify_format = NULL")
        self.db.execute("DELETE FROM garagebooks.ev_charging_rate WHERE fbt_year NOT BETWEEN 2022 AND 2026")
        self.db.execute("UPDATE garagebooks.ev_charging_rate SET cents_per_km = CASE WHEN fbt_year = 2026 THEN 5.47 ELSE 4.20 END")

    def prepare(self):
        """A clean database, a signed-in OneDrive and a chosen folder for car 1 in each store."""
        self.db.execute("TRUNCATE garagebooks.car_folder, garagebooks.onedrive_account")
        self.reset()
        self.client = self.app.test_client()
        od.sign_in(self.client, self.ms)
        assert od.choose_folder(self.client).status_code == 303
        assert self.client.post("/local/folders/1", data={"folder_id": "/Cars/Test car"}, headers=ORIGIN).status_code == 303
        self.seed_files()
        return self.client

    def seed_files(self):
        """PDFs in the car's folder, and a canary outside the invoice folder that no request may ever reach."""
        folder = self.root / "Cars" / "Test car"
        for name in ("a.pdf", "b.pdf"):
            if not (folder / name).exists():
                (folder / name).write_bytes(PDF)
        outside = self.root.parent / "canary.pdf"
        outside.write_bytes(b"%PDF-1.7 CANARY")
        link = folder / "link.pdf"
        if not link.is_symlink():
            link.symlink_to(outside)

    def expenses(self):
        return self.db.execute("SELECT * FROM garagebooks.expense ORDER BY id").fetchall()

    def seed_expense(self, **over):
        row = {"car_id": 1, "category_id": self.category("Tyres"), "occurred_at": "2026-11-07 04:05+00", "amount": "10.00",
               "odometer_km": None, "vendor": "Shop", "notes": None, "invoice_url": None, "invoice_path": None}
        row.update(over)
        return self.db.execute(
            "INSERT INTO garagebooks.expense (car_id, category_id, occurred_at, amount, odometer_km, vendor, notes,"
            " invoice_url, invoice_path) VALUES (%(car_id)s, %(category_id)s, %(occurred_at)s, %(amount)s,"
            " %(odometer_km)s, %(vendor)s, %(notes)s, %(invoice_url)s, %(invoice_path)s) RETURNING id", row).fetchone()["id"]

    def category(self, name):
        return self.db.execute("SELECT id FROM garagebooks.category WHERE name = %s", (name,)).fetchone()["id"]


@pytest.fixture(scope="module")
def world(pg, tmp_path_factory):
    root = tmp_path_factory.mktemp("props") / "invoices"
    (root / "Cars" / "Test car").mkdir(parents=True)
    ms = TolerantMicrosoft()
    folders = ({k: dict(val) for k, val in od.FOLDERS.items()}, {k: list(val) for k, val in od.CHILDREN.items()})
    with pytest.MonkeyPatch.context() as mp:
        mp.delenv("BASE_PATH", raising=False)
        mp.setenv("APP_TZ", "Australia/Brisbane")
        mp.setenv("INVOICES_DIR", str(root))
        mp.setenv("GARAGEBOOKS_ENCRYPTION_KEY", od.KEY)
        app = create_app(connect=lambda: psycopg.connect(pg.app_url, row_factory=dict_row), onedrive_transport=ms,
                         notify_send=lambda *args, **kw: None)       # no request ever leaves a test
        app.testing = True
        w = World(pg, app, ms, root)
        try:
            yield w
        finally:
            w.reset()           # leave the shared tables as the other modules expect them
            w.db.execute("TRUNCATE garagebooks.car_folder, garagebooks.onedrive_account")
            w.db.close()
            od.FOLDERS.clear(); od.FOLDERS.update(folders[0])      # undo folders the properties created in OneDrive
            od.CHILDREN.clear(); od.CHILDREN.update(folders[1])


# ---- what gets sent ---------------------------------------------------------------------------

junk = st.text(max_size=60)
big_int = st.integers(-2**70, 2**70).map(str)
text = st.one_of(junk, junk, big_int, st.sampled_from(["", " ", "0", "-1", "\x00", "../..", "/", "%", "{{7*7}}", "<script>", " "]))
ids = st.integers(0, 10**25)                  # what a <int:...> path part can hold
amount = st.one_of(text, st.decimals(min_value=-10**11, max_value=10**11, places=2).map(lambda d: f"{d:,.2f}"))
day = st.one_of(text, st.dates().map(str))
clock = st.one_of(text, st.times().map(lambda t: t.strftime("%H:%M")))


def stored_row_is_sound(row):
    """What the database may hold, whatever was typed: the limits the form promises."""
    assert abs(row["amount"]) <= v.MAX_AMOUNT
    assert row["odometer_km"] is None or 0 <= row["odometer_km"] <= v.MAX_ODOMETER
    assert row["invoice_url"] is None or row["invoice_url"].startswith("https://")
    assert row["invoice_path"] is None or row["invoice_path"].lower().endswith(".pdf")
    assert len(row["vendor"] or "") <= v.MAX_TEXT_LEN and len(row["notes"] or "") <= v.MAX_NOTES_LEN
    assert row["car_id"] == 1


def expense_form(w):
    return st.fixed_dictionaries({}, optional={
        "car_id": st.one_of(st.just("1"), text), "category_id": st.one_of(st.just(str(w.category("Tyres"))), text),
        "date": day, "time": clock, "amount": amount,
        "odometer_km": st.one_of(text, st.integers(0, 10**7).map(str)),
        "invoice_url": st.one_of(text, st.just("https://example.com/a")),
        "invoice_path": st.one_of(text, st.just("/Cars/Test car/a.pdf")),
        "vendor": text, "notes": text, "confirm_duplicate": st.sampled_from(["", "1"]),
        "remove_invoice": st.sampled_from(["", "1"]), "packaged": st.one_of(text, st.sampled_from(["auto", "package", "personal"])), "category": text, "sort": text, "dir": text})


pdf_upload = st.one_of(
    st.none(),
    st.binary(max_size=300).map(lambda b: (b, "x.bin")),
    st.binary(max_size=300).map(lambda b: (b"%PDF-" + b, "x.pdf")),
    st.tuples(st.just(b""), junk.filter(lambda s: "\x00" not in s)))


def with_file(data, upload):
    if upload is not None:
        content, name = upload
        data = {**data, "invoice_file": (io.BytesIO(content), name or "x.pdf")}
    return data


# ---- the expense form -------------------------------------------------------------------------

@EXAMPLES
@given(st.data(), pdf_upload)
def test_new_expense_saves_only_what_validates(world, data, upload):
    client = world.prepare() if world.client is None else world.client
    world.reset()
    form = data.draw(expense_form(world))
    resp = client.post("/new", data=with_file(form, upload), headers=ORIGIN)
    assert resp.status_code in OK
    saved = world.expenses()
    if resp.status_code == 303:
        assert len(saved) == 1
    else:
        assert saved == []
    for row in saved:
        stored_row_is_sound(row)


@EXAMPLES
@given(st.data(), pdf_upload)
def test_edit_expense_keeps_only_what_validates(world, data, upload):
    client = world.prepare() if world.client is None else world.client
    world.reset()
    expense_id = world.seed_expense(vendor="Before")
    form = data.draw(expense_form(world))
    resp = client.post(f"/{expense_id}/edit", data=with_file(form, upload), headers=ORIGIN)
    assert resp.status_code in OK
    (row,) = world.expenses()
    stored_row_is_sound(row)
    if resp.status_code != 303:
        assert row["vendor"] == "Before" and row["amount"] == Decimal("10.00")


@EXAMPLES
@given(ids, st.sampled_from(["edit", "delete", "view"]), st.data())
def test_expense_ids_of_any_size_are_a_404_or_work(world, expense_id, action, data):
    client = world.prepare() if world.client is None else world.client
    world.reset()
    world.seed_expense()
    path = f"/{expense_id}/edit" if action != "view" else f"/invoice/{expense_id}"
    resp = client.post(path.replace("edit", "delete"), headers=ORIGIN) if action == "delete" else client.get(path)
    assert resp.status_code in OK


@EXAMPLES
@given(st.dictionaries(st.sampled_from(["category", "sort", "dir", "msg", "n", "renamed", "rename_failed", "store", "x"]), text))
def test_the_list_takes_any_query_string(world, query):
    client = world.prepare() if world.client is None else world.client
    world.reset()
    world.seed_expense()
    assert client.get("/", query_string=query).status_code in OK
    assert client.get("/new", query_string=query).status_code in OK
    assert client.get("/1/edit", query_string=query).status_code in OK


# ---- categories, learned labels, settings ------------------------------------------------------

@EXAMPLES
@given(text)
def test_adding_a_category_saves_only_a_tidy_name(world, name):
    client = world.prepare() if world.client is None else world.client
    world.reset()
    before = world.db.execute("SELECT count(*) AS n FROM garagebooks.category").fetchone()["n"]
    resp = client.post("/categories", data={"name": name}, headers=ORIGIN)
    assert resp.status_code in OK
    names = [r["name"] for r in world.db.execute("SELECT name FROM garagebooks.category WHERE sort_order = 100")]
    assert len(names) <= 1
    if resp.status_code == 303:
        assert len(names) == 1 and names[0] == " ".join(names[0].split()) and 0 < len(names[0]) <= v.MAX_CATEGORY_LEN
    assert before + len(names) == world.db.execute("SELECT count(*) AS n FROM garagebooks.category").fetchone()["n"]


@EXAMPLES
@given(st.one_of(ids, st.just(1)), text)
def test_a_category_upload_folder_saves_only_a_legal_name(world, category_id, folder):
    client = world.prepare() if world.client is None else world.client
    world.reset()
    category_id = world.category("Tyres") if category_id == 1 else category_id
    resp = client.post(f"/categories/{category_id}/folder", data={"upload_folder": folder}, headers=ORIGIN)
    assert resp.status_code in OK
    saved = world.db.execute("SELECT upload_folder FROM garagebooks.category WHERE name = 'Tyres'").fetchone()["upload_folder"]
    if saved:
        assert v.parse_folder_name(saved) == saved


@EXAMPLES
@given(st.one_of(ids, st.just(1)), text)
def test_a_category_gst_free_tick_is_a_yes_or_no_and_never_a_crash(world, category_id, tick):
    client = world.prepare() if world.client is None else world.client
    world.reset()
    category_id = world.category("Tyres") if category_id == 1 else category_id
    assert client.post(f"/categories/{category_id}/gst_free", data={"gst_free": tick}, headers=ORIGIN).status_code in OK
    assert world.db.execute("SELECT gst_free FROM garagebooks.category WHERE name = 'Tyres'").fetchone()["gst_free"] in (True, False)
    world.db.execute("UPDATE garagebooks.category SET gst_free = false WHERE name = 'Tyres'")


@EXAMPLES
@given(st.sampled_from(["/learned/label/delete", "/learned/vendor/delete"]), st.dictionaries(st.sampled_from(["field", "label", "vendor"]), text))
def test_forgetting_a_learned_label_or_vendor(world, path, data):
    client = world.prepare() if world.client is None else world.client
    assert client.post(path, data=data, headers=ORIGIN).status_code in OK
    assert client.get("/learned").status_code in OK


settings_form = st.fixed_dictionaries({}, optional={
    "app_url": st.one_of(text, st.just("https://example.com/app")), "grafana_url": st.one_of(text, st.just("http://grafana:3000")),
    "invoice_date_format": st.one_of(text, st.sampled_from(["yymmdd", "yyyy-mm-dd", "dd.mm.yy"])),
    "invoice_name_format": st.one_of(text, st.sampled_from(["%date %category - %vendor", "%date_%car", "%date %vendor %%"])),
    "multi_amount": st.one_of(text, st.sampled_from(sorted(v.MULTI_AMOUNT_CHOICES))),
    "marginal_tax_rate": st.one_of(text, st.decimals(min_value=-5, max_value=105, places=2).map(str)),
    "ev_rate_2026": text, "new_rate_year": st.one_of(text, st.integers(1980, 2210).map(str)), "new_rate_cents": text})


@EXAMPLES
@given(settings_form)
def test_settings_save_only_what_validates(world, form):
    client = world.prepare() if world.client is None else world.client
    world.reset()
    resp = client.post("/settings", data=form, headers=ORIGIN)
    assert resp.status_code in OK
    row = world.db.execute("SELECT * FROM garagebooks.setting").fetchone()
    if resp.status_code == 303:
        assert v.parse_url_setting(row["app_url"]) == row["app_url"]
        assert v.parse_url_setting(row["grafana_url"]) == row["grafana_url"]
        assert v.parse_date_pattern(row["invoice_date_format"]) == row["invoice_date_format"]
        assert v.parse_name_format(row["invoice_name_format"]) == row["invoice_name_format"]
        assert row["multi_amount"] in v.MULTI_AMOUNT_CHOICES
        assert row["marginal_tax_rate"] is None or 0 <= row["marginal_tax_rate"] <= 100
        for rate in world.db.execute("SELECT * FROM garagebooks.ev_charging_rate"):
            assert 1990 <= rate["fbt_year"] <= 2200 and 0 <= rate["cents_per_km"] <= v.MAX_CENTS_PER_KM
    else:
        assert row["app_url"] is None and row["invoice_name_format"] is None and row["marginal_tax_rate"] is None
    assert client.get("/settings").status_code == 200       # whatever is saved, the page still draws


notify_form = st.fixed_dictionaries({}, optional={
    "notify_url": st.one_of(text, st.just("https://ntfy.example.invalid/zq7-secret"), st.just("http://127.0.0.1:8080/t"),
                            st.just("ftp://x.example.invalid/t")),
    "notify_format": st.one_of(text, st.sampled_from(["ntfy", "json", "NTFY", ""])),
    "notify_clear": st.sampled_from(["", "1"])})


@EXAMPLES
@given(notify_form)
def test_notification_settings_save_only_what_validates(world, form):
    client = world.prepare() if world.client is None else world.client
    world.reset()
    resp = client.post("/settings/notifications", data=form, headers=ORIGIN)
    assert resp.status_code in OK
    row = world.db.execute("SELECT notify_url, notify_format FROM garagebooks.setting").fetchone()
    if resp.status_code == 303:
        assert v.parse_notify_url(row["notify_url"]) == row["notify_url"]
        assert row["notify_format"] in v.NOTIFY_FORMATS
        assert (row["notify_url"] is None) == (not form.get("notify_url", "").strip() or bool(form.get("notify_clear")))
    else:
        assert row["notify_url"] is None and row["notify_format"] is None
    html = client.get("/settings").data.decode()
    assert "zq7-secret" not in html and "http://127.0.0.1:8080/t" not in html   # never echoed back


@EXAMPLES
@given(st.sampled_from(["https://ntfy.example.invalid/zq7-secret", "http://[::1]:1/zq7-secret", ""]))
def test_the_test_notice_button_answers_whatever_is_saved(world, address):
    client = world.prepare() if world.client is None else world.client
    world.reset()
    world.db.execute("UPDATE garagebooks.setting SET notify_url = %s", (address or None,))
    resp = client.post("/settings/notifications/test", headers=ORIGIN)   # the app was made with a stand-in sender: no network
    assert resp.status_code == 200 and "zq7-secret" not in resp.data.decode()


# ---- the JSON endpoints ------------------------------------------------------------------------

@EXAMPLES
@given(st.sampled_from(["/api/duplicates", "/api/odometer"]),
       st.dictionaries(st.sampled_from(["car_id", "date", "time", "amount"]), st.one_of(text, ids.map(str), day)))
def test_json_lookups_answer_json(world, path, query):
    client = world.prepare() if world.client is None else world.client
    resp = client.get(path, query_string=query)
    assert resp.status_code in OK and resp.is_json


@EXAMPLES
@given(st.one_of(st.none(), st.binary(max_size=400).map(lambda b: (b, "x.bin")), st.binary(max_size=400).map(lambda b: (b"%PDF-" + b, "x.pdf"))),
       st.dictionaries(st.sampled_from(["onedrive_id", "local_id", "name"]), text))
def test_prefill_answers_json_for_any_file(world, upload, fields):
    client = world.prepare() if world.client is None else world.client
    resp = client.post("/api/prefill", data=with_file(fields, upload), headers=ORIGIN)
    assert resp.status_code in OK and resp.is_json


@EXAMPLES
@given(text)
def test_provider_icon_names(world, name):
    client = world.prepare() if world.client is None else world.client
    assert client.get("/provider-icon/" + name.replace("/", "_").replace("%", "_")).status_code in OK


# ---- serving invoices from the folder ----------------------------------------------------------

@EXAMPLES
@given(st.one_of(text, st.sampled_from(["/Cars/Test car/a.pdf", "/Cars/Test car/link.pdf", "/../canary.pdf", "/Cars/../../canary.pdf",
                                        "/Cars/Test car/../../../canary.pdf", "//Cars/Test car/a.pdf", "/Cars/Test car/a.pdf/"])))
def test_the_invoice_folder_never_serves_outside_itself(world, ref):
    client = world.prepare() if world.client is None else world.client
    resp = client.get("/local/file", query_string={"path": ref, "download": ref})
    assert resp.status_code in OK and b"CANARY" not in resp.data
    if "\x00" not in ref:
        world.reset()
        try:
            expense_id = world.seed_expense(invoice_path=ref)
        except psycopg.errors.CheckViolation:
            return                  # the database itself refuses this one as a path: nothing to serve
        resp = client.get(f"/invoice/{expense_id}")
        assert resp.status_code in OK and b"CANARY" not in resp.data


# ---- storage: choosing a folder, browsing, importing ------------------------------------------

kinds = st.sampled_from(["local", "onedrive"])
folder_ids = st.one_of(text, st.sampled_from(["/Cars", "/Cars/Test car", "/Cars/Test car/a.pdf", "ROOT", "CARS", "INV", "root", "F1", "NEW4"]))


@EXAMPLES
@given(kinds, ids, folder_ids, text)
def test_browsing_and_the_file_picker(world, kind, car_id, folder, name):
    client = world.prepare() if world.client is None else world.client
    for car in (1, car_id):
        assert client.get(f"/{kind}/browse/{car}", query_string={"folder": folder}).status_code in OK
        assert client.get(f"/api/{kind}/browse/{car}", query_string={"folder": folder}).status_code in OK
        assert client.post(f"/{kind}/browse/{car}/new-folder", data={"parent_id": folder, "name": name}, headers=ORIGIN).status_code in OK


@EXAMPLES
@given(kinds, st.one_of(st.just(1), ids), folder_ids)
def test_choosing_and_clearing_a_car_folder(world, kind, car_id, folder):
    client = world.prepare() if world.client is None else world.client
    assert client.post(f"/{kind}/folders/{car_id}", data={"folder_id": folder}, headers=ORIGIN).status_code in OK
    assert client.post(f"/{kind}/folders/{car_id}/clear", headers=ORIGIN).status_code in OK
    world.prepare()           # the folder was cleared (or changed): put it back for the next example


@EXAMPLES
@given(kinds, st.one_of(st.just(1), ids), st.sampled_from(["", "1"]))
def test_import_pages(world, kind, car_id, sync):
    client = world.prepare() if world.client is None else world.client
    assert client.get(f"/{kind}/import/{car_id}", query_string={"sync": sync}).status_code in OK
    assert client.get(f"/{kind}/import/{car_id}/scan").status_code in OK
    assert client.post(f"/{kind}/import/{car_id}/unskip", headers=ORIGIN).status_code in OK


@EXAMPLES
@given(kinds, st.dictionaries(st.sampled_from(["n", "id", "name", "folder", "url", "size"]),
                              st.one_of(text, st.sampled_from(["F1", "F2", "/Cars/Test car/a.pdf", "a.pdf", "0", "1"]))))
def test_reading_one_file_for_import(world, kind, fields):
    client = world.prepare() if world.client is None else world.client
    assert client.post(f"/{kind}/import/1/read", data=fields, headers=ORIGIN).status_code in OK


def import_rows(kind):
    ref = st.sampled_from(["F1", "/Cars/Test car/a.pdf", "/Cars/Test car/b.pdf"]) if kind == "local" else st.just("F1")
    row = st.fixed_dictionaries({}, optional={
        "include": st.sampled_from(["", "1"]), "skip": st.sampled_from(["", "1"]), "rename": st.sampled_from(["", "1"]),
        "date": day, "category_id": text, "vendor": text, "amount": amount, "odometer_km": text,
        "url": st.one_of(text, ref, st.just("https://onedrive.live.com/?id=F1")), "name": text, "folder": text,
        "id": st.one_of(text, ref), "guess_date": text, "guess_amount": text, "guess_odometer_km": text, "guess_vendor": text,
        "guess_category_id": text})
    return row


@EXAMPLES
@given(st.data(), kinds, st.one_of(text, st.integers(0, 4).map(str)))
def test_confirming_an_import_saves_only_what_validates(world, data, kind, count):
    client = world.prepare() if world.client is None else world.client
    world.reset()
    world.seed_files()
    form = {"count": count}
    for n in range(data.draw(st.integers(0, 3))):
        for key, value in data.draw(import_rows(kind)).items():
            form[f"{key}_{n}"] = value
    resp = client.post(f"/{kind}/import/1", data=form, headers=ORIGIN)
    assert resp.status_code in OK
    for row in world.expenses():
        stored_row_is_sound(row)


# ---- OneDrive sign-in ---------------------------------------------------------------------------

@EXAMPLES
@given(st.dictionaries(st.sampled_from(["client_id", "client_secret", "secret_expires"]),
                       st.one_of(text, st.just(od.CLIENT_ID), st.just(od.SECRET), st.dates().map(str))))
def test_onedrive_credentials_save_only_what_validates(world, form):
    client = world.prepare() if world.client is None else world.client
    resp = client.post("/onedrive/credentials", data=form, headers=ORIGIN)
    assert resp.status_code in OK
    account = world.db.execute("SELECT * FROM garagebooks.onedrive_account").fetchone()
    if resp.status_code != 303:
        assert account["client_id"] == od.CLIENT_ID          # a refused form changes nothing
    else:
        assert v.parse_client_id(account["client_id"]) == account["client_id"]
    assert client.get("/storage").status_code == 200
    world.prepare()


@EXAMPLES
@given(st.dictionaries(st.sampled_from(["state", "code", "error", "error_description"]),
                       st.one_of(text, st.just("AUTH-CODE"))), st.one_of(st.none(), text.filter(lambda s: s.isascii() and s.isprintable() and ";" not in s)))
def test_onedrive_callback_takes_any_query(world, query, cookie):
    client = world.prepare() if world.client is None else world.client
    if cookie is not None:
        client.set_cookie("onedrive_sign_in", cookie)
    assert client.get("/onedrive/callback", query_string=query).status_code in OK
    client.delete_cookie("onedrive_sign_in")
    world.prepare()


def test_onedrive_connect_and_disconnect(world):
    client = world.prepare()
    assert client.post("/onedrive/connect", headers=ORIGIN).status_code in OK
    assert client.post("/onedrive/disconnect", headers=ORIGIN).status_code in OK
