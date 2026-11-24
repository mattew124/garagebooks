"""Reminders (roadmap item 4), on a real database: the table and the view, what is checked on entry,
the pages, resolving from either side, recurrence, un-resolving, the reminders derived from a lease,
the odometer from TeslaMate or by hand, the least-privilege role and the dashboard panel.
All figures are invented. Most tests run on a database with no TeslaMate, so they pass both ways."""

import json
import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import psycopg
import pytest
from psycopg.rows import dict_row

from garagebooks import create_app, dashboard, db, reminders, sources
from tests.test_lease import good_form, today_here
from tests.test_sources import STAND_IN, runnable

ORIGIN = {"Origin": "http://localhost"}
TZ = ZoneInfo("Australia/Brisbane")


def connect(url):
    return psycopg.connect(url, row_factory=dict_row, autocommit=True)


def start(url, monkeypatch, **env):
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("APP_TZ", "Australia/Brisbane")
    for key in ("SKIP_MIGRATE", "DASHBOARD_DIR", "BASE_PATH", "ALLOW_UNTESTED_TESLAMATE"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    app = create_app()
    app.testing = True
    return app.test_client()


@pytest.fixture()
def app(empty_db, monkeypatch):
    client = start(empty_db, monkeypatch)
    assert client.post("/cars", data={"name": "Rem car", "kind": "BEV"}, headers=ORIGIN).status_code == 303
    with connect(empty_db) as conn:
        car_id = conn.execute("SELECT id FROM garagebooks.car").fetchone()["id"]
    return client, empty_db, car_id


def day(offset):
    return (today_here() + timedelta(days=offset)).isoformat()


def form(car_id, /, **over):
    return {"car_id": str(car_id), "title": "Tyre rotation", "category_id": "", "due_date": day(100), "due_km": "",
            "lead_days": "", "lead_km": "", "every_months": "", "every_km": "", "notes": "", **over}


def add(client, url, car_id, **over):
    response = client.post("/reminders", data=form(car_id, **over), headers=ORIGIN)
    assert response.status_code == 303, response.data.decode()[:2000]
    with connect(url) as conn:
        return conn.execute("SELECT max(id) AS id FROM garagebooks.reminder").fetchone()["id"]


def category(url, name):
    with connect(url) as conn:
        return conn.execute("SELECT id FROM garagebooks.category WHERE name = %s", (name,)).fetchone()["id"]


def expense(url, car_id, cat="Service", when="2026-10-05 10:00+10", km=None, vendor="Tyre shop"):
    with connect(url) as conn:
        return conn.execute(
            "INSERT INTO garagebooks.expense (car_id, category_id, occurred_at, amount, odometer_km, vendor) "
            "VALUES (%s, %s, %s, 80, %s, %s) RETURNING id", (car_id, category(url, cat), when, km, vendor)).fetchone()["id"]


def reading(url, car_id, km, when="2026-10-01 12:00+10"):
    with connect(url) as conn:
        conn.execute("INSERT INTO garagebooks.odometer_reading (car_id, read_at, km) VALUES (%s, %s, %s)", (car_id, when, km))


def one(url, sql, *args):
    with connect(url) as conn:
        return conn.execute(sql, args).fetchone()


def statuses(page):
    return re.findall(r'data-status="(\w+)"', page)


def resolve(client, rid, **data):
    return client.post(f"/reminders/{rid}/resolve", data={"expense_id": "", "date": "", "km": "", **data}, headers=ORIGIN)


# ---- the table and the view ---------------------------------------------------------

def test_the_table_insists_on_a_date_or_an_odometer_and_keeps_its_links_tidy(app):
    _, url, car_id = app
    with connect(url) as conn:
        for sql in ("INSERT INTO garagebooks.reminder (car_id, title) VALUES (%s, 'x')",
                    "INSERT INTO garagebooks.reminder (car_id, title, due_km, lead_days) VALUES (%s, 'x', 5, -1)",
                    "INSERT INTO garagebooks.reminder (car_id, title, due_km, every_km) VALUES (%s, 'x', 5, 0)",
                    "INSERT INTO garagebooks.reminder (car_id, title, due_km, every_months) VALUES (%s, 'x', 5, 0)",
                    "INSERT INTO garagebooks.reminder (car_id, title, due_km) VALUES (%s, '  ', 5)"):
            with pytest.raises(psycopg.errors.CheckViolation):
                conn.execute(sql, (car_id,))
        cat = conn.execute("INSERT INTO garagebooks.category (name) VALUES ('Temp') RETURNING id").fetchone()["id"]
        rid = conn.execute("INSERT INTO garagebooks.reminder (car_id, title, due_km, category_id) VALUES (%s, 'x', 5, %s) "
                           "RETURNING id", (car_id, cat)).fetchone()["id"]
        conn.execute("DELETE FROM garagebooks.category WHERE id = %s", (cat,))
        assert conn.execute("SELECT category_id FROM garagebooks.reminder WHERE id = %s", (rid,)).fetchone()["category_id"] is None
        conn.execute("DELETE FROM garagebooks.car WHERE id = %s", (car_id,))
        assert conn.execute("SELECT count(*) AS n FROM garagebooks.reminder").fetchone()["n"] == 0


def test_the_view_lists_open_reminders_with_their_lead_times(app):
    client, url, car_id = app
    open_id = add(client, url, car_id, title="Open", due_km="50000", lead_days="14", lead_km="250")
    done = add(client, url, car_id, title="Done")
    assert resolve(client, done, date=day(-1)).status_code == 303
    with connect(url) as conn:
        rows = conn.execute("SELECT * FROM garagebooks.reminder_due").fetchall()
    assert [(r["kind"], r["reminder_id"], r["title"], r["due_km"], r["lead_days"], r["lead_km"]) for r in rows] == [
        ("reminder", open_id, "Open", 50000, 14, 250)]
    assert {"car_id", "title", "kind", "due_date", "due_km", "lead_days", "lead_km"} <= set(rows[0])


def test_registration_and_lease_end_come_from_the_current_lease_and_follow_it(app):
    client, url, car_id = app
    assert client.post(f"/cars/{car_id}/lease/new", data=good_form(), headers=ORIGIN).status_code == 303
    with connect(url) as conn:
        lease = conn.execute("SELECT * FROM garagebooks.lease").fetchone()
        derived = {r["kind"]: r for r in conn.execute("SELECT * FROM garagebooks.reminder_due ORDER BY kind")}
    assert set(derived) == {"registration", "lease_end"}
    assert derived["registration"]["due_date"] == lease["rego_expiry"] and derived["registration"]["informational"]
    assert derived["lease_end"]["due_date"] == lease["end_date"] and derived["lease_end"]["lead_days"] == 90
    assert derived["lease_end"]["reminder_id"] is None and not derived["lease_end"]["informational"]
    # changing the lease changes the reminders: they are not copies
    changed = good_form(rego_expiry=day(40), rego_renewed_by_provider="", end_date=day(500))
    assert client.post(f"/cars/{car_id}/lease/{lease['id']}/edit", data=changed, headers=ORIGIN).status_code == 303
    with connect(url) as conn:
        derived = {r["kind"]: r for r in conn.execute("SELECT * FROM garagebooks.reminder_due")}
    assert derived["registration"]["due_date"] == today_here() + timedelta(days=40) and not derived["registration"]["informational"]
    assert derived["lease_end"]["due_date"] == today_here() + timedelta(days=500)
    # no rego date, no registration reminder; no lease, none at all
    assert client.post(f"/cars/{car_id}/lease/{lease['id']}/edit", data=good_form(rego_expiry=""), headers=ORIGIN).status_code == 303
    assert [r["kind"] for r in connect(url).execute("SELECT kind FROM garagebooks.reminder_due")] == ["lease_end"]
    assert client.post(f"/cars/{car_id}/lease/{lease['id']}/delete", headers=ORIGIN).status_code == 303
    assert connect(url).execute("SELECT count(*) AS n FROM garagebooks.reminder_due").fetchone()["n"] == 0


def test_only_the_current_lease_counts_not_one_that_has_ended(app):
    client, url, car_id = app
    old = good_form(start_date=day(-900), end_date=day(-300), rego_expiry=day(-310), term_months="24")
    assert client.post(f"/cars/{car_id}/lease/new", data=old, headers=ORIGIN).status_code == 303
    assert connect(url).execute("SELECT count(*) AS n FROM garagebooks.reminder_due").fetchone()["n"] == 0
    assert client.post(f"/cars/{car_id}/lease/new", data=good_form(start_date=day(-200)), headers=ORIGIN).status_code == 303
    assert connect(url).execute("SELECT count(*) AS n FROM garagebooks.reminder_due").fetchone()["n"] == 2


def test_a_lease_followed_by_the_next_one_says_so_and_never_falls_due(app):
    """Two consecutive leases of one car: the first one's end is information (the next is already loaded), not a
    reminder that falls due, is told or counts in the header."""
    client, url, car_id = app
    ending = good_form(start_date=day(-700), end_date=day(10), rego_expiry="", term_months="24")
    following = good_form(start_date=day(11), end_date=day(741), rego_expiry="", term_months="24")
    for f in (ending, following):
        assert client.post(f"/cars/{car_id}/lease/new", data=f, headers=ORIGIN).status_code == 303
    with connect(url) as conn:
        rows = conn.execute("SELECT * FROM garagebooks.reminder_due WHERE kind = 'lease_end'").fetchall()
    assert len(rows) == 1
    row = rows[0]
    assert row["due_date"] == today_here() + timedelta(days=10) and row["informational"]
    assert row["next_lease_start"] == today_here() + timedelta(days=11)
    page = client.get("/reminders").data.decode()
    card = re.search(r'data-kind="lease_end".*?</li>', page, re.S).group(0)
    assert 'data-status="info"' in card and "Next lease loaded" in card
    starts = today_here() + timedelta(days=11)
    assert f"next lease already loaded (starts {starts.day} {starts:%b %Y})" in card
    assert "overdue" not in card and "due in" not in card
    assert '<span class="count">' not in page                               # nothing needs attention
    # a gap of more than a week is not a follow-on: the end is a reminder as before
    with connect(url) as conn:
        conn.execute("UPDATE garagebooks.lease SET start_date = %s WHERE start_date = %s",
                     (today_here() + timedelta(days=30), starts))
        row = conn.execute("SELECT * FROM garagebooks.reminder_due WHERE kind = 'lease_end'").fetchone()
    assert not row["informational"] and row["next_lease_start"] is None


def test_the_current_lease_is_chosen_on_the_apps_day_not_the_databases(empty_db, monkeypatch):
    """The view judges "today" in APP_TZ (stored by the app), not the database server's UTC day. UTC+14 and
    UTC-12 are always on different days, so a lease ending on the UTC-12 day has ended at UTC+14."""
    try:
        for tz, current in (("Etc/GMT+12", "ending"), ("Etc/GMT-14", "following")):
            client = start(empty_db, monkeypatch, APP_TZ=tz)
            with connect(empty_db) as conn:
                assert conn.execute("SELECT app_tz FROM garagebooks.setting").fetchone()["app_tz"] == tz
                conn.execute("DELETE FROM garagebooks.lease")
                conn.execute("INSERT INTO garagebooks.car (id, name) VALUES (1600, 'Day car') ON CONFLICT DO NOTHING")
                end = conn.execute("SELECT (now() AT TIME ZONE 'Etc/GMT+12')::date AS d").fetchone()["d"]
                for start_date, end_date in ((end - timedelta(days=700), end),
                                             (end + timedelta(days=1), end + timedelta(days=731))):
                    conn.execute("INSERT INTO garagebooks.lease (car_id, financier, start_date, end_date, term_months, "
                                 "pay_frequency) VALUES (1600, 'Provider', %s, %s, 24, 'monthly')", (start_date, end_date))
                row = conn.execute("SELECT * FROM garagebooks.reminder_due WHERE kind = 'lease_end'").fetchone()
            assert row["due_date"] == (end if current == "ending" else end + timedelta(days=731)), tz
            assert client.get("/reminders").status_code == 200
    finally:
        with connect(empty_db) as conn:
            conn.execute("DELETE FROM garagebooks.lease")
            conn.execute("DELETE FROM garagebooks.car WHERE id = 1600")
            conn.execute("UPDATE garagebooks.setting SET app_tz = NULL")


def test_upgrading_twice_keeps_reminders_and_the_view(app, monkeypatch):
    client, url, car_id = app
    rid = add(client, url, car_id, title="Kept")
    db.migrate()
    start(url, monkeypatch)
    start(url, monkeypatch)
    assert one(url, "SELECT title FROM garagebooks.reminder WHERE id = %s", rid)["title"] == "Kept"
    assert one(url, "SELECT count(*) AS n FROM garagebooks.reminder_due")["n"] == 1


# ---- what is checked on entry -----------------------------------------------------------

@pytest.mark.parametrize("over, field, text", [
    ({"title": "  "}, "title", "Enter what"),
    ({"title": "x" * 101}, "title", "too long"),
    ({"due_date": "", "due_km": ""}, "due_date", "date, an odometer"),
    ({"due_date": "31/12/2026"}, "due_date", "valid"),
    ({"due_date": "2026-02-30"}, "due_date", "valid"),
    ({"due_date": "", "due_km": "lots"}, "due_km", "number of km"),
    ({"due_date": "", "due_km": "-5"}, "due_km", "out of range"),
    ({"due_date": "", "due_km": "99999999"}, "due_km", "out of range"),
    ({"lead_days": "-1"}, "lead_days", "whole number"),
    ({"lead_days": "1.5"}, "lead_days", "whole number"),
    ({"lead_days": "99999"}, "lead_days", "whole number"),
    ({"lead_km": "-1"}, "lead_km", "km"),
    ({"lead_km": "abc"}, "lead_km", "km"),
    ({"every_months": "0"}, "every_months", "whole number"),
    ({"every_months": "700"}, "every_months", "whole number"),
    ({"every_months": "six"}, "every_months", "whole number"),
    ({"every_km": "0"}, "every_km", "more than 0"),
    ({"every_km": "-4"}, "every_km", "more than 0"),
    ({"notes": "n" * 2001}, "notes", "too long"),
    ({"category_id": "99999"}, "category_id", "category"),
    ({"car_id": ""}, "car_id", "Choose the car"),
    ({"car_id": "999999"}, "car_id", "Choose the car"),
])
def test_each_field_is_checked_with_the_error_beside_it_and_the_input_kept(app, over, field, text):
    client, url, car_id = app
    response = client.post("/reminders", data=form(car_id, **{"notes": "Keep me", **over}), headers=ORIGIN)
    page = response.data.decode()
    assert response.status_code == 400
    assert re.search(rf'id="e-{field}"[^>]*>[^<]*{re.escape(text)}', page), page[:3000]
    assert connect(url).execute("SELECT count(*) AS n FROM garagebooks.reminder").fetchone()["n"] == 0
    if field != "notes":
        assert "Keep me" in page            # what was typed is still there


def test_blank_lead_times_get_the_defaults_and_numbers_are_read_loosely(app):
    client, url, car_id = app
    rid = add(client, url, car_id, due_date="", due_km="12,345.5 km", every_km="10,000", every_months="6")
    row = one(url, "SELECT * FROM garagebooks.reminder WHERE id = %s", rid)
    assert (row["lead_days"], row["lead_km"], row["due_km"], row["every_km"], row["every_months"], row["due_date"]) == (
        30, 1000, 12345.5, 10000, 6, None)


# ---- the Reminders page -----------------------------------------------------------------------

def test_the_page_lists_the_most_urgent_first_with_a_word_for_each_status(app):
    client, url, car_id = app
    add(client, url, car_id, title="Later", due_date=day(200))
    add(client, url, car_id, title="Late", due_date=day(-3))
    add(client, url, car_id, title="Soon", due_date=day(10))
    add(client, url, car_id, title="By km", due_date="", due_km="30000")
    page = client.get("/reminders").data.decode()
    assert statuses(page)[:4] == ["overdue", "due_soon", "unknown", "upcoming"]
    assert [m for m in re.findall(r'class="reminder card"[^>]*data-title="([^"]+)"', page)][:4] == ["Late", "Soon", "By km", "Later"]
    assert "Overdue" in page and "Due soon" in page and "Upcoming" in page
    assert "3 days overdue" in page and "due in 10 days" in page
    assert "Odometer unknown" in page and "not recorded" in page          # said, never treated as not due
    assert client.get("/reminders").status_code == 200


def test_an_odometer_reminder_uses_the_latest_reading_by_hand(app):
    client, url, car_id = app
    add(client, url, car_id, title="Service", due_date="", due_km="30000")
    reading(url, car_id, 29_500)
    page = client.get("/reminders").data.decode()
    assert statuses(page)[:1] == ["due_soon"] and "500 km to go" in page
    assert "29,500 km" in page and "entered" in page
    reading(url, car_id, 30_200, when="2026-10-10 12:00+10")
    assert statuses(client.get("/reminders").data.decode())[:1] == ["overdue"]
    assert "200 km over" in client.get("/reminders").data.decode()


def test_the_nav_counts_what_is_overdue_or_due_soon_and_the_list_page_says_so(app):
    client, url, car_id = app
    assert not re.search(r'class="count"', client.get("/").data.decode())
    add(client, url, car_id, title="Fine", due_date=day(300))
    assert not re.search(r'class="count"', client.get("/").data.decode())
    add(client, url, car_id, title="Late", due_date=day(-1))
    add(client, url, car_id, title="Soon", due_date=day(2))
    add(client, url, car_id, title="Km", due_date="", due_km="9000")           # unknown: counts, never hidden
    page = client.get("/").data.decode()
    assert re.search(r'Reminders\s*<span class="count"[^>]*>\s*3\s*<', page)
    assert 'id="reminder-notice"' in page and "1 overdue" in page
    assert 'id="reminder-notice"' not in client.get("/reminders").data.decode()   # the page itself says it already


def test_an_empty_page_says_so_and_a_missing_edit_page_is_a_404(app):
    client, _, _ = app
    page = client.get("/reminders").data.decode()
    assert "No open reminders" in page and "Resolved" in page
    assert client.get("/reminders/999/edit").status_code == 404
    assert client.post("/reminders/999/edit", data=form(1), headers=ORIGIN).status_code == 404
    assert client.post("/reminders/999/delete", headers=ORIGIN).status_code == 404
    assert client.post("/reminders/999/resolve", data={}, headers=ORIGIN).status_code == 404
    assert client.post("/reminders/999/unresolve", headers=ORIGIN).status_code == 404
    assert client.post("/reminders", data=form(1), headers={}).status_code == 403          # same-origin guard


def test_edit_and_delete_an_open_reminder_but_not_a_resolved_one(app):
    client, url, car_id = app
    rid = add(client, url, car_id, title="Old title")
    assert b"Old title" in client.get(f"/reminders/{rid}/edit").data
    saved = client.post(f"/reminders/{rid}/edit", data=form(car_id, title="New title", due_date=day(5), lead_days="7"),
                        headers=ORIGIN)
    assert saved.status_code == 303
    row = one(url, "SELECT * FROM garagebooks.reminder WHERE id = %s", rid)
    assert (row["title"], row["lead_days"], row["edited_at"] is not None) == ("New title", 7, True)
    bad = client.post(f"/reminders/{rid}/edit", data=form(car_id, title=""), headers=ORIGIN)
    assert bad.status_code == 400 and b"Enter what" in bad.data
    assert client.post(f"/reminders/{rid}/delete", headers=ORIGIN).status_code == 303
    assert one(url, "SELECT count(*) AS n FROM garagebooks.reminder")["n"] == 0
    kept = add(client, url, car_id, title="Resolved one")
    assert resolve(client, kept, date=day(-1)).status_code == 303
    assert client.get(f"/reminders/{kept}/edit").status_code == 404
    assert client.post(f"/reminders/{kept}/delete", headers=ORIGIN).status_code == 404
    assert one(url, "SELECT count(*) AS n FROM garagebooks.reminder")["n"] == 1


def test_the_delete_button_asks_first_and_derived_ones_have_no_buttons(app):
    client, url, car_id = app
    add(client, url, car_id, title="Mine")
    client.post(f"/cars/{car_id}/lease/new", data=good_form(), headers=ORIGIN)
    page = client.get("/reminders").data.decode()
    assert re.search(r'data-confirm="Delete[^"]*Mine', page)
    assert page.count("/delete") == 1                                   # only the stored one
    assert page.count("/resolve") == 1
    assert "Registration expires" in page and "Lease ends" in page and "renewed by the provider" in page
    assert f'/cars/{car_id}/lease' in page
    registration = re.search(r'data-kind="registration".*?</li>', page, re.S).group(0)
    assert "informational" in registration or 'data-status="info"' in registration


# ---- resolving, from the reminder -------------------------------------------------------------

def test_resolving_with_an_expense_links_it_keeps_the_reminder_and_shows_it_in_resolved(app):
    client, url, car_id = app
    rid = add(client, url, car_id, title="Brakes", due_date=day(5))
    eid = expense(url, car_id, "Tyres", vendor="Fix-It Garage", km=30_500)
    response = resolve(client, rid, expense_id=str(eid))
    assert response.status_code == 303
    row = one(url, "SELECT * FROM garagebooks.reminder WHERE id = %s", rid)
    assert row["resolved_expense_id"] == eid and row["resolved_at"] is not None and row["resolved_km"] == 30_500
    page = client.get("/reminders").data.decode()
    assert "No open reminders" in page
    resolved = page.split('id="resolved"')[1]
    assert "Brakes" in resolved and "Fix-It Garage" in resolved and f"/{eid}/edit" in resolved
    assert connect(url).execute("SELECT count(*) AS n FROM garagebooks.reminder_due").fetchone()["n"] == 0
    # resolving twice is refused, and the expense can go without losing the reminder
    assert resolve(client, rid, expense_id=str(eid)).status_code == 404
    connect(url).execute("DELETE FROM garagebooks.expense WHERE id = %s", (eid,))
    row = one(url, "SELECT * FROM garagebooks.reminder WHERE id = %s", rid)
    assert row["resolved_at"] is not None and row["resolved_expense_id"] is None


def test_another_cars_expense_cannot_resolve_a_reminder(app):
    client, url, car_id = app
    client.post("/cars", data={"name": "Other", "kind": "ICE"}, headers=ORIGIN)
    other = one(url, "SELECT max(id) AS id FROM garagebooks.car")["id"]
    rid = add(client, url, car_id)
    foreign = expense(url, other)
    response = resolve(client, rid, expense_id=str(foreign))
    assert response.status_code == 400 and b"this car" in response.data
    assert resolve(client, rid, expense_id="999999").status_code == 400
    assert resolve(client, rid, expense_id="abc").status_code == 400
    assert one(url, "SELECT resolved_at FROM garagebooks.reminder WHERE id = %s", rid)["resolved_at"] is None


def test_resolving_without_an_expense_needs_a_date_and_takes_an_optional_odometer(app):
    client, url, car_id = app
    rid = add(client, url, car_id)
    for data in ({}, {"date": "nonsense"}, {"date": day(-2), "km": "far"}, {"date": day(3)}):
        assert resolve(client, rid, **data).status_code == 400
    assert resolve(client, rid, date=day(-2), km="31,000").status_code == 303
    row = one(url, "SELECT * FROM garagebooks.reminder WHERE id = %s", rid)
    assert row["resolved_expense_id"] is None and row["resolved_km"] == 31_000
    assert row["resolved_at"].astimezone(TZ).date() == today_here() - timedelta(days=2)


# ---- recurrence --------------------------------------------------------------------------

def recurring(client, url, car_id, **over):
    return add(client, url, car_id, title="Tyre rotation", due_date=day(-5), due_km="40000", every_km="10000",
               every_months="6", lead_days="14", lead_km="500", category_id=str(category(url, "Service")),
               notes="Front to back", **over)


def next_of(url, rid):
    return one(url, "SELECT * FROM garagebooks.reminder WHERE previous_id = %s", rid)


def test_resolving_a_recurring_reminder_makes_the_next_one_from_the_expenses_date_and_odometer(app):
    client, url, car_id = app
    rid = recurring(client, url, car_id)
    eid = expense(url, car_id, when="2026-10-31 10:00+10", km=40_250)
    resolve(client, rid, expense_id=str(eid))
    nxt = next_of(url, rid)
    assert (nxt["due_date"], nxt["due_km"], nxt["title"], nxt["lead_days"], nxt["lead_km"], nxt["every_km"],
            nxt["every_months"], nxt["notes"], nxt["resolved_at"]) == (
        date(2027, 4, 30), 50_250, "Tyre rotation", 14, 500, 10_000, 6, "Front to back", None)    # 31 Oct + 6 months: month end
    assert nxt["category_id"] == category(url, "Service") and nxt["car_id"] == car_id


def test_the_next_one_chains_on_so_the_history_can_be_traced(app):
    client, url, car_id = app
    first = recurring(client, url, car_id)
    resolve(client, first, date=day(-1), km="40,100")
    second = next_of(url, first)["id"]
    resolve(client, second, date=day(0), km="50,200")
    third = next_of(url, second)
    assert third["previous_id"] == second and third["due_km"] == 60_200 and second != first


@pytest.mark.parametrize("setup, expected", [
    ("expense", 51_000),     # the expense's own odometer, 41,000, plus 10,000
    ("entered", 52_000),     # typed when resolving, no expense
    ("reading", 53_000),     # a reading entered by hand before the resolution date
    ("none", 50_000),        # nothing known: counted from the previous due_km
])
def test_the_next_km_counts_from_the_best_odometer_known(app, setup, expected):
    client, url, car_id = app
    rid = recurring(client, url, car_id)
    if setup == "expense":
        resolve(client, rid, expense_id=str(expense(url, car_id, km=41_000)))
    elif setup == "entered":
        resolve(client, rid, date=day(-1), km="42000")
    elif setup == "reading":
        reading(url, car_id, 43_000, when=f"{day(-3)} 12:00+10")
        resolve(client, rid, date=day(-1))
    else:
        resolve(client, rid, date=day(-1))
    assert next_of(url, rid)["due_km"] == expected


def test_a_one_off_makes_no_next_and_a_recurrence_with_nothing_to_count_from_says_so(app):
    client, url, car_id = app
    once = add(client, url, car_id)
    resolve(client, once, date=day(-1))
    assert next_of(url, once) is None
    odd = add(client, url, car_id, due_date=day(1), every_km="5000")          # km recurrence, but no odometer anywhere
    response = resolve(client, odd, date=day(-1))
    assert next_of(url, odd) is None
    assert "no odometer" in client.get(response.location).data.decode().lower()


def test_a_date_only_recurrence_adds_calendar_months(app):
    client, url, car_id = app
    rid = add(client, url, car_id, title="Rego", due_date=day(-1), every_months="12")
    eid = expense(url, car_id, when="2028-02-29 09:00+10")
    resolve(client, rid, expense_id=str(eid))
    nxt = next_of(url, rid)
    assert nxt["due_date"] == date(2029, 2, 28) and nxt["due_km"] is None


# ---- un-resolving ---------------------------------------------------------------------------------

def test_unresolving_removes_the_link_and_the_untouched_next_one(app):
    client, url, car_id = app
    rid = recurring(client, url, car_id)
    resolve(client, rid, expense_id=str(expense(url, car_id, km=40_000)))
    assert next_of(url, rid) is not None
    response = client.post(f"/reminders/{rid}/unresolve", headers=ORIGIN)
    assert response.status_code == 303
    row = one(url, "SELECT * FROM garagebooks.reminder WHERE id = %s", rid)
    assert (row["resolved_at"], row["resolved_expense_id"], row["resolved_km"]) == (None, None, None)
    assert next_of(url, rid) is None
    assert "reopened" in client.get(response.location).data.decode().lower()
    assert client.post(f"/reminders/{rid}/unresolve", headers=ORIGIN).status_code == 404     # not resolved now


def test_unresolving_leaves_a_next_one_that_was_edited_or_resolved_and_says_so(app):
    client, url, car_id = app
    rid = recurring(client, url, car_id)
    resolve(client, rid, date=day(-1), km="40,000")
    nxt = next_of(url, rid)["id"]
    client.post(f"/reminders/{nxt}/edit", data=form(car_id, title="Edited", due_date=day(50)), headers=ORIGIN)
    response = client.post(f"/reminders/{rid}/unresolve", headers=ORIGIN)
    assert next_of(url, rid)["id"] == nxt and "left" in client.get(response.location).data.decode()
    # resolving again must not make a second next one
    resolve(client, rid, date=day(-1), km="40,000")
    assert one(url, "SELECT count(*) AS n FROM garagebooks.reminder WHERE previous_id = %s", rid)["n"] == 1
    # a next one that has itself been resolved is kept too
    rid2 = recurring(client, url, car_id)
    resolve(client, rid2, date=day(-1), km="1")
    resolve(client, next_of(url, rid2)["id"], date=day(0))
    client.post(f"/reminders/{rid2}/unresolve", headers=ORIGIN)
    assert next_of(url, rid2) is not None


# ---- resolving, from the expense ---------------------------------------------------------------------

def test_saving_an_expense_offers_the_cars_open_reminders_with_the_matching_category_ticked(app):
    client, url, car_id = app
    match = add(client, url, car_id, title="Rotation due", category_id=str(category(url, "Service")))
    other = add(client, url, car_id, title="Insurance renewal", category_id=str(category(url, "Insurance")))
    data = {"car_id": str(car_id), "category_id": str(category(url, "Service")), "date": day(0), "time": "09:00",
            "amount": "90", "odometer_km": "41000", "vendor": "Tyre shop", "invoice_url": "", "notes": ""}
    saved = client.post("/new", data=data, headers=ORIGIN)
    assert saved.status_code == 303
    eid = one(url, "SELECT max(id) AS id FROM garagebooks.expense")["id"]
    assert saved.location.endswith(f"/{eid}/reminders") or f"/{eid}/reminders" in saved.location
    page = client.get(saved.location).data.decode()
    assert re.search(rf'value="{match}"[^>]*checked', page) and not re.search(rf'value="{other}"[^>]*checked', page)
    done = client.post(f"/{eid}/reminders", data={"reminder_id": [str(match)]}, headers=ORIGIN)
    assert done.status_code == 303
    row = one(url, "SELECT * FROM garagebooks.reminder WHERE id = %s", match)
    assert row["resolved_expense_id"] == eid and row["resolved_km"] == 41_000
    assert one(url, "SELECT resolved_at FROM garagebooks.reminder WHERE id = %s", other)["resolved_at"] is None


def test_an_expense_for_a_car_with_nothing_open_goes_straight_back_to_the_list(app):
    client, url, car_id = app
    data = {"car_id": str(car_id), "category_id": str(category(url, "Tyres")), "date": day(0), "time": "09:00",
            "amount": "90", "odometer_km": "", "vendor": "", "invoice_url": "", "notes": ""}
    saved = client.post("/new", data=data, headers=ORIGIN)
    assert saved.status_code == 303 and "/reminders" not in saved.location.split("?")[0]


def test_the_edit_page_links_to_the_offer_and_the_offer_refuses_what_is_not_theirs(app):
    client, url, car_id = app
    client.post("/cars", data={"name": "Other", "kind": "ICE"}, headers=ORIGIN)
    other = one(url, "SELECT max(id) AS id FROM garagebooks.car")["id"]
    mine = add(client, url, car_id, title="Mine")
    theirs = add(client, url, other, title="Theirs")
    eid = expense(url, car_id)
    assert f"/{eid}/reminders" in client.get(f"/{eid}/edit").data.decode()
    assert client.get("/999999/reminders").status_code == 404
    assert client.post("/999999/reminders", data={}, headers=ORIGIN).status_code == 404
    refused = client.post(f"/{eid}/reminders", data={"reminder_id": [str(theirs)]}, headers=ORIGIN)
    assert refused.status_code == 400
    assert one(url, "SELECT resolved_at FROM garagebooks.reminder WHERE id = %s", theirs)["resolved_at"] is None
    assert client.post(f"/{eid}/reminders", data={"reminder_id": ["x"]}, headers=ORIGIN).status_code == 400
    ok = client.post(f"/{eid}/reminders", data={"reminder_id": [str(mine)]}, headers=ORIGIN)
    assert ok.status_code == 303 and one(url, "SELECT resolved_at FROM garagebooks.reminder WHERE id = %s", mine)["resolved_at"]


def test_resolved_reminders_are_paginated_newest_first(app):
    client, url, car_id = app
    with connect(url) as conn:
        for n in range(reminders.RESOLVED_PAGE + 3):
            conn.execute("INSERT INTO garagebooks.reminder (car_id, title, due_km, resolved_at) "
                         "VALUES (%s, %s, 5, now() - make_interval(days => %s))", (car_id, f"R{n:02d}", n))
    first = client.get("/reminders").data.decode()
    assert "R00" in first and f"R{reminders.RESOLVED_PAGE:02d}" not in first and "page=2" in first
    second = client.get("/reminders?page=2").data.decode()
    assert f"R{reminders.RESOLVED_PAGE:02d}" in second and "R00" not in second
    assert client.get("/reminders?page=banana").status_code == 200


# ---- the odometer: TeslaMate or by hand --------------------------------------------------------------------

def test_a_car_teslamate_tracks_uses_its_odometer_and_a_car_by_hand_uses_its_readings(empty_db, monkeypatch):
    with connect(empty_db) as conn:
        conn.execute(STAND_IN)
    client = start(empty_db, monkeypatch)
    with connect(empty_db) as conn:
        conn.execute("INSERT INTO garagebooks.car (id, name) VALUES (1500, 'By hand')")
    reading(empty_db, 1, 9_999)                      # a stale hand reading on the TeslaMate car: ignored while TeslaMate answers
    reading(empty_db, 1500, 4_800)
    for car in (1, 1500):
        add(client, empty_db, car, title=f"Service {car}", due_date="", due_km="5000" if car == 1500 else "1000")
    page = client.get("/reminders").data.decode()
    assert "900 km" in page and "from TeslaMate" in page          # 900 of 1000: due soon (within 1000)
    assert "4,800 km" in page and "entered" in page
    by_title = dict(zip(re.findall(r'class="reminder card"[^>]*data-title="([^"]+)"', page), statuses(page), strict=True))
    assert by_title == {"Service 1": "due_soon", "Service 1500": "due_soon"}


def test_when_teslamate_cannot_be_read_the_hand_readings_carry_on(empty_db, monkeypatch):
    with connect(empty_db) as conn:
        conn.execute(STAND_IN)
    client = start(empty_db, monkeypatch)
    add(client, empty_db, 1, due_date="", due_km="20000")
    with connect(empty_db) as conn:
        conn.execute("ALTER TABLE public.positions RENAME COLUMN odometer TO odo")
    client = start(empty_db, monkeypatch)
    assert "Odometer unknown" in client.get("/reminders").data.decode()
    reading(empty_db, 1, 19_500)
    page = client.get("/reminders").data.decode()
    assert "19,500 km" in page and statuses(page)[:1] == ["due_soon"]


# ---- the least-privilege role -----------------------------------------------------------------------------------

def test_the_least_privilege_role_can_use_reminders_and_read_the_view(pg, admin):
    with psycopg.connect(pg.app_url, row_factory=dict_row, autocommit=True) as conn:
        rid = conn.execute("INSERT INTO garagebooks.reminder (car_id, title, due_km) VALUES (1, 'Role', 5) RETURNING id").fetchone()["id"]
        conn.execute("UPDATE garagebooks.reminder SET title = 'Role 2' WHERE id = %s", (rid,))
        assert conn.execute("SELECT title FROM garagebooks.reminder_due WHERE reminder_id = %s", (rid,)).fetchone()["title"] == "Role 2"
        conn.execute("DELETE FROM garagebooks.reminder WHERE id = %s", (rid,))
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("DROP VIEW garagebooks.reminder_due")


# ---- the dashboard panel --------------------------------------------------------------------------------------------

def panel(dash):
    return next(p for p in dash["panels"] if p["title"] == "Reminders")


def test_the_dashboard_has_a_reminders_table_above_the_expenses_table():
    for teslamate in (None, sources.TeslaMate(sources.ABSENT), sources.TeslaMate(sources.UNTESTED, "held")):
        dash = dashboard.render("Australia/Brisbane", "", teslamate=teslamate)
        mine = panel(dash)
        expenses = next(p for p in dash["panels"] if p["title"] == "Expenses")
        assert mine["type"] == "table" and mine["gridPos"]["y"] + mine["gridPos"]["h"] <= expenses["gridPos"]["y"]
        assert mine["gridPos"]["w"] == 24 and "charging_processes" not in mine["targets"][0]["rawSql"]
        assert len({p["id"] for p in dash["panels"]}) == len(dash["panels"])


@pytest.mark.parametrize("teslamate", [None, sources.TeslaMate(sources.ABSENT)])
def test_the_panel_works_out_the_same_status_as_the_app(app, teslamate):
    client, url, car_id = app
    cases = {"Late": dict(due_date=day(-3)), "Soon": dict(due_date=day(10)), "Far": dict(due_date=day(200)),
             "KmOver": dict(due_date="", due_km="30500"), "KmSoon": dict(due_date="", due_km="31000"),
             "KmFar": dict(due_date="", due_km="45000"), "Both": dict(due_date=day(100), due_km="30100"),
             "Edge": dict(due_date=day(30))}
    for title, over in cases.items():
        add(client, url, car_id, title=title, **over)
    client.post(f"/cars/{car_id}/lease/new", data=good_form(rego_renewed_by_provider="", rego_expiry=day(20)), headers=ORIGIN)
    reading(url, car_id, 30_500)
    dash = dashboard.render("UTC", "", teslamate=teslamate)
    sql = runnable(panel(dash)["targets"][0]["rawSql"].replace("$car_id", str(car_id)))
    with connect(url) as conn:
        rows = conn.execute(sql).fetchall()
    shown = {r["Reminder"]: r["Status"] for r in rows}
    odometer, today = 30_500, datetime.now(ZoneInfo("UTC")).date()
    with connect(url) as conn:
        for r in conn.execute("SELECT * FROM garagebooks.reminder_due WHERE car_id = %s", (car_id,)):
            expected = reminders.status(r, today, odometer)
            assert shown[r["title"]] == expected.label, (r["title"], shown[r["title"]], expected)
    assert len(rows) == len(cases) + 2 and rows[0]["Status"] == "Overdue"          # most urgent first
    assert {"Reminder", "Due date", "Due km", "Days left", "Km left", "Status"} <= set(rows[0])


def test_the_panel_says_the_next_lease_is_loaded_as_the_app_does(app):
    client, url, car_id = app
    for f in (good_form(start_date=day(-700), end_date=day(10), rego_expiry="", term_months="24"),
              good_form(start_date=day(11), end_date=day(741), rego_expiry="", term_months="24")):
        assert client.post(f"/cars/{car_id}/lease/new", data=f, headers=ORIGIN).status_code == 303
    raw = panel(dashboard.render("Australia/Brisbane", ""))["targets"][0]["rawSql"]
    with connect(url) as conn:
        rows = conn.execute(runnable(raw.replace("$car_id", str(car_id)))).fetchall()
        due = conn.execute("SELECT * FROM garagebooks.reminder_due WHERE car_id = %s", (car_id,)).fetchone()
    assert [(r["Reminder"], r["Status"]) for r in rows] == [("Lease ends", "Next lease loaded")]
    assert reminders.status(due, today_here(), None).label == "Next lease loaded"


def test_the_panel_says_odometer_unknown_and_leaves_out_resolved_and_other_cars(app):
    client, url, car_id = app
    add(client, url, car_id, title="NoKm", due_date="", due_km="9000")
    done = add(client, url, car_id, title="Done")
    resolve(client, done, date=day(-1))
    dash = dashboard.render("UTC", "")
    raw = panel(dash)["targets"][0]["rawSql"]
    with connect(url) as conn:
        rows = conn.execute(runnable(raw.replace("$car_id", str(car_id)))).fetchall()
        none = conn.execute(runnable(raw.replace("$car_id", "-1"))).fetchall()
    assert [(r["Reminder"], r["Status"], r["Km left"]) for r in rows] == [("NoKm", "Odometer unknown", None)]
    assert none == []


# ---- standard reminders on the Add a reminder form -----------------------------------------------------

def test_the_standard_reminders_are_well_formed_and_an_electric_car_gets_no_engine_items():
    keys = [s["key"] for s in reminders.STANDARD]
    assert len(keys) == len(set(keys)) and len(keys) >= 10
    for item in reminders.STANDARD:
        assert item["title"] and (item["months"] or item["km"]), item           # each repeats by time, distance or both
        assert item["kinds"] is None or set(item["kinds"]) <= {"BEV", "ICE", "PHEV", "HEV"}
    engine = {"engine_oil", "oil_filter", "air_filter", "spark_plugs", "timing_belt"}
    for kind in ("ICE", "PHEV", "HEV"):
        assert engine <= {s["key"] for s in reminders.standard_for(kind)}
    bev = {s["key"] for s in reminders.standard_for("BEV")}
    assert not (engine & bev) and {"tyre_rotation", "wheel_alignment", "cabin_air_filter", "registration", "insurance"} <= bev
    assert reminders.standard_for("BEV") == [s for s in reminders.STANDARD if s["kinds"] is None]
    assert len(reminders.standard_for("ICE")) == len(reminders.STANDARD)
    assert len(reminders.standard_for("something else")) == len(reminders.standard_for("BEV"))


def test_the_form_offers_the_standard_reminders_with_the_car_types_they_apply_to(app):
    client, _url, car_id = app
    page = client.get("/reminders").data.decode()
    block = re.search(r'<label[^>]*id="standard-reminder".*?</label>', page, re.S).group(0)
    assert "data-today=" in block and f'data-today="{today_here().isoformat()}"' in block
    options = re.findall(r'<option value="(\w+)" data-title="([^"]+)" data-months="(\d*)" data-km="(\d*)"\s+data-kinds="([^"]*)"', block)
    assert [o[0] for o in options] == [s["key"] for s in reminders.STANDARD]
    by_key = {o[0]: o for o in options}
    assert by_key["engine_oil"][1:] == ("Engine oil", "12", "10000", "ICE PHEV HEV")
    assert by_key["tyre_rotation"][1:] == ("Tyre rotation", "", "10000", "")           # every car: no kinds
    assert 'data-category="Service"' in block and 'data-item="Tyre rotation"' in block
    assert f'<option value="{car_id}" data-kind="BEV"' in page


def test_each_car_option_carries_its_type_and_current_odometer(empty_db, monkeypatch):
    client = start(empty_db, monkeypatch)
    client.post("/cars", data={"name": "Ute", "kind": "ICE"}, headers=ORIGIN)
    client.post("/cars", data={"name": "Bike", "kind": "HEV"}, headers=ORIGIN)
    with connect(empty_db) as conn:
        ute, bike = [r["id"] for r in conn.execute("SELECT id FROM garagebooks.car ORDER BY id")]
    reading(empty_db, ute, 12_345)
    page = client.get("/reminders").data.decode()
    assert re.search(rf'<option value="{ute}" data-kind="ICE" data-odometer="12345', page)
    assert re.search(rf'<option value="{bike}" data-kind="HEV" ', page) and f'<option value="{bike}" data-kind="HEV" data-odometer' not in page


# ---- reminders at the service-item level --------------------------------------------------------------

def item(url, name):
    with connect(url) as conn:
        return conn.execute("SELECT id FROM garagebooks.service_item WHERE name = %s", (name,)).fetchone()["id"]


def test_a_reminder_can_be_for_a_service_item(app):
    client, url, car_id = app
    cabin = item(url, "Cabin air filter")
    rid = add(client, url, car_id, title="Cabin air filter", category_id=str(category(url, "Service")),
              service_item_id=str(cabin), every_months="24", every_km="20000", due_km="60000")
    assert one(url, "SELECT service_item_id, category_id FROM garagebooks.reminder WHERE id = %s", rid) == {
        "service_item_id": cabin, "category_id": category(url, "Service")}
    assert one(url, "SELECT service_item_id FROM garagebooks.reminder_due WHERE reminder_id = %s", rid)["service_item_id"] == cabin
    assert "Cabin air filter" in client.get("/reminders").data.decode()
    page = client.get(f"/reminders/{rid}/edit").data.decode()
    assert re.search(rf'<option value="{cabin}"\s+selected', page) or f'value="{cabin}" selected' in page


def test_the_form_offers_the_items_and_refuses_one_that_isnt_real(app):
    client, url, car_id = app
    assert 'name="service_item_id"' in client.get("/reminders").data.decode()
    bad = client.post("/reminders", data=form(car_id, service_item_id="99999"), headers=ORIGIN)
    assert bad.status_code == 400 and "Choose a service item from the list." in bad.data.decode().replace("&#39;", "'")


def test_an_expense_with_the_item_ticked_is_offered_that_reminder_ticked(app):
    client, url, car_id = app
    cabin, brakes = item(url, "Cabin air filter"), item(url, "Brakes")
    service = category(url, "Service")
    mine = add(client, url, car_id, title="Cabin filter", category_id=str(service), service_item_id=str(cabin))
    other = add(client, url, car_id, title="Brakes", category_id=str(service), service_item_id=str(brakes))
    by_category = add(client, url, car_id, title="Any service", category_id=str(service))
    data = {"car_id": str(car_id), "category_id": str(service), "date": day(0), "time": "09:00", "amount": "90",
            "odometer_km": "41000", "vendor": "Garage", "invoice_url": "", "notes": "", "service_item": [str(cabin)]}
    saved = client.post("/new", data=data, headers=ORIGIN)
    page = client.get(saved.location).data.decode()
    assert re.search(rf'value="{mine}"[^>]*checked', page)
    assert not re.search(rf'value="{other}"[^>]*checked', page)         # an item the expense doesn't have
    assert re.search(rf'value="{by_category}"[^>]*checked', page)       # no item: the category rule, as before
    eid = one(url, "SELECT max(id) AS id FROM garagebooks.expense")["id"]
    assert client.post(f"/{eid}/reminders", data={"reminder_id": [str(mine)]}, headers=ORIGIN).status_code == 303
    assert one(url, "SELECT resolved_expense_id FROM garagebooks.reminder WHERE id = %s", mine)["resolved_expense_id"] == eid


def test_a_repeating_item_reminder_keeps_its_item_when_resolved(app):
    client, url, car_id = app
    cabin = item(url, "Cabin air filter")
    rid = add(client, url, car_id, title="Cabin air filter", service_item_id=str(cabin), every_months="24")
    eid = expense(url, car_id)
    assert client.post(f"/{eid}/reminders", data={"reminder_id": [str(rid)]}, headers=ORIGIN).status_code == 303
    nxt = one(url, "SELECT service_item_id FROM garagebooks.reminder WHERE previous_id = %s", rid)
    assert nxt["service_item_id"] == cabin


def test_deleting_an_item_leaves_its_reminder_in_place(app):
    client, url, car_id = app
    spare = client.post("/api/service-items", json={"name": "Spare part"}, headers=ORIGIN).get_json()["id"]
    rid = add(client, url, car_id, title="Spare", service_item_id=str(spare))
    assert client.post(f"/categories/services/{spare}/delete", headers=ORIGIN).status_code == 303
    assert one(url, "SELECT service_item_id FROM garagebooks.reminder WHERE id = %s", rid)["service_item_id"] is None
