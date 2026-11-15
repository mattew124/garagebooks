"""Selling a car: a sale date and price per car. TCO becomes purchase + running + charging - sale price,
the time owned and the distance stop at the sale, and the dashboard and Cars page show the car as sold."""

import html
import re
from datetime import datetime, timezone
from decimal import Decimal

import psycopg
import pytest

from garagebooks import dashboard, sources
from tests.test_sources import ORIGIN, category, connect, runnable, start

UTC = timezone.utc


def sale_client(empty_db, monkeypatch):
    client = start(empty_db, monkeypatch)
    client.post("/cars", data={"name": "Ute", "kind": "ICE"}, headers=ORIGIN)
    with connect(empty_db) as conn:
        car_id = conn.execute("SELECT id FROM garagebooks.car").fetchone()["id"]
        buy, tyres = category(conn, "Initial purchase"), category(conn, "Tyres")
        conn.execute("INSERT INTO garagebooks.expense (car_id, category_id, occurred_at, amount, odometer_km) VALUES "
                     "(%s, %s, '2024-01-01+10', 40000, 1000), (%s, %s, '2024-06-01+10', 1000, 5000)",
                     (car_id, buy, car_id, tyres))
        conn.execute("INSERT INTO garagebooks.odometer_reading (car_id, read_at, km) VALUES "
                     "(%s, '2025-01-01+10', 20000), (%s, '2025-06-01+10', 30000)", (car_id, car_id))
    return client, car_id


def sell(client, car_id, **data):
    return client.post(f"/cars/{car_id}/sale", data=data, headers=ORIGIN)


def text(page):
    """A page as the reader sees it: no tags, entities decoded, spaces collapsed."""
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", page.data.decode())).split())


def ownership(url):
    with connect(url) as conn:
        return conn.execute("SELECT * FROM garagebooks.ownership").fetchone()


def test_selling_a_car_lowers_tco_and_stops_the_distance_at_the_sale(empty_db, monkeypatch):
    client, car_id = sale_client(empty_db, monkeypatch)
    own = ownership(empty_db)
    assert (own["sale_price"], own["sold_at"], own["owned_until"]) == (None, None, None)
    assert own["tco"] == 41000 and own["current_km"] == 30000
    assert sell(client, car_id, date="2025-03-01", price="$31,000").status_code == 303
    own = ownership(empty_db)
    assert own["sale_price"] == Decimal("31000.00") and own["tco"] == 10000
    assert own["current_km"] == 20000 and own["distance_km"] == 19000      # the June reading is after the sale
    assert own["owned_until"] == own["sold_at"] and own["sold_at"] > own["owned_since"]
    assert sell(client, car_id, action="clear").status_code == 303
    own = ownership(empty_db)
    assert (own["sale_price"], own["sold_at"], own["tco"], own["current_km"]) == (None, None, 41000, 30000)


def test_a_sale_is_validated(empty_db, monkeypatch):
    client, car_id = sale_client(empty_db, monkeypatch)
    for data in ({"date": "2025-03-01", "price": ""}, {"date": "", "price": "100"}, {"date": "2999-01-01", "price": "1"},
                 {"date": "2025-03-01", "price": "-5"}, {"date": "2025-03-01", "price": "lots"}, {"date": "x", "price": "1"}):
        assert sell(client, car_id, **data).status_code == 400, data
    assert sell(client, 9999, date="2025-03-01", price="1").status_code == 404
    with connect(empty_db) as conn:
        assert conn.execute("SELECT sold_at FROM garagebooks.car").fetchone()["sold_at"] is None
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute("UPDATE garagebooks.car SET sold_at = now()")      # both or neither
    page = sell(client, car_id, date="2025-03-01", price="-5")
    assert b'name="price"' in page.data and "The sale price can't be negative." in text(page)
    assert sell(client, car_id, date="2025-03-01", price="0").status_code == 303   # given away


def test_a_sale_before_the_first_expense_is_saved_with_a_warning(empty_db, monkeypatch):
    client, car_id = sale_client(empty_db, monkeypatch)
    response = sell(client, car_id, date="2023-01-01", price="10")
    assert response.status_code == 303 and "msg=car_sold_early" in response.headers["Location"]
    assert ownership(empty_db)["sale_price"] == 10


def test_a_sold_car_is_shown_as_sold_and_goes_last_in_the_add_form(empty_db, monkeypatch):
    client, car_id = sale_client(empty_db, monkeypatch)
    client.post("/cars", data={"name": "Newer", "kind": "BEV"}, headers=ORIGIN)
    sell(client, car_id, date="2025-03-01", price="31000")
    page = text(client.get("/cars"))
    assert "Sold 01 Mar 2025 for $31,000.00" in page and "20,000 km" in page
    options = re.findall(r"<option value=\"\d+\"[^>]*>([^<]*)</option>", client.get("/new").data.decode())
    assert [o for o in options if o.strip() in ("Newer", "Ute (sold)")] == ["Newer", "Ute (sold)"]
    with connect(empty_db) as conn:
        cat = category(conn, "Tyres")
    late = client.post("/new", data={"car_id": str(car_id), "category_id": str(cat), "date": "2025-04-01", "time": "09:00",
                                     "amount": "50", "odometer_km": "", "vendor": "", "invoice_url": "", "notes": ""},
                       headers=ORIGIN)
    assert late.status_code == 303                     # a late bill is still accepted


