"""The suite never reaches the internet. A scheduled job that reads a public page (the ATO's rates) gets a
stand-in in tests unless a test gives it one of its own: a real fetch made runs slow and, in CI, flaky (a job
waiting on the network held the scheduler up long enough for another job to be skipped)."""

import urllib.request

from garagebooks import ato, create_app


def test_the_ato_rates_job_never_opens_a_connection_in_tests(empty_db, monkeypatch):
    opened = []
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: opened.append(a) or (_ for _ in ()).throw(OSError("no")))
    monkeypatch.setenv("DATABASE_URL", empty_db)
    app = create_app()
    with app.app_context():
        with app.extensions["scheduler"].connect() as conn:
            conn.execute("DELETE FROM garagebooks.job WHERE name = 'ato_rates'")
        assert "ato_rates" in app.extensions["scheduler"].run_due()
    assert opened == []


def test_the_stand_in_refuses_at_once():
    try:
        ato.get("https://www.ato.gov.au/tax-rates-and-codes/tax-rates-australian-residents")
    except ato.RatesError as exc:
        assert "tests" in str(exc)
    else:
        raise AssertionError("ato.get read the ATO's page during a test")
