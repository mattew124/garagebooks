"""Fuel: litres on a fill-up, the distance since the last one at once, and L/100 km of the tank.

The arithmetic lives in the garagebooks.fuel_fill view (so the app and the Fuel dashboard share it):
  - distance_km: odometer less the previous fill-up's, whatever kind of fill either was
  - l_per_100km: only on a fill that fills the tank, over the litres since the last full fill (partial
    fills in between are added in) and the distance since it. Not worked out where a fuel-up was missed,
    or where there is no earlier full fill to measure from.
"""
import json
from decimal import Decimal

import psycopg
import pytest
from psycopg.rows import dict_row

from garagebooks import create_app, dashboard, db, fuel, validation
from tests.test_app import ORIGIN, category_id
from tests.test_dashboard import overlapping
from tests.test_sources import runnable

pytestmark = pytest.mark.usefixtures("admin")


@pytest.fixture()
def client(pg, admin, monkeypatch):
    monkeypatch.delenv("BASE_PATH", raising=False)
    monkeypatch.setenv("APP_TZ", "Australia/Brisbane")
    app = create_app(connect=lambda: psycopg.connect(pg.app_url, row_factory=dict_row))
    app.testing = True
    return app.test_client()


@pytest.fixture()
def ute(admin):
    """A petrol car, id 2."""
    admin.execute("INSERT INTO garagebooks.car (id, name, kind) VALUES (2, 'Ute', 'ICE')")
    return 2


def fill(admin, day, km, litres, amount="100", partial=False, missed=False, car=2):
    return admin.execute(
        "INSERT INTO garagebooks.expense (car_id, category_id, occurred_at, amount, odometer_km, litres, full_tank, missed_fill) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
        (car, category_id(admin, "Fuel"), f"2026-10-{day:02d} 09:00+10", amount, km, litres, not partial, missed)).fetchone()["id"]


def figures(admin, car=2):
    return {r["odometer_km"]: r for r in admin.execute(
        "SELECT * FROM garagebooks.fuel_fill WHERE car_id = %s ORDER BY occurred_at, id", (car,))}


def test_fuel_is_a_category_that_records_litres(admin):
    assert admin.execute("SELECT has_litres FROM garagebooks.category WHERE name = 'Fuel'").fetchone()["has_litres"]
    assert not admin.execute("SELECT has_litres FROM garagebooks.category WHERE name = 'Charging'").fetchone()["has_litres"]


def test_distance_shows_from_the_second_fill_and_l_per_100km_from_the_second_full_one(admin, ute):
    fill(admin, 1, 10000, 40)
    fill(admin, 8, 10500, 40, amount="80")
    got = figures(admin)
    assert got[Decimal("10000.0")]["distance_km"] is None and got[Decimal("10000.0")]["l_per_100km"] is None
    second = got[Decimal("10500.0")]
    assert second["distance_km"] == 500 and second["tank_km"] == 500
    assert second["l_per_100km"] == pytest.approx(8.0)
    assert second["price_per_litre"] == pytest.approx(2.0)


def test_a_partial_fill_shows_its_distance_and_the_next_full_fill_counts_it_in(admin, ute):
    fill(admin, 1, 10000, 40)
    fill(admin, 4, 10200, 15, partial=True)
    fill(admin, 8, 10500, 25)
    got = figures(admin)
    partial = got[Decimal("10200.0")]
    assert partial["distance_km"] == 200 and partial["l_per_100km"] is None       # unknown until the tank is filled
    full = got[Decimal("10500.0")]
    assert full["distance_km"] == 300                                              # since the last fuel-up
    assert full["tank_km"] == 500 and full["tank_litres"] == 40                    # the whole tank, from the last full fill
    assert full["l_per_100km"] == pytest.approx(8.0)


def test_a_missed_fill_shows_the_distance_but_no_l_per_100km_for_that_tank_only(admin, ute):
    fill(admin, 1, 10000, 40)
    fill(admin, 9, 11000, 50, missed=True)             # a fuel-up between these two wasn't recorded
    fill(admin, 16, 11400, 36)
    got = figures(admin)
    missed = got[Decimal("11000.0")]
    assert missed["distance_km"] == 1000 and missed["l_per_100km"] is None
    after = got[Decimal("11400.0")]
    assert after["distance_km"] == 400 and after["l_per_100km"] == pytest.approx(9.0)   # the tank after it is fine again


def test_a_missed_flag_on_a_partial_fill_spoils_the_tank_it_belongs_to(admin, ute):
    fill(admin, 1, 10000, 40)
    fill(admin, 4, 10200, 15, partial=True, missed=True)
    fill(admin, 8, 10500, 25)
    assert figures(admin)[Decimal("10500.0")]["l_per_100km"] is None


