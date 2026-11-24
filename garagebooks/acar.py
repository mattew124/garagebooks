"""Reading an aCar "records" CSV export (Android app aCar, by Zonewalker).

Pure functions, no database or Flask. The file is several tables in one: a Metadata block, then
`Vehicles`, `Fill-Up Records`, `Event Records` (services, expenses, purchases, sales, notes) and
`Trip Records`, each a title line, a header row and its rows. Columns are found by name, not
position, because aCar adds some between versions (and repeats one: `Place City`).

Everything is read except trips: fill-ups, the expense, service and purchase events, notes (which
have no cost) and the sale (`kind == "sale"`). Trips are counted in `left_out` so the page can say so.
A record with no cost has an amount of 0. The plate is never read. A vehicle's VIN is returned so the person can make a car from it; it is never logged.
"""

import csv
import io
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, time
from decimal import Decimal, InvalidOperation

MAX_VENDOR = 500
MAX_NOTES = 2000
KM_PER_MILE = Decimal("1.609344")
SECTIONS = ("Metadata", "Vehicles", "Fill-Up Records", "Event Records", "Trip Records")
DEFAULT_DATE_FORMAT = "d/M/yyyy"

# aCar's own sub-type names (lower case) -> the category they belong to here. A tyre purchase is Tyres;
# otherwise a service (and a key's battery) is a Service, with a service item for each sub-type. Anything else
# is matched against the category names the person has (`match_category`) or goes to the catch-all.
SUBTYPE_CATEGORY = {"insurance": "Insurance", "registration": "Registration"}
TYRE_PURCHASE = {"new tires", "new tyres", "tire a", "tire b", "tire c", "tire d"}
SERVICE_EXPENSES = {"key battery"}
# Categories that take what fits nowhere else, best first.
CATCH_ALL = ("expenses", "other expenses", "other", "miscellaneous", "general", "accessories")
MAX_ITEM = 60
MONTHS = {m: i for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}


class NotAnExport(ValueError):
    """The file isn't an aCar records export. The message is for the person."""


@dataclass
class Record:
    kind: str                   # "fillup", "event" or "sale"
    vehicle: str
    date: date
    time: time
    amount: Decimal             # negative for a refund
    event_type: str = ""        # Expense, Service, Purchased (events only)
    subtypes: list = field(default_factory=list)
    odometer_km: Decimal | None = None
    vendor: str = ""
    notes: str = ""
    category_hint: str | None = None

    @property
    def service_subtypes(self):
        """The sub-types that are this record's service items: those of a record filed under Service."""
        return self.subtypes if self.category_hint == "Service" else []

    @property
    def label(self):
        """What the person knows it as: 'Engine Oil, Inspection', 'Fuel', 'Purchased'."""
        if self.kind == "fillup":
            return "Fuel"
        return ", ".join(self.subtypes) or self.event_type


@dataclass
class Export:
    vehicles: list
    records: list
    date_format: str
    left_out: Counter


def _decode(data):
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return data.decode("utf-16", errors="replace")
    return data.decode("utf-8-sig", errors="replace")


def _number(raw):
    """'$1,234.50' -> Decimal('1234.50'); '-$311.67' -> Decimal('-311.67'); blank or text -> None."""
    cleaned = re.sub(r"[^\d.\-]", "", raw or "")
    if not re.fullmatch(r"-?\d+(\.\d+)?|-?\.\d+", cleaned):
        return None
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        return None


def _date(raw, pattern):
    """Read `raw` by aCar's own pattern (Java style: 'd/M/yyyy', 'M/d/yyyy', 'yyyy-MM-dd', 'd MMM yyyy')."""
    letters = [t[0] for t in re.findall(r"[A-Za-z]+", pattern)]
    parts = [p for p in re.split(r"[^0-9A-Za-z]+", (raw or "").strip()) if p]
    if len(parts) != len(letters) or {"d", "M", "y"} - set(letters):
        raise ValueError(raw)
    found = {}
    for letter, part in zip(letters, parts):
        if letter == "M" and not part.isdigit():
            number = MONTHS.get(part[:3].lower())
            if number is None:
                raise ValueError(raw)
        elif part.isdigit():
            number = int(part)
        else:
            raise ValueError(raw)
        found[letter] = number
    year = found["y"] + 2000 if found["y"] < 100 else found["y"]
    return date(year, found["M"], found["d"])


