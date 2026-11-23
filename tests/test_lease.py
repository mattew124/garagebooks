"""Lease details per car (roadmap L1): the tables, what is checked on entry, the pages, the
category-to-pool mapping, the least-privilege role and the figures shown beside an entry.
All figures are invented. Runs on a database with no TeslaMate, so it passes both ways."""

import io
import re
from datetime import date, timedelta
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import psycopg
import pytest
from psycopg.rows import dict_row

from garagebooks import create_app, db, invoices, lease, validation
from tests.test_validation import PDF

def today_here():
    """Today as the app sees it (APP_TZ, which these tests set to Brisbane), not the runner's UTC date."""
    return datetime.now(ZoneInfo("Australia/Brisbane")).date()


ORIGIN = {"Origin": "http://localhost"}


def connect(url):
    return psycopg.connect(url, row_factory=dict_row, autocommit=True)


def start(url, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("APP_TZ", "Australia/Brisbane")
    for key in ("SKIP_MIGRATE", "DASHBOARD_DIR", "BASE_PATH"):
        monkeypatch.delenv(key, raising=False)
    app = create_app()
    app.testing = True
    return app.test_client()


@pytest.fixture()
def app(empty_db, monkeypatch):
    client = start(empty_db, monkeypatch)
    assert client.post("/cars", data={"name": "Lease car", "kind": "BEV"}, headers=ORIGIN).status_code == 303
    with connect(empty_db) as conn:
        car_id = conn.execute("SELECT id FROM garagebooks.car").fetchone()["id"]
    return client, empty_db, car_id


def budgets_on(url, on=True):
    """Turn the lease budgets setting on (it is off by default): each test has its own database."""
    with connect(url) as conn:
        conn.execute("UPDATE garagebooks.setting SET lease_budgets = %s", (on,))


def good_form(**over):
    today = today_here()
    data = {"financier": "Example Finance", "package_name": "Example package",
            "start_date": (today - timedelta(days=100)).isoformat(), "end_date": (today + timedelta(days=1000)).isoformat(),
            "refinance_date": "", "term_months": "36", "pay_frequency": "fortnightly",
            "pretax_payment": "300.00", "posttax_payment": "20.00", "admin_fee": "5.00",
            "car_cost": "60,000", "residual_amount": "", "residual_percent": "46.875",
            "fbt_base_value": "62000", "fbt_method": "statutory", "statutory_percent": "20",
            "tax_treatment": "employee_contribution", "tax_treatment_note": "",
            "estimated_km_per_year": "15,000", "rego_expiry": (today + timedelta(days=200)).isoformat(),
            "rego_renewed_by_provider": "on",
            "budget_lease": "8450", "budget_fuel_charging": "1200", "budget_insurance": "1500",
            "budget_registration": "900", "budget_maintenance": "600", "budget_tyres": "400", "budget_other": ""}
    data.update(over)
    return data


def add(client, car_id, **over):
    return client.post(f"/cars/{car_id}/lease/new", data=good_form(**over), headers=ORIGIN)


# ---- the schema --------------------------------------------------------------------

def test_the_schema_can_be_applied_twice_and_keeps_a_lease(empty_db, monkeypatch):
    client = start(empty_db, monkeypatch)
    client.post("/cars", data={"name": "Car", "kind": "BEV"}, headers=ORIGIN)
    budgets_on(empty_db)
    with connect(empty_db) as conn:
        car_id = conn.execute("SELECT id FROM garagebooks.car").fetchone()["id"]
    assert add(client, car_id).status_code == 303
    start(empty_db, monkeypatch)        # the app starts again: schema.sql runs again
    start(empty_db, monkeypatch)
    with connect(empty_db) as conn:
        assert conn.execute("SELECT count(*) AS n FROM garagebooks.lease").fetchone()["n"] == 1
        assert conn.execute("SELECT count(*) AS n FROM garagebooks.lease_budget").fetchone()["n"] == 6


def pools(conn):
    return {r["name"]: r["budget_pool"] for r in conn.execute("SELECT name, budget_pool FROM garagebooks.category")}


def test_standard_categories_start_with_a_pool(empty_db, monkeypatch):
    start(empty_db, monkeypatch)
    with connect(empty_db) as conn:
        assert pools(conn) == {
            "Initial purchase": None, "Insurance": "insurance", "Registration": "registration", "Tyres": "tyres",
            "Charging": "fuel_charging", "Fuel": "fuel_charging", "Service": "maintenance"}


def test_an_upgrade_adds_the_pool_column_once_and_keeps_the_owners_choice(empty_db, monkeypatch):
    start(empty_db, monkeypatch)
    with connect(empty_db) as conn:   # a database as it was before this release
        conn.execute("DROP VIEW garagebooks.reminder_due; DROP TABLE garagebooks.lease_budget; DROP TABLE garagebooks.lease;"
                     " ALTER TABLE garagebooks.category DROP COLUMN budget_pool")
    start(empty_db, monkeypatch)
    with connect(empty_db) as conn:
        assert pools(conn)["Insurance"] == "insurance"
        conn.execute("UPDATE garagebooks.category SET budget_pool = NULL WHERE name = 'Insurance'")
        conn.execute("UPDATE garagebooks.category SET budget_pool = 'other' WHERE name = 'Fuel'")
    start(empty_db, monkeypatch)         # a later start never overwrites it
    with connect(empty_db) as conn:
        assert pools(conn)["Insurance"] is None and pools(conn)["Fuel"] == "other"


def lease_sql(**over):
    fields = {"financier": "'F'", "start_date": "'2026-01-01'", "end_date": "'2029-01-01'", "term_months": "36",
              "pay_frequency": "'weekly'"}
    fields.update(over)
    return ("INSERT INTO garagebooks.lease (car_id, " + ", ".join(fields) + ") SELECT id, "
            + ", ".join(fields.values()) + " FROM garagebooks.car LIMIT 1")


@pytest.mark.parametrize("bad", [
    {"end_date": "'2026-01-01'"}, {"end_date": "'2025-12-31'"}, {"term_months": "0"}, {"term_months": "121"},
    {"pay_frequency": "'daily'"}, {"pretax_payment": "-1"}, {"posttax_payment": "-1"}, {"admin_fee": "-0.01"},
    {"car_cost": "-1"}, {"residual_amount": "-1"}, {"residual_percent": "100.001"}, {"residual_percent": "-1"},
    {"statutory_percent": "101"}, {"fbt_method": "'guess'"}, {"tax_treatment": "'cash'"}, {"fbt_base_value": "-1"},
    {"refinance_date": "'2025-12-31'"}, {"refinance_date": "'2029-01-02'"}, {"financier": "' '"},
    {"estimated_km_per_year": "-5"}])
def test_the_database_refuses_nonsense(app, bad):
    _client, url, _car = app
    with connect(url) as conn:
        conn.execute(lease_sql())            # the baseline is fine
        conn.execute("DELETE FROM garagebooks.lease")
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(lease_sql(**bad))


def test_defaults_and_cascades(app):
    _client, url, car_id = app
    with connect(url) as conn:
        conn.execute(lease_sql())
        row = conn.execute("SELECT * FROM garagebooks.lease").fetchone()
        assert (row["statutory_percent"], row["fbt_method"], row["ev_exempt"], row["admin_fee"]) == (
            Decimal("20.00"), "statutory", False, 0)
        conn.execute("INSERT INTO garagebooks.lease_budget VALUES (%s, 'tyres', 400, '2026-01-01')", (row["id"],))
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute("INSERT INTO garagebooks.lease_budget VALUES (%s, 'fuel', 1, '2026-01-01')", (row["id"],))
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute("INSERT INTO garagebooks.lease_budget VALUES (%s, 'other', -1, '2026-01-01')", (row["id"],))
        with pytest.raises(psycopg.errors.UniqueViolation):
            conn.execute("INSERT INTO garagebooks.lease_budget VALUES (%s, 'tyres', 500, '2026-01-01')", (row["id"],))
        conn.execute("DELETE FROM garagebooks.car WHERE id = %s", (car_id,))       # the car goes: its lease and budgets go too
        assert conn.execute("SELECT (SELECT count(*) FROM garagebooks.lease) + "
                            "(SELECT count(*) FROM garagebooks.lease_budget) AS n").fetchone()["n"] == 0


# ---- what is checked on entry ------------------------------------------------------------

def parse(**over):
    return validation.parse_lease_form(good_form(**over), lease.POOLS, lease.FREQUENCIES, lease.FBT_METHODS,
                                       lease.TAX_TREATMENTS)


def test_a_statement_parses():
    values, errors = parse()
    assert errors == {}
    assert values["financier"] == "Example Finance" and values["term_months"] == 36
    assert values["pretax_payment"] == Decimal("300.00") and values["car_cost"] == Decimal("60000.00")
    assert values["residual_percent"] == Decimal("46.875") and values["residual_amount"] is None
    assert values["statutory_percent"] == Decimal("20.00") and values["estimated_km_per_year"] == 15000
    assert values["rego_renewed_by_provider"] is True and values["ev_exempt"] is False
    assert values["budgets"]["tyres"] == Decimal("400.00") and "other" not in values["budgets"]


@pytest.mark.parametrize("field, value, shown", [
    ("financier", "", "financier"), ("financier", "x" * 101, "too long"),
    ("start_date", "", "start date"), ("start_date", "2026-02-30", "isn't valid"),
    ("end_date", "nope", "isn't valid"),
    ("refinance_date", "2026-13-01", "isn't valid"),
    ("term_months", "0", "term"), ("term_months", "121", "term"), ("term_months", "x", "term"),
    ("pay_frequency", "", "how often"), ("pay_frequency", "daily", "how often"),
    ("pretax_payment", "abc", "number"), ("pretax_payment", "-5", "negative"),
    ("posttax_payment", "-5", "negative"), ("admin_fee", "-1", "negative"), ("admin_fee", "lots", "number"),
    ("car_cost", "-1", "negative"), ("residual_amount", "-1", "negative"), ("residual_amount", "70000", "more than"),
    ("residual_percent", "101", "between 0 and 100"), ("residual_percent", "-1", "between 0 and 100"),
    ("residual_percent", "abc", "number"), ("fbt_base_value", "-1", "negative"),
    ("fbt_method", "guess", "method"), ("statutory_percent", "150", "between 0 and 100"),
    ("tax_treatment", "cash", "treatment"), ("tax_treatment_note", "x" * 101, "too long"),
    ("estimated_km_per_year", "-1", "whole number"), ("estimated_km_per_year", "1.5", "whole number"),
    ("rego_expiry", "soon", "isn't valid"), ("budget_tyres", "-1", "negative"), ("budget_other", "x", "number")])
def test_every_field_is_checked(field, value, shown):
    _values, errors = parse(**{field: value})
    assert field in errors and shown in errors[field].lower().replace("’", "'")


def test_the_end_date_and_the_term_fill_each_other_in_but_one_of_them_is_needed():
    values, errors = parse(end_date="", term_months="36", start_date="2026-01-01")
    assert errors == {} and values["end_date"] == date(2028, 12, 31) and values["term_months"] == 36
    values, errors = parse(end_date="2028-12-31", term_months="", start_date="2026-01-01")
    assert errors == {} and values["term_months"] == 36 and values["end_date"] == date(2028, 12, 31)
    _values, errors = parse(end_date="", term_months="")
    assert "end date" in errors["end_date"]
    _values, errors = parse(end_date="", term_months="", start_date="")
    assert "start_date" in errors and "term_months" not in errors


def test_dates_must_run_the_right_way_round():
    _values, errors = parse(start_date="2026-05-01", end_date="2026-05-01")
    assert "after the start" in errors["end_date"]
    _values, errors = parse(start_date="2026-05-01", end_date="2029-05-01", refinance_date="2030-01-01")
    assert "within the lease" in errors["refinance_date"]


def test_a_payment_is_needed_before_or_after_tax():
    _values, errors = parse(pretax_payment="", posttax_payment="0")
    assert "payment" in errors["pretax_payment"]
    values, errors = parse(pretax_payment="", posttax_payment="50")
    assert errors == {} and values["pretax_payment"] == Decimal("0.00")


def test_the_residual_amount_and_percentage_must_agree_with_the_cost():
    _values, errors = parse(car_cost="60000", residual_amount="20000", residual_percent="46.875")
    assert "don't match" in errors["residual_percent"]
    values, errors = parse(car_cost="60000", residual_amount="28125.00", residual_percent="46.875")
    assert errors == {} and values["residual_amount"] == Decimal("28125.00")


def test_a_blank_statutory_percentage_means_twenty_and_a_note_needs_other():
    values, _ = parse(statutory_percent="", tax_treatment_note="ignored")
    assert values["statutory_percent"] == Decimal("20.00") and values["tax_treatment_note"] is None
    values, _ = parse(tax_treatment="other", tax_treatment_note="Salary sacrifice")
    assert values["tax_treatment_note"] == "Salary sacrifice"


# ---- the figures shown beside an entry --------------------------------------------------------

def sample(**over):
    row = {"start_date": date(2026, 1, 1), "end_date": date(2029, 1, 1), "term_months": 36, "pay_frequency": "fortnightly",
           "pretax_payment": Decimal("300"), "posttax_payment": Decimal("20"), "admin_fee": Decimal("5"),
           "car_cost": Decimal("60000"), "residual_amount": None, "residual_percent": Decimal("46.875")}
    row.update(over)
    return row


def test_payment_figures():
    figures = lease.derived(sample(), [], date(2026, 6, 1))
    assert (figures["pays_per_year"], figures["total_per_pay"], figures["yearly_payments"]) == (26, Decimal("325"), Decimal("8450.00"))
    assert lease.yearly_payments(sample(pay_frequency="weekly")) == Decimal("16900.00")
    assert lease.yearly_payments(sample(pay_frequency="monthly")) == Decimal("3900.00")


def test_budgets_are_totalled_against_the_payments():
    budgets = [{"pool": "lease", "yearly_amount": Decimal("8450"), "effective_from": date(2026, 1, 1)},
               {"pool": "tyres", "yearly_amount": Decimal("400"), "effective_from": date(2026, 1, 1)},
               {"pool": "tyres", "yearly_amount": Decimal("600"), "effective_from": date(2027, 1, 1)}]
    figures = lease.derived(sample(), budgets, date(2026, 6, 1))
    assert figures["budget_total"] == Decimal("8850") and figures["budget_gap"] == Decimal("530.00")   # the first set only
    assert lease.budget_on(budgets, "tyres", date(2026, 12, 31)) == Decimal("400")
    assert lease.budget_on(budgets, "tyres", date(2027, 1, 1)) == Decimal("600")
    assert lease.budget_on(budgets, "tyres", date(2025, 12, 31)) is None and lease.budget_on(budgets, "other", date(2027, 1, 1)) is None


def test_residual_is_worked_out_either_way_and_held_against_the_minimum():
    assert lease.residual_figures(sample()) == (Decimal("28125.00"), Decimal("46.875"))
    assert lease.residual_figures(sample(residual_percent=None, residual_amount=Decimal("30000"))) == (Decimal("30000"), Decimal("50.000"))
    assert lease.residual_figures(sample(car_cost=None)) == (None, Decimal("46.875"))
    assert [lease.minimum_residual_percent(m) for m in (12, 24, 36, 48, 60)] == [
        Decimal("65.625"), Decimal("56.25"), Decimal("46.875"), Decimal("37.5"), Decimal("28.125")]
    assert lease.minimum_residual_percent(30) is None and lease.minimum_residual_percent(72) is None
    assert lease.derived(sample(residual_percent=Decimal("40")), [], date(2026, 6, 1))["residual_below_minimum"] is True
    assert lease.derived(sample(), [], date(2026, 6, 1))["residual_below_minimum"] is False


def test_days_remaining_and_status():
    row = sample()
    assert lease.days_remaining(row, date(2028, 12, 31)) == 1 and lease.days_remaining(row, date(2029, 1, 1)) == 0
    assert lease.days_remaining(row, date(2029, 1, 11)) == -10
    assert [lease.status(row, d) for d in (date(2025, 12, 31), date(2026, 1, 1), date(2029, 1, 1), date(2029, 1, 2))] == [
        "upcoming", "current", "current", "past"]


def test_overlap_includes_both_ends():
    assert lease.overlaps(date(2026, 1, 1), date(2026, 6, 1), date(2026, 6, 1), date(2027, 1, 1))
    assert not lease.overlaps(date(2026, 1, 1), date(2026, 5, 31), date(2026, 6, 1), date(2027, 1, 1))


# ---- the pages -------------------------------------------------------------------------------

def test_an_unknown_car_has_no_lease_page(app):
    client, _url, _car = app
    for path in ("/cars/99999/lease", "/cars/99999/lease/new", "/cars/99999/lease/1/edit"):
        assert client.get(path).status_code == 404
    assert client.post("/cars/99999/lease/new", data=good_form(), headers=ORIGIN).status_code == 404
    assert client.post("/cars/99999/lease/1/delete", headers=ORIGIN).status_code == 404


def test_the_cars_page_links_to_the_lease(app):
    client, _url, car_id = app
    assert f'href="/cars/{car_id}/lease"' in client.get("/cars").data.decode()


def test_a_car_without_a_lease_says_so_and_offers_to_add_one(app):
    client, _url, car_id = app
    page = client.get(f"/cars/{car_id}/lease").data.decode()
    assert "No lease recorded" in page and f"/cars/{car_id}/lease/new" in page and "not tax advice" in page


def test_add_shows_the_lease_with_its_figures(app):
    client, url, car_id = app
    budgets_on(url)
    resp = add(client, car_id)
    assert resp.status_code == 303 and resp.headers["Location"].endswith(f"/cars/{car_id}/lease?msg=lease_added")
    page = client.get(f"/cars/{car_id}/lease?msg=lease_added").data.decode()
    assert "Lease added." in page and "Example Finance" in page and "Current lease" in page
    assert "$325.00" in page and "$8,450.00" in page and "$28,125.00" in page and "46.875%" in page
    assert "ATO minimum residual for 3 years: 46.875%" in re.sub(r"\s+", " ", page) and "meets it" in page
    assert "1,000 day" in page or "days remaining" in page
    assert "$13,050.00" in page          # total of the budgets
    assert "come to $8,320.00 a year" in page and "differ from that by $4,730.00 (more)" in page
    with connect(url) as conn:
        row = conn.execute("SELECT * FROM garagebooks.lease").fetchone()
        assert row["residual_amount"] is None and row["residual_percent"] == Decimal("46.875")
        budgets = conn.execute("SELECT pool, effective_from FROM garagebooks.lease_budget").fetchall()
        assert len(budgets) == 6 and {b["effective_from"] for b in budgets} == {row["start_date"]}


def test_a_mistake_keeps_what_was_typed_and_marks_the_field(app):
    client, url, car_id = app
    resp = add(client, car_id, admin_fee="-3", financier="Keep me")
    page = resp.data.decode()
    assert resp.status_code == 400 and "can&#39;t be negative" in page and 'value="Keep me"' in page
    assert 'aria-invalid="true"' in page and 'aria-describedby="e-admin_fee"' in page
    with connect(url) as conn:
        assert conn.execute("SELECT count(*) AS n FROM garagebooks.lease").fetchone()["n"] == 0


def test_overlapping_leases_are_refused_but_a_refinance_from_the_next_day_is_not(app):
    client, url, car_id = app
    assert add(client, car_id, start_date="2026-01-01", end_date="2028-12-31").status_code == 303
    clash = add(client, car_id, start_date="2028-12-31", end_date="2031-12-31")
    assert clash.status_code == 400 and "overlap" in clash.data.decode()
    assert add(client, car_id, start_date="2024-01-01", end_date="2026-01-01").status_code == 400
    assert add(client, car_id, start_date="2029-01-01", end_date="2031-12-31").status_code == 303
    assert add(client, car_id, start_date="2023-01-01", end_date="2025-12-31").status_code == 303
    with connect(url) as conn:
        assert conn.execute("SELECT count(*) AS n FROM garagebooks.lease").fetchone()["n"] == 3


def test_past_and_next_leases_are_listed_below_the_current_one(app):
    client, url, car_id = app
    today = today_here()
    add(client, car_id, financier="Old Finance", start_date=(today - timedelta(days=2000)).isoformat(),
        end_date=(today - timedelta(days=400)).isoformat())
    add(client, car_id, financier="Current Finance", start_date=(today - timedelta(days=399)).isoformat(),
        end_date=(today + timedelta(days=500)).isoformat())
    page = client.get(f"/cars/{car_id}/lease").data.decode()
    assert page.index("Current Finance") < page.index("Old Finance")
    assert "Other lease periods" in page and "Ended 400 days ago" in page and "500</strong> days remaining" in page
    assert page.count('data-confirm="Delete this lease') == 2


def test_edit_changes_the_lease_and_its_first_budgets_only(app):
    client, url, car_id = app
    budgets_on(url)
    add(client, car_id)
    with connect(url) as conn:
        row = conn.execute("SELECT id, start_date FROM garagebooks.lease").fetchone()
        conn.execute("INSERT INTO garagebooks.lease_budget VALUES (%s, 'tyres', 650, %s)",
                     (row["id"], row["start_date"] + timedelta(days=400)))      # a later re-budget (L3)
    form = client.get(f"/cars/{car_id}/lease/{row['id']}/edit").data.decode()
    assert 'value="Example Finance"' in form and 'name="budget_tyres"' in form and 'value="400.00"' in form
    new_start = row["start_date"] + timedelta(days=10)
    resp = client.post(f"/cars/{car_id}/lease/{row['id']}/edit", headers=ORIGIN, data=good_form(
        financier="Other Finance", start_date=new_start.isoformat(), budget_tyres="450", budget_lease=""))
    assert resp.status_code == 303 and "msg=lease_saved" in resp.headers["Location"]
    with connect(url) as conn:
        assert conn.execute("SELECT financier FROM garagebooks.lease").fetchone()["financier"] == "Other Finance"
        budgets = {(b["pool"], b["effective_from"]): b["yearly_amount"] for b in conn.execute("SELECT * FROM garagebooks.lease_budget")}
        assert budgets[("tyres", new_start)] == 450 and ("lease", new_start) not in budgets
        assert budgets[("tyres", row["start_date"] + timedelta(days=400))] == 650     # the later one is untouched


def test_editing_cannot_make_a_lease_overlap_another(app):
    client, url, car_id = app
    add(client, car_id, start_date="2020-01-01", end_date="2022-12-31")
    add(client, car_id, start_date="2023-01-01", end_date="2025-12-31")
    with connect(url) as conn:
        second = conn.execute("SELECT id FROM garagebooks.lease ORDER BY start_date DESC").fetchone()["id"]
    resp = client.post(f"/cars/{car_id}/lease/{second}/edit", headers=ORIGIN, data=good_form(start_date="2022-06-01", end_date="2025-12-31"))
    assert resp.status_code == 400 and "overlap" in resp.data.decode()
    assert client.post(f"/cars/{car_id}/lease/{second}/edit", headers=ORIGIN,
                       data=good_form(start_date="2023-01-01", end_date="2026-06-30")).status_code == 303   # its own range is fine


def test_a_lease_of_another_car_cannot_be_reached(app):
    client, url, car_id = app
    client.post("/cars", data={"name": "Second", "kind": "ICE"}, headers=ORIGIN)
    add(client, car_id)
    with connect(url) as conn:
        lease_id = conn.execute("SELECT id FROM garagebooks.lease").fetchone()["id"]
        other = conn.execute("SELECT id FROM garagebooks.car WHERE id <> %s", (car_id,)).fetchone()["id"]
    assert client.get(f"/cars/{other}/lease/{lease_id}/edit").status_code == 404
    assert client.post(f"/cars/{other}/lease/{lease_id}/edit", data=good_form(), headers=ORIGIN).status_code == 404
    assert client.post(f"/cars/{other}/lease/{lease_id}/delete", headers=ORIGIN).status_code == 404


def test_delete_removes_the_lease_and_its_budgets(app):
    client, url, car_id = app
    add(client, car_id)
    with connect(url) as conn:
        lease_id = conn.execute("SELECT id FROM garagebooks.lease").fetchone()["id"]
    resp = client.post(f"/cars/{car_id}/lease/{lease_id}/delete", headers=ORIGIN)
    assert resp.status_code == 303 and "msg=lease_deleted" in resp.headers["Location"]
    with connect(url) as conn:
        assert conn.execute("SELECT (SELECT count(*) FROM garagebooks.lease) + "
                            "(SELECT count(*) FROM garagebooks.lease_budget) AS n").fetchone()["n"] == 0
    assert client.post(f"/cars/{car_id}/lease/{lease_id}/delete", headers=ORIGIN).status_code == 404


def test_reading_functions(app):
    client, url, car_id = app
    today = today_here()
    budgets_on(url)
    add(client, car_id)
    with connect(url) as conn:
        assert db.current_lease(conn, car_id, today)["financier"] == "Example Finance"
        assert db.current_lease(conn, car_id, today + timedelta(days=5000)) is None
        row = db.car_leases(conn, car_id)[0]
        assert db.get_lease(conn, row["id"], car_id)["id"] == row["id"] and db.get_lease(conn, row["id"], car_id + 1) is None
        assert len(db.lease_budgets(conn, row["id"])) == 6


# ---- the category pool ------------------------------------------------------------------------

def test_a_category_can_be_given_a_pool_and_cleared(app):
    client, url, _car = app
    budgets_on(url)
    with connect(url) as conn:
        cat = conn.execute("SELECT id FROM garagebooks.category WHERE name = 'Tyres'").fetchone()["id"]
    page = client.get("/categories").data.decode()
    assert "Lease budget pool" in page and '<option value="tyres" selected>Tyres</option>' in page
    assert client.post(f"/categories/{cat}/pool", data={"budget_pool": "other"}, headers=ORIGIN).status_code == 303
    with connect(url) as conn:
        assert pools(conn)["Tyres"] == "other"
    assert client.post(f"/categories/{cat}/pool", data={"budget_pool": ""}, headers=ORIGIN).status_code == 303
    with connect(url) as conn:
        assert pools(conn)["Tyres"] is None
    assert client.post(f"/categories/{cat}/pool", data={"budget_pool": "fuel"}, headers=ORIGIN).status_code == 400
    assert client.post("/categories/99999/pool", data={"budget_pool": "other"}, headers=ORIGIN).status_code == 404


# ---- the least-privilege role ---------------------------------------------------------------------

def test_the_restricted_role_can_use_leases_and_pools(pg, admin):
    """pg.provision() ran scripts/provision-db.sh: the role it makes can work with leases and pools, and no more."""
    with psycopg.connect(pg.app_url, row_factory=dict_row, autocommit=True) as conn:
        lease_id = conn.execute(
            "INSERT INTO garagebooks.lease (car_id, financier, start_date, end_date, term_months, pay_frequency) "
            "VALUES (1, 'F', '2026-01-01', '2029-01-01', 36, 'weekly') RETURNING id").fetchone()["id"]
        conn.execute("INSERT INTO garagebooks.lease_budget VALUES (%s, 'lease', 100, '2026-01-01')", (lease_id,))
        conn.execute("UPDATE garagebooks.lease SET financier = 'G' WHERE id = %s", (lease_id,))
        assert conn.execute("SELECT budget_pool FROM garagebooks.category WHERE name = 'Insurance'").fetchone()
        conn.execute("UPDATE garagebooks.category SET budget_pool = budget_pool WHERE name = 'Insurance'")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("UPDATE garagebooks.category SET name = 'x' WHERE name = 'Insurance'")
        conn.execute("DELETE FROM garagebooks.lease WHERE id = %s", (lease_id,))
    assert pg.provision().returncode == 0          # and provisioning again is safe


# ---- lease budgets are optional (Settings) -------------------------------------------------------

def count(url, table):
    with connect(url) as conn:
        return conn.execute(f"SELECT count(*) AS n FROM garagebooks.{table}").fetchone()["n"]


def test_lease_budgets_are_off_until_turned_on_in_settings(app):
    client, url, _car = app
    with connect(url) as conn:
        assert db.lease_budgets_on(conn) is False and db.get_settings(conn)["lease_budgets"] is False
    assert 'name="lease_budgets"' in client.get("/settings").data.decode()
    assert 'name="lease_budgets" value="1" checked' not in client.get("/settings").data.decode()
    assert client.post("/settings", data={"app_url": "", "grafana_url": "", "lease_budgets": "1"}, headers=ORIGIN).status_code == 303
    with connect(url) as conn:
        assert db.lease_budgets_on(conn) is True
    assert "checked" in re.search(r'<input[^>]*name="lease_budgets"[^>]*>', client.get("/settings").data.decode()).group(0)
    assert client.post("/settings", data={"app_url": "", "grafana_url": ""}, headers=ORIGIN).status_code == 303   # unticked
    with connect(url) as conn:
        assert db.lease_budgets_on(conn) is False


def test_with_budgets_off_the_lease_form_has_no_budget_boxes_and_what_is_posted_is_ignored(app):
    client, url, car_id = app
    form = client.get(f"/cars/{car_id}/lease/new").data.decode()
    assert "budget_tyres" not in form and "Yearly budgets" not in form
    assert add(client, car_id).status_code == 303                 # good_form still carries budget_* values
    assert count(url, "lease") == 1 and count(url, "lease_budget") == 0
    page = client.get(f"/cars/{car_id}/lease").data.decode()
    assert "Yearly budgets" not in page and "No budgets entered" not in page and "$13,050.00" not in page
    with connect(url) as conn:
        lease_id = conn.execute("SELECT id FROM garagebooks.lease").fetchone()["id"]
    assert "budget_tyres" not in client.get(f"/cars/{car_id}/lease/{lease_id}/edit").data.decode()


def test_parsing_with_budgets_off_leaves_them_out_and_with_them_on_reads_them():
    off = validation.parse_lease_form(good_form(budget_tyres="-5"), lease.POOLS, lease.FREQUENCIES, lease.FBT_METHODS,
                                      lease.TAX_TREATMENTS, budgets_on=False)
    assert off[1] == {} and off[0]["budgets"] is None                   # not even a mistake in them is reported
    on, errors = validation.parse_lease_form(good_form(), lease.POOLS, lease.FREQUENCIES, lease.FBT_METHODS,
                                             lease.TAX_TREATMENTS, budgets_on=True)
    assert errors == {} and on["budgets"]["lease"] == Decimal("8450.00")


def test_editing_with_budgets_off_keeps_the_saved_budgets_and_moves_them_with_the_start_date(app):
    client, url, car_id = app
    budgets_on(url)
    add(client, car_id)
    budgets_on(url, False)
    with connect(url) as conn:
        row = conn.execute("SELECT id, start_date FROM garagebooks.lease").fetchone()
        later = row["start_date"] + timedelta(days=400)
        conn.execute("INSERT INTO garagebooks.lease_budget VALUES (%s, 'tyres', 650, %s)", (row["id"], later))
    new_start = row["start_date"] + timedelta(days=10)
    resp = client.post(f"/cars/{car_id}/lease/{row['id']}/edit", headers=ORIGIN,
                       data=good_form(start_date=new_start.isoformat(), budget_tyres="1", budget_lease=""))
    assert resp.status_code == 303
    with connect(url) as conn:
        budgets = {(b["pool"], b["effective_from"]): b["yearly_amount"] for b in conn.execute("SELECT * FROM garagebooks.lease_budget")}
    assert len(budgets) == 7                                            # none dropped, none added
    assert budgets[("tyres", new_start)] == 400 and budgets[("lease", new_start)] == Decimal("8450.00")   # as saved, now from the new start
    assert budgets[("tyres", later)] == 650
    assert all(start in (new_start, later) for _pool, start in budgets)
    budgets_on(url)                                                     # turned on again: they are all there
    assert "$13,050.00" in client.get(f"/cars/{car_id}/lease").data.decode()


def test_with_budgets_off_the_categories_page_has_no_pool_selectors(app):
    client, url, _car = app
    page = client.get("/categories").data.decode()
    assert "Lease budget pool" not in page and 'name="budget_pool"' not in page and "yearly budget" not in page
    budgets_on(url)
    page = client.get("/categories").data.decode()
    assert "Lease budget pool" in page and 'name="budget_pool"' in page and "yearly budget" in page


# ---- associate leases, the end date and the term ----------------------------------------------------

def associate_form(**over):
    return good_form(**{"associate_lease": "on", "financier": "", "residual_amount": "30000", "residual_percent": "", **over})


def test_an_associate_lease_needs_no_financier_and_keeps_no_residual(app):
    client, url, car_id = app
    assert add(client, car_id, financier="").status_code == 400            # a financier is needed otherwise
    resp = client.post(f"/cars/{car_id}/lease/new", data=associate_form(), headers=ORIGIN)
    assert resp.status_code == 303
    with connect(url) as conn:
        row = conn.execute("SELECT * FROM garagebooks.lease").fetchone()
    assert row["associate_lease"] is True and row["financier"] is None
    assert row["residual_amount"] is None and row["residual_percent"] is None
    page = client.get(f"/cars/{car_id}/lease").data.decode()
    assert "Associate lease" in page and "Residual" not in page and "ATO minimum residual" not in page
    assert "Financier" not in page and "Example package" in page
    form = client.get(f"/cars/{car_id}/lease/{row['id']}/edit").data.decode()
    assert re.search(r'name="associate_lease"\s+checked|checked[^>]*name="associate_lease"|name="associate_lease"[^>]*checked', form)


def test_an_associate_lease_ignores_residual_mistakes_in_the_hidden_boxes():
    values, errors = validation.parse_lease_form(
        associate_form(residual_amount="999999999", residual_percent="500"), lease.POOLS, lease.FREQUENCIES,
        lease.FBT_METHODS, lease.TAX_TREATMENTS)
    assert errors == {} and values["residual_amount"] is None and values["residual_percent"] is None and values["financier"] is None


def test_a_lease_without_a_financier_can_be_saved_in_the_database(app):
    _client, url, _car = app
    with connect(url) as conn:
        conn.execute(lease_sql(financier="NULL"))
    with connect(url) as conn:
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(lease_sql(financier="' '", start_date="'2030-01-01'", end_date="'2033-01-01'"))   # blank still refused


def test_the_end_date_is_filled_from_the_start_and_the_term_and_the_term_from_the_dates(app):
    client, url, car_id = app
    assert add(client, car_id, start_date="2026-01-01", end_date="", term_months="24").status_code == 303
    assert add(client, car_id, start_date="2028-03-15", end_date="2031-03-14", term_months="").status_code == 303
    with connect(url) as conn:
        rows = conn.execute("SELECT start_date, end_date, term_months FROM garagebooks.lease ORDER BY start_date").fetchall()
    assert (rows[0]["end_date"], rows[0]["term_months"]) == (date(2027, 12, 31), 24)
    assert (rows[1]["end_date"], rows[1]["term_months"]) == (date(2031, 3, 14), 36)
    refused = add(client, car_id, start_date="2040-01-01", end_date="", term_months="")
    assert refused.status_code == 400 and "end date, or the term" in refused.data.decode()
    assert add(client, car_id, start_date="2040-01-01", end_date="", term_months="121").status_code == 400


def test_the_new_lease_page_does_not_guess_the_start_date(app):
    client, _url, car_id = app
    page = client.get(f"/cars/{car_id}/lease/new").data.decode()
    assert re.search(r'id="f-start_date"[^>]*value=""', page, re.S)
    assert 'data-prefill-url="/api/lease-prefill"' in page and 'name="form_file"' in page and 'name="agreement_file"' in page
    assert 'value="20"' in page and "statutory" in page            # the other defaults remain


# ---- documents kept with a lease -------------------------------------------------------------------------

def test_documents_can_be_linked_shown_and_removed(app):
    client, url, car_id = app
    assert add(client, car_id, form_url="https://files.example.test/form.pdf",
               agreement_url="https://files.example.test/agreement.pdf").status_code == 303
    page = client.get(f"/cars/{car_id}/lease").data.decode()
    assert 'href="https://files.example.test/form.pdf"' in page and 'href="https://files.example.test/agreement.pdf"' in page
    assert "Lease form" in page and "Lease agreement" in page and 'data-document="form"' in page
    with connect(url) as conn:
        lease_id = conn.execute("SELECT id FROM garagebooks.lease").fetchone()["id"]
    edit = client.get(f"/cars/{car_id}/lease/{lease_id}/edit").data.decode()
    assert 'value="https://files.example.test/form.pdf"' in edit and 'name="remove_form"' in edit
    resp = client.post(f"/cars/{car_id}/lease/{lease_id}/edit", headers=ORIGIN, data=good_form(
        form_url="https://files.example.test/form.pdf", agreement_url="https://files.example.test/agreement.pdf", remove_form="1"))
    assert resp.status_code == 303
    with connect(url) as conn:
        row = conn.execute("SELECT form_url, form_path, agreement_url FROM garagebooks.lease").fetchone()
    assert row["form_url"] is None and row["form_path"] is None and row["agreement_url"] == "https://files.example.test/agreement.pdf"
    page = client.get(f"/cars/{car_id}/lease").data.decode()
    assert 'data-document="form"' not in page and 'data-document="agreement"' in page


def test_a_document_link_that_is_not_https_is_refused(app):
    client, url, car_id = app
    for field in ("form_url", "agreement_url"):
        resp = add(client, car_id, **{field: "javascript:alert(1)"})
        assert resp.status_code == 400
    assert count(url, "lease") == 0
    assert add(client, car_id, form_path="/../outside.pdf").status_code == 400
    assert add(client, car_id, form_path="/Cars/x/Lease/form.pdf").status_code == 303


def test_the_database_refuses_a_document_that_is_not_a_link_or_a_path(app):
    _client, url, _car = app
    with connect(url) as conn:
        for bad in ({"form_url": "'http://insecure.example.test/a.pdf'"}, {"agreement_url": "'not a link'"},
                    {"form_path": "'relative.pdf'"}, {"agreement_path": "'/'"}):
            with pytest.raises(psycopg.errors.CheckViolation):
                conn.execute(lease_sql(**bad))


@pytest.fixture()
def folder(empty_db, monkeypatch, tmp_path):
    """The app with INVOICES_DIR set and a folder for the car, in the temporary directory."""
    root = tmp_path / "invoices"
    (root / "Cars" / "Lease car").mkdir(parents=True)
    monkeypatch.setenv("INVOICES_DIR", str(root))
    client = start(empty_db, monkeypatch)
    client.post("/cars", data={"name": "Lease car", "kind": "BEV"}, headers=ORIGIN)
    with connect(empty_db) as conn:
        car_id = conn.execute("SELECT id FROM garagebooks.car").fetchone()["id"]
    return client, empty_db, car_id, root


def choose_folder(client, car_id):
    assert client.post(f"/local/folders/{car_id}", data={"folder_id": "/Cars/Lease car"}, headers=ORIGIN).status_code == 303


def pdf_upload(name="form.pdf", content=PDF):
    return (io.BytesIO(content), name, "application/pdf")


def lease_files(root):
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file() and p.name != ".garagebooks")


