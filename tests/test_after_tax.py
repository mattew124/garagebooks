"""Cost after tax (roadmap L5): what a packaged expense costs once the GST is claimed back and the rest comes
from pre-tax salary, the home charging reimbursement, and where they show (Settings, the expense form and
list, Categories, the lease form, the FBT year report and the dashboard). All figures are invented.
Runs with TeslaMate absent, present and held back."""

import json
import re
from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import psycopg
from hypothesis import given, settings, strategies as st
from psycopg.rows import dict_row

from garagebooks import dashboard, db, lease, sources, validation as v
from tests.conftest import TESLAMATE
from tests.test_fbt import add_lease, expense, text, text_of, tessie
from tests.test_lease import ORIGIN, app, budgets_on, connect, good_form  # noqa: F401 - app is a fixture
from tests.test_sources import category, queries, runnable, start

D = Decimal
BRISBANE = ZoneInfo("Australia/Brisbane")
RATES = {2022: D("4.20"), 2023: D("4.20"), 2024: D("4.20"), 2025: D("4.20"), 2026: D("5.47")}


# ---- the formula, as pure functions -----------------------------------------------------------------
# Worked example: a $1,100.00 invoice (GST included), a lease paying 300.00 pre-tax and 20.00 post-tax a pay,
# and a marginal rate of 34%. GST is 1/11 of the invoice, so 1,000.00 is left. The post-tax share of the lease
# is 20 / 320 = 0.0625, so 1,000.00 x (0.9375 x 0.66 + 0.0625) = 681.25.

def test_the_worked_example():
    assert lease.effective_cost(D("1100.00"), True, False, D("300.00"), D("20.00"), D("34")) == D("681.25")


def test_a_gst_free_invoice_keeps_all_of_its_amount_before_tax_and_rounds_half_up():
    assert lease.effective_cost(D("1100.00"), True, True, D("300.00"), D("20.00"), D("34")) == D("749.38")   # 749.375


def test_an_expense_paid_personally_costs_its_invoice_and_needs_no_rate():
    assert lease.effective_cost(D("1100.00"), False, False, D("300.00"), D("20.00"), D("34")) == D("1100.00")
    assert lease.effective_cost(D("1100.00"), False, False, D("300.00"), D("20.00"), None) == D("1100.00")


def test_without_a_rate_a_packaged_expense_has_no_after_tax_cost_never_zero():
    assert lease.effective_cost(D("1100.00"), True, False, D("300.00"), D("20.00"), None) is None


def test_a_rebate_follows_the_same_formula():
    assert lease.effective_cost(D("-1100.00"), True, False, D("300.00"), D("20.00"), D("34")) == D("-681.25")


def test_packaged_with_no_lease_or_no_payments_has_no_post_tax_share():
    assert lease.effective_cost(D("1100.00"), True, False, None, None, D("34")) == D("660.00")        # 1,000 x 0.66
    assert lease.effective_cost(D("1100.00"), True, False, D("0.00"), D("0.00"), D("34")) == D("660.00")


def test_the_extremes_of_the_rate():
    assert lease.effective_cost(D("1100.00"), True, False, D("300.00"), D("20.00"), D("0")) == D("1000.00")
    assert lease.effective_cost(D("1100.00"), True, False, D("300.00"), D("20.00"), D("100")) == D("62.50")   # only the post-tax share


def test_the_automatic_rule_and_the_owners_choice():
    # automatic: a lease covers the date, the category has a pool, and it is not a purchase
    assert lease.is_packaged(None, True, "insurance", "running") is True
    assert lease.is_packaged(None, False, "insurance", "running") is False
    assert lease.is_packaged(None, True, None, "running") is False
    assert lease.is_packaged(None, True, "insurance", "purchase") is False
    assert lease.is_packaged(None, True, "fuel_charging", "energy") is True
    # the choice wins, whatever the rule says
    assert lease.is_packaged(True, False, None, "purchase") is True
    assert lease.is_packaged(False, True, "insurance", "running") is False


def test_the_ev_rate_of_a_year_without_one_is_the_latest_earlier_years():
    assert lease.ev_rate(RATES, 2025) == (2025, D("4.20"))
    assert lease.ev_rate(RATES, 2026) == (2026, D("5.47"))
    assert lease.ev_rate(RATES, 2030) == (2026, D("5.47"))           # the latest year with a row at or before it
    assert lease.ev_rate({2022: D("4.20"), 2026: D("5.47")}, 2024) == (2022, D("4.20"))
    assert lease.ev_rate(RATES, 2021) is None                        # before the first row: no rate


def test_the_charging_reimbursement_and_the_tax_saved_on_it():
    got = lease.charging_reimbursement(D("2000.0"), D("4.20"), D("34"))
    assert got == {"km": D("2000.0"), "cents_per_km": D("4.20"), "amount": D("84.00"), "tax_saved": D("28.56")}
    assert lease.charging_reimbursement(D("1234.5"), D("5.47"), D("34"))["amount"] == D("67.53")      # 67.52715
    assert lease.charging_reimbursement(None, D("4.20"), D("34")) is None                              # not known: never 0
    assert lease.charging_reimbursement(D("2000.0"), None, D("34")) is None
    assert lease.charging_reimbursement(D("-5.0"), D("4.20"), D("34")) is None                         # readings the wrong way round
    assert lease.charging_reimbursement(D("0.0"), D("4.20"), D("34"))["amount"] == D("0.00")           # known to be 0


