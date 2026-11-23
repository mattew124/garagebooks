"""A car's VIN, year, model year and body type: on the Cars page, filled in from the purchase invoice on its import
row, and a folder chosen for a car that doesn't exist yet (the import builds it). Works with or without TeslaMate."""

import psycopg
import pytest

from garagebooks import invoices
from tests.test_app import ORIGIN, category_id, rows
from tests.test_invoices import DEALER_CAR
from tests.test_local import app_for, choose, client, root  # noqa: F401  (fixtures)

PATH = "/Cars/Test car/purchase.pdf"
VIN = "1ABCD23EFGH456789"


@pytest.fixture()
def stocked(client, root, monkeypatch):  # noqa: F811
    (root / "Cars" / "Test car" / "purchase.pdf").write_bytes(b"%PDF-purchase")
    monkeypatch.setattr(invoices, "extract_text", lambda pdf: DEALER_CAR)
    choose(client)
    return client


def car(admin, car_id=1):
    return admin.execute("SELECT * FROM garagebooks.car WHERE id = %s", (car_id,)).fetchone()


def read(client, car_id=1, query="", folder="Initial purchase"):
    return client.post(f"/local/import/{car_id}/read{query}", headers=ORIGIN, data={
        "n": "0", "id": PATH, "url": PATH, "name": "purchase.pdf", "folder": folder}).data.decode()


def confirm(client, admin, car_id=1, query="", **over):
    data = {"count": "1", "include_0": "on", "id_0": PATH, "url_0": PATH, "name_0": "purchase.pdf",
            "date_0": "2026-03-15", "category_id_0": str(category_id(admin, "Initial purchase")), "amount_0": "52000.00",
            "vendor_0": "Example Motors Pty Ltd", "car_fill_0": "on", "car_vin_0": VIN, "car_build_year_0": "2025",
            "car_model_year_0": "2026", "car_body_type_0": "Hatchback", "car_make_0": "Example", "car_model_0": "Roadster"}
    data.update(over)
    return client.post(f"/local/import/{car_id}{query}", data=data, headers=ORIGIN)


# ---- the columns and the Cars page ----------------------------------------------------------------

def test_the_car_columns_and_their_checks(admin):
    admin.execute("UPDATE garagebooks.car SET vin = %s, build_year = 2025, model_year = 2026, body_type = 'SUV' WHERE id = 1", (VIN,))
    for column, bad in (("vin", "SHORT"), ("vin", "1ABCO23EFGH456789"), ("build_year", 1800), ("model_year", 2200), ("body_type", " ")):
        with pytest.raises(psycopg.errors.CheckViolation):
            admin.execute(f"UPDATE garagebooks.car SET {column} = %s WHERE id = 1", (bad,))


def test_the_cars_page_shows_and_saves_the_details(client, admin):  # noqa: F811
    page = client.get("/cars").data.decode()
    assert 'name="vin"' in page and 'name="build_year"' in page and 'name="model_year"' in page and 'name="body_type"' in page
    before = car(admin)              # the make and model are the suite's shared car's: posted back as they are
    data = {"kind": "BEV", "name": "Test car", "make": before["make"] or "", "model": before["model"] or "", "vin": "1abcd 23efgh456789",
            "build_year": "2025", "model_year": "2026", "body_type": " Hatchback "}
    assert client.post("/cars/1", data=data, headers=ORIGIN).status_code == 303
    saved = car(admin)
    assert (saved["vin"], saved["build_year"], saved["model_year"], saved["body_type"]) == (VIN, 2025, 2026, "Hatchback")
    assert VIN in client.get("/cars").data.decode()
    for field, bad in (("vin", "SHORT"), ("build_year", "1800"), ("model_year", "next year"), ("body_type", "x" * 50)):
        resp = client.post("/cars/1", data={**data, field: bad}, headers=ORIGIN)
        assert resp.status_code == 400 and f'name="{field}"' in resp.data.decode(), field
    data.update(vin="", build_year="", model_year="", body_type="")
    client.post("/cars/1", data=data, headers=ORIGIN)
    assert car(admin)["vin"] is None and car(admin)["build_year"] is None