def test_uploaded_lease_documents_go_to_the_lease_folder_of_the_cars_folder(folder):
    client, url, car_id, root = folder
    choose_folder(client, car_id)
    assert "Choose from the invoice folder" in client.get(f"/cars/{car_id}/lease/new").data.decode()
    resp = client.post(f"/cars/{car_id}/lease/new", headers=ORIGIN, content_type="multipart/form-data",
                       data={**good_form(), "form_file": pdf_upload("a.pdf"), "agreement_file": pdf_upload("b.pdf", PDF + b"2")})
    assert resp.status_code == 303 and resp.headers["Location"].endswith("msg=lease_added")
    saved = lease_files(root)
    assert len(saved) == 2 and all(f.startswith("Cars/Lease car/Lease/") and f.endswith(".pdf") for f in saved)
    with connect(url) as conn:
        row = conn.execute("SELECT form_path, form_url, agreement_path, agreement_url FROM garagebooks.lease").fetchone()
    assert row["form_url"] is None and row["agreement_url"] is None
    assert row["form_path"].startswith("/Cars/Lease car/Lease/") and "Lease form" in row["form_path"]
    assert row["agreement_path"].startswith("/Cars/Lease car/Lease/") and "Lease agreement" in row["agreement_path"]
    page = client.get(f"/cars/{car_id}/lease").data.decode()
    assert "/local/file?path=" in page and "Lease form" in page
    link = re.search(r'href="(/local/file\?path=[^"]+)"', page).group(1)
    served = client.get(link.replace("&amp;", "&"))
    assert served.status_code == 200 and served.data.startswith(b"%PDF")


