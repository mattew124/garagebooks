"""The tax rate by income year: each year's ATO brackets as data, the bracket and Medicare levy status chosen for
each income year (1 July to 30 June) on the Settings page, the marginal rate an expense's date gives, the move
from the single rate of earlier releases, and the scheduled job that reads the brackets and the home charging
rate from the ATO. No salary is ever stored. All figures are the ATO's published rates or invented.
Runs with TeslaMate absent, present and held back (the rate never reads TeslaMate)."""

import re
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import psycopg
import pytest

from garagebooks import ato, db, jobs, tax
from tests.test_ato_rates import RESIDENT
from tests.test_fbt import text
from tests.test_lease import ORIGIN, app, connect  # noqa: F401 - app is a fixture
from tests.test_sources import start

D = Decimal
EV_PAGE = (Path(__file__).parent / "fixtures" / "ato-ev-charging.html").read_text()

# The ATO's published resident brackets, as the schema seeds them: {income year: [(first dollar, rate %)]}.
SEEDED = {
    **{y: [(0, D("0")), (18201, D("19")), (45001, D("32.5")), (120001, D("37")), (180001, D("45"))] for y in range(2020, 2024)},
    **{y: [(0, D("0")), (18201, D("16")), (45001, D("30")), (135001, D("37")), (190001, D("45"))] for y in (2024, 2025)},
    2026: [(0, D("0")), (18201, D("15")), (45001, D("30")), (135001, D("37")), (190001, D("45"))],
}


def brackets(conn):
    out = {}
    for r in conn.execute("SELECT * FROM garagebooks.tax_bracket ORDER BY income_year, lower_income"):
        out.setdefault(r["income_year"], []).append((r["lower_income"], r["rate"]))
    return out


def marginal(conn, person, day):
    return conn.execute("SELECT garagebooks.marginal_rate(%s, %s) AS r", (person, day)).fetchone()["r"]


def choose(conn, year, lower, medicare="not_exempt", person="employee"):
    db.set_tax_choice(conn, person, year, lower, medicare)


# ---- the data ------------------------------------------------------------------------------------------------

def test_the_published_brackets_from_2020_21_are_seeded_with_their_tax_at_each_start(empty_db, monkeypatch):
    start(empty_db, monkeypatch)
    with connect(empty_db) as conn:
        assert brackets(conn) == SEEDED
        row = conn.execute("SELECT * FROM garagebooks.tax_bracket WHERE income_year = 2025 AND lower_income = 135001").fetchone()
        assert (row["threshold"], row["base_tax"]) == (D("135000"), D("31288.00"))
        assert conn.execute("SELECT count(*) AS n FROM garagebooks.tax_choice").fetchone()["n"] == 0


def test_the_income_year_runs_1_july_to_30_june(empty_db, monkeypatch):
    start(empty_db, monkeypatch)
    with connect(empty_db) as conn:
        for day, year in ((date(2025, 6, 30), 2024), (date(2025, 7, 1), 2025), (date(2026, 3, 31), 2025)):
            assert conn.execute("SELECT garagebooks.income_year(%s) AS y", (day,)).fetchone()["y"] == year
            assert tax.income_year(day) == year


def test_the_marginal_rate_is_the_years_bracket_plus_the_medicare_levy(empty_db, monkeypatch):
    start(empty_db, monkeypatch)
    with connect(empty_db) as conn:
        assert marginal(conn, "employee", date(2025, 11, 1)) is None              # nothing chosen: no rate
        choose(conn, 2025, 45001)
        assert marginal(conn, "employee", date(2025, 11, 1)) == D("32")          # 30% + 2%
        assert marginal(conn, "employee", date(2025, 7, 1)) == D("32")
        assert marginal(conn, "employee", date(2025, 6, 30)) is None             # 2024-25: no choice at or before it
        choose(conn, 2025, 45001, "half_exempt")
        assert marginal(conn, "employee", date(2025, 11, 1)) == D("31")
        choose(conn, 2025, 45001, "exempt")
        assert marginal(conn, "employee", date(2025, 11, 1)) == D("30")
        choose(conn, 2022, 45001)
        assert marginal(conn, "employee", date(2023, 1, 1)) == D("34.5")         # 32.5% + 2%
        assert marginal(conn, "associate", date(2023, 1, 1)) is None             # a second person is separate


