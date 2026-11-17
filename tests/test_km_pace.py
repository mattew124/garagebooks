"""Kilometre pace (roadmap L4): the pure figures, the odometer function the dashboard shares, the two
pages and the dashboard panel, with TeslaMate present, absent and held back. All figures are invented
(plate ABC123, round numbers). Runs with or without TeslaMate."""

import json
import re
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from html import unescape
from zoneinfo import ZoneInfo

import psycopg
import pytest
from psycopg.rows import dict_row

from garagebooks import dashboard, lease, sources
from tests.conftest import ROOT
from tests.test_lease import ORIGIN, app, connect, good_form, start, today_here  # noqa: F401 - app is a fixture
from tests.test_sources import STAND_IN, queries, runnable

TZ = ZoneInfo("Australia/Brisbane")
UTC = ZoneInfo("UTC")
D = Decimal


def a_lease(**over):
    row = {"start_date": date(2024, 4, 1), "end_date": date(2027, 3, 31), "estimated_km_per_year": 15000}
    row.update(over)
    return row


# ---- the pure figures ---------------------------------------------------------------------------

def test_a_full_year_expects_the_estimate_and_needs_a_steady_pace():
    p = lease.km_pace(a_lease(), 2025, date(2025, 4, 1), 20000, None)
    assert (p["first"], p["last"]) == (date(2025, 4, 1), date(2026, 3, 31))
    assert (p["days_held"], p["days_in_year"]) == (365, 365)
    assert p["expected_km"] == D("15000.0") and p["start_km"] == D("20000.0") and p["end_km_needed"] == D("35000.0")
    assert p["per_day_needed"] == D("41.1")                       # 15000 / 365 = 41.09...
    assert p["state"] == "current"


def test_a_lease_starting_part_way_through_is_pro_rata_by_days_held():
    p = lease.km_pace(a_lease(start_date=date(2025, 10, 1)), 2025, date(2025, 10, 1), 1000, None)
    assert p["first"] == date(2025, 10, 1) and p["days_held"] == 182
    assert p["expected_km"] == D("7479.5")                        # 15000 x 182 / 365 = 7479.45...
    assert p["end_km_needed"] == D("8479.5")
    assert p["per_day_needed"] == D("41.1")                       # g / f is the yearly estimate / 365 either way


def test_a_lease_ending_part_way_through_stops_at_its_end_date():
    p = lease.km_pace(a_lease(start_date=date(2022, 6, 1), end_date=date(2025, 9, 30)), 2025, date(2025, 6, 1), 5000, None)
    assert p["last"] == date(2025, 9, 30) and p["days_held"] == 183
    assert p["expected_km"] == D("7520.5")                        # 15000 x 183 / 365 = 7520.54...


def test_a_leap_year_divides_by_366():
    p = lease.km_pace(a_lease(start_date=date(2023, 4, 1)), 2023, date(2023, 4, 1), 0, None)
    assert p["days_in_year"] == 366 and p["per_day_needed"] == D("41.0")      # 15000 / 366 = 40.98...


def test_what_the_statement_cant_show_so_far_this_year():
    # 1 April to 30 September is 183 days gone; 182 remain (today is counted as still to drive).
    p = lease.km_pace(a_lease(), 2025, date(2025, 10, 1), 20000, 27000)
    assert p["state"] == "current" and (p["elapsed_days"], p["remaining_days"]) == (183, 182)
    assert p["driven"] == D("7000.0") and p["per_day_driven"] == D("38.3")    # 7000 / 183
    assert p["expected_so_far"] == D("7520.5")                    # 15000 x 183 / 365
    assert p["ahead"] == D("-520.5")                              # negative: behind the pace
    assert p["per_day_to_go"] == D("44.0")                        # (15000 - 7000) / 182 = 43.96


def test_ahead_of_pace_is_positive_and_a_car_already_past_the_estimate_needs_nothing_more():
    p = lease.km_pace(a_lease(), 2025, date(2025, 10, 1), 20000, 29000)
    assert p["ahead"] == D("1479.5") and p["per_day_to_go"] == D("33.0")      # 6000 / 182 = 32.97
    over = lease.km_pace(a_lease(), 2025, date(2025, 10, 1), 20000, 36000)
    assert over["driven"] == D("16000.0") and over["per_day_to_go"] == D("0.0")


