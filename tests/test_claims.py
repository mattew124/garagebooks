"""Claims (roadmap L3, the "claimed" part): sending an expense and its invoice to a claims connector,
showing what will be claimed, submitting on the owner's say-so and remembering the claim.

The connector is tests/fake_connector.py on 127.0.0.1, never a real provider. All values are invented.
Runs on either kind of database (the car is the suite's own test car)."""

import html as htmllib
import re
import runpy
from datetime import date
from decimal import Decimal

import psycopg
import pytest
from psycopg.rows import dict_row

from garagebooks import claims, create_app, db
from tests.fake_connector import TOKEN, Fake
from tests.test_onedrive import FILE_DOWNLOAD, KEY, FakeMicrosoft, an_hour_passes, sign_in
from tests.test_validation import PDF

ORIGIN = {"Origin": "http://localhost"}
# OneDrive's address for a file in a personal drive, as Graph gives it (webUrl): the item's id is in it.
ONEDRIVE_URL = "https://onedrive.live.com?cid=ABC&id=F1"
SHARE_URL = "https://1drv.ms/b/s!SHARED"           # an address with no id in it: Graph's shares lookup finds it


@pytest.fixture()
def fake():
    server = Fake()
    server.url = server.start()
    yield server
    server.stop()


def make_client(pg, monkeypatch, tmp_path, url, transport=None, **env):
    monkeypatch.delenv("BASE_PATH", raising=False)
    monkeypatch.setenv("APP_TZ", "Australia/Brisbane")
    for key in ("CLAIMS_CONNECTOR_URL", "CLAIMS_CONNECTOR_TOKEN", "CLAIMS_CONNECTOR_TOKEN_FILE",
                "CLAIMS_CONNECTOR_KEY_FILE", "INVOICES_DIR"):
        monkeypatch.delenv(key, raising=False)
    (tmp_path / "invoices" / "Tyres").mkdir(parents=True)
    (tmp_path / "invoices" / "Tyres" / "INV12345.pdf").write_bytes(PDF)
    monkeypatch.setenv("INVOICES_DIR", str(tmp_path / "invoices"))
    if url:
        monkeypatch.setenv("CLAIMS_CONNECTOR_URL", url)
        monkeypatch.setenv("CLAIMS_CONNECTOR_TOKEN", TOKEN)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    app = create_app(connect=lambda: psycopg.connect(pg.app_url, row_factory=dict_row), onedrive_transport=transport)
    app.testing = True
    return app.test_client()


@pytest.fixture()
def client(pg, admin, fake, monkeypatch, tmp_path):
    return make_client(pg, monkeypatch, tmp_path, fake.url)


def category_id(admin, name):
    return admin.execute("SELECT id FROM garagebooks.category WHERE name=%s", (name,)).fetchone()["id"]


def lease(admin, start="2026-01-01", end="2029-01-01", car=1):
    admin.execute("INSERT INTO garagebooks.lease (car_id, financier, start_date, end_date, term_months, pay_frequency)"
                  " VALUES (%s, 'Example Finance', %s, %s, 36, 'weekly')", (car, start, end))


def expense(admin, category="Tyres", amount="1200.00", at="2026-11-07 04:05+00", invoice_path="/Tyres/INV12345.pdf",
            invoice_url=None, vendor="Example Tyres Pty Ltd", car=1):
    return admin.execute(
        "INSERT INTO garagebooks.expense (car_id, category_id, occurred_at, amount, vendor, notes, invoice_path, invoice_url)"
        " VALUES (%s, %s, %s, %s, %s, 'Four tyres', %s, %s) RETURNING id",
        (car, category_id(admin, category), at, amount, vendor, invoice_path, invoice_url)).fetchone()["id"]


def claim_rows(admin):
    return admin.execute("SELECT * FROM garagebooks.expense_claim ORDER BY expense_id").fetchall()


@pytest.fixture()
def tyres(admin):
    """An eligible expense: tyres on a car with a lease, an invoice file, nothing claimed."""
    lease(admin)
    return expense(admin)


def said(resp):
    """The page's text, with its HTML escapes undone (an apostrophe is written &#39;)."""
    return htmllib.unescape(resp.data.decode())


def last(fake, path):
    return [c for c in fake.calls if c[1] == path][-1]


# ---- the feature is invisible until it is configured ------------------------------------------------------

def test_with_no_connector_the_feature_is_hidden(pg, admin, monkeypatch, tmp_path):
    lease(admin)
    eid = expense(admin)
    client = make_client(pg, monkeypatch, tmp_path, None)
    for method, path in (("get", f"/{eid}/claim"), ("post", f"/{eid}/claim"), ("post", f"/{eid}/claim/submit"),
                         ("post", f"/{eid}/claim/forget"), ("get", "/claims"), ("post", "/claims/refresh"),
                         ("post", "/claims/signin"), ("get", "/claims/signin/status"), ("post", "/claims/signin/code")):
        assert getattr(client, method)(path, headers=ORIGIN).status_code == 404, path
    html = client.get("/").data.decode()
    assert "Claim" not in html and "/claims" not in html


