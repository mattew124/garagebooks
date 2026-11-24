"""Turn the submitted form into clean values, or a dict of per-field errors.

Pure functions, no database or Flask — everything the DB would otherwise reject
with an opaque constraint error is caught here with a message a human can act on.
"""

import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from . import leaseform

MAX_AMOUNT = Decimal("9999999999.99")  # numeric(12,2)
MAX_ODOMETER = 9_999_999.9  # numeric(9,1)
MAX_URL_LEN = 2048
MAX_TEXT_LEN = 500
MAX_NOTES_LEN = 2000
MAX_CATEGORY_LEN = 60
MAX_PDF_BYTES = 20 * 1024 * 1024
MAX_FILENAME_LEN = 120
MAX_FOLDER_NAME_LEN = 255
GUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
# Characters OneDrive refuses in file and folder names.
ONEDRIVE_FORBIDDEN = set('"*:<>?/\\|')


def parse_amount(raw):
    """'$1,234.5' -> Decimal('1234.50'); '-$3,000' -> Decimal('-3000.00'). Raises ValueError with a user message."""
    cleaned = (raw or "").replace("$", "").replace(",", "").replace(" ", "")
    if not cleaned:
        raise ValueError("Enter the amount paid.")
    try:
        value = Decimal(cleaned)
    except InvalidOperation:
        raise ValueError("Amount must be a number, e.g. 1234.56.") from None
    if not value.is_finite():
        raise ValueError("Amount must be a number, e.g. 1234.56.")
    # Negative amounts are rebates and refunds: they reduce the total.
    if value.copy_abs() > MAX_AMOUNT:  # before quantize(), which overflows on huge exponents; abs() overflows too
        raise ValueError("Amount is too large.")
    value = value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if abs(value) > MAX_AMOUNT:
        raise ValueError("Amount is too large.")
    return value


def parse_odometer(raw):
    """Optional. '57,197.3' -> 57197.3; blank -> None."""
    cleaned = (raw or "").replace(",", "").replace("km", "").strip()
    if not cleaned:
        return None
    try:
        value = float(cleaned)
    except ValueError:
        raise ValueError("Odometer must be a number of km.") from None
    if value != value or value in (float("inf"), float("-inf")):
        raise ValueError("Odometer must be a number of km.")
    if value < 0 or value > MAX_ODOMETER:
        raise ValueError("Odometer is out of range.")
    return round(value, 1)


def parse_invoice_url(raw):
    """Optional. Must be an https link: it is rendered as a clickable href, so
    javascript:, data: and plain http: are all refused."""
    url = (raw or "").strip()
    if not url:
        return None
    if len(url) > MAX_URL_LEN:
        raise ValueError("Link is too long.")
    if any(ch.isspace() for ch in url):
        raise ValueError("Link must not contain spaces. Copy the share link from your storage.")
    try:
        parts = urlparse(url)
    except ValueError:
        raise ValueError("That doesn't look like a valid link.") from None
    if parts.scheme.lower() != "https" or not parts.hostname:
        raise ValueError("Link must start with https:// (use your storage's Share → Copy link).")
    return url


MAX_PATH_LEN = 1024


def parse_invoice_path(raw):
    """Optional: an invoice kept in the folder on the server, as its path below that folder
    ('/Model 3/Tyres/261107 Tyres.pdf'). Only the shape is checked here; that it stays inside the
    folder is checked, symlinks and all, whenever the file is opened (storage.LocalFolder)."""
    path = raw or ""
    if not path.strip():
        return None
    parts = path[1:].split("/")
    if (not path.startswith("/") or len(path) > MAX_PATH_LEN or any(ord(ch) < 32 for ch in path)
            or any(part in ("", ".", "..") for part in parts) or not path.lower().endswith(".pdf")):
        raise ValueError("That isn't a PDF in the invoice folder.")
    return path


def parse_invoice_pdf(filename, data):
    """Optional upload. -> the bytes, or None when no file was chosen. Checks the
    content, not just the name: a PDF has its %PDF- header in the first 1 KiB."""
    if not filename and not data:
        return None
    if not data:
        raise ValueError("That file is empty.")
    if len(data) > MAX_PDF_BYTES:
        raise ValueError("Invoice PDF is too large (max 20 MB).")
    if b"%PDF-" not in data[:1024]:
        raise ValueError("Only PDF invoices can be uploaded.")
    return data


def parse_client_id(raw):
    """An Entra Application (client) ID: a GUID."""
    value = (raw or "").strip().lower()
    if not GUID.fullmatch(value):
        raise ValueError("Paste the Application (client) ID from the app's Overview page: "
                         "it looks like 1a2b3c4d-1a2b-1a2b-1a2b-1a2b3c4d5e6f.")
    return value