def test_a_lease_saved_without_a_folder_says_the_pdf_was_not_kept(folder):
    client, url, car_id, root = folder                                 # no folder chosen for the car
    resp = client.post(f"/cars/{car_id}/lease/new", headers=ORIGIN, content_type="multipart/form-data",
                       data={**good_form(), "form_file": pdf_upload()})
    assert resp.status_code == 303 and resp.headers["Location"].endswith("msg=lease_added_not_uploaded")
    assert "The PDF wasn't kept" in client.get(resp.headers["Location"]).data.decode().replace("&#39;", "'")
    assert count(url, "lease") == 1 and lease_files(root) == []
    with connect(url) as conn:
        lease_id = conn.execute("SELECT id FROM garagebooks.lease").fetchone()["id"]
    again = client.post(f"/cars/{car_id}/lease/{lease_id}/edit", headers=ORIGIN, content_type="multipart/form-data",
                        data={**good_form(), "agreement_file": pdf_upload()})
    assert again.headers["Location"].endswith("msg=lease_saved_not_uploaded")


def test_a_lease_document_that_is_not_a_pdf_is_refused_and_nothing_is_saved(folder):
    client, url, car_id, root = folder
    choose_folder(client, car_id)
    resp = client.post(f"/cars/{car_id}/lease/new", headers=ORIGIN, content_type="multipart/form-data",
                       data={**good_form(), "form_file": pdf_upload("notes.pdf", b"just text")})
    assert resp.status_code == 400 and 'aria-describedby="e-form_file"' in resp.data.decode()
    assert count(url, "lease") == 0 and lease_files(root) == []
    bad = client.post(f"/cars/{car_id}/lease/new", headers=ORIGIN, content_type="multipart/form-data",
                      data={**good_form(admin_fee="-1"), "agreement_file": pdf_upload("b.pdf")})
    assert bad.status_code == 400 and "choose the PDF again" in bad.data.decode()
    assert lease_files(root) == []                                      # not saved while the form has a mistake


