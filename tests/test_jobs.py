"""Scheduled jobs: a small scheduler that runs periodic work, in the web process or in a worker
started from the same image. What ran, when and how it went is kept in garagebooks.job, and that
row is also how two processes agree that only one of them runs a job that is due."""

import logging
import threading
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from psycopg.rows import dict_row

from garagebooks import create_app, jobs
from tests.test_tax_years import pages as ato_pages

NOW = datetime(2026, 11, 16, 2, 0, tzinfo=UTC)
HOUR = timedelta(hours=1)


@pytest.fixture()
def connect(pg, admin):
    """Connections as the app's own least-privilege role, and no job rows left behind."""
    admin.execute("DELETE FROM garagebooks.job")
    try:
        yield lambda: psycopg.connect(pg.app_url, row_factory=dict_row)
    finally:
        admin.execute("DELETE FROM garagebooks.job")


def row(admin, name):
    return admin.execute("SELECT * FROM garagebooks.job WHERE name = %s", (name,)).fetchone()


def scheduler(connect, clock=None):
    return jobs.Scheduler(connect, logging.getLogger("test.jobs"), clock=clock or (lambda: NOW))


# ---- what a job is ---------------------------------------------------------------

def test_a_job_needs_a_plain_name_and_an_interval_of_a_minute_or_more(connect):
    s = scheduler(connect)
    for bad in ("", "Has Spaces", "9lives", "x" * 41, "drop;table"):
        with pytest.raises(ValueError):
            s.add(bad, HOUR, lambda conn: None)
    with pytest.raises(ValueError):
        s.add("too_often", timedelta(seconds=30), lambda conn: None)
    s.add("fine", HOUR, lambda conn: None)
    with pytest.raises(ValueError):      # the name is the key of its row: never two jobs under one
        s.add("fine", HOUR, lambda conn: None)


# ---- running what is due ---------------------------------------------------------

def test_a_new_job_runs_at_once_and_records_how_it_went(connect, admin):
    s = scheduler(connect)
    s.add("first", HOUR, lambda conn: "did 3 things", "Does things")
    assert s.run_due() == ["first"]
    saved = row(admin, "first")
    assert saved["last_started_at"] == NOW and saved["last_finished_at"] == NOW
    assert saved["last_ok"] is True and saved["last_message"] == "did 3 things"


def test_a_job_is_not_run_again_until_its_interval_has_passed(connect):
    ran, clock = [], [NOW]
    s = scheduler(connect, clock=lambda: clock[0])
    s.add("hourly", HOUR, lambda conn: ran.append(clock[0]))
    assert s.run_due() == ["hourly"]
    clock[0] = NOW + timedelta(minutes=59)
    assert s.run_due() == []
    clock[0] = NOW + HOUR
    assert s.run_due() == ["hourly"]
    assert ran == [NOW, NOW + HOUR]


def test_a_long_gap_runs_a_job_once_not_once_for_every_missed_interval(connect):
    ran, clock = [], [NOW]
    s = scheduler(connect, clock=lambda: clock[0])
    s.add("hourly", HOUR, lambda conn: ran.append(1))
    s.run_due()
    clock[0] = NOW + timedelta(days=3)
    assert s.run_due() == ["hourly"] and s.run_due() == []
    assert len(ran) == 2


def test_two_schedulers_never_both_run_a_job_that_is_due(connect):
    """Two web workers, or the app and a worker container: the row is claimed in one UPDATE."""
    ran = []
    both = [scheduler(connect), scheduler(connect)]
    for s in both:
        s.add("shared", HOUR, lambda conn: ran.append(1))
    start = threading.Barrier(2)

    def go(s):
        start.wait()
        s.run_due()

    threads = [threading.Thread(target=go, args=(s,)) for s in both]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert ran == [1]


def test_a_job_writes_through_the_connection_it_is_given_and_it_is_committed(connect, admin):
    s = scheduler(connect)
    s.add("writer", HOUR, lambda conn: conn.execute(
        "UPDATE garagebooks.setting SET multi_amount = 'min' WHERE id = 1") and None)
    s.run_due()
    assert admin.execute("SELECT multi_amount FROM garagebooks.setting").fetchone()["multi_amount"] == "min"


