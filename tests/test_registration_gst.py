"""A registration's GST (only the CTP part carries any) read from the notice and stored per expense, overriding the
category's GST rule, and the payment references on the notice (kept out of sight). Figures are invented."""

import re
from decimal import Decimal

import psycopg
import pytest

from garagebooks import invoices, lease, validation
from tests.test_app import ORIGIN, category_id, client, form, rows  # noqa: F401  (fixtures)
from tests.test_claims import expense, fake, tyres  # noqa: F401  (fixtures)
from tests.test_claims import client as claims_client  # noqa: F401
from tests.test_history import plain
from tests.test_local import app_for, choose, root  # noqa: F401  (fixtures)
from tests.test_local import client as local_client  # noqa: F401

D = Decimal
CATS = ["Initial purchase", "Insurance", "Registration", "Tyres"]

NOTICE = """
   Department of Transport and Main Roads
   VEHICLE REGISTRATION RENEWAL NOTICE
   Registration number   ABC123            Registration expiry date   30/06/2027
   Registration period   12 months
   Total Due   $300.00
   Includes CTP insurance premium            $200.00
   GST included in CTP insurance premium     $18.18
   Pay by card.  Card payment reference:   1234 5678 9012
   BPAY   Biller code: 12345      Ref: 9876 5432 1098
"""


# ---- reading the notice ---------------------------------------------------------------------

def test_the_gst_and_payment_references_are_read_from_a_registration_notice():
    got = invoices.propose("", "x.pdf", NOTICE, CATS)
    assert got["category"] == "Registration"
    assert got["gst_amount"] == D("18.18")
    assert got["card_payment_ref"] == "1234 5678 9012"
    assert (got["bpay_biller_code"], got["bpay_ref"]) == ("12345", "9876 5432 1098")


def test_a_notice_with_no_gst_line_leaves_it_empty_rather_than_guessing():
    text = NOTICE.replace("   GST included in CTP insurance premium     $18.18\n", "")
    assert invoices.propose("", "x.pdf", text, CATS)["gst_amount"] is None


def test_other_documents_read_no_gst_or_references():
    text = "TAX INVOICE\nExample Tyres Pty Ltd\nGST      $112.23\nTotal    $1,234.50\nCard payment reference 99887766\n"
    got = invoices.propose("", "x.pdf", text, CATS)
    assert got["category"] == "Tyres" or got["category"] is None
    assert (got["gst_amount"], got["card_payment_ref"], got["bpay_biller_code"], got["bpay_ref"]) == (None, None, None, None)


def test_a_gst_that_is_more_than_the_total_is_not_used():
    text = NOTICE.replace("$18.18", "$900.00")
    assert invoices.propose("", "x.pdf", text, CATS)["gst_amount"] is None


# ---- the database ---------------------------------------------------------------------------

def add(admin, amount, gst, category="Registration", packaged=True):
    return admin.execute(
        "INSERT INTO garagebooks.expense (car_id, category_id, occurred_at, amount, gst_amount, packaged) "
        "VALUES (1, %s, '2026-11-07 04:05+00', %s, %s, %s) RETURNING id",
        (category_id(admin, category), amount, gst, packaged)).fetchone()["id"]


def test_the_columns_and_their_checks(admin):
    for amount, gst in (("300", "301"), ("300", "-1"), ("-300", "1"), ("-300", "-301")):
        with pytest.raises(psycopg.errors.CheckViolation):
            add(admin, amount, gst)
    add(admin, "300", "18.18")
    add(admin, "300", "0")
    add(admin, "-300", "-18.18")
    admin.execute("UPDATE garagebooks.expense SET card_payment_ref = '1234', bpay_biller_code = '12345', bpay_ref = '987'")
    with pytest.raises(psycopg.errors.CheckViolation):
        admin.execute("UPDATE garagebooks.expense SET card_payment_ref = %s", ("1" * 61,))