def test_a_connector_address_that_is_not_http_stops_the_app_with_a_clear_message(pg, admin, monkeypatch, tmp_path):
    with pytest.raises(RuntimeError, match="CLAIMS_CONNECTOR_URL"):
        make_client(pg, monkeypatch, tmp_path, "ftp://example.invalid")


def test_the_token_can_come_from_a_file_read_at_each_request(pg, admin, fake, monkeypatch, tmp_path):
    lease(admin)
    eid = expense(admin)
    token_file = tmp_path / "token"
    token_file.write_text("wrong\n")
    client = make_client(pg, monkeypatch, tmp_path, fake.url, CLAIMS_CONNECTOR_TOKEN_FILE=str(token_file))
    assert b"refused the token" in client.get(f"/{eid}/claim").data
    token_file.write_text(TOKEN + "\n")               # rotated: no restart needed
    assert b"Submit claim" in client.get(f"/{eid}/claim").data


def test_an_unreadable_token_file_is_explained(pg, admin, fake, monkeypatch, tmp_path):
    lease(admin)
    eid = expense(admin)
    client = make_client(pg, monkeypatch, tmp_path, fake.url, CLAIMS_CONNECTOR_TOKEN_FILE=str(tmp_path / "nope"))
    assert "token file can't be read" in said(client.get(f"/{eid}/claim"))


# ---- which expenses can be claimed ----------------------------------------------------------------------

@pytest.mark.parametrize("pool", claims.CLAIMABLE_POOLS)
def test_every_claimable_pool_is_eligible(pool):
    assert claims.claimable(pool, Decimal("10"), True, False)


@pytest.mark.parametrize("pool,amount,lease_covers,claimed", [
    ("lease", Decimal("10"), True, False), (None, Decimal("10"), True, False),
    ("tyres", Decimal("0"), True, False), ("tyres", Decimal("-5"), True, False),
    ("tyres", Decimal("10"), False, False), ("tyres", Decimal("10"), True, True)])
def test_what_is_not_eligible(pool, amount, lease_covers, claimed):
    assert not claims.claimable(pool, amount, lease_covers, claimed)


def test_the_list_offers_a_claim_only_for_eligible_expenses(client, admin):
    lease(admin, start="2026-01-01", end="2029-01-01")
    eligible = expense(admin)
    expense(admin, category="Initial purchase")                    # no pool
    expense(admin, amount="-50.00")                                # a refund
    expense(admin, at="2024-05-01 04:00+00")                       # before the lease
    claimed = expense(admin, at="2026-10-01 04:00+00")
    admin.execute("INSERT INTO garagebooks.expense_claim (expense_id, provider, provider_id, reference, status)"
                  " VALUES (%s, 'Example', 'P1', 'REF9', 'Paid')", (claimed,))
    html = client.get("/").data.decode()
    assert html.count('class="claim-link"') == 1 and f"/{eligible}/claim" in html
    assert "REF9" in html and "Paid" in html


def test_the_claim_page_refuses_an_expense_that_cannot_be_claimed(client, admin):
    lease(admin)
    refund = expense(admin, amount="-50.00")
    outside = expense(admin, at="2024-05-01 04:00+00")
    for eid in (refund, outside):
        resp = client.get(f"/{eid}/claim")
        assert resp.status_code == 400 and "can't be claimed" in said(resp)
    assert client.get("/9999/claim").status_code == 404


# ---- preview -------------------------------------------------------------------------------------------

def test_the_preview_shows_what_will_be_claimed_and_sends_the_expense_and_invoice(client, admin, fake, tyres):
    resp = client.get(f"/{tyres}/claim")
    html = resp.data.decode()
    assert resp.status_code == 200
    assert "Example Salary Packaging" in html and "Claim $1200.00 for Tyres" in html
    assert "$1,200.00" in html and "$109.09" in html and "$1,090.91" in html     # amount, GST, ex GST
    assert "Submit claim" in html and "Already claimed" not in html
    method, path, auth, body = last(fake, "/v1/preview")
    assert auth == f"Bearer {TOKEN}"
    exp = body["expense"]
    car = admin.execute("SELECT make, model FROM garagebooks.car WHERE id = 1").fetchone()
    assert exp == {"id": tyres, "car": {"id": 1, "name": "Test car", **car},
                   "pool": "tyres", "category": "Tyres", "date": "2026-11-07", "amount": 1200.0,
                   "vendor": "Example Tyres Pty Ltd", "notes": "Four tyres", "fields": {}}
    assert body["invoice"]["name"] == "INV12345.pdf" and body["invoice"]["content_type"] == "application/pdf"
    assert fake.invoice_bytes(body) == PDF                       # read through storage.py


