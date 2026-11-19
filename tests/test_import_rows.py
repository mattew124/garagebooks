"""Import rows (notes, periods, skipping what isn't an invoice) and the expense's period.

Uses the invoice folder on the server as the source, as test_local does: the row code is shared by
every source, so one is enough. PDF text is canned.
"""

import pytest

from garagebooks import invoices
from tests.test_app import ORIGIN, category_id, form, rows
from tests.test_local import app_for, choose, client, root  # noqa: F401  (fixtures)

BROCHURE = "Our new range\nCome and see the showroom\n"
TAX = "TAX INVOICE\nExample Tyres Pty Ltd\nInvoice Date: 24/10/2026\n"
POLICY = ("Period of insurance: from 24/10/2026 to 24/10/2027\nPolicy change: change of address\n"
          "Example Insurer Limited\nTotal   $20.00\n")
TEXT = {b"%PDF-brochure": BROCHURE, b"%PDF-tax": TAX, b"%PDF-policy": POLICY}


@pytest.fixture()
def stocked(client, root, monkeypatch):  # noqa: F811
    car = root / "Cars" / "Test car"
    (car / "brochure.pdf").write_bytes(b"%PDF-brochure")
    (car / "tax.pdf").write_bytes(b"%PDF-tax")
    (car / "policy.pdf").write_bytes(b"%PDF-policy")
    monkeypatch.setattr(invoices, "extract_text", lambda pdf: TEXT[pdf])
    choose(client)
    return client


def read(client, name, n=0):
    path = f"/Cars/Test car/{name}"
    return client.post("/local/import/1/read", headers=ORIGIN, data={
        "n": str(n), "id": path, "url": path, "name": name, "folder": ""}).data.decode()


def test_a_row_that_is_not_an_invoice_starts_skipped_and_unticked(stocked):
    row = read(stocked, "brochure.pdf")
    assert 'name="skip_0" checked' in row and 'name="include_0" checked' not in row
    assert "Doesn&#39;t look like an invoice" in row


def test_a_row_that_looks_like_an_invoice_but_misses_a_field_is_unticked_not_skipped(stocked):
    row = read(stocked, "tax.pdf")
    assert 'name="skip_0" checked' not in row and 'name="include_0" checked' not in row


def test_the_row_shows_notes_and_the_period_read_from_the_document(stocked, admin):
    row = read(stocked, "policy.pdf")
    assert 'name="notes_0"' in row and "Change of address; policy change" in row
    assert 'name="period_start_0" value="2026-10-24"' in row and 'name="period_end_0" value="2027-10-24"' in row


def test_the_period_is_shown_only_for_a_category_that_has_one(stocked, admin):
    row = read(stocked, "policy.pdf")
    assert f'<option value="{category_id(admin, "Insurance")}" data-period="1"' in row
    assert f'<option value="{category_id(admin, "Tyres")}" data-period="0"' in row


def confirm(client, admin, **over):
    path = "/Cars/Test car/policy.pdf"
    data = {"count": "1", "include_0": "on", "id_0": path, "url_0": path, "name_0": "policy.pdf",
            "date_0": "2026-10-24", "category_id_0": str(category_id(admin, "Insurance")), "amount_0": "20.00",
            "vendor_0": "Example Insurer Limited", "notes_0": "Change of address",
            "period_start_0": "2026-10-24", "period_end_0": "2027-10-24"}
    data.update(over)
    return client.post("/local/import/1", data=data, headers=ORIGIN)


def test_confirming_saves_the_notes_and_the_period(stocked, admin):
    assert confirm(stocked, admin).status_code == 303
    row = rows(admin)[0]
    assert row["notes"] == "Change of address"
    assert str(row["period_start"]) == "2026-10-24" and str(row["period_end"]) == "2027-10-24"


def test_a_period_that_ends_before_it_starts_is_refused(stocked, admin):
    resp = confirm(stocked, admin, period_end_0="2026-01-01")
    assert resp.status_code == 400 and "can&#39;t be before" in resp.data.decode() and rows(admin) == []


def test_the_database_refuses_a_period_that_ends_before_it_starts(admin):
    import psycopg
    with pytest.raises(psycopg.errors.CheckViolation):
        admin.execute("INSERT INTO garagebooks.expense (car_id, category_id, occurred_at, amount, period_start, period_end)"
                      " VALUES (1, 1, now(), 1, '2026-02-01', '2026-01-01')")


def test_the_categories_that_have_a_period_by_default(admin):
    have = {r["name"] for r in admin.execute("SELECT name FROM garagebooks.category WHERE has_period")}
    assert have == {"Insurance", "Registration"}


def test_a_category_can_be_marked_as_having_a_period(client, admin):  # noqa: F811
    tyres = category_id(admin, "Tyres")
    assert "/has_period" in client.get("/categories").data.decode()
    assert client.post(f"/categories/{tyres}/has_period", data={"has_period": "1"}, headers=ORIGIN).status_code == 303
    assert admin.execute("SELECT has_period FROM garagebooks.category WHERE id = %s", (tyres,)).fetchone()["has_period"]
    client.post(f"/categories/{tyres}/has_period", data={}, headers=ORIGIN)
    assert not admin.execute("SELECT has_period FROM garagebooks.category WHERE id = %s", (tyres,)).fetchone()["has_period"]
    assert client.post("/categories/99999/has_period", data={"has_period": "1"}, headers=ORIGIN).status_code == 404


def test_the_add_and_edit_form_keep_the_period(client, admin):  # noqa: F811
    insurance = category_id(admin, "Insurance")
    page = client.get("/new").data.decode()
    assert 'name="period_start"' in page and f'<option value="{insurance}" data-period="1"' in page
    client.post("/new", data=form(admin, category_id=str(insurance), period_start="2026-11-01", period_end="2027-11-01"),
                headers=ORIGIN)
    expense = rows(admin)[0]
    edit = client.get(f"/{expense['id']}/edit").data.decode()
    assert 'name="period_start" value="2026-11-01"' in edit and 'name="period_end" value="2027-11-01"' in edit
    client.post(f"/{expense['id']}/edit", data=form(admin, category_id=str(insurance), period_start="2026-11-01",
                                                      period_end="2027-12-01"), headers=ORIGIN)
    assert str(rows(admin)[0]["period_end"]) == "2027-12-01"


def test_the_list_shows_the_period(client, admin):  # noqa: F811
    insurance = category_id(admin, "Insurance")
    client.post("/new", data=form(admin, category_id=str(insurance), period_start="2026-11-01", period_end="2027-11-01"),
                headers=ORIGIN)
    page = client.get("/").data.decode()
    assert "01 Nov 2026" in page and "01 Nov 2027" in page