def test_registration_is_gst_free_by_default_and_a_stored_gst_overrides_the_rule(admin):
    admin.execute("UPDATE garagebooks.setting SET marginal_tax_rate = 34")
    assert admin.execute("SELECT gst_free FROM garagebooks.category WHERE name = 'Registration'").fetchone()["gst_free"]
    plain_one, with_gst = add(admin, "300.00", None), add(admin, "300.00", "18.18")
    got = {r["expense_id"]: r["effective_cost"] for r in admin.execute(
        "SELECT * FROM garagebooks.expense_after_tax('Australia/Brisbane')")}
    assert got[plain_one] == D("198.00")                  # GST-free: no GST to claim back
    assert got[with_gst] == D("186.00")                   # (300.00 - 18.18) x 0.66, rounded to the cent


def test_a_stored_gst_overrides_a_category_that_charges_it_too(admin):
    admin.execute("UPDATE garagebooks.setting SET marginal_tax_rate = 34")
    eid = add(admin, "330.00", "0.00", category="Tyres")
    got = admin.execute("SELECT effective_cost FROM garagebooks.expense_after_tax('Australia/Brisbane') "
                        "WHERE expense_id = %s", (eid,)).fetchone()["effective_cost"]
    assert got == D("217.80")                             # no GST at all: 330.00 x 0.66, not x 10/11


def test_the_python_formula_takes_the_stored_gst_too():
    assert lease.effective_cost(D("300.00"), True, True, None, None, D("34"), gst_amount=D("18.18")) == D("186.00")
    assert lease.effective_cost(D("330.00"), True, False, None, None, D("34"), gst_amount=D("0")) == D("217.80")
    assert lease.effective_cost(D("330.00"), True, False, None, None, D("34")) == D("198.00")
    assert lease.effective_cost(D("300.00"), False, True, None, None, D("34"), gst_amount=D("18.18")) == D("300.00")


# ---- the form --------------------------------------------------------------------------------

def parse(**over):
    data = {"car_id": "1", "category_id": "3", "date": "2026-11-07", "time": "12:00", "amount": "300.00", **over}
    return validation.parse_expense_form(data, "Australia/Brisbane", {1}, {3})


def test_the_form_parses_gst_and_references():
    values, errors = parse(gst_amount="18.18", card_payment_ref=" 1234 5678 ", bpay_biller_code="12345", bpay_ref="9876 5432")
    assert not errors
    assert values["gst_amount"] == D("18.18") and values["card_payment_ref"] == "1234 5678"
    assert (values["bpay_biller_code"], values["bpay_ref"]) == ("12345", "9876 5432")
    values, errors = parse()
    assert not errors and values["gst_amount"] is None and values["card_payment_ref"] is None


def test_a_bad_gst_or_reference_is_refused():
    assert "gst_amount" in parse(gst_amount="lots")[1]
    assert "gst_amount" in parse(gst_amount="301")[1]
    assert "gst_amount" in parse(gst_amount="-1")[1]
    assert "card_payment_ref" in parse(card_payment_ref="x" * 61)[1]
    assert "bpay_biller_code" in parse(bpay_biller_code="12 <b>")[1]


def saved_form(admin, **over):
    return form(admin, category_id=str(category_id(admin, "Registration")), amount="300.00", **over)


def test_the_edit_form_keeps_gst_and_has_the_references_in_a_closed_details(client, admin):  # noqa: F811
    client.post("/new", data=saved_form(admin, gst_amount="18.18", card_payment_ref="1234 5678 9012",
                                        bpay_biller_code="12345", bpay_ref="9876 5432 1098"), headers=ORIGIN)
    expense_id = rows(admin)[0]["id"]
    row = rows(admin)[0]
    assert row["gst_amount"] == D("18.18") and row["bpay_ref"] == "9876 5432 1098"
    page = client.get(f"/{expense_id}/edit").data.decode()
    assert 'name="gst_amount" inputmode="decimal" autocomplete="off" value="18.18"' in page
    details = re.search(r"<details[^>]*>\s*<summary>Payment references</summary>(.*?)</details>", page, re.S)
    assert details and "open" not in re.search(r"<details[^>]*>", details.group(0)).group(0)
    assert 'value="1234 5678 9012"' in details.group(1) and 'value="12345"' in details.group(1)
    client.post(f"/{expense_id}/edit", data=saved_form(admin, gst_amount="", card_payment_ref="", bpay_biller_code="",
                                                       bpay_ref=""), headers=ORIGIN)
    row = rows(admin)[0]
    assert row["gst_amount"] is None and row["card_payment_ref"] is None


