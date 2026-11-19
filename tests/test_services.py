"""Service items: the Service category, its editable list of items, choosing them per expense (the Add and
Edit form, the import row), adding one from the import page, and the view Grafana reads."""

import pytest

from garagebooks import invoices
from tests.test_app import ORIGIN, category_id, form, rows
from tests.test_dev_stack import ROOT
from tests.test_local import app_for, choose, client, root  # noqa: F401  (fixtures)

SEED = ["Fuel filter", "Oil filter", "Engine oil", "Tyre rotation", "Windscreen washer fluid", "Windscreen wipers",
        "Brakes", "Brake fluid", "Timing belt", "Air filter", "Coolant", "Cabin air filter"]


def items(admin):
    return {r["name"]: r["id"] for r in admin.execute("SELECT id, name FROM garagebooks.service_item")}


def test_the_service_category_and_its_items_are_seeded(admin):
    assert category_id(admin, "Service")
    assert sorted(items(admin)) == sorted(SEED)


def test_the_schema_can_run_again_without_bringing_back_a_removed_item(admin):
    admin.execute("DELETE FROM garagebooks.service_item WHERE name = 'Coolant'")
    try:
        admin.execute((ROOT / "garagebooks" / "schema.sql").read_text())
        assert "Coolant" not in items(admin) and len(items(admin)) == len(SEED) - 1
        assert admin.execute("SELECT count(*) AS n FROM garagebooks.category WHERE name = 'Service'").fetchone()["n"] == 1
    finally:
        admin.execute("INSERT INTO garagebooks.service_item (name, sort_order) VALUES ('Coolant', 120) ON CONFLICT DO NOTHING")


def test_the_categories_page_lists_the_items_and_edits_them(client, admin):  # noqa: F811
    page = client.get("/categories").data.decode()
    assert "Windscreen wipers" in page and "/categories/services" in page
    assert client.post("/categories/services", data={"name": "Spark plugs"}, headers=ORIGIN).status_code == 303
    assert "Spark plugs" in items(admin)
    # a name that exists (any case) or is blank is refused, and says why
    for bad in ("spark PLUGS", "  "):
        resp = client.post("/categories/services", data={"name": bad}, headers=ORIGIN)
        assert resp.status_code == 400 and b"error" in resp.data
    item = items(admin)["Spark plugs"]
    assert client.post(f"/categories/services/{item}/rename", data={"name": "Spark plug set"},
                       headers=ORIGIN).status_code == 303
    assert "Spark plug set" in items(admin) and "Spark plugs" not in items(admin)
    assert client.post(f"/categories/services/{item}/rename", data={"name": "Brakes"}, headers=ORIGIN).status_code == 400
    assert client.post(f"/categories/services/{item}/delete", headers=ORIGIN).status_code == 303
    assert "Spark plug set" not in items(admin)
    assert client.post("/categories/services/99999/delete", headers=ORIGIN).status_code == 404
    assert client.post("/categories/services/99999/rename", data={"name": "X"}, headers=ORIGIN).status_code == 404


def service_form(admin, *names, **over):
    ids = items(admin)
    return {**form(admin, category_id=str(category_id(admin, "Service")), **over),
            "service_item": [str(ids[n]) for n in names]}


def linked(admin, expense_id):
    return sorted(r["name"] for r in admin.execute(
        "SELECT i.name FROM garagebooks.expense_service_item l JOIN garagebooks.service_item i ON i.id = l.item_id "
        "WHERE l.expense_id = %s", (expense_id,)))


def test_the_form_offers_the_items_and_saves_the_ones_chosen(client, admin):  # noqa: F811
    page = client.get("/new").data.decode()
    assert 'name="service_item"' in page and "Windscreen washer fluid" in page
    client.post("/new", data=service_form(admin, "Oil filter", "Engine oil"), headers=ORIGIN)
    expense = rows(admin)[0]
    assert linked(admin, expense["id"]) == ["Engine oil", "Oil filter"]
    edit = client.get(f"/{expense['id']}/edit").data.decode()
    assert edit.count(" checked") >= 2 and f'value="{items(admin)["Oil filter"]}" checked' in edit
    client.post(f"/{expense['id']}/edit", data=service_form(admin, "Brakes"), headers=ORIGIN)
    assert linked(admin, expense["id"]) == ["Brakes"]


def test_items_chosen_for_another_category_are_not_kept(client, admin):  # noqa: F811
    data = service_form(admin, "Brakes")
    data["category_id"] = str(category_id(admin, "Tyres"))
    client.post("/new", data=data, headers=ORIGIN)
    assert linked(admin, rows(admin)[0]["id"]) == []


def test_removing_an_item_unlinks_it_and_renaming_keeps_the_link(client, admin):  # noqa: F811
    client.post("/new", data=service_form(admin, "Brakes", "Coolant"), headers=ORIGIN)
    expense_id = rows(admin)[0]["id"]
    ids = items(admin)
    try:
        client.post(f"/categories/services/{ids['Brakes']}/rename", data={"name": "Brake pads"}, headers=ORIGIN)
        client.post(f"/categories/services/{ids['Coolant']}/delete", headers=ORIGIN)
        assert linked(admin, expense_id) == ["Brake pads"]
    finally:                  # the seeded items are shared by every test
        admin.execute("INSERT INTO garagebooks.service_item (name, sort_order) VALUES ('Coolant', 110) ON CONFLICT DO NOTHING")
        admin.execute("UPDATE garagebooks.service_item SET name = 'Brakes' WHERE name = 'Brake pads'")


