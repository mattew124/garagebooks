"""A service station's receipt on the Add form, the optional price per litre, and where a fill-up was made.

The receipt texts are invented, laid out like a real thermal receipt and like what OCR makes of a scan of one
(an L read as a 1, a decimal point as a comma, a column of garble).
"""
import io
import json
from datetime import date
from decimal import Decimal

import pytest

from garagebooks import dashboard, invoices
from tests.test_app import ORIGIN, category_id
from tests.test_fuel import client, fill, post, preview, ute  # noqa: F401 - fixtures
from tests.test_sources import runnable

pytestmark = pytest.mark.usefixtures("admin")

CATS = ["Fuel", "Service", "Tyres", "Oil filter"]

RECEIPT = """\
                  SHELL
        Example FuelCo Pty Ltd
     TAX INVOICE - ABN 11 111 111 111
                                          $
 #PREMIUM 95          PUMP #1
   40.00L    @   $2.000/L             80.00
 Member Bonus                         -1.00
 Fuel eVoucher                        -2.00
    2 SUBTOTAL                       $77.00
      TOTAL                          $77.00
  PURCHASE          $77.00
  05/03/26 07:45    000001
 TOTAL Includes GST                   $7.00
      TOTAL SAVINGS
         $3.00
 Your next Fuel Savings offer will expire on 14/03/2026.
 STORE 10000 POS 1 TRANS 1000 07:45 05/03/2026
"""

# As OCR reads a scan of it badly: nothing usable on the pump's line.
GARBLED = RECEIPT.replace("40.00L    @   $2.000/L             80.00", "4.008 $2,000 |")


def read(text, extra=""):
    return invoices.propose("", "Scan.pdf", text, CATS, fuel_category="Fuel", fuel_text=extra)


def test_a_fuel_receipt_fills_the_whole_expense():
    got = read(RECEIPT)
    assert (got["category"], got["vendor"], got["amount"]) == ("Fuel", "Shell", Decimal("77.00"))
    assert (got["date"], got["time"]) == (date(2026, 3, 5), "07:45")
    assert (got["litres"], got["unit_price"]) == (Decimal("40.00"), Decimal("2.000"))


@pytest.mark.parametrize("line", [
    "40,00L @  $2.000A                   80.00",       # a comma, and /L read as A
    "40,001 @ «$2. 0004 80.00 p",                      # L read as 1, a space in the price
    "40.00L @ = $2.0B0/ £0.00\n 83.20",                # B for 8, the pump's figure garbled but read elsewhere
    "40.00 L  @  200.0 c/L   80.00",                   # cents per litre
])
def test_ocr_slips_in_the_pump_line_are_read_through(line):
    got = invoices.fuel_figures("PUMP #1\n" + line)
    assert got and got[0] == Decimal("40.00")


@pytest.mark.parametrize("line", ["44.00L @ $2.000/L   80.00", "40.00L @ $2.600/L   80.00", "4.008 $2,000 |"])
def test_figures_that_do_not_multiply_out_are_not_guessed(line):
    assert invoices.fuel_figures("PUMP #1\n" + line + "\nTOTAL $77.00") is None


def test_a_second_reading_supplies_the_pump_line_and_nothing_else():
    got = read(GARBLED, extra="40,001 @ $2.000/t 80.00\nTOTAL $177.00")
    assert (got["litres"], got["unit_price"], got["amount"]) == (Decimal("40.00"), Decimal("2.000"), Decimal("77.00"))
    assert read(GARBLED)["litres"] is None and read(GARBLED)["category"] == "Fuel"


def test_oil_by_the_litre_on_a_service_invoice_is_not_a_fill_up():
    got = read("Example Motors Pty Ltd\nTAX INVOICE\nService\n Engine oil 5.00L @ $20.000/L   100.00\n TOTAL $300.00")
    assert got["category"] != "Fuel" and got["litres"] is None and got["time"] is None


def test_a_scan_is_read_again_only_while_the_pump_line_is_unread(monkeypatch):
    calls = []

    def ocr(pdf, dpi, psm):
        calls.append((dpi, psm))
        return "40.00L @ $2.000/L 80.00" if len(calls) == 2 else "nothing"

    monkeypatch.setattr(invoices, "_ocr", ocr)
    assert "40.00L" in invoices.reread_fuel(b"pdf", GARBLED) and len(calls) == 2
    calls.clear()
    assert invoices.reread_fuel(b"pdf", RECEIPT) == "" and invoices.reread_fuel(b"pdf", "Tyres\nTOTAL $5.00") == ""
    assert calls == []