def test_a_year_with_no_choice_uses_the_latest_earlier_years_bracket_for_the_income_it_stands_for(empty_db, monkeypatch):
    start(empty_db, monkeypatch)
    with connect(empty_db) as conn:
        choose(conn, 2022, 120001)                     # $120,001 - $180,000 in 2022-23: its middle is $150,000.50
        assert marginal(conn, "employee", date(2023, 3, 1)) == D("39")
        assert marginal(conn, "employee", date(2024, 9, 1)) == D("39")            # 2024-25: $135,001 - $190,000 (37%)
        choose(conn, 2022, 45001)                      # middle $82,500.50: 30% from 2024-25
        assert marginal(conn, "employee", date(2025, 9, 1)) == D("32")
        assert marginal(conn, "employee", date(2031, 9, 1)) == D("32")            # a year the ATO hasn't published: the newest
        found = tax_position(conn, 2025)
        assert found["chosen_year"] == 2022 and found["table_year"] == 2025 and found["lower_income"] == 45001


def tax_position(conn, year, person="employee"):
    return conn.execute("SELECT * FROM garagebooks.tax_position(%s, %s)", (person, year)).fetchone()


def test_a_choice_must_name_a_bracket_of_its_year_and_a_known_medicare_status(empty_db, monkeypatch):
    start(empty_db, monkeypatch)
    with connect(empty_db) as conn:
        for sql in ("INSERT INTO garagebooks.tax_choice VALUES ('employee', 2025, 45001, 'sometimes')",
                    "INSERT INTO garagebooks.tax_choice VALUES ('someone', 2025, 45001, 'exempt')"):
            with pytest.raises(psycopg.errors.CheckViolation):
                with conn.transaction():
                    conn.execute(sql)


def test_the_bracket_labels_read_like_the_atos(empty_db, monkeypatch):
    start(empty_db, monkeypatch)
    with connect(empty_db) as conn:
        labels = [b["label"] for b in tax.year_brackets(db.tax_brackets(conn), 2025)]
    assert labels == ["$0 – $18,200 (0%)", "$18,201 – $45,000 (16%)", "$45,001 – $135,000 (30%)",
                      "$135,001 – $190,000 (37%)", "$190,001 and over (45%)"]


# ---- the single rate of earlier releases ----------------------------------------------------------------------

def test_an_earlier_single_rate_becomes_the_matching_bracket_for_the_current_and_earlier_years(empty_db, monkeypatch):
    start(empty_db, monkeypatch)
    with connect(empty_db) as conn:
        conn.execute("UPDATE garagebooks.setting SET marginal_tax_rate = 32")
    start(empty_db, monkeypatch)
    this_year = tax.income_year(date.today())
    with connect(empty_db) as conn:
        chosen = {r["income_year"]: (r["bracket_lower"], r["medicare"]) for r in conn.execute(
            "SELECT * FROM garagebooks.tax_choice WHERE person = 'employee'")}
        assert set(chosen) == set(range(2020, this_year + 1))
        assert chosen[2025] == (45001, "not_exempt")                              # 30% + 2% is 32: the figures are the same
        assert chosen[2022] == (45001, "not_exempt")                              # 32.5% + 2% = 34.5, the closest to 32
        assert marginal(conn, "employee", date(2025, 11, 1)) == D("32")
        assert conn.execute("SELECT marginal_tax_rate FROM garagebooks.setting").fetchone()["marginal_tax_rate"] is None
    start(empty_db, monkeypatch)                                                  # once only: a later start changes nothing
    with connect(empty_db) as conn:
        assert conn.execute("SELECT count(*) AS n FROM garagebooks.tax_choice").fetchone()["n"] == len(chosen)


