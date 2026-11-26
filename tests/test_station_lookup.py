"""Where a fuel receipt's station is: its address is read off the receipt and looked up on OpenStreetMap.

The addresses and coordinates are invented. Nothing here reaches the network.
"""
import io
import json
import urllib.parse

import psycopg
import pytest
from psycopg.rows import dict_row

from garagebooks import create_app, geocode, invoices
from tests.test_app import ORIGIN
from tests.test_fuel import client  # noqa: F401 - fixture
from tests.test_fuel_receipt import RECEIPT

pytestmark = pytest.mark.usefixtures("admin")

# A till's heading: the company, "<store number> <suburb> PH: ...", then the street; the suburb and state
# again on the card slip.
HEADED = RECEIPT.replace("     TAX INVOICE", """   10000 Exampleton  PH: 07 0000 0000
            12 Sample Creek Road
     TAX INVOICE""").replace("  PURCHASE", "       EXAMPLETON    QLD\n  PURCHASE")


def found(road="Sample Creek Road", suburb="Exampleton", house="12"):
    address = {"road": road, "suburb": suburb, "state": "Queensland"}
    if house:
        address["house_number"] = house
    return [{"lat": "-30.1234561", "lon": "150.6543219", "address": address,
             "display_name": f"{house or ''} {road}, {suburb}, Queensland"}]


def test_the_stations_address_is_read_off_the_receipt():
    assert invoices.station_queries(HEADED)[0] == "12 Sample Creek Road, Exampleton, QLD"


def test_a_suburb_line_under_the_street_and_one_line_addresses_are_read():
    top = "SHELL\nPUMP #1 UNLEADED 91\n"
    assert invoices.station_queries(top + "12 Sample Creek Rd\nExampleton QLD 4000\nTAX INVOICE")[0] == \
        "12 Sample Creek Rd, Exampleton, QLD 4000"
    assert invoices.station_queries(top + "Cnr 12 Sample Highway, Exampleton NSW 2000\n")[0] == \
        "12 Sample Highway, Exampleton, NSW 2000"


def test_ocrs_old_for_qld_and_a_second_reading_of_the_street_are_used():
    garbled = HEADED.replace("12 Sample Creek Road", "12 Sample Creek Roac").replace("EXAMPLETON    QLD", "EXAMPLETON    OLD")
    garbled = garbled.replace("Exampleton  PH:", "Eranpleton  Pi:")
    assert invoices.station_queries(garbled) == []                       # no street to go on
    assert invoices.station_queries(garbled + "\x1eSHELL\n12 Sample Creek Road\n")[0] == "12 Sample Creek Road, Exampleton, QLD"


@pytest.mark.parametrize("text", [
    RECEIPT,                                                                       # no address
    "Example Motors\n12 Sample Creek Road\nExampleton QLD 4000\nTAX INVOICE Tyres",  # not a fuel receipt
    "SHELL\nPUMP #1 UNLEADED 91\n12 Sample Creek Road\nTAX INVOICE",                 # a street alone could be anywhere
])
def test_nothing_is_looked_up_without_a_fuel_receipts_full_address(text):
    assert invoices.station_queries(text) == []


def test_the_lookup_sends_only_the_address_and_names_only_the_app():
    sent = []

    def fetch(url, headers):
        sent.append((url, headers))
        return json.dumps(found()).encode()

    got = geocode.Lookup("1.2.3", fetch=fetch, pause=0).find(["12 Sample Creek Road, Exampleton, QLD"])
    assert (got["location"], got["exact"]) == ("-30.123456, 150.654322", True)
    url, headers = sent[0]
    assert url.startswith("https://nominatim.openstreetmap.org/search?") and headers == {"User-Agent": "garagebooks/1.2.3"}
    params = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    assert params["q"] == ["12 Sample Creek Road, Exampleton, QLD"] and params["countrycodes"] == ["au"]
    assert set(params) == {"q", "format", "limit", "addressdetails", "countrycodes"}


def test_the_next_address_is_tried_and_a_place_somewhere_else_is_refused():
    replies = [[], found(road="Other Street"), found(house=None)]
    lookup = geocode.Lookup("dev", fetch=lambda url, headers: json.dumps(replies.pop(0)).encode(), pause=0)
    got = lookup.find(["12 Sample Creek Roac, Exampleton, QLD", "12 Sample Creek Road, Exampleton, QLD",
                       "12 Sample Creek Road, Exampleton, QLD 4000"])
    assert got["exact"] is False and not replies


@pytest.mark.parametrize("reply", [OSError("offline"), b"<html>", b"{}", b'[{"lat": "x"}]', b'[{"lat": "99", "lon": "1", "address": {"road": "Sample Creek Road"}}]'])
def test_a_lookup_that_fails_finds_nothing(reply):
    def fetch(url, headers):
        if isinstance(reply, Exception):
            raise reply
        return reply

    assert geocode.Lookup("dev", fetch=fetch, pause=0).find(["12 Sample Creek Road, Exampleton, QLD"]) is None


def test_at_most_four_addresses_are_asked_about():
    asked = []
    lookup = geocode.Lookup("dev", fetch=lambda url, headers: asked.append(url) or b"[]", pause=0)
    assert lookup.find([f"{n} Sample Road, Exampleton, QLD" for n in range(9)]) is None and len(asked) == 4


def prefill(client, monkeypatch, text, reply):
    asked = []
    monkeypatch.setattr(invoices, "extract_text", lambda pdf: text)
    monkeypatch.setattr(invoices, "_ocr", lambda pdf, dpi, psm: "")
    monkeypatch.setattr(geocode, "_get", lambda url, headers: asked.append(url) or json.dumps(reply).encode())
    resp = client.post("/api/prefill", data={"invoice_file": (io.BytesIO(b"%PDF-1.4 x"), "Scan.pdf")}, headers=ORIGIN)
    return resp.get_json(), asked


def test_prefill_gives_the_location_of_the_receipts_address(client, monkeypatch):
    got, asked = prefill(client, monkeypatch, HEADED, found())
    assert got["location"] == "-30.123456, 150.654322" and len(asked) == 1
    assert "12 Sample Creek Road" in got["location_note"] and "OpenStreetMap" in got["location_note"]


def test_prefill_points_to_the_current_location_when_the_address_isnt_found(client, monkeypatch):
    got, _ = prefill(client, monkeypatch, HEADED, [])
    assert got["location"] is None and "Use my location" in got["location_note"]
    got, asked = prefill(client, monkeypatch, RECEIPT, found())
    assert got["location"] is None and "Use my location" in got["location_note"] and asked == []


def test_the_lookup_can_be_switched_off(pg, admin, monkeypatch):
    monkeypatch.setenv("ADDRESS_LOOKUP", "0")
    monkeypatch.delenv("BASE_PATH", raising=False)
    app = create_app(connect=lambda: psycopg.connect(pg.app_url, row_factory=dict_row))
    got, asked = prefill(app.test_client(), monkeypatch, HEADED, found())
    assert got["location"] is None and asked == []
