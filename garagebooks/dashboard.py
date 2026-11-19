"""The Grafana dashboard ships inside the image and is written to DASHBOARD_DIR on start.

Grafana mounts that directory (a shared volume) read-only and provisions from it, so the
dashboard is always the one that matches the running schema, with nothing to import by hand.
Two values are filled in from this installation's settings: the timezone box defaults to
APP_TZ, the "Add / edit expenses" link falls back to the app's own BASE_PATH, and the time range starts
at your earliest data (Grafana has no "all time", so the app works it out each time it starts).

It also follows TeslaMate (sources.py). The file as shipped reads TeslaMate's charging costs;
when TeslaMate is absent, or its figures are held back, those parts of the queries are left
out, so no panel fails on a table that isn't there. Absent ("not configured") is nothing to
remark on. Held back ("unavailable") puts a note at the top with the reason, and the headline
panels that need TeslaMate for that car say "unavailable" where they would otherwise be blank.
"""
import json
import re
import os
import tempfile
from pathlib import Path

FILENAME = "cost-of-ownership.json"
# /srv/garagebooks/ beside /srv/grafana/ in the image; the repository layout is the same.
SOURCE = Path(__file__).resolve().parent.parent / "grafana" / FILENAME
_FALLBACK = "'/garagebooks/'"     # the dashboard's own placeholder for the app's address


# The parts of a panel's SQL that read TeslaMate's tables sit between these two comments.
_TESLAMATE_SQL = re.compile(r"\s*/\*teslamate\*/.*?/\*end\*/", re.S)
NOTE_PANEL_ID = 900


def _without_teslamate(dash, teslamate):
    """Leave TeslaMate's tables out of every query, and say so when it is there but held back."""
    for panel in dash["panels"]:
        for target in panel.get("targets", []):
            target["rawSql"] = _TESLAMATE_SQL.sub("", target["rawSql"])
    dash["links"] = [link for link in dash.get("links", []) if link.get("type") != "dashboards"]
    if not teslamate.installed:
        return
    for panel in dash["panels"]:
        if panel["type"] == "stat" and panel["title"] != "Sold":     # the sale is ours, not TeslaMate's
            panel["fieldConfig"]["defaults"]["noValue"] = "unavailable"
        panel["gridPos"]["y"] += 3
    dash["panels"].insert(0, {
        "id": NOTE_PANEL_ID, "type": "text", "title": "TeslaMate's figures are unavailable",
        "gridPos": {"h": 3, "w": 24, "x": 0, "y": 0},
        "options": {"mode": "markdown", "content": (
            "Odometer and charging costs from TeslaMate are left out, so the totals that need them say "
            "*unavailable* for a car TeslaMate tracks. Everything entered in GarageBooks is shown as usual.\n\n"
            + teslamate.reason)},
    })


SOLD_PANEL = "Sold"


def _without_sold(dash):
    """Leave the Sold panel out, and give its place in the row to the panels beside it."""
    sold = next((p for p in dash["panels"] if p["title"] == SOLD_PANEL), None)
    if sold is None:
        return
    dash["panels"].remove(sold)
    row = sorted((p for p in dash["panels"] if p["gridPos"]["y"] == sold["gridPos"]["y"]
                  and p["gridPos"]["h"] == sold["gridPos"]["h"]), key=lambda p: p["gridPos"]["x"])
    if row and 24 % len(row) == 0:
        width = 24 // len(row)
        for i, panel in enumerate(row):
            panel["gridPos"].update(x=i * width, w=width)


AFTER_TAX_PANELS = ("Running costs after tax", "After tax by FBT year")


def _without_after_tax(dash):
    """Leave the cost-after-tax panels out: they have nothing to show until a marginal rate is entered and a car has a lease.
    They are the last row, so nothing needs to move."""
    dash["panels"] = [p for p in dash["panels"] if p["title"] not in AFTER_TAX_PANELS]


PACE_PANEL = "Kilometre pace"


def _without_pace(dash):
    """Leave the Kilometre pace panel out, and move whatever is below it up into its place."""
    pace = next((p for p in dash["panels"] if p["title"] == PACE_PANEL), None)
    if pace is None:
        return
    dash["panels"].remove(pace)
    for panel in dash["panels"]:
        if panel["gridPos"]["y"] >= pace["gridPos"]["y"] + pace["gridPos"]["h"]:
            panel["gridPos"]["y"] -= pace["gridPos"]["h"]


def render(tz, base_path, source=SOURCE, start=None, teslamate=None, sold=True, pace=True, after_tax=True):
    """`teslamate` is a sources.TeslaMate; None means it is there and usable (the file as shipped).
    `sold` is whether any car has been sold: with none, the Sold panel is left out (it would only ever
    say "not sold"), and it comes back when a sale is recorded. `pace` is whether any lease has an estimated
    km per year: with none, the Kilometre pace panel is left out, and it comes back when one is entered.
    `after_tax` is whether the cost-after-tax panels have anything to show (a marginal rate is entered and
    some car has a lease)."""
    dash = json.loads(Path(source).read_text())
    if not sold:
        _without_sold(dash)
    if not after_tax:
        _without_after_tax(dash)
    if not pace:
        _without_pace(dash)
    if teslamate is not None and not teslamate.usable:
        _without_teslamate(dash, teslamate)
    if start is not None:
        dash["time"] = {"from": start.strftime("%Y-%m-01T00:00:00.000Z"), "to": "now"}
    variables = {v["name"]: v for v in dash["templating"]["list"]}
    tzvar = variables["tz"]
    tzvar["query"] = tz
    tzvar["current"] = {"selected": False, "text": tz, "value": tz}
    tzvar["options"] = [{"selected": True, "text": tz, "value": tz}]
    link = variables["app_url"]
    here = "'" + (base_path or "").replace("'", "''") + "/'"
    for key in ("query", "definition"):
        if _FALLBACK not in link[key]:
            raise ValueError("The dashboard's app-link placeholder moved: update dashboard.py.")
        link[key] = link[key].replace(_FALLBACK, here)
    return dash


def export(directory, tz, base_path, source=SOURCE, start=None, teslamate=None, sold=True, pace=True, after_tax=True):
    """Write the dashboard if it differs from what's there. True if it wrote."""
    text = json.dumps(render(tz, base_path, source, start, teslamate, sold, pace, after_tax), indent=2) + "\n"
    target = Path(directory) / FILENAME
    if target.exists() and target.read_text() == text:
        return False
    # Write beside the target and rename, so Grafana never reads half a file.
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".dashboard-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(text)
        os.chmod(tmp, 0o644)
        os.replace(tmp, target)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return True
