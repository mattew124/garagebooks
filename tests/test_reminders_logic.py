"""Reminders (roadmap item 4): what is due, worked out from a date and/or an odometer reading.
Pure: no database, no clock. All figures are invented."""

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from garagebooks import reminders as rm

TODAY = date(2026, 11, 16)


def reminder(**over):
    return {"due_date": None, "due_km": None, "lead_days": 30, "lead_km": 1000, "resolved_at": None,
            "informational": False, **over}


def code(r, odometer=None, today=TODAY):
    return rm.status(r, today, odometer).code


# ---- by date only -----------------------------------------------------------------

@pytest.mark.parametrize("due, expected, left", [
    (date(2026, 11, 15), rm.OVERDUE, -1),        # yesterday: past the date
    (date(2026, 11, 16), rm.DUE_SOON, 0),        # today is not yet late
    (date(2026, 12, 15), rm.DUE_SOON, 29),
    (date(2026, 12, 16), rm.DUE_SOON, 30),       # exactly the lead time: inside it
    (date(2026, 12, 17), rm.UPCOMING, 31),       # one day outside
    (date(2030, 1, 1), rm.UPCOMING, (date(2030, 1, 1) - TODAY).days),
])
def test_a_date_reminder_counts_days(due, expected, left):
    s = rm.status(reminder(due_date=due), TODAY, None)
    assert (s.code, s.days_left, s.km_left, s.odometer_unknown) == (expected, left, None, False)


def test_the_odometer_is_not_needed_for_a_date_reminder():
    assert code(reminder(due_date=date(2026, 11, 1)), odometer=None) == rm.OVERDUE
    assert code(reminder(due_date=date(2027, 6, 1)), odometer=123) == rm.UPCOMING


def test_a_lead_time_of_zero_means_due_soon_only_on_the_day():
    r = reminder(due_date=date(2026, 11, 17), lead_days=0)
    assert code(r) == rm.UPCOMING
    assert code(r, today=date(2026, 11, 17)) == rm.DUE_SOON
    assert code(r, today=date(2026, 11, 18)) == rm.OVERDUE


# ---- by odometer only -----------------------------------------------------------------

@pytest.mark.parametrize("odometer, expected, left", [
    (10_500, rm.OVERDUE, -500),
    (10_000, rm.OVERDUE, 0),          # at the reading: due now, so overdue
    (9_999.9, rm.DUE_SOON, 0.1),
    (9_000, rm.DUE_SOON, 1000),       # exactly the lead: inside it
    (8_999.9, rm.UPCOMING, 1000.1),
    (0, rm.UPCOMING, 10_000),
])
def test_an_odometer_reminder_counts_kilometres(odometer, expected, left):
    s = rm.status(reminder(due_km=10_000), TODAY, odometer)
    assert s.code == expected and s.days_left is None
    assert s.km_left == pytest.approx(left) and not s.odometer_unknown


def test_the_lead_km_is_the_reminders_own():
    r = reminder(due_km=10_000, lead_km=250)
    assert code(r, 9_700) == rm.UPCOMING and code(r, 9_750) == rm.DUE_SOON


def test_numeric_values_from_the_database_work_the_same():
    r = reminder(due_km=Decimal("10000.0"), lead_km=Decimal("1000.0"))
    assert code(r, Decimal("9500.5")) == rm.DUE_SOON
    assert rm.status(r, TODAY, Decimal("9500.5")).km_left == pytest.approx(499.5)


# ---- an unknown odometer is never "not due" -------------------------------------------

def test_a_km_reminder_with_no_odometer_is_unknown_not_upcoming():
    s = rm.status(reminder(due_km=10_000), TODAY, None)
    assert s.code == rm.UNKNOWN and s.odometer_unknown and s.km_left is None
    assert "odometer" in s.label.lower() and "unknown" in s.label.lower()
    assert s.rank == rm.status(reminder(due_date=date(2026, 12, 1)), TODAY, None).rank   # listed with due soon


