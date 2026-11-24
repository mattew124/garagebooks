"""Notifications (roadmap item 8): the `notifications` job tells the owner, through the address set on the
Settings page, about a reminder coming due or overdue, the OneDrive client secret nearing its expiry and a
OneDrive sign-in that stopped working. Each thing is told once. Nothing here reaches the network: a
stand-in takes the place of the sender, and every address is invented.

Most tests run on a database with no TeslaMate (so they pass both ways); the odometer ones also run with
TeslaMate readable and with it held back."""

import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import psycopg
import pytest
from psycopg.rows import dict_row

from garagebooks import create_app, db, jobs, notices, notify, sources, validation
from tests.test_lease import today_here
from tests.test_sources import STAND_IN

ORIGIN = {"Origin": "http://localhost"}
TZ = ZoneInfo("Australia/Brisbane")
NOW = datetime(2026, 11, 17, 9, 0, tzinfo=TZ)
TODAY = NOW.date()
URL = "https://ntfy.example.invalid/your-secret-topic"


def connect(url):
    return psycopg.connect(url, row_factory=dict_row, autocommit=True)


class Sender:
    """Stands in for notify.send: remembers what it was asked to send, and can be made to fail."""

    def __init__(self):
        self.sent, self.fail = [], None

    def __call__(self, url, fmt, title, message, tags=(), fetch=None):
        if self.fail:
            raise self.fail
        self.sent.append({"url": url, "fmt": fmt, "title": title, "message": message, "tags": tuple(tags)})

    def titles(self):
        return [s["title"] for s in self.sent]


