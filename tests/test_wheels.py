"""Which wheels a cost was for: on per-wheel service items (brakes) and per-wheel categories (tyres)."""

import re

import pytest

from garagebooks import acar, db, validation
from tests.test_acar_import import as_previewed, engine_car, upload  # noqa: F401  (fixtures)
from tests.test_app import ORIGIN, category_id, client, form, rows  # noqa: F401  (fixtures)


@pytest.mark.parametrize("raw, got", [
    (["RR", "FL"], ["FL", "RR"]), (["FL", "FL"], ["FL"]), (["XX", ""], None), ([], None),
])
def test_wheels_are_kept_in_one_order_once_each(raw, got):
    assert validation.parse_wheels(raw) == got


@pytest.mark.parametrize("wheels, text", [
    (["FL", "FR", "RL", "RR"], "all four"), (["FL", "FR"], "front"), (["RL", "RR"], "rear"),
    (["FL", "RL"], "left"), (["FR", "RR"], "right"), (["FL", "RR"], "front left, rear right"), (None, ""),
])
def test_wheels_read_as_a_person_would_say_them(wheels, text):
    assert db.wheels_text(wheels) == text


@pytest.mark.parametrize("name, expected", [
    ("Brakes", True), ("Brake pads", True), ("Wheel bearing", True), ("Shock absorbers", True), ("Wheel repair", True),
    ("Brake fluid", False), ("Wheel alignment", False), ("Tyre rotation", False), ("Engine oil", False),
])
def test_which_new_items_are_per_wheel(name, expected):
    assert db.looks_per_wheel(name) is expected


@pytest.mark.parametrize("subtypes, wheels", [
    (["Brakes (rear)"], ["RL", "RR"]), (["Brakes (front)"], ["FL", "FR"]),
    (["Brakes (front)", "Brakes (rear)"], ["FL", "FR", "RL", "RR"]), (["Tire A", "Tire B", "Tire C", "Tire D"], ["FL", "FR", "RL", "RR"]),
    (["Tire A", "Tire B"], None), (["New Tires"], None),
])
def test_what_acar_says_about_wheels(subtypes, wheels):
    assert acar.wheels_in(subtypes) == wheels


def test_brakes_and_tyres_start_per_wheel_and_nothing_else(admin):
    assert [r["name"] for r in admin.execute("SELECT name FROM garagebooks.service_item WHERE per_wheel")] == ["Brakes"]
    assert [r["name"] for r in admin.execute("SELECT name FROM garagebooks.category WHERE per_wheel")] == ["Tyres"]


def brakes(admin):
    return admin.execute("SELECT id FROM garagebooks.service_item WHERE name = 'Brakes'").fetchone()["id"]


def item_wheels(admin):
    return [r["wheels"] for r in admin.execute("SELECT wheels FROM garagebooks.expense_service_item ORDER BY item_id")]


def test_a_service_keeps_which_wheels_had_brakes_and_the_list_says_so(client, admin):
    data = form(admin, category_id=str(category_id(admin, "Service")), service_item=[str(brakes(admin))],
                **{f"service_item_wheels_{brakes(admin)}": ["RL", "RR"]})
    assert client.post("/new", data=data, headers=ORIGIN).status_code == 303
    assert item_wheels(admin) == [["RL", "RR"]]
    assert "Brakes (rear)" in client.get("/").data.decode()
    (row,) = rows(admin)
    edit = client.get(f"/{row['id']}/edit").data.decode()
    assert re.search(rf'name="service_item_wheels_{brakes(admin)}" value="RL" checked', edit)


def test_tyres_keep_which_wheels_and_other_categories_never_do(client, admin):
    client.post("/new", data=form(admin, wheels=["FL", "RR"]), headers=ORIGIN)
    client.post("/new", data=form(admin, category_id=str(category_id(admin, "Insurance")), amount="2", wheels=["FL"]), headers=ORIGIN)
    assert [r["wheels"] for r in rows(admin)] == [["FL", "RR"], None]
    assert "front left, rear right" in client.get("/").data.decode()


def test_the_choice_shows_only_for_a_per_wheel_category(client, admin):
    html = client.get("/new").data.decode()
    assert re.search(r'<fieldset class="wheel-field"[^>]*data-wheels-field[^>]*hidden', html)
    assert re.search(rf'value="{category_id(admin, "Tyres")}"[^>]*data-wheels="1"', html)


def test_a_service_item_can_be_made_per_wheel_on_the_categories_page(client, admin):
    html = client.get("/categories").data.decode()
    assert re.search(rf'name="item_per_wheel_{brakes(admin)}"[^>]*checked', html)
    coolant = admin.execute("SELECT id, name, applies_to FROM garagebooks.service_item WHERE name = 'Coolant'").fetchone()
    data = {"item_id": str(coolant["id"]), f"item_name_{coolant['id']}": "Coolant", f"item_applies_to_{coolant['id']}": "all",
            f"item_per_wheel_asked_{coolant['id']}": "1", f"item_per_wheel_{coolant['id']}": "1"}
    try:
        assert client.post("/categories/save", data=data, headers=ORIGIN).status_code == 303
        assert admin.execute("SELECT per_wheel FROM garagebooks.service_item WHERE id = %s", (coolant["id"],)).fetchone()["per_wheel"]
    finally:
        admin.execute("UPDATE garagebooks.service_item SET per_wheel = false WHERE id = %s", (coolant["id"],))


def test_acar_rear_brakes_arrive_with_the_rear_wheels_ticked(client, admin, monkeypatch):
    from tests import test_acar_import as t
    text = (t.FIXTURES / "acar-records.csv").read_text(encoding="utf-8").replace(
        '"Engine Oil, Inspection, Oil Filter"', '"Brakes, Rear"')
    html = client.post("/import/acar", headers=ORIGIN, data={"car_id": "1", "csv_text": text}).data.decode()
    assert re.search(rf'name="service_item_\d+_wheels_{brakes(admin)}" value="RL" checked', html)
    assert re.search(rf'name="service_item_\d+_wheels_{brakes(admin)}" value="RR" checked', html)