def test_no_odometer_means_no_distance_and_no_figure(admin, ute):
    fill(admin, 1, 10000, 40)
    fill(admin, 8, None, 40)
    row = [r for r in admin.execute("SELECT * FROM garagebooks.fuel_fill WHERE car_id = 2 ORDER BY occurred_at")][1]
    assert row["distance_km"] is None and row["l_per_100km"] is None


def test_each_car_is_measured_on_its_own(admin, ute):
    admin.execute("INSERT INTO garagebooks.car (id, name, kind) VALUES (3, 'Wagon', 'ICE')")
    fill(admin, 1, 10000, 40)
    fill(admin, 2, 50000, 40, car=3)
    fill(admin, 8, 10400, 32)
    assert figures(admin)[Decimal("10400.0")]["distance_km"] == 400
    assert figures(admin, car=3)[Decimal("50000.0")]["distance_km"] is None


def test_an_expense_without_litres_is_not_a_fill(admin, ute):
    admin.execute("INSERT INTO garagebooks.expense (car_id, category_id, occurred_at, amount, odometer_km) VALUES (2, %s, now(), 50, 100)",
                  (category_id(admin, "Fuel"),))
    assert figures(admin) == {}


# ---- the form and the list ----------------------------------------------------------------

def post(admin, client, **over):
    data = {"car_id": "2", "category_id": str(category_id(admin, "Fuel")), "date": "2026-10-08", "time": "09:00",
            "amount": "80.00", "odometer_km": "10500", "vendor": "Servo", "litres": "40"}
    data.update(over)
    return client.post("/new", data=data, headers=ORIGIN)


def test_litres_are_stored_and_the_list_shows_distance_and_l_per_100km_at_once(client, admin, ute):
    fill(admin, 1, 10000, 40)
    assert post(admin, client).status_code == 303
    row = admin.execute("SELECT litres, full_tank, missed_fill FROM garagebooks.expense ORDER BY id DESC LIMIT 1").fetchone()
    assert (row["litres"], row["full_tank"], row["missed_fill"]) == (Decimal("40.00"), True, False)
    page = client.get("/").data.decode()
    assert "500 km since the last fuel-up" in page and "8.0 L/100 km" in page and "40.00 L" in page


def test_a_partial_and_a_missed_fill_are_marked_in_the_list(client, admin, ute):
    fill(admin, 1, 10000, 40)
    assert post(admin, client, litres="15", partial="1", odometer_km="10200").status_code == 303
    assert post(admin, client, litres="30", missed="1", odometer_km="11000", date="2026-10-12", amount="95.00").status_code == 303
    page = client.get("/").data.decode()
    assert "Partial fill" in page and "After a fuel-up missed" in page
    assert "L/100 km" not in page


@pytest.mark.parametrize("bad", ["abc", "0", "-5", "100000", "nan"])
def test_litres_must_be_a_positive_number(client, admin, ute, bad):
    resp = post(admin, client, litres=bad)
    assert resp.status_code == 400 and "litres" in resp.data.decode().lower()
    assert admin.execute("SELECT count(*) AS n FROM garagebooks.expense").fetchone()["n"] == 0


def test_blank_litres_keeps_the_expense_and_forgets_the_flags(client, admin, ute):
    assert post(admin, client, litres="", partial="1", missed="1").status_code == 303
    row = admin.execute("SELECT litres, full_tank, missed_fill FROM garagebooks.expense").fetchone()
    assert (row["litres"], row["full_tank"], row["missed_fill"]) == (None, True, False)


def test_editing_keeps_litres_and_the_flags(client, admin, ute):
    fill(admin, 1, 10000, 40)
    eid = fill(admin, 4, 10200, 15, partial=True)
    page = client.get(f"/{eid}/edit").data.decode()
    assert 'name="litres"' in page and 'value="15.00"' in page and "checked" in page
    assert client.post(f"/{eid}/edit", data={
        "car_id": "2", "category_id": str(category_id(admin, "Fuel")), "date": "2026-10-04", "time": "09:00", "amount": "100",
        "odometer_km": "10200", "litres": "16", "missed": "1"}, headers=ORIGIN).status_code == 303
    row = admin.execute("SELECT litres, full_tank, missed_fill FROM garagebooks.expense WHERE id = %s", (eid,)).fetchone()
    assert (row["litres"], row["full_tank"], row["missed_fill"]) == (Decimal("16.00"), True, True)


def test_the_litres_fields_follow_the_category(client, admin):
    page = client.get("/new").data.decode()
    fuel_id, charging_id = category_id(admin, "Fuel"), category_id(admin, "Charging")
    assert f'<option value="{fuel_id}" ' in page and 'data-litres="1"' in page
    option = page[page.index(f'<option value="{charging_id}"'):].split(">", 1)[0]
    assert 'data-litres="0"' in option
    assert 'id="litres-fields"' in page