def test_the_days_the_reimbursement_covers_are_the_held_days_up_to_today():
    row = {"start_date": date(2025, 10, 1), "end_date": date(2028, 9, 30), "charging_reimbursed": True}
    off = {**row, "charging_reimbursed": False}
    assert lease.reimbursed_spans([row], 2025, date(2026, 1, 10)) == [(date(2025, 10, 1), date(2026, 1, 10))]
    assert lease.reimbursed_spans([row], 2025, date(2026, 6, 1)) == [(date(2025, 10, 1), date(2026, 3, 31))]
    assert lease.reimbursed_spans([row], 2025, date(2025, 9, 1)) == []           # not yet held
    assert lease.reimbursed_spans([off], 2025, date(2026, 6, 1)) == []           # not reimbursed
    assert lease.reimbursed_spans([row], 2024, date(2026, 6, 1)) == []           # not held in that year


# ---- validation -----------------------------------------------------------------------------------

def test_the_rate_across_days_is_each_income_years_rate_weighted_by_days():
    rates = {2025: D("32"), 2026: D("30")}
    spans = [(date(2026, 4, 1), date(2026, 6, 30)), (date(2026, 7, 1), date(2026, 7, 31))]   # 91 days, then 31
    assert lease.rate_over_days(spans, rates.get).quantize(D("1e-20")) == ((D("32") * 91 + D("30") * 31) / 122).quantize(D("1e-20"))
    assert lease.rate_over_days([(date(2025, 8, 1), date(2025, 8, 31))], rates.get) == D("32")
    assert lease.rate_over_days([(date(2024, 6, 30), date(2024, 7, 1))], rates.get) is None   # 2023-24 has no rate
    assert lease.rate_over_days([], rates.get) is None


def test_paid_from_is_automatic_the_package_or_personally():
    assert v.parse_paid_from("") is None and v.parse_paid_from(None) is None and v.parse_paid_from("auto") is None
    assert v.parse_paid_from("package") is True and v.parse_paid_from("personal") is False
    try:
        v.parse_paid_from("tax")
    except ValueError:
        pass
    else:
        raise AssertionError


# ---- the schema ------------------------------------------------------------------------------------

def columns(conn, table):
    return {r["column_name"]: r for r in conn.execute(
        "SELECT column_name, is_nullable, column_default FROM information_schema.columns "
        "WHERE table_schema = 'garagebooks' AND table_name = %s", (table,))}


def test_the_new_columns_and_the_rate_table_are_there_with_their_defaults(empty_db, monkeypatch):
    start(empty_db, monkeypatch)
    with connect(empty_db) as conn:
        assert "marginal_tax_rate" in columns(conn, "setting")
        assert conn.execute("SELECT marginal_tax_rate FROM garagebooks.setting").fetchone()["marginal_tax_rate"] is None
        assert columns(conn, "expense")["packaged"]["is_nullable"] == "YES"
        assert columns(conn, "lease")["charging_reimbursed"]["is_nullable"] == "NO"
        assert columns(conn, "category")["gst_free"]["is_nullable"] == "NO"
        free = {r["name"] for r in conn.execute("SELECT name FROM garagebooks.category WHERE gst_free")}
        assert free == {"Registration"}                                    # the standard registration and CTP category only
        rates = {r["fbt_year"]: r["cents_per_km"] for r in conn.execute("SELECT * FROM garagebooks.ev_charging_rate")}
        assert rates == RATES


def test_a_rate_outside_0_to_100_and_a_negative_cents_per_km_are_refused_by_the_database(empty_db, monkeypatch):
    start(empty_db, monkeypatch)
    with connect(empty_db) as conn:
        for sql in ("UPDATE garagebooks.setting SET marginal_tax_rate = 100.01",
                    "UPDATE garagebooks.setting SET marginal_tax_rate = -1",
                    "INSERT INTO garagebooks.ev_charging_rate VALUES (2030, -0.01)"):
            try:
                conn.execute(sql)
            except psycopg.errors.CheckViolation:
                continue
            raise AssertionError(sql)


def test_running_the_schema_again_is_safe_and_keeps_the_owners_choices(empty_db, monkeypatch):
    start(empty_db, monkeypatch)
    with connect(empty_db) as conn:
        db.set_tax_choice(conn, "employee", 2025, 45001, "half_exempt")
        conn.execute("UPDATE garagebooks.category SET gst_free = NOT gst_free WHERE name IN ('Registration', 'Tyres')")
        conn.execute("UPDATE garagebooks.ev_charging_rate SET cents_per_km = 9.99 WHERE fbt_year = 2026")
        conn.execute("INSERT INTO garagebooks.ev_charging_rate VALUES (2027, 6.00)")
    start(empty_db, monkeypatch)
    start(empty_db, monkeypatch)
    with connect(empty_db) as conn:
        assert conn.execute("SELECT garagebooks.marginal_rate('employee', '2025-11-01') AS r").fetchone()["r"] == D("31")
        free = {r["name"] for r in conn.execute("SELECT name FROM garagebooks.category WHERE gst_free")}
        assert free == {"Tyres"}                                          # not put back to the defaults
        rates = {r["fbt_year"]: r["cents_per_km"] for r in conn.execute("SELECT * FROM garagebooks.ev_charging_rate")}
        assert rates[2026] == D("9.99") and rates[2027] == D("6.00")      # an edited or added year is never overwritten


