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
    admin.execute("UPDATE garagebooks.car SET kind = 'BEV', sold_at = NULL, sale_price = NULL WHERE id = 1")


def upload(client, name="acar-records.csv", **over):
    data = {"car_id": "1", "fill_ups": "1", "file": (io.BytesIO((FIXTURES / name).read_bytes()), name), **over}
    return client.post("/import/acar", data=data, headers=ORIGIN, content_type="multipart/form-data")


def preview_fields(html):
    """The preview's inputs as the browser would send them back."""
    card = html.find('id="new-car"')       # the new car's inputs sit above the form and point at it
    form = html[card if card >= 0 else html.index('<form method="post" action="/import/acar/confirm"'):]
    data = {}
    for tag in re.findall(r"<input[^>]*>", form):
        name = re.search(r'name="([^"]+)"', tag)
        value = re.search(r'value="([^"]*)"', tag)
        if name and 'type="checkbox"' not in tag:
            data[name.group(1)] = value.group(1) if value else ""
    for select in re.finditer(r'<select name="([^"]+)"[^>]*>(.*?)</select>', form, re.S):
        chosen = re.search(r'<option value="([^"]*)"[^>]*?\sselected', select.group(2))
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


def test_the_file_box_takes_a_dropped_csv_like_the_add_pages_invoice_box(client):
    html = client.get("/import/acar").data.decode()
    assert 'class="drop" id="acar-drop" data-drop-ext=".csv"' in html
    assert re.search(r'<small class="drop-hint" id="acar-drop-hint" hidden>You can also drop the CSV file onto this box\.</small>', html)
    assert "or-pick" not in html and "Choose from" not in html          # no storage to choose from here


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
    data["category_id_1"] = ""
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
    assert f'<option value="{category_id(admin, "Fuel")}"' not in html
    fill_up = re.search(r'<tr[^>]*>(?:(?!</tr>).)*aCar fill-up(?:(?!</tr>).)*</tr>', html, re.S).group(0)
    assert re.search(r'<option value="new:catchall"[^>]*\sselected', fill_up)       # it still comes in, under the catch-all
    data = tick_all(preview_fields(html))
    key = next(k for k in data if k.startswith("category_id_"))
    data[key] = str(category_id(admin, "Fuel"))
    resp = client.post("/import/acar/confirm", data=data, headers=ORIGIN)
    assert resp.status_code == 400 and "does not apply to this kind of car" in resp.data.decode()
    assert rows(admin) == []


# ---- making the car from the export --------------------------------------------------

def new_cars(admin):
    return admin.execute("SELECT * FROM garagebooks.car WHERE id <> 1 ORDER BY id").fetchall()


def forget_new_cars(admin):
    admin.execute("DELETE FROM garagebooks.expense WHERE car_id <> 1")
    admin.execute("DELETE FROM garagebooks.odometer_reading WHERE car_id <> 1")
    admin.execute("DELETE FROM garagebooks.car WHERE id <> 1")


@pytest.fixture()
def made(admin):
    yield
    forget_new_cars(admin)


def test_the_upload_page_offers_to_create_the_car(client):
    html = client.get("/import/acar").data.decode()
    assert '<option value="new"' in html and "Create a new car from this file" in html


def test_a_new_car_preview_shows_the_cars_details_to_check_and_creates_nothing(client, admin, made):
    html = upload(client, car_id="new").data.decode()
    fields = preview_fields(html)
    assert (fields["car_name"], fields["car_make"], fields["car_model"], fields["car_model_year"], fields["car_vin"],
            fields["car_body_type"], fields["car_kind"]) == ("Example", "Example", "Hatch", "2020", "1ABCD23EF45678901",
                                                              "Hatchback", "ICE")
    assert "New car" in html and new_cars(admin) == [] and rows(admin) == []
    assert "ABC123" not in html                                   # the plate isn't read


