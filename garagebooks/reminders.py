"""Reminders (roadmap item 4): service and paperwork that fall due on a date, an odometer reading,
or whichever comes first.

`status()` is the one place that decides what is due. It is pure: it is given the reminder, today's
date and the car's odometer, and does no I/O, so the rules can be tested exhaustively. (The dashboard's
Reminders panel repeats the same rules in SQL, and a test checks the two agree.) What is due is worked
out when a page or the dashboard is viewed, never stored.

Resolving, un-resolving and the recurrence that makes the next reminder are here too, because they
need the odometer from `sources` as well as the database.
"""

import calendar
from dataclasses import dataclass
from datetime import date, datetime

from . import db, sources

OVERDUE, DUE_SOON, UNKNOWN, UPCOMING, INFO, RESOLVED = "overdue", "due_soon", "unknown", "upcoming", "info", "resolved"

LABELS = {OVERDUE: "Overdue", DUE_SOON: "Due soon", UNKNOWN: "Odometer unknown", UPCOMING: "Upcoming",
          INFO: "Provider renews", RESOLVED: "Resolved"}
# The order a list is shown in. An unknown odometer sorts with due soon: it may well be due, so it is never hidden.
RANK = {OVERDUE: 0, DUE_SOON: 1, UNKNOWN: 1, UPCOMING: 2, INFO: 3, RESOLVED: 4}
ATTENTION = {OVERDUE, DUE_SOON, UNKNOWN}

DEFAULT_LEAD_DAYS = 30
DEFAULT_LEAD_KM = 1000
RESOLVED_PAGE = 10          # resolved reminders per page


# Standard reminders offered on the Add a reminder form: a title and how often it usually repeats, as a
# starting point to check against the car's handbook. `kinds` is the types of car it applies to (None is
# every car): a battery electric car has no engine oil, oil filter, engine air filter, spark plugs or
# timing belt. `category` names a standard category and `item` a service item (they are chosen too when they
# still exist): the service-type ones are items of the Service category.
ENGINE = ("ICE", "PHEV", "HEV")
STANDARD = (
    {"key": "engine_oil", "title": "Engine oil", "months": 12, "km": 10000, "kinds": ENGINE, "category": "Service", "item": "Engine oil"},
    {"key": "oil_filter", "title": "Oil filter", "months": 12, "km": 10000, "kinds": ENGINE, "category": "Service", "item": "Oil filter"},
    {"key": "air_filter", "title": "Engine air filter", "months": 24, "km": 30000, "kinds": ENGINE, "category": "Service", "item": "Air filter"},
    {"key": "spark_plugs", "title": "Spark plugs", "months": 48, "km": 60000, "kinds": ENGINE, "category": None},
    {"key": "timing_belt", "title": "Timing belt", "months": 72, "km": 100000, "kinds": ENGINE, "category": "Service", "item": "Timing belt"},
    {"key": "tyre_rotation", "title": "Tyre rotation", "months": None, "km": 10000, "kinds": None, "category": "Service", "item": "Tyre rotation"},
    {"key": "wheel_alignment", "title": "Wheel alignment", "months": 12, "km": 20000, "kinds": None, "category": "Service", "item": "Wheel alignment"},
    {"key": "cabin_air_filter", "title": "Cabin air filter", "months": 24, "km": None, "kinds": None, "category": "Service", "item": "Cabin air filter"},
    {"key": "brake_fluid", "title": "Brake fluid", "months": 24, "km": None, "kinds": None, "category": "Service", "item": "Brake fluid"},
    {"key": "brake_pads", "title": "Brake pads and discs check", "months": 12, "km": 20000, "kinds": None, "category": "Service", "item": "Brakes"},
    {"key": "coolant", "title": "Coolant", "months": 48, "km": None, "kinds": None, "category": "Service", "item": "Coolant"},
    {"key": "wiper_blades", "title": "Wiper blades", "months": 12, "km": None, "kinds": None, "category": "Service", "item": "Windscreen wipers"},
    {"key": "battery_12v", "title": "12 V battery check", "months": 24, "km": None, "kinds": None, "category": None},
    {"key": "service", "title": "Scheduled service", "months": 12, "km": 15000, "kinds": None, "category": None},
    {"key": "registration", "title": "Registration renewal", "months": 12, "km": None, "kinds": None, "category": "Registration"},
    {"key": "insurance", "title": "Insurance renewal", "months": 12, "km": None, "kinds": None, "category": "Insurance"},
)


def standard_for(kind):
    """The standard reminders that apply to a type of car (BEV, ICE, PHEV or HEV)."""
    return [s for s in STANDARD if s["kinds"] is None or kind in s["kinds"]]