def test_a_database_from_before_this_release_gains_the_columns_and_keeps_its_expenses(empty_db, monkeypatch):
    start(empty_db, monkeypatch)
    with connect(empty_db) as conn:
        conn.execute("INSERT INTO garagebooks.car (id, name) VALUES (7, 'Old')")
        conn.execute("INSERT INTO garagebooks.expense (car_id, category_id, occurred_at, amount) "
                     "VALUES (7, (SELECT id FROM garagebooks.category WHERE name = 'Tyres'), now(), 12.34)")
        conn.execute("DROP FUNCTION garagebooks.expense_after_tax(text)")
        conn.execute("DROP TABLE garagebooks.ev_charging_rate")
        conn.execute("ALTER TABLE garagebooks.setting DROP COLUMN marginal_tax_rate")
        conn.execute("ALTER TABLE garagebooks.expense DROP COLUMN packaged")
        conn.execute("ALTER TABLE garagebooks.category DROP COLUMN gst_free")
        conn.execute("ALTER TABLE garagebooks.lease DROP COLUMN charging_reimbursed")
    start(empty_db, monkeypatch)
    with connect(empty_db) as conn:
        assert conn.execute("SELECT packaged, amount FROM garagebooks.expense").fetchone() == {"packaged": None, "amount": D("12.34")}
        free = {r["name"] for r in conn.execute("SELECT name FROM garagebooks.category WHERE gst_free")}
        assert free == {"Registration"}
        assert conn.execute("SELECT count(*) AS n FROM garagebooks.ev_charging_rate").fetchone()["n"] == 5


def test_the_restricted_role_can_do_what_the_app_does(pg, admin):
    category_id = category(admin, "Tyres")
    with psycopg.connect(pg.app_url, row_factory=dict_row) as conn:      # one transaction, rolled back at the end
        conn.execute("INSERT INTO garagebooks.tax_choice VALUES ('employee', 2020, 45001, 'not_exempt')")
        conn.execute("UPDATE garagebooks.category SET gst_free = true WHERE id = %s", (category_id,))
        conn.execute("INSERT INTO garagebooks.ev_charging_rate VALUES (2031, 7.00) "
                     "ON CONFLICT (fbt_year) DO UPDATE SET cents_per_km = excluded.cents_per_km")
        assert conn.execute("SELECT count(*) AS n FROM garagebooks.ev_charging_rate").fetchone()["n"] >= 6
        conn.execute("INSERT INTO garagebooks.expense (car_id, category_id, occurred_at, amount, packaged) "
                     "VALUES (1, %s, now(), 10, true)", (category_id,))
        assert conn.execute("SELECT effective_cost FROM garagebooks.expense_after_tax('UTC')").fetchone()["effective_cost"] is not None
        conn.execute("UPDATE garagebooks.lease SET charging_reimbursed = false")
        conn.rollback()
    assert admin.execute("SELECT count(*) AS n FROM garagebooks.tax_choice").fetchone()["n"] == 0
    assert pg.provision().returncode == 0                                # the restricted-role path still provisions


# ---- the SQL function and the Python function agree ---------------------------------------------------

POOLS = ["Insurance", "Initial purchase", "Misc"]           # a pool and running; a purchase; running with no pool
# 2025-26's and 2026-27's brackets by their first dollar (the same edges in both years), and the Medicare levies.
BRACKET_RATES = {2025: {0: 0, 18201: 16, 45001: 30, 135001: 37, 190001: 45},
                 2026: {0: 0, 18201: 15, 45001: 30, 135001: 37, 190001: 45}}
LEVIES = {"not_exempt": 2, "half_exempt": 1, "exempt": 0}


def expected_rate(choices, day):
    """The marginal rate the brackets chosen give on a date: its income year's choice, else the latest earlier one's."""
    year = day.year if day.month >= 7 else day.year - 1
    earlier = [y for y in choices if y <= year]
    if not earlier:
        return None
    lower, medicare = choices[max(earlier)]
    return D(BRACKET_RATES[year][lower] + LEVIES[medicare])


@settings(max_examples=40)
@given(
    choices=st.dictionaries(st.sampled_from([2025, 2026]),
                            st.tuples(st.sampled_from(sorted(BRACKET_RATES[2025])), st.sampled_from(sorted(LEVIES)))),
    pretax=st.decimals(0, 2000, places=2), posttax=st.decimals(0, 2000, places=2),
    lease_days=st.tuples(st.integers(0, 200), st.integers(1, 200)),
    gst_free=st.booleans(),
    items=st.lists(st.tuples(st.integers(-5, 300), st.integers(0, 23), st.sampled_from(POOLS),
                             st.decimals(-5000, 5000, places=2), st.sampled_from([None, True, False]),
                             st.one_of(st.none(), st.decimals(0, 1, places=2))),      # a GST read from the document, as a share of it
                   min_size=1, max_size=6))
