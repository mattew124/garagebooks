"""The FBT year and the car's taxable value (roadmap L2): the date arithmetic, the statutory formula, the
contribution and pay counting, the per-FBT-year report, the SQL function the dashboard shares, and the
dashboard panel. All figures are invented. Runs with or without TeslaMate."""

import json
import re
from datetime import date, timedelta
from html import unescape
from decimal import Decimal

import psycopg
from psycopg.rows import dict_row

from garagebooks import dashboard, lease, sources
from tests.conftest import ROOT, TESLAMATE
from tests.test_lease import ORIGIN, app, budgets_on, connect, good_form, start, today_here  # noqa: F401 - app is a fixture
from tests.test_sources import STAND_IN, category, queries, runnable


def a_lease(**over):
    row = {"start_date": date(2025, 10, 1), "end_date": date(2028, 9, 30), "pay_frequency": "fortnightly",
           "posttax_payment": Decimal("20.00"), "fbt_base_value": Decimal("62000.00"), "fbt_method": "statutory",
           "statutory_percent": Decimal("20.00"), "ev_exempt": False, "tax_treatment": "employee_contribution"}
    row.update(over)
    return row


# ---- the date arithmetic ---------------------------------------------------------------------------

def test_the_fbt_year_runs_from_1_april_to_31_march():
    assert lease.fbt_year(date(2026, 3, 31)) == 2025
    assert lease.fbt_year(date(2026, 4, 1)) == 2026
    assert lease.fbt_year(date(2026, 1, 1)) == 2025 and lease.fbt_year(date(2026, 12, 31)) == 2026
    assert (lease.fbt_start(2025), lease.fbt_end(2025)) == (date(2025, 4, 1), date(2026, 3, 31))
    assert lease.fbt_label(2025) == "FBT 2025–26" and lease.fbt_label(2099) == "FBT 2099–00"


def test_days_in_an_fbt_year_count_the_leap_day_only_when_it_falls_inside():
    assert lease.days_in_fbt_year(2025) == 365
    assert lease.days_in_fbt_year(2023) == 366       # 1 April 2023 to 31 March 2024 holds 29 February 2024
    assert lease.days_in_fbt_year(2024) == 365       # 2025 is not a leap year
    assert lease.days_in_fbt_year(2027) == 366       # holds 29 February 2028
    assert lease.days_in_fbt_year(2099) == 365       # 2100 is not a leap year


def test_the_income_tax_year_runs_from_1_july_to_30_june():
    assert lease.income_tax_year(date(2026, 6, 30)) == 2025 and lease.income_tax_year(date(2026, 7, 1)) == 2026
    assert lease.income_tax_label(2025) == "2025–26"
    assert lease.fbt_year(date(2026, 5, 1)) == 2026 and lease.income_tax_year(date(2026, 5, 1)) == 2025   # the two differ


def test_days_held_are_clipped_to_the_year_with_both_ends_counted():
    full = a_lease(start_date=date(2024, 4, 1), end_date=date(2027, 3, 31))
    assert lease.days_held(full, 2025) == 365 and lease.days_held(full, 2023) == 0
    assert lease.days_held(full, 2024) == 365 and lease.days_held(full, 2026) == 365
    starts = a_lease()                                   # starts 1 October 2025: 1 Oct to 31 Mar = 182 days
    assert lease.days_held(starts, 2025) == 182
    assert lease.days_held(starts, 2024) == 0
    ends = a_lease(start_date=date(2022, 6, 1), end_date=date(2025, 9, 30))
    assert lease.days_held(ends, 2025) == 183            # 1 April to 30 September
    one_day = a_lease(start_date=date(2026, 3, 31), end_date=date(2026, 4, 1))
    assert lease.days_held(one_day, 2025) == 1 and lease.days_held(one_day, 2026) == 1     # both ends are counted
    leap = a_lease(start_date=date(2024, 1, 1), end_date=date(2026, 3, 31))
    assert lease.days_held(leap, 2023) == 91             # 1 January to 31 March 2024: 31 + 29 + 31


