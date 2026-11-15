"""Property-based tests (Hypothesis) for everything that parses what a person or a PDF supplies.

The properties, for each parser: it never raises anything but the ValueError it documents, whatever
text it is given; what it accepts has the shape the rest of the app relies on; and a value written
the way the app writes it reads back unchanged. Routes are in test_properties_routes.py.
"""

import string
from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from hypothesis import example, given, strategies as st

from garagebooks import invoices, updates, validation as v

# Arbitrary text, and text made of the characters these fields are really full of.
junk = st.text(max_size=80)
nasty = st.text(alphabet=st.sampled_from(list("0123456789.,-+$ eE%/\\:*?\"<>|\t\n\x00  ٠xX_")), max_size=40)
anything = st.one_of(st.none(), junk, nasty)
amounts = st.decimals(min_value=-v.MAX_AMOUNT, max_value=v.MAX_AMOUNT, places=2)
FORBIDDEN = v.ONEDRIVE_FORBIDDEN


def only_value_error(fn, *args):
    """fn(*args), or None when it refuses with a ValueError: any other exception fails the test."""
    try:
        return fn(*args)
    except ValueError:
        return None


# ---- amounts, odometer -----------------------------------------------------------------------

@given(anything)
@example("1e999999999")
@example("-0")
@example("９９")
def test_amount_accepts_a_number_or_refuses_politely(raw):
    got = only_value_error(v.parse_amount, raw)
    if got is not None:
        assert got == got.quantize(Decimal("0.01")) and abs(got) <= v.MAX_AMOUNT and got.is_finite()


@given(amounts, st.sampled_from(["{:.2f}", "${:,.2f}", " $ {:,.2f} ", "{:,.2f}"]))
def test_amount_round_trips_how_the_app_shows_it(value, pattern):
    text = pattern.format(abs(value))
    assert v.parse_amount(("-" if value < 0 else "") + text.strip()) == value


@given(anything)
def test_odometer_accepts_a_distance_or_refuses_politely(raw):
    got = only_value_error(v.parse_odometer, raw)
    if got is not None:
        assert 0 <= got <= v.MAX_ODOMETER


@given(st.integers(0, int(v.MAX_ODOMETER * 10)).map(lambda n: n / 10), st.booleans())
def test_odometer_round_trips(value, with_unit):
    assert v.parse_odometer(f"{value:,.1f}" + (" km" if with_unit else "")) == value


# ---- links, paths, uploads, ids ---------------------------------------------------------------

@given(anything)
@example("https://[")
@example("https:// x")
def test_invoice_url_is_only_ever_a_plain_https_link(raw):
    got = only_value_error(v.parse_invoice_url, raw)
    if got is not None:
        assert got.lower().startswith("https://") and len(got) <= v.MAX_URL_LEN
        assert not any(ch.isspace() for ch in got)


@given(st.from_regex(r"[a-z0-9]{1,12}(\.[a-z]{2,6}){1,2}", fullmatch=True),
       st.text(string.ascii_letters + "/-_%?=&#", max_size=40))
def test_invoice_url_round_trips(host, rest):
    assert v.parse_invoice_url(f"https://{host}/{rest}") == f"https://{host}/{rest}"


@given(anything)
@example("/../x.pdf")
@example("/a//b.pdf")
@example("/a/./b.pdf")
@example("/a/b.pdf\n")
def test_invoice_path_never_leaves_the_folder(raw):
    got = only_value_error(v.parse_invoice_path, raw)
    if got is not None:
        parts = got[1:].split("/")
        assert got.startswith("/") and got.lower().endswith(".pdf")
        assert all(part not in ("", ".", "..") for part in parts)
        assert not any(ord(ch) < 32 for ch in got)


@given(st.lists(st.text(string.ascii_letters + string.digits + " -_", min_size=1, max_size=12), min_size=1, max_size=4))
def test_invoice_path_round_trips(parts):
    path = "/" + "/".join(parts) + ".pdf"
    assert v.parse_invoice_path(path) == path