def test_the_sql_function_and_the_python_one_agree(pg, choices, pretax, posttax, lease_days, gst_free, items):
    first = date(2026, 1, 10)                     # the expenses run from 2025-26 into 2026-27
    start_day, end_day = first + timedelta(days=lease_days[0]), first + timedelta(days=lease_days[0] + lease_days[1])
    with pg.admin() as conn:
        with conn.transaction():                                          # nothing here outlives the example
            conn.execute("DELETE FROM garagebooks.tax_choice")
            for year, (lower, medicare) in choices.items():
                db.set_tax_choice(conn, "employee", year, lower, medicare)
            car = conn.execute("INSERT INTO garagebooks.car (name) VALUES ('Prop') RETURNING id").fetchone()["id"]
            conn.execute("INSERT INTO garagebooks.category (name, kind, sort_order, budget_pool) VALUES ('Misc', 'running', 100, NULL) "
                         "ON CONFLICT (name) DO NOTHING")
            conn.execute("UPDATE garagebooks.category SET gst_free = %s WHERE name IN ('Insurance', 'Initial purchase', 'Misc')", (gst_free,))
            if pretax or posttax or lease_days[1] % 2:                      # sometimes no lease at all
                conn.execute("INSERT INTO garagebooks.lease (car_id, financier, start_date, end_date, term_months, pay_frequency,"
                             " pretax_payment, posttax_payment) VALUES (%s, 'Example Finance', %s, %s, 12, 'fortnightly', %s, %s)",
                             (car, start_day, end_day, pretax, posttax))
                has_lease = True
            else:
                has_lease = False
            ids = {}
            for day, hour, name, amount, choice, share in items:
                when = datetime.combine(first + timedelta(days=day), datetime.min.time(), BRISBANE) + timedelta(hours=hour)
                gst = None if share is None else (amount * share).quantize(D("0.01"))
                ids[conn.execute("INSERT INTO garagebooks.expense (car_id, category_id, occurred_at, amount, packaged, gst_amount) "
                                 "VALUES (%s, (SELECT id FROM garagebooks.category WHERE name = %s), %s, %s, %s, %s) RETURNING id",
                                 (car, name, when, amount, choice, gst)).fetchone()["id"]] = (when, name, amount, choice, gst)
            got = {r["expense_id"]: r for r in conn.execute(
                "SELECT * FROM garagebooks.expense_after_tax('Australia/Brisbane') WHERE car_id = %s", (car,))}
            assert set(got) == set(ids)
            for expense_id, (when, name, amount, choice, gst) in ids.items():
                day = when.astimezone(BRISBANE).date()
                covered = has_lease and start_day <= day <= end_day
                kind = "purchase" if name == "Initial purchase" else "running"
                packaged = lease.is_packaged(choice, covered, None if name == "Misc" else "insurance", kind)
                rate = expected_rate(choices, day)
                want = lease.effective_cost(amount, packaged, gst_free, pretax if covered else None,
                                            posttax if covered else None, rate, gst_amount=gst)
                assert got[expense_id]["packaged"] == packaged
                assert got[expense_id]["effective_cost"] == want, (amount, packaged, gst_free, pretax, posttax, rate)
            raise psycopg.Rollback()


# ---- Settings -----------------------------------------------------------------------------------------

def post_settings(client, **over):
    return client.post("/settings", data={"app_url": "", "grafana_url": "", **over}, headers=ORIGIN)


def set_rate(client, on=True, **over):
    """Choose $45,001 - $135,000 (30%), not exempt from the Medicare levy, for 2025-26: a marginal rate of 32% from
    1 July 2025 on (later years follow it). on=False clears it, so no year has a rate."""
    chosen = {"tax_employee_2025_bracket": "45001" if on else "", "tax_employee_2025_medicare": "not_exempt"}
    assert post_settings(client, **chosen, **over).status_code == 303


# ---- the expense form -----------------------------------------------------------------------------------

def new_expense(client, car_id, category_id, **over):
    data = {"car_id": str(car_id), "category_id": str(category_id), "date": "2025-11-10", "time": "09:00",
            "amount": "1100.00", "odometer_km": "", "vendor": "Example Insurer", "invoice_url": "", "notes": ""}
    data.update(over)
    return client.post("/new", data=data, headers=ORIGIN)


def stored_expense(url):
    with connect(url) as conn:
        return conn.execute("SELECT * FROM garagebooks.expense ORDER BY id DESC LIMIT 1").fetchone()


def test_paid_from_is_offered_only_for_a_car_with_a_lease(app):
    client, url, car_id = app
    assert 'name="packaged"' not in client.get("/new").data.decode()
    add_lease(client, car_id)
    page = client.get("/new").data.decode()
    assert 'name="packaged"' in page and "The package" in page and "Personally" in page and "Automatic" in page


def test_paid_from_is_saved_and_edited(app):
    client, url, car_id = app
    add_lease(client, car_id)
    with connect(url) as conn:
        tyres = category(conn, "Insurance")
    assert new_expense(client, car_id, tyres).status_code == 303
    assert stored_expense(url)["packaged"] is None                          # automatic by default
    assert new_expense(client, car_id, tyres, packaged="package", amount="1101.00").status_code == 303
    assert stored_expense(url)["packaged"] is True
    assert new_expense(client, car_id, tyres, packaged="personal", amount="1102.00").status_code == 303
    row = stored_expense(url)
    assert row["packaged"] is False
    assert re.search(r'value="personal"\s+selected', client.get(f"/{row['id']}/edit").data.decode())
    assert client.post(f"/{row['id']}/edit", data={"car_id": str(car_id), "category_id": str(tyres), "date": "2025-11-10",
                                                  "time": "09:00", "amount": "1100.00", "packaged": "auto"},
                       headers=ORIGIN).status_code == 303
    assert stored_expense(url)["packaged"] is None
    refused = new_expense(client, car_id, tyres, packaged="tax", amount="1103.00")
    assert refused.status_code == 400 and "paid from" in text(refused).lower()