def test_a_refinanced_car_counts_each_leases_days_in_the_year():
    first = a_lease(start_date=date(2022, 10, 1), end_date=date(2025, 9, 30))
    second = a_lease(start_date=date(2025, 10, 1), end_date=date(2028, 9, 30))
    assert (lease.days_held(first, 2025), lease.days_held(second, 2025)) == (183, 182)
    assert lease.days_held(first, 2025) + lease.days_held(second, 2025) == lease.days_in_fbt_year(2025)


# ---- the taxable value --------------------------------------------------------------------------

def test_taxable_value_is_base_times_percent_times_days_over_days_in_year():
    # 62,000 x 20% = 12,400 a year. A full year is all of it.
    full = a_lease(start_date=date(2024, 4, 1), end_date=date(2027, 3, 31))
    assert lease.fbt_figures(full, 2025, date(2026, 6, 1))["taxable_value"] == Decimal("12400.00")
    # 182 of 365 days: 12,400 x 182 / 365 = 6,183.0137... -> 6,183.01
    assert lease.fbt_figures(a_lease(), 2025, date(2026, 6, 1))["taxable_value"] == Decimal("6183.01")
    # a leap year: 91 of 366 days: 12,400 x 91 / 366 = 3,083.0601... -> 3,083.06
    leap = a_lease(start_date=date(2024, 1, 1), end_date=date(2026, 3, 31))
    assert lease.fbt_figures(leap, 2023, date(2026, 6, 1))["taxable_value"] == Decimal("3083.06")


def test_the_percentage_is_the_leases_own_not_a_constant():
    ten = a_lease(start_date=date(2024, 4, 1), end_date=date(2027, 3, 31), statutory_percent=Decimal("10.00"))
    assert lease.fbt_figures(ten, 2025, date(2026, 6, 1))["taxable_value"] == Decimal("6200.00")
    odd = a_lease(start_date=date(2024, 4, 1), end_date=date(2027, 3, 31), fbt_base_value=Decimal("40001.00"),
                  statutory_percent=Decimal("7.50"))
    assert lease.fbt_figures(odd, 2025, date(2026, 6, 1))["taxable_value"] == Decimal("3000.08")    # 3,000.0750 rounds up


def test_rounding_happens_once_at_the_end():
    # 12,400 x 7 / 365 = 237.8082 -> 237.81 (seven days, each 33.97 rounded: 237.79)
    seven = a_lease(start_date=date(2026, 3, 25), end_date=date(2030, 1, 1))
    assert lease.days_held(seven, 2025) == 7
    assert lease.fbt_figures(seven, 2025, date(2026, 6, 1))["taxable_value"] == Decimal("237.81")


def test_an_exempt_electric_car_has_no_taxable_value_and_needs_no_contribution():
    figures = lease.fbt_figures(a_lease(ev_exempt=True), 2025, date(2026, 6, 1))
    assert figures["exempt"] and figures["taxable_value"] == Decimal("0.00") and figures["required"] == Decimal("0.00")


def test_the_operating_cost_method_is_not_worked_out():
    figures = lease.fbt_figures(a_lease(fbt_method="operating_cost"), 2025, date(2026, 6, 1))
    assert figures["taxable_value"] is None and figures["required"] is None
    assert "operating cost" in figures["reason"].lower() and "not worked out" in figures["reason"]


def test_without_a_base_value_there_is_nothing_to_work_out_and_it_says_so():
    figures = lease.fbt_figures(a_lease(fbt_base_value=None), 2025, date(2026, 6, 1))
    assert figures["taxable_value"] is None and "base value" in figures["reason"]


def test_a_year_the_car_was_not_held_has_nothing():
    figures = lease.fbt_figures(a_lease(), 2020, date(2026, 6, 1))
    assert figures["days_held"] == 0 and figures["taxable_value"] == Decimal("0.00") and figures["pays_in_year"] == 0


# ---- pays and contributions ---------------------------------------------------------------------