# ---- the import row fills in the blanks ------------------------------------------------------------

def test_the_import_row_offers_to_fill_in_the_cars_blank_details(stocked, admin):
    row = read(stocked)
    assert 'name="car_fill_0" checked' in row and "Fill in the car&#39;s details from this invoice" in row
    for name, value in (("car_vin", VIN), ("car_build_year", "2025"), ("car_model_year", "2026"), ("car_body_type", "Hatchback")):
        assert f'name="{name}_0" value="{value}"' in row, name
    assert 'name="car_make_0"' not in row               # an existing car keeps its make and model


def test_only_blank_details_are_offered(stocked, admin):
    admin.execute("UPDATE garagebooks.car SET vin = '1ZZZZ23EFGH456789', model_year = 2020 WHERE id = 1")
    row = read(stocked)
    assert 'name="car_vin_0"' not in row and 'name="car_model_year_0"' not in row
    assert 'name="car_build_year_0" value="2025"' in row


def test_nothing_is_offered_for_a_document_that_isnt_a_purchase(stocked, monkeypatch):
    # a tyre invoice that happens to name the VIN must not offer to fill the car in
    monkeypatch.setattr(invoices, "extract_text", lambda pdf: "TAX INVOICE\nTyres\nVIN: " + VIN + "\nTyres\nTyres\nTotal $200.00\n")
    row = read(stocked, folder="Maintenance")
    assert 'name="car_fill_0"' not in row and "Tyres" in row


def test_confirming_fills_blanks_only_and_never_touches_names(stocked, admin):
    admin.execute("UPDATE garagebooks.car SET vin = '1ZZZZ23EFGH456789' WHERE id = 1")
    before = car(admin)
    assert confirm(stocked, admin).status_code == 303
    after = car(admin)
    assert after["vin"] == "1ZZZZ23EFGH456789"                           # already there: kept
    assert (after["build_year"], after["model_year"], after["body_type"]) == (2025, 2026, "Hatchback")
    assert (after["name"], after["make"], after["model"], after["kind"], after["teslamate_car_id"]) == (
        before["name"], before["make"], before["model"], before["kind"], before["teslamate_car_id"])
    assert len(rows(admin)) == 1


def test_an_unticked_row_changes_nothing(stocked, admin):
    data = {"count": "1", "include_0": "on", "id_0": PATH, "url_0": PATH, "name_0": "purchase.pdf", "date_0": "2026-03-15",
            "category_id_0": str(category_id(admin, "Initial purchase")), "amount_0": "52000.00", "car_vin_0": VIN}
    stocked.post("/local/import/1", data=data, headers=ORIGIN)
    assert car(admin)["vin"] is None and len(rows(admin)) == 1


def test_a_bad_detail_on_the_row_is_refused_before_anything_is_saved(stocked, admin):
    resp = confirm(stocked, admin, car_vin_0="NOT A VIN")
    assert resp.status_code == 400 and b"VIN" in resp.data and rows(admin) == [] and car(admin)["vin"] is None


# ---- a folder for a car that doesn't exist yet -----------------------------------------------------

def new_cars(admin):
    return admin.execute("SELECT * FROM garagebooks.car WHERE id <> 1 ORDER BY id").fetchall()


def test_the_storage_page_offers_a_folder_for_a_new_car(client):  # noqa: F811
    page = client.get("/storage").data.decode()
    assert "Folder for a new car" in page and 'href="/local/browse/0"' in page


def test_choosing_a_folder_for_a_new_car_goes_to_its_import_without_saving_anything(client, admin):  # noqa: F811
    page = client.get("/local/browse/0").data.decode()
    assert "a new car" in page and 'action="/local/folders/0"' in page
    resp = client.post("/local/folders/0", data={"folder_id": "/Cars/Test car"}, headers=ORIGIN)
    assert resp.status_code == 303 and resp.headers["Location"] == "/local/import/0?folder=/Cars/Test+car"
    assert admin.execute("SELECT count(*) AS n FROM garagebooks.car_folder").fetchone()["n"] == 0 and new_cars(admin) == []