def start(url, monkeypatch, sender=None, **env):
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("APP_TZ", "Australia/Brisbane")
    for key in ("SKIP_MIGRATE", "DASHBOARD_DIR", "BASE_PATH", "ALLOW_UNTESTED_TESLAMATE"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    app = create_app(notify_send=sender)
    app.testing = True
    return app


@pytest.fixture()
def world(empty_db, monkeypatch):
    """An app on a database with no TeslaMate, one car by hand, the address set, and a stand-in sender."""
    sender = Sender()
    app = start(empty_db, monkeypatch, sender)
    with connect(empty_db) as conn:
        conn.execute("INSERT INTO garagebooks.car (id, name) VALUES (1500, 'Test car')")
        conn.execute("UPDATE garagebooks.setting SET notify_url = %s, notify_format = NULL", (URL,))
    try:
        yield app, empty_db, sender
    finally:
        with connect(empty_db) as conn:
            conn.execute("UPDATE garagebooks.setting SET notify_url = NULL, notify_format = NULL")
            conn.execute("TRUNCATE garagebooks.notification, garagebooks.reminder, garagebooks.onedrive_account"
                         " RESTART IDENTITY CASCADE")
            conn.execute("DELETE FROM garagebooks.lease")


NO_TESLAMATE = sources.TeslaMate(sources.ABSENT)


def tell(url, sender, now=NOW, teslamate=NO_TESLAMATE, **kw):
    """One pass of the job. -> its words."""
    with connect(url) as conn:
        return notices.run(conn, sender, teslamate, now, **kw)


def remind(url, title="Tyre rotation", due_date=None, due_km=None, car_id=1500, **over):
    with connect(url) as conn:
        return conn.execute(
            "INSERT INTO garagebooks.reminder (car_id, title, due_date, due_km, lead_days, lead_km) "
            "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
            (car_id, title, due_date, due_km, over.get("lead_days", 30), over.get("lead_km", 1000))).fetchone()["id"]


def keys(url):
    with connect(url) as conn:
        return sorted(r["key"] for r in conn.execute("SELECT key FROM garagebooks.notification"))


def secret_expiring(url, on, **over):
    values = {"client_id": "id", "client_secret": "x", "secret_expires": on, **over}
    with connect(url) as conn:
        cols = ", ".join(values)
        conn.execute(f"INSERT INTO garagebooks.onedrive_account (id, {cols}) VALUES (true, "
                     + ", ".join(f"%({c})s" for c in values) + ") ON CONFLICT (id) DO UPDATE SET "
                     + ", ".join(f"{c} = EXCLUDED.{c}" for c in values), values)


# ---- off by default ----------------------------------------------------------------------------------

def test_with_no_address_nothing_is_told_and_the_job_says_it_is_off(world):
    _, url, sender = world
    remind(url, due_date=TODAY + timedelta(days=3))
    with connect(url) as conn:
        conn.execute("UPDATE garagebooks.setting SET notify_url = NULL")
    assert tell(url, sender) == "off"
    assert sender.sent == [] and keys(url) == []


def test_the_job_is_registered_every_fifteen_minutes(world):
    app, _, _ = world
    job = app.extensions["scheduler"].jobs["notifications"]
    assert job.every == timedelta(minutes=15) and "notice" in job.description.lower()


# ---- reminders ----------------------------------------------------------------------------------------

def test_a_reminder_coming_due_is_told_once_with_the_car_and_what_it_is_due_on(world):
    _, url, sender = world
    remind(url, due_date=TODAY + timedelta(days=10))
    assert tell(url, sender) == "1 sent"
    (notice,) = sender.sent
    assert notice["url"] == URL and notice["fmt"] == "ntfy"
    assert "Tyre rotation" in notice["title"] and "Test car" in notice["title"]
    assert "27 Nov 2026" in notice["message"] and "in 10 days" in notice["message"]
    assert tell(url, sender) == "nothing new" and len(sender.sent) == 1        # told once


def test_a_reminder_not_yet_in_its_lead_time_is_not_told(world):
    _, url, sender = world
    remind(url, due_date=TODAY + timedelta(days=31))
    assert tell(url, sender) == "nothing new" and sender.sent == []


def test_it_is_told_again_when_it_becomes_overdue_and_not_again_after(world):
    _, url, sender = world
    remind(url, due_date=TODAY + timedelta(days=2))
    tell(url, sender)
    assert tell(url, sender, now=NOW + timedelta(days=1)) == "nothing new"
    assert tell(url, sender, now=NOW + timedelta(days=3)) == "1 sent"
    assert "overdue" in sender.sent[-1]["title"].lower() and "1 day overdue" in sender.sent[-1]["message"]
    assert tell(url, sender, now=NOW + timedelta(days=9)) == "nothing new" and len(sender.sent) == 2


def test_one_already_overdue_when_first_seen_is_told_once_as_overdue(world):
    _, url, sender = world
    remind(url, due_date=TODAY - timedelta(days=5))
    tell(url, sender)
    assert len(sender.sent) == 1 and "overdue" in sender.sent[0]["title"].lower()


def test_editing_the_due_date_or_odometer_lets_it_be_told_again(world):
    _, url, sender = world
    rid = remind(url, due_date=TODAY + timedelta(days=10))
    tell(url, sender)
    with connect(url) as conn:
        conn.execute("UPDATE garagebooks.reminder SET due_date = %s WHERE id = %s", (TODAY + timedelta(days=5), rid))
    assert tell(url, sender) == "1 sent"
    with connect(url) as conn:
        conn.execute("UPDATE garagebooks.reminder SET due_date = NULL, due_km = 99999 WHERE id = %s", (rid,))   # not near it: nothing
    assert tell(url, sender) == "nothing new"


def test_resolving_forgets_what_was_told_so_reopening_it_tells_again(world):
    _, url, sender = world
    rid = remind(url, due_date=TODAY + timedelta(days=10))
    tell(url, sender)
    with connect(url) as conn:
        conn.execute("UPDATE garagebooks.reminder SET resolved_at = now() WHERE id = %s", (rid,))
    tell(url, sender)
    assert keys(url) == []                                        # resolved: what was told about it is forgotten
    with connect(url) as conn:
        conn.execute("UPDATE garagebooks.reminder SET resolved_at = NULL WHERE id = %s", (rid,))
    assert tell(url, sender) == "1 sent" and len(sender.sent) == 2


def test_what_was_told_about_a_deleted_reminder_goes_with_it(world):
    _, url, sender = world
    rid = remind(url, due_date=TODAY + timedelta(days=10))
    tell(url, sender)
    with connect(url) as conn:
        conn.execute("DELETE FROM garagebooks.reminder WHERE id = %s", (rid,))
    tell(url, sender)
    assert keys(url) == []


def test_a_lease_end_is_not_told_when_the_next_lease_is_already_loaded(world):
    _, url, sender = world
    today = today_here()
    with connect(url) as conn:
        for start_date, end_date in ((today - timedelta(days=700), today + timedelta(days=5)),
                                     (today + timedelta(days=6), today + timedelta(days=736))):
            conn.execute("INSERT INTO garagebooks.lease (car_id, financier, start_date, end_date, term_months, "
                         "pay_frequency) VALUES (1500, 'Provider', %s, %s, 24, 'monthly')", (start_date, end_date))
    tell(url, sender, now=datetime.now(TZ))
    assert not any("Lease ends" in t for t in sender.titles())


def test_a_lease_registration_and_the_lease_end_are_told_but_a_provider_renewed_registration_is_not(world):
    _, url, sender = world
    with connect(url) as conn:
        conn.execute(
            "INSERT INTO garagebooks.lease (car_id, financier, start_date, end_date, term_months, pay_frequency, "
            "rego_expiry, rego_renewed_by_provider) VALUES (1500, 'Provider', %s, %s, 36, 'monthly', %s, false)",
            (TODAY - timedelta(days=700), TODAY + timedelta(days=120), TODAY + timedelta(days=12)))
    tell(url, sender)
    titles = " ".join(sender.titles())
    assert "Registration expires" in titles and "Lease ends" not in titles      # the lease ends in 120 days: the lead is 90
    with connect(url) as conn:
        conn.execute("UPDATE garagebooks.lease SET end_date = %s", (TODAY + timedelta(days=80),))
    tell(url, sender)
    assert any("Lease ends" in t for t in sender.titles())
    sender.sent.clear()
    with connect(url) as conn:
        conn.execute("UPDATE garagebooks.lease SET rego_renewed_by_provider = true, rego_expiry = %s",
                     (TODAY + timedelta(days=2),))
    tell(url, sender)
    assert not any("Registration" in t for t in sender.titles())            # the provider takes care of it


def test_at_most_ten_are_sent_in_a_pass_and_the_rest_next_time(world):
    _, url, sender = world
    for n in range(12):
        remind(url, title=f"Job {n:02d}", due_date=TODAY + timedelta(days=n))
    assert tell(url, sender) == "10 sent" and len(sender.sent) == 10
    assert tell(url, sender) == "2 sent" and len(sender.sent) == 12
    assert len({t for t in sender.titles()}) == 12


def test_a_failed_send_is_not_recorded_and_is_tried_again_next_pass(world):
    _, url, sender = world
    remind(url, due_date=TODAY + timedelta(days=3))
    sender.fail = jobs.JobError("The notification service could not be reached.")
    with pytest.raises(jobs.JobError, match="could not be reached"):
        tell(url, sender)
    assert keys(url) == []
    sender.fail = None
    assert tell(url, sender) == "1 sent" and len(keys(url)) == 1


def test_a_failure_after_some_were_sent_keeps_those_and_says_so(world):
    _, url, sender = world
    remind(url, title="First", due_date=TODAY + timedelta(days=1))
    remind(url, title="Second", due_date=TODAY + timedelta(days=2))
    real = sender.__call__

    def flaky(*args, **kw):
        if len(sender.sent) == 1:
            raise jobs.JobError("The notification service answered HTTP 500.")
        return real(*args, **kw)
    message = tell(url, flaky)
    assert message.startswith("1 sent") and "HTTP 500" in message and len(keys(url)) == 1
    assert tell(url, sender) == "1 sent" and sender.titles()[-1].find("Second") >= 0


def test_the_json_format_is_passed_on(world):
    _, url, sender = world
    with connect(url) as conn:
        conn.execute("UPDATE garagebooks.setting SET notify_format = 'json'")
    remind(url, due_date=TODAY + timedelta(days=1))
    tell(url, sender)
    assert sender.sent[0]["fmt"] == "json"


def test_rows_older_than_a_year_are_pruned(world):
    _, url, sender = world
    with connect(url) as conn:
        conn.execute("INSERT INTO garagebooks.notification (key, sent_at, title) VALUES ('old', now() - interval '13 months', 'Old'),"
                     " ('recent', now() - interval '2 months', 'Recent')")
    tell(url, sender)
    assert keys(url) == ["recent"]


# ---- the odometer: TeslaMate, by hand, or held back ---------------------------------------------------------

def reading(url, car, km):
    with connect(url) as conn:
        conn.execute("INSERT INTO garagebooks.odometer_reading (car_id, read_at, km) VALUES (%s, %s, %s)", (car, NOW - timedelta(days=1), km))


def test_a_reminder_due_by_odometer_uses_the_hand_readings_and_says_the_figure(world):
    _, url, sender = world
    remind(url, due_km=5000)
    assert tell(url, sender) == "nothing new"                      # no reading: unknown, never a zero
    reading(url, 1500, 4800)
    assert tell(url, sender) == "1 sent" and "5,000 km" in sender.sent[0]["message"] and "200 km to go" in sender.sent[0]["message"]


def test_an_odometer_not_known_is_not_told_on_odometer_grounds_but_a_date_still_is(world):
    _, url, sender = world
    remind(url, due_km=500, due_date=TODAY + timedelta(days=4))     # a zero reading would put 500 km inside the lead
    tell(url, sender)
    assert len(sender.sent) == 1 and "odometer unknown" not in sender.sent[0]["message"]
    assert "in 4 days" in sender.sent[0]["message"]


def test_with_teslamate_readable_its_odometer_is_used(empty_db, monkeypatch):
    with connect(empty_db) as conn:
        conn.execute(STAND_IN)                                      # car 1 at 900 km
    sender = Sender()
    app = start(empty_db, monkeypatch, sender)
    try:
        with connect(empty_db) as conn:
            conn.execute("UPDATE garagebooks.setting SET notify_url = %s", (URL,))
            remind(empty_db, due_km=1500, car_id=1)
            app.extensions["scheduler"].jobs["notifications"].run(conn)
        assert len(sender.sent) == 1 and "600 km to go" in sender.sent[0]["message"]
    finally:
        with connect(empty_db) as conn:
            conn.execute("UPDATE garagebooks.setting SET notify_url = NULL")
            conn.execute("TRUNCATE garagebooks.notification, garagebooks.reminder RESTART IDENTITY CASCADE")


def test_with_teslamate_held_back_a_wrong_reading_is_never_the_ground_for_a_notice(empty_db, monkeypatch):
    with connect(empty_db) as conn:
        conn.execute(STAND_IN)
    start(empty_db, monkeypatch)                                    # readable at first: its car is added
    with connect(empty_db) as conn:
        conn.execute("ALTER TABLE public.positions RENAME COLUMN odometer TO odo")    # a table that changed: held back
    sender = Sender()
    app = start(empty_db, monkeypatch, sender)
    try:
        with connect(empty_db) as conn:
            conn.execute("UPDATE garagebooks.setting SET notify_url = %s", (URL,))
            remind(empty_db, due_km=500, car_id=1)                  # 500 km: due soon if the odometer were read as 0
            assert app.extensions["scheduler"].jobs["notifications"].run(conn) == "nothing new"
            assert sender.sent == []
            conn.execute("INSERT INTO garagebooks.odometer_reading (car_id, read_at, km) VALUES (1, now() - interval '1 day', 100)")
            assert app.extensions["scheduler"].jobs["notifications"].run(conn) == "1 sent"     # a reading made by hand still counts
            assert "400 km to go" in sender.sent[0]["message"]
    finally:
        with connect(empty_db) as conn:
            conn.execute("UPDATE garagebooks.setting SET notify_url = NULL")
            conn.execute("TRUNCATE garagebooks.notification, garagebooks.reminder RESTART IDENTITY CASCADE")


# ---- the OneDrive client secret -------------------------------------------------------------------------------

def test_the_secret_expiring_is_told_at_30_days_at_7_days_and_when_it_has_passed(world):
    _, url, sender = world
    expiry = TODAY + timedelta(days=40)
    secret_expiring(url, expiry)
    assert tell(url, sender, onedrive=True) == "nothing new"
    assert tell(url, sender, now=NOW + timedelta(days=12), onedrive=True) == "1 sent"      # 28 days left
    assert tell(url, sender, now=NOW + timedelta(days=20), onedrive=True) == "nothing new"
    assert tell(url, sender, now=NOW + timedelta(days=35), onedrive=True) == "1 sent"      # 5 days left
    assert tell(url, sender, now=NOW + timedelta(days=41), onedrive=True) == "1 sent"      # passed
    assert tell(url, sender, now=NOW + timedelta(days=60), onedrive=True) == "nothing new"
    assert [t for t in sender.titles()] == ["Microsoft client secret expires in 28 days",
                                            "Microsoft client secret expires in 5 days",
                                            "Microsoft client secret has expired"]
    assert keys(url) == [f"onedrive_secret:{expiry}:30d", f"onedrive_secret:{expiry}:7d", f"onedrive_secret:{expiry}:expired"]
    assert "Invoice storage" in sender.sent[0]["message"]


def test_a_secret_already_inside_a_week_is_told_once_not_twice(world):
    _, url, sender = world
    secret_expiring(url, TODAY + timedelta(days=3))
    tell(url, sender, onedrive=True)
    assert len(sender.sent) == 1 and "3 days" in sender.titles()[0]


def test_a_new_secret_with_a_later_date_is_told_afresh(world):
    _, url, sender = world
    secret_expiring(url, TODAY + timedelta(days=20))
    tell(url, sender, onedrive=True)
    secret_expiring(url, TODAY + timedelta(days=400))
    tell(url, sender, onedrive=True)
    secret_expiring(url, TODAY + timedelta(days=25))
    assert tell(url, sender, onedrive=True) == "1 sent"


def test_nothing_about_onedrive_is_told_where_onedrive_is_off_or_no_date_was_given(world):
    _, url, sender = world
    secret_expiring(url, TODAY + timedelta(days=3))
    assert tell(url, sender, onedrive=False) == "nothing new"
    secret_expiring(url, None)
    assert tell(url, sender, onedrive=True) == "nothing new"


# ---- the OneDrive sign-in ---------------------------------------------------------------------------------------

def test_a_lost_sign_in_is_told_once(world):
    _, url, sender = world
    secret_expiring(url, None, refresh_token="x", signin_lost_at=NOW - timedelta(hours=2))
    assert tell(url, sender, onedrive=True) == "1 sent"
    assert "sign-in" in sender.titles()[0].lower() and "Invoice storage" in sender.sent[0]["message"]
    assert tell(url, sender, onedrive=True) == "nothing new"
    assert keys(url) == ["onedrive_signin:2026-11-17"]
    with connect(url) as conn:                                     # signed in again, and lost again another day
        conn.execute("UPDATE garagebooks.onedrive_account SET signin_lost_at = %s", (NOW + timedelta(days=3),))
    assert tell(url, sender, now=NOW + timedelta(days=3), onedrive=True) == "1 sent"


def test_the_daily_check_runs_once_a_day_and_only_when_signed_in(world):
    _, url, sender = world
    calls = []
    secret_expiring(url, None, refresh_token="x")
    tell(url, sender, onedrive=True, signin_check=lambda: calls.append(1))
    assert calls == [1]
    with connect(url) as conn:
        conn.execute("UPDATE garagebooks.onedrive_account SET signin_checked_at = %s", (NOW - timedelta(hours=3),))
    tell(url, sender, onedrive=True, signin_check=lambda: calls.append(1))
    assert calls == [1]                                            # checked 3 hours ago: not again
    with connect(url) as conn:
        conn.execute("UPDATE garagebooks.onedrive_account SET signin_checked_at = %s", (NOW - timedelta(hours=25),))
    tell(url, sender, onedrive=True, signin_check=lambda: calls.append(1))
    assert calls == [1, 1]
    with connect(url) as conn:                                     # already known to be lost, or never signed in: nothing to ask
        conn.execute("UPDATE garagebooks.onedrive_account SET signin_checked_at = NULL, refresh_token = NULL")
    tell(url, sender, onedrive=True, signin_check=lambda: calls.append(1))
    assert calls == [1, 1]


def test_the_check_losing_the_sign_in_is_told_in_the_same_pass(world):
    _, url, sender = world
    secret_expiring(url, None, refresh_token="x")

    def check():
        with connect(url) as conn:
            conn.execute("UPDATE garagebooks.onedrive_account SET signin_lost_at = %s", (NOW,))
    tell(url, sender, onedrive=True, signin_check=check)
    assert len(sender.sent) == 1 and "sign-in" in sender.titles()[0].lower()


# ---- the job itself, through the scheduler -----------------------------------------------------------------------

def test_a_failed_run_shows_words_without_the_address_in_the_job_row_and_the_log(empty_db, monkeypatch, caplog):
    def unreachable(address, fmt, title, message, tags=(), fetch=None):
        def refuse(*args):
            raise OSError(f"cannot connect to {address}")
        return notify.send(address, fmt, title, message, tags, fetch=refuse)
    app = start(empty_db, monkeypatch, unreachable)
    caplog.set_level("DEBUG")
    try:
        with connect(empty_db) as conn:
            conn.execute("UPDATE garagebooks.setting SET notify_url = %s", (URL,))
            conn.execute("INSERT INTO garagebooks.car (id, name) VALUES (1500, 'Test car')")
            conn.execute("DELETE FROM garagebooks.job WHERE name = 'notifications'")
        remind(empty_db, due_date=today_here() + timedelta(days=2))
        assert "notifications" in app.extensions["scheduler"].run_due()
        with connect(empty_db) as conn:
            row = conn.execute("SELECT * FROM garagebooks.job WHERE name = 'notifications'").fetchone()
        assert row["last_ok"] is False and "could not be reached" in row["last_message"]
        assert "your-secret-topic" not in row["last_message"] and "your-secret-topic" not in caplog.text
    finally:
        with connect(empty_db) as conn:
            conn.execute("UPDATE garagebooks.setting SET notify_url = NULL")
            conn.execute("TRUNCATE garagebooks.notification, garagebooks.reminder RESTART IDENTITY CASCADE")


def test_through_run_due_the_job_row_records_how_many_were_sent(world):
    app, url, sender = world
    remind(url, due_date=today_here() + timedelta(days=2))
    with connect(url) as conn:
        conn.execute("DELETE FROM garagebooks.job WHERE name = 'notifications'")
    assert "notifications" in app.extensions["scheduler"].run_due()
    with connect(url) as conn:
        row = conn.execute("SELECT * FROM garagebooks.job WHERE name = 'notifications'").fetchone()
    assert row["last_ok"] is True and row["last_message"] == "1 sent" and len(sender.sent) == 1


# ---- the Settings page ------------------------------------------------------------------------------------------

@pytest.fixture()
def page(empty_db, monkeypatch):
    sender = Sender()
    app = start(empty_db, monkeypatch, sender)
    try:
        yield app.test_client(), empty_db, sender
    finally:
        with connect(empty_db) as conn:
            conn.execute("UPDATE garagebooks.setting SET notify_url = NULL, notify_format = NULL")


def saved(url):
    with connect(url) as conn:
        return conn.execute("SELECT notify_url, notify_format FROM garagebooks.setting").fetchone()


def test_the_notifications_section_is_on_the_settings_page_with_the_address_empty(page):
    client, _, _ = page
    html = client.get("/settings").data.decode()
    assert "Notifications" in html and 'name="notify_url"' in html and 'name="notify_format"' in html
    assert "Send a test notice" in html and "https://ntfy.example/your-topic" in html


def test_saving_the_address_stores_it_and_the_page_never_shows_it_back(page):
    client, url, _ = page
    assert client.post("/settings/notifications", data={"notify_url": URL, "notify_format": "json"}, headers=ORIGIN).status_code == 303
    assert saved(url) == {"notify_url": URL, "notify_format": "json"}
    html = client.get("/settings").data.decode()
    assert "your-secret-topic" not in html and "ntfy.example.invalid" in html           # the host, to know which one it is
    assert 'type="password"' in html and 'value="https://' not in html.split("Notifications", 1)[1]


def test_a_blank_address_keeps_the_saved_one_and_the_remove_box_clears_it(page):
    client, url, _ = page
    client.post("/settings/notifications", data={"notify_url": URL, "notify_format": "ntfy"}, headers=ORIGIN)
    client.post("/settings/notifications", data={"notify_url": "", "notify_format": "json"}, headers=ORIGIN)
    assert saved(url) == {"notify_url": URL, "notify_format": "json"}
    client.post("/settings/notifications", data={"notify_url": "", "notify_format": "json", "notify_clear": "1"}, headers=ORIGIN)
    assert saved(url)["notify_url"] is None


@pytest.mark.parametrize("bad", ["ftp://x.example.invalid/t", "javascript:alert(1)", "not an address", "https://a b.example/c",
                                 "https://" + "x" * 600 + ".example/t", "https:///nohost"])
def test_a_bad_address_is_refused_and_nothing_is_saved(page, bad):
    client, url, _ = page
    resp = client.post("/settings/notifications", data={"notify_url": bad, "notify_format": "ntfy"}, headers=ORIGIN)
    assert resp.status_code == 400 and "address" in resp.data.decode().split("Notifications", 1)[1].lower()
    assert saved(url)["notify_url"] is None


def test_an_unknown_format_is_refused(page):
    client, url, _ = page
    resp = client.post("/settings/notifications", data={"notify_url": URL, "notify_format": "sms"}, headers=ORIGIN)
    assert resp.status_code == 400 and saved(url)["notify_url"] is None


def test_the_test_button_sends_a_notice_now_and_says_so(page):
    client, url, sender = page
    client.post("/settings/notifications", data={"notify_url": URL, "notify_format": "json"}, headers=ORIGIN)
    resp = client.post("/settings/notifications/test", headers=ORIGIN)
    assert resp.status_code == 200 and "test notice was sent" in resp.data.decode()
    (notice,) = sender.sent
    assert notice["url"] == URL and notice["fmt"] == "json" and "test" in notice["title"].lower()
    assert "your-secret-topic" not in resp.data.decode()


def test_the_test_button_shows_the_failure_in_words_without_the_address(page):
    client, url, sender = page
    client.post("/settings/notifications", data={"notify_url": URL, "notify_format": "ntfy"}, headers=ORIGIN)
    sender.fail = jobs.JobError("The notification service answered HTTP 403.")
    resp = client.post("/settings/notifications/test", headers=ORIGIN)
    html = resp.data.decode()
    assert resp.status_code == 200 and "HTTP 403" in html and "your-secret-topic" not in html


def test_the_test_button_without_an_address_says_to_set_one_first(page):
    client, _, sender = page
    resp = client.post("/settings/notifications/test", headers=ORIGIN)
    assert "Save an address first" in resp.data.decode() and sender.sent == []


@pytest.mark.parametrize("path", ["/settings/notifications", "/settings/notifications/test"])
def test_both_posts_refuse_a_request_from_another_site(page, path):
    client, url, sender = page
    assert client.post(path, data={"notify_url": URL}, headers={"Origin": "https://evil.example.invalid"}).status_code == 403
    assert client.post(path, data={"notify_url": URL}).status_code == 403
    assert saved(url)["notify_url"] is None and sender.sent == []


# ---- validation -----------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("raw, expected", [("", None), ("  ", None), (URL, URL), (f"  {URL} ", URL),
                                           ("http://ntfy.local:8080/t", "http://ntfy.local:8080/t"),
                                           ("https://user:pw@ntfy.example.invalid/t", "https://user:pw@ntfy.example.invalid/t")])