def test_the_date_is_the_day_in_app_tz_not_utc(client, admin, fake):
    lease(admin)
    eid = expense(admin, at="2026-11-06 22:30+00")               # 08:30 on the 7th in Brisbane
    client.get(f"/{eid}/claim")
    assert last(fake, "/v1/preview")[3]["expense"]["date"] == "2026-11-07"


def test_a_warning_is_shown_prominently_and_there_is_no_submit_button(client, admin, fake, tyres):
    fake.config["warnings"] = ["Already claimed: a claim for this invoice exists"]
    html = client.get(f"/{tyres}/claim").data.decode()
    assert 'class="notice bad"' in html and "Already claimed: a claim for this invoice exists" in html
    assert "Submit claim" not in html


def test_gst_can_be_entered_and_is_sent_with_the_fields(client, admin, fake, tyres):
    html = client.post(f"/{tyres}/claim", data={"field_gst": "100.00"}, headers=ORIGIN).data.decode()
    assert last(fake, "/v1/preview")[3]["expense"]["fields"] == {"gst": "100.00"}
    assert 'name="field_gst" value="100.00"' in html
    html = client.post(f"/{tyres}/claim", data={"field_gst": "lots"}, headers=ORIGIN).data.decode()
    assert "GST must be an amount" in html


def test_needed_fields_are_asked_for_then_sent_back_to_the_preview(client, admin, fake, tyres):
    fake.config["needs_fields"] = [{"key": "description", "label": "What was it for?", "type": "text"},
                                   {"key": "invoice_date", "label": "Invoice date", "type": "date"}]
    html = client.get(f"/{tyres}/claim").data.decode()
    assert "What was it for?" in html and "Invoice date" in html
    assert 'name="field_description"' in html and 'name="field_invoice_date" type="date"' in html
    assert "Submit claim" not in html and "Check again" in html
    html = client.post(f"/{tyres}/claim", headers=ORIGIN,
                       data={"field_description": "New tyres", "field_invoice_date": "2026-11-07"}).data.decode()
    assert last(fake, "/v1/preview")[3]["expense"]["fields"] == {"description": "New tyres", "invoice_date": "2026-11-07"}
    assert "Submit claim" in html and 'value="New tyres"' in html


def test_an_unknown_field_asked_for_by_a_connector_is_ignored():
    reply = claims.Reply(422, {"ok": False, "needs_fields": [{"key": "bank_account", "label": "x", "type": "text"},
                                                              {"key": "start", "label": "From", "type": "weird"}]})
    assert reply.needs_fields == [{"key": "start", "label": "From", "type": "text"}]


@pytest.mark.parametrize("status,body,shown", [
    (400, {"ok": False, "error": "The invoice is unreadable."}, "The invoice is unreadable."),
    (502, {"ok": False, "error": "The provider's site is down."}, "The provider&#39;s site is down."),
    (500, {"ok": False, "error": "Something broke <b>badly</b>"}, "Something broke &lt;b&gt;badly&lt;/b&gt;"),
    (500, {"ok": False}, "answered 500")])
def test_a_connector_error_is_shown_as_text(client, admin, fake, tyres, status, body, shown):
    fake.config["override"] = {"preview": (status, body)}
    resp = client.get(f"/{tyres}/claim")
    assert shown in resp.data.decode() and "Submit claim" not in resp.data.decode()
    assert resp.status_code == 200


def test_an_unreachable_connector_is_explained(pg, admin, monkeypatch, tmp_path):
    lease(admin)
    eid = expense(admin)
    client = make_client(pg, monkeypatch, tmp_path, "http://127.0.0.1:9")
    assert "can't be reached" in said(client.get(f"/{eid}/claim"))


def test_an_answer_that_is_not_json_is_explained(client, admin, fake, tyres):
    fake.config["override"] = {"preview": (200, ["not", "an", "object"])}
    assert b"something other than JSON" in client.get(f"/{tyres}/claim").data


def test_a_slow_connector_is_given_long_enough_and_gunicorn_waits_longer():
    assert claims.TIMEOUT_SECONDS >= 150
    conf = runpy.run_path("gunicorn.conf.py")
    assert conf["timeout"] > claims.TIMEOUT_SECONDS
    assert "--timeout" not in open("Dockerfile").read()          # the command line would override the file


# ---- the invoice -----------------------------------------------------------------------------------

def test_a_link_only_invoice_asks_for_the_file_first(client, admin, fake):
    lease(admin)
    eid = expense(admin, invoice_path=None, invoice_url="https://example.com/inv.pdf")
    resp = client.get(f"/{eid}/claim")
    assert "attach the invoice file first" in said(resp) and b"Submit claim" not in resp.data
    assert not [c for c in fake.calls if c[1] == "/v1/preview"]