def test_with_a_date_too_the_date_decides_when_the_odometer_is_unknown():
    both = dict(due_km=10_000)
    s = rm.status(reminder(due_date=date(2026, 11, 1), **both), TODAY, None)
    assert s.code == rm.OVERDUE and s.odometer_unknown          # the date has passed: that is enough
    s = rm.status(reminder(due_date=date(2026, 12, 10), **both), TODAY, None)
    assert s.code == rm.DUE_SOON and s.odometer_unknown
    s = rm.status(reminder(due_date=date(2027, 6, 1), **both), TODAY, None)
    assert s.code == rm.UPCOMING and s.odometer_unknown          # still flagged: the km half can't be checked


# ---- both: whichever comes first ----------------------------------------------------------

@pytest.mark.parametrize("due_date, odometer, expected", [
    (date(2027, 6, 1), 5_000, rm.UPCOMING),       # neither close
    (date(2026, 11, 20), 5_000, rm.DUE_SOON),     # the date is close
    (date(2027, 6, 1), 9_500, rm.DUE_SOON),       # the odometer is close
    (date(2026, 11, 1), 5_000, rm.OVERDUE),       # the date has passed, the odometer has not
    (date(2027, 6, 1), 10_001, rm.OVERDUE),       # the odometer has, the date has not
    (date(2026, 11, 20), 10_001, rm.OVERDUE),     # soon by date, over by odometer: the worse wins
])
def test_whichever_comes_first_is_the_worse_of_the_two(due_date, odometer, expected):
    s = rm.status(reminder(due_date=due_date, due_km=10_000), TODAY, odometer)
    assert s.code == expected
    assert s.days_left == (due_date - TODAY).days and s.km_left == pytest.approx(10_000 - odometer)


# ---- resolved, provider-renewed ---------------------------------------------------------------