def test_pay_dates_step_from_the_lease_start_by_the_frequency():
    weekly = a_lease(pay_frequency="weekly", start_date=date(2025, 10, 1))
    dates = lease.pay_dates(weekly, date(2025, 10, 1), date(2025, 10, 31))
    assert dates == [date(2025, 10, 1), date(2025, 10, 8), date(2025, 10, 15), date(2025, 10, 22), date(2025, 10, 29)]
    fortnightly = a_lease(pay_frequency="fortnightly", start_date=date(2025, 10, 1))
    assert lease.pay_dates(fortnightly, date(2025, 10, 2), date(2025, 11, 12)) == [
        date(2025, 10, 15), date(2025, 10, 29), date(2025, 11, 12)]
    monthly = a_lease(pay_frequency="monthly", start_date=date(2025, 1, 31))
    assert lease.pay_dates(monthly, date(2025, 1, 1), date(2025, 6, 30)) == [
        date(2025, 1, 31), date(2025, 2, 28), date(2025, 3, 31), date(2025, 4, 30), date(2025, 5, 31), date(2025, 6, 30)]
    assert lease.pay_dates(monthly, date(2025, 3, 1), date(2025, 2, 1)) == []          # an empty range


def test_pays_in_the_held_part_of_a_year_for_each_frequency():
    # held 1 October 2025 to 31 March 2026: the last pay of each kind falls within the 181 days after the first
    for frequency, pays in {"weekly": 26, "fortnightly": 13, "monthly": 6}.items():
        figures = lease.fbt_figures(a_lease(pay_frequency=frequency), 2025, date(2026, 6, 1))
        assert figures["pays_in_year"] == pays, frequency
    # a whole year, paid from 3 April: 52 weekly, 26 fortnightly, 12 monthly pays
    for frequency, pays in {"weekly": 52, "fortnightly": 26, "monthly": 12}.items():
        full = a_lease(pay_frequency=frequency, start_date=date(2024, 4, 3), end_date=date(2028, 3, 31))
        assert lease.fbt_figures(full, 2025, date(2026, 6, 1))["pays_in_year"] == pays, frequency
    # paid from the first day, the 364th day after it is the last day of the year: one more weekly and fortnightly pay
    first_day = a_lease(pay_frequency="weekly", start_date=date(2025, 4, 1), end_date=date(2028, 3, 31))
    assert lease.fbt_figures(first_day, 2025, date(2026, 6, 1))["pays_in_year"] == 53
    assert lease.fbt_figures({**first_day, "pay_frequency": "fortnightly"}, 2025, date(2026, 6, 1))["pays_in_year"] == 27


def test_contributions_made_so_far_and_expected_by_year_end_against_what_is_required():
    # 13 fortnightly pays of $20.00 = $260.00 expected; required is the taxable value, 6,183.01
    today = date(2025, 12, 1)           # pays on 1 Oct, 15 Oct, 29 Oct, 12 Nov, 26 Nov have fallen: five
    figures = lease.fbt_figures(a_lease(), 2025, today)
    assert figures["pays_so_far"] == 5 and figures["made"] == Decimal("100.00")
    assert figures["pays_in_year"] == 13 and figures["expected"] == Decimal("260.00")
    assert figures["required"] == Decimal("6183.01")
    assert figures["difference"] == Decimal("-5923.01")           # expected less required: a shortfall
    figures = lease.fbt_figures(a_lease(posttax_payment=Decimal("500.00")), 2025, today)
    assert figures["expected"] == Decimal("6500.00") and figures["difference"] == Decimal("316.99")   # an excess


def test_nothing_is_made_before_the_year_starts_and_all_of_it_once_it_has_ended():
    assert lease.fbt_figures(a_lease(), 2025, date(2025, 9, 30))["made"] == Decimal("0.00")
    assert lease.fbt_figures(a_lease(), 2025, date(2026, 4, 1))["made"] == Decimal("260.00")
    assert lease.fbt_figures(a_lease(), 2025, date(2025, 10, 1))["pays_so_far"] == 1     # the day of a pay counts


# ---- budgets pro-rated by days held ---------------------------------------------------------------