def test_no_invoice_at_all_is_sent_as_null_and_the_connector_decides(client, admin, fake):
    lease(admin)
    eid = expense(admin, invoice_path=None)
    resp = client.get(f"/{eid}/claim")
    assert b"No invoice is attached" in resp.data
    assert last(fake, "/v1/preview")[3]["invoice"] is None


def test_an_invoice_file_that_has_gone_is_explained(client, admin, fake):
    lease(admin)
    eid = expense(admin, invoice_path="/Tyres/missing.pdf")
    assert "isn't in the invoice folder" in said(client.get(f"/{eid}/claim"))
    assert not [c for c in fake.calls if c[1] == "/v1/preview"]


def test_an_invoice_over_ten_megabytes_is_refused(client, admin, fake, tyres, monkeypatch):
    monkeypatch.setattr(claims, "MAX_INVOICE_BYTES", len(PDF) - 1)
    assert b"too large" in client.get(f"/{tyres}/claim").data
    assert not [c for c in fake.calls if c[1] == "/v1/preview"]


# ---- the invoice, from either storage -------------------------------------------------------------
# Every claim test above uses an invoice in the folder on the server. An expense whose invoice was uploaded to
# (or imported from) OneDrive keeps only the file's OneDrive address, and claiming one used to be refused as
# "only a link". These run the claim against both storages, so neither can be left out again.

@pytest.fixture()
def ms():
    return FakeMicrosoft()


@pytest.fixture()
def drive_client(pg, admin, fake, ms, monkeypatch, tmp_path):
    """The app with OneDrive connected (and the folder on the server too, as the plain client has it)."""
    client = make_client(pg, monkeypatch, tmp_path, fake.url, transport=ms, GARAGEBOOKS_ENCRYPTION_KEY=KEY)
    sign_in(client, ms)
    return client


@pytest.fixture(params=["local", "onedrive"])
def stored(request, admin):
    """-> (client, the eligible expense, the invoice's file name), the invoice kept in each storage in turn."""
    lease(admin)
    if request.param == "local":
        return request.getfixturevalue("client"), expense(admin), "INV12345.pdf"
    client = request.getfixturevalue("drive_client")
    return client, expense(admin, invoice_path=None, invoice_url=ONEDRIVE_URL), "receipt.pdf"


def test_the_preview_sends_the_invoice_from_either_storage(stored, fake):
    client, eid, name = stored
    resp = client.get(f"/{eid}/claim")
    assert resp.status_code == 200 and b"Submit claim" in resp.data and b"attach the invoice" not in resp.data
    body = last(fake, "/v1/preview")[3]
    assert body["invoice"]["name"] == name and body["invoice"]["content_type"] == "application/pdf"
    assert fake.invoice_bytes(body) == PDF


def test_submit_sends_the_invoice_from_either_storage(stored, fake, admin):
    client, eid, name = stored
    assert submit(client, eid).status_code == 303
    body = last(fake, "/v1/submit")[3]
    assert body["invoice"]["name"] == name and fake.invoice_bytes(body) == PDF
    assert [r["expense_id"] for r in claim_rows(admin)] == [eid]


def test_a_onedrive_invoice_is_fetched_by_its_item_id_without_a_token_to_the_cdn(drive_client, admin, fake, ms):
    lease(admin)
    eid = expense(admin, invoice_path=None, invoice_url=ONEDRIVE_URL)
    drive_client.get(f"/{eid}/claim")
    asked = [c for c in ms.calls if "/me/drive/items/F1" in c[1]]
    assert len(asked) == 1 and "/content" not in asked[0][1]
    (cdn,) = [c for c in ms.calls if c[1] == FILE_DOWNLOAD]
    assert "Authorization" not in cdn[3]


def test_a_onedrive_address_without_an_item_id_is_looked_up_as_a_share(drive_client, admin, fake, ms):
    lease(admin)
    eid = expense(admin, invoice_path=None, invoice_url=SHARE_URL)
    resp = drive_client.get(f"/{eid}/claim")
    assert b"Submit claim" in resp.data and fake.invoice_bytes(last(fake, "/v1/preview")[3]) == PDF
    assert any("/shares/u!" in c[1] for c in ms.calls)


def test_a_onedrive_invoice_that_has_gone_is_explained(drive_client, admin, fake):
    lease(admin)
    eid = expense(admin, invoice_path=None, invoice_url="https://onedrive.live.com?cid=ABC&id=ABC!GONE")
    page = said(drive_client.get(f"/{eid}/claim"))
    assert "isn't in OneDrive any more" in page and "Submit claim" not in page
    assert not [c for c in fake.calls if c[1] == "/v1/preview"]