@dataclass(frozen=True)
class Status:
    code: str
    days_left: int | None = None        # negative once the date has passed
    km_left: float | None = None        # negative once the odometer is past
    odometer_unknown: bool = False      # a km figure that can't be compared: no odometer is known
    next_lease_start: date | None = None    # a lease end whose next lease is already loaded: when that one starts

    @property
    def label(self):
        return "Next lease loaded" if self.next_lease_start is not None else LABELS[self.code]

    @property
    def rank(self):
        return RANK[self.code]

    @property
    def needs_attention(self):
        return self.code in ATTENTION

    @property
    def describe(self):
        """What is left, in words, for each of the date and the odometer: 'due in 10 days', '500 km to go'."""
        parts = []
        if self.next_lease_start is not None:      # nothing is due: say when it ends and when the next one starts
            d = self.days_left
            parts.append("ends today" if d == 0 else f"ended {-d} day{'' if d == -1 else 's'} ago" if d < 0
                         else f"ends in {d} day{'' if d == 1 else 's'}")
            n = self.next_lease_start
            parts.append(f"next lease already loaded (starts {n.day} {n:%b %Y})")
            return parts
        if self.days_left is not None:
            d = self.days_left
            parts.append("due today" if d == 0 else f"{-d} day{'' if d == -1 else 's'} overdue" if d < 0
                         else f"due in {d} day{'' if d == 1 else 's'}")
        if self.km_left is not None:
            k = self.km_left
            parts.append("due now" if k == 0 else f"{abs(k):,.0f} km over" if k < 0 else f"{max(k, 1):,.0f} km to go")
        if self.odometer_unknown:
            parts.append("odometer unknown")
        return parts

    @property
    def sort_key(self):
        """Most urgent first: by status, then the fewest days left, then the fewest km."""
        return (self.rank, self.days_left if self.days_left is not None else 10 ** 9,
                self.km_left if self.km_left is not None else 10 ** 9)


def _date_state(days_left, lead_days):
    if days_left < 0:
        return OVERDUE
    return DUE_SOON if days_left <= lead_days else UPCOMING


def _km_state(km_left, lead_km):
    if km_left <= 0:
        return OVERDUE
    return DUE_SOON if km_left <= lead_km else UPCOMING


def status(reminder, today, odometer_km):
    """What a reminder's state is today. `reminder` has due_date, due_km, lead_days, lead_km and
    resolved_at (and `informational` for one derived from a lease that the provider takes care of, or a lease end
    with `next_lease_start`: the next lease is already loaded).
    `odometer_km` is the car's odometer, or None when it isn't known: a km reminder then says so,
    and is never treated as "not due"."""
    if reminder.get("resolved_at") is not None:
        return Status(RESOLVED)
    due_date, due_km = reminder.get("due_date"), reminder.get("due_km")
    if due_date is None and due_km is None:
        raise ValueError("A reminder needs a date, an odometer reading or both.")
    days_left = None if due_date is None else (due_date - today).days
    km_left = None
    unknown = False
    if due_km is not None:
        if odometer_km is None:
            unknown = True
        else:
            km_left = float(due_km) - float(odometer_km)
    states = []
    if days_left is not None:
        states.append(_date_state(days_left, reminder.get("lead_days", DEFAULT_LEAD_DAYS)))
    if km_left is not None:
        states.append(_km_state(km_left, float(reminder.get("lead_km", DEFAULT_LEAD_KM))))
    # Whichever comes first: the worse of the two. With only an unknown odometer there is nothing to judge by.
    code = min(states, key=RANK.get) if states else UNKNOWN
    next_start = reminder.get("next_lease_start")
    if (reminder.get("informational") or next_start is not None) and code in (OVERDUE, DUE_SOON, UPCOMING):
        code = INFO
    return Status(code, days_left, km_left, unknown, next_start if code == INFO else None)


# ---- recurrence ---------------------------------------------------------------------------

def add_months(start, months):
    """Calendar months later, clamped to the end of a shorter month (31 Jan + 1 month = 28 Feb)."""
    index = start.year * 12 + (start.month - 1) + months
    year, month = divmod(index, 12)
    month += 1
    return date(year, month, min(start.day, calendar.monthrange(year, month)[1]))