def test_a_yearly_budget_is_pro_rated_by_days_held_and_follows_a_re_budget():
    budgets = [{"pool": "tyres", "yearly_amount": Decimal("730.00"), "effective_from": date(2025, 10, 1)}]
    row = a_lease()
    assert lease.budget_for_year(row, budgets, "tyres", 2025) == Decimal("364.00")        # 730 x 182 / 365
    assert lease.budget_for_year(row, budgets, "insurance", 2025) is None                 # no budget for that pool
    assert lease.budget_for_year(row, budgets, "tyres", 2020) is None                     # not held then
    later = budgets + [{"pool": "tyres", "yearly_amount": Decimal("365.00"), "effective_from": date(2026, 1, 1)}]
    # 1 Oct to 31 Dec = 92 days at 730 a year, then 1 Jan to 31 Mar = 90 days at 365: 184 + 90 = 274
    assert lease.budget_for_year(row, later, "tyres", 2025) == Decimal("274.00")


# ---- the SQL function agrees with Python -----------------------------------------------------------

def test_the_sql_function_agrees_with_the_python_one_across_many_dates(empty_db, monkeypatch):
    start(empty_db, monkeypatch)
    days = [date(1999, 1, 1) + timedelta(days=n) for n in range(0, 365 * 40, 7)]
    days += [date(y, m, d) for y in range(2019, 2031) for m, d in ((3, 31), (4, 1), (12, 31), (1, 1))]
    days += [date(2024, 2, 29), date(2028, 2, 29)]
    with connect(empty_db) as conn:
        for day in days:
            got = conn.execute("SELECT garagebooks.fbt_year(%s) AS y", (day,)).fetchone()["y"]
            assert got == lease.fbt_year(day), day
        info = conn.execute("SELECT provolatile FROM pg_proc WHERE proname = 'fbt_year'").fetchone()
        assert info["provolatile"] == "i"                           # IMMUTABLE, so it can be indexed or used in a view
    start(empty_db, monkeypatch)                                    # the app starts again: the schema runs again
    with connect(empty_db) as conn:
        assert conn.execute("SELECT garagebooks.fbt_year('2026-03-31') AS y").fetchone()["y"] == 2025


def test_the_restricted_role_can_use_the_function(pg, admin):
    with psycopg.connect(pg.app_url, row_factory=dict_row, autocommit=True) as conn:
        assert conn.execute("SELECT garagebooks.fbt_year('2026-04-01') AS y").fetchone()["y"] == 2026
    assert pg.provision().returncode == 0
    with psycopg.connect(pg.app_url, row_factory=dict_row, autocommit=True) as conn:
        assert conn.execute("SELECT garagebooks.fbt_year('2026-03-31') AS y").fetchone()["y"] == 2025


# ---- the report -----------------------------------------------------------------------------------

def add_lease(client, car_id, **over):
    over = {"start_date": "2025-10-01", "end_date": "2028-09-30", **over}
    assert client.post(f"/cars/{car_id}/lease/new", data=good_form(**over), headers=ORIGIN).status_code == 303


def expense(conn, car_id, name, when, amount):
    conn.execute("INSERT INTO garagebooks.expense (car_id, category_id, occurred_at, amount) VALUES (%s, %s, %s, %s)",
                 (car_id, category(conn, name), when, amount))


def text_of(html):
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", html)))


def text(response):
    return text_of(response.data.decode())


def pool_line(body, pool):
    """The text of one pool's row of the running-costs table."""
    return text_of(re.search(rf'data-pool="{pool}"[^>]*>(.*?)</tr>', body, re.S).group(1))


def total_line(body):
    return text_of(re.search(r'id="pool-total".*?</tr>', body, re.S).group(0))


def test_the_report_of_an_unknown_car_is_not_found_and_a_bad_year_too(app):
    client, _, car_id = app
    assert client.get("/cars/9999/fbt").status_code == 404
    assert client.get(f"/cars/{car_id}/fbt?year=abc").status_code == 404
    assert client.get(f"/cars/{car_id}/fbt?year=1850").status_code == 404


def test_a_car_with_no_lease_is_told_how_to_add_one(app):
    client, _, car_id = app
    page = client.get(f"/cars/{car_id}/fbt")
    assert page.status_code == 200
    body = page.data.decode()
    assert 'id="fbt-nolease"' in body and f"/cars/{car_id}/lease/new" in body