# ---- when a job fails ------------------------------------------------------------

def test_a_failing_job_is_recorded_rolled_back_and_does_not_stop_the_others(connect, admin, caplog):
    def broken(conn):
        conn.execute("UPDATE garagebooks.setting SET multi_amount = 'blank' WHERE id = 1")
        raise RuntimeError("https://example.invalid/secret-token went wrong")

    s = scheduler(connect)
    s.add("broken", HOUR, broken)
    s.add("healthy", HOUR, lambda conn: None)
    with caplog.at_level(logging.ERROR, logger="test.jobs"):
        assert s.run_due() == ["broken", "healthy"]
    saved = row(admin, "broken")
    assert saved["last_ok"] is False and saved["last_finished_at"] == NOW
    # What an unexpected error says may hold an address or a token: only its kind is kept for the page.
    assert saved["last_message"] == "RuntimeError: see the log"
    assert "secret-token" in caplog.text            # the log has the whole of it
    assert admin.execute("SELECT multi_amount FROM garagebooks.setting").fetchone()["multi_amount"] is None
    assert row(admin, "healthy")["last_ok"] is True


def test_a_job_can_say_why_it_failed_in_words_meant_for_the_page(connect, admin):
    def refused(conn):
        raise jobs.JobError("The notification address answered 403.")

    s = scheduler(connect)
    s.add("refused", HOUR, refused)
    s.run_due()
    saved = row(admin, "refused")
    assert saved["last_ok"] is False and saved["last_message"] == "The notification address answered 403."


def test_a_failed_job_waits_for_its_next_turn(connect):
    ran, clock = [], [NOW]

    def flaky(conn):
        ran.append(1)
        raise RuntimeError("no")

    s = scheduler(connect, clock=lambda: clock[0])
    s.add("flaky", HOUR, flaky)
    s.run_due()
    clock[0] = NOW + timedelta(minutes=5)
    assert s.run_due() == []
    assert len(ran) == 1


def test_a_long_message_is_cut_to_fit(connect, admin):
    s = scheduler(connect)
    s.add("wordy", HOUR, lambda conn: "x" * 2000)
    s.run_due()
    assert len(row(admin, "wordy")["last_message"]) == 500


def test_no_database_is_logged_and_tried_again_later(caplog):
    def nowhere():
        raise psycopg.OperationalError("connection refused")

    s = scheduler(nowhere)
    s.add("any", HOUR, lambda conn: None)
    with caplog.at_level(logging.WARNING, logger="test.jobs"):
        assert s.run_due() == []
    assert "connection refused" in caplog.text


# ---- what a page can show --------------------------------------------------------

def test_status_lists_every_job_with_its_last_run_and_when_it_is_next_due(connect):
    s = scheduler(connect)
    s.add("never_ran", timedelta(days=1), lambda conn: None, "Not yet run")
    s.add("ran", HOUR, lambda conn: "fine", "Runs hourly")
    with connect() as conn:
        before = {j["name"]: j for j in s.status(conn)}
    assert before["never_ran"]["last_started_at"] is None and before["never_ran"]["next_due_at"] is None
    s.jobs.pop("never_ran")
    s.run_due()
    s.add("never_ran", timedelta(days=1), lambda conn: None, "Not yet run")
    with connect() as conn:
        listed = s.status(conn)
    assert [j["name"] for j in listed] == ["never_ran", "ran"]
    ran = listed[1]
    assert ran["description"] == "Runs hourly" and ran["every"] == HOUR
    assert ran["last_ok"] is True and ran["last_message"] == "fine"
    assert ran["next_due_at"] == NOW + HOUR


# ---- the loop --------------------------------------------------------------------

def test_the_loop_runs_what_is_due_until_it_is_told_to_stop(connect):
    stop, ran = threading.Event(), []

    def once(conn):
        ran.append(1)
        stop.set()

    s = scheduler(connect)
    s.add("once", HOUR, once)
    s.run_forever(stop, tick=0.01, delay=0)
    assert ran == [1]