@given(st.one_of(st.none(), junk), st.binary(max_size=3000))
def test_pdf_upload_is_refused_unless_it_is_a_pdf(filename, data):
    got = only_value_error(v.parse_invoice_pdf, filename, data)
    if got is not None:
        assert got == data and b"%PDF-" in data[:1024]


@given(st.binary(max_size=500), st.binary(max_size=500))
def test_pdf_upload_accepts_anything_with_the_header(before, after):
    data = b"%PDF-" + before + after
    assert v.parse_invoice_pdf("x.pdf", data) == data


@given(anything)
def test_client_id_is_only_ever_a_guid(raw):
    got = only_value_error(v.parse_client_id, raw)
    if got is not None:
        assert UUID(got) and got == got.lower()


@given(st.uuids(), st.booleans(), st.sampled_from(["", " ", "\n"]))
def test_client_id_round_trips(uid, upper, pad):
    text = str(uid).upper() if upper else str(uid)
    assert v.parse_client_id(pad + text + pad) == str(uid)


# ---- dates and times ---------------------------------------------------------------------------

@given(anything)
def test_optional_date_is_a_date_or_none(raw):
    got = only_value_error(v.parse_optional_date, raw)
    assert got is None or isinstance(got, date)


@given(st.dates())
def test_optional_date_round_trips(day):
    assert v.parse_optional_date(day.isoformat()) == day


@given(anything, anything)
def test_local_datetime_is_aware_or_refused(day, clock):
    got = only_value_error(v.parse_local_datetime, day, clock, "Australia/Brisbane")
    if got is not None:
        assert got.utcoffset() is not None


@given(st.datetimes(min_value=datetime(1900, 1, 1)), st.sampled_from(["Australia/Brisbane", "UTC", "America/New_York"]))
def test_local_datetime_round_trips(moment, tz):
    got = v.parse_local_datetime(moment.strftime("%Y-%m-%d"), moment.strftime("%H:%M"), tz)
    assert (got.year, got.month, got.day, got.hour, got.minute) == (moment.year, moment.month, moment.day,
                                                                    moment.hour, moment.minute)


# ---- names and settings ------------------------------------------------------------------------

@given(anything)
def test_folder_name_is_one_legal_name(raw):
    got = only_value_error(v.parse_folder_name, raw)
    if got is not None:
        assert got and len(got) <= v.MAX_FOLDER_NAME_LEN and not got.endswith(".")
        assert not any(ch in FORBIDDEN or ord(ch) < 32 for ch in got)
        assert only_value_error(v.parse_upload_folder, raw) == got


@given(st.text(max_size=400))
def test_safe_folder_name_is_always_legal(name):
    got = v.safe_folder_name(name)
    assert got and len(got) <= v.MAX_FOLDER_NAME_LEN and not got.endswith(".")
    assert not any(ch in FORBIDDEN or ord(ch) < 32 for ch in got)


@given(anything)
def test_category_name_is_tidy(raw):
    got = only_value_error(v.parse_category_name, raw)
    if got is not None:
        assert 0 < len(got) <= v.MAX_CATEGORY_LEN
        assert got == " ".join(got.split())
        assert v.parse_category_name(got) == got        # idempotent


@given(anything)
def test_url_setting_is_http_or_blank(raw):
    got = only_value_error(v.parse_url_setting, raw)
    if got is not None:
        assert got.startswith(("http://", "https://")) and len(got) <= v.MAX_URL_LEN
        assert not any(ch.isspace() for ch in got)


@given(anything)
def test_multi_amount_is_one_of_the_choices(raw):
    got = only_value_error(v.parse_multi_amount, raw)
    if got is not None:
        assert got in v.MULTI_AMOUNT_CHOICES


@given(st.sampled_from(sorted(v.MULTI_AMOUNT_CHOICES)), st.sampled_from(["", " "]))
def test_multi_amount_round_trips(choice, pad):
    assert v.parse_multi_amount(pad + choice.upper() + pad) == choice


date_pattern_text = st.one_of(junk, st.lists(st.sampled_from(["yyyy", "yy", "mm", "dd", "-", "_", ".", " ", "x"]),
                                             max_size=8).map("".join))