def test_the_lease_page_links_to_the_report(app):
    client, _, car_id = app
    add_lease(client, car_id)
    assert f"/cars/{car_id}/fbt" in client.get(f"/cars/{car_id}/lease").data.decode()


def test_the_report_groups_costs_by_pool_and_keeps_what_is_not_in_one(app):
    client, url, car_id = app
    budgets_on(url)
    add_lease(client, car_id)
    with connect(url) as conn:
        expense(conn, car_id, "Insurance", "2025-11-10T00:00+10", 1200)
        expense(conn, car_id, "Insurance", "2026-02-10T00:00+10", 30)
        expense(conn, car_id, "Tyres", "2025-12-01T00:00+10", 800)
        expense(conn, car_id, "Charging", "2026-01-05T00:00+10", 55.5)
        expense(conn, car_id, "Initial purchase", "2025-10-02T00:00+10", 62000)
        expense(conn, car_id, "Insurance", "2026-04-02T00:00+10", 999)          # the next FBT year
        conn.execute("INSERT INTO garagebooks.category (name, kind, sort_order) VALUES ('Detailing', 'running', 100)")
        expense(conn, car_id, "Detailing", "2025-12-20T00:00+10", 90)
    page = client.get(f"/cars/{car_id}/fbt?year=2025")
    body, shown = page.data.decode(), text(page)
    assert "FBT 2025–26" in shown
    assert "$1,230.00" in pool_line(body, "insurance")
    assert "$800.00" in pool_line(body, "tyres")
    assert "$55.50" in pool_line(body, "fuel_charging")
    assert "$90.00" in pool_line(body, "none") and "not in a pool" in pool_line(body, "none").lower()
    assert "$999.00" not in shown                                                                  # a later year stays out
    assert "$2,175.50" in total_line(body)                                                         # 1230 + 800 + 55.50 + 90
    purchases = re.search(r'id="purchases".*?</section>', body, re.S).group(0)
    assert "Initial purchase" in purchases and "$62,000.00" in purchases
    assert "$62,000.00" not in total_line(body) and "$64,175.50" not in shown                      # not in the running total


def test_an_expense_belongs_to_the_fbt_year_of_its_date_in_the_apps_time_zone(app):
    client, url, car_id = app
    add_lease(client, car_id, start_date="2024-04-01", end_date="2027-03-31")
    with connect(url) as conn:
        expense(conn, car_id, "Insurance", "2026-03-31T15:00Z", 100)      # 1 April 01:00 in Brisbane: FBT 2026
        expense(conn, car_id, "Insurance", "2026-03-31T13:59Z", 7)        # 31 March 23:59 in Brisbane: FBT 2025
    older, newer = text(client.get(f"/cars/{car_id}/fbt?year=2025")), text(client.get(f"/cars/{car_id}/fbt?year=2026"))
    assert "$7.00" in older and "$100.00" not in older
    assert "$100.00" in newer and "$7.00" not in newer


def test_the_report_shows_days_held_taxable_value_and_contributions(app):
    client, _, car_id = app
    add_lease(client, car_id, pay_frequency="fortnightly", posttax_payment="20.00", fbt_base_value="62000")
    shown = text(client.get(f"/cars/{car_id}/fbt?year=2025"))
    assert "182 of 365 days" in shown
    assert "$6,183.01" in shown                                     # the taxable value and the contribution that cancels it
    assert "$260.00" in shown and "13 pays" in shown                # expected by year end
    assert "not tax advice" in shown.lower()
    assert "pay dates" in shown.lower()                             # the stated assumption


def test_an_exempt_electric_car_shows_nil(app):
    client, _, car_id = app
    add_lease(client, car_id, ev_exempt="on")
    body = client.get(f"/cars/{car_id}/fbt?year=2025").data.decode()
    assert 'id="fbt-exempt"' in body
    assert "$6,183.01" not in body


def test_the_operating_cost_method_says_it_is_not_worked_out(app):
    client, _, car_id = app
    add_lease(client, car_id, fbt_method="operating_cost")
    shown = text(client.get(f"/cars/{car_id}/fbt?year=2025"))
    assert "statutory figure doesn't apply" in shown and "not worked out" in shown