def test_a_notification_address_is_blank_or_a_full_http_address(raw, expected):
    assert validation.parse_notify_url(raw) == expected


@pytest.mark.parametrize("raw", ["ftp://a.example/t", "a.example/t", "https://", "https://a b.example/", "https://a.example/\x07",
                                 "https://a.example/" + "x" * 500, "mailto:me@example.invalid", "file:///etc/passwd"])
def test_anything_else_is_refused(raw):
    with pytest.raises(ValueError):
        validation.parse_notify_url(raw)


def test_the_format_is_ntfy_or_json_and_blank_means_ntfy():
    assert validation.parse_notify_format("") == "ntfy" and validation.parse_notify_format(None) == "ntfy"
    assert validation.parse_notify_format("json") == "json" and validation.parse_notify_format(" NTFY ") == "ntfy"
    for bad in ("sms", "xml", "json;", "ntfy\n"):
        with pytest.raises(ValueError):
            validation.parse_notify_format(bad)


# ---- the table, the role, the upgrade --------------------------------------------------------------------------------

def test_the_schema_refuses_a_bad_address_and_format(admin):
    for sql in ("UPDATE garagebooks.setting SET notify_url = 'ftp://x.example/t'",
                "UPDATE garagebooks.setting SET notify_url = 'https://a b.example'",
                "UPDATE garagebooks.setting SET notify_url = 'https://a.example/' || repeat('x', 500)",
                "UPDATE garagebooks.setting SET notify_format = 'sms'"):
        with pytest.raises(psycopg.errors.CheckViolation):
            admin.execute(sql)


