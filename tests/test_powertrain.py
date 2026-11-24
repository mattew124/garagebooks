"""Categories and service items that only apply to some cars: fuel and engine items to cars with an engine,
charging to cars that plug in. The Categories page sets it; the expense form, the import page and the service
item ticks only offer what applies to the car, and the server refuses the rest. Expenses that already break the
rule stay as they are."""

import re

import pytest

from garagebooks import db, invoices
from tests.test_app import ORIGIN, category_id, client, form, rows  # noqa: F401  (fixture)
from tests.test_dev_stack import ROOT
from tests.test_local import app_for, choose, root  # noqa: F401  (fixtures)
from tests.test_services import items


@pytest.fixture(autouse=True)
def keep_applies(admin):
    """Tests edit the seeded settings; put them back."""
    cats = admin.execute("SELECT id, applies_to FROM garagebooks.category").fetchall()
    its = admin.execute("SELECT id, applies_to FROM garagebooks.service_item").fetchall()
    yield
    for c in cats:
        admin.execute("UPDATE garagebooks.category SET applies_to = %s WHERE id = %s", (c["applies_to"], c["id"]))
    for i in its:
        admin.execute("UPDATE garagebooks.service_item SET applies_to = %s WHERE id = %s", (i["applies_to"], i["id"]))


def set_kind(admin, kind, car_id=1):
    admin.execute("UPDATE garagebooks.car SET kind = %s WHERE id = %s", (kind, car_id))


def applies(admin, table):
    return {r["name"]: r["applies_to"] for r in admin.execute(f"SELECT name, applies_to FROM garagebooks.{table}")}


def test_the_built_in_ones_have_sensible_defaults(admin):
    cats = applies(admin, "category")
    assert cats["Fuel"] == "combustion" and cats["Charging"] == "electric"
    assert {cats[n] for n in ("Insurance", "Registration", "Tyres", "Service", "Initial purchase")} == {"all"}
    its = applies(admin, "service_item")
    assert {n for n, a in its.items() if a == "combustion"} == {"Fuel filter", "Oil filter", "Engine oil", "Timing belt",
                                                               "Air filter"}
    assert {a for n, a in its.items() if n not in ("Fuel filter", "Oil filter", "Engine oil", "Timing belt",
                                                   "Air filter")} == {"all"}


def test_running_the_schema_again_keeps_the_owners_choice(admin):
    admin.execute("UPDATE garagebooks.category SET applies_to = 'all' WHERE name = 'Fuel'")
    admin.execute("UPDATE garagebooks.service_item SET applies_to = 'all' WHERE name = 'Oil filter'")
    admin.execute((ROOT / "garagebooks" / "schema.sql").read_text())
    assert applies(admin, "category")["Fuel"] == "all" and applies(admin, "service_item")["Oil filter"] == "all"


@pytest.mark.parametrize("applies_to,kinds", [("all", {"BEV", "ICE", "PHEV", "HEV"}), ("electric", {"BEV", "PHEV"}),
                                              ("combustion", {"ICE", "PHEV", "HEV"})])
def test_which_cars_each_choice_applies_to(applies_to, kinds):
    assert {k for k in db.CAR_KINDS if db.applies(applies_to, k)} == kinds
    assert db.applies(applies_to, None)                   # no car chosen yet: everything is offered


def test_the_categories_page_sets_it(client, admin):  # noqa: F811
    tyres, oil = category_id(admin, "Tyres"), items(admin)["Oil filter"]
    page = client.get("/categories").data.decode()
    assert f'name="applies_to_{tyres}"' in page and f'name="item_applies_to_{oil}"' in page
    resp = client.post("/categories/save", data={"category_id": str(tyres), f"applies_to_{tyres}": "electric",
                                                 "item_id": str(oil), f"item_name_{oil}": "Oil filter",
                                                 f"item_applies_to_{oil}": "all"}, headers=ORIGIN)
    assert resp.status_code == 303
    assert applies(admin, "category")["Tyres"] == "electric" and applies(admin, "service_item")["Oil filter"] == "all"
    bad = client.post("/categories/save", data={"category_id": str(tyres), f"applies_to_{tyres}": "diesel"}, headers=ORIGIN)
    assert bad.status_code == 400 and applies(admin, "category")["Tyres"] == "electric"


def test_the_expense_form_refuses_what_doesnt_apply_to_the_car(client, admin):  # noqa: F811
    fuel, charging = category_id(admin, "Fuel"), category_id(admin, "Charging")
    resp = client.post("/new", data=form(admin, category_id=str(fuel)), headers=ORIGIN)       # car 1 is a BEV
    assert resp.status_code == 400 and "doesn't apply" in resp.data.decode() and rows(admin) == []
    set_kind(admin, "ICE")
    assert client.post("/new", data=form(admin, category_id=str(charging)), headers=ORIGIN).status_code == 400
    assert client.post("/new", data=form(admin, category_id=str(fuel)), headers=ORIGIN).status_code == 303
    set_kind(admin, "PHEV")
    assert client.post("/new", data=form(admin, category_id=str(charging), amount="20"), headers=ORIGIN).status_code == 303
    set_kind(admin, "HEV")
    assert client.post("/new", data=form(admin, category_id=str(charging), amount="30"), headers=ORIGIN).status_code == 400