def test_the_report_moves_between_years_that_have_data(app):
    client, url, car_id = app
    add_lease(client, car_id, start_date="2024-10-01", end_date="2028-09-30")
    body = client.get(f"/cars/{car_id}/fbt?year=2025").data.decode()
    assert f"/cars/{car_id}/fbt?year=2024" in body and f"/cars/{car_id}/fbt?year=2026" in body
    first = client.get(f"/cars/{car_id}/fbt?year=2024").data.decode()
    assert 'id="fbt-prev"' not in first and 'id="fbt-next"' in first
    default = text(client.get(f"/cars/{car_id}/fbt"))
    assert lease.fbt_label(lease.fbt_year(today_here())) in default            # no year given: the current FBT year


def test_two_leases_in_one_year_add_their_days_and_taxable_values(app):
    client, _, car_id = app
    add_lease(client, car_id, start_date="2022-10-01", end_date="2025-09-30", fbt_base_value="62000")
    add_lease(client, car_id, start_date="2025-10-01", end_date="2028-09-30", fbt_base_value="50000")
    shown = text(client.get(f"/cars/{car_id}/fbt?year=2025"))
    assert "365 of 365 days" in shown                                           # 183 + 182
    # 12,400 x 183 / 365 = 6,216.9863 -> 6,216.99 ; 10,000 x 182 / 365 = 4,986.3014 -> 4,986.30 ; together 11,203.29
    assert "$6,216.99" in shown and "$4,986.30" in shown and "$11,203.29" in shown


def test_the_budget_for_each_pool_is_shown_pro_rated_for_reference(app):
    client, url, car_id = app
    budgets_on(url)
    add_lease(client, car_id, budget_tyres="730", budget_insurance="")
    body = client.get(f"/cars/{car_id}/fbt?year=2025").data.decode()
    assert "$364.00" in pool_line(body, "tyres")                                # 730 x 182 / 365


def test_a_year_with_no_lease_days_still_lists_costs_and_says_the_car_was_not_leased(app):
    client, url, car_id = app
    add_lease(client, car_id)
    with connect(url) as conn:
        expense(conn, car_id, "Insurance", "2023-05-01T00:00+10", 300)
    shown = text(client.get(f"/cars/{car_id}/fbt?year=2023"))
    assert "$300.00" in shown and "0 of 366 days" in shown        # the FBT year holding 29 February 2024


# ---- TeslaMate's charging in the report ---------------------------------------------------------

def test_charging_cost_can_leave_out_what_was_entered_by_hand(empty_db, monkeypatch):
    start(empty_db, monkeypatch)
    with connect(empty_db) as conn:
        conn.execute("INSERT INTO garagebooks.car (id, name) VALUES (7, 'Car')")
        expense(conn, 7, "Charging", "2026-01-01T00:00Z", 60)
        state = sources.detect(conn)
        assert sources.charging_cost(conn, state, 7).value == 60
        assert sources.charging_cost(conn, state, 7, include_manual=False).value == 0       # not TeslaMate's, so nothing


def test_a_car_by_hand_has_no_teslamate_line_in_the_charging_pool(app):
    client, url, car_id = app
    budgets_on(url)
    add_lease(client, car_id)
    with connect(url) as conn:
        expense(conn, car_id, "Charging", "2026-01-05T00:00+10", 40)
    body = client.get(f"/cars/{car_id}/fbt?year=2025").data.decode()
    assert "$40.00" in pool_line(body, "fuel_charging") and "unavailable" not in pool_line(body, "fuel_charging")
    assert 'id="teslamate-charging"' not in body


def tessie(url, monkeypatch):
    """The stand-in TeslaMate (one charge of $12.50 on 1 February 2026) and its car, as the app lists it."""
    with connect(url) as conn:
        conn.execute(STAND_IN)
    client = start(url, monkeypatch)
    client.get("/cars")
    with connect(url) as conn:
        return client, conn.execute("SELECT id FROM garagebooks.car WHERE name = 'Tessie'").fetchone()["id"]