def test_a_year_that_is_over_uses_the_whole_period_and_has_nothing_left_to_drive():
    p = lease.km_pace(a_lease(), 2025, date(2026, 11, 1), 20000, 34000)
    assert p["state"] == "past" and (p["elapsed_days"], p["remaining_days"]) == (365, 0)
    assert p["driven"] == D("14000.0") and p["per_day_driven"] == D("38.4")
    assert p["expected_so_far"] == D("15000.0") and p["ahead"] == D("-1000.0")
    assert p["per_day_to_go"] is None                             # no days remain: not 0


def test_a_year_that_has_not_started_expects_but_has_driven_nothing_yet():
    p = lease.km_pace(a_lease(), 2027, date(2026, 11, 1), None, None)
    assert p["state"] == "not_held"                               # the lease ends on 31 March 2027
    p = lease.km_pace(a_lease(end_date=date(2029, 3, 31)), 2027, date(2026, 11, 1), None, 99999)
    assert p["state"] == "upcoming" and p["expected_km"] == D("15000.0") and p["per_day_needed"] == D("41.0")
    assert p["driven"] is None and p["ahead"] is None and p["per_day_driven"] is None and p["start_km"] is None
    assert (p["elapsed_days"], p["remaining_days"]) == (0, 366)


def test_a_year_the_lease_does_not_cover_has_no_figures():
    p = lease.km_pace(a_lease(), 2020, date(2026, 11, 1), 1, 2)
    assert p["state"] == "not_held" and p["days_held"] == 0
    assert all(p[k] is None for k in ("expected_km", "end_km_needed", "driven", "ahead", "per_day_needed"))


def test_nothing_is_a_zero_when_it_is_not_known():
    none = lease.km_pace(a_lease(estimated_km_per_year=None), 2025, date(2025, 10, 1), 20000, 27000)
    assert none["estimate"] is None and none["state"] == "current"
    assert all(none[k] is None for k in ("expected_km", "end_km_needed", "per_day_needed", "driven", "ahead", "per_day_to_go"))
    no_start = lease.km_pace(a_lease(), 2025, date(2025, 10, 1), None, 27000)
    assert no_start["expected_km"] == D("15000.0") and no_start["per_day_needed"] == D("41.1")
    assert all(no_start[k] is None for k in ("start_km", "end_km_needed", "driven", "ahead", "per_day_driven", "per_day_to_go"))
    no_now = lease.km_pace(a_lease(), 2025, date(2025, 10, 1), 20000, None)
    assert no_now["end_km_needed"] == D("35000.0")
    assert all(no_now[k] is None for k in ("driven", "ahead", "per_day_driven", "per_day_to_go"))
    zero = lease.km_pace(a_lease(estimated_km_per_year=0), 2025, date(2025, 10, 1), 20000, 20000)
    assert zero["expected_km"] == D("0.0") and zero["driven"] == D("0.0") and zero["per_day_needed"] == D("0.0")


def test_an_odometer_that_went_backwards_is_not_negative_distance():
    assert lease.km_pace(a_lease(), 2025, date(2025, 10, 1), 20000, 19990)["driven"] == D("0.0")


# ---- the odometer function the dashboard shares --------------------------------------------------

def moments():
    return [datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 2, 15, 12, tzinfo=UTC), datetime(2026, 3, 1, tzinfo=UTC),
            datetime(2026, 6, 1, tzinfo=UTC)]


def hand(url, car_id, km, when):
    with connect(url) as conn:
        conn.execute("INSERT INTO garagebooks.odometer_reading (car_id, read_at, km) VALUES (%s, %s, %s)", (car_id, when, km))


def agree(url, car_id):
    """The SQL odometer function and sources.odometer_at say the same at several moments."""
    with connect(url) as conn:
        state = sources.detect(conn)
        for when in moments():
            known = sources.odometer_at(conn, state, car_id, when)
            got = conn.execute("SELECT garagebooks.odometer_at(%s, %s) AS km", (car_id, when)).fetchone()["km"]
            assert (None if known is None else float(known.km)) == got, (when, known, got)


def test_the_odometer_function_agrees_with_the_app_with_no_teslamate(app):
    _client, url, car_id = app
    agree(url, car_id)                                              # nothing known: NULL, never 0
    with connect(url) as conn:
        assert conn.execute("SELECT garagebooks.odometer_at(%s, now()) AS km", (car_id,)).fetchone()["km"] is None
    hand(url, car_id, 12000, "2026-01-10 12:00+00")
    hand(url, car_id, 12500, "2026-03-01 00:00+00")
    agree(url, car_id)
    with connect(url) as conn:
        assert conn.execute("SELECT garagebooks.odometer_at(%s, '2026-02-01Z') AS km", (car_id,)).fetchone()["km"] == 12000
        assert conn.execute("SELECT garagebooks.odometer_at(%s, '2025-12-01Z') AS km", (car_id,)).fetchone()["km"] is None