def test_the_category_page_lets_litres_be_turned_on_or_off(client, admin):
    tyres = category_id(admin, "Tyres")
    assert f'name="has_litres_{tyres}"' in client.get("/categories").data.decode()

    def save(**ticks):
        return client.post("/categories/save", data={"category_id": str(tyres), **ticks}, headers=ORIGIN)

    def has_litres():
        return admin.execute("SELECT has_litres FROM garagebooks.category WHERE id = %s", (tyres,)).fetchone()["has_litres"]

    try:
        assert save(**{f"has_litres_{tyres}": "1"}).status_code == 303
        assert has_litres()
    finally:
        save()
    assert not has_litres()


def test_a_new_expense_with_no_fuel_fields_is_not_logged_as_having_a_full_tank(client, admin):
    client.post("/new", data={"car_id": "1", "category_id": str(category_id(admin, "Tyres")), "date": "2026-10-08",
                              "time": "09:00", "amount": "10"}, headers=ORIGIN)
    page = client.get("/history").data.decode()
    assert "Full tank" not in page and "Litres" not in page


# ---- the live preview -----------------------------------------------------------------------

def preview(client, admin, **over):
    q = {"car_id": "2", "category_id": str(category_id(admin, "Fuel")), "date": "2026-10-08", "time": "09:00",
         "odometer_km": "10500", "litres": "40"}
    q.update(over)
    return client.get("/api/fuel", query_string=q)


def test_the_preview_gives_the_distance_before_any_litres_are_typed(client, admin, ute):
    fill(admin, 1, 10000, 40)
    got = preview(client, admin, litres="").get_json()
    assert got["distance_km"] == 500 and got["l_per_100km"] is None
    assert any("500 km since the last fuel-up" in line for line in got["lines"])


def test_the_preview_works_out_the_tank_and_writes_nothing(client, admin, ute):
    fill(admin, 1, 10000, 40)
    before = admin.execute("SELECT count(*) AS n FROM garagebooks.expense").fetchone()["n"]
    got = preview(client, admin).get_json()
    assert got["l_per_100km"] == pytest.approx(8.0)
    assert any("8.0 L/100 km" in line for line in got["lines"])
    assert admin.execute("SELECT count(*) AS n FROM garagebooks.expense").fetchone()["n"] == before


def test_the_preview_of_an_edit_leaves_the_saved_expense_alone(client, admin, ute):
    fill(admin, 1, 10000, 40)
    eid = fill(admin, 8, 10500, 40)
    got = preview(client, admin, expense_id=str(eid), litres="30").get_json()
    assert got["l_per_100km"] == pytest.approx(6.0)
    assert admin.execute("SELECT litres FROM garagebooks.expense WHERE id = %s", (eid,)).fetchone()["litres"] == 40


def test_the_preview_says_when_it_is_the_first_fill_or_has_no_odometer(client, admin, ute):
    assert any("first" in line.lower() for line in preview(client, admin).get_json()["lines"])
    fill(admin, 1, 10000, 40)
    assert any("odometer" in line.lower() for line in preview(client, admin, odometer_km="").get_json()["lines"])


def test_the_preview_refuses_bad_input(client, admin, ute):
    assert preview(client, admin, litres="abc").status_code == 400
    assert preview(client, admin, car_id="").status_code == 400
    assert preview(client, admin, date="nope").status_code == 400


def test_describe_a_partial_and_a_missed_fill():
    assert any("partial" in line.lower() for line in fuel.describe(
        {"odometer_km": 10200, "distance_km": 200, "full_tank": False, "missed_fill": False, "l_per_100km": None, "litres": Decimal("15")}))
    assert any("missed" in line.lower() for line in fuel.describe(
        {"odometer_km": 11000, "distance_km": 900, "full_tank": True, "missed_fill": True, "l_per_100km": None, "litres": Decimal("50")}))


def test_parse_litres():
    assert validation.parse_litres("") is None and validation.parse_litres("  ") is None
    assert validation.parse_litres("42.456") == Decimal("42.46")
    assert validation.parse_litres("1,042.5") == Decimal("1042.50")
    for bad in ("abc", "0", "-1", "100000", "inf"):
        with pytest.raises(ValueError):
            validation.parse_litres(bad)


# ---- the dashboards -------------------------------------------------------------------------

SHIPPED = json.loads((dashboard.ROOT / "grafana" / dashboard.FUEL_FILENAME).read_text())


def test_the_fuel_dashboard_is_a_dashboard_of_its_own_with_unique_panels():
    assert SHIPPED["uid"] == "garagebooks-fuel" and SHIPPED["title"] == "Fuel"
    ids = [p["id"] for p in SHIPPED["panels"]]
    assert len(ids) == len(set(ids)) and overlapping(SHIPPED["panels"]) == []
    assert {v["name"] for v in SHIPPED["templating"]["list"]} >= {"car_id", "period", "tz", "app_url"}