def test_teslamates_charging_is_added_to_the_fuel_or_charging_pool(empty_db, monkeypatch):
    client, car_id = tessie(empty_db, monkeypatch)
    budgets_on(empty_db)
    add_lease(client, car_id)
    with connect(empty_db) as conn:
        expense(conn, car_id, "Charging", "2026-01-05T00:00+10", 40)            # entered by hand, counted once
        expense(conn, car_id, "Insurance", "2026-02-10T00:00+10", 100)
    body = client.get(f"/cars/{car_id}/fbt?year=2025").data.decode()
    assert "$52.50" in pool_line(body, "fuel_charging")                         # 40 by hand + 12.50 from TeslaMate
    assert "$152.50" in total_line(body)
    assert 'id="teslamate-charging"' in body and "$12.50" in text_of(re.search(r'id="teslamate-charging".*?</tr>', body, re.S).group(0))


def test_when_teslamate_cannot_answer_the_charging_pool_and_the_totals_are_unavailable(empty_db, monkeypatch):
    client, car_id = tessie(empty_db, monkeypatch)
    budgets_on(empty_db)
    add_lease(client, car_id)
    with connect(empty_db) as conn:
        expense(conn, car_id, "Insurance", "2026-02-10T00:00+10", 100)
        conn.execute("ALTER TABLE public.positions RENAME COLUMN odometer TO odo")     # a table that changed
    client = start(empty_db, monkeypatch)
    page = client.get(f"/cars/{car_id}/fbt?year=2025")
    body = page.data.decode()
    assert "unavailable" in pool_line(body, "fuel_charging")
    assert "unavailable" in total_line(body) and "$100.00" not in total_line(body)    # not understated as $100
    assert "can't be read" in text(page)                                              # the reason
    assert "$100.00" in pool_line(body, "insurance")


# ---- the dashboard panel ----------------------------------------------------------------------

def shipped():
    return json.loads((ROOT / "grafana" / "cost-of-ownership.json").read_text())


def panel(dash):
    return next(p for p in dash["panels"] if p["title"] == "Running costs per FBT year")


def test_the_panel_sits_below_the_charts_and_above_the_expenses_table():
    dash = shipped()
    new, table = panel(dash), next(p for p in dash["panels"] if p["title"] == "Expenses")
    others = [p for p in dash["panels"] if p is not new and p is not table and p["title"] != "Reminders"]   # it sits between the two
    assert new["gridPos"]["x"] == 0 and new["gridPos"]["w"] == 24
    assert new["gridPos"]["y"] >= max(p["gridPos"]["y"] + p["gridPos"]["h"] for p in others)
    assert table["gridPos"]["y"] >= new["gridPos"]["y"] + new["gridPos"]["h"]
    assert len({p["id"] for p in dash["panels"]}) == len(dash["panels"])


def test_the_panel_follows_teslamate_through_the_marker_convention():
    sql = panel(shipped())["targets"][0]["rawSql"]
    assert "/*teslamate*/" in sql and "charging_processes" in sql and "garagebooks.fbt_year" in sql
    assert "$__timeFilter(e.occurred_at)" in sql and "'$tz'" in sql
    for form in (sources.TeslaMate(sources.ABSENT), sources.TeslaMate(sources.UNTESTED, "x")):
        assert "charging_processes" not in panel(dashboard.render("UTC", "", teslamate=form))["targets"][0]["rawSql"]


def run_panel(conn, tm):
    dash = dashboard.render("UTC", "", teslamate=None if tm else sources.TeslaMate(sources.ABSENT))
    sql = runnable(panel(dash)["targets"][0]["rawSql"]).replace("'UTC'", "'Australia/Brisbane'")
    return conn.execute(sql).fetchall()