def test_the_least_privilege_role_can_use_the_table_and_the_settings(pg, admin):
    with psycopg.connect(pg.app_url, row_factory=dict_row, autocommit=True) as conn:
        try:
            conn.execute("UPDATE garagebooks.setting SET notify_url = %s, notify_format = 'json'", (URL,))
            conn.execute("INSERT INTO garagebooks.notification (key, title) VALUES ('k', 'T')")
            assert conn.execute("SELECT title FROM garagebooks.notification WHERE key = 'k'").fetchone()["title"] == "T"
            conn.execute("UPDATE garagebooks.onedrive_account SET signin_lost_at = now()")
        finally:
            conn.execute("DELETE FROM garagebooks.notification")
            conn.execute("UPDATE garagebooks.setting SET notify_url = NULL, notify_format = NULL")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("UPDATE garagebooks.notification SET title = 'x'")        # told once, never rewritten


def test_an_older_database_gains_the_table_and_the_columns_on_start(empty_db, monkeypatch):
    start(empty_db, monkeypatch)
    with connect(empty_db) as conn:
        conn.execute("DROP TABLE garagebooks.notification")
        conn.execute("ALTER TABLE garagebooks.setting DROP COLUMN notify_url, DROP COLUMN notify_format")
        conn.execute("ALTER TABLE garagebooks.onedrive_account DROP COLUMN signin_lost_at, DROP COLUMN signin_checked_at")
    start(empty_db, monkeypatch)
    with connect(empty_db) as conn:
        assert conn.execute("SELECT notify_url, notify_format FROM garagebooks.setting").fetchone() == {"notify_url": None, "notify_format": None}
        conn.execute("INSERT INTO garagebooks.notification (key, title) VALUES ('k', 'T')")
        conn.execute("SELECT signin_lost_at, signin_checked_at FROM garagebooks.onedrive_account")
        conn.execute("DELETE FROM garagebooks.notification")