def test_a_new_car_gets_fuel_because_the_guessed_type_has_an_engine(client, admin, made):
    html = upload(client, car_id="new").data.decode()
    assert f'<option value="{category_id(admin, "Fuel")}"' in html


def test_confirming_makes_the_car_and_puts_the_costs_on_it(client, admin, made):
    data = tick_all(preview_fields(upload(client, car_id="new").data.decode()))
    for key, value in list(data.items()):
        if key.startswith("category_id_") and not value:
            data[key] = str(category_id(admin, "Registration"))
    resp = client.post("/import/acar/confirm", data=data, headers=ORIGIN)
    assert resp.status_code == 303 and "Location" in resp.headers
    (car,) = new_cars(admin)
    assert (car["name"], car["kind"], car["make"], car["model"], car["vin"], car["model_year"], car["body_type"]) == (
        "Example", "ICE", "Example", "Hatch", "1ABCD23EF45678901", 2020, "Hatchback")
    assert car["id"] >= 1000 and str(car["sale_price"]) == "2500.00"            # the sale went on the new car
    assert len(rows(admin)) == 9 and {r["car_id"] for r in rows(admin)} == {car["id"]}


def test_the_car_details_can_be_corrected_before_it_is_made(client, admin, made):
    data = tick_all(preview_fields(upload(client, car_id="new").data.decode()))
    data.update(car_name="Family hatch", car_kind="PHEV", car_model="Hatch RS")
    for key, value in list(data.items()):
        if key.startswith("category_id_") and not value:
            data[key] = str(category_id(admin, "Registration"))
    client.post("/import/acar/confirm", data=data, headers=ORIGIN)
    (car,) = new_cars(admin)
    assert (car["name"], car["kind"], car["model"]) == ("Family hatch", "PHEV", "Hatch RS")


def test_a_bad_car_detail_makes_nothing_and_keeps_what_was_typed(client, admin, made):
    data = tick_all(preview_fields(upload(client, car_id="new").data.decode()))
    data.update(car_vin="NOTAVIN", car_name="Keep me")
    for key, value in list(data.items()):
        if key.startswith("category_id_") and not value:
            data[key] = str(category_id(admin, "Registration"))
    resp = client.post("/import/acar/confirm", data=data, headers=ORIGIN)
    page = resp.data.decode()
    assert resp.status_code == 400 and "17 letters and digits" in page and "Keep me" in page
    assert new_cars(admin) == [] and rows(admin) == []


def test_a_bad_row_makes_no_car_either(client, admin, made):
    data = tick_all(preview_fields(upload(client, car_id="new").data.decode()))
    data["amount_1"] = "abc"
    assert client.post("/import/acar/confirm", data=data, headers=ORIGIN).status_code == 400
    assert new_cars(admin) == []


def test_an_electric_new_car_is_offered_no_fuel_and_refused_it(client, admin, made):
    data = tick_all(preview_fields(upload(client, car_id="new").data.decode()))
    data["car_kind"] = "BEV"
    for key, value in list(data.items()):
        if key.startswith("category_id_") and not value:
            data[key] = str(category_id(admin, "Registration"))
    resp = client.post("/import/acar/confirm", data=data, headers=ORIGIN)
    assert resp.status_code == 400 and "does not apply to this kind of car" in resp.data.decode()
    assert new_cars(admin) == []


def test_two_vehicles_ask_which_then_make_that_one(client, admin, made):
    html = upload(client, "acar-two-vehicles.csv", car_id="new").data.decode()
    assert "Which vehicle" in html and 'value="new"' in html
    text = re.search(r'name="csv_text"[^>]*>(.*?)</textarea>', html, re.S).group(1)
    text = text.replace("&#34;", '"').replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
    page = client.post("/import/acar", data={"car_id": "new", "fill_ups": "1", "vehicle": "Old Example", "csv_text": text},
                       headers=ORIGIN).data.decode()
    assert preview_fields(page)["car_name"] == "Old Example" and preview_fields(page)["car_model"] == "Ute"