def _time(raw):
    """'14:07' or '2:07 PM'; blank -> noon, so a record with no time still lands in its day."""
    match = re.fullmatch(r"\s*(\d{1,2}):(\d{2})(?::\d{2})?\s*([AaPp][Mm])?\s*", raw or "")
    if not match:
        return time(12, 0)
    hour, minute = int(match.group(1)), int(match.group(2))
    meridian = (match.group(3) or "").lower()
    if meridian == "pm" and hour < 12:
        hour += 12
    elif meridian == "am" and hour == 12:
        hour = 0
    if hour > 23 or minute > 59:
        return time(12, 0)
    return time(hour, minute)


def _odometer(raw, unit):
    """In kilometres. aCar writes 0 where no reading was entered, which is no reading."""
    value = _number(raw)
    if value is None or value <= 0:
        return None
    if (unit or "").strip().lower().startswith("mi"):
        value = value * KM_PER_MILE
    return value.quantize(Decimal("0.1"))


def guess_kind(engine):
    """BEV, ICE, PHEV or HEV from aCar's free-text engine ('V6 GAS', '1.6 PLUG-IN HYBRID'). Blank is ICE: the
    person can change it before the car is made."""
    text = (engine or "").upper()
    if "PLUG" in text or "PHEV" in text:
        return "PHEV"
    if "HYBRID" in text or "HEV" in text:
        return "HEV"
    if "ELECTRIC" in text or re.search(r"\b(BEV|EV)\b", text):
        return "BEV"
    return "ICE"


def category_hint(event_type, subtypes):
    """The category name aCar's own type and sub-types point to, or None."""
    if event_type == "Purchased":
        return "Initial purchase"
    lower = [s.strip().lower() for s in subtypes]
    if TYRE_PURCHASE.intersection(lower):
        return "Tyres"
    if event_type == "Service" or SERVICE_EXPENSES.intersection(lower):
        return "Service"
    return next((SUBTYPE_CATEGORY[s] for s in lower if s in SUBTYPE_CATEGORY), None)


def _key(text):
    """A name as compared: case, American spellings and a trailing '(rear)' don't matter."""
    text = re.sub(r"\s*\([^)]*\)$", "", text.strip().lower())
    return re.sub(r"\btire(s?)\b", r"tyre\1", text.replace("windshield", "windscreen"))


def match_category(record, names):
    """The name in `names` this record belongs under: its hint, else a category named like one of its
    sub-types or its type ('Toll'), ignoring case and American spelling. None when there is none."""
    by_key = {_key(n): n for n in names}
    wanted = [record.category_hint] if record.category_hint else []
    wanted += record.subtypes + ([record.event_type] if record.event_type else [])
    return next((by_key[_key(c)] for c in wanted if _key(c) in by_key), None)


def catch_all(names):
    """The category in `names` meant for whatever fits no other, or None."""
    by_name = {n.strip().lower(): n for n in names}
    return next((by_name[n] for n in CATCH_ALL if n in by_name), None)


def split_subtypes(cell):
    """aCar joins sub-types with commas, and two of its names have one: 'Brakes, Rear' is one sub-type."""
    got = []
    for part in (p.strip() for p in (cell or "").split(",")):
        if part.lower() in ("front", "rear") and got and got[-1].lower().startswith("brake"):
            got[-1] += f" ({part.lower()})"
        elif part:
            got.append(part)
    return got


def match_service_item(subtype, names):
    """The service item in `names` that aCar's sub-type is, or None."""
    by_key = {_key(n): n for n in names}
    return by_key.get(_key(subtype))


def new_item_name(subtype):
    """What a service item made from aCar's sub-type is called: Australian spelling, sentence case,
    initials (PCV) left as they are."""
    words = []
    for i, word in enumerate(subtype.split()):
        if not (len(word) > 1 and word.isupper() and word.isalpha() and len(word) <= 5):
            word = re.sub(r"^tire", "tyre", word.lower().replace("windshield", "windscreen"))
            if i == 0:
                word = word[:1].upper() + word[1:]
        words.append(word)
    name = " ".join(words)
    return name[:MAX_ITEM] if len(name) > MAX_ITEM else name


def _clip(text, limit):
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def _tables(text):
    """-> {section name: (header row, [rows])} for the sections found."""
    tables, name, header = {}, None, None
    for row in csv.reader(io.StringIO(text)):
        if len(row) == 1 and row[0].strip() in SECTIONS:
            name, header = row[0].strip(), None
            tables[name] = ([], [])
            continue
        if name is None or not any(c.strip() for c in row):
            continue
        if header is None:
            header = [c.strip() for c in row]
            tables[name] = (header, [])
        else:
            tables[name][1].append(row)
    return tables


