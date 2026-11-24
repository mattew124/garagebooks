"""Read a provider's lease form (an associate lease form, a package confirmation) for the Add a lease page.

Only ever a proposal: the page fills its boxes with what was read, for the owner to check before saving.
It reads the lease's own figures by their labels and nothing else: no names, employer, payroll, bank or
registration numbers are looked for, returned or kept. The values come back as the form's own text
fields, so the page can show them as if they had been typed.
"""

import re
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation

_MONEY = r"\$?\s*([\d,]+(?:\.\d{1,2})?)"
_DATE = r"(\d{4}-\d{2}-\d{2}|\d{1,2}[-/. ][A-Za-z]{3,9}[-/. ]\d{2,4}|\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4})"
_DATE_FORMATS = ("%Y-%m-%d", "%d-%b-%y", "%d-%b-%Y", "%d-%B-%Y", "%d-%m-%Y", "%d-%m-%y")

START = re.compile(r"lease\s+start\s+date\s*:?\s*" + _DATE, re.I)
END = re.compile(r"lease\s+end\s+date\s*:?\s*" + _DATE, re.I)
YEARLY = re.compile(r"annual\s+lease\s+payment\s*:?\s*" + _MONEY, re.I)
INSTALMENT = re.compile(r"instal?ment\s+amount(?:\s*\([^)\n]*\))?\s*:?\s*" + _MONEY, re.I)
FBT_BASE = re.compile(r"fbt\s+base\s+(?:cost|value)\s*:?\s*" + _MONEY, re.I)
ELIGIBLE_EV = re.compile(r"eligible\s+ev\b", re.I)
KM = re.compile(r"(?:nominated|estimated)\s+annual(?:[^\n]*\n)?[^\n]*?kilomet(?:re|er)s\s*:?\s*([\d,]+)", re.I)
PROVIDER = re.compile(r"mail\s+to\s*:\s*(\S.*?)\s*$", re.I | re.M)
ASSOCIATE = re.compile(r"associate\s+lease", re.I)
AGREEMENT = re.compile(r"lease\s+agreement", re.I)
INVOICE_WORDS = re.compile(r"tax\s+invoice|amount\s+due|invoice\s+(?:no|number|date)", re.I)
# The yearly budget section: a label, a dollar sign and the figure. The dollar sign keeps "Registration"
# apart from the plate and the state it also labels.
BUDGETS = {"maintenance": "maintenance", "registration": "registration", "insurance": "insurance",
           "fuel_charging": r"(?:fuel|charging)", "tyres": "tyres"}
_COMPANY_TAIL = re.compile(r"\s+(?:pty\.?\s+)?(?:ltd|limited)\.?$", re.I)
# Pays in a year by frequency; the instalment must divide the yearly payment into about one of these.
PAYS = {"weekly": 52, "fortnightly": 26, "monthly": 12}


def parse_date(raw):
    """'01-Jul-26', '01/07/2026', '1 July 2026' or '2026-07-01' -> a date, else None."""
    text = re.sub(r"[/. ]", "-", (raw or "").strip())
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _money(raw):
    try:
        return Decimal(raw.replace(",", "")).quantize(Decimal("0.01"))
    except InvalidOperation:
        return None


def term_months(start, end):
    """Whole months from `start` to `end`, both days included (21 Nov to 20 Nov two years on is 24); at least 1."""
    after = end + timedelta(days=1)
    months = (after.year - start.year) * 12 + after.month - start.month - (1 if after.day < start.day else 0)
    return max(months, 1)


def _first(pattern, text, convert):
    m = pattern.search(text)
    return convert(m.group(1)) if m else None


def read(text):
    """-> {lease form field: text or True} for what the document states. {} when it isn't a lease form."""
    start, end = _first(START, text, parse_date), _first(END, text, parse_date)
    yearly, instalment = _first(YEARLY, text, _money), _first(INSTALMENT, text, _money)
    if not (start or end or yearly or instalment):
        return {}
    found = {}
    if ASSOCIATE.search(text):
        found["associate_lease"] = True
    provider = _first(PROVIDER, text, lambda s: _COMPANY_TAIL.sub("", s)[:100])
    if provider:
        found["package_name"] = provider
    if start:
        found["start_date"] = start.isoformat()
    if end:
        found["end_date"] = end.isoformat()
    if start and end and end > start:
        found["term_months"] = str(term_months(start, end))
    if instalment:
        found["pretax_payment"] = f"{instalment:.2f}"
    if yearly and instalment:
        pays = yearly / instalment
        for frequency, n in PAYS.items():
            if abs(pays - n) <= Decimal("0.02") * n:
                found["pay_frequency"] = frequency
    base = _first(FBT_BASE, text, _money)
    if base is not None:
        found["fbt_base_value"] = f"{base:.2f}"
        if ELIGIBLE_EV.search(text):
            found["ev_exempt"] = True
    km = _first(KM, text, lambda s: s.replace(",", ""))
    if km:
        found["estimated_km_per_year"] = km
    if yearly:
        found["budget_lease"] = f"{yearly:.2f}"
    for pool, label in BUDGETS.items():
        amount = _first(re.compile(r"\b" + label + r"\s+\$\s*([\d,]+(?:\.\d{1,2})?)", re.I), text, _money)
        if amount is not None:
            found[f"budget_{pool}"] = f"{amount:.2f}"
    return found


def document_kind(text):
    """What a PDF's text is among a lease's documents: "form" (a provider's lease form with its dates or
    figures, or titled an associate lease), "agreement" (a lease agreement that is not an invoice), else None.
    Used by the import page to offer such a file as a lease document, not an expense."""
    found = read(text)
    if found and ("associate_lease" in found or ("start_date" in found and "end_date" in found)):
        return "form"
    if AGREEMENT.search(text) and not INVOICE_WORDS.search(text):
        return "agreement"
    return None


def end_date(start, months):
    """The last day of a lease of `months` from `start`: the day before the same day that many months on."""
    year, month = divmod(start.year * 12 + start.month - 1 + months, 12)
    month += 1
    last = (date(year + month // 12, month % 12 + 1, 1) - timedelta(days=1)).day
    return date(year, month, min(start.day, last)) - timedelta(days=1)