def test_the_history_names_the_new_fields(app):
    client, url, car_id = app
    add_lease(client, car_id)
    with connect(url) as conn:
        insurance = category(conn, "Insurance")
    assert new_expense(client, car_id, insurance, packaged="package").status_code == 303
    row = stored_expense(url)
    assert client.post(f"/{row['id']}/edit", data={"car_id": str(car_id), "category_id": str(insurance), "date": "2025-11-10",
                                                  "time": "09:00", "amount": "1100.00", "packaged": "personal"},
                       headers=ORIGIN).status_code == 303
    shown = text(client.get("/history"))
    assert "Paid from" in shown and "The package" in shown and "Personally" in shown
    client.post("/categories/save", data={"category_id": str(insurance), f"gst_partial_{insurance}": "1"}, headers=ORIGIN)
    assert "GST only partly applies" in text(client.get("/history"))


# ---- the list ------------------------------------------------------------------------------------------

def test_a_packaged_expense_shows_what_it_costs_after_tax_and_the_totals_do_too(app):
    client, url, car_id = app
    add_lease(client, car_id)
    set_rate(client)
    with connect(url) as conn:
        expense(conn, car_id, "Insurance", "2025-11-10T09:00+10", 1100)
        expense(conn, car_id, "Tyres", "2025-11-11T09:00+10", 200)
    with connect(url) as conn:                                              # the tyres were paid personally
        conn.execute("UPDATE garagebooks.expense SET packaged = false WHERE amount = 200")
    page = client.get("/")
    body, shown = page.data.decode(), text(page)
    assert "$700.00" in shown and "after tax" in shown.lower()
    assert body.count('class="after-tax"') == 1                              # only the packaged one
    assert "$1,300.00" in shown and "$900.00" in text_of(re.search(r'id="after-tax-total".*?</span>', body, re.S).group(0))


def test_without_a_rate_the_list_says_so_and_shows_no_after_tax_figure(app):
    client, url, car_id = app
    add_lease(client, car_id)
    with connect(url) as conn:
        expense(conn, car_id, "Insurance", "2025-11-10T09:00+10", 1100)
    body = client.get("/").data.decode()
    assert 'class="after-tax"' not in body and 'id="after-tax-total"' not in body and "$700.00" not in body
    assert 'id="no-tax-rate"' in body and "/settings" in body


def test_a_car_with_no_lease_has_nothing_extra_in_the_list(app):
    client, url, car_id = app
    set_rate(client)
    with connect(url) as conn:
        expense(conn, car_id, "Insurance", "2025-11-10T09:00+10", 1100)
    body = client.get("/").data.decode()
    assert 'class="after-tax"' not in body and 'id="after-tax-total"' not in body and 'id="no-tax-rate"' not in body


def test_an_expense_outside_the_lease_dates_is_personal_and_a_purchase_is_never_automatic(app):
    client, url, car_id = app
    add_lease(client, car_id, start_date="2025-10-01", end_date="2026-09-30")
    set_rate(client)
    with connect(url) as conn:
        expense(conn, car_id, "Insurance", "2025-09-30T23:00+10", 1100)       # the day before the lease
        expense(conn, car_id, "Initial purchase", "2025-11-10T09:00+10", 60000)
    assert 'class="after-tax"' not in client.get("/").data.decode()
    with connect(url) as conn:
        conn.execute("UPDATE garagebooks.expense SET packaged = true WHERE amount = 1100")
    assert 'class="after-tax"' in client.get("/").data.decode()               # the choice wins


# ---- the categories ---------------------------------------------------------------------------------------

def test_a_category_can_be_marked_as_only_partly_gst_and_the_cost_follows(app):
    client, url, car_id = app
    add_lease(client, car_id)
    set_rate(client)
    with connect(url) as conn:
        expense(conn, car_id, "Insurance", "2025-11-10T09:00+10", 1100)
        insurance = category(conn, "Insurance")
    page = client.get("/categories").data.decode()
    assert f'name="gst_partial_{insurance}"' in page and "GST only partly applies" in page
    assert "$700.00" in text(client.get("/"))
    assert client.post("/categories/save", data={"category_id": str(insurance), f"gst_partial_{insurance}": "1"},
                       headers=ORIGIN).status_code == 303
    assert "$770.00" in text(client.get("/"))           # no GST read or entered: none is counted
    client.post("/categories/save", data={"category_id": str(insurance)}, headers=ORIGIN)   # an unticked box sends nothing
    assert "$700.00" in text(client.get("/"))


# ---- the lease form ----------------------------------------------------------------------------------------

def test_charging_reimbursement_is_offered_for_an_electric_car_only(app):
    client, url, car_id = app
    page = client.get(f"/cars/{car_id}/lease/new").data.decode()
    assert 'name="charging_reimbursed"' in page and "cents-per-km" in page
    add_lease(client, car_id, charging_reimbursed="on")
    with connect(url) as conn:
        assert conn.execute("SELECT charging_reimbursed FROM garagebooks.lease").fetchone()["charging_reimbursed"] is True
        row = conn.execute("SELECT id FROM garagebooks.lease").fetchone()
    assert re.search(r'name="charging_reimbursed"[^>]*checked', client.get(f"/cars/{car_id}/lease/{row['id']}/edit").data.decode())
    client.post("/cars", data={"name": "Ute", "kind": "ICE"}, headers=ORIGIN)
    client.post("/cars", data={"name": "Hybrid", "kind": "PHEV"}, headers=ORIGIN)
    with connect(url) as conn:
        ute = conn.execute("SELECT id FROM garagebooks.car WHERE name = 'Ute'").fetchone()["id"]
        hybrid = conn.execute("SELECT id FROM garagebooks.car WHERE name = 'Hybrid'").fetchone()["id"]
    for other in (ute, hybrid):
        assert 'name="charging_reimbursed"' not in client.get(f"/cars/{other}/lease/new").data.decode()
        add_lease(client, other, charging_reimbursed="on")                     # a hand-made request is ignored
    with connect(url) as conn:
        assert [r["charging_reimbursed"] for r in conn.execute("SELECT charging_reimbursed FROM garagebooks.lease WHERE car_id IN (%s, %s)", (ute, hybrid))] == [False, False]