# ---- every cost gets a category, and a service gets its items -----------------------------

def ticked(html):
    return {n for n in re.findall(r'<input type="checkbox" name="include_(\d+)"[^>]*checked', html)}


def item_names(admin, expense_id):
    return [r["name"] for r in admin.execute(
        "SELECT i.name FROM garagebooks.expense_service_item l JOIN garagebooks.service_item i ON i.id = l.item_id "
        "WHERE l.expense_id = %s ORDER BY i.name", (expense_id,))]


def as_previewed(client, **over):
    """Import exactly what the preview proposes, ticked rows only, as a person pressing Import straight away does."""
    html = upload(client, **over).data.decode()
    data = preview_fields(html)
    for n in ticked(html):
        data[f"include_{n}"] = "on"
    for box in re.finditer(r'<input type="checkbox" name="(service_item_\d+)" value="([^"]*)"\s*checked', html):
        data.setdefault(box.group(1), [])
        data[box.group(1)].append(box.group(2))
    return html, client.post("/import/acar/confirm", data=data, headers=ORIGIN)


def test_pressing_import_straight_away_brings_in_every_cost(client, admin):
    html, resp = as_previewed(client)
    assert resp.status_code == 303, resp.data.decode()[:3000]
    assert len(ticked(html)) == 10
    assert len(rows(admin)) == 9                                     # and the sale, on the car


def test_what_fits_no_category_goes_to_a_new_catch_all_made_only_on_import(client, admin):
    upload(client)
    assert not admin.execute("SELECT 1 FROM garagebooks.category WHERE name = 'Expenses'").fetchone()
    _, resp = as_previewed(client)
    toll = [r for r in rows(admin) if str(r["amount"]) == "4.50"][0]
    assert toll["category_id"] == category_id(admin, "Expenses")
    assert "Created the category Expenses" in client.get(resp.headers["Location"]).data.decode()


def test_the_persons_own_catch_all_is_used_when_there_is_one(client, admin):
    own = admin.execute("INSERT INTO garagebooks.category (name, kind) VALUES ('Other', 'running') RETURNING id").fetchone()["id"]
    html = upload(client).data.decode()
    assert "new:catchall" not in html
    as_previewed(client)
    assert [r["category_id"] for r in rows(admin) if str(r["amount"]) == "4.50"] == [own]


def test_a_service_ticks_its_items_and_makes_the_missing_ones_only_on_import(client, admin):
    html = upload(client).data.decode()
    assert "Inspection (new)" in html and "New service items" in html
    assert not admin.execute("SELECT 1 FROM garagebooks.service_item WHERE name = 'Inspection'").fetchone()
    _, resp = as_previewed(client)
    service = [r for r in rows(admin) if str(r["amount"]) == "429.30"][0]
    assert service["category_id"] == category_id(admin, "Service")
    assert item_names(admin, service["id"]) == ["Engine oil", "Inspection", "Oil filter"]
    page = client.get(resp.headers["Location"]).data.decode()
    assert "Created 1 new service item: Inspection." in page


def test_an_item_is_not_made_for_a_row_left_unticked(client, admin):
    html = upload(client).data.decode()
    data = preview_fields(html)
    toll = re.search(r'name="include_(\d+)"(?:(?!</tr>).)*?Toll', html, re.S).group(1)
    data[f"include_{toll}"] = "on"
    for key in [k for k in data if k.startswith("category_id_")]:
        data[key] = data[key] or str(category_id(admin, "Registration"))
    data[f"category_id_{toll}"] = str(category_id(admin, "Registration"))
    assert client.post("/import/acar/confirm", data=data, headers=ORIGIN).status_code == 303
    assert not admin.execute("SELECT 1 FROM garagebooks.service_item WHERE name = 'Inspection'").fetchone()


