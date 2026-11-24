"""Importing an aCar CSV export: upload, preview, confirm."""

import html as htmllib
import io
import re
from pathlib import Path

import pytest

from tests.test_app import ORIGIN, category_id, client, rows  # noqa: F401  (fixtures)

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def engine_car(admin):
    """The test car is electric, which has no Fuel: these tests import fill-ups, so it has an engine too."""
    admin.execute("UPDATE garagebooks.car SET kind = 'PHEV' WHERE id = 1")
    yield
    admin.execute("UPDATE garagebooks.car SET kind = 'BEV' WHERE id = 1")


def upload(client, name="acar-records.csv", **over):
    data = {"car_id": "1", "fill_ups": "1", "file": (io.BytesIO((FIXTURES / name).read_bytes()), name), **over}
    return client.post("/import/acar", data=data, headers=ORIGIN, content_type="multipart/form-data")


def preview_fields(html):
    """The preview's inputs as the browser would send them back."""
    form = html[html.index('<form method="post" action="/import/acar/confirm"'):]
    data = {}
    for tag in re.findall(r"<input[^>]*>", form):
        name = re.search(r'name="([^"]+)"', tag)
        value = re.search(r'value="([^"]*)"', tag)
        if name and 'type="checkbox"' not in tag:
            data[name.group(1)] = value.group(1) if value else ""
    for select in re.finditer(r'<select name="([^"]+)">(.*?)</select>', form, re.S):
        chosen = re.search(r'<option value="(\d*)" selected', select.group(2))
        data[select.group(1)] = chosen.group(1) if chosen else ""
    return data


def tick_all(data):
    for key in list(data):
        if key.startswith("date_"):
            data["include_" + key.split("_", 1)[1]] = "on"
    return data


def test_the_upload_page_asks_for_a_file_and_a_car(client):
    html = client.get("/import/acar").data.decode()
    assert 'name="file"' in html and 'name="car_id"' in html and "Test car" in html


def test_the_cars_page_links_to_it(client):
    assert 'href="/import/acar"' in client.get("/cars").data.decode()


def test_the_preview_lists_every_cost_oldest_first_and_saves_nothing(client, admin):
    resp = upload(client)
    assert resp.status_code == 200
    html = resp.data.decode()
    dates = re.findall(r'name="date_\d+" value="([\d-]+)"', html)
    assert dates == sorted(dates) and len(dates) == 10
    assert "Example Tyres Pty Ltd" in html and "1377.00" in html and "-311.67" in html
    assert rows(admin) == []


def test_a_row_with_a_category_is_ticked_and_one_without_is_not(client):
    html = upload(client).data.decode()
    tolls = re.search(r'<tr[^>]*>(?:(?!</tr>).)*Toll(?:(?!</tr>).)*</tr>', html, re.S).group(0)
    insurance = re.search(r'<tr[^>]*>(?:(?!</tr>).)*aCar expense: Insurance(?:(?!</tr>).)*</tr>', html, re.S).group(0)
    assert "checked" not in re.search(r'<input type="checkbox" name="include_\d+"[^>]*>', tolls).group(0)
    assert "checked" in re.search(r'<input type="checkbox" name="include_\d+"[^>]*>', insurance).group(0)


def test_what_was_left_out_is_said(client):
    html = upload(client).data.decode()
    assert "1 trip" in html and "sale" not in html.split("Left out:")[1].split("</p>")[0]


def test_confirming_saves_the_ticked_rows_in_the_chosen_car_in_local_time(client, admin):
    data = tick_all(preview_fields(upload(client).data.decode()))
    for key, value in list(data.items()):          # the Toll has no category: give it one
        if key.startswith("category_id_") and not value:
            data[key] = str(category_id(admin, "Registration"))
    resp = client.post("/import/acar/confirm", data=data, headers=ORIGIN)
    assert resp.status_code == 303 and "from=acar" in resp.headers["Location"]
    saved = rows(admin)
    assert len(saved) == 9 and {r["car_id"] for r in saved} == {1}     # the sale is the car's, not an expense
    (tyres,) = [r for r in saved if str(r["amount"]) == "1377.00"]
    assert tyres["category_id"] == category_id(admin, "Tyres") and tyres["vendor"] == "Example Tyres Pty Ltd"
    assert tyres["occurred_at"].strftime("%Y-%m-%d %H:%M") == "2026-03-30 02:35"     # 12:35 in Brisbane, as UTC
    assert tyres["odometer_km"] == 19490 and "aCar service" in tyres["notes"]
    assert any(str(r["amount"]) == "-311.67" for r in saved)