def test_running_the_schema_again_keeps_the_choices_and_a_refreshed_bracket(empty_db, monkeypatch):
    start(empty_db, monkeypatch)
    with connect(empty_db) as conn:
        choose(conn, 2025, 135001, "half_exempt")
        conn.execute("UPDATE garagebooks.tax_bracket SET checked_at = now() WHERE income_year = 2025")
    start(empty_db, monkeypatch)
    with connect(empty_db) as conn:
        assert marginal(conn, "employee", date(2025, 11, 1)) == D("38")
        assert conn.execute("SELECT count(*) AS n FROM garagebooks.tax_bracket WHERE checked_at IS NOT NULL").fetchone()["n"] == 5


# ---- Settings -------------------------------------------------------------------------------------------------

def post_settings(client, **over):
    return client.post("/settings", data={"app_url": "", "grafana_url": "", **over}, headers=ORIGIN)


def test_settings_show_one_row_per_income_year_with_a_bracket_and_a_medicare_choice(app):
    client, url, _ = app
    page = client.get("/settings").data.decode()
    table = re.search(r'id="tax-years".*?</table>', page, re.S).group(0)
    assert 'name="tax_employee_2025_bracket"' in table and 'name="tax_employee_2025_medicare"' in table
    assert 'name="tax_employee_2020_bracket"' in table
    assert "$45,001 – $135,000 (30%)" in table and "Half exempt" in table and "Not exempt" in table
    assert 'name="marginal_tax_rate"' not in page and "salary" in text(client.get("/settings")).lower()


def test_settings_save_a_years_choice_and_say_which_years_follow_it(app):
    client, url, _ = app
    assert post_settings(client, tax_employee_2024_bracket="45001", tax_employee_2024_medicare="half_exempt").status_code == 303
    with connect(url) as conn:
        assert marginal(conn, "employee", date(2024, 11, 1)) == D("31")
        assert marginal(conn, "employee", date(2025, 11, 1)) == D("31")
    page = client.get("/settings").data.decode()
    assert re.search(r'name="tax_employee_2024_bracket".*?<option value="45001"\s+selected', page, re.S)
    row = re.search(r'<tr data-year="2025".*?</tr>', page, re.S).group(0)
    assert "2024–25" in text_of_html(row)                                          # "uses 2024–25's choice"
    assert post_settings(client, tax_employee_2024_bracket="").status_code == 303   # blank: no choice for the year
    with connect(url) as conn:
        assert marginal(conn, "employee", date(2024, 11, 1)) is None


def text_of_html(html):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))


def test_a_bracket_that_is_not_one_of_the_years_is_refused_and_nothing_is_saved(app):
    client, url, _ = app
    for bad in ({"tax_employee_2025_bracket": "45000"}, {"tax_employee_2025_bracket": "lots"},
                {"tax_employee_2025_bracket": "45001", "tax_employee_2025_medicare": "sometimes"}):
        resp = post_settings(client, **bad)
        assert resp.status_code == 400 and "bracket" in text(resp).lower() or "medicare" in text(resp).lower()
    with connect(url) as conn:
        assert conn.execute("SELECT count(*) AS n FROM garagebooks.tax_choice").fetchone()["n"] == 0


def test_the_home_charging_rates_are_a_read_only_table_with_their_source(app):
    client, url, _ = app
    page = client.get("/settings").data.decode()
    table = re.search(r'id="ev-rates".*?</table>', page, re.S).group(0)
    assert "FBT 2026–27" in table and "5.47" in table and "4.20" in table
    assert "<input" not in table and 'name="ev_rate_' not in page and 'name="new_rate_year"' not in page
    assert "Last checked" in table or "last checked" in table.lower()
    assert post_settings(client, ev_rate_2026="9.99").status_code == 303           # an old form's field is ignored
    with connect(url) as conn:
        assert db.ev_charging_rates(conn)[2026] == D("5.47")


# ---- reading the ATO's pages -----------------------------------------------------------------------------------

def test_the_ev_home_charging_rate_is_read_from_the_atos_table():
    assert ato.parse_ev_charging(EV_PAGE) == {2022: "4.20", 2026: "5.47"}


def test_a_changed_ev_page_is_refused_not_guessed():
    with pytest.raises(ato.RatesError):
        ato.parse_ev_charging("<p>Nothing here</p>")
    with pytest.raises(ato.RatesError):
        ato.parse_ev_charging(EV_PAGE.replace("5.47 cents per km", "about five and a half cents"))