def next_reminder(reminder, resolved_on, odometer_km):
    """The values of the reminder that follows a recurring one resolved on `resolved_on` at `odometer_km`
    (None if unknown: the reminder's own due_km is counted from then), or None for a one-off, or when
    there is nothing to count the next from."""
    months, every_km = reminder.get("every_months"), reminder.get("every_km")
    if not months and not every_km:
        return None
    due_date = add_months(resolved_on, months) if months else None
    due_km = None
    if every_km:
        base = odometer_km if odometer_km is not None else reminder.get("due_km")
        if base is not None:
            due_km = round(float(base) + float(every_km), 1)
    if due_date is None and due_km is None:
        return None
    return {"car_id": reminder["car_id"], "title": reminder["title"], "category_id": reminder.get("category_id"),
            "service_item_id": reminder.get("service_item_id"), "due_date": due_date, "due_km": due_km, "lead_days": reminder["lead_days"], "lead_km": reminder["lead_km"],
            "every_months": months, "every_km": every_km, "notes": reminder.get("notes"), "previous_id": reminder["id"]}


# ---- what is due, for the pages -----------------------------------------------------------

KINDS = {"reminder": "Reminder", "registration": "Registration", "lease_end": "Lease"}


def due_items(conn, teslamate, today, now, car_id=None):
    """Every open reminder (stored and derived), with its status, most urgent first. Each is a dict of
    the view's columns plus car_name, category, odometer (a sources.Reading or None), status and the
    reminder's notes and recurrence."""
    rows = db.open_reminders(conn, car_id)
    odometers = {}
    for row in rows:
        if row["car_id"] not in odometers:
            odometers[row["car_id"]] = sources.odometer_at(conn, teslamate, row["car_id"], now)
        reading = odometers[row["car_id"]]
        row["odometer"] = reading
        row["status"] = status(row, today, None if reading is None else reading.km)
    return sorted(rows, key=lambda r: (r["status"].sort_key, r["car_name"], r["title"]))


def attention(items):
    """(overdue, needing a look) among `items`: the derived ones the provider takes care of don't count."""
    overdue = sum(1 for i in items if i["status"].code == OVERDUE)
    return overdue, sum(1 for i in items if i["status"].needs_attention)


# ---- resolving ----------------------------------------------------------------------------

@dataclass(frozen=True)
class Resolution:
    outcome: str               # "resolved", "missing" (no such open reminder) or "wrong_car"
    next_id: int | None = None
    next_note: str = ""        # "exists" or "no_odometer": why a recurring reminder made no next one


def resolve(conn, teslamate, reminder_id, tz, expense=None, resolved_at=None, entered_km=None):
    """Resolve a stored, open reminder, in the caller's transaction. Either with an `expense` (a db.get_expense
    row, which must be the same car's) or at `resolved_at` (an aware datetime) with an optional `entered_km`.
    A recurring reminder gets its next one now: the date counts from the resolution day, the odometer from
    the expense's, else the one entered, else the best known then, else the previous due_km."""
    row = db.get_reminder(conn, reminder_id, lock=True)
    if row is None or row["resolved_at"] is not None:
        return Resolution("missing")
    if expense is not None and expense["car_id"] != row["car_id"]:
        return Resolution("wrong_car")
    when = expense["occurred_at"] if expense is not None else resolved_at
    km = None
    if expense is not None and expense["odometer_km"] is not None:
        km = float(expense["odometer_km"])
    elif expense is None and entered_km is not None:
        km = float(entered_km)
    if km is None:
        found = sources.odometer_at(conn, teslamate, row["car_id"], when)
        km = None if found is None else found.km
    db.mark_resolved(conn, reminder_id, when, expense["id"] if expense is not None else None, km)
    if not (row["every_months"] or row["every_km"]):
        return Resolution("resolved")
    if db.following_reminder(conn, reminder_id) is not None:
        return Resolution("resolved", None, "exists")
    values = next_reminder(row, when.astimezone(tz).date(), km)
    if values is None:
        return Resolution("resolved", None, "no_odometer")
    return Resolution("resolved", db.add_reminder(conn, values))


def unresolve(conn, reminder_id):
    """Reopen a resolved reminder (a mistake). The next one it created is deleted only if it is still
    open and unedited. -> None if there was nothing to reopen, else (reopened, next_left): whether a
    following reminder was left alone because it had been edited or resolved."""
    row = db.get_reminder(conn, reminder_id, lock=True)
    if row is None or row["resolved_at"] is None:
        return None
    following = db.following_reminder(conn, reminder_id)
    db.mark_open(conn, reminder_id)
    left = False
    if following is not None:
        if following["resolved_at"] is None and following["edited_at"] is None:
            db.delete_reminder(conn, following["id"])
        else:
            left = True
    return True, left


def resolution_moment(day: date, tz, now: datetime):
    """The moment a reminder resolved on a date is stamped with: noon that day, or now for today
    (noon may still be ahead)."""
    noon = datetime(day.year, day.month, day.day, 12, tzinfo=tz)
    return min(noon, now.replace(microsecond=0)) if day == now.date() else noon