def test_untouched_rows_are_not_saved(client, admin):
    data = preview_fields(upload(client).data.decode())      # nothing ticked by the browser
    resp = client.post("/import/acar/confirm", data=data, headers=ORIGIN)
    assert resp.status_code == 400 and "Tick at least one" in resp.data.decode()
    assert rows(admin) == []


def test_a_row_that_is_ticked_but_has_no_category_is_refused_with_the_page_kept(client, admin):
    data = tick_all(preview_fields(upload(client).data.decode()))
    resp = client.post("/import/acar/confirm", data=data, headers=ORIGIN)
    assert resp.status_code == 400 and "Choose a category" in resp.data.decode()
    assert rows(admin) == []


def test_importing_twice_flags_the_second_time_and_a_double_submit_saves_once(client, admin):
    data = tick_all(preview_fields(upload(client).data.decode()))
    for key, value in list(data.items()):
        if key.startswith("category_id_") and not value:
            data[key] = str(category_id(admin, "Registration"))
    client.post("/import/acar/confirm", data=data, headers=ORIGIN)
    client.post("/import/acar/confirm", data=data, headers=ORIGIN)       # a double click
    assert len(rows(admin)) == 9
    second = upload(client).data.decode()
    assert second.count("Possible duplicate") + second.count("Add the odometer reading to") >= 6
    ticked = [t for t in re.findall(r'<input type="checkbox" name="include_\d+"[^>]*>', second) if "checked" in t]
    assert ticked == []


def test_leaving_fill_ups_out(client):
    html = upload(client, fill_ups="").data.decode()
    assert len(re.findall(r'name="date_\d+"', html)) == 8