def test_the_loop_survives_a_pass_that_blows_up(connect, caplog):
    stop, passes = threading.Event(), []
    s = scheduler(connect)

    def run_due():
        passes.append(1)
        if len(passes) == 1:
            raise RuntimeError("unexpected")
        stop.set()
        return []

    s.run_due = run_due
    with caplog.at_level(logging.ERROR, logger="test.jobs"):
        s.run_forever(stop, tick=0.01, delay=0)
    assert len(passes) == 2 and "unexpected" in caplog.text


# ---- where it runs ---------------------------------------------------------------

@pytest.mark.parametrize("value, mode", [(None, "app"), ("", "app"), ("app", "app"), (" Worker ", "worker"),
                                         ("off", "off")])
def test_scheduler_setting(value, mode):
    assert jobs.mode({} if value is None else {"SCHEDULER": value}) == mode


def test_an_unknown_scheduler_setting_is_refused():
    with pytest.raises(ValueError, match="SCHEDULER"):
        jobs.mode({"SCHEDULER": "cron"})


def test_the_app_has_its_scheduler_with_the_built_in_jobs_and_starts_no_thread_under_test(pg, admin, monkeypatch, connect):
    monkeypatch.delenv("BASE_PATH", raising=False)
    before = threading.active_count()
    app = create_app(connect=connect, ato_fetch=ato_pages())          # the ATO's pages as saved: no network in tests
    s = app.extensions["scheduler"]
    assert {"teslamate", "dashboard"} <= set(s.jobs)
    assert threading.active_count() == before
    assert sorted(s.run_due()) == sorted(s.jobs)        # each built-in job runs against a real database
    for name in s.jobs:
        assert row(admin, name)["last_ok"] is True, row(admin, name)


@pytest.mark.parametrize("setting, background_jobs, started", [
    (None, True, True), ("app", True, True), ("worker", True, False), ("off", True, False),
    (None, False, False),           # the worker itself: it runs the loop in the foreground
    ("cron", True, True),           # an unknown value is reported and treated as unset
])
def test_a_real_start_runs_the_jobs_in_the_background_only_where_scheduler_says(
        blank_db, monkeypatch, caplog, setting, background_jobs, started):
    monkeypatch.setenv("DATABASE_URL", blank_db)
    monkeypatch.delenv("SCHEDULER")
    if setting:
        monkeypatch.setenv("SCHEDULER", setting)
    threads = []
    monkeypatch.setattr(jobs.Scheduler, "start_thread", lambda self: threads.append(self))
    with caplog.at_level(logging.INFO):
        app = create_app(background_jobs=background_jobs)
    assert (threads == [app.extensions["scheduler"]]) is started
    assert app.extensions["scheduler_mode"] == ("app" if setting in (None, "cron") else setting)
    assert ("SCHEDULER must be one of" in caplog.text) is (setting == "cron")
    assert "scheduled jobs: " in caplog.text


def test_the_background_thread_runs_the_loop_and_stops_when_told(connect):
    ran = threading.Event()
    s = scheduler(connect)
    s.add("once", HOUR, lambda conn: ran.set())
    s.run_forever = lambda stop: jobs.Scheduler.run_forever(s, stop, tick=0.01, delay=0)
    stop = s.start_thread()
    try:
        assert ran.wait(10)
    finally:
        stop.set()


def test_the_worker_command_runs_the_apps_scheduler_until_stopped(monkeypatch, connect):
    from garagebooks import __main__ as cli

    seen = {}

    class FakeScheduler:
        def run_forever(self, stop):
            seen["ran"] = True
            assert not stop.is_set()

    class FakeApp:
        extensions = {"scheduler": FakeScheduler()}

    def fake_create_app(**kwargs):
        seen["kwargs"] = kwargs
        return FakeApp()

    monkeypatch.setattr(cli, "create_app", fake_create_app)
    assert cli.main(["worker"]) == 0
    assert seen == {"ran": True, "kwargs": {"background_jobs": False}}


def test_the_command_line_explains_itself(capsys):
    from garagebooks import __main__ as cli

    assert cli.main([]) == 2
    assert cli.main(["nonsense"]) == 2
    assert "worker" in capsys.readouterr().err