# ---- the FBT year report -------------------------------------------------------------------------------------

def fill_year(url, car_id):
    """Insurance 1,100 and Registration 1,000 (GST-free) packaged, and 200 of insurance paid personally."""
    with connect(url) as conn:
        expense(conn, car_id, "Insurance", "2025-11-10T09:00+10", 1100)
        expense(conn, car_id, "Registration", "2025-12-01T09:00+10", 1000)
        expense(conn, car_id, "Insurance", "2026-02-01T09:00+10", 200)
        conn.execute("UPDATE garagebooks.expense SET packaged = false WHERE amount = 200")


def readings(url, car_id, start_km=10000, end_km=12000):
    with connect(url) as conn:
        conn.execute("INSERT INTO garagebooks.odometer_reading (car_id, read_at, km) VALUES (%s, '2025-09-30T12:00+10', %s),"
                     " (%s, '2026-03-31T12:00+10', %s)", (car_id, start_km, car_id, end_km))


def section(body):
    return re.search(r'id="fbt-after-tax".*?</section>', body, re.S).group(0)


def test_the_report_shows_invoice_and_effective_cost_per_line_and_the_total(app):
    client, url, car_id = app
    add_lease(client, car_id)
    set_rate(client)
    fill_year(url, car_id)
    shown = text_of(section(client.get(f"/cars/{car_id}/fbt?year=2025").data.decode()))
    assert "$2,300.00" in shown and "$1,600.00" in shown                      # 1,100 + 1,000 + 200 invoiced; 700.00 x 2 + 200 (a 32% rate)
    assert "2 packaged" in shown and "dashboard" in shown.lower()           # the breakdown by line is on the dashboard
    assert "not tax advice" in text(client.get(f"/cars/{car_id}/fbt?year=2025")).lower()


def test_the_report_adds_the_home_charging_reimbursement_and_the_tax_saved(app):
    client, url, car_id = app
    add_lease(client, car_id, charging_reimbursed="on")
    set_rate(client)
    fill_year(url, car_id)
    readings(url, car_id)
    box = text_of(re.search(r'id="fbt-reimbursement".*?</div>', section(client.get(f"/cars/{car_id}/fbt?year=2025").data.decode()), re.S).group(0))
    assert "2,000 km" in box and "4.20" in box and "$84.00" in box and "$26.88" in box
    total = text_of(re.search(r'id="fbt-after-tax-total".*?</tr>', client.get(f"/cars/{car_id}/fbt?year=2025").data.decode(), re.S).group(0))
    assert "$1,573.12" in total                                              # 1,600.00 less 26.88 (84.00 x 32%)


def test_an_unknown_odometer_makes_the_reimbursement_unknown_not_zero(app):
    client, url, car_id = app
    add_lease(client, car_id, charging_reimbursed="on")
    set_rate(client)
    fill_year(url, car_id)
    body = client.get(f"/cars/{car_id}/fbt?year=2025").data.decode()
    box = text_of(re.search(r'id="fbt-reimbursement".*?</div>', section(body), re.S).group(0))
    assert "not known" in box.lower() and "$0.00" not in box
    total = text_of(re.search(r'id="fbt-after-tax-total".*?</tr>', body, re.S).group(0))
    assert "unavailable" in total.lower() and "$1,600.00" not in total.replace("before", "")  # no figure stands in for it


def test_the_rate_of_an_earlier_year_is_used_and_named_when_the_year_has_none(app):
    client, url, car_id = app
    add_lease(client, car_id, charging_reimbursed="on")
    set_rate(client)
    readings(url, car_id)
    with connect(url) as conn:
        conn.execute("DELETE FROM garagebooks.ev_charging_rate WHERE fbt_year = 2025")
    box = text_of(re.search(r'id="fbt-reimbursement".*?</div>', section(client.get(f"/cars/{car_id}/fbt?year=2025").data.decode()), re.S).group(0))
    assert "$84.00" in box and "2024" in box                                  # the 2024 rate, and it says so
    with connect(url) as conn:
        conn.execute("DELETE FROM garagebooks.ev_charging_rate")
    box = text_of(re.search(r'id="fbt-reimbursement".*?</div>', section(client.get(f"/cars/{car_id}/fbt?year=2025").data.decode()), re.S).group(0))
    assert "no rate" in box.lower() and "$0.00" not in box


def test_without_a_rate_the_report_says_so_and_shows_no_after_tax_figure(app):
    client, url, car_id = app
    add_lease(client, car_id, charging_reimbursed="on")
    fill_year(url, car_id)
    readings(url, car_id)
    body = client.get(f"/cars/{car_id}/fbt?year=2025").data.decode()
    box = section(body)
    assert 'id="fbt-after-tax-norate"' in box and "/settings" in box
    assert "$1,600.00" not in box and "$84.00" not in box and "$26.88" not in box