def test_a_lapsed_onedrive_sign_in_is_explained(drive_client, admin, fake, ms, monkeypatch):
    lease(admin)
    eid = expense(admin, invoice_path=None, invoice_url=ONEDRIVE_URL)
    ms.refresh_ok = False
    an_hour_passes(monkeypatch)                         # the kept access token has expired
    page = said(drive_client.get(f"/{eid}/claim"))
    assert "OneDrive sign-in has expired" in page and "Submit claim" not in page
    assert not [c for c in fake.calls if c[1] == "/v1/preview"]


def test_a_onedrive_invoice_over_ten_megabytes_is_refused_before_it_is_downloaded(drive_client, admin, fake, ms,
                                                                                  monkeypatch):
    lease(admin)
    eid = expense(admin, invoice_path=None, invoice_url=ONEDRIVE_URL)
    monkeypatch.setattr(claims, "MAX_INVOICE_BYTES", len(PDF) - 1)
    assert b"too large" in drive_client.get(f"/{eid}/claim").data
    assert not [c for c in ms.calls if c[1] == FILE_DOWNLOAD]
    assert not [c for c in fake.calls if c[1] == "/v1/preview"]


def test_a_onedrive_invoice_without_onedrive_connected_says_to_connect_it(pg, admin, fake, ms, monkeypatch, tmp_path):
    client = make_client(pg, monkeypatch, tmp_path, fake.url, transport=ms, GARAGEBOOKS_ENCRYPTION_KEY=KEY)
    lease(admin)
    eid = expense(admin, invoice_path=None, invoice_url=ONEDRIVE_URL)
    page = said(client.get(f"/{eid}/claim"))
    assert "Connect OneDrive first" in page and "Submit claim" not in page


# ---- signing in to the provider ----------------------------------------------------------------------

def test_a_409_offers_the_sign_in_and_the_flow_runs_to_signed_in(client, admin, fake, tyres):
    fake.config["signed_in"] = False
    html = client.get(f"/{tyres}/claim").data.decode()
    assert "Sign in to Example Salary Packaging" not in html     # the name isn't known until health answers
    assert 'id="claim-signin"' in html and "Submit claim" not in html
    assert client.get("/claims/signin/status").get_json()["signin"]["state"] == "idle"
    assert client.post("/claims/signin", headers=ORIGIN).get_json()["ok"] is True
    state = client.get("/claims/signin/status").get_json()
    assert state["signin"]["state"] == "code_needed" and "code" in state["signin"]["message"]
    assert client.post("/claims/signin/code", json={"code": "123456"}, headers=ORIGIN).get_json()["ok"] is True
    assert client.get("/claims/signin/status").get_json()["signin"]["state"] == "signed_in"
    assert b"Submit claim" in client.get(f"/{tyres}/claim").data
    assert last(fake, "/v1/signin/code")[3] == {"code": "123456"}


def test_a_wrong_code_shows_the_connectors_error(client, admin, fake, tyres):
    fake.config["signed_in"] = False
    client.post("/claims/signin", headers=ORIGIN)
    client.post("/claims/signin/code", json={"code": "000000"}, headers=ORIGIN)
    signin = client.get("/claims/signin/status").get_json()["signin"]
    assert signin == {"state": "failed", "error": "That code was not accepted."}


@pytest.mark.parametrize("payload", [{}, {"code": ""}, {"code": "x" * 50}, {"code": 123456}, {"code": ["1"]}])
def test_a_bad_code_is_refused_before_it_reaches_the_connector(client, fake, payload):
    resp = client.post("/claims/signin/code", json=payload, headers=ORIGIN)
    assert resp.status_code == 400 and not [c for c in fake.calls if c[1] == "/v1/signin/code"]


def test_the_sign_in_status_is_cleaned_before_it_reaches_the_page(client, fake):
    fake.config["signin"] = {"state": "code_needed", "message": "Enter it", "secret": "x", "error": 7}
    assert client.get("/claims/signin/status").get_json() == {
        "ok": True, "provider": "Example Salary Packaging", "signin": {"state": "code_needed", "message": "Enter it"}}
    fake.config["signin"] = {"state": "bogus"}
    assert client.get("/claims/signin/status").get_json()["signin"] == {"state": "idle"}


def test_sign_in_calls_report_an_unreachable_connector(pg, admin, monkeypatch, tmp_path):
    client = make_client(pg, monkeypatch, tmp_path, "http://127.0.0.1:9")
    resp = client.get("/claims/signin/status")
    assert resp.status_code == 502 and "can't be reached" in resp.get_json()["error"]
    assert client.post("/claims/signin", headers=ORIGIN).status_code == 502


# ---- submitting ------------------------------------------------------------------------------------------

