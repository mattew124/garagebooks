"""The app writes its own Grafana dashboard into DASHBOARD_DIR on start, so Grafana can
provision it from a shared volume: the dashboard always matches the running version."""
import json
import logging
import os

from tests.test_dev_stack import ROOT
from garagebooks import create_app, dashboard

SOURCE = json.loads((ROOT / "grafana" / "cost-of-ownership.json").read_text())


def var(dash, name):
    return next(v for v in dash["templating"]["list"] if v["name"] == name)


def test_render_sets_timezone_and_app_link():
    dash = dashboard.render("Australia/Brisbane", "/expenses")
    tz = var(dash, "tz")
    assert tz["query"] == "Australia/Brisbane"
    assert tz["current"]["value"] == "Australia/Brisbane" and tz["current"]["text"] == "Australia/Brisbane"
    assert [o["value"] for o in tz["options"]] == ["Australia/Brisbane"]
    q = var(dash, "app_url")["query"]
    assert "'/expenses/'" in q and "garagebooks.setting" in q
    assert var(dash, "app_url")["definition"] == q
    # nothing else changes
    assert dash["uid"] == SOURCE["uid"] and dash["panels"] == SOURCE["panels"]


def test_render_handles_no_base_path_and_quotes():
    assert "'/'" in var(dashboard.render("UTC", ""), "app_url")["query"]
    q = var(dashboard.render("UTC", "/it's"), "app_url")["query"]
    assert "'/it''s/'" in q                      # quote doubled, so the SQL stays valid


def test_export_writes_atomically_and_only_when_changed(tmp_path):
    assert dashboard.export(tmp_path, "UTC", "/expenses") is True
    target = tmp_path / dashboard.FILENAME
    assert json.loads(target.read_text())["uid"] == SOURCE["uid"]
    assert [p.name for p in tmp_path.iterdir()] == [dashboard.FILENAME]   # no temp file left
    before = target.stat().st_mtime_ns
    assert dashboard.export(tmp_path, "UTC", "/expenses") is False         # unchanged: not rewritten
    assert target.stat().st_mtime_ns == before
    assert dashboard.export(tmp_path, "Australia/Brisbane", "/expenses") is True


def test_app_exports_on_start_when_asked(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_TZ", "Australia/Brisbane")
    monkeypatch.setenv("BASE_PATH", "/expenses")
    monkeypatch.setenv("DASHBOARD_DIR", str(tmp_path))
    create_app(connect=lambda: None)
    dash = json.loads((tmp_path / dashboard.FILENAME).read_text())
    assert var(dash, "tz")["query"] == "Australia/Brisbane"


def test_app_does_nothing_without_dashboard_dir(tmp_path, monkeypatch):
    monkeypatch.delenv("DASHBOARD_DIR", raising=False)
    monkeypatch.chdir(tmp_path)
    create_app(connect=lambda: None)
    assert list(tmp_path.iterdir()) == []


def test_unwritable_dashboard_dir_warns_but_the_app_starts(tmp_path, monkeypatch, caplog):
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o500)
    monkeypatch.setenv("DASHBOARD_DIR", str(locked))
    try:
        if os.access(locked, os.W_OK):
            return                                   # running as root: can't make a dir unwritable
        caplog.set_level(logging.WARNING, logger="garagebooks")
        create_app(connect=lambda: None)
        assert any("dashboard" in m.lower() for m in caplog.messages)
    finally:
        locked.chmod(0o700)


def test_render_starts_the_time_range_at_the_earliest_data():
    from datetime import datetime, timezone
    dash = dashboard.render("UTC", "", start=datetime(2023, 11, 14, 9, 30, tzinfo=timezone.utc))
    assert dash["time"] == {"from": "2023-11-01T00:00:00.000Z", "to": "now"}
    assert dashboard.render("UTC", "")["time"] == SOURCE["time"]


def test_period_variable_drives_the_bucketed_charts():
    assert [o["value"] for o in var(SOURCE, "period")["options"]] == ["week", "month", "year"]
    for p in SOURCE["panels"]:
        if p["title"].startswith(("Running costs per", "Cumulative")) and "FBT year" not in p["title"]:   # that one is always per FBT year
            assert "date_trunc('$period'" in p["targets"][0]["rawSql"]


def _lab(colour):
    """#rrggbb -> CIELAB (D65), where distance tracks how different two colours look."""
    def linear(v):
        v /= 255
        return v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4
    r, g, b = (linear(int(colour[i:i + 2], 16)) for i in (1, 3, 5))
    xyz = ((0.4124 * r + 0.3576 * g + 0.1805 * b) / 0.95047, 0.2126 * r + 0.7152 * g + 0.0722 * b,
           (0.0193 * r + 0.1192 * g + 0.9505 * b) / 1.08883)
    f = [t ** (1 / 3) if t > 0.008856 else 7.787 * t + 16 / 116 for t in xyz]
    return 116 * f[1] - 16, 500 * (f[0] - f[1]), 200 * (f[1] - f[2])


def test_category_colours_are_fixed_and_clearly_different():
    """Grafana's default palette hands out neighbouring shades; the starter categories and charging get
    fixed, well-separated colours (the colour-blind-safe Okabe-Ito set), the same in every chart."""
    seed = (ROOT / "garagebooks" / "schema.sql").read_text()
    running = [n for n in ("Insurance", "Registration", "Tyres", "Cabin air filter", "Air filter",
                           "Wheel alignment", "Tyre rotation") if f"('{n}'" in seed]
    assert len(running) == 7
    panels = {p["id"]: p for p in SOURCE["panels"]}
    palettes = []
    for pid in (11, 12):
        found = {}
        for o in panels[pid]["fieldConfig"]["overrides"]:
            colour = next(p["value"] for p in o["properties"] if p["id"] == "color")
            assert colour["mode"] == "fixed"
            found[o["matcher"]["options"]] = colour["fixedColor"]
        assert set(found) == {"Charging", *running}
        palettes.append(found)
    assert palettes[0] == palettes[1]
    colours = list(palettes[0].values())
    for i, a in enumerate(colours):
        for b in colours[i + 1:]:
            gap = sum((x - y) ** 2 for x, y in zip(_lab(a), _lab(b), strict=True)) ** 0.5
            assert gap > 30, f"{a} and {b} are too alike"