def test_the_address_is_never_written_to_the_change_history_or_the_log(page, caplog):
    client, url, _ = page
    caplog.set_level("DEBUG")
    client.post("/settings/notifications", data={"notify_url": URL, "notify_format": "ntfy"}, headers=ORIGIN)
    client.post("/settings/notifications/test", headers=ORIGIN)
    with connect(url) as conn:
        assert conn.execute("SELECT count(*) AS n FROM garagebooks.change_log WHERE after_row::text LIKE '%your-secret-topic%'").fetchone()["n"] == 0
    assert "your-secret-topic" not in caplog.text


# ---- TeslaMate newer than tested (opt-in) --------------------------------------------------------------

UNTESTED_TM = sources.TeslaMate(sources.UNTESTED, "Newer than tested.", False, 20270101000000)


def set_teslamate_notice(url, on):
    with connect(url) as conn:
        conn.execute("UPDATE garagebooks.setting SET notify_teslamate = %s", (on,))


def test_a_newer_teslamate_is_told_only_when_the_box_is_ticked(world):
    _, url, sender = world
    assert tell(url, sender, teslamate=UNTESTED_TM) == "nothing new"      # off unless asked for
    set_teslamate_notice(url, True)
    try:
        assert tell(url, sender, teslamate=UNTESTED_TM) == "1 sent"
        assert "TeslaMate" in sender.titles()[0] and "held back" in sender.sent[0]["message"]
        assert keys(url) == ["teslamate:20270101000000:untested"]
    finally:
        set_teslamate_notice(url, False)