def test_a_resolved_reminder_is_resolved_whatever_the_date():
    r = reminder(due_date=date(2020, 1, 1), due_km=1, resolved_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
    s = rm.status(r, TODAY, 99_999)
    assert s.code == rm.RESOLVED and s.rank > rm.status(reminder(due_date=date(2030, 1, 1)), TODAY, None).rank


def test_a_provider_renewed_item_is_informational_and_asks_nothing_of_you():
    r = reminder(due_date=date(2026, 11, 20), informational=True)
    s = rm.status(r, TODAY, None)
    assert s.code == rm.INFO and not s.needs_attention and s.days_left == 4


def test_only_overdue_due_soon_and_unknown_need_attention():
    needs = {rm.status(reminder(due_date=d), TODAY, None).needs_attention
             for d in (date(2026, 1, 1), date(2026, 11, 20))}
    assert needs == {True}
    assert not rm.status(reminder(due_date=date(2030, 1, 1)), TODAY, None).needs_attention
    assert rm.status(reminder(due_km=5), TODAY, None).needs_attention                    # unknown is never hidden


def test_a_reminder_with_neither_date_nor_km_is_refused():
    with pytest.raises(ValueError):
        rm.status(reminder(), TODAY, None)


# ---- ordering: most urgent first -------------------------------------------------------------------

def test_the_most_urgent_comes_first():
    items = [("soon late", reminder(due_date=date(2026, 12, 10))), ("later", reminder(due_date=date(2027, 3, 1))),
             ("overdue", reminder(due_date=date(2026, 10, 1))), ("unknown", reminder(due_km=5)),
             ("renewed", reminder(due_date=date(2026, 11, 20), informational=True)),
             ("soonest", reminder(due_date=date(2026, 11, 18)))]
    ordered = sorted(items, key=lambda i: rm.status(i[1], TODAY, None).sort_key)
    assert [i[0] for i in ordered] == ["overdue", "soonest", "soon late", "unknown", "later", "renewed"]


# ---- recurrence: months ------------------------------------------------------------------------

@pytest.mark.parametrize("start, months, expected", [
    (date(2026, 1, 15), 6, date(2026, 7, 15)),
    (date(2026, 1, 31), 1, date(2026, 2, 28)),          # clamped to month end
    (date(2028, 1, 31), 1, date(2028, 2, 29)),          # leap year
    (date(2026, 8, 31), 1, date(2026, 9, 30)),
    (date(2026, 3, 31), 12, date(2027, 3, 31)),
    (date(2028, 2, 29), 12, date(2029, 2, 28)),
    (date(2026, 11, 30), 3, date(2027, 2, 28)),         # across a year
    (date(2026, 5, 10), 0, date(2026, 5, 10)),
    (date(2026, 5, 10), 24, date(2028, 5, 10)),
])
def test_add_months_clamps_to_the_end_of_the_month(start, months, expected):
    assert rm.add_months(start, months) == expected


def test_the_next_reminder_carries_everything_over_and_follows_the_resolution():
    r = reminder(id=7, car_id=3, title="Tyre rotation", category_id=5, due_date=date(2026, 11, 1), due_km=40_000,
                 lead_days=14, lead_km=500, every_months=6, every_km=10_000, notes="Front to back")
    nxt = rm.next_reminder(r, date(2026, 11, 20), 40_250)
    assert nxt == {"car_id": 3, "title": "Tyre rotation", "category_id": 5, "service_item_id": None, "due_date": date(2027, 5, 20),
                   "due_km": 50_250, "lead_days": 14, "lead_km": 500, "every_months": 6, "every_km": 10_000,
                   "notes": "Front to back", "previous_id": 7}


def test_a_months_only_recurrence_makes_a_date_only_reminder_and_km_only_a_km_only_one():
    base = dict(id=1, car_id=1, title="x", category_id=None, notes=None, lead_days=30, lead_km=1000,
                due_date=date(2026, 1, 1), due_km=100)
    a = rm.next_reminder(reminder(**base, every_months=12, every_km=None), date(2026, 3, 31), 500)
    assert a["due_date"] == date(2027, 3, 31) and a["due_km"] is None
    b = rm.next_reminder(reminder(**base, every_months=None, every_km=10_000), date(2026, 3, 31), 500)
    assert b["due_date"] is None and b["due_km"] == 10_500


def test_the_next_km_falls_back_to_the_previous_due_km_then_to_nothing():
    base = dict(id=1, car_id=1, title="x", category_id=None, notes=None, every_km=10_000, every_months=None)
    with_prev = rm.next_reminder(reminder(**base, due_km=40_000), date(2026, 3, 1), None)
    assert with_prev["due_km"] == 50_000
    assert rm.next_reminder(reminder(**base, due_date=date(2026, 1, 1)), date(2026, 3, 1), None) is None   # nothing to count from


def test_a_one_off_has_no_next():
    assert rm.next_reminder(reminder(id=1, car_id=1, title="x", due_date=date(2026, 1, 1)), date(2026, 1, 2), 1) is None


def test_a_lease_end_with_the_next_lease_loaded_is_information_even_when_past():
    """The derived lease end of a lease whose successor is already entered: never due soon or overdue."""
    for due in (date(2026, 11, 20), date(2026, 11, 16), date(2026, 11, 15)):
        r = reminder(due_date=due, lead_days=90, informational=True, next_lease_start=date(2026, 11, 21))
        s = rm.status(r, TODAY, None)
        assert s.code == rm.INFO and not s.needs_attention
        assert s.label == "Next lease loaded"
        assert s.describe[-1] == "next lease already loaded (starts 21 Nov 2026)"
        assert not any("overdue" in p or "due" in p.split() for p in s.describe)
    assert rm.status(reminder(due_date=date(2026, 11, 20), lead_days=90, informational=True,
                              next_lease_start=date(2026, 11, 21)), TODAY, None).describe == [
        "ends in 4 days", "next lease already loaded (starts 21 Nov 2026)"]
    # a provider-renewed registration keeps its own label
    assert rm.status(reminder(due_date=date(2026, 11, 20), informational=True), TODAY, None).label == "Provider renews"