def test_the_after_tax_part_follows_the_pools_when_budgets_are_on(app):
    client, url, car_id = app
    add_lease(client, car_id)
    set_rate(client, lease_budgets="1")             # saving Settings sets the budgets switch too
    fill_year(url, car_id)
    shown = text_of(section(client.get(f"/cars/{car_id}/fbt?year=2025").data.decode()))
    assert "breakdown by pool" in shown and "$1,600.00" in shown


def test_a_car_that_is_not_reimbursed_has_no_reimbursement_box(app):
    client, url, car_id = app
    add_lease(client, car_id)
    set_rate(client)
    fill_year(url, car_id)
    assert 'id="fbt-reimbursement"' not in client.get(f"/cars/{car_id}/fbt?year=2025").data.decode()


def test_teslamates_charging_is_counted_as_paid_personally_and_its_odometer_is_used(empty_db, monkeypatch):
    client, car_id = tessie(empty_db, monkeypatch)
    add_lease(client, car_id, start_date="2026-01-02", end_date="2028-12-31", charging_reimbursed="on")
    set_rate(client)
    body = client.get(f"/cars/{car_id}/fbt?year=2025").data.decode()
    box = section(body)
    assert "$12.50" in text_of(box)                                           # the charge: paid personally, so its invoice and effective cost
    assert "800 km" in text_of(re.search(r'id="fbt-reimbursement".*?</div>', box, re.S).group(0))      # 900 less 100, from TeslaMate
    assert "$33.60" in text_of(box) and "$10.75" in text_of(box)               # 800 x 4.20 c ; x 32%


def test_when_teslamate_is_held_back_its_charging_and_odometer_are_unavailable(empty_db, monkeypatch):
    client, car_id = tessie(empty_db, monkeypatch)
    add_lease(client, car_id, start_date="2026-01-02", end_date="2028-12-31", charging_reimbursed="on")
    set_rate(client)
    with connect(empty_db) as conn:
        conn.execute("ALTER TABLE public.positions RENAME COLUMN odometer TO odo")
    client = start(empty_db, monkeypatch)
    body = client.get(f"/cars/{car_id}/fbt?year=2025").data.decode()
    box = text_of(section(body))
    assert "unavailable" in box.lower() and "not known" in box.lower()
    assert "$33.60" not in box and "$0.00" not in text_of(re.search(r'id="fbt-reimbursement".*?</div>', section(body), re.S).group(0))


def test_with_no_teslamate_at_all_the_after_tax_part_still_draws(empty_db, monkeypatch):
    client = start(empty_db, monkeypatch)
    client.post("/cars", data={"name": "Own", "kind": "BEV"}, headers=ORIGIN)
    with connect(empty_db) as conn:
        car_id = conn.execute("SELECT id FROM garagebooks.car").fetchone()["id"]
    add_lease(client, car_id, charging_reimbursed="on")
    set_rate(client)
    fill_year(empty_db, car_id)
    assert "$1,600.00" in text_of(section(client.get(f"/cars/{car_id}/fbt?year=2025").data.decode()))


# ---- the dashboard -------------------------------------------------------------------------------------------

AFTER_TAX_PANELS = ("Running costs after tax", "After tax by FBT year")
MINE = {23, 24, 25, 26}


def panel_titles(dash):
    return [p["title"] for p in dash["panels"]]


def test_the_two_panels_use_only_the_ids_reserved_for_them_and_sit_in_a_new_row_below_the_rest():
    shipped = json.loads((dashboard.SOURCE).read_text())
    mine = [p for p in shipped["panels"] if p["title"] in AFTER_TAX_PANELS]
    assert sorted(p["title"] for p in mine) == sorted(AFTER_TAX_PANELS) and {p["id"] for p in mine} <= MINE
    others = [p for p in shipped["panels"] if p["title"] not in dashboard.AFTER_TAX_PANELS]     # the after-tax rows come last
    assert all(p["gridPos"]["y"] >= max(o["gridPos"]["y"] + o["gridPos"]["h"] for o in others) for p in mine)
    assert len({p["id"] for p in shipped["panels"]}) == len(shipped["panels"])
    assert "odometer" in " ".join(p["description"] for p in mine).lower()      # says the reimbursement is in the app only


def test_the_panels_are_left_out_unless_asked_for():
    assert all(t in panel_titles(dashboard.render("UTC", "")) for t in AFTER_TAX_PANELS)
    plain = dashboard.render("UTC", "", after_tax=False)
    assert not set(AFTER_TAX_PANELS) & set(panel_titles(plain))
    shipped = json.loads(dashboard.SOURCE.read_text())
    assert [p for p in plain["panels"]] == [p for p in shipped["panels"] if p["title"] not in dashboard.AFTER_TAX_PANELS]


def test_the_app_writes_the_panels_only_with_a_rate_and_a_lease_and_rewrites_when_either_changes(empty_db, monkeypatch, tmp_path):
    client = start(empty_db, monkeypatch, DASHBOARD_DIR=str(tmp_path))
    target = tmp_path / dashboard.FILENAME
    def written():
        return set(AFTER_TAX_PANELS) & set(panel_titles(json.loads(target.read_text())))
    assert not written()                                                     # nothing yet
    client.post("/cars", data={"name": "Own", "kind": "BEV"}, headers=ORIGIN)
    with connect(empty_db) as conn:
        car_id = conn.execute("SELECT id FROM garagebooks.car").fetchone()["id"]
    set_rate(client)
    assert not written()                                                     # a rate, but no lease
    add_lease(client, car_id)
    assert written() == set(AFTER_TAX_PANELS)                                 # a lease now: written at once
    set_rate(client, False)
    assert not written()                                                     # the rate cleared: dropped
    set_rate(client)
    assert written() == set(AFTER_TAX_PANELS)                                 # and back when saved
    assert client.post(f"/cars/{car_id}/lease/{lease_id(empty_db)}/delete", headers=ORIGIN).status_code == 303
    assert not written()
    start(empty_db, monkeypatch, DASHBOARD_DIR=str(tmp_path))                 # a restart agrees with what is stored
    assert not written()