def tracked(url, monkeypatch):
    """The stand-in TeslaMate (odometer 100 on 1 Jan 2026 and 900 on 1 Mar 2026) and its car as the app lists it."""
    with connect(url) as conn:
        conn.execute(STAND_IN)
    client = start(url, monkeypatch)
    client.get("/cars")
    with connect(url) as conn:
        return client, conn.execute("SELECT id FROM garagebooks.car WHERE name = 'Tessie'").fetchone()["id"]


def test_the_odometer_function_follows_teslamate_and_falls_back_to_hand_readings_when_it_is_held_back(empty_db, monkeypatch):
    client, car_id = tracked(empty_db, monkeypatch)
    hand(empty_db, car_id, 9999, "2026-02-01 00:00+00")             # a stale hand reading: ignored while TeslaMate answers
    agree(empty_db, car_id)
    with connect(empty_db) as conn:
        assert conn.execute("SELECT garagebooks.odometer_at(%s, '2026-02-01Z') AS km", (car_id,)).fetchone()["km"] == 100
        assert conn.execute("SELECT garagebooks.odometer_at(%s, '2025-12-31Z') AS km", (car_id,)).fetchone()["km"] is None
        conn.execute("ALTER TABLE public.positions RENAME COLUMN odometer TO odo")      # a table that changed
    client = start(empty_db, monkeypatch)
    client.get("/cars")                                              # the app looks again and rebuilds the function without it
    agree(empty_db, car_id)
    with connect(empty_db) as conn:
        assert conn.execute("SELECT garagebooks.odometer_at(%s, '2026-03-01Z') AS km", (car_id,)).fetchone()["km"] == 9999


def test_the_least_privilege_role_can_switch_the_odometer_function_and_call_it(pg, admin):
    with psycopg.connect(pg.app_url, row_factory=dict_row, autocommit=True) as conn:
        assert conn.execute("SELECT garagebooks.build_odometer_at(true) AS tm").fetchone()["tm"] in (True, False)
        conn.execute("SELECT garagebooks.build_odometer_at(false)")
        assert conn.execute("SELECT garagebooks.odometer_at(1, now()) AS km").fetchone()["km"] is None
        conn.execute("SELECT garagebooks.build_odometer_at(true)")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("DROP FUNCTION garagebooks.odometer_at(integer, timestamptz)")


# ---- the pages ---------------------------------------------------------------------------------------------

def fbt_year_now():
    return lease.fbt_year(today_here())


def figure(body, name):
    """The text of one figure on the kilometre pace section, by its data-figure attribute."""
    found = re.search(rf'data-figure="{name}"[^>]*>(.*?)</dd>', section(body), re.S)
    assert found, (name, section(body)[:1500])
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", found.group(1)))).strip()


def section(body):
    found = re.search(r'<section[^>]*id="km-pace".*?</section>', body, re.S)
    assert found, "no kilometre pace section"
    return found.group(0)


def plain(html):
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", html))).strip()


def add_lease(client, car_id, **over):
    assert client.post(f"/cars/{car_id}/lease/new", data=good_form(**over), headers=ORIGIN).status_code == 303


def test_a_past_fbt_year_shows_the_figures_from_the_readings_by_hand(app):
    client, url, car_id = app
    add_lease(client, car_id, start_date="2024-04-01", end_date="2027-03-31")
    hand(url, car_id, 20000, "2025-03-31 12:00+10")                       # the day before the year: the start odometer
    hand(url, car_id, 34000, "2026-03-31 12:00+10")
    hand(url, car_id, 36000, "2026-05-01 12:00+10")                       # after the year: not this year's
    body = client.get(f"/cars/{car_id}/fbt?year=2025").data.decode()
    assert figure(body, "start") == "1 Apr 2025" and figure(body, "end") == "31 Mar 2026"
    assert figure(body, "estimate") == "15,000 km"
    assert figure(body, "expected") == "15,000 km"
    assert figure(body, "start-odometer") == "20,000 km" and figure(body, "needed-odometer") == "35,000 km"
    assert figure(body, "needed-per-day") == "41.1 km"
    assert figure(body, "driven") == "14,000 km"                          # to the end of the year, not to today
    assert figure(body, "driven-per-day") == "38.4 km"
    assert figure(body, "ahead") == "1,000 km behind"
    assert 'data-figure="to-go"' not in section(body)                     # the year is over


