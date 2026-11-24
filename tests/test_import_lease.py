"""The import offers a lease form (or agreement) PDF as a lease document, never as an expense, and never renames it.

Uses the invoice folder on the server as the source, as test_import_rows does. PDF text is canned: the lease
form is the invented fixture, the invoice is a plain tax invoice.
"""

from pathlib import Path

import pytest

from garagebooks import invoices
from tests.test_app import ORIGIN, rows
from tests.test_local import app_for, choose, client, root  # noqa: F401  (fixtures)

FORM = (Path(__file__).parent / "fixtures" / "associate-lease-form.txt").read_text()
AGREEMENT = "Associate Lease Agreement\nThis agreement is made between Example Packaging Pty Ltd and the lessee.\n"
TAX = "TAX INVOICE\nExample Tyres Pty Ltd\nInvoice Date: 24/10/2026\nTotal   $55.00\n"
TEXT = {b"%PDF-form": FORM, b"%PDF-agreement": AGREEMENT, b"%PDF-tax": TAX}
FORM_PATH = "/Cars/Test car/lease form.pdf"


@pytest.fixture()
def stocked(client, root, monkeypatch):  # noqa: F811
    car = root / "Cars" / "Test car"
    (car / "lease form.pdf").write_bytes(b"%PDF-form")
    (car / "lease agreement.pdf").write_bytes(b"%PDF-agreement")
    (car / "tax.pdf").write_bytes(b"%PDF-tax")
    monkeypatch.setattr(invoices, "extract_text", lambda pdf: TEXT[pdf])
    choose(client)
    return client


def read(client, name, n=0):
    path = f"/Cars/Test car/{name}"
    return client.post("/local/import/1/read", headers=ORIGIN, data={
        "n": str(n), "id": path, "url": path, "name": name, "folder": ""}).data.decode()


def add_lease(admin, start="2026-07-01"):
    admin.execute("INSERT INTO garagebooks.lease (car_id, financier, start_date, end_date, term_months, pay_frequency)"
                  f" VALUES (1, 'F', '{start}', '2028-06-30', 24, 'weekly')")


def lease_row(admin):
    return admin.execute("SELECT * FROM garagebooks.lease WHERE car_id = 1").fetchone()


def test_a_lease_form_matching_a_lease_is_offered_to_attach_and_not_as_an_expense(stocked, admin):
    add_lease(admin)
    row = read(stocked, "lease form.pdf")
    assert 'name="lease_0" checked' in row and "Lease form" in row and "for the lease from 1 Jul 2026" in row
    assert 'name="include_0" checked' not in row and 'name="skip_0" checked' not in row
    assert 'name="rename_0"' not in row          # nothing to rename: the file stays where and as it is


def test_a_lease_form_with_no_lease_offers_to_add_one_from_it(stocked, admin):
    row = read(stocked, "lease form.pdf")
    assert 'name="lease_0"' not in row and "Add a lease from this" in row and "/cars/1/lease/new" in row
    assert 'name="include_0" checked' not in row and 'name="rename_0"' not in row


def test_an_agreement_is_offered_as_the_lease_agreement(stocked, admin):
    add_lease(admin, start="2026-01-01")
    row = read(stocked, "lease agreement.pdf")
    assert "Lease agreement" in row and 'name="include_0" checked' not in row


def test_confirming_attaches_the_file_where_it_is_and_it_is_not_offered_again(stocked, admin, root):
    add_lease(admin)
    lease_id = lease_row(admin)["id"]
    resp = stocked.post("/local/import/1", headers=ORIGIN, data={
        "count": "1", "lease_0": "on", "lease_id_0": str(lease_id), "lease_doc_0": "form", "id_0": FORM_PATH, "url_0": FORM_PATH,
        "name_0": "lease form.pdf", "rename_0": "on"})        # even a stray rename is ignored for a lease document
    assert resp.status_code == 303
    assert lease_row(admin)["form_path"] == FORM_PATH
    assert (root / "Cars" / "Test car" / "lease form.pdf").exists()      # not renamed, not moved
    assert rows(admin) == []                                              # no expense made from it
    files = stocked.get("/local/import/1/scan").get_json()["files"]
    assert "lease form.pdf" not in [f["name"] for f in files]


def test_an_invoice_is_unchanged_and_renaming_is_off_by_default(stocked, admin):
    row = read(stocked, "tax.pdf")
    assert 'name="lease_0"' not in row and "Add a lease from this" not in row
    assert 'name="rename_0"' in row and 'name="rename_0" checked' not in row