def parse_optional_date(raw):
    value = (raw or "").strip()
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ValueError("Date isn't valid.") from None


def parse_folder_name(raw):
    """A new OneDrive folder's name, checked against what OneDrive refuses."""
    name = (raw or "").strip()
    if not name:
        raise ValueError("Enter a name for the new folder.")
    if len(name) > MAX_FOLDER_NAME_LEN:
        raise ValueError(f"Folder name is too long (max {MAX_FOLDER_NAME_LEN} characters).")
    if any(ch in ONEDRIVE_FORBIDDEN or ord(ch) < 32 for ch in name):
        raise ValueError('Folder names can\'t contain \\ / : * ? " < > | or tabs.')
    if name.endswith("."):
        raise ValueError("Folder names can't end with a dot.")
    return name


def parse_upload_folder(raw):
    """The OneDrive subfolder for a category's invoices. Blank -> None (named after the category)."""
    name = (raw or "").strip()
    return parse_folder_name(name) if name else None


def safe_folder_name(name):
    """A category name as a legal OneDrive folder name (for categories with odd characters)."""
    cleaned = "".join("-" if ch in ONEDRIVE_FORBIDDEN or ord(ch) < 32 else ch for ch in name)
    return cleaned[:MAX_FOLDER_NAME_LEN].strip(" .") or "Invoices"


MAX_DATE_PATTERN_LEN = 20
_DATE_TOKENS = (("yyyy", "%Y"), ("yy", "%y"), ("mm", "%m"), ("dd", "%d"))
DEFAULT_DATE_PATTERN = "yymmdd"


def parse_date_pattern(raw):
    """The date part of uploaded invoice names, as typed on the Settings page: yyyy, yy, mm
    and dd with - _ . or a space between. Blank means the default (None)."""
    pattern = (raw or "").strip().lower()
    if not pattern:
        return None
    rest = pattern
    for token, _ in _DATE_TOKENS:
        rest = rest.replace(token, "")
    if (len(pattern) > MAX_DATE_PATTERN_LEN or rest.strip("-_. ") or len(rest) == len(pattern)
            or pattern == rest):
        raise ValueError("Use yyyy, yy, mm and dd, with - _ . or a space between, e.g. yymmdd or yyyy-mm-dd.")
    return pattern


# The file name template: %aliases for what is known about an expense, anything else as typed.
NAME_ALIASES = {
    "date": "the invoice date, in the date format above",
    "category": "the category, e.g. Tyres",
    "vendor": "the vendor or shop (left out when blank)",
    "car": "the car's name",
    "amount": "the amount paid, e.g. 1234.50",
}
DEFAULT_NAME_FORMAT = "%date %category - %vendor"
MAX_NAME_FORMAT_LEN = 100
_NAME_TOKEN = re.compile(r"(%%|%[a-z]+|%)")
_SEPARATOR_ONLY = re.compile(r"[\s\-_.,()]*")


# Which figure to take when an invoice offers several, e.g. a registration renewal for 6 or 12 months.
MULTI_AMOUNT_CHOICES = {
    "max": "Use the largest (e.g. the 12-month figure)",
    "min": "Use the smallest (e.g. the 6-month figure)",
    "blank": "Leave it blank for me to choose",
}


def parse_multi_amount(raw):
    choice = (raw or "").strip().lower()
    if choice not in MULTI_AMOUNT_CHOICES:
        raise ValueError("Choose one of the options.")
    return choice


def parse_name_format(raw):
    """The Settings 'Invoice file name' line. Blank -> None (the default). Raises
    ValueError saying what is wrong, naming the aliases that are allowed."""
    text = (raw or "").strip()
    if not text:
        return None
    allowed = ", ".join("%" + a for a in NAME_ALIASES)
    if len(text) > MAX_NAME_FORMAT_LEN:
        raise ValueError(f"Keep it under {MAX_NAME_FORMAT_LEN} characters.")
    used = set()
    for part in _NAME_TOKEN.split(text):
        if part == "%":
            raise ValueError("A lone % isn't allowed: write %% for a percent sign, or use an alias (" + allowed + ").")
        if part.startswith("%") and part != "%%":
            if part[1:] not in NAME_ALIASES:
                raise ValueError(f"Unknown alias {part}. Use {allowed}.")
            used.add(part[1:])
        elif not part.startswith("%") and any(ch in ONEDRIVE_FORBIDDEN or ord(ch) < 32 for ch in part):
            raise ValueError('File names can\'t contain \\ / : * ? " < > | here: remove "'
                             + next(ch for ch in part if ch in ONEDRIVE_FORBIDDEN or ord(ch) < 32) + '".')
    if "date" not in used:
        raise ValueError("Include %date so files sort by date.")
    if not used & {"category", "vendor"}:
        raise ValueError("Include %category or %vendor so files can be told apart.")
    return text