name_format_text = st.one_of(junk, st.lists(st.sampled_from(["%date", "%category", "%vendor", "%car", "%amount", "%%", "%", "%x",
                                                             " - ", "_", "a", "/"]), max_size=8).map("".join))


@given(date_pattern_text)
def test_date_pattern_is_tokens_and_separators_only(raw):
    got = only_value_error(v.parse_date_pattern, raw)
    if got is not None:
        rest = got
        for token in ("yyyy", "yy", "mm", "dd"):
            rest = rest.replace(token, "")
        assert rest.strip("-_. ") == "" and rest != got and len(got) <= v.MAX_DATE_PATTERN_LEN


@given(name_format_text)
def test_name_format_always_names_the_date_and_something_else(raw):
    got = only_value_error(v.parse_name_format, raw)
    if got is not None:
        assert "%date" in got and ("%category" in got or "%vendor" in got)


@given(date_pattern_text, name_format_text, st.datetimes(min_value=datetime(1900, 1, 1)),
       st.one_of(st.none(), junk), st.one_of(st.none(), junk), st.one_of(st.none(), junk), st.one_of(st.none(), amounts))
def test_invoice_filename_is_always_a_legal_pdf_name(pattern, template, when, category, vendor, car, amount):
    pattern = only_value_error(v.parse_date_pattern, pattern)
    template = only_value_error(v.parse_name_format, template)
    name = v.invoice_filename(when, category, vendor, pattern, template, car=car, amount=amount)
    assert name.endswith(".pdf") and len(name) <= v.MAX_FILENAME_LEN
    assert not any(ch in FORBIDDEN or ord(ch) < 32 for ch in name)


# ---- a whole expense form ----------------------------------------------------------------------

form_fields = ("car_id", "category_id", "date", "time", "amount", "odometer_km", "invoice_url", "invoice_path", "vendor", "notes")


@given(st.dictionaries(st.sampled_from(form_fields), anything))
def test_expense_form_gives_clean_values_or_errors(form):
    values, errors = v.parse_expense_form(form, "Australia/Brisbane", {1, 2}, {3, 4})
    if not errors:
        assert values["car_id"] in {1, 2} and values["category_id"] in {3, 4}
        assert values["amount"] == values["amount"].quantize(Decimal("0.01"))
        assert values["occurred_at"].utcoffset() is not None
        assert len(values["vendor"] or "") <= v.MAX_TEXT_LEN and len(values["notes"] or "") <= v.MAX_NOTES_LEN
    else:
        assert set(errors) <= set(form_fields)


@given(st.integers(1, 2), st.integers(3, 4), st.dates(min_value=date(1990, 1, 1)), st.times(), amounts,
       st.one_of(st.none(), st.integers(0, 9_999_999)), st.text(string.ascii_letters + " ", max_size=30))
def test_expense_form_round_trips(car, category, day, clock, amount, odometer, vendor):
    form = {"car_id": str(car), "category_id": str(category), "date": day.isoformat(), "time": clock.strftime("%H:%M"),
            "amount": f"{amount:.2f}", "odometer_km": "" if odometer is None else str(odometer), "vendor": vendor}
    values, errors = v.parse_expense_form(form, "Australia/Brisbane", {1, 2}, {3, 4})
    assert errors == {}
    assert (values["car_id"], values["category_id"], values["amount"]) == (car, category, amount)
    assert values["odometer_km"] == (None if odometer is None else float(odometer))
    assert values["vendor"] == (vendor.strip() or None)


# ---- reading an invoice's text -----------------------------------------------------------------

CATEGORIES = ["Tyres", "Insurance", "Registration", "Servicing"]
pdf_text = st.one_of(
    junk,
    st.lists(st.sampled_from(["Total  $1,250.00", "Tax Invoice Date: 07/11/2026", "Odometer: 57,000 km", "Tyres", "Pty Ltd", "\n",
                              "Annual premium  $55.00", "Amount paid:  $12.50", "Tyre Shop Pty Ltd ABN 11", "trading as Bob",
                              "  ", "$", "Registration"]), max_size=12).map(" ".join))


@given(pdf_text)
def test_amount_from_text_is_a_sensible_figure_or_none(text):
    for choice in v.MULTI_AMOUNT_CHOICES:
        got = invoices._amount(text, choice)
        assert got is None or (isinstance(got, Decimal) and got.is_finite() and got >= 0)