# ---- reading a lease form ---------------------------------------------------------------------------------

FORM_TEXT = (Path(__file__).parent / "fixtures" / "associate-lease-form.txt").read_text()


def prefill(client, content=PDF, name="form.pdf", **extra):
    data = {**extra}
    if content is not None:
        data["form_file"] = pdf_upload(name, content)
    return client.post("/api/lease-prefill", data=data, headers=ORIGIN, content_type="multipart/form-data")


def test_a_lease_form_is_read_into_the_forms_fields(app, monkeypatch):
    client, url, _car = app
    monkeypatch.setattr(invoices, "extract_text", lambda pdf: FORM_TEXT)
    budgets_on(url)
    resp = prefill(client)
    assert resp.status_code == 200
    fields = resp.get_json()["fields"]
    assert fields["associate_lease"] is True and fields["start_date"] == "2026-07-01" and fields["end_date"] == "2028-06-30"
    assert fields["term_months"] == "24" and fields["pay_frequency"] == "fortnightly" and fields["pretax_payment"] == "500.00"
    assert fields["budget_lease"] == "13000.00" and fields["budget_insurance"] == "1150.00"
    for private in ("Alex", "Sample", "0400", "000AAA"):
        assert private not in resp.get_data(as_text=True)


def test_budgets_are_left_out_of_a_read_lease_form_while_the_setting_is_off(app, monkeypatch):
    client, _url, _car = app
    monkeypatch.setattr(invoices, "extract_text", lambda pdf: FORM_TEXT)
    fields = prefill(client).get_json()["fields"]
    assert not [k for k in fields if k.startswith("budget_")] and fields["fbt_base_value"] == "61000.00"