def invoice_filename(occurred_at, category, vendor, date_pattern=None, name_format=None, car=None, amount=None):
    """'261107 Tyres - Tyre Shop.pdf' by default; the Settings template (`name_format`, see
    parse_name_format) otherwise. A blank alias is dropped together with the separator just
    before it, so '%date - %category - %vendor' without a vendor is '261107 - Tyres.pdf'. Values
    are made legal OneDrive names whatever they hold. `date_pattern` is as parse_date_pattern."""
    fmt = date_pattern or DEFAULT_DATE_PATTERN
    for token, code in _DATE_TOKENS:
        fmt = fmt.replace(token, code)
    values = {"date": occurred_at.strftime(fmt), "category": category or "", "vendor": vendor or "",
              "car": car or "", "amount": "" if amount is None else f"{amount:.2f}"}
    parts = _NAME_TOKEN.split(name_format or DEFAULT_NAME_FORMAT)   # literal, token, literal, ...
    out = []
    for i, part in enumerate(parts):
        if part == "%%":
            out.append("%")
        elif part.startswith("%") and part[1:] in values:
            value = values[part[1:]]
            if not value:
                while out and out[-1] == "":
                    out.pop()
                if out and _SEPARATOR_ONLY.fullmatch(out[-1]):
                    out.pop()        # the separator before an empty alias goes with it
                elif not out and i + 1 < len(parts) and _SEPARATOR_ONLY.fullmatch(parts[i + 1]):
                    parts[i + 1] = ""    # ...or the one after it, when it comes first
            out.append(value)
        else:
            out.append(part)
    stem = "".join(out)
    stem = "".join("-" if ch in ONEDRIVE_FORBIDDEN or ord(ch) < 32 else ch for ch in stem)
    stem = stem[:MAX_FILENAME_LEN - 4].rstrip(" .")
    return stem + ".pdf"


def parse_local_datetime(date_raw, time_raw, tz):
    """'2026-11-07' + '14:05' in tz -> aware datetime."""
    date_raw, time_raw = (date_raw or "").strip(), (time_raw or "").strip()
    if not date_raw:
        raise ValueError("Enter the date.")
    if not time_raw:
        raise ValueError("Enter the time.")
    try:
        parsed = datetime.strptime(f"{date_raw} {time_raw[:5]}", "%Y-%m-%d %H:%M")
    except ValueError:
        raise ValueError("Date or time isn't valid.") from None
    return parsed.replace(tzinfo=ZoneInfo(tz))


def _text(raw, limit, label):
    value = (raw or "").strip()
    if len(value) > limit:
        raise ValueError(f"{label} is too long (max {limit} characters).")
    return value or None


def parse_category_name(raw):
    name = " ".join((raw or "").split())
    if not name:
        raise ValueError("Enter a category name.")
    if len(name) > MAX_CATEGORY_LEN:
        raise ValueError(f"Category name is too long (max {MAX_CATEGORY_LEN} characters).")
    return name


MAX_SERVICE_ITEM_LEN = 60


def parse_service_item_name(raw):
    name = " ".join((raw or "").split())
    if not name:
        raise ValueError("Enter a name for the item.")
    if "\x00" in name:
        raise ValueError("The name can't contain that character.")
    if len(name) > MAX_SERVICE_ITEM_LEN:
        raise ValueError(f"The name is too long (max {MAX_SERVICE_ITEM_LEN} characters).")
    return name


def parse_service_ids(raw_ids, valid_ids):
    """Item ids ticked on a form: those that are real items, once each. Anything else is ignored (a stale page)."""
    got = []
    for raw in raw_ids:
        try:
            number = int(raw)
        except (TypeError, ValueError):
            continue
        if number in valid_ids and number not in got:
            got.append(number)
    return got


MAX_CAR_NAME_LEN = 100
MAX_CAR_TEXT_LEN = 60


_VIN = re.compile(r"[A-HJ-NPR-Z0-9]{17}")
MAX_BODY_TYPE_LEN = 40