def test_the_panel_groups_running_costs_by_fbt_year_and_category(admin):
    admin.execute("INSERT INTO garagebooks.expense (car_id, category_id, occurred_at, amount) VALUES "
                  "(1, %s, '2026-03-31T13:59Z', 10), (1, %s, '2026-03-31T14:00Z', 20), (1, %s, '2026-05-01T00:00Z', 5),"
                  " (1, %s, '2026-05-02T00:00Z', 7), (1, %s, '2026-05-03T00:00Z', 62000)",
                  (category(admin, "Insurance"), category(admin, "Insurance"), category(admin, "Insurance"),
                   category(admin, "Charging"), category(admin, "Initial purchase")))
    rows = {(r["FBT year"], r["Category"]): r["Cost"] for r in run_panel(admin, TESLAMATE)}
    assert rows == {("FBT 2025–26", "Insurance"): 10, ("FBT 2026–27", "Insurance"): 25, ("FBT 2026–27", "Charging"): 7}
    if TESLAMATE:
        admin.execute("INSERT INTO positions (latitude, longitude, date, odometer, car_id) VALUES (0, 0, '2026-01-01', 10, 1)")
        admin.execute("INSERT INTO charging_processes (start_date, position_id, cost, car_id) VALUES ('2026-03-31 13:59', 1, 3.25, 1)")
        rows = {(r["FBT year"], r["Category"]): r["Cost"] for r in run_panel(admin, True)}
        assert rows[("FBT 2025–26", "Charging")] == Decimal("3.25")


def test_every_panel_query_still_runs_with_the_new_one(empty_db, monkeypatch):
    start(empty_db, monkeypatch)
    dash = dashboard.render("UTC", "", teslamate=sources.TeslaMate(sources.ABSENT))
    with connect(empty_db) as conn:
        for sql in queries(dash):
            conn.execute(runnable(sql)).fetchall()


# ---- with lease budgets turned off (the default) --------------------------------------------------------

def test_with_budgets_off_the_report_lists_costs_by_category_and_has_no_budget_column(app):
    client, url, car_id = app
    add_lease(client, car_id)
    with connect(url) as conn:
        expense(conn, car_id, "Insurance", "2025-11-10T00:00+10", 1200)
        expense(conn, car_id, "Insurance", "2026-02-10T00:00+10", 30)
        expense(conn, car_id, "Tyres", "2025-12-01T00:00+10", 800)
        expense(conn, car_id, "Insurance", "2026-04-02T00:00+10", 999)          # the next FBT year
    page = client.get(f"/cars/{car_id}/fbt?year=2025")
    body, shown = page.data.decode(), text(page)
    assert 'id="fbt-categories"' in body and 'id="fbt-pools"' not in body and "data-pool=" not in body
    table = re.search(r'id="fbt-categories".*?</table>', body, re.S).group(0)
    rows = {text_of(r[0]).strip(): text_of(r[1]).strip() for r in re.findall(r"<tr><td>(.*?)</td><td[^>]*>(.*?)</td></tr>", table, re.S)}
    assert rows == {"Insurance": "$1,230.00", "Tyres": "$800.00"}
    assert "$2,030.00" in total_line(body) and "$999.00" not in shown
    assert "Budget" not in table and "budget" not in shown.lower() and "pool" not in shown.lower()
    assert "Running costs" in shown and "$62,000.00" not in total_line(body)


def test_with_budgets_off_a_year_without_costs_says_so_and_budgets_already_entered_stay_out_of_the_report(app):
    client, url, car_id = app
    budgets_on(url)
    add_lease(client, car_id, budget_tyres="730")
    budgets_on(url, False)
    body = client.get(f"/cars/{car_id}/fbt?year=2025").data.decode()
    assert "No running costs in this FBT year." in body and "$364.00" not in body and 'id="fbt-pools"' not in body
    budgets_on(url)
    assert "$364.00" in pool_line(client.get(f"/cars/{car_id}/fbt?year=2025").data.decode(), "tyres")      # back when turned on


def test_with_budgets_off_teslamates_charging_is_still_listed(empty_db, monkeypatch):
    client, car_id = tessie(empty_db, monkeypatch)
    add_lease(client, car_id)
    with connect(empty_db) as conn:
        expense(conn, car_id, "Charging", "2026-01-05T00:00+10", 40)
    body = client.get(f"/cars/{car_id}/fbt?year=2025").data.decode()
    assert 'id="fbt-categories"' in body and 'id="teslamate-charging"' in body
    assert "$12.50" in text_of(re.search(r'id="teslamate-charging".*?</tr>', body, re.S).group(0))
    assert "$40.00" in text_of(re.search(r'id="fbt-categories".*?</table>', body, re.S).group(0))
