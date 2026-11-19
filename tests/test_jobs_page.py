"""The Settings page lists the scheduled jobs: what each does, how often, when it last ran and how it
went, and says where the jobs run. Read-only."""

import re
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from psycopg.rows import dict_row

from garagebooks import compat, create_app, jobtext

from .test_compat import set_newest

START = datetime(2026, 11, 16, 2, 0, tzinfo=UTC)         # 12:00 in Brisbane


@pytest.fixture()
def clean(admin):
    admin.execute("DELETE FROM garagebooks.job")
    try:
        yield admin
    finally:
        admin.execute("DELETE FROM garagebooks.job")


def start(url, monkeypatch, scheduler="app", real=False):
    monkeypatch.delenv("BASE_PATH", raising=False)
    monkeypatch.setenv("APP_TZ", "Australia/Brisbane")
    monkeypatch.setenv("SCHEDULER", scheduler)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    if real:        # as a real start: it makes its own schema in a database that has none
        monkeypatch.setenv("DATABASE_URL", url)
        app = create_app()
    else:
        app = create_app(connect=lambda: psycopg.connect(url, row_factory=dict_row))
    app.testing = True
    return app.test_client()


def job_html(page, name):
    return re.search(rf'<li[^>]*data-job="{name}".*?</li>', page, re.S).group(0)


def text_of(html):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).strip()


def test_a_job_that_has_never_run_says_so(pg, clean, monkeypatch):
    page = start(pg.app_url, monkeypatch).get("/settings").data.decode()
    assert 'id="scheduled-jobs"' in page and "Scheduled jobs" in page
    for name in ("teslamate", "dashboard"):
        assert f'data-job="{name}"' in page
    one = job_html(page, "teslamate")
    assert 'data-state="never"' in one and "Not run yet" in one
    assert "every hour" in one and "every 6 hours" in job_html(page, "dashboard")
    assert "Looks at TeslaMate again" in one          # its description


def test_a_job_that_went_well_shows_when_it_ran_in_the_app_timezone_and_when_it_is_next_due(pg, clean, monkeypatch):
    clean.execute("INSERT INTO garagebooks.job (name, last_started_at, last_finished_at, last_ok, last_message)"
                  " VALUES ('teslamate', %s, %s, true, 'TeslaMate: ok')", (START, START + timedelta(seconds=2)))
    one = job_html(start(pg.app_url, monkeypatch).get("/settings").data.decode(), "teslamate")
    assert 'data-state="ok"' in one and "OK" in one and "TeslaMate: ok" in one
    assert "16 Nov 2026 12:00" in one                  # the last run, in Brisbane time
    assert "16 Nov 2026 13:00" in one                  # an hour later: next due


def test_a_failed_job_shows_its_message(pg, clean, monkeypatch):
    clean.execute("INSERT INTO garagebooks.job (name, last_started_at, last_finished_at, last_ok, last_message)"
                  " VALUES ('dashboard', %s, %s, false, %s)", (START, START, "The dashboard folder is read-only <b>x</b>"))
    one = job_html(start(pg.app_url, monkeypatch).get("/settings").data.decode(), "dashboard")
    assert 'data-state="failed"' in one and "Failed" in one
    assert "The dashboard folder is read-only &lt;b&gt;x&lt;/b&gt;" in one      # escaped, never markup
    assert "<b>x</b>" not in one


def test_a_job_started_and_not_finished_is_running_or_was_interrupted(pg, clean, monkeypatch):
    clean.execute("INSERT INTO garagebooks.job (name, last_started_at) VALUES ('teslamate', %s)", (START,))
    one = job_html(start(pg.app_url, monkeypatch).get("/settings").data.decode(), "teslamate")
    assert 'data-state="running"' in one and "Running, or interrupted" in one


@pytest.mark.parametrize("setting, words", [
    ("app", "by this app"), ("worker", "worker container"), ("off", "switched off")])
def test_the_page_says_where_the_jobs_run(pg, clean, monkeypatch, setting, words):
    page = start(pg.app_url, monkeypatch, setting).get("/settings").data.decode()
    sentence_html = re.search(r'<p[^>]*id="jobs-where".*?</p>', page, re.S).group(0)
    sentence = text_of(sentence_html)
    assert words in sentence
    assert ("garagebooks/scheduled-jobs/" in sentence_html) is (setting == "off")           # off points to the documentation


def test_the_section_is_read_only(pg, clean, monkeypatch):
    page = start(pg.app_url, monkeypatch).get("/settings").data.decode()
    section = page[page.index('id="scheduled-jobs"'):]
    section = section[:section.index("</section>")]
    assert "<form" not in section and "<button" not in section and "|safe" not in section


@pytest.mark.parametrize("every, words", [
    (timedelta(minutes=1), "every minute"), (timedelta(minutes=30), "every 30 minutes"),
    (timedelta(hours=1), "every hour"), (timedelta(hours=6), "every 6 hours"),
    (timedelta(days=1), "every day"), (timedelta(days=7), "every 7 days"), (timedelta(minutes=90), "every 90 minutes")])
def test_how_often_is_said_in_words(every, words):
    assert jobtext.every_in_words(every) == words


# ---- with TeslaMate present, absent and held back -------------------------------------------------

def test_the_section_is_there_with_teslamate_present(pg, clean, monkeypatch):
    assert 'id="scheduled-jobs"' in start(pg.app_url, monkeypatch).get("/settings").data.decode()


def test_the_section_is_there_with_no_teslamate_at_all(empty_db, monkeypatch):
    page = start(empty_db, monkeypatch, real=True).get("/settings").data.decode()
    assert 'data-job="teslamate"' in page and 'data-state="never"' in page


@pytest.mark.teslamate
def test_the_section_is_there_with_teslamate_held_back(blank_db, monkeypatch):
    set_newest(blank_db, compat.TESTED_MIGRATION + 1)
    client = start(blank_db, monkeypatch, real=True)
    page = client.get("/settings").data.decode()
    assert 'data-job="dashboard"' in page and 'data-state="never"' in page