def parse_car_extras(get):
    """A car's VIN, build year, model year and body type, from `get(field)` (raw text or None) -> (values, errors).
    All optional: blank is None. The VIN is upper case without spaces."""
    values, errors = {}, {}
    vin = re.sub(r"\s+", "", get("vin") or "").upper()
    if vin and not _VIN.fullmatch(vin):
        errors["vin"] = "A VIN is 17 letters and digits (no I, O or Q)."
    values["vin"] = vin or None
    for field, label in (("build_year", "The year built"), ("model_year", "The model year")):
        raw = (get(field) or "").strip()
        try:
            year = int(raw) if raw else None
            if year is not None and not 1900 <= year <= 2100:
                raise ValueError
        except ValueError:
            errors[field] = f"{label} is a year such as 2025."
            year = None
        values[field] = year
    body = " ".join((get("body_type") or "").split())
    if len(body) > MAX_BODY_TYPE_LEN:
        errors["body_type"] = f"The body type is too long (max {MAX_BODY_TYPE_LEN} characters)."
    values["body_type"] = body or None
    return values, errors


def parse_car_form(form, kinds):
    """A car's name, type (one of `kinds`), make and model. -> (values, errors). The name is optional:
    a car without a nickname is called by its make and model, so one of the two must be there."""
    values, errors = {}, {}
    name = " ".join((form.get("name") or "").split())
    if len(name) > MAX_CAR_NAME_LEN:
        errors["name"] = f"The name is too long (max {MAX_CAR_NAME_LEN} characters)."
    values["kind"] = (form.get("kind") or "").strip()
    if values["kind"] not in kinds:
        errors["kind"] = "Choose the type of car."
    for field, label in (("make", "The make"), ("model", "The model")):
        text = " ".join((form.get(field) or "").split())
        if len(text) > MAX_CAR_TEXT_LEN:
            errors[field] = f"{label} is too long (max {MAX_CAR_TEXT_LEN} characters)."
        values[field] = text or None
    values["name"] = name or car_name(values["make"], values["model"])
    if not values["name"]:
        errors["name"] = "Enter the make and model, or a name for the car."
    extras, more = parse_car_extras(form.get)
    return {**values, **extras}, {**errors, **more}


def car_name(make, model):
    """What a car with no name of its own is called: its make and model ('Tesla Model Y'), or '' with neither."""
    return " ".join(part for part in (make, model) if part)


def parse_purchase(form, tz, now):
    """What the car cost to buy, as entered on the Cars page: the price, the date and, optionally, the odometer
    then. -> (purchase or None, errors); None when no price is entered, which leaves the purchase as it is."""
    price_raw = (form.get("purchase_price") or "").strip()
    if not price_raw:
        return None, {}
    values, errors = {}, {}
    try:
        values["amount"] = parse_amount(price_raw)
        if values["amount"] < 0:
            raise ValueError("The purchase price can't be negative.")
    except ValueError as exc:
        errors["purchase_price"] = str(exc)
    try:
        if not (form.get("purchase_date") or "").strip():
            raise ValueError("Enter the date you bought it.")
        values["occurred_at"] = parse_local_datetime(form.get("purchase_date"), "12:00", tz)
        if values["occurred_at"].date() > now.date():
            raise ValueError("The purchase date can't be in the future.")
    except ValueError as exc:
        errors["purchase_date"] = str(exc)
    try:
        values["odometer_km"] = parse_odometer(form.get("purchase_km"))
    except ValueError as exc:
        errors["purchase_km"] = str(exc)
    return values, errors


def parse_sale(form, tz, now, km_required=False):
    """A sale: the date it sold (a day in `tz`, not in the future), the price and the final odometer
    (`km_required` for a car with no other source for it). -> (values, errors). All blank is no sale
    (values None). The moment saved is the end of that day, or now for today."""
    date_raw, price_raw = (form.get("date") or "").strip(), (form.get("price") or "").strip()
    km_raw = (form.get("km") or "").strip()
    if not date_raw and not price_raw and not km_raw:
        return {"sold_at": None, "sale_price": None, "sale_km": None}, {}
    errors, values = {}, {}
    try:
        values["sale_km"] = parse_odometer(km_raw)
        if values["sale_km"] is None and km_required:
            raise ValueError("Enter the odometer reading when it sold.")
    except ValueError as exc:
        errors["sold_km"] = str(exc)
    try:
        if not date_raw:
            raise ValueError("Enter the date it sold.")
        sold_at = parse_local_datetime(date_raw, "23:59", tz).replace(second=59)
        if sold_at.date() > now.date():
            raise ValueError("The sale date can't be in the future.")
        values["sold_at"] = min(sold_at, now.replace(microsecond=0))
    except ValueError as exc:
        errors["sold_date"] = str(exc)
    try:
        if not price_raw:
            raise ValueError("Enter what it sold for (0 if it was given away).")
        price = parse_amount(price_raw)
        if price < 0:
            raise ValueError("The sale price can't be negative.")
        values["sale_price"] = price
    except ValueError as exc:
        errors["sold_price"] = str(exc)
    return values, errors