def test_the_invoice_column_links_a_stored_invoice_to_the_app():
    """An invoice kept in the folder on the server has no link of its own: the app serves it at
    /invoice/<id>, under the address the dashboard already knows the app by."""
    sql = next(t["rawSql"] for p in SOURCE["panels"] for t in p.get("targets", []) if '"Invoice"' in t.get("rawSql", ""))
    assert "e.invoice_path IS NOT NULL" in sql and "${app_url:sqlstring}" in sql
    assert "'/invoice/' || e.id" in sql and "ELSE e.invoice_url END" in sql


# ---- the Sold panel is there only once a car has been sold ------------------------------------

def titles(dash):
    return [p["title"] for p in dash["panels"]]


def test_the_sold_panel_is_left_out_until_a_car_is_sold_and_its_row_is_shared_by_the_rest():
    dash = dashboard.render("UTC", "", sold=False)
    assert "Sold" not in titles(dash) and "Sold" in titles(dashboard.render("UTC", ""))
    assert "Sold" in titles(dashboard.render("UTC", "", sold=True))
    sold = next(p for p in SOURCE["panels"] if p["title"] == "Sold")
    row = [p for p in dash["panels"] if p["gridPos"]["y"] == sold["gridPos"]["y"] and p["gridPos"]["h"] == sold["gridPos"]["h"]]
    assert len(row) == 3 and [p["gridPos"]["w"] for p in row] == [8, 8, 8]
    assert sorted(p["gridPos"]["x"] for p in row) == [0, 8, 16]
    assert len(dash["panels"]) == len(SOURCE["panels"]) - 1
    ids = [p["id"] for p in dash["panels"]]
    assert len(ids) == len(set(ids))
    other = [p for p in dash["panels"] if p not in row]       # every other panel is as shipped
    assert all(p in SOURCE["panels"] for p in other)


def test_export_writes_the_dashboard_with_or_without_the_sold_panel(tmp_path):
    assert dashboard.export(tmp_path, "UTC", "", sold=False) is True
    target = tmp_path / dashboard.FILENAME
    assert "Sold" not in titles(json.loads(target.read_text()))
    assert dashboard.export(tmp_path, "UTC", "", sold=False) is False       # unchanged: not rewritten
    assert dashboard.export(tmp_path, "UTC", "", sold=True) is True         # a sale brings the panel back
    assert "Sold" in titles(json.loads(target.read_text()))


def test_the_app_writes_the_panel_after_a_sale_and_drops_it_again_when_the_sale_is_cleared(empty_db, monkeypatch, tmp_path):
    from tests.test_sources import ORIGIN, connect, start
    client = start(empty_db, monkeypatch, DASHBOARD_DIR=str(tmp_path))
    target = tmp_path / dashboard.FILENAME
    assert "Sold" not in titles(json.loads(target.read_text()))              # nothing sold at start
    client.post("/cars", data={"name": "Ute", "kind": "ICE"}, headers=ORIGIN)
    with connect(empty_db) as conn:
        car_id = conn.execute("SELECT id FROM garagebooks.car").fetchone()["id"]
    sale = {"date": "2025-03-01", "price": "1000", "km": "5000"}
    assert client.post(f"/cars/{car_id}/sale", data=sale, headers=ORIGIN).status_code == 303
    assert "Sold" in titles(json.loads(target.read_text()))
    assert client.post(f"/cars/{car_id}/sale", data={"action": "clear"}, headers=ORIGIN).status_code == 303
    assert "Sold" not in titles(json.loads(target.read_text()))
    sale_again = {**sale, "price": "1200"}
    client.post(f"/cars/{car_id}/sale", data=sale_again, headers=ORIGIN)
    start(empty_db, monkeypatch, DASHBOARD_DIR=str(tmp_path))                # a restart keeps it, as a car is sold
    assert "Sold" in titles(json.loads(target.read_text()))


def overlapping(panels):
    """Pairs of panels whose grid rectangles share any cell."""
    found = []
    for i, a in enumerate(panels):
        for b in panels[i + 1:]:
            ga, gb = a["gridPos"], b["gridPos"]
            if (ga["x"] < gb["x"] + gb["w"] and gb["x"] < ga["x"] + ga["w"]
                    and ga["y"] < gb["y"] + gb["h"] and gb["y"] < ga["y"] + ga["h"]):
                found.append((a["title"], b["title"]))
    return found


def test_panel_ids_are_unique_and_no_two_panels_overlap():
    """Branches that each add a row at the end of the file can land on the same rows; this catches it."""
    ids = [p["id"] for p in SOURCE["panels"]]
    assert len(ids) == len(set(ids)), "a panel id is used twice"
    assert overlapping(SOURCE["panels"]) == []
    for sold in (True, False):
        for pace in (True, False):
            for after_tax in (True, False):
                dash = dashboard.render("UTC", "", sold=sold, pace=pace, after_tax=after_tax)
                assert overlapping(dash["panels"]) == [], (sold, pace, after_tax)
                titles = [p["title"] for p in dash["panels"]]
                assert (dashboard.PACE_PANEL in titles) is pace
                assert (dashboard.AFTER_TAX_PANELS[0] in titles) is after_tax
