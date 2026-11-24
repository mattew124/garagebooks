"""The after-tax cost of ownership on the dashboard: garagebooks.after_tax_by_income_year() (the running costs after
tax and each lease's household figures, by income year) and the row of panels that draws it, below the others.
The panels are left out until a tax bracket is chosen and a car has a lease, and their SQL runs with TeslaMate
present, absent and held back. All figures are invented (the worked example of tests/test_associate_tax.py)."""

import json
from decimal import Decimal

from garagebooks import dashboard, sources
from tests.test_associate_tax import associate_lease, choose
from tests.test_lease import connect
from tests.test_sources import category, queries, runnable, start

D = Decimal
TCO_PANELS = ("After-tax cost by income year", "Household cost after tax", "Lease after tax by income year",
              "After tax by category and FBT year")
TCO_IDS = {30, 31, 32, 33}


def shipped():
    return json.loads(dashboard.SOURCE.read_text())


def test_the_row_is_below_the_others_with_its_own_ids():
    panels = shipped()["panels"]
    mine = [p for p in panels if p["title"] in TCO_PANELS]
    assert sorted(p["title"] for p in mine) == sorted(TCO_PANELS) and {p["id"] for p in mine} == TCO_IDS
    others = [p for p in panels if p not in mine]
    bottom = max(o["gridPos"]["y"] + o["gridPos"]["h"] for o in others)
    assert all(p["gridPos"]["y"] >= bottom for p in mine)
    assert len({p["id"] for p in panels}) == len(panels)
    for p in mine:                                  # side by side panels don't overlap
        for q in mine:
            if p is not q and p["gridPos"]["y"] == q["gridPos"]["y"]:
                a, b = sorted((p, q), key=lambda r: r["gridPos"]["x"])
                assert a["gridPos"]["x"] + a["gridPos"]["w"] <= b["gridPos"]["x"]


def test_the_row_is_left_out_with_nothing_to_show():
    titles = [p["title"] for p in dashboard.render("UTC", "", after_tax=False)["panels"]]
    assert not set(TCO_PANELS) & set(titles)
    titles = [p["title"] for p in dashboard.render("UTC", "")["panels"]]
    assert set(TCO_PANELS) <= set(titles)


def by_year(conn, tz="Australia/Brisbane"):
    return {(r["car_id"], r["income_year"]): r for r in conn.execute(
        "SELECT * FROM garagebooks.after_tax_by_income_year(%s)", (tz,))}


def test_the_function_adds_the_running_costs_and_the_leases_household_figures(admin):
    try:
        associate_lease(admin)
        choose(admin)                      # you 39%; the associate 30% + 2%
        admin.execute("INSERT INTO garagebooks.expense (car_id, category_id, occurred_at, amount) VALUES "
                      "(1, %s, '2026-11-10T09:00+10', 1100)", (category(admin, "Insurance"),))
        got = by_year(admin)
        first = got[(1, 2026)]
        # 1,100 of insurance packaged: 1,000 x (1 - 0.39) = 610.00 (no post-tax share)
        assert (first["running_invoices"], first["running_after_tax"]) == (D("1100.00"), D("610.00"))
        assert (first["lease_payments"], first["employee_saving"], first["depreciation"], first["associate_cost"]) == (
            D("12000.00"), D("4680.00"), D("10000.00"), D("576.97"))
        assert first["lease_household"] == D("-4103.03") and first["household_cost"] == D("-3493.03")
        second = got[(1, 2027)]
        assert second["running_invoices"] == 0 and second["household_cost"] == D("-3383.77")
        admin.execute("DELETE FROM garagebooks.tax_choice WHERE person = 'associate'")
        assert by_year(admin)[(1, 2026)]["household_cost"] is None          # never a figure standing in for it
    finally:
        admin.execute("DELETE FROM garagebooks.lease")
        admin.execute("TRUNCATE garagebooks.tax_choice")


def test_the_panel_queries_run_with_teslamate_absent_held_back_and_present(empty_db, monkeypatch):
    start(empty_db, monkeypatch)
    with connect(empty_db) as conn:
        for form in (sources.TeslaMate(sources.ABSENT), sources.TeslaMate(sources.UNTESTED, "held back")):
            dash = dashboard.render("UTC", "", teslamate=form)
            for panel in dash["panels"]:
                if panel["title"] in TCO_PANELS:
                    for sql in (t["rawSql"] for t in panel["targets"]):
                        conn.execute(runnable(sql)).fetchall()
        conn.execute("CREATE TABLE public.charging_processes (car_id smallint, start_date timestamp, cost numeric(6, 2));"
                     "CREATE TABLE public.cars (id smallint PRIMARY KEY);")
        dash = dashboard.render("UTC", "", teslamate=None)
        for panel in dash["panels"]:
            if panel["title"] in TCO_PANELS:
                for sql in (t["rawSql"] for t in panel["targets"]):
                    conn.execute(runnable(sql)).fetchall()


def test_the_bar_chart_counts_teslamates_charging_as_paid_personally(admin):
    try:
        associate_lease(admin)
        choose(admin)
        dash = dashboard.render("UTC", "", teslamate=sources.TeslaMate(sources.ABSENT))
        panel = next(p for p in dash["panels"] if p["title"] == "After-tax cost by income year")
        rows = admin.execute(runnable(panel["targets"][0]["rawSql"]).replace("'UTC'", "'Australia/Brisbane'")).fetchall()
        assert [(r["Income year"], r["Lease (household)"]) for r in rows] == [("2026–27", D("-4103.03")), ("2027–28", D("-3383.77"))]
    finally:
        admin.execute("DELETE FROM garagebooks.lease")
        admin.execute("TRUNCATE garagebooks.tax_choice")


def test_the_fbt_report_leaves_the_breakdown_to_the_dashboard(empty_db, monkeypatch):
    from tests.test_after_tax import fill_year, set_rate
    from tests.test_fbt import add_lease, text_of
    from tests.test_lease import ORIGIN
    client = start(empty_db, monkeypatch)
    client.post("/cars", data={"name": "Own", "kind": "BEV"}, headers=ORIGIN)
    with connect(empty_db) as conn:
        car_id = conn.execute("SELECT id FROM garagebooks.car").fetchone()["id"]
    add_lease(client, car_id)
    set_rate(client)
    fill_year(empty_db, car_id)
    body = client.get(f"/cars/{car_id}/fbt?year=2025").data.decode()
    assert 'id="fbt-after-tax-lines"' not in body                          # the per-category table moved to Grafana
    shown = text_of(body)
    assert "$2,300.00" in shown and "$1,600.00" in shown and "dashboard" in shown.lower()