def submit(client, eid, **data):
    return client.post(f"/{eid}/claim/submit", data=data, headers=ORIGIN)


def test_submit_sends_confirm_stores_the_claim_and_flashes_the_reference(client, admin, fake, tyres):
    resp = submit(client, tyres, field_gst="100.00", field_description="New tyres")
    assert resp.status_code == 303
    body = last(fake, "/v1/submit")[3]
    assert body["confirm"] is True and body["expense"]["fields"] == {"description": "New tyres", "gst": "100.00"}
    assert fake.invoice_bytes(body) == PDF
    (row,) = claim_rows(admin)
    assert (row["expense_id"], row["provider"], row["provider_id"], row["reference"], row["status"]) == (
        tyres, "Example Salary Packaging", "P0001", "REF0001", "Submitted")
    assert row["amount"] == Decimal("1200.00") and row["gst"] == Decimal("100.00")
    assert row["fields"] == {"description": "New tyres", "gst": "100.00"} and row["submitted_at"] is not None
    page = client.get(resp.headers["Location"]).data.decode()
    assert "REF0001" in page and "Claim submitted" in page
    assert 'class="claim-link"' not in page                      # nothing left to claim
    assert "Submitted" in page


def test_the_gst_stored_is_the_one_previewed_when_none_was_typed(client, admin, tyres):
    submit(client, tyres)
    (row,) = claim_rows(admin)
    assert row["amount"] == Decimal("1200.00") and row["gst"] is None     # the connector worked it out itself


def test_a_claim_is_in_the_change_history(client, admin, tyres):
    submit(client, tyres)
    entry = admin.execute("SELECT * FROM garagebooks.change_log WHERE table_name = 'expense_claim'").fetchone()
    assert entry["row_id"] == tyres and entry["operation"] == "insert"
    page = client.get("/history").data.decode()
    assert "Claim" in page and "REF0001" in page
    assert client.get("/history?table=expense_claim").status_code == 200


def test_a_refused_submit_stores_nothing_and_says_why(client, admin, fake, tyres):
    fake.config["override"] = {"submit": (400, {"ok": False, "error": "Already claimed: REF0099"})}
    resp = submit(client, tyres)
    assert resp.status_code == 200 and b"Already claimed: REF0099" in resp.data
    assert claim_rows(admin) == []


def test_a_submit_needing_fields_or_sign_in_goes_back_to_the_page(client, admin, fake, tyres):
    fake.config["signed_in"] = False
    resp = submit(client, tyres)
    assert resp.status_code == 200 and b'id="claim-signin"' in resp.data and claim_rows(admin) == []
    fake.config.update(signed_in=True, needs_fields=[{"key": "start", "label": "From", "type": "date"}])
    resp = submit(client, tyres)
    assert b'name="field_start"' in resp.data and claim_rows(admin) == []


def test_submitting_twice_never_claims_twice(client, admin, fake, tyres):
    submit(client, tyres)
    again = submit(client, tyres)
    assert again.status_code == 400 and b"already been claimed" in again.data
    assert len([c for c in fake.calls if c[1] == "/v1/submit"]) == 1 and len(claim_rows(admin)) == 1
    assert b"already been claimed" in client.get(f"/{tyres}/claim").data


def test_a_reply_missing_its_id_is_not_stored_as_a_claim(client, admin, fake, tyres):
    fake.config["override"] = {"submit": (200, {"ok": True, "reference": "REF1"})}
    resp = submit(client, tyres)
    assert resp.status_code == 502 and claim_rows(admin) == []
    assert b"check the provider" in resp.data.lower()


def test_submit_is_a_post_with_the_same_origin_guard(client, tyres):
    assert client.get(f"/{tyres}/claim/submit").status_code == 405
    assert client.post(f"/{tyres}/claim/submit", data={}).status_code == 403


# ---- refreshing the statuses ---------------------------------------------------------------------------

def test_refresh_updates_status_approved_amount_and_paid_date(client, admin, fake, tyres):
    submit(client, tyres)
    fake.config["statuses"] = {"P0001": {"provider_id": "P0001", "reference": "REF0001", "status": "Paid",
                                         "approved_amount": 1100.0, "paid_date": "2026-11-14"}}
    resp = client.post("/claims/refresh", headers=ORIGIN)
    assert resp.status_code == 303 and "msg=claims_refreshed" in resp.headers["Location"]
    assert last(fake, "/v1/status")[3] == {"provider_ids": ["P0001"]}
    (row,) = claim_rows(admin)
    assert (row["status"], row["approved_amount"], row["paid_date"]) == ("Paid", Decimal("1100.00"), date(2026, 11, 14))
    assert row["status_checked_at"] is not None
    page = client.get("/claims").data.decode()
    assert "Paid" in page and "14 Nov 2026" in page and "$1,100.00" in page and "REF0001" in page