def test_a_pdf_that_is_not_a_lease_form_gives_no_fields_and_a_bad_request_is_refused(app, monkeypatch):
    client, _url, _car = app
    monkeypatch.setattr(invoices, "extract_text", lambda pdf: "Tax invoice\nTotal $10.00\n")
    assert prefill(client).get_json() == {"fields": {}}
    assert prefill(client, content=None).status_code == 400                     # nothing chosen
    assert prefill(client, content=b"not a pdf").status_code == 400
    assert client.post("/api/lease-prefill", data={}, headers={"Origin": "http://evil.example.test"}).status_code in (400, 403)
    assert client.get("/api/lease-prefill").status_code == 405


def test_a_lease_form_in_the_invoice_folder_can_be_read_without_uploading_it(folder, monkeypatch):
    client, _url, car_id, root = folder
    choose_folder(client, car_id)
    (root / "Cars" / "Lease car" / "form.pdf").write_bytes(PDF)
    monkeypatch.setattr(invoices, "extract_text", lambda pdf: FORM_TEXT)
    resp = client.post("/api/lease-prefill", headers=ORIGIN, data={"local_id": "/Cars/Lease car/form.pdf", "name": "form.pdf"})
    assert resp.status_code == 200 and resp.get_json()["fields"]["start_date"] == "2026-07-01"
    gone = client.post("/api/lease-prefill", headers=ORIGIN, data={"local_id": "/Cars/Lease car/gone.pdf", "name": "gone.pdf"})
    assert gone.status_code == 502 and "error" in gone.get_json()