def test_prefill_gives_the_form_the_receipts_litres_price_and_time(client, admin, monkeypatch):
    monkeypatch.setattr(invoices, "extract_text", lambda pdf: RECEIPT)
    resp = client.post("/api/prefill", data={"invoice_file": (io.BytesIO(b"%PDF-1.4 x"), "Scan.pdf")}, headers=ORIGIN)
    got = resp.get_json()
    assert (got["litres"], got["unit_price"], got["time"], got["date"]) == ("40.00", "2.000", "07:45", "2026-03-05")
    assert got["category_id"] == category_id(admin, "Fuel") and got["amount"] == "77.00"


def test_the_litres_and_price_come_before_the_odometer_and_the_figures_after_it(client, admin):
    page = client.get("/new").data.decode()
    at = [page.index(s) for s in ('name="litres"', 'name="unit_price"', 'name="odometer_km"', 'id="fuel-summary"',
                                  'name="location"')]
    assert at == sorted(at)


def stored(admin):
    return admin.execute("SELECT litres, unit_price, latitude, longitude FROM garagebooks.expense "
                         "ORDER BY id DESC LIMIT 1").fetchone()


def test_price_per_litre_is_optional_and_the_pump_price_is_what_shows(client, admin, ute):
    assert post(admin, client, unit_price="$2.100/L").status_code == 303
    assert (stored(admin)["litres"], stored(admin)["unit_price"]) == (Decimal("40.00"), Decimal("2.100"))
    shown = admin.execute("SELECT price_per_litre FROM garagebooks.fuel_fill").fetchone()["price_per_litre"]
    assert shown == Decimal("2.1")                 # what the pump said, not 80/40 after discounts


def test_litres_are_worked_out_from_the_amount_and_the_price(client, admin, ute):
    assert post(admin, client, litres="", unit_price="2", partial="1").status_code == 303
    assert stored(admin)["litres"] == Decimal("40.00")
    assert admin.execute("SELECT full_tank FROM garagebooks.expense").fetchone()["full_tank"] is False
    got = preview(client, admin, litres="", unit_price="2", amount="80").get_json()
    assert any("40.00 L" in line for line in got["lines"])


@pytest.mark.parametrize("bad", ["0", "-1.5", "abc"])
def test_a_price_per_litre_must_be_a_positive_number(client, admin, ute, bad):
    resp = post(admin, client, unit_price=bad)
    assert resp.status_code == 400 and "per litre" in resp.data.decode()


def test_the_location_is_kept_shown_again_on_edit_and_can_be_cleared(client, admin, ute):
    assert post(admin, client, location="-30.123456, 150.654321").status_code == 303
    assert (stored(admin)["latitude"], stored(admin)["longitude"]) == (Decimal("-30.123456"), Decimal("150.654321"))
    eid = admin.execute("SELECT id FROM garagebooks.expense").fetchone()["id"]
    page = client.get(f"/{eid}/edit").data.decode()
    assert 'value="-30.123456, 150.654321"' in page
    form = {"car_id": "2", "category_id": category_id(admin, "Fuel"), "date": "2026-10-05", "time": "09:00",
            "amount": "80.00", "litres": "40", "location": ""}
    assert client.post(f"/{eid}/edit", data=form, headers=ORIGIN).status_code == 303
    assert stored(admin)["latitude"] is None and stored(admin)["longitude"] is None


@pytest.mark.parametrize("bad", ["-30.1", "91, 10", "10, 181", "here", "nan, 1"])
def test_a_location_must_be_latitude_and_longitude(client, admin, ute, bad):
    resp = post(admin, client, location=bad)
    assert resp.status_code == 400 and "latitude" in resp.data.decode()


def test_the_fuel_dashboard_has_a_heat_map_of_where_fuel_was_bought(admin, ute):
    panels = json.loads((dashboard.SOURCE.parent / "fuel.json").read_text())["panels"]
    heat = [p for p in panels if p["type"] == "geomap"]
    assert len(heat) == 1 and heat[0]["options"]["layers"][0]["type"] == "heatmap"
    eid = fill(admin, 1, 10000, 40)
    fill(admin, 8, 10500, 30)                       # no location: not on the map
    admin.execute("UPDATE garagebooks.expense SET latitude = -30.5, longitude = 150.5 WHERE id = %s", (eid,))
    rows = admin.execute(runnable(heat[0]["targets"][0]["rawSql"].replace("$car_id", "2"))).fetchall()
    assert [(float(r["latitude"]), float(r["longitude"]), float(r["litres"])) for r in rows] == [(-30.5, 150.5, 40.0)]