def test_the_sale_columns_are_added_to_an_older_database_and_again_harmlessly(empty_db, monkeypatch):
    sale_client(empty_db, monkeypatch)
    with connect(empty_db) as conn:
        conn.execute("DROP VIEW garagebooks.ownership")
        conn.execute("ALTER TABLE garagebooks.car DROP CONSTRAINT car_sale_both, DROP COLUMN sold_at, DROP COLUMN sale_price")
    start(empty_db, monkeypatch)
    start(empty_db, monkeypatch)
    own = ownership(empty_db)
    assert (own["tco"], own["sale_price"]) == (41000, None)


def test_tco_stays_unknown_for_a_sold_teslamate_car_while_its_figures_are_held_back(empty_db, monkeypatch):
    sale_client(empty_db, monkeypatch)
    with connect(empty_db) as conn:
        conn.execute("UPDATE garagebooks.car SET teslamate_car_id = 7, sold_at = '2025-03-01+10', sale_price = 1000")
        conn.execute("SELECT garagebooks.build_ownership(false)")
        own = conn.execute("SELECT * FROM garagebooks.ownership").fetchone()
    assert own["tco"] is None and own["charging_total"] is None and own["sale_price"] == 1000
    assert own["current_km"] == 20000


@pytest.mark.teslamate
def test_a_sale_stops_teslamates_odometer_and_still_counts_its_charging(admin):
    admin.execute("INSERT INTO positions (latitude, longitude, date, odometer, car_id) VALUES "
                  "(0, 0, '2025-01-01', 100, 1), (0, 0, '2025-02-01', 500, 1), (0, 0, '2025-09-01', 900, 1)")
    admin.execute("INSERT INTO charging_processes (start_date, position_id, cost, car_id) VALUES ('2025-01-02', 1, 20, 1)")
    admin.execute("INSERT INTO garagebooks.expense (car_id, category_id, occurred_at, amount) VALUES (1, %s, '2025-01-01+00', 5000)",
                  (category(admin, "Initial purchase"),))
    admin.execute("UPDATE garagebooks.car SET sold_at = '2025-03-01+00', sale_price = 3000 WHERE id = 1")
    admin.execute("SELECT garagebooks.build_ownership(true)")
    own = admin.execute("SELECT * FROM garagebooks.ownership WHERE car_id = 1").fetchone()
    assert (own["current_km"], own["charging_total"], own["tco"]) == (500, 20, 2020)
    admin.execute("UPDATE garagebooks.car SET sold_at = NULL, sale_price = NULL WHERE id = 1")


def panel_value(conn, dash, title, car_id):
    panel = next(p for p in dash["panels"] if p["title"] == title)
    sql = panel["targets"][0]["rawSql"].replace("$car_id", str(car_id)).replace("$category", "'Initial purchase', 'Tyres'")
    return list(conn.execute(runnable(sql)).fetchone().values())[0]


def test_every_dashboard_query_runs_for_a_sold_car_in_both_forms(empty_db, monkeypatch):
    client, car_id = sale_client(empty_db, monkeypatch)
    sell(client, car_id, date="2025-03-01", price="31000")
    days = (datetime(2025, 3, 1, 23, 59, tzinfo=UTC) - datetime(2023, 12, 31, 14, tzinfo=UTC)).total_seconds() / 86400
    with connect(empty_db) as conn:
        for form in (sources.TeslaMate(sources.ABSENT), sources.TeslaMate(sources.UNTESTED, "Newer.")):
            dash = dashboard.render("UTC", "", teslamate=form)
            assert panel_value(conn, dash, "Sold", car_id) == "Sold 1 Mar 2025 · $31,000"
            assert panel_value(conn, dash, "Total cost", car_id) == 10000 and panel_value(conn, dash, "TCO per month", car_id) > 0
            assert abs(float(panel_value(conn, dash, "TCO per day", car_id)) - 10000 / days) < 5
            assert panel_value(conn, dash, "Time owned", car_id) == "1 year, 2 months"
            names = [r["__text"] for r in conn.execute(runnable(next(
                v["query"] for v in dash["templating"]["list"] if v["name"] == "car_id")))]
            assert "Ute (sold)" in names
            for p in dash["panels"]:
                for t in p.get("targets", []):
                    conn.execute(runnable(t["rawSql"].replace("$car_id", str(car_id)))).fetchall()
            assert [p["title"] for p in dash["panels"]].count("Sold") == 1