def parse_url_setting(raw):
    """A blank clears the setting (None). Otherwise an http(s) URL with no spaces."""
    url = (raw or "").strip()
    if not url:
        return None
    parts = urlparse(url)
    if parts.scheme not in ("http", "https") or not parts.netloc or any(c.isspace() for c in url):
        raise ValueError("Enter a full address starting with http:// or https://")
    if len(url) > MAX_URL_LEN:
        raise ValueError("That address is too long.")
    return url


PAID_FROM = {"auto": None, "package": True, "personal": False}


def parse_paid_from(raw):
    """Where an expense was paid from: automatic (None), the package (True) or personally (False)."""
    key = (raw or "").strip() or "auto"
    if key not in PAID_FROM:
        raise ValueError("Choose where this was paid from: automatic, the package or personally.")
    return PAID_FROM[key]


MAX_NOTIFY_URL_LEN = 500       # the same limit as the column's CHECK
NOTIFY_FORMATS = ("ntfy", "json")


def parse_notify_url(raw):
    """The Settings 'Notification address'. Blank -> None (notifications off). Otherwise a full http(s)
    address with a host, no spaces or control characters, at most 500 characters (it may carry a login:
    https://user:password@host/topic)."""
    url = (raw or "").strip()
    if not url:
        return None
    try:
        parts = urlparse(url)
        usable = parts.scheme in ("http", "https") and bool(parts.hostname)
    except ValueError:
        usable = False
    if not usable or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in url):
        raise ValueError("Enter a full address starting with http:// or https://")
    if len(url) > MAX_NOTIFY_URL_LEN:
        raise ValueError("That address is too long (500 characters at most).")
    return url


def parse_notify_format(raw):
    """The Settings notification format: 'ntfy' or 'json'. Blank means ntfy."""
    choice = (raw or "").strip(" ").lower() or "ntfy"      # only spaces are forgiven, not a newline
    if choice not in NOTIFY_FORMATS:
        raise ValueError("Choose ntfy or JSON.")
    return choice


MAX_PAYMENT_REF_LEN = 60
_PAYMENT_REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9 .\-/]*")


def parse_payment_ref(raw, label):
    """A card payment reference or BPAY code as printed: letters, digits and a few separators. Blank -> None."""
    text = " ".join((raw or "").split())
    if not text:
        return None
    if len(text) > MAX_PAYMENT_REF_LEN:
        raise ValueError(f"{label} is too long (max {MAX_PAYMENT_REF_LEN} characters).")
    if not _PAYMENT_REF.fullmatch(text):
        raise ValueError(f"{label} can only hold letters, digits, spaces and . - /")
    return text


def parse_gst_amount(raw, amount):
    """The GST the document states: blank -> None. Money between nothing and the amount (the amount's own sign)."""
    if not (raw or "").strip():
        return None
    gst = parse_amount(raw)
    if amount is not None and not (min(Decimal(0), amount) <= gst <= max(Decimal(0), amount)):
        raise ValueError("GST must be between nothing and the amount.")
    return gst


def parse_expense_form(form, tz, valid_car_ids, valid_category_ids):
    """-> (values, errors). `values` is only meaningful when `errors` is empty."""
    values, errors = {}, {}

    def attempt(field, fn, *args):
        try:
            values[field] = fn(*args)
        except ValueError as exc:
            errors[field] = str(exc)

    def pick(field, raw, valid, message):
        try:
            number = int(raw)
        except (TypeError, ValueError):
            number = None
        if number in valid:
            values[field] = number
        else:
            errors[field] = message

    pick("car_id", form.get("car_id"), valid_car_ids, "Choose a car.")
    pick("category_id", form.get("category_id"), valid_category_ids, "Choose a category.")
    attempt("amount", parse_amount, form.get("amount"))
    attempt("odometer_km", parse_odometer, form.get("odometer_km"))
    attempt("invoice_url", parse_invoice_url, form.get("invoice_url"))
    attempt("invoice_path", parse_invoice_path, form.get("invoice_path"))
    attempt("vendor", _text, form.get("vendor"), MAX_TEXT_LEN, "Vendor")
    attempt("notes", _text, form.get("notes"), MAX_NOTES_LEN, "Notes")
    attempt("packaged", parse_paid_from, form.get("packaged"))
    attempt("period_start", parse_optional_date, form.get("period_start"))
    attempt("period_end", parse_optional_date, form.get("period_end"))
    attempt("gst_amount", parse_gst_amount, form.get("gst_amount"), values.get("amount"))
    for field, label in (("card_payment_ref", "Card payment reference"), ("bpay_biller_code", "BPAY biller code"),
                         ("bpay_ref", "BPAY reference")):
        attempt(field, parse_payment_ref, form.get(field), label)
    start, end = values.get("period_start"), values.get("period_end")
    if start and end and end < start:
        errors["period_end"] = "The period's end can't be before its start."

    try:
        values["occurred_at"] = parse_local_datetime(form.get("date"), form.get("time"), tz)
    except ValueError as exc:
        errors["date"] = str(exc)
    return values, errors