def test_the_references_are_not_on_the_list_or_in_the_change_history(client, admin):  # noqa: F811
    client.post("/new", data=saved_form(admin, gst_amount="18.18", card_payment_ref="1234 5678 9012",
                                        bpay_biller_code="12345", bpay_ref="9876 5432 1098"), headers=ORIGIN)
    for path in ("/", "/history"):
        page = client.get(path).data.decode()
        assert "9876" not in page and "1234 5678 9012" not in page and "12345" not in plain(page)
    history = plain(client.get("/history").data.decode())
    assert "GST" in history and "18.18" in history


# ---- claims ----------------------------------------------------------------------------------

def test_the_claim_page_offers_the_stored_gst_in_the_protocols_gst_field(claims_client, fake, admin, tyres):  # noqa: F811
    admin.execute("UPDATE garagebooks.expense SET gst_amount = 109.09 WHERE id = %s", (tyres,))
    html = claims_client.get(f"/{tyres}/claim").data.decode()
    assert 'name="field_gst" value="109.09"' in html
    sent = [c for c in fake.calls if c[1] == "/v1/preview"][-1][3]["expense"]["fields"]
    assert sent == {"gst": "109.09"}


def test_clearing_the_gst_on_the_claim_page_is_respected(claims_client, fake, admin, tyres):  # noqa: F811
    admin.execute("UPDATE garagebooks.expense SET gst_amount = 109.09 WHERE id = %s", (tyres,))
    html = claims_client.post(f"/{tyres}/claim", data={"field_gst": ""}, headers=ORIGIN).data.decode()
    assert 'name="field_gst" value=""' in html
    assert [c for c in fake.calls if c[1] == "/v1/preview"][-1][3]["expense"]["fields"] == {}


def test_without_a_stored_gst_the_claim_page_is_as_before(claims_client, fake, tyres):  # noqa: F811
    assert 'name="field_gst" value=""' in claims_client.get(f"/{tyres}/claim").data.decode()


# ---- import ----------------------------------------------------------------------------------

@pytest.fixture()
def stocked(local_client, root, monkeypatch):
    (root / "Cars" / "Test car" / "rego.pdf").write_bytes(b"%PDF-rego")
    monkeypatch.setattr(invoices, "extract_text", lambda pdf: NOTICE)
    choose(local_client)
    return local_client


def test_the_import_row_shows_the_gst_hides_the_references_and_saves_them(stocked, admin):
    path = "/Cars/Test car/rego.pdf"
    row = stocked.post("/local/import/1/read", headers=ORIGIN, data={
        "n": "0", "id": path, "url": path, "name": "rego.pdf", "folder": ""}).data.decode()
    assert 'name="gst_amount_0" inputmode="decimal" value="18.18"' in row
    for name, value in (("card_payment_ref", "1234 5678 9012"), ("bpay_biller_code", "12345"), ("bpay_ref", "9876 5432 1098")):
        assert f'<input type="hidden" name="{name}_0" value="{value}">' in row
    assert 'type="hidden" name="gst_amount_0"' not in row
    data = {"count": "1", "include_0": "on", "id_0": path, "url_0": path, "name_0": "rego.pdf", "date_0": "2026-06-01",
            "category_id_0": str(category_id(admin, "Registration")), "amount_0": "300.00", "vendor_0": "Department of Transport",
            "gst_amount_0": "18.18", "card_payment_ref_0": "1234 5678 9012", "bpay_biller_code_0": "12345",
            "bpay_ref_0": "9876 5432 1098"}
    assert stocked.post("/local/import/1", data=data, headers=ORIGIN).status_code == 303
    saved = rows(admin)[0]
    assert (saved["gst_amount"], saved["card_payment_ref"], saved["bpay_biller_code"], saved["bpay_ref"]) == (
        D("18.18"), "1234 5678 9012", "12345", "9876 5432 1098")