def pages(over=None):
    found = {ato.URLS["resident"]: RESIDENT, ato.URLS["ev_charging"]: EV_PAGE, **(over or {})}

    def fetch(url):
        if isinstance(found[url], Exception):
            raise found[url]
        return found[url]
    return fetch


def test_the_job_keeps_the_brackets_and_rates_and_notes_when_they_were_checked(empty_db, monkeypatch):
    start(empty_db, monkeypatch)
    now = datetime(2026, 11, 20, 3, 0, tzinfo=timezone.utc)
    with connect(empty_db) as conn:
        said = tax.refresh(conn, pages(), now)
        assert "2026–27" in said or "2026-27" in said
        assert brackets(conn)[2026] == SEEDED[2026] and brackets(conn)[2022] == SEEDED[2022]
        checked = {r["income_year"]: r["checked_at"] for r in conn.execute("SELECT * FROM garagebooks.tax_bracket")}
        assert checked[2026] == now and checked[2022] == now and checked[2025] is None      # only what the page lists
        ev = {r["fbt_year"]: r for r in conn.execute("SELECT * FROM garagebooks.ev_charging_rate")}
        assert ev[2026]["checked_at"] == now and ev[2026]["source_url"] == ato.URLS["ev_charging"]
        assert ev[2024]["checked_at"] is None and ev[2024]["cents_per_km"] == D("4.20")


def test_the_job_takes_a_changed_rate_and_a_new_year(empty_db, monkeypatch):
    start(empty_db, monkeypatch)
    newer = RESIDENT.replace("2026&ndash;27", "2027&ndash;28").replace("15c for each $1 over $18,200", "14c for each $1 over $18,200") \
        .replace("$4,020 plus", "$3,752 plus").replace("$31,020 plus", "$30,752 plus").replace("$51,370 plus", "$51,102 plus")
    ev = EV_PAGE.replace("5.47 cents", "5.80 cents")
    with connect(empty_db) as conn:
        tax.refresh(conn, pages({ato.URLS["resident"]: newer, ato.URLS["ev_charging"]: ev}), datetime.now(timezone.utc))
        assert brackets(conn)[2027][1] == (18201, D("14"))
        assert db.ev_charging_rates(conn)[2026] == D("5.80")


def test_a_failed_read_keeps_the_last_good_set_and_says_why(empty_db, monkeypatch):
    start(empty_db, monkeypatch)
    with connect(empty_db) as conn:
        before_brackets, before_ev = brackets(conn), db.ev_charging_rates(conn)
        for broken in ({ato.URLS["resident"]: "<p>moved</p>"}, {ato.URLS["ev_charging"]: ato.RatesError("offline")},
                       {ato.URLS["resident"]: RESIDENT.replace("$31,020 plus", "$1 plus")}):
            with pytest.raises(jobs.JobError):
                tax.refresh(conn, pages(broken), datetime.now(timezone.utc))
            assert brackets(conn) == before_brackets and db.ev_charging_rates(conn) == before_ev


def test_the_job_is_on_the_settings_page(app):
    client, _, _ = app
    assert 'data-job="ato_rates"' in client.get("/settings").data.decode()


def test_the_restricted_role_can_keep_the_brackets_and_choices(pg, admin):
    with psycopg.connect(pg.app_url) as conn:                                      # one transaction, rolled back
        conn.execute("INSERT INTO garagebooks.tax_choice VALUES ('employee', 2025, 45001, 'not_exempt')")
        conn.execute("UPDATE garagebooks.tax_bracket SET checked_at = now() WHERE income_year = 2025")
        conn.execute("UPDATE garagebooks.ev_charging_rate SET checked_at = now(), source_url = 'https://www.ato.gov.au/x'")
        assert conn.execute("SELECT garagebooks.marginal_rate('employee', '2025-11-01')").fetchone()[0] == D("32")
        conn.execute("DELETE FROM garagebooks.tax_choice")
        conn.rollback()
    assert pg.provision().returncode == 0
