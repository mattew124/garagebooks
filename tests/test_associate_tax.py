"""An associate lease after tax, for the household (roadmap L7): the associate owns the car, receives the lease
payments as business income less depreciation (diminishing value, 25% a year on the lower of the car's value and
the car limit, by days held), and gets the small business income tax offset; the employee saves tax on the
pre-tax payments. garagebooks.lease_income_year() is the one definition; the FBT report and the dashboard read it.

Worked example (invented): a car valued at $40,000 leased to the employer from 1 July 2026 to 30 June 2028 for
$1,000 a month, all pre-tax. You: $135,001 - $190,000 (37%), not exempt (39%). The associate: $45,001 - $135,000
(30%), not exempt, which stands for $90,000.50.

  2026-27  payments 12,000.00; depreciation 40,000 x 25% x 365/365 = 10,000.00; business income 2,000.00
           associate's tax on 92,000.50 less on 90,000.50 = 600.00; Medicare 2% x 2,000 = 40.00
           small business offset 16% x tax on 92,000.50 (18,120.15) x 2,000 / 92,000.50 = 63.03
           associate's extra tax 600.00 + 40.00 - 63.03 = 576.97; your saving 12,000 x 39% = 4,680.00
           household: 576.97 - 4,680.00 = -4,103.03 (the lease saves the household $4,103.03)
  2027-28  depreciation 30,000 x 25% x 366/366 = 7,500.00; business income 4,500.00
           extra tax 1,350.00 + Medicare 90.00 - offset 143.77 = 1,296.23; household 1,296.23 - 4,680.00 = -3,383.77
"""

import re
from decimal import Decimal

from garagebooks import db
from tests.test_fbt import text_of
from tests.test_lease import ORIGIN, app, connect, good_form  # noqa: F401 - app is a fixture

D = Decimal


def associate_lease(conn, car_id=1, value=None, cost="40000", start="2026-07-01", end="2028-06-30", **over):
    cols = {"car_id": car_id, "financier": "Associate", "start_date": start, "end_date": end, "term_months": 24,
            "pay_frequency": "monthly", "pretax_payment": "1000", "posttax_payment": "0", "car_cost": cost,
            "associate_lease": True, "depreciation_value": value, **over}
    names = ", ".join(cols)
    return conn.execute(f"INSERT INTO garagebooks.lease ({names}) VALUES ({', '.join(['%s'] * len(cols))}) RETURNING id",
                        list(cols.values())).fetchone()["id"]


def choose(conn):
    db.set_tax_choice(conn, "employee", 2026, 135001, "not_exempt")
    db.set_tax_choice(conn, "associate", 2026, 45001, "not_exempt")


def years(conn, lease_id):
    return {r["income_year"]: r for r in conn.execute(
        "SELECT * FROM garagebooks.lease_income_year() WHERE lease_id = %s ORDER BY income_year", (lease_id,))}


def test_the_worked_example(admin):
    try:
        lease_id = associate_lease(admin)
        choose(admin)
        got = years(admin, lease_id)
        assert set(got) == {2026, 2027}
        first, second = got[2026], got[2027]
        assert (first["pays"], first["payments"], first["depreciation"], first["business_income"]) == (
            12, D("12000.00"), D("10000.00"), D("2000.00"))
        assert (first["associate_tax"], first["associate_medicare"], first["small_business_offset"]) == (
            D("600.00"), D("40.00"), D("63.03"))
        assert first["associate_cost"] == D("576.97") and first["employee_saving"] == D("4680.00")
        assert first["household_cost"] == D("-4103.03")
        assert (second["depreciation"], second["associate_cost"], second["household_cost"]) == (
            D("7500.00"), D("1296.23"), D("-3383.77"))
    finally:
        admin.execute("DELETE FROM garagebooks.lease")
        admin.execute("TRUNCATE garagebooks.tax_choice")


def test_depreciation_is_on_the_car_limit_at_most_and_by_days_held(admin):
    try:
        lease_id = associate_lease(admin, cost="90000", start="2026-10-01", end="2028-09-30", term_months=24)
        choose(admin)
        got = years(admin, lease_id)
        # 2026-27's car limit follows 2025-26's ($69,674): 69,674 x 25% x 273/365, then the rest of the value
        assert got[2026]["days_held"] == 273 and got[2026]["depreciation"] == D("13028.08")
        assert got[2027]["depreciation"] == ((D("69674") - D("13028.08")) * D("0.25")).quantize(D("0.01"))
        db_value = associate_lease(admin, car_id=1, value="20000", start="2029-01-01", end="2029-12-31", term_months=12)
        assert years(admin, db_value)[2028]["depreciation"] == (D("20000") * D("0.25") * 181 / 365).quantize(D("0.01"))
    finally:
        admin.execute("DELETE FROM garagebooks.lease")
        admin.execute("TRUNCATE garagebooks.tax_choice")