def test_the_list_shows_the_items(client, admin):  # noqa: F811
    client.post("/new", data=service_form(admin, "Timing belt"), headers=ORIGIN)
    assert "Timing belt" in client.get("/").data.decode()


def test_grafana_can_read_the_items_from_a_view(client, admin):  # noqa: F811
    client.post("/new", data=service_form(admin, "Oil filter", "Engine oil", vendor="Example Garage"), headers=ORIGIN)
    got = admin.execute("SELECT item, vendor, car_id FROM garagebooks.expense_service ORDER BY item").fetchall()
    assert [(r["item"], r["vendor"], r["car_id"]) for r in got] == [("Engine oil", "Example Garage", 1),
                                                                    ("Oil filter", "Example Garage", 1)]


# ---- reading the items in a PDF, and the import row -----------------------------------------

NAMES = ["Fuel filter", "Oil filter", "Engine oil", "Air filter", "Cabin air filter", "Brakes", "Tyre rotation"]


def test_items_named_in_the_text_are_found_longest_first():
    text = "TAX INVOICE\nReplace OIL FILTER\nEngine oil 5L\nCabin air filter\nBrakes inspected\n"
    assert invoices.service_items_in(text, NAMES) == ["Oil filter", "Engine oil", "Cabin air filter", "Brakes"]
    # 'Cabin air filter' doesn't also tick 'Air filter'; a part of a word isn't a match
    assert invoices.service_items_in("Air filter changed\n", NAMES) == ["Air filter"]
    assert invoices.service_items_in("Brakeswear and oilfilters", NAMES) == []
    assert invoices.service_items_in("", NAMES) == []


SERVICE = ("TAX INVOICE\nExample Garage Pty Ltd\nInvoice Date: 24/10/2026\nOil filter replaced\nEngine oil 5L\n"
           "Service\nTotal   $300.00\n")


@pytest.fixture()
def stocked(client, root, monkeypatch):  # noqa: F811
    car = root / "Cars" / "Test car"
    (car / "service.pdf").write_bytes(b"%PDF-service")
    monkeypatch.setattr(invoices, "extract_text", lambda pdf: SERVICE)
    choose(client)
    return client


def read(client):
    path = "/Cars/Test car/service.pdf"
    return client.post("/local/import/1/read", headers=ORIGIN, data={
        "n": "0", "id": path, "url": path, "name": "service.pdf", "folder": "Service"}).data.decode()


def test_the_import_row_offers_the_items_and_ticks_those_in_the_pdf(stocked, admin):
    row = read(stocked)
    ids = items(admin)
    assert f'name="service_item_0" value="{ids["Oil filter"]}" checked' in row
    assert f'name="service_item_0" value="{ids["Engine oil"]}" checked' in row
    assert f'name="service_item_0" value="{ids["Brakes"]}" checked' not in row and f'value="{ids["Brakes"]}"' in row


def test_confirming_an_import_row_saves_its_items(stocked, admin):
    ids = items(admin)
    path = "/Cars/Test car/service.pdf"
    data = {"count": "1", "include_0": "on", "id_0": path, "url_0": path, "name_0": "service.pdf",
            "date_0": "2026-10-24", "category_id_0": str(category_id(admin, "Service")), "amount_0": "300.00",
            "vendor_0": "Example Garage", "service_item_0": [str(ids["Oil filter"]), str(ids["Brakes"])]}
    assert stocked.post("/local/import/1", data=data, headers=ORIGIN).status_code == 303
    assert linked(admin, rows(admin)[0]["id"]) == ["Brakes", "Oil filter"]


def test_a_bad_item_id_is_ignored(stocked, admin):
    path = "/Cars/Test car/service.pdf"
    data = {"count": "1", "include_0": "on", "id_0": path, "url_0": path, "name_0": "service.pdf",
            "date_0": "2026-10-24", "category_id_0": str(category_id(admin, "Service")), "amount_0": "300.00",
            "vendor_0": "Example Garage", "service_item_0": ["99999", "abc"]}
    assert stocked.post("/local/import/1", data=data, headers=ORIGIN).status_code == 303
    assert linked(admin, rows(admin)[0]["id"]) == []


def test_the_import_page_has_a_dialog_to_add_an_item_without_losing_progress(stocked):
    page = stocked.get("/local/import/1?sync=1").data.decode()
    assert '<dialog id="service-dialog"' in page and 'data-service-url="/api/service-items"' in page
    assert "<script>" not in page          # the CSP forbids inline scripts


def test_the_json_endpoint_adds_an_item(client, admin):  # noqa: F811
    resp = client.post("/api/service-items", json={"name": "  Wheel   bearings "}, headers=ORIGIN)
    assert resp.status_code == 201
    got = resp.get_json()
    assert got["name"] == "Wheel bearings" and got["id"] == items(admin)["Wheel bearings"]
    again = client.post("/api/service-items", json={"name": "wheel bearings"}, headers=ORIGIN)
    assert again.status_code == 200 and again.get_json() == {**got, "existing": True}
    assert client.post("/api/service-items", json={"name": ""}, headers=ORIGIN).status_code == 400
    assert client.post("/api/service-items", json={"name": "x" * 200}, headers=ORIGIN).status_code == 400
    assert client.post("/api/service-items", json={"name": "Foo"}).status_code == 403          # needs the site's own Origin
