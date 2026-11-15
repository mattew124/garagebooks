"""TeslaMate is optional (roadmap T1). GarageBooks keeps its own car list, asks `sources` for
odometer and charging figures, and works with TeslaMate absent, unreadable or on a version it
wasn't tested with: the figures that need it say so, nothing shows a wrong zero, nothing else stops."""

import json
import re
from datetime import datetime, timezone
from decimal import Decimal

import psycopg
import pytest
from psycopg.rows import dict_row

from garagebooks import compat, create_app, dashboard, sources
from tests.conftest import TESLAMATE

ORIGIN = {"Origin": "http://localhost"}
UTC = timezone.utc


def connect(url):
    return psycopg.connect(url, row_factory=dict_row, autocommit=True)


def start(url, monkeypatch, **env):
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("APP_TZ", "Australia/Brisbane")
    for key in ("SKIP_MIGRATE", "ALLOW_UNTESTED_TESLAMATE", "DASHBOARD_DIR", "BASE_PATH"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    app = create_app()
    app.testing = True
    return app.test_client()


def category(conn, name):
    return conn.execute("SELECT id FROM garagebooks.category WHERE name = %s", (name,)).fetchone()["id"]


# ---- no TeslaMate at all ---------------------------------------------------------

def test_the_app_starts_and_works_on_a_database_with_no_teslamate(empty_db, monkeypatch, tmp_path):
    client = start(empty_db, monkeypatch, DASHBOARD_DIR=str(tmp_path))
    assert client.get("/").status_code == 200
    page = client.get("/cars").data.decode()
    assert 'data-state="absent"' in page and "not configured" in page and "No cars yet" in page

    assert client.post("/cars", data={"name": "Old ute", "kind": "ICE", "make": "Toyota", "model": "HiLux"},
                       headers=ORIGIN).status_code == 303
    with connect(empty_db) as conn:
        car = conn.execute("SELECT * FROM garagebooks.car").fetchone()
        assert (car["name"], car["kind"], car["make"], car["model"]) == ("Old ute", "ICE", "Toyota", "HiLux")
        assert car["id"] >= 1000 and car["teslamate_car_id"] is None
        tyres, fuel = category(conn, "Tyres"), category(conn, "Fuel")
    for cat, amount, odo in ((tyres, "800", "150000"), (fuel, "95.50", "150420")):
        saved = client.post("/new", data={"car_id": str(car["id"]), "category_id": str(cat), "date": "2026-10-01",
                                          "time": "09:00", "amount": amount, "odometer_km": odo, "vendor": "",
                                          "invoice_url": "", "notes": ""}, headers=ORIGIN)
        assert saved.status_code == 303
    assert client.post(f"/cars/{car['id']}/readings", data={"date": "2026-11-01", "km": "151,000"},
                       headers=ORIGIN).status_code == 303
    with connect(empty_db) as conn:
        own = conn.execute("SELECT * FROM garagebooks.ownership").fetchone()
    assert (own["car_name"], own["running_total"], own["charging_total"], own["tco"]) == ("Old ute", 800, Decimal("95.50"), Decimal("895.50"))
    assert (own["start_km"], own["current_km"], own["distance_km"]) == (150000, 151000, 1000)
    assert own["teslamate_charging_total"] == 0
    page = client.get("/cars").data.decode()
    assert "151,000 km" in page and "added by hand" in page

    # the dashboard was written without TeslaMate's tables, and with nothing to remark on
    dash = json.loads((tmp_path / dashboard.FILENAME).read_text())
    assert "charging_processes" not in json.dumps(dash)
    assert all(p["id"] != dashboard.NOTE_PANEL_ID for p in dash["panels"])


def test_the_schema_has_no_foreign_key_onto_teslamate(pg):
    with pg.admin() as conn:
        refs = conn.execute(
            "SELECT con.conrelid::regclass::text AS tbl, con.confrelid::regclass::text AS ref FROM pg_constraint con "
            "JOIN pg_namespace n ON n.oid = con.connamespace WHERE con.contype = 'f' AND n.nspname = 'garagebooks'").fetchall()
    assert {r["tbl"] for r in refs if r["ref"] == "garagebooks.car"} >= {
        "garagebooks.expense", "garagebooks.car_folder", "garagebooks.import_skip", "garagebooks.odometer_reading"}
    assert not [r for r in refs if not r["ref"].startswith("garagebooks.")]


def test_the_add_form_opens_with_more_than_one_car_and_none_chosen(empty_db, monkeypatch, tmp_path):
    client = start(empty_db, monkeypatch, INVOICES_DIR=str(tmp_path))
    for name in ("One", "Two"):
        client.post("/cars", data={"name": name, "kind": "BEV"}, headers=ORIGIN)
    page = client.get("/new")
    assert page.status_code == 200 and b"Choose" in page.data and b">Two<" in page.data


def test_car_form_is_validated(empty_db, monkeypatch):
    client = start(empty_db, monkeypatch)
    for data in ({"name": " ", "kind": "BEV"}, {"name": "x" * 101, "kind": "BEV"}, {"name": "Car", "kind": "TANK"},
                 {"name": "Car", "kind": "BEV", "make": "m" * 61}):
        assert client.post("/cars", data=data, headers=ORIGIN).status_code == 400
    with connect(empty_db) as conn:
        assert conn.execute("SELECT count(*) AS n FROM garagebooks.car").fetchone()["n"] == 0


def test_a_car_added_by_hand_can_be_edited_and_deleted_until_it_has_expenses(empty_db, monkeypatch):
    client = start(empty_db, monkeypatch)
    client.post("/cars", data={"name": "Bike", "kind": "ICE"}, headers=ORIGIN)
    with connect(empty_db) as conn:
        car_id = conn.execute("SELECT id FROM garagebooks.car").fetchone()["id"]
    assert client.post(f"/cars/{car_id}", data={"name": "Motorbike", "kind": "ICE", "make": "Honda"},
                       headers=ORIGIN).status_code == 303
    assert b"Motorbike" in client.get("/cars").data
    assert client.post("/cars/9999", data={"name": "x", "kind": "ICE"}, headers=ORIGIN).status_code == 404
    with connect(empty_db) as conn:
        conn.execute("INSERT INTO garagebooks.expense (car_id, category_id, occurred_at, amount) VALUES (%s, %s, now(), 1)",
                     (car_id, category(conn, "Tyres")))
    refused = client.post(f"/cars/{car_id}/delete", headers=ORIGIN)
    assert refused.status_code == 400 and b"no expenses" in refused.data
    with connect(empty_db) as conn:
        conn.execute("DELETE FROM garagebooks.expense")
    assert client.post(f"/cars/{car_id}/delete", headers=ORIGIN).status_code == 303
    with connect(empty_db) as conn:
        assert conn.execute("SELECT count(*) AS n FROM garagebooks.car").fetchone()["n"] == 0


def test_odometer_readings_are_validated_and_can_be_deleted(empty_db, monkeypatch):
    client = start(empty_db, monkeypatch)
    client.post("/cars", data={"name": "Car", "kind": "HEV"}, headers=ORIGIN)
    with connect(empty_db) as conn:
        car_id = conn.execute("SELECT id FROM garagebooks.car").fetchone()["id"]
    for data in ({"date": "bad", "km": "5"}, {"date": "2026-01-01", "km": ""}, {"date": "2026-01-01", "km": "-4"}):
        assert client.post(f"/cars/{car_id}/readings", data=data, headers=ORIGIN).status_code == 400
    assert client.post("/cars/9999/readings", data={"date": "2026-01-01", "km": "5"}, headers=ORIGIN).status_code == 404
    client.post(f"/cars/{car_id}/readings", data={"date": "2026-01-01", "km": "5000"}, headers=ORIGIN)
    client.post(f"/cars/{car_id}/readings", data={"date": "2026-01-01", "km": "5100"}, headers=ORIGIN)   # same moment: replaced
    with connect(empty_db) as conn:
        (row,) = conn.execute("SELECT id, km FROM garagebooks.odometer_reading").fetchall()
    assert row["km"] == 5100
    assert client.post(f"/cars/{car_id}/readings/{row['id']}/delete", headers=ORIGIN).status_code == 303
    assert client.post(f"/cars/{car_id}/readings/{row['id']}/delete", headers=ORIGIN).status_code == 404


def test_a_reading_entered_today_counts_straight_away(empty_db, monkeypatch):
    """Even before noon: it is never stamped later than now."""
    from zoneinfo import ZoneInfo
    client = start(empty_db, monkeypatch)
    client.post("/cars", data={"name": "Car", "kind": "BEV"}, headers=ORIGIN)
    with connect(empty_db) as conn:
        car_id = conn.execute("SELECT id FROM garagebooks.car").fetchone()["id"]
    today = datetime.now(ZoneInfo("Australia/Brisbane")).date().isoformat()
    client.post(f"/cars/{car_id}/readings", data={"date": today, "km": "777"}, headers=ORIGIN)
    with connect(empty_db) as conn:
        assert conn.execute("SELECT read_at <= now() AS past FROM garagebooks.odometer_reading").fetchone()["past"]
    assert b"777 km" in client.get("/cars").data


def test_sources_answer_from_what_was_entered_by_hand(empty_db, monkeypatch):
    start(empty_db, monkeypatch)
    with connect(empty_db) as conn:
        state = sources.detect(conn)
        assert state.state == sources.ABSENT and not state.usable and not state.installed
        assert sources.first_activity(conn, state) is None
        conn.execute("INSERT INTO garagebooks.car (id, name) VALUES (7, 'Car')")
        when = datetime(2026, 6, 1, tzinfo=UTC)
        assert sources.odometer_at(conn, state, 7, when) is None                 # no answer, not zero
        conn.execute("INSERT INTO garagebooks.odometer_reading (car_id, read_at, km) VALUES (7, '2026-05-01+00', 1200)")
        conn.execute("INSERT INTO garagebooks.expense (car_id, category_id, occurred_at, amount, odometer_km) VALUES "
                     "(7, %s, '2026-05-20+00', 60, 1500), (7, %s, '2026-07-01+00', 70, 2500)",
                     (category(conn, "Charging"), category(conn, "Charging")))
        reading = sources.odometer_at(conn, state, 7, when)
        assert (reading.km, reading.source, reading.at) == (1500, "manual", datetime(2026, 5, 20, tzinfo=UTC))
        assert sources.odometer_at(conn, state, 7, when, manual=False) is None
        assert sources.odometer_at(conn, state, 7, datetime(2026, 4, 1, tzinfo=UTC)) is None
        assert sources.charging_cost(conn, state, 7) == sources.Answer(Decimal("130.00"))
        assert sources.charging_cost(conn, state, 7, start=when).value == 70
        assert sources.charging_cost(conn, state, 7, end=when).value == 60
        assert sources.first_activity(conn, state) == datetime(2026, 5, 1, tzinfo=UTC)


# ---- TeslaMate appears, or can't be read ------------------------------------------

STAND_IN = """
CREATE TABLE public.cars (id smallint PRIMARY KEY, name text, vin text, model text);
CREATE TABLE public.positions (car_id smallint, date timestamp, odometer double precision);
CREATE TABLE public.charging_processes (car_id smallint, start_date timestamp, cost numeric(6, 2));
INSERT INTO public.cars VALUES (1, 'Tessie', '5YJ000000000123456', 'Y'), (2, NULL, '5YJ000000000654321', '3');
INSERT INTO public.positions VALUES (1, '2026-01-01', 100), (1, '2026-03-01', 900);
INSERT INTO public.charging_processes VALUES (1, '2026-02-01', 12.50), (1, '2026-02-02', NULL);
"""   # a stand-in with only the columns the app reads, not TeslaMate's schema


def test_teslamate_appearing_later_is_picked_up_without_a_restart(empty_db, monkeypatch, tmp_path):
    import garagebooks
    client = start(empty_db, monkeypatch, DASHBOARD_DIR=str(tmp_path))
    client.post("/cars", data={"name": "Old ute", "kind": "ICE"}, headers=ORIGIN)
    with connect(empty_db) as conn:
        conn.execute("UPDATE garagebooks.car SET id = 1")        # a car by hand already has TeslaMate's id
        conn.execute(STAND_IN)
    monkeypatch.setattr(garagebooks, "TESLAMATE_RECHECK_SECONDS", 0)
    page = client.get("/cars").data.decode()
    assert 'data-state="ok"' in page and "Tessie" in page and "from TeslaMate" in page and "123456" not in page
    with connect(empty_db) as conn:
        cars = {r["name"]: r for r in conn.execute("SELECT * FROM garagebooks.car")}
        assert cars["Old ute"]["id"] == 1 and cars["Old ute"]["teslamate_car_id"] is None    # never overwritten
        assert cars["Tessie"]["teslamate_car_id"] == 1 and cars["Tessie"]["id"] >= 1000       # its id was taken
        assert cars["654321"]["id"] == 2 and cars["654321"]["make"] == "Tesla"               # no name: the VIN's end
        own = conn.execute("SELECT * FROM garagebooks.ownership WHERE car_name = 'Tessie'").fetchone()
        assert (own["charging_total"], own["start_km"], own["current_km"], own["distance_km"]) == (Decimal("12.50"), 100, 900, 800)
        state = sources.detect(conn)
        tessie = cars["Tessie"]["id"]
        assert sources.odometer_at(conn, state, tessie, datetime(2026, 2, 1, tzinfo=UTC)) == sources.Reading(100, "teslamate")
        assert sources.charging_cost(conn, state, tessie).value == Decimal("12.50")
        assert sources.first_activity(conn, state) == datetime(2026, 1, 1, tzinfo=UTC)
        # renamed in TeslaMate: follows. Renamed here: ours is kept.
        conn.execute("UPDATE public.cars SET name = 'Tess' WHERE id = 1")
    client.post("/cars/2", data={"name": "Blue 3", "kind": "BEV"}, headers=ORIGIN)
    with connect(empty_db) as conn:
        conn.execute("UPDATE public.cars SET name = 'Model 3' WHERE id = 2")
    page = client.get("/cars").data.decode()
    assert "Tess " in page and "Tessie" not in page and "Blue 3" in page and "Model 3" not in page
    assert "charging_processes" in (tmp_path / dashboard.FILENAME).read_text()   # the dashboard followed


def test_a_teslamate_that_cannot_be_read_is_unavailable_and_nothing_else_stops(empty_db, monkeypatch, tmp_path):
    with connect(empty_db) as conn:
        conn.execute(STAND_IN)
    client = start(empty_db, monkeypatch)                     # readable at first: its cars are listed
    with connect(empty_db) as conn:
        conn.execute("ALTER TABLE public.positions RENAME COLUMN odometer TO odo")    # a table that changed
        tyres = category(conn, "Tyres")
    client = start(empty_db, monkeypatch, DASHBOARD_DIR=str(tmp_path))
    page = client.get("/cars").data.decode()
    assert 'data-state="unavailable"' in page and "UndefinedColumn" in page
    assert "<strong>unavailable</strong>" in page                                      # the odometer: not 0, not blank
    saved = client.post("/new", data={"car_id": "1", "category_id": str(tyres), "date": "2026-10-01", "time": "09:00",
                                      "amount": "800", "odometer_km": "", "vendor": "", "invoice_url": "", "notes": ""},
                        headers=ORIGIN)
    assert saved.status_code == 303
    assert client.get("/api/odometer?car_id=1&date=2026-10-01&time=09:00").get_json() == {"odometer_km": None}
    with connect(empty_db) as conn:
        own = conn.execute("SELECT * FROM garagebooks.ownership WHERE car_id = 1").fetchone()
        assert own["running_total"] == 800 and own["charging_total"] is None and own["tco"] is None
        state = sources.detect(conn)
        answer = sources.charging_cost(conn, state, 1)
        assert answer.value is None and "can't be read" in answer.reason
        conn.execute("INSERT INTO garagebooks.car (id, name) VALUES (50, 'By hand')")
        assert sources.charging_cost(conn, state, 50).value == 0                       # a car by hand is complete
    dash = json.loads((tmp_path / dashboard.FILENAME).read_text())
    note = next(p for p in dash["panels"] if p["id"] == dashboard.NOTE_PANEL_ID)
    assert "UndefinedColumn" in note["options"]["content"]
    assert "charging_processes" not in json.dumps(dash)


def test_teslamate_deleting_its_car_leaves_ours_and_its_expenses(empty_db, monkeypatch):
    with connect(empty_db) as conn:
        conn.execute(STAND_IN)
    client = start(empty_db, monkeypatch)
    with connect(empty_db) as conn:
        conn.execute("INSERT INTO garagebooks.expense (car_id, category_id, occurred_at, amount) VALUES (1, %s, now(), 5)",
                     (category(conn, "Tyres"),))
        conn.execute("DELETE FROM public.cars WHERE id = 1")       # our foreign key used to stop this
    assert b"1 expense" in client.get("/cars").data and b"Tessie" in client.get("/cars").data
    assert b"$5.00" in client.get("/").data


# ---- with the real TeslaMate -------------------------------------------------------

@pytest.mark.teslamate
def test_teslamates_car_keeps_its_id_and_is_linked(admin):
    car = admin.execute("SELECT * FROM garagebooks.car").fetchone()
    assert (car["id"], car["teslamate_car_id"], car["name"], car["make"]) == (1, 1, "Test car", "Tesla")


@pytest.mark.teslamate
def test_the_real_teslamate_is_usable_and_the_least_privilege_role_can_ask_it(pg, admin):
    admin.execute("INSERT INTO positions (latitude, longitude, date, odometer, car_id) VALUES (0, 0, '2026-01-01', 1234.56, 1)")
    admin.execute("INSERT INTO charging_processes (start_date, position_id, cost, car_id) VALUES ('2026-01-02', 1, 7.25, 1)")
    with psycopg.connect(pg.app_url, row_factory=dict_row) as conn:
        state = sources.detect(conn)
        assert state == sources.TeslaMate(sources.OK, "", True)
        assert sources.sync_cars(conn, state) == 0
        assert sources.odometer_at(conn, state, 1, datetime(2026, 6, 1, tzinfo=UTC)) == sources.Reading(1234.6, "teslamate")
        assert sources.charging_cost(conn, state, 1) == sources.Answer(Decimal("7.25"))
        assert sources.charging_cost(conn, state, 1, start=datetime(2026, 2, 1, tzinfo=UTC)).value == 0
        assert conn.execute("SELECT garagebooks.build_ownership(false) AS tm").fetchone()["tm"] is False
        assert conn.execute("SELECT garagebooks.build_ownership(true) AS tm").fetchone()["tm"] is True


@pytest.mark.teslamate
def test_an_untested_teslamate_is_reported_with_the_reason(blank_db):
    with connect(blank_db) as conn:
        conn.execute("INSERT INTO schema_migrations (version, inserted_at) VALUES (%s, now())", (compat.TESTED_MIGRATION + 1,))
        state = sources.detect(conn)
        assert state.state == sources.UNTESTED and not state.usable and compat.TESTED_TESLAMATE in state.reason
        allowed = sources.detect(conn, allow_untested=True)
        assert allowed.usable and allowed.reason == state.reason


@pytest.mark.no_teslamate
def test_this_run_really_has_no_teslamate(pg, admin):
    assert admin.execute("SELECT to_regclass('public.cars') AS t").fetchone()["t"] is None
    with psycopg.connect(pg.app_url, row_factory=dict_row) as conn:      # the provisioned, least-privilege role
        assert sources.detect(conn).state == sources.ABSENT


# ---- the dashboard, in each form, really runs ---------------------------------------

def runnable(sql):
    """A panel's query with Grafana's variables filled in, so PostgreSQL itself can check it."""
    sql = re.sub(r"\$__timeFilter\(([^)]*)\)", r"\1 IS NOT NULL", sql)
    for name, value in (("${app_url:sqlstring}", "'/'"), ("$car_id", "1"), ("$category", "'Charging', 'Tyres'"),
                        ("$period", "month"), ("$tz", "UTC")):
        sql = sql.replace(name, value)
    assert "$" not in sql, sql
    return sql


def queries(dash):
    yield from (v["query"] for v in dash["templating"]["list"] if v["type"] == "query")
    yield from (t["rawSql"] for p in dash["panels"] for t in p.get("targets", []))


def test_every_dashboard_query_runs_with_no_teslamate(empty_db, monkeypatch):
    start(empty_db, monkeypatch)
    dash = dashboard.render("UTC", "", teslamate=sources.TeslaMate(sources.ABSENT))
    assert not any(link["type"] == "dashboards" for link in dash["links"])
    with connect(empty_db) as conn:
        for sql in queries(dash):
            conn.execute(runnable(sql)).fetchall()


def test_every_dashboard_query_runs_in_the_form_for_this_database(admin):
    dash = dashboard.render("UTC", "", teslamate=None if TESLAMATE else sources.TeslaMate(sources.ABSENT))
    admin.execute("INSERT INTO garagebooks.expense (car_id, category_id, occurred_at, amount) VALUES "
                  "(1, %s, '2026-01-05+00', 40), (1, %s, '2026-01-06+00', 300)",
                  (category(admin, "Charging"), category(admin, "Tyres")))
    if TESLAMATE:
        admin.execute("INSERT INTO positions (latitude, longitude, date, odometer, car_id) VALUES (0, 0, '2026-01-01', 10, 1)")
        admin.execute("INSERT INTO charging_processes (start_date, position_id, cost, car_id) VALUES ('2026-01-02', 1, 7.25, 1)")
    results = {p["id"]: admin.execute(runnable(p["targets"][0]["rawSql"])).fetchall()
               for p in dash["panels"] if p.get("targets")}
    for sql in (v["query"] for v in dash["templating"]["list"] if v["type"] == "query"):
        admin.execute(runnable(sql)).fetchall()
    charging = Decimal("47.25") if TESLAMATE else Decimal("40")
    assert list(results[4][0].values()) == [charging]                         # Charging: by hand, plus TeslaMate's
    assert list(results[1][0].values()) == [charging + 300]                   # Total cost
    by_category = {r["Category"]: r["Cost"] for r in results[11]}
    assert by_category == {"Charging": charging, "Tyres": 300}                # one Charging slice, not two
    names = [r["name"] for r in admin.execute(runnable(next(
        v["query"] for v in dash["templating"]["list"] if v["name"] == "category")))]
    assert names.count("Charging") == 1 and "Fuel" in names


def test_a_held_back_dashboard_says_so_and_an_absent_one_does_not():
    held = dashboard.render("UTC", "", teslamate=sources.TeslaMate(sources.UNTESTED, "Newer than tested."))
    note = held["panels"][0]
    assert note["id"] == dashboard.NOTE_PANEL_ID and "Newer than tested." in note["options"]["content"]
    assert all(p["fieldConfig"]["defaults"]["noValue"] == "unavailable" for p in held["panels"] if p["type"] == "stat" and p["title"] != "Sold")
    assert min(p["gridPos"]["y"] for p in held["panels"][1:]) == note["gridPos"]["h"]     # everything moved down
    absent = dashboard.render("UTC", "", teslamate=sources.TeslaMate(sources.ABSENT))
    assert all(p["id"] != dashboard.NOTE_PANEL_ID for p in absent["panels"])
    for dash in (held, absent):
        assert "charging_processes" not in json.dumps(dash) and "/*teslamate*/" not in json.dumps(dash)
    shipped = dashboard.render("UTC", "", teslamate=sources.TeslaMate(sources.OK))
    assert shipped == dashboard.render("UTC", "") and "charging_processes" in json.dumps(shipped)