def test_without_the_associates_bracket_their_figures_are_unknown_never_zero(admin):
    try:
        lease_id = associate_lease(admin)
        db.set_tax_choice(admin, "employee", 2026, 135001, "not_exempt")
        first = years(admin, lease_id)[2026]
        assert first["employee_saving"] == D("4680.00") and first["depreciation"] == D("10000.00")
        assert first["associate_cost"] is None and first["household_cost"] is None
    finally:
        admin.execute("DELETE FROM garagebooks.lease")
        admin.execute("TRUNCATE garagebooks.tax_choice")


def test_a_loss_has_no_offset_and_lowers_the_associates_tax(admin):
    try:
        lease_id = associate_lease(admin, cost="60000")                      # depreciation 15,000 > 12,000 received
        choose(admin)
        first = years(admin, lease_id)[2026]
        assert first["business_income"] == D("-3000.00") and first["small_business_offset"] == D("0.00")
        assert first["associate_tax"] == D("-900.00") and first["associate_cost"] == D("-960.00")
    finally:
        admin.execute("DELETE FROM garagebooks.lease")
        admin.execute("TRUNCATE garagebooks.tax_choice")


def test_a_novated_lease_has_no_associate_figures_and_costs_its_payments_less_the_saving(admin):
    try:
        lease_id = associate_lease(admin, associate_lease=False, financier="Example Finance")
        choose(admin)
        first = years(admin, lease_id)[2026]
        assert first["depreciation"] is None and first["associate_cost"] is None
        assert first["household_cost"] == D("12000.00") - D("4680.00")
    finally:
        admin.execute("DELETE FROM garagebooks.lease")
        admin.execute("TRUNCATE garagebooks.tax_choice")


# ---- the pages -------------------------------------------------------------------------------------------------

def test_the_lease_form_takes_the_value_for_depreciation_on_an_associate_lease(app):
    client, url, car_id = app
    page = client.get(f"/cars/{car_id}/lease/new").data.decode()
    assert 'name="depreciation_value"' in page
    form = good_form(associate_lease="on", start_date="2026-07-01", end_date="2028-06-30", term_months="24",
                     depreciation_value="38000")
    assert client.post(f"/cars/{car_id}/lease/new", data=form, headers=ORIGIN).status_code == 303
    with connect(url) as conn:
        assert conn.execute("SELECT depreciation_value FROM garagebooks.lease").fetchone()["depreciation_value"] == D("38000.00")
    bad = client.post(f"/cars/{car_id}/lease/new", data={**form, "depreciation_value": "-1", "start_date": "2030-07-01",
                                                          "end_date": "2031-06-30", "term_months": "12"}, headers=ORIGIN)
    assert bad.status_code == 400


def test_settings_ask_for_the_associates_bracket_only_with_an_associate_lease(app):
    client, url, car_id = app
    assert 'name="tax_associate_2026_bracket"' not in client.get("/settings").data.decode()
    with connect(url) as conn:
        associate_lease(conn, car_id=car_id)
    page = client.get("/settings").data.decode()
    assert 'name="tax_associate_2026_bracket"' in page and "Associate" in page
    assert client.post("/settings", data={"app_url": "", "grafana_url": "", "tax_associate_2026_bracket": "45001",
                                          "tax_associate_2026_medicare": "half_exempt"}, headers=ORIGIN).status_code == 303
    with connect(url) as conn:
        assert db.tax_choices(conn)[("associate", 2026)]["medicare"] == "half_exempt"


def test_the_fbt_report_shows_the_associates_year_briefly(app):
    client, url, car_id = app
    with connect(url) as conn:
        associate_lease(conn, car_id=car_id)
        choose(conn)
    body = client.get(f"/cars/{car_id}/fbt?year=2026").data.decode()
    box = text_of(re.search(r'id="fbt-associate".*?</section>', body, re.S).group(0))
    assert "2026–27" in box and "$10,000.00" in box and "$576.97" in box and "$4,680.00" in box
    assert "$4,103.03" in box                                          # the household saving


def test_the_restricted_role_can_read_the_lease_years(pg, admin):
    import psycopg
    with psycopg.connect(pg.app_url) as conn:                                      # one transaction, rolled back
        conn.execute("UPDATE garagebooks.lease SET depreciation_value = 1000")
        conn.execute("SELECT * FROM garagebooks.lease_income_year()").fetchall()
        assert conn.execute("SELECT count(*) FROM garagebooks.car_limit").fetchone()[0] >= 6
        conn.rollback()
    assert pg.provision().returncode == 0