def test_several_vehicles_ask_which_one_before_anything_else(client):
    resp = upload(client, "acar-two-vehicles.csv")
    html = resp.data.decode()
    assert resp.status_code == 200 and "Which vehicle" in html and "Old Example" in html and 'name="date_0"' not in html
    again = client.post("/import/acar", data={"car_id": "1", "fill_ups": "1", "vehicle": "Example",
                                              "csv_text": re.search(r'name="csv_text"[^>]*>(.*?)</textarea>', html, re.S).group(1)
                                              .replace("&#34;", '"').replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")},
                        headers=ORIGIN)
    assert again.status_code == 200 and 'name="date_0"' in again.data.decode()


def test_a_file_that_is_not_an_export_says_so(client):
    resp = client.post("/import/acar", data={"car_id": "1", "file": (io.BytesIO(b"a,b\n1,2\n"), "x.csv")},
                       headers=ORIGIN, content_type="multipart/form-data")
    assert resp.status_code == 400 and "doesn't look like an aCar export" in htmllib.unescape(resp.data.decode())


def test_no_file_and_no_car_are_refused(client):
    assert client.post("/import/acar", data={"car_id": "1"}, headers=ORIGIN).status_code == 400
    bad = upload(client, car_id="999")
    assert bad.status_code == 400 and "Choose a car" in bad.data.decode()


def confirmed(client, admin, **over):
    data = tick_all(preview_fields(upload(client).data.decode()))
    for key, value in list(data.items()):
        if key.startswith("category_id_") and not value:
            data[key] = str(category_id(admin, "Registration"))
    data.update(over)
    return client.post("/import/acar/confirm", data=data, headers=ORIGIN)


def test_the_sale_is_recorded_on_the_car(client, admin):
    assert confirmed(client, admin).status_code == 303
    car = admin.execute("SELECT sold_at, sale_price FROM garagebooks.car WHERE id = 1").fetchone()
    assert str(car["sale_price"]) == "2500.00" and car["sold_at"].astimezone().strftime("%Y-%m-%d") in ("2026-06-01", "2026-05-31")
    assert admin.execute("SELECT km FROM garagebooks.odometer_reading WHERE car_id = 1").fetchone()["km"] == 20500


def test_a_car_already_sold_leaves_the_sale_unticked(client, admin):
    admin.execute("UPDATE garagebooks.car SET sold_at = '2026-01-01 00:00+00', sale_price = 1 WHERE id = 1")
    html = upload(client).data.decode()
    assert "already has a sale" in html


def test_notes_and_free_fill_ups_import_at_zero(client, admin):
    confirmed(client, admin)
    assert len([r for r in rows(admin) if str(r["amount"]) == "0.00"]) == 2


def seed_invoice(admin, odometer=None):
    return admin.execute(
        "INSERT INTO garagebooks.expense (car_id, category_id, occurred_at, amount, odometer_km, vendor, invoice_url) "
        "VALUES (1, %s, '2026-02-19 04:10+00', 429.30, %s, 'Example Garage', 'https://1drv.ms/b/service') RETURNING id",
        (category_id(admin, "Tyres"), odometer)).fetchone()["id"]


def test_an_existing_expense_without_an_odometer_is_offered_the_reading_and_merged_by_default(client, admin):
    existing = seed_invoice(admin)
    html = upload(client).data.decode()
    assert f'<option value="{existing}" selected>Add the odometer' in html
    confirmed(client, admin)
    assert len(rows(admin)) == 9
    row = admin.execute("SELECT * FROM garagebooks.expense WHERE id = %s", (existing,)).fetchone()
    assert row["odometer_km"] == 19281 and row["invoice_url"] == "https://1drv.ms/b/service"
    assert row["category_id"] == category_id(admin, "Tyres") and str(row["amount"]) == "429.30"
    assert len([r for r in rows(admin) if str(r["amount"]) == "429.30"]) == 1       # merged, not added


def test_a_merge_never_overwrites_an_odometer_or_vendor_already_there(client, admin):
    existing = seed_invoice(admin, odometer=19000)
    html = upload(client).data.decode()
    assert f'<option value="{existing}" selected>Add the odometer' not in html        # nothing to add: only flagged
    data = tick_all(preview_fields(html))
    key = next(k for k, v in preview_fields(html).items() if k.startswith("amount_") and v == "429.30").replace("amount_", "action_")
    data[key] = str(existing)
    for k, v in list(data.items()):
        if k.startswith("category_id_") and not v:
            data[k] = str(category_id(admin, "Registration"))
    client.post("/import/acar/confirm", data=data, headers=ORIGIN)
    row = admin.execute("SELECT * FROM garagebooks.expense WHERE id = %s", (existing,)).fetchone()
    assert row["odometer_km"] == 19000


def test_choosing_add_as_new_keeps_both(client, admin):
    existing = seed_invoice(admin)
    data = preview_fields(upload(client).data.decode())
    key = next(k for k, v in data.items() if k.startswith("action_") and v == str(existing))
    data = tick_all(data)
    data[key] = "new"
    for k, v in list(data.items()):
        if k.startswith("category_id_") and not v:
            data[k] = str(category_id(admin, "Registration"))
    client.post("/import/acar/confirm", data=data, headers=ORIGIN)
    assert len([r for r in rows(admin) if str(r["amount"]) == "429.30"]) == 2
    assert admin.execute("SELECT odometer_km FROM garagebooks.expense WHERE id = %s", (existing,)).fetchone()["odometer_km"] is None


def test_a_merge_into_another_cars_expense_is_refused(client, admin):
    admin.execute("INSERT INTO garagebooks.car (id, name) VALUES (2, 'Other car')")
    try:
        other = admin.execute("INSERT INTO garagebooks.expense (car_id, category_id, occurred_at, amount) VALUES (2, %s, now(), 1) RETURNING id",
                              (category_id(admin, "Tyres"),)).fetchone()["id"]
        data = preview_fields(upload(client).data.decode())
        data["action_0"] = str(other)
        data["include_0"] = "on"
        resp = client.post("/import/acar/confirm", data=data, headers=ORIGIN)
        assert resp.status_code == 400
        assert admin.execute("SELECT odometer_km FROM garagebooks.expense WHERE id = %s", (other,)).fetchone()["odometer_km"] is None
    finally:
        admin.execute("DELETE FROM garagebooks.expense WHERE car_id = 2")
        admin.execute("DELETE FROM garagebooks.car WHERE id = 2")


def test_an_electric_car_is_not_offered_fuel_and_a_forced_fuel_row_is_refused(client, admin):
    admin.execute("UPDATE garagebooks.car SET kind = 'BEV' WHERE id = 1")
    html = upload(client).data.decode()
    assert "<option value=\"%s\"" % category_id(admin, "Fuel") not in html
    fill_up = re.search(r'<tr>(?:(?!</tr>).)*aCar fill-up(?:(?!</tr>).)*</tr>', html, re.S).group(0)
    assert re.search(r'name="category_id_\d+">\s*<option value="" selected', fill_up) or 'selected' not in fill_up
    data = tick_all(preview_fields(html))
    key = next(k for k in data if k.startswith("category_id_"))
    data[key] = str(category_id(admin, "Fuel"))
    resp = client.post("/import/acar/confirm", data=data, headers=ORIGIN)
    assert resp.status_code == 400 and "does not apply to this kind of car" in resp.data.decode()
    assert rows(admin) == []
