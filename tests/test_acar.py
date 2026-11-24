"""Reading an aCar CSV export (garagebooks/acar.py): sections, formats, and what each record becomes.

The fixtures are invented; they copy only the layout of a real export.
"""

from datetime import date, time
from decimal import Decimal
from pathlib import Path

import pytest

from garagebooks import acar

FIXTURES = Path(__file__).parent / "fixtures"


def load(name="acar-records.csv", **kw):
    return acar.parse((FIXTURES / name).read_bytes(), **kw)


def test_reads_the_vehicle_and_its_records():
    export = load()
    assert [v["name"] for v in export.vehicles] == ["Example"]
    assert export.vehicles[0]["make"] == "Example" and export.vehicles[0]["model"] == "Hatch"
    assert len(export.records) == 10    # 2 fill-ups + 6 costs + the sale + the note; only the trip is left out


def test_a_fill_up_is_a_fuel_cost_with_its_litres_in_the_notes():
    fill = [r for r in load().records if r.kind == "fillup"][0]
    assert (fill.date, fill.time, fill.amount, fill.odometer_km) == (date(2026, 4, 15), time(14, 7), Decimal("70.71"), 19525)
    assert fill.vendor == "Example Fuel Stop" and fill.category_hint == "Fuel"
    assert "55.5 L" in fill.notes


def test_a_fill_up_with_no_cost_is_kept_at_zero():
    free = [r for r in load().records if r.kind == "fillup" and r.date == date(2026, 5, 3)]
    assert len(free) == 1 and free[0].amount == Decimal("0")


def test_a_refund_stays_negative():
    refund = [r for r in load().records if r.subtypes == ["Insurance"]][0]
    assert refund.amount == Decimal("-311.67") and refund.category_hint == "Insurance"


def test_a_sale_is_kept_with_its_price_and_date():
    (sold,) = [r for r in load().records if r.kind == "sale"]
    assert (sold.date, sold.amount, sold.odometer_km) == (date(2026, 6, 1), Decimal("2500.00"), Decimal("20500.0"))
    assert sold.category_hint is None and sold.label == "Sold"


def test_a_note_is_kept_with_no_cost():
    (note,) = [r for r in load().records if r.event_type == "Note"]
    assert note.amount == Decimal("0") and "Remember to book the tyre check" in note.notes


def test_only_trips_are_left_out_and_counted():
    assert dict(load().left_out) == {"trips": 1}


def test_a_purchase_is_the_initial_purchase():
    (bought,) = [r for r in load().records if r.event_type == "Purchased"]
    assert bought.category_hint == "Initial purchase" and bought.amount == Decimal("25000.00")


def test_a_service_takes_its_category_from_the_first_sub_type_that_has_one_and_keeps_the_rest_in_the_notes():
    (tyres,) = [r for r in load().records if r.amount == Decimal("1377.00")]
    assert tyres.category_hint == "Tyres"
    assert "New Tires, Tire Rotation, Tire Pressure, Wheel Alignment" in tyres.notes
    assert "Example Visa" in tyres.notes and "Rear tyres swapped" in tyres.notes
    assert tyres.vendor == "Example Tyres Pty Ltd"


@pytest.mark.parametrize("subtypes, expected", [
    (["Registration", "Stamp Duty"], "Registration"),
    (["Tire Rotation"], "Tyre rotation"),
    (["Wheel Alignment"], "Wheel alignment"),
    (["Cabin Air Filter"], "Cabin air filter"),
    (["Air Filter", "Engine Oil"], "Air filter"),
    (["Tire A", "Tire B"], "Tyres"),
    (["Tire Pressure"], None),
    (["Toll"], None),
    ([], None),
])
def test_sub_types_map_to_the_seeded_categories(subtypes, expected):
    assert acar.category_hint("Service", subtypes) == expected


def test_an_unknown_sub_type_is_still_shown_so_it_can_be_matched_to_a_category():
    toll = [r for r in load().records if r.subtypes == ["Toll"]][0]
    assert toll.category_hint is None and toll.odometer_km is None   # an odometer of 0 means none was entered
    assert toll.label == "Toll"


def test_the_date_follows_the_exports_own_pattern():
    export = load("acar-two-vehicles.csv")
    assert export.date_format == "M/d/yyyy"
    assert {r.date for r in export.records} == {date(2010, 4, 21), date(2026, 4, 22)}


def test_miles_become_kilometres():
    (old,) = [r for r in load("acar-two-vehicles.csv").records if r.vehicle == "Old Example"]
    assert old.odometer_km == Decimal("160934.4")


def test_one_vehicle_can_be_chosen_from_several():
    export = load("acar-two-vehicles.csv", vehicle="Old Example")
    assert [r.vehicle for r in export.records] == ["Old Example"]
    assert [v["name"] for v in load("acar-two-vehicles.csv").vehicles] == ["Example", "Old Example"]


def test_fill_ups_can_be_left_out():
    export = load(fill_ups=False)
    assert all(r.kind != "fillup" for r in export.records) and len(export.records) == 8


@pytest.mark.parametrize("data", [b"", b"just,some,words\n1,2,3\n", b"\xff\xfe\x00\x01 not csv"])
def test_a_file_that_is_not_an_acar_export_is_refused(data):
    with pytest.raises(acar.NotAnExport):
        acar.parse(data)


def test_only_the_statistics_or_trip_export_is_refused_with_a_hint():
    with pytest.raises(acar.NotAnExport, match="Records"):
        acar.parse(b'\xef\xbb\xbfMetadata\n"Export Version"\n"2"\n\n\nVehicle Statistics\n"a"\n"1"\n')


def test_a_row_with_a_bad_date_or_amount_is_left_out_not_fatal():
    text = (FIXTURES / "acar-records.csv").read_text(encoding="utf-8").replace('"24/4/2026"', '"31/31/2026"')
    export = acar.parse(text.encode())
    assert export.left_out["unreadable"] == 1


def test_a_row_for_a_vehicle_the_file_does_not_list_is_still_read():
    text = (FIXTURES / "acar-records.csv").read_text(encoding="utf-8").replace('"Example","19/2/2026"', '"Ghost","19/2/2026"')
    assert any(r.vehicle == "Ghost" for r in acar.parse(text.encode()).records)