def test_the_current_fbt_year_counts_to_today_and_says_what_is_needed_from_here(app):
    client, url, car_id = app
    year, today = fbt_year_now(), today_here()
    add_lease(client, car_id, start_date=f"{year - 1}-04-01", end_date=f"{year + 2}-03-31")
    start_at = datetime.combine(lease.fbt_start(year), time(0), TZ) - timedelta(hours=1)
    hand(url, car_id, 10000, start_at)
    hand(url, car_id, 10000 + 100 * ((today - lease.fbt_start(year)).days + 1), datetime.now(TZ) - timedelta(minutes=5))
    body = client.get(f"/cars/{car_id}/fbt").data.decode()
    assert figure(body, "start-odometer") == "10,000 km"
    elapsed = (today - lease.fbt_start(year)).days
    expected = lease.km_pace(a_lease(start_date=date(year - 1, 4, 1), end_date=date(year + 2, 3, 31)), year, today,
                             10000, 10000 + 100 * (elapsed + 1))
    assert figure(body, "driven") == f"{expected['driven']:,.0f} km"
    size = abs(expected["ahead"])
    assert figure(body, "ahead") == (f"{size:,.0f} km ahead" if expected["ahead"] > 0 else f"{size:,.0f} km behind")
    if expected["remaining_days"]:
        assert figure(body, "to-go") == f"{expected['per_day_to_go']} km"


def test_a_lease_with_no_estimate_says_so_and_links_to_the_lease_to_edit(app):
    client, url, car_id = app
    add_lease(client, car_id, start_date="2024-04-01", end_date="2027-03-31", estimated_km_per_year="")
    body = client.get(f"/cars/{car_id}/fbt?year=2025").data.decode()
    assert "no estimated km" in plain(section(body)).lower() and "/lease/" in section(body) and "/edit" in section(body)
    assert 'data-figure="expected"' not in section(body)


def test_without_an_odometer_the_figures_that_need_it_say_not_known_and_why(app):
    client, url, car_id = app
    add_lease(client, car_id, start_date="2024-04-01", end_date="2027-03-31")
    body = client.get(f"/cars/{car_id}/fbt?year=2025").data.decode()
    assert figure(body, "expected") == "15,000 km" and figure(body, "needed-per-day") == "41.1 km"
    for name in ("start-odometer", "needed-odometer", "driven", "driven-per-day", "ahead"):
        assert figure(body, name).startswith("not known"), name
    assert "no odometer reading" in plain(section(body)).lower()
    assert "—" not in figure(body, "driven") and "0 km" not in figure(body, "driven")


def test_a_year_the_lease_does_not_cover_has_no_section_of_figures(app):
    client, url, car_id = app
    add_lease(client, car_id, start_date="2025-10-01", end_date="2028-09-30")
    body = client.get(f"/cars/{car_id}/fbt?year=2024").data.decode()
    assert 'id="km-pace"' not in body or 'data-figure="expected"' not in section(body)


def test_the_page_says_when_teslamate_is_held_back_and_uses_the_readings_by_hand(empty_db, monkeypatch):
    client, car_id = tracked(empty_db, monkeypatch)
    add_lease(client, car_id, start_date="2024-04-01", end_date="2027-03-31")
    with connect(empty_db) as conn:
        conn.execute("INSERT INTO public.positions VALUES (1, '2025-03-31 12:00', 5000), (1, '2026-03-30 12:00', 19000)")
    body = client.get(f"/cars/{car_id}/fbt?year=2025").data.decode()
    assert figure(body, "start-odometer") == "5,000 km" and figure(body, "driven") == "14,000 km"
    assert "from TeslaMate" in plain(section(body))
    with connect(empty_db) as conn:
        conn.execute("ALTER TABLE public.positions RENAME COLUMN odometer TO odo")
    client = start(empty_db, monkeypatch)
    body = client.get(f"/cars/{car_id}/fbt?year=2025").data.decode()
    assert figure(body, "start-odometer").startswith("not known") and figure(body, "driven").startswith("not known")
    assert "held back" in plain(section(body)) or "can't be read" in plain(section(body))
    hand(empty_db, car_id, 7000, "2025-03-31 12:00+10")
    hand(empty_db, car_id, 20000, "2026-03-30 12:00+10")
    body = client.get(f"/cars/{car_id}/fbt?year=2025").data.decode()
    assert figure(body, "start-odometer") == "7,000 km" and figure(body, "driven") == "13,000 km"