@given(st.decimals(min_value=Decimal("0.01"), max_value=Decimal("99999999.99"), places=2))
def test_a_total_line_is_read_back(amount):
    assert invoices._amount(f"Subtotal  $1.00\nTotal  ${amount:,.2f}\n", "max") == amount


@given(junk, junk, pdf_text, st.sampled_from(["max", "min", "blank", None]),
       st.dictionaries(st.sampled_from(invoices.LEARNABLE), st.sets(junk, max_size=3)),
       st.dictionaries(junk, st.sampled_from(CATEGORIES + ["Nonsense"]), max_size=3))
def test_propose_never_fails_and_only_proposes_what_it_may(folder, name, text, multi, labels, vendors):
    got = invoices.propose(folder, name, text, CATEGORIES, {"labels": labels, "vendors": vendors}, multi)
    assert got["category"] is None or got["category"] in CATEGORIES
    assert got["date"] is None or isinstance(got["date"], date)
    assert got["amount"] is None or (isinstance(got["amount"], Decimal) and got["amount"].is_finite())
    assert got["odometer_km"] is None or str(got["odometer_km"]).isdigit()
    assert got["missing"] == [f for f in ("date", "category", "amount") if got[f] is None]


@given(st.dates(min_value=date(2000, 1, 1), max_value=date(2099, 12, 31)), st.sampled_from(["", " Tyres", "_Tyres"]))
def test_a_date_in_a_file_name_is_read_back(day, tail):
    assert invoices._date_from_filename(f"{day:%y%m%d}{tail}.pdf") == day
    assert invoices._date_from_filename(f"{day.isoformat()}{tail}.pdf") == day


@given(junk)
def test_file_name_and_text_dates_never_fail(text):
    for found in (invoices._date_from_filename(text), invoices._date_from_text(text)):
        assert found is None or isinstance(found, date)


@given(st.dates(min_value=date(1990, 1, 1)))
def test_an_invoice_date_in_the_text_is_read_back(day):
    assert invoices._date_from_text(f"Tax Invoice Date: {day:%d/%m/%Y}\n") == day


@given(junk)
def test_labels_are_plain_words(raw):
    got = invoices.norm_label(raw)
    assert got is None or (got == got.lower() and not any(ch.isdigit() for ch in got))


@given(pdf_text, st.one_of(
    st.tuples(st.just("amount"), st.decimals(min_value=0, max_value=10**7, places=2)),
    st.tuples(st.just("date"), st.dates()),
    st.tuples(st.just("odometer_km"), st.integers(0, 10**7))))
def test_finding_labels_never_fails(text, field_and_value):
    assert isinstance(invoices.find_labels(text, *field_and_value), set)


@given(st.lists(st.text(string.ascii_lowercase, min_size=2, max_size=8), min_size=2, max_size=3).map(" ".join),
       st.decimals(min_value=Decimal("0.01"), max_value=Decimal("99999.99"), places=2))
def test_a_label_next_to_a_figure_is_found(label, amount):
    assert label in invoices.find_labels(f"{label}    ${amount:,.2f}", "amount", amount)


@given(st.binary(max_size=2000))
def test_extract_text_is_a_string_for_any_bytes(data):
    assert isinstance(invoices.extract_text(data), str)


# ---- release tags ------------------------------------------------------------------------------

@given(anything)
def test_release_version_is_a_key_or_none(raw):
    got = updates.parse(raw)
    assert got is None or isinstance(got, tuple)


@given(st.integers(0, 50), st.integers(0, 50), st.integers(0, 50), st.integers(1, 30))
def test_a_candidate_sorts_before_its_release_and_by_number(major, minor, patch, n):
    base = f"{major}.{minor}.{patch}"
    assert updates.parse(f"{base}-rc{n}") < updates.parse(base) == updates.parse(f"v{base}")
    assert updates.parse(f"{base}-rc{n}") < updates.parse(f"{base}-rc{n + 1}")
    assert updates.parse(base) < updates.parse(f"{major}.{minor}.{patch + 1}")