def test_a_newer_teslamate_is_told_once_and_again_for_a_later_migration(world):
    _, url, sender = world
    set_teslamate_notice(url, True)
    try:
        tell(url, sender, teslamate=UNTESTED_TM)
        assert tell(url, sender, teslamate=UNTESTED_TM) == "nothing new"
        assert tell(url, sender, teslamate=sources.TeslaMate(sources.UNTESTED, "x", False, 20270202000000)) == "1 sent"
        assert tell(url, sender, teslamate=sources.TeslaMate(sources.OK)) == "nothing new"     # nothing to tell when fine
    finally:
        set_teslamate_notice(url, False)


def test_the_settings_checkbox_saves_and_shows_the_choice(page):
    client, url, _ = page
    html = client.get("/settings").data.decode()
    assert 'name="notify_teslamate"' in html and "checked" not in html.split('name="notify_teslamate"', 1)[1].split(">", 1)[0]
    client.post("/settings/notifications", data={"notify_url": URL, "notify_format": "ntfy", "notify_teslamate": "1"}, headers=ORIGIN)
    try:
        with connect(url) as conn:
            assert conn.execute("SELECT notify_teslamate FROM garagebooks.setting").fetchone()["notify_teslamate"] is True
        assert "checked" in client.get("/settings").data.decode().split('name="notify_teslamate"', 1)[1].split(">", 1)[0]
        client.post("/settings/notifications", data={"notify_url": "", "notify_format": "ntfy"}, headers=ORIGIN)
        with connect(url) as conn:
            assert conn.execute("SELECT notify_teslamate FROM garagebooks.setting").fetchone()["notify_teslamate"] is False
    finally:
        with connect(url) as conn:
            conn.execute("UPDATE garagebooks.setting SET notify_url = NULL, notify_format = NULL, notify_teslamate = false")