# ---- a lease (roadmap L1) ----------------------------------------------------------

MAX_LEASE_TEXT_LEN = 100
# The documents a lease keeps for reference: the provider's form its details were read from, and the agreement.
LEASE_DOCUMENTS = {"form": "Lease form", "agreement": "Lease agreement"}


def _non_negative_money(raw, label):
    """Blank -> None. Otherwise dollars, never negative."""
    if not (raw or "").strip():
        return None
    value = parse_amount(raw)
    if value < 0:
        raise ValueError(f"{label[0].upper() + label[1:]} can't be negative.")
    return value


def _percent(raw, label, places):
    """Blank -> None. Otherwise 0 to 100, as typed ('20', '46.875%')."""
    cleaned = (raw or "").replace("%", "").replace(",", "").strip()
    if not cleaned:
        return None
    try:
        value = Decimal(cleaned)
    except InvalidOperation:
        raise ValueError(f"{label} must be a number, e.g. 20.") from None
    if not value.is_finite() or value < 0 or value > 100:
        raise ValueError(f"{label} must be between 0 and 100.")
    return value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)


def parse_lease_form(form, pools, frequencies, fbt_methods, tax_treatments, budgets_on=True, electric=True):
    """A lease as typed from the provider's statement. -> (values, errors); `values` holds the lease
    columns plus `budgets` ({pool: yearly amount}, blanks left out; None when lease budgets are turned
    off, which leaves any already saved as they are). Whether it overlaps another lease of the car is
    for the caller to check: it needs the database.

    An associate lease (the car is owned outright and leased to the employer by an associate) has no
    financier to name and no residual. The end date and the term fill each other in: an empty end date
    is the start plus the term, and an empty term is the whole months between the two dates."""
    values, errors = {}, {}
    associate = values["associate_lease"] = form.get("associate_lease") == "on"

    def attempt(field, fn, *args):
        try:
            values[field] = fn(*args)
        except ValueError as exc:
            errors[field] = str(exc)

    def text(field, label, required=False):
        raw = " ".join((form.get(field) or "").split())
        if len(raw) > MAX_LEASE_TEXT_LEN:
            errors[field] = f"{label} is too long (max {MAX_LEASE_TEXT_LEN} characters)."
        elif required and not raw:
            errors[field] = f"Enter {label[0].lower() + label[1:]}."
        values[field] = raw or None

    def choice(field, options, message, default=None):
        raw = (form.get(field) or "").strip() or default
        if raw is None or raw in options:
            values[field] = raw
        else:
            errors[field] = message

    def required_date(field, label):
        attempt(field, parse_optional_date, form.get(field))
        if values.get(field) is None and field not in errors:
            errors[field] = f"Enter the {label}."

    def money(field, label):
        attempt(field, _non_negative_money, form.get(field), label)

    text("package_name", "The package name")
    text("financier", "The financier", required=not associate)
    required_date("start_date", "start date")
    term_raw = (form.get("term_months") or "").strip()
    try:
        term = int(term_raw) if term_raw else None
        if term is not None and not 1 <= term <= 120:
            raise ValueError
        values["term_months"] = term
    except ValueError:
        term = None
        errors["term_months"] = "Enter the term in months, from 1 to 120."
    if not (form.get("end_date") or "").strip() and values.get("start_date") and term:
        values["end_date"] = leaseform.end_date(values["start_date"], term)
    else:
        required_date("end_date", "end date" if term_raw else "end date, or the term in months")
    attempt("refinance_date", parse_optional_date, form.get("refinance_date"))
    start, end, refi = values.get("start_date"), values.get("end_date"), values.get("refinance_date")
    if start and end and end <= start:
        errors["end_date"] = "The end date must be after the start date."
    if refi and start and end and not (start <= refi <= end):
        errors["refinance_date"] = "The refinance date must fall within the lease."

    if not term_raw:
        if start and end and end > start:
            values["term_months"] = min(leaseform.term_months(start, end), 120)
        elif "end_date" not in errors and "start_date" not in errors:
            errors["term_months"] = "Enter the term in months, from 1 to 120."

    choice("pay_frequency", frequencies, "Choose how often the lease is paid.")
    if values.get("pay_frequency") is None and "pay_frequency" not in errors:
        errors["pay_frequency"] = "Choose how often the lease is paid."
    for field, label in (("pretax_payment", "the pre-tax payment"), ("posttax_payment", "the post-tax payment"),
                         ("admin_fee", "the admin fee")):
        money(field, label)
        values[field] = values[field] if values.get(field) is not None else Decimal("0.00")
    if not errors.keys() & {"pretax_payment", "posttax_payment"} \
            and not (values["pretax_payment"] or values["posttax_payment"]):
        errors["pretax_payment"] = "Enter the payment, pre-tax or post-tax."

    money("car_cost", "the car's cost")
    money("residual_amount", "the residual amount")
    attempt("residual_percent", _percent, form.get("residual_percent"), "The residual percentage", 3)
    if associate:        # no balloon on an associate lease: whatever was in the hidden boxes is dropped
        values["residual_amount"] = values["residual_percent"] = None
        errors.pop("residual_amount", None)
        errors.pop("residual_percent", None)
    cost, amount, percent = values.get("car_cost"), values.get("residual_amount"), values.get("residual_percent")
    if cost is not None and amount is not None and amount > cost:
        errors["residual_amount"] = "The residual can't be more than the car's cost."
    elif cost and amount is not None and percent is not None and abs(cost * percent / 100 - amount) > Decimal("1.00"):
        errors["residual_percent"] = "The residual amount and percentage don't match for this cost."

    money("fbt_base_value", "the FBT base value")
    choice("fbt_method", fbt_methods, "Choose the valuation method.", default="statutory")
    attempt("statutory_percent", _percent, form.get("statutory_percent"), "The statutory percentage", 2)
    if values.get("statutory_percent") is None and "statutory_percent" not in errors:
        values["statutory_percent"] = Decimal("20.00")
    values["ev_exempt"] = form.get("ev_exempt") == "on"
    # The ATO's cents-per-km rate is for a battery electric car; anything else's tick is dropped.
    values["charging_reimbursed"] = electric and form.get("charging_reimbursed") == "on"
    choice("tax_treatment", tax_treatments, "Choose the tax treatment.")
    text("tax_treatment_note", "The note")
    if values.get("tax_treatment") != "other":
        values["tax_treatment_note"] = None

    km_raw = (form.get("estimated_km_per_year") or "").replace(",", "").replace("km", "").strip()
    values["estimated_km_per_year"] = None
    if km_raw:
        try:
            km = int(km_raw)
            if not 0 <= km <= 1_000_000:
                raise ValueError
            values["estimated_km_per_year"] = km
        except ValueError:
            errors["estimated_km_per_year"] = "Enter the estimated km per year as a whole number."
    attempt("rego_expiry", parse_optional_date, form.get("rego_expiry"))
    values["rego_renewed_by_provider"] = form.get("rego_renewed_by_provider") == "on"

    # The documents kept with the lease: each a link, or a path in the invoice folder, or neither.
    for doc in LEASE_DOCUMENTS:
        attempt(f"{doc}_url", parse_invoice_url, form.get(f"{doc}_url"))
        attempt(f"{doc}_path", parse_invoice_path, form.get(f"{doc}_path"))
        if form.get(f"remove_{doc}"):
            values[f"{doc}_url"] = values[f"{doc}_path"] = None     # the file itself stays where it is

    values["budgets"] = None
    if budgets_on:
        budgets = {}
        for pool in pools:
            try:
                amount_ = _non_negative_money(form.get(f"budget_{pool}"), "the yearly budget")
            except ValueError as exc:
                errors[f"budget_{pool}"] = str(exc)
                continue
            if amount_ is not None:
                budgets[pool] = amount_
        values["budgets"] = budgets
    return values, errors