def test_the_import_of_a_new_cars_folder_needs_the_folder(client):  # noqa: F811
    resp = client.get("/local/import/0")
    assert resp.status_code == 303 and resp.headers["Location"].endswith("/storage")
    assert client.get("/local/import/0/scan").status_code == 409


FOLDER = "?folder=%2FCars%2FTest+car"


def test_the_new_car_import_page_scans_and_reads_that_folder(stocked):
    page = stocked.get(f"/local/import/0{FOLDER}").data.decode()
    assert "a new car" in page and "/local/import/0/scan?folder=/Cars/Test+car" in page
    scan = stocked.get(f"/local/import/0/scan{FOLDER}").get_json()
    assert [f["name"] for f in scan["files"]] == ["purchase.pdf"]
    row = read(stocked, 0, FOLDER)
    assert 'name="car_make_0" value="Example"' in row and 'name="car_vin_0"' in row and 'name="car_fill_0" checked' in row


def test_the_purchase_invoice_builds_the_car(stocked, admin):
    assert confirm(stocked, admin, 0, FOLDER).status_code == 303
    [made] = new_cars(admin)
    assert (made["name"], made["make"], made["model"], made["kind"], made["teslamate_car_id"]) == (
        "Example Roadster", "Example", "Roadster", "BEV", None)
    assert (made["vin"], made["build_year"], made["model_year"], made["body_type"]) == (VIN, 2025, 2026, "Hatchback")
    [expense] = rows(admin)
    assert expense["car_id"] == made["id"]
    folder = admin.execute("SELECT * FROM garagebooks.car_folder WHERE car_id = %s", (made["id"],)).fetchone()
    assert folder["folder_id"] == "/Cars/Test car" and folder["backend"] == "local"


def test_without_a_purchase_invoice_the_car_is_made_by_hand_in_a_dialog(stocked, admin):
    other = {"category_id_0": str(category_id(admin, "Tyres")), "car_fill_0": ""}
    page = confirm(stocked, admin, 0, FOLDER, **other)
    assert page.status_code == 400 and 'data-needs-car="1"' in page.data.decode() and 'id="car-dialog"' in page.data.decode()
    assert new_cars(admin) == [] and rows(admin) == []
    assert confirm(stocked, admin, 0, FOLDER, new_car_name="  ", **other).status_code == 400
    done = confirm(stocked, admin, 0, FOLDER, new_car_name="Family ute", new_car_kind="ICE", new_car_vin=VIN.lower(),
                   new_car_build_year="2019", new_car_model_year="2020", new_car_body_type="Ute", **other)
    assert done.status_code == 303
    [made] = new_cars(admin)
    assert (made["name"], made["kind"], made["vin"], made["build_year"], made["body_type"]) == ("Family ute", "ICE", VIN, 2019, "Ute")
    assert rows(admin)[0]["car_id"] == made["id"]


def test_a_bad_dialog_field_is_refused_and_nothing_is_saved(stocked, admin):
    other = {"category_id_0": str(category_id(admin, "Tyres")), "car_fill_0": ""}
    resp = confirm(stocked, admin, 0, FOLDER, new_car_name="Ute", new_car_kind="ICE", new_car_vin="SHORT", **other)
    assert resp.status_code == 400 and b"VIN" in resp.data and new_cars(admin) == [] and rows(admin) == []


def test_nothing_ticked_for_a_new_car_is_refused(stocked, admin):
    resp = stocked.post(f"/local/import/0{FOLDER}", data={"count": "1", "url_0": PATH, "name_0": "purchase.pdf"}, headers=ORIGIN)
    assert resp.status_code == 400 and b"Tick at least one invoice" in resp.data and new_cars(admin) == []


def test_a_double_submit_makes_one_car(stocked, admin):
    confirm(stocked, admin, 0, FOLDER)
    confirm(stocked, admin, 0, FOLDER)
    assert len(rows(admin)) == 1
    assert len(new_cars(admin)) == 1