def test_the_lease_page_has_a_short_version_for_the_current_fbt_year(app):
    client, url, car_id = app
    year = fbt_year_now()
    add_lease(client, car_id, start_date=f"{year - 1}-04-01", end_date=f"{year + 2}-03-31")
    hand(url, car_id, 10000, datetime.combine(lease.fbt_start(year), time(0), TZ) - timedelta(hours=1))
    hand(url, car_id, 10500, datetime.now(TZ) - timedelta(minutes=5))
    page = client.get(f"/cars/{car_id}/lease").data.decode()
    short = re.search(r'id="lease-km-pace".*?</p>', page, re.S)
    assert short, "no short kilometre pace on the lease page"
    text = plain(short.group(0))
    assert lease.fbt_label(year) in text and "500 km" in text and f"/cars/{car_id}/fbt" in short.group(0)


def test_the_lease_page_says_when_there_is_no_estimate(app):
    client, url, car_id = app
    year = fbt_year_now()
    add_lease(client, car_id, start_date=f"{year - 1}-04-01", end_date=f"{year + 2}-03-31", estimated_km_per_year="")
    page = client.get(f"/cars/{car_id}/lease").data.decode()
    assert "no estimated km" in plain(re.search(r'id="lease-km-pace".*?</p>', page, re.S).group(0)).lower()


# ---- the dashboard panel ------------------------------------------------------------------------------------

TITLE = "Kilometre pace"


def shipped():
    return json.loads((ROOT / "grafana" / "cost-of-ownership.json").read_text())


def panel(dash):
    return next(p for p in dash["panels"] if p["title"] == TITLE)


def test_the_panel_has_the_reserved_id_sits_below_the_rest_and_moves_nothing():
    dash = shipped()
    mine = panel(dash)
    assert mine["id"] == 21 and mine["type"] == "table" and mine["gridPos"]["w"] == 24
    others = [p for p in dash["panels"] if p is not mine]
    assert mine["gridPos"]["y"] >= max(p["gridPos"]["y"] + p["gridPos"]["h"] for p in others)
    assert len({p["id"] for p in dash["panels"]}) == len(dash["panels"])


def test_the_panel_reads_no_teslamate_table_itself():
    sql = panel(shipped())["targets"][0]["rawSql"]
    for word in ("positions", "charging_processes", "public."):
        assert word not in sql
    assert "garagebooks.odometer_at" in sql and "garagebooks.lease" in sql


def test_the_panel_is_left_out_until_a_lease_has_an_estimate():
    assert all(p["title"] != TITLE for p in dashboard.render("UTC", "", pace=False)["panels"])
    assert panel(dashboard.render("UTC", "", pace=True))


def test_the_panel_stays_in_the_dashboard_whatever_teslamate_is_doing():
    for form in (None, sources.TeslaMate(sources.ABSENT), sources.TeslaMate(sources.UNTESTED, "held")):
        dash = dashboard.render("UTC", "", teslamate=form)
        assert panel(dash)["id"] == 21 and len({p["id"] for p in dash["panels"]}) == len(dash["panels"])


def run_panel(conn, car_id, form=None):
    dash = dashboard.render("UTC", "", teslamate=form)
    sql = runnable(panel(dash)["targets"][0]["rawSql"].replace("$car_id", str(car_id))).replace("'UTC'", "'Australia/Brisbane'")
    return conn.execute(sql).fetchall()


COLUMNS = {"FBT year": None, "Expected km": "expected_km", "Odometer at start": "start_km", "Odometer needed at end": "end_km_needed",
           "Km per day needed": "per_day_needed", "Driven so far": "driven", "Km per day driven": "per_day_driven",
           "Ahead (+) or behind (-)": "ahead", "Km per day needed from today": "per_day_to_go"}


def app_figures(url, car_id, row):
    """What the app works out for the same lease today: the same odometer function, the same pure figures."""
    year, today = lease.fbt_year(today_here()), today_here()
    with connect(url) as conn:
        state = sources.detect(conn)
        first, last = lease._held_range(row, year)
        start = sources.odometer_at(conn, state, car_id, datetime.combine(first, time(0), TZ))
        now = sources.odometer_at(conn, state, car_id, min(datetime.now(TZ), datetime.combine(last + timedelta(days=1), time(0), TZ)))
    return lease.km_pace(row, year, today, None if start is None else start.km, None if now is None else now.km)


