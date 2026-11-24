"""The Categories page: every category and service item is edited in one form with one Save button, which saves
every changed row together or nothing at all; deleting a service item is a small x button on its row."""

import re

import pytest

from tests.test_app import ORIGIN, category_id, client  # noqa: F401  (fixture)
from tests.test_local import app_for, root  # noqa: F401  (fixtures)
from tests.test_services import items


@pytest.fixture(autouse=True)
def keep_categories(admin):
    """Tests edit the seeded categories and items; put them back."""
    cats = admin.execute("SELECT id, upload_folder, gst_free, has_period, budget_pool FROM garagebooks.category").fetchall()
    names = admin.execute("SELECT id, name, sort_order FROM garagebooks.service_item").fetchall()
    yield
    for c in cats:
        admin.execute("UPDATE garagebooks.category SET upload_folder = %s, gst_free = %s, has_period = %s, budget_pool = %s "
                      "WHERE id = %s", (c["upload_folder"], c["gst_free"], c["has_period"], c["budget_pool"], c["id"]))
    admin.execute("DELETE FROM garagebooks.service_item WHERE name LIKE 'Test %%'")
    admin.execute("UPDATE garagebooks.service_item SET name = '~' || id")
    for n in names:                    # renamed back, and a deleted one is put back (its expense links are not)
        admin.execute("INSERT INTO garagebooks.service_item (id, name, sort_order) VALUES (%(id)s, %(name)s, %(sort_order)s) "
                      "ON CONFLICT (id) DO UPDATE SET name = excluded.name", n)


def save(client, admin, categories=("Tyres",), item_names=None, **fields):
    """Post the bulk form for these categories (by name) and these items ({old name: new name}). `fields` are
    extra form fields by their full name."""
    data = {"category_id": [str(category_id(admin, n)) for n in categories], **fields}
    ids = items(admin)
    if item_names:
        data["item_id"] = [str(ids[n]) for n in item_names]
        for old, new in item_names.items():
            data[f"item_name_{ids[old]}"] = new
    return client.post("/categories/save", data=data, headers=ORIGIN)


def category_row(admin, name):
    return admin.execute("SELECT * FROM garagebooks.category WHERE name = %s", (name,)).fetchone()


def test_the_page_is_one_form_with_one_save_button(client):  # noqa: F811
    page = client.get("/categories").data.decode()
    assert page.count('action="/categories/save"') == 1
    form = page[page.index('action="/categories/save"'):]
    form = form[:form.index("</form>")]
    assert len(re.findall(r'<button type="submit"[^>]*>\s*Save', form)) == 1
    for old in ("/gst_free", "/has_period", "/pool", "/folder", "/rename"):
        assert old not in page


def test_every_changed_row_is_saved_together(client, admin):  # noqa: F811
    tyres, service = category_id(admin, "Tyres"), category_id(admin, "Service")
    ids = items(admin)
    resp = client.post("/categories/save", data={
        "category_id": [str(tyres), str(service)],
        f"gst_partial_{tyres}": "1", f"has_period_{service}": "1",
        "item_id": [str(ids["Brakes"]), str(ids["Coolant"])],
        f"item_name_{ids['Brakes']}": "Brake pads", f"item_name_{ids['Coolant']}": "Coolant",
    }, headers=ORIGIN)
    assert resp.status_code == 303
    assert category_row(admin, "Tyres")["gst_free"] and not category_row(admin, "Service")["gst_free"]
    assert category_row(admin, "Service")["has_period"] and not category_row(admin, "Tyres")["has_period"]
    assert "Brake pads" in items(admin) and "Brakes" not in items(admin)


def test_a_row_left_alone_is_not_written(client, admin):  # noqa: F811
    before = admin.execute("SELECT count(*) AS n FROM garagebooks.change_log").fetchone()["n"]
    assert save(client, admin, ("Tyres", "Service"), {"Brakes": "Brakes"}).status_code == 303
    assert admin.execute("SELECT count(*) AS n FROM garagebooks.change_log").fetchone()["n"] == before


def test_one_invalid_row_saves_nothing_and_says_which(app_for, admin):  # noqa: F811
    client = app_for()                                                   # the invoice folder is on, so the box is shown
    tyres, service = category_id(admin, "Tyres"), category_id(admin, "Service")
    brakes = items(admin)["Brakes"]
    resp = client.post("/categories/save", data={
        "category_id": [str(tyres), str(service)], f"gst_partial_{tyres}": "1",
        f"upload_folder_{service}": "bad/name", "item_id": [str(brakes)], f"item_name_{brakes}": "Brake pads",
    }, headers=ORIGIN)
    assert resp.status_code == 400
    page = resp.data.decode()
    row = re.search(rf'<li[^>]*id="category-{service}".*?</li>', page, re.S).group(0)
    assert 'class="error"' in row and "bad/name" in row                  # next to the row, and what was typed is kept
    assert not category_row(admin, "Tyres")["gst_free"]                  # the valid rows were not saved either
    assert "Brake pads" not in items(admin)
    assert "Brake pads" in page                                          # but still shown for the person to fix


def test_a_service_item_name_is_checked_against_the_others_and_the_batch(client, admin):  # noqa: F811
    ids = items(admin)
    resp = save(client, admin, (), {"Brakes": "coolant"})
    assert resp.status_code == 400 and f'id="item-{ids["Brakes"]}"' in resp.data.decode()
    resp = save(client, admin, (), {"Brakes": "Test same", "Coolant": "test SAME"})
    assert resp.status_code == 400 and items(admin).keys() >= {"Brakes", "Coolant"}
    assert save(client, admin, (), {"Brakes": "  "}).status_code == 400


def test_two_items_can_swap_names(client, admin):  # noqa: F811
    ids = items(admin)
    assert save(client, admin, (), {"Brakes": "Coolant", "Coolant": "Brakes"}).status_code == 303
    now = {v: k for k, v in items(admin).items()}
    assert now[ids["Brakes"]] == "Coolant" and now[ids["Coolant"]] == "Brakes"


def test_stale_ids_and_junk_are_ignored_not_a_crash(client, admin):  # noqa: F811
    resp = client.post("/categories/save", data={"category_id": ["99999", "x"], "item_id": ["99999", ""],
                                                 "item_name_99999": "Test x"}, headers=ORIGIN)
    assert resp.status_code == 303


def test_a_service_item_is_deleted_with_a_small_labelled_button(client, admin):  # noqa: F811
    item = items(admin)["Brakes"]
    page = client.get("/categories").data.decode()
    assert not re.search(r">\s*Remove\s*<", page)
    button = re.search(r'<button[^>]*aria-label="Delete Brakes"[^>]*>', page).group(0)
    assert f'form="remove-item-{item}"' in button and "icon-button" in button
    assert re.search(rf'<form[^>]*id="remove-item-{item}"[^>]*action="/categories/services/{item}/delete"[^>]*data-confirm=', page)
    assert client.post(f"/categories/services/{item}/delete", headers=ORIGIN).status_code == 303
    assert "Brakes" not in items(admin)


def test_the_gst_option_says_it_only_partly_applies(client):  # noqa: F811
    page = client.get("/categories").data.decode()
    assert "GST only partly applies" in page and "GST-free" not in page