def test_merging_a_service_gives_an_expense_with_no_items_its_items(client, admin):
    existing = admin.execute(
        "INSERT INTO garagebooks.expense (car_id, category_id, occurred_at, amount) VALUES (1, %s, '2026-02-19 04:10+00', 429.30) RETURNING id",
        (category_id(admin, "Service"),)).fetchone()["id"]
    as_previewed(client)
    assert item_names(admin, existing) == ["Engine oil", "Inspection", "Oil filter"]


# ---- finding what is wrong -------------------------------------------------------------

def broken(client, admin):
    html = upload(client).data.decode()
    data = tick_all(preview_fields(html))
    data["amount_2"], data["date_5"] = "abc", ""
    return client.post("/import/acar/confirm", data=data, headers=ORIGIN)


def test_errors_are_listed_at_the_top_each_linking_to_its_row(client, admin):
    resp = broken(client, admin)
    page = resp.data.decode()
    assert resp.status_code == 400 and rows(admin) == []
    summary = page[page.index('id="error-summary"'):page.index("<table")]
    assert 'role="alert"' in page and 'tabindex="-1"' in summary
    assert "2 rows need fixing" in summary and "Nothing has been saved" in summary
    assert 'href="#row-2"' in summary and 'href="#row-5"' in summary
    assert '<tr id="row-2" class="has-error">' in page and '<tr id="row-5" class="has-error">' in page


def test_each_wrong_row_leads_to_the_next(client, admin):
    page = broken(client, admin).data.decode()
    row2 = page[page.index('<tr id="row-2"'):page.index('<tr id="row-3"')]
    assert 'href="#row-5">Next error' in row2 and 'href="#error-summary"' in row2


def test_a_merge_choice_survives_an_error_elsewhere(client, admin):
    existing = seed_invoice(admin)
    html = upload(client).data.decode()
    data = tick_all(preview_fields(html))
    merge_row = next(k for k, v in data.items() if k.startswith("action_") and v == str(existing)).split("_")[1]
    other = "0" if merge_row != "0" else "1"
    data[f"amount_{other}"] = "abc"
    page = client.post("/import/acar/confirm", data=data, headers=ORIGIN).data.decode()
    assert f'<option value="{existing}" selected>Add the odometer' in page


def test_service_ticks_and_new_items_survive_an_error(client, admin):
    html = upload(client).data.decode()
    data = tick_all(preview_fields(html))
    n = re.search(r'name="service_item_(\d+)" value="new:Inspection"\s*checked', html).group(1)
    data[f"service_item_{n}"] = ["new:Inspection"]
    data["amount_0"] = "abc"
    page = client.post("/import/acar/confirm", data=data, headers=ORIGIN).data.decode()
    assert re.search(rf'name="service_item_{n}" value="new:Inspection"\s*checked', page)


# ---- categories added while the page is open ---------------------------------------------

def test_a_category_can_be_added_from_the_import_page(client, admin):
    resp = client.post("/api/categories", json={"name": "Accessories"}, headers=ORIGIN)
    assert resp.status_code == 201 and resp.get_json()["name"] == "Accessories"
    again = client.post("/api/categories", json={"name": "accessories"}, headers=ORIGIN)
    assert again.status_code == 200 and again.get_json()["existing"] is True
    assert client.post("/api/categories", json={"name": "  "}, headers=ORIGIN).status_code == 400


def test_the_lists_can_be_read_again_for_categories_made_in_another_tab(client, admin):
    admin.execute("INSERT INTO garagebooks.category (name, kind) VALUES ('Accessories', 'running')")
    got = client.get("/api/import-lists").get_json()
    assert "Accessories" in [c["name"] for c in got["categories"]]
    assert "Engine oil" in [i["name"] for i in got["service_items"]]


def test_both_dialogs_are_on_the_acar_preview(client):
    html = upload(client).data.decode()
    assert 'id="category-dialog"' in html and 'id="service-dialog"' in html and "data-lists-url" in html