def agree_with_panel(url, car_id, form=None):
    with connect(url) as conn:
        row = conn.execute("SELECT * FROM garagebooks.lease WHERE car_id = %s", (car_id,)).fetchone()
        rows = run_panel(conn, car_id, form)
    assert len(rows) == 1
    expected = app_figures(url, car_id, row)
    for column, key in COLUMNS.items():
        if key is None:
            assert rows[0][column] == lease.fbt_label(lease.fbt_year(today_here()))
        else:
            assert rows[0][column] == expected[key], (column, rows[0][column], expected[key])
    return rows[0], expected


def lease_now(client, car_id, **over):
    year = fbt_year_now()
    add_lease(client, car_id, start_date=f"{year - 1}-10-01", end_date=f"{year + 2}-09-30", **over)
    return year


def test_the_panel_agrees_with_the_app_by_hand_readings(app):
    client, url, car_id = app
    year = lease_now(client, car_id)
    hand(url, car_id, 10000, datetime.combine(lease.fbt_start(year), time(0), TZ) - timedelta(hours=1))
    row, expected = agree_with_panel(url, car_id)                    # a start but no current reading yet... it is the start
    assert expected["driven"] == D("0.0") and row["Driven so far"] == D("0.0")
    hand(url, car_id, 10750, datetime.now(TZ) - timedelta(minutes=5))
    row, expected = agree_with_panel(url, car_id)
    assert row["Driven so far"] == D("750.0") and row["Expected km"] == expected["expected_km"] == D("15000.0")


def test_the_panel_says_not_known_as_null_and_never_zero(app):
    client, url, car_id = app
    lease_now(client, car_id)
    row, expected = agree_with_panel(url, car_id)
    for column in ("Odometer at start", "Odometer needed at end", "Driven so far", "Km per day driven", "Ahead (+) or behind (-)",
                   "Km per day needed from today"):
        assert row[column] is None, column
    assert row["Expected km"] == D("15000.0") and row["Km per day needed"] is not None
    assert expected["driven"] is None


def test_the_panel_shows_nothing_for_a_lease_with_no_estimate_or_another_car(app):
    client, url, car_id = app
    lease_now(client, car_id, estimated_km_per_year="")
    with connect(url) as conn:
        assert run_panel(conn, car_id) == []
    with connect(url) as conn:
        assert run_panel(conn, -1) == []


@pytest.mark.parametrize("form", [sources.TeslaMate(sources.ABSENT), sources.TeslaMate(sources.UNTESTED, "held")])
def test_every_form_of_the_dashboard_runs_the_new_query(app, form):
    client, url, car_id = app
    lease_now(client, car_id)
    with connect(url) as conn:
        dash = dashboard.render("UTC", "", teslamate=form)
        for sql in queries(dash):
            conn.execute(runnable(sql)).fetchall()


def test_the_panel_agrees_with_the_app_for_a_car_teslamate_tracks_and_when_it_is_held_back(empty_db, monkeypatch):
    client, car_id = tracked(empty_db, monkeypatch)
    year = lease_now(client, car_id)
    begin = datetime.combine(lease.fbt_start(year), time(0), TZ).astimezone(UTC).replace(tzinfo=None)
    with connect(empty_db) as conn:
        conn.execute("INSERT INTO public.positions VALUES (1, %s, 40000), (1, %s, 40900)",
                     (begin - timedelta(days=1), datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=5)))
    hand(empty_db, car_id, 1, "2020-01-01 00:00+00")                 # a stale hand reading: ignored while TeslaMate answers
    row, expected = agree_with_panel(empty_db, car_id)
    assert row["Odometer at start"] == D("40000.0") and row["Driven so far"] == D("900.0")
    with connect(empty_db) as conn:
        conn.execute("ALTER TABLE public.positions RENAME COLUMN odometer TO odo")      # held back: its figures are not used
    client = start(empty_db, monkeypatch)
    client.get("/cars")
    row, expected = agree_with_panel(empty_db, car_id, sources.TeslaMate(sources.UNAVAILABLE, "held"))
    assert row["Odometer at start"] == D("1.0") and row["Driven so far"] == D("0.0")    # only the stale reading from 2020 is left