# ---- reminders (roadmap item 4) -----------------------------------------------------

MAX_REMINDER_TITLE_LEN = 100


def _whole(raw, lo, hi, message, default=None):
    """Blank -> `default`. Otherwise a whole number from lo to hi, else ValueError(message)."""
    text = (raw or "").strip()
    if not text:
        return default
    if not text.isdigit() or not lo <= int(text) <= hi:
        raise ValueError(message)
    return int(text)


def _km_figure(raw, message):
    """Blank -> None. A distance in km, 0 or more, else ValueError(message)."""
    try:
        return parse_odometer(raw)
    except ValueError:
        raise ValueError(message) from None


def parse_reminder_form(form, car_ids, category_ids, car_id=None, service_item_ids=frozenset()):
    """A reminder as typed: a title, a due date and/or odometer reading, lead times, and how often it
    repeats. `car_id` fixes the car (editing); otherwise the form's own must be one of `car_ids`.
    -> (values, errors); `values` has every column of the reminder, blank lead times at their defaults."""
    values, errors = {}, {}

    def attempt(field, fn, *args):
        try:
            values[field] = fn(*args)
        except ValueError as exc:
            errors[field] = str(exc)
            values[field] = None

    if car_id is None:
        raw = (form.get("car_id") or "").strip()
        car_id = int(raw) if raw.isdigit() and int(raw) in car_ids else None
        if car_id is None:
            errors["car_id"] = "Choose the car."
    values["car_id"] = car_id
    title = " ".join((form.get("title") or "").split())
    if not title:
        errors["title"] = "Enter what the reminder is for, such as Tyre rotation."
    elif len(title) > MAX_REMINDER_TITLE_LEN:
        errors["title"] = f"The title is too long (max {MAX_REMINDER_TITLE_LEN} characters)."
    values["title"] = title
    raw = (form.get("category_id") or "").strip()
    if raw and not (raw.isdigit() and int(raw) in category_ids):
        errors["category_id"] = "Choose a category from the list."
    values["category_id"] = int(raw) if raw.isdigit() and int(raw) in category_ids else None
    raw = (form.get("service_item_id") or "").strip()
    if raw and not (raw.isdigit() and int(raw) in service_item_ids):
        errors["service_item_id"] = "Choose a service item from the list."
    values["service_item_id"] = int(raw) if raw.isdigit() and int(raw) in service_item_ids else None
    attempt("due_date", parse_optional_date, form.get("due_date"))
    attempt("due_km", parse_odometer, form.get("due_km"))
    if "due_date" not in errors and "due_km" not in errors and values["due_date"] is None and values["due_km"] is None:
        errors["due_date"] = "Enter a due date, an odometer reading or both."
    attempt("lead_days", _whole, form.get("lead_days"), 0, 3650,
            "The lead time must be a whole number of days, 0 to 3650.", 30)
    attempt("lead_km", _km_figure, form.get("lead_km"), "The lead distance must be a number of km, 0 or more.")
    if values["lead_km"] is None and "lead_km" not in errors:
        values["lead_km"] = 1000
    attempt("every_months", _whole, form.get("every_months"), 1, 600,
            "Repeat must be a whole number of months, 1 to 600.")
    attempt("every_km", _km_figure, form.get("every_km"), "Repeat must be a number of km, more than 0.")
    if values["every_km"] is not None and values["every_km"] <= 0:
        errors["every_km"] = "Repeat must be a number of km, more than 0."
    notes = (form.get("notes") or "").strip()
    if len(notes) > MAX_NOTES_LEN:
        errors["notes"] = f"The notes are too long (max {MAX_NOTES_LEN} characters)."
    values["notes"] = notes or None
    return values, errors