def test_a_refresh_is_not_a_change_in_the_history(client, admin, fake, tyres):
    submit(client, tyres)
    fake.config["statuses"] = {"P0001": {"provider_id": "P0001", "status": "Submitted", "approved_amount": None,
                                         "paid_date": None}}
    client.post("/claims/refresh", headers=ORIGIN)
    assert admin.execute("SELECT count(*) AS n FROM garagebooks.change_log WHERE table_name = 'expense_claim'"
                         ).fetchone()["n"] == 1
    fake.config["statuses"]["P0001"]["status"] = "Paid"
    client.post("/claims/refresh", headers=ORIGIN)
    assert admin.execute("SELECT count(*) AS n FROM garagebooks.change_log WHERE table_name = 'expense_claim'"
                         ).fetchone()["n"] == 2


def test_a_claim_the_provider_does_not_know_keeps_its_status(client, admin, fake, tyres):
    submit(client, tyres)
    resp = client.post("/claims/refresh", headers=ORIGIN)       # the fake knows no statuses: "not found"
    (row,) = claim_rows(admin)
    assert row["status"] == "Submitted" and row["status_checked_at"] is not None
    assert "msg=claims_refreshed" in resp.headers["Location"]


def test_refresh_with_nothing_claimed_asks_nothing(client, fake):
    resp = client.post("/claims/refresh", headers=ORIGIN)
    assert "msg=claims_none" in resp.headers["Location"] and not [c for c in fake.calls if c[1] == "/v1/status"]


def test_refresh_reports_a_connector_that_needs_signing_in_or_is_down(client, admin, fake, tyres):
    submit(client, tyres)
    fake.config["override"] = {"status": (409, {"ok": False, "error": "x", "needs": "signin"})}
    page = client.get(client.post("/claims/refresh", headers=ORIGIN).headers["Location"]).data.decode()
    assert "sign in" in page.lower()
    fake.config["override"] = {"status": (502, {"ok": False, "error": "The provider's site is down."})}
    page = client.get(client.post("/claims/refresh", headers=ORIGIN).headers["Location"]).data.decode()
    assert "site is down" in page


def test_the_claims_page_lists_claims_and_the_unclaimed_expenses_to_do(client, admin, tyres):
    other = expense(admin, category="Insurance", amount="900.00", at="2026-10-01 04:00+00")
    submit(client, other)
    page = client.get("/claims").data.decode()
    assert "Refresh claim statuses" in page and "REF0001" in page
    assert f"/{tyres}/claim" in page                         # still to claim
    assert f"/{other}/claim" not in page                     # already claimed


# ---- deleting -------------------------------------------------------------------------------------------

def test_a_claimed_expense_cannot_be_deleted_by_accident(client, admin, tyres):
    submit(client, tyres)
    resp = client.post(f"/{tyres}/delete", headers=ORIGIN)
    assert resp.status_code == 409 and b"has been claimed" in resp.data and b"Remove" in resp.data
    assert admin.execute("SELECT count(*) AS n FROM garagebooks.expense").fetchone()["n"] == 1


def test_the_database_itself_refuses_it_too(admin, tyres):
    admin.execute("INSERT INTO garagebooks.expense_claim (expense_id, provider, provider_id) VALUES (%s, 'P', 'x')", (tyres,))
    with pytest.raises(psycopg.errors.RestrictViolation):
        admin.execute("DELETE FROM garagebooks.expense WHERE id = %s", (tyres,))


def test_removing_the_claim_record_lets_the_expense_be_deleted_and_is_logged(client, admin, tyres):
    submit(client, tyres)
    resp = client.post(f"/{tyres}/claim/forget", headers=ORIGIN)
    assert resp.status_code == 303 and claim_rows(admin) == []
    ops = [r["operation"] for r in admin.execute("SELECT operation FROM garagebooks.change_log"
                                                 " WHERE table_name = 'expense_claim' ORDER BY id")]
    assert ops == ["insert", "delete"]
    assert client.post(f"/{tyres}/delete", headers=ORIGIN).status_code == 303
    assert client.post(f"/{tyres}/claim/forget", headers=ORIGIN).status_code == 404


def test_the_edit_page_shows_the_claim_and_a_way_to_remove_the_record(client, admin, tyres):
    page = client.get(f"/{tyres}/edit").data.decode()
    assert f"/{tyres}/claim" in page and "forget" not in page
    submit(client, tyres)
    page = client.get(f"/{tyres}/edit").data.decode()
    assert "REF0001" in page and f"/{tyres}/claim/forget" in page


# ---- storage --------------------------------------------------------------------------------------------