# ---- several lease periods ----------------------------------------------------------------------------------

def test_a_car_can_hold_three_lease_periods_each_with_its_own_documents(app):
    client, url, car_id = app
    today = today_here()
    periods = [(today - timedelta(days=1500), today - timedelta(days=801)), (today - timedelta(days=800), today + timedelta(days=100)),
               (today + timedelta(days=101), today + timedelta(days=900))]
    for n, (start, end) in enumerate(periods, 1):
        resp = add(client, car_id, financier=f"Finance {n}", start_date=start.isoformat(), end_date=end.isoformat(),
                   form_url=f"https://files.example.test/form-{n}.pdf", agreement_url=f"https://files.example.test/agreement-{n}.pdf")
        assert resp.status_code == 303
    assert add(client, car_id, start_date=(today - timedelta(days=900)).isoformat(),
               end_date=(today - timedelta(days=700)).isoformat()).status_code == 400          # overlaps: still refused
    with connect(url) as conn:
        rows = conn.execute("SELECT financier, form_url, agreement_url FROM garagebooks.lease ORDER BY start_date").fetchall()
    assert [(r["form_url"], r["agreement_url"]) for r in rows] == [
        (f"https://files.example.test/form-{n}.pdf", f"https://files.example.test/agreement-{n}.pdf") for n in (1, 2, 3)]
    page = client.get(f"/cars/{car_id}/lease").data.decode()
    assert "Other lease periods" in page and 'id="add-lease-top"' in page
    assert len(re.findall(r'<details class="lease-period"', page)) == 2
    assert page.count("Current lease") >= 1 and page.count("Add a lease</a>") == 1       # the bottom button, with a lease present
    for n in (1, 2, 3):
        assert f'href="https://files.example.test/form-{n}.pdf"' in page and f'href="https://files.example.test/agreement-{n}.pdf"' in page
    assert page.index("Finance 2") < page.index("Finance 1")                              # current first


def test_the_page_offers_one_add_button_while_the_car_has_no_lease(app):
    client, _url, car_id = app
    page = client.get(f"/cars/{car_id}/lease").data.decode()
    assert 'id="add-lease-top"' in page and "Add a lease period" in page and "Add a lease</a>" not in page
    assert "<details" not in page