def lease_id(url):
    with connect(url) as conn:
        return conn.execute("SELECT id FROM garagebooks.lease").fetchone()["id"]


def panel(name):
    return next(p for p in json.loads(dashboard.SOURCE.read_text())["panels"] if p["title"] == name)


def test_the_panel_queries_run_whether_teslamate_is_absent_present_or_held_back(empty_db, monkeypatch):
    start(empty_db, monkeypatch)
    forms = [sources.TeslaMate(sources.ABSENT), sources.TeslaMate(sources.UNTESTED, "held back"), None]
    with connect(empty_db) as conn:
        for form in forms[:2]:
            dash = dashboard.render("UTC", "", teslamate=form)
            for sql in queries(dash):
                conn.execute(runnable(sql)).fetchall()
        conn.execute(
            "CREATE TABLE public.charging_processes (car_id smallint, start_date timestamp, cost numeric(6, 2));"
            "CREATE TABLE public.cars (id smallint PRIMARY KEY);")
        dash = dashboard.render("UTC", "", teslamate=forms[2])                # usable: the charging part is in
        for name in AFTER_TAX_PANELS:
            sql = runnable(next(p for p in dash["panels"] if p["title"] == name)["targets"][0]["rawSql"])
            assert ("charging_processes" in sql) == (name == "After tax by FBT year")   # the stat takes TeslaMate's from the ownership view
            conn.execute(sql).fetchall()


def test_the_panels_total_invoice_and_effective_cost_by_fbt_year(admin):
    try:
        admin.execute("INSERT INTO garagebooks.tax_choice VALUES ('employee', 2025, 45001, 'not_exempt')")     # 32%
        admin.execute("INSERT INTO garagebooks.lease (car_id, financier, start_date, end_date, term_months, pay_frequency,"
                      " pretax_payment, posttax_payment) VALUES (1, 'Example Finance', '2025-10-01', '2028-09-30', 36, 'fortnightly', 300, 20)")
        admin.execute("INSERT INTO garagebooks.expense (car_id, category_id, occurred_at, amount) VALUES "
                      "(1, %s, '2025-11-10T00:00+10', 1100), (1, %s, '2026-05-10T00:00+10', 1100)",
                      (category(admin, "Tyres"), category(admin, "Tyres")))
        dash = dashboard.render("UTC", "", teslamate=sources.TeslaMate(sources.ABSENT))
        rows = admin.execute(runnable(next(p for p in dash["panels"] if p["title"] == "After tax by FBT year")["targets"][0]["rawSql"])
                             .replace("'UTC'", "'Australia/Brisbane'")).fetchall()
        assert [(r["FBT year"], r["Invoices"], r["After tax"]) for r in rows] == [
            ("FBT 2025–26", D("1100.00"), D("700.00")), ("FBT 2026–27", D("1100.00"), D("700.00"))]
        stat = admin.execute(runnable(next(p for p in dash["panels"] if p["title"] == "Running costs after tax")["targets"][0]["rawSql"])
                             .replace("'UTC'", "'Australia/Brisbane'")).fetchone()
        assert (stat["Invoices"], stat["After tax"]) == (D("2200.00"), D("1400.00"))
        admin.execute("TRUNCATE garagebooks.tax_choice")
        stat = admin.execute(runnable(next(p for p in dash["panels"] if p["title"] == "Running costs after tax")["targets"][0]["rawSql"])
                             .replace("'UTC'", "'Australia/Brisbane'")).fetchone()
        assert stat["After tax"] is None                                      # no rate: no figure, never the invoice standing in
    finally:
        admin.execute("TRUNCATE garagebooks.tax_choice")
        admin.execute("DELETE FROM garagebooks.lease")


def test_db_helpers_round_trip_the_choices_and_the_rates_table(empty_db, monkeypatch):
    start(empty_db, monkeypatch)
    with connect(empty_db) as conn:
        assert not db.any_tax_choice(conn) and db.tax_choices(conn) == {}
        db.set_tax_choice(conn, "employee", 2025, 45001, "exempt")
        assert db.tax_choices(conn)[("employee", 2025)]["medicare"] == "exempt" and db.any_tax_choice(conn)
        assert db.tax_positions(conn, "employee", [2024, 2025])[2025]["marginal_rate"] == D("30")
        assert db.tax_positions(conn, "employee", [2024])[2024] is None
        db.set_tax_choice(conn, "employee", 2025, None, None)
        assert not db.any_tax_choice(conn)
        db.set_ev_charging_rate(conn, 2027, D("6.00"))
        assert db.ev_charging_rates(conn)[2027] == D("6.00")


def test_a_category_filter_outside_the_id_range_is_an_empty_list_not_a_crash(app):
    client, _, _ = app
    for n in ("32768", "2147483648", "9223372036854775808"):
        assert client.get(f"/?category={n}").status_code == 200