def test_the_claim_table_is_there_and_the_restricted_role_can_use_it(pg, admin, tyres):
    """The suite's database is provisioned by scripts/provision-db.sh, so the app here is the least-privilege role."""
    with psycopg.connect(pg.app_url, row_factory=dict_row, autocommit=True) as app:
        app.execute("INSERT INTO garagebooks.expense_claim (expense_id, provider, provider_id) VALUES (%s, 'P', 'x')", (tyres,))
        app.execute("UPDATE garagebooks.expense_claim SET status = 'Paid'")
        assert app.execute("DELETE FROM garagebooks.expense_claim").rowcount == 1


def test_one_claim_per_expense(admin, tyres):
    admin.execute("INSERT INTO garagebooks.expense_claim (expense_id, provider, provider_id) VALUES (%s, 'P', 'x')", (tyres,))
    with pytest.raises(psycopg.errors.UniqueViolation):
        admin.execute("INSERT INTO garagebooks.expense_claim (expense_id, provider, provider_id) VALUES (%s, 'P', 'y')", (tyres,))


def test_the_schema_applies_twice_and_upgrades_a_log_made_before_claims(empty_db, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", empty_db)
    db.migrate()
    with psycopg.connect(empty_db, autocommit=True) as conn:       # a database as it was before this release
        conn.execute("DROP TABLE garagebooks.expense_claim")
        conn.execute("ALTER TABLE garagebooks.change_log DROP CONSTRAINT change_log_table_name_check;"
                     " ALTER TABLE garagebooks.change_log ADD CONSTRAINT change_log_table_name_check"
                     " CHECK (table_name IN ('expense', 'category'))")
    db.migrate()
    db.migrate()
    with psycopg.connect(empty_db) as conn:
        conn.execute("INSERT INTO garagebooks.car (id, name) VALUES (5, 'C')")
        conn.execute("INSERT INTO garagebooks.expense (id, car_id, category_id, occurred_at, amount)"
                     " VALUES (7, 5, 1, now(), 10)")
        conn.execute("INSERT INTO garagebooks.expense_claim (expense_id, provider, provider_id) VALUES (7, 'P', 'x')")
        assert conn.execute("SELECT row_id FROM garagebooks.change_log WHERE table_name = 'expense_claim'"
                            ).fetchone()[0] == 7


def test_the_clean_fields_are_bounded():
    fields, errors = claims.clean_fields({"field_description": " " + "x" * 900, "field_bogus": "y", "field_start": ""})
    assert list(fields) == ["description"] and len(fields["description"]) == claims.FIELD_LIMIT and errors == []


def test_a_numeric_look_alike_from_the_connector_is_not_a_money_value():
    assert claims.number("12.34") == Decimal("12.34")
    assert claims.number(True) is None and claims.number("NaN") is None and claims.number("abc") is None
    assert re.fullmatch(r"-?\d+\.\d\d", str(claims.number(5)))


@pytest.mark.parametrize("value", ["1E30", "1e400", 10**30, "99999999999.99", "-1E30"])
def test_a_money_value_too_big_for_the_database_is_refused_not_a_crash(value):
    """Found by the property test: 1E30 made quantize() raise."""
    assert claims.number(value) is None
    fields, errors = claims.clean_fields({"field_gst": str(value)})
    assert "gst" not in fields and errors or str(value).startswith("-") and errors


# ---- the period the expense covers ------------------------------------------------------------------

def registration(admin):
    lease(admin)
    eid = expense(admin, category="Registration", amount="500.00")
    admin.execute("UPDATE garagebooks.expense SET period_start='2026-12-01', period_end='2027-11-30' WHERE id=%s", (eid,))
    return eid


def test_the_claim_page_starts_with_the_expenses_period(client, admin, fake):
    eid = registration(admin)
    page = client.get(f"/{eid}/claim").data.decode()
    assert last(fake, "/v1/preview")[3]["expense"]["fields"] == {"start": "2026-12-01", "end": "2027-11-30"}
    # and carries it on to Submit
    assert 'name="field_start" value="2026-12-01"' in page and 'name="field_end" value="2027-11-30"' in page


def test_a_period_changed_on_the_claim_page_wins_over_the_expenses(client, admin, fake):
    eid = registration(admin)
    client.post(f"/{eid}/claim", data={"field_start": "2026-12-02", "field_end": "2027-12-01"}, headers=ORIGIN)
    assert last(fake, "/v1/preview")[3]["expense"]["fields"] == {"start": "2026-12-02", "end": "2027-12-01"}


def test_an_expense_with_half_a_period_sends_the_half_it_has(client, admin, fake):
    eid = registration(admin)
    admin.execute("UPDATE garagebooks.expense SET period_start=NULL WHERE id=%s", (eid,))
    client.get(f"/{eid}/claim")
    assert last(fake, "/v1/preview")[3]["expense"]["fields"] == {"end": "2027-11-30"}