def parse_resolve_form(form, today):
    """How a reminder was dealt with: the expense that did it (`expense_id`), or, without one, the date
    and optionally the odometer. -> (values, errors) with expense_id, date, km."""
    values, errors = {"expense_id": None, "date": None, "km": None}, {}
    raw = (form.get("expense_id") or "").strip()
    if raw:
        if raw.isdigit() and int(raw) < 2 ** 63:
            values["expense_id"] = int(raw)
        else:
            errors["expense_id"] = "Choose one of this car's expenses."
        return values, errors
    try:
        values["date"] = parse_optional_date(form.get("date"))
        if values["date"] is None:
            errors["date"] = "Enter the date it was done, or choose the expense that did it."
        elif values["date"] > today:
            errors["date"] = "That date is in the future."
    except ValueError as exc:
        errors["date"] = str(exc)
    try:
        values["km"] = parse_odometer(form.get("km"))
    except ValueError as exc:
        errors["km"] = str(exc)
    return values, errors
ACTOR_MAX = 200


def parse_actor(raw):
    """The signed-in user as the sign-in gate names them in a request header, for the change history:
    control characters dropped, ends trimmed, cut to ACTOR_MAX. None when nothing is left."""
    if not raw:
        return None
    cleaned = "".join(ch for ch in raw if ch.isprintable()).strip()[:ACTOR_MAX].strip()
    return cleaned or None