def _reader(header):
    """A function that gets a row's value by column name (the first column of that name)."""
    index = {}
    for i, name in enumerate(header):
        index.setdefault(name, i)

    def get(row, name):
        i = index.get(name)
        return row[i].strip() if i is not None and i < len(row) else ""
    return get


def parse(data, vehicle=None, fill_ups=True):
    """Read an export. `vehicle` keeps only that vehicle's records; `fill_ups=False` leaves fuel out.
    Raises NotAnExport when it isn't a records export."""
    text = _decode(data)
    tables = _tables(text)
    if "Fill-Up Records" not in tables and "Event Records" not in tables:
        raise NotAnExport("That doesn't look like an aCar export. Use the Records CSV that aCar writes "
                          "(it has Vehicles, Fill-Up Records and Event Records in one file).")
    meta_header, meta_rows = tables.get("Metadata", ([], []))
    date_format = (_reader(meta_header)(meta_rows[0], "Date Format") if meta_rows else "") or DEFAULT_DATE_FORMAT

    vehicles = []
    header, rows = tables.get("Vehicles", ([], []))
    get = _reader(header)
    for row in rows:
        if get(row, "Name"):
            vehicles.append({"name": get(row, "Name"), "make": get(row, "Make"), "model": get(row, "Model"),
                             "year": get(row, "Year"), "body_type": get(row, "Body Type"), "vin": get(row, "VIN"),
                             "engine": get(row, "Engine"), "kind": guess_kind(get(row, "Engine"))})

    left_out, records = Counter(), []

    def add(rec, name):
        if vehicle is None or name == vehicle:
            records.append(rec)

    header, rows = tables.get("Fill-Up Records", ([], []))
    get = _reader(header)
    for row in rows if fill_ups else []:
        name = get(row, "Vehicle")
        if vehicle is not None and name != vehicle:
            continue
        try:
            when = _date(get(row, "Date"), date_format)
        except ValueError:
            left_out["unreadable"] += 1
            continue
        amount = _number(get(row, "Total Cost")) or Decimal(0)
        volume, unit = _number(get(row, "Volume")), get(row, "Volume Unit") or "L"
        price = _number(get(row, "Price per Unit"))
        notes = ["aCar fill-up"]
        if volume:
            notes[0] += f": {format(volume.normalize(), 'f')} {unit}" + (f" at ${price}/{unit}" if price else "")
        if get(row, "Payment"):
            notes.append(f"Paid with {get(row, 'Payment')}")
        if get(row, "Notes"):
            notes.append(get(row, "Notes"))
        add(Record("fillup", name, when, _time(get(row, "Time")), amount, odometer_km=_odometer(
            get(row, "Odometer Reading"), get(row, "Distance Unit")), vendor=_clip(get(row, "Fuel Brand"), MAX_VENDOR),
            notes=_clip(". ".join(notes), MAX_NOTES), category_hint="Fuel"), name)

    header, rows = tables.get("Event Records", ([], []))
    get = _reader(header)
    for row in rows:
        name, kind = get(row, "Vehicle"), get(row, "Type")
        if vehicle is not None and name != vehicle:
            continue
        try:
            when = _date(get(row, "Date"), date_format)
        except ValueError:
            left_out["unreadable"] += 1
            continue
        amount = _number(get(row, "Total Cost")) or Decimal(0)
        subtypes = split_subtypes(get(row, "Sub Types"))
        notes = [f"aCar {kind.lower()}" + (f": {', '.join(subtypes)}" if subtypes else "")]
        if get(row, "Payment"):
            notes.append(f"Paid with {get(row, 'Payment')}")
        if get(row, "Notes"):
            notes.append(get(row, "Notes"))
        add(Record("sale" if kind == "Sold" else "event", name, when, _time(get(row, "Time")), amount, event_type=kind, subtypes=subtypes,
                   odometer_km=_odometer(get(row, "Odometer Reading"), get(row, "Distance Unit")),
                   vendor=_clip(get(row, "Place Name"), MAX_VENDOR), notes=_clip(". ".join(notes), MAX_NOTES),
                   category_hint=category_hint(kind, subtypes)), name)

    header, rows = tables.get("Trip Records", ([], []))
    get = _reader(header)
    left_out["trips"] += sum(1 for row in rows if vehicle is None or get(row, "Vehicle") == vehicle)
    if not left_out["trips"]:
        del left_out["trips"]
    return Export(vehicles, records, date_format, left_out)
