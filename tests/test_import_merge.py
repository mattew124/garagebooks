"""Several PDFs merged into one expense on the import page: an invoice plus its service report. The main PDF is the
expense's invoice; the others are kept as its documents, count as known, and are listed on its edit page."""

import psycopg
import pytest

from garagebooks import db, invoices
from tests.test_app import ORIGIN, category_id, rows
from tests.test_local import app_for, choose, client, root  # noqa: F401  (fixtures)

INVOICE = "TAX INVOICE\nExample Garage Pty Ltd\nInvoice Date: 24/10/2026\nTotal   $300.00\n"
REPORT = "Service report\nOil filter replaced\n"
TEXT = {b"%PDF-invoice": INVOICE, b"%PDF-report": REPORT, b"%PDF-extra": "Photos\n"}
FILES = {"invoice.pdf": b"%PDF-invoice", "report.pdf": b"%PDF-report", "extra.pdf": b"%PDF-extra"}


def path(name):
    return f"/Cars/Test car/{name}"


@pytest.fixture()
def stocked(client, root, monkeypatch):  # noqa: F811
    for name, content in FILES.items():
        (root / "Cars" / "Test car" / name).write_bytes(content)
    monkeypatch.setattr(invoices, "extract_text", lambda pdf: TEXT[pdf])
    choose(client)
    return client


def post(client, admin, files=("invoice.pdf", "report.pdf", "extra.pdf"), **over):
    """The import form for `files` in that order: row 0 is a ticked invoice, the rest are untouched unless `over` says."""
    data = {"count": str(len(files))}
    for n, name in enumerate(files):
        data.update({f"id_{n}": path(name), f"url_{n}": path(name), f"name_{n}": name})
    data.update({"include_0": "on", "date_0": "2026-10-24", "category_id_0": str(category_id(admin, "Service")),
                 "amount_0": "300.00", "vendor_0": "Example Garage Pty Ltd"})
    data.update(over)
    return client.post("/local/import/1", data=data, headers=ORIGIN)


def documents(admin):
    return admin.execute("SELECT * FROM garagebooks.expense_document ORDER BY id").fetchall()


def test_the_table_takes_a_link_or_a_path_and_goes_with_its_expense(admin):
    eid = admin.execute("INSERT INTO garagebooks.expense (car_id, category_id, occurred_at, amount) VALUES (1, 1, now(), 1) "
                        "RETURNING id").fetchone()["id"]
    admin.execute("INSERT INTO garagebooks.expense_document (expense_id, name, invoice_path) VALUES (%s, 'a.pdf', '/a.pdf')", (eid,))
    admin.execute("INSERT INTO garagebooks.expense_document (expense_id, name, invoice_url) VALUES (%s, 'b.pdf', 'https://x.example/b')", (eid,))
    for url, path_ in ((None, None), ("https://x.example/c", "/c.pdf"), ("http://x.example/c", None), (None, "no-slash.pdf")):
        with pytest.raises(psycopg.errors.CheckViolation):
            admin.execute("INSERT INTO garagebooks.expense_document (expense_id, name, invoice_url, invoice_path) "
                          "VALUES (%s, 'c.pdf', %s, %s)", (eid, url, path_))
    admin.execute("DELETE FROM garagebooks.expense WHERE id = %s", (eid,))
    assert documents(admin) == []


def test_known_invoices_include_attached_documents(admin):
    eid = admin.execute("INSERT INTO garagebooks.expense (car_id, category_id, occurred_at, amount, invoice_path) "
                        "VALUES (1, 1, now(), 1, '/main.pdf') RETURNING id").fetchone()["id"]
    admin.execute("INSERT INTO garagebooks.expense_document (expense_id, name, invoice_path) VALUES (%s, 'a.pdf', '/a.pdf')", (eid,))
    admin.execute("INSERT INTO garagebooks.expense_document (expense_id, name, invoice_url) VALUES (%s, 'b.pdf', 'https://x.example/b')", (eid,))
    assert db.known_invoices(admin, ["/main.pdf", "/a.pdf", "https://x.example/b", "/other.pdf"]) == {
        "/main.pdf", "/a.pdf", "https://x.example/b"}