def test_the_form_marks_each_choice_with_the_cars_it_applies_to(client, admin):  # noqa: F811
    page = client.get("/new").data.decode()
    fuel = category_id(admin, "Fuel")
    assert re.search(rf'<option value="{fuel}"[^>]*data-applies="combustion"', page)
    assert re.search(r'<option value="1"[^>]*data-kind="BEV"', page)
    assert re.search(rf'<option value="{fuel}"[^>]*hidden disabled', page)         # the only car is a BEV: not offered
    oil = items(admin)["Oil filter"]
    assert re.search(rf'<label class="inline"[^>]*data-applies="combustion"[^>]*hidden><input type="checkbox" name="service_item" value="{oil}"', page)


def test_service_items_that_dont_apply_are_refused(client, admin):  # noqa: F811
    ids = items(admin)
    service = str(category_id(admin, "Service"))
    data = {**form(admin, category_id=service), "service_item": [str(ids["Oil filter"]), str(ids["Brakes"])]}
    resp = client.post("/new", data=data, headers=ORIGIN)
    assert resp.status_code == 400 and "Oil filter" in resp.data.decode() and rows(admin) == []
    set_kind(admin, "HEV")
    assert client.post("/new", data=data, headers=ORIGIN).status_code == 303


def test_an_expense_that_already_breaks_the_rule_stays_and_can_still_be_edited(client, admin):  # noqa: F811
    fuel, service = category_id(admin, "Fuel"), category_id(admin, "Service")
    ids = items(admin)
    one = admin.execute("INSERT INTO garagebooks.expense (car_id, category_id, occurred_at, amount) "
                        "VALUES (1, %s, '2026-11-01T10:00+10', 50) RETURNING id", (fuel,)).fetchone()["id"]
    two = admin.execute("INSERT INTO garagebooks.expense (car_id, category_id, occurred_at, amount) "
                        "VALUES (1, %s, '2026-11-02T10:00+10', 300) RETURNING id", (service,)).fetchone()["id"]
    admin.execute("INSERT INTO garagebooks.expense_service_item (expense_id, item_id) VALUES (%s, %s)", (two, ids["Oil filter"]))
    assert "Fuel" in client.get("/").data.decode()
    page = client.get(f"/{one}/edit").data.decode()
    assert re.search(rf'<option value="{fuel}"[^>]*selected', page) and not re.search(rf'<option value="{fuel}"[^>]*hidden', page)
    assert client.post(f"/{one}/edit", data=form(admin, category_id=str(fuel), amount="55"), headers=ORIGIN).status_code == 303
    edit = client.get(f"/{two}/edit").data.decode()
    assert f'value="{ids["Oil filter"]}" checked' in edit
    data = {**form(admin, category_id=str(service), amount="300"), "service_item": [str(ids["Oil filter"])]}
    assert client.post(f"/{two}/edit", data=data, headers=ORIGIN).status_code == 303
    assert [r["name"] for r in admin.execute(
        "SELECT i.name FROM garagebooks.expense_service_item l JOIN garagebooks.service_item i ON i.id = l.item_id "
        "WHERE l.expense_id = %s", (two,))] == ["Oil filter"]
    # but moving it to another car it doesn't apply to is refused like a new one
    admin.execute("INSERT INTO garagebooks.car (id, name, kind) VALUES (2, 'Other EV', 'BEV')")
    assert client.post(f"/{one}/edit", data=form(admin, car_id="2", category_id=str(fuel)), headers=ORIGIN).status_code == 400


TEXT = "TAX INVOICE\nExample Garage Pty Ltd\nInvoice Date: 24/10/2026\nUnleaded 40L\nTotal   $80.00\n"


@pytest.fixture()
def stocked(app_for, root, monkeypatch):  # noqa: F811
    client = app_for()
    car = root / "Cars" / "Test car"
    (car / "fuel.pdf").write_bytes(b"%PDF-fuel")
    monkeypatch.setattr(invoices, "extract_text", lambda pdf: TEXT)
    choose(client)
    return client


def test_the_import_row_only_offers_what_applies_and_refuses_the_rest(stocked, admin):
    fuel, charging = category_id(admin, "Fuel"), category_id(admin, "Charging")
    path = "/Cars/Test car/fuel.pdf"
    row = stocked.post("/local/import/1/read", headers=ORIGIN, data={
        "n": "0", "id": path, "url": path, "name": "fuel.pdf", "folder": ""}).data.decode()
    assert re.search(rf'<option value="{fuel}"[^>]*hidden disabled', row)
    assert not re.search(rf'<option value="{charging}"[^>]*hidden', row)
    resp = stocked.post("/local/import/1", headers=ORIGIN, data={
        "count": "1", "include_0": "on", "date_0": "2026-10-24", "category_id_0": str(fuel), "amount_0": "80.00",
        "vendor_0": "Example Garage", "url_0": path, "id_0": path, "name_0": "fuel.pdf"})
    assert resp.status_code == 400 and "doesn't apply" in resp.data.decode() and rows(admin) == []
