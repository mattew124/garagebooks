"""Turn the submitted form into clean values, or a dict of per-field errors.

Pure functions, no database or Flask — everything the DB would otherwise reject
with an opaque constraint error is caught here with a message a human can act on.
"""

import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

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
    if abs(value) > MAX_AMOUNT:  # before quantize(), which overflows on huge exponents
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


MAX_CAR_NAME_LEN = 100
MAX_CAR_TEXT_LEN = 60


def parse_car_form(form, kinds):
    """A car's name, type (one of `kinds`), make and model. -> (values, errors)."""
    values, errors = {}, {}
    name = " ".join((form.get("name") or "").split())
    if not name:
        errors["name"] = "Enter a name for the car."
    elif len(name) > MAX_CAR_NAME_LEN:
        errors["name"] = f"The name is too long (max {MAX_CAR_NAME_LEN} characters)."
    values["name"] = name
    values["kind"] = (form.get("kind") or "").strip()
    if values["kind"] not in kinds:
        errors["kind"] = "Choose the type of car."
    for field, label in (("make", "The make"), ("model", "The model")):
        text = " ".join((form.get(field) or "").split())
        if len(text) > MAX_CAR_TEXT_LEN:
            errors[field] = f"{label} is too long (max {MAX_CAR_TEXT_LEN} characters)."
        values[field] = text or None
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

    try:
        values["occurred_at"] = parse_local_datetime(form.get("date"), form.get("time"), tz)
    except ValueError as exc:
        errors["date"] = str(exc)
    return values, errors


# ---- a lease (roadmap L1) ----------------------------------------------------------

MAX_LEASE_TEXT_LEN = 100


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


def parse_lease_form(form, pools, frequencies, fbt_methods, tax_treatments):
    """A lease as typed from the provider's statement. -> (values, errors); `values` holds the lease
    columns plus `budgets` ({pool: yearly amount}, blanks left out). Whether it overlaps another lease
    of the car is for the caller to check: it needs the database."""
    values, errors = {}, {}

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
    text("financier", "The financier", required=True)
    required_date("start_date", "start date")
    required_date("end_date", "end date")
    attempt("refinance_date", parse_optional_date, form.get("refinance_date"))
    start, end, refi = values.get("start_date"), values.get("end_date"), values.get("refinance_date")
    if start and end and end <= start:
        errors["end_date"] = "The end date must be after the start date."
    if refi and start and end and not (start <= refi <= end):
        errors["refinance_date"] = "The refinance date must fall within the lease."

    try:
        term = int((form.get("term_months") or "").strip())
        if not 1 <= term <= 120:
            raise ValueError
        values["term_months"] = term
    except ValueError:
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