def test_each_row_can_be_attached_to_another(stocked):
    row = stocked.post("/local/import/1/read", headers=ORIGIN, data={
        "n": "1", "id": path("report.pdf"), "url": path("report.pdf"), "name": "report.pdf", "folder": ""}).data.decode()
    assert '<select name="attach_1"' in row and "Attach to" in row


def test_attached_pdfs_become_documents_of_one_expense(stocked, admin):
    assert post(stocked, admin, attach_1="0", attach_2="0").status_code == 303
    [expense] = rows(admin)
    assert expense["invoice_path"] == path("invoice.pdf")
    assert [(d["expense_id"], d["name"], d["invoice_path"], d["invoice_url"]) for d in documents(admin)] == [
        (expense["id"], "report.pdf", path("report.pdf"), None), (expense["id"], "extra.pdf", path("extra.pdf"), None)]


def test_merged_files_are_never_offered_again(stocked, admin):
    post(stocked, admin, attach_1="0")
    scan = stocked.get("/local/import/1/scan").get_json()
    assert [f["name"] for f in scan["files"]] == ["extra.pdf"] and scan["already_imported"] == 2


def test_an_attachment_follows_a_row_that_is_itself_attached(stocked, admin):
    post(stocked, admin, attach_1="0", attach_2="1")
    assert len(rows(admin)) == 1 and [d["name"] for d in documents(admin)] == ["report.pdf", "extra.pdf"]


def test_a_row_attached_to_one_that_is_not_ticked_is_refused(stocked, admin):
    resp = post(stocked, admin, include_0="", attach_1="0")
    assert resp.status_code == 400 and "isn&#39;t ticked" in resp.data.decode()
    assert rows(admin) == [] and documents(admin) == []


def test_a_row_cant_be_attached_to_itself_or_in_a_loop_or_to_nothing(stocked, admin):
    for over in ({"attach_1": "1"}, {"attach_1": "2", "attach_2": "1"}, {"attach_1": "9"}, {"attach_1": "x"}):
        resp = post(stocked, admin, **over)
        assert resp.status_code == 400, over
        assert rows(admin) == [] and documents(admin) == []


def test_an_attached_row_is_not_imported_on_its_own(stocked, admin):
    post(stocked, admin, include_1="on", date_1="2026-10-24", category_id_1=str(category_id(admin, "Service")),
         amount_1="1.00", attach_1="0")
    assert len(rows(admin)) == 1 and len(documents(admin)) == 1


def test_a_double_submit_attaches_once(stocked, admin):
    post(stocked, admin, attach_1="0")
    post(stocked, admin, attach_1="0")
    assert len(rows(admin)) == 1 and len(documents(admin)) == 1


def test_the_edit_page_lists_the_documents_with_links_and_serves_them(stocked, admin):
    post(stocked, admin, attach_1="0")
    [expense] = rows(admin)
    [document] = documents(admin)
    page = stocked.get(f"/{expense['id']}/edit").data.decode()
    assert "Attached documents" in page and "report.pdf" in page and f'href="/document/{document["id"]}"' in page
    assert stocked.get(f"/document/{document['id']}").data == b"%PDF-report"
    assert stocked.get("/document/99999").status_code == 404


def test_a_document_can_be_removed_and_is_then_offered_again(stocked, admin):
    post(stocked, admin, attach_1="0")
    [expense] = rows(admin)
    [document] = documents(admin)
    assert stocked.post(f"/{expense['id']}/documents/{document['id']}/remove", headers=ORIGIN).status_code == 303
    assert documents(admin) == []
    names = [f["name"] for f in stocked.get("/local/import/1/scan").get_json()["files"]]
    assert "report.pdf" in names and len(rows(admin)) == 1
    assert stocked.post(f"/{expense['id']}/documents/{document['id']}/remove", headers=ORIGIN).status_code == 404


def test_the_list_and_the_edit_page_of_an_expense_without_documents_are_unchanged(stocked, admin):
    post(stocked, admin)
    [expense] = rows(admin)
    assert "Attached documents" not in stocked.get(f"/{expense['id']}/edit").data.decode()