def for_car(sql, car=2):
    """A query with Grafana's variables filled in, for this car."""
    return runnable(sql.replace("$car_id", str(car)))


def test_every_fuel_panel_runs(admin, ute):
    fill(admin, 1, 10000, 40)
    fill(admin, 4, 10200, 15, partial=True)
    fill(admin, 8, 10500, 25)
    dash = dashboard.render_fuel("Australia/Brisbane", "")
    for var in dash["templating"]["list"]:
        if var["type"] == "query":
            admin.execute(for_car(var["query"])).fetchall()
    for panel in dash["panels"]:
        for target in panel.get("targets", []):
            admin.execute(for_car(target["rawSql"]).replace("'UTC'", "'Australia/Brisbane'")).fetchall()


def test_the_fuel_stats_agree_with_the_fills(admin, ute):
    fill(admin, 1, 10000, 40, amount="80")
    fill(admin, 4, 10200, 15, amount="30", partial=True)
    fill(admin, 8, 10500, 25, amount="50")
    dash = dashboard.render_fuel("UTC", "")

    def stat(title):
        sql = for_car(next(p for p in dash["panels"] if p["title"] == title)["targets"][0]["rawSql"])
        return list(admin.execute(sql).fetchone().values())[0]

    assert stat("Fuel cost") == 160 and stat("Litres") == 80 and stat("Fill-ups") == 3
    assert stat("Average L/100 km") == pytest.approx(8.0)


def test_the_car_list_offers_only_cars_that_use_fuel(admin, ute):
    dash = dashboard.render_fuel("UTC", "")
    sql = next(v["query"] for v in dash["templating"]["list"] if v["name"] == "car_id")
    names = [r["__text"] for r in admin.execute(sql).fetchall()]
    assert "Ute" in names and "Test car" not in names


def test_the_main_dashboard_names_its_energy_panel_after_the_car_and_lists_only_that_cars_categories(admin, ute):
    dash = dashboard.render("UTC", "")
    energy = next(p for p in dash["panels"] if p["id"] == 4)
    assert energy["title"] == "${energy_label}"
    label_sql = next(v["query"] for v in dash["templating"]["list"] if v["name"] == "energy_label")
    category_sql = next(v["query"] for v in dash["templating"]["list"] if v["name"] == "category")
    assert admin.execute(for_car(label_sql, 2)).fetchone()["label"] == "Fuel"
    assert admin.execute(for_car(label_sql, 1)).fetchone()["label"] == "Charging"
    assert "Charging" not in [r["name"] for r in admin.execute(for_car(category_sql, 2)).fetchall()]
    assert "Charging" in [r["name"] for r in admin.execute(for_car(category_sql, 1)).fetchall()]


def test_the_fuel_dashboard_is_written_only_when_a_car_uses_fuel(tmp_path):
    assert dashboard.export(tmp_path, "UTC", "", fuel=False) is True
    assert [p.name for p in tmp_path.iterdir()] == [dashboard.FILENAME]
    assert "garagebooks-fuel" not in json.dumps(json.loads((tmp_path / dashboard.FILENAME).read_text())["links"])
    assert dashboard.export(tmp_path, "UTC", "", fuel=True) is True
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted([dashboard.FILENAME, dashboard.FUEL_FILENAME])
    main = json.loads((tmp_path / dashboard.FILENAME).read_text())
    assert any(link.get("tags") == ["garagebooks"] for link in main["links"])
    assert dashboard.export(tmp_path, "UTC", "", fuel=True) is False
    assert json.loads((tmp_path / dashboard.FUEL_FILENAME).read_text())["uid"] == "garagebooks-fuel"


def test_the_app_writes_the_fuel_dashboard_once_a_petrol_car_exists(admin, pg, monkeypatch, tmp_path):
    monkeypatch.setenv("DASHBOARD_DIR", str(tmp_path))
    connect = lambda: psycopg.connect(pg.app_url, row_factory=dict_row)   # noqa: E731
    create_app(connect=connect)
    assert not (tmp_path / dashboard.FUEL_FILENAME).exists()
    admin.execute("INSERT INTO garagebooks.car (id, name, kind) VALUES (2, 'Ute', 'ICE')")
    create_app(connect=connect)
    assert (tmp_path / dashboard.FUEL_FILENAME).exists()


def test_the_image_ships_every_dashboard():
    """The app writes its dashboards from the image's own copy, so each one must be copied in."""
    root = dashboard.ROOT
    dockerfile, ignore = (root / "Dockerfile").read_text(), (root / ".dockerignore").read_text()
    for path in (root / "grafana").glob("*.json"):
        assert path.name in dockerfile and f"!grafana/{path.name}" in ignore, path.name
