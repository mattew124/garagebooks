"""Read Australian tax rates from the ATO's own pages.

Used by the app's `ato_rates` scheduled job (the income tax brackets and the home charging rate it keeps in
the database) and by scripts/ato_rates.py and scripts/build_lease_workbook.py (the lease what-if workbook).
It reads only https://www.ato.gov.au.

The ATO publishes these as web pages, not as data, so this reads the tables on them. When a page changes
shape it stops with a reason rather than guessing: every table is checked (brackets in order, rates that are
percentages, tax figures that add up from one bracket to the next), because a quietly wrong rate is worse
than no rate. Standard library only.
"""
import html
import re
import urllib.parse
import urllib.request
from datetime import date
from html.parser import HTMLParser

BASE = "https://www.ato.gov.au"
URLS = {
    "resident": f"{BASE}/tax-rates-and-codes/tax-rates-australian-residents",
    "help": f"{BASE}/tax-rates-and-codes/study-and-training-support-loans-rates-and-repayment-thresholds",
    "medicare": f"{BASE}/individuals-and-families/medicare-and-private-health-insurance/medicare-levy/"
                "medicare-levy-reduction/medicare-levy-reduction-for-low-income-earners",
    "medicare_family": f"{BASE}/individuals-and-families/medicare-and-private-health-insurance/medicare-levy/"
                       "medicare-levy-reduction/medicare-levy-reduction-family-income",
    "lito": f"{BASE}/individuals-and-families/income-deductions-offsets-and-records/tax-offsets/low-income-tax-offset",
    # PCG 2024/2, the EV home charging rate: its Table 2 lists the cents per km from each FBT year on.
    "ev_charging": f"{BASE}/law/view/document?docid=COG/PCG20242/NAT/ATO/00001",
}
WORKBOOK_PAGES = ("resident", "help", "medicare", "medicare_family", "lito")   # what the what-if workbook reads
FIRST_YEAR = 2020           # 2020-21 onward: every year since has the same five tax brackets
MAX_BYTES = 5_000_000
ROUNDING = 1.0              # the ATO rounds the tax at each bracket's start to a whole dollar


class RatesError(Exception):
    """A page is missing, or doesn't look like the one this script was written for."""


# ---- fetching -------------------------------------------------------------------

def get(url):
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https" or parts.hostname != "www.ato.gov.au":
        raise RatesError(f"only https://www.ato.gov.au is read, not {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "garagebooks-rates/1 (tax rate check)"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:  # nosec B310 - https and www.ato.gov.au checked above
            if resp.status != 200:
                raise RatesError(f"{url} answered {resp.status}")
            return resp.read(MAX_BYTES).decode("utf-8", "replace")
    except OSError as err:
        raise RatesError(f"could not read {url}: {err}") from err


# ---- reading the pages ------------------------------------------------------------

def clean(text):
    text = html.unescape(text).replace(" ", " ").replace("–", "-").replace("−", "-")
    return re.sub(r"\s+", " ", text).strip()


class _Tables(HTMLParser):
    """Every table as {caption, head, rows}, cell text cleaned."""

    def __init__(self):
        super().__init__()
        self.tables, self._t, self._row, self._cell, self._cap = [], None, None, None, None

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self._t = {"caption": "", "head": [], "rows": []}
        elif self._t is not None and tag == "caption":
            self._cap = ""
        elif self._t is not None and tag == "tr":
            self._row = []
        elif self._row is not None and tag in ("td", "th"):
            self._cell = ""

    def handle_endtag(self, tag):
        if tag == "caption" and self._cap is not None:
            self._t["caption"] = clean(self._cap)
            self._cap = None
        elif tag in ("td", "th") and self._cell is not None:
            self._row.append(clean(self._cell))
            self._cell = None
        elif tag == "tr" and self._row is not None and self._t is not None:
            if self._row:
                self._t["rows"].append(self._row)
            self._row = None
        elif tag == "table" and self._t is not None:
            if self._t["rows"]:
                self._t["head"] = self._t["rows"][0]
            self.tables.append(self._t)
            self._t = None

    def handle_data(self, data):
        if self._cell is not None:
            self._cell += data
        elif self._cap is not None:
            self._cap += data


def tables(page):
    parser = _Tables()
    parser.feed(page)
    return parser.tables


def text_of(page):
    page = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", page, flags=re.S | re.I)
    return clean(re.sub(r"<[^>]+>", " ", page))


def money(s):
    return float(s.replace(",", ""))


def lower_bound(cell):
    """The first dollar figure of a bracket such as '$18,201 - $45,000'; 0 for '0 - ...' or 'Below ...'."""
    if cell.lower().startswith("below"):
        return 0.0
    m = re.match(r"\$?([\d,]+)", cell)
    if not m:
        raise RatesError(f"no income figure in {cell!r}")
    return money(m.group(1))


def start_of(lower):
    """Half a dollar under the bracket's first whole dollar, so a spreadsheet lookup on a whole-dollar
    income puts $18,201 in the bracket that starts there and $18,200 in the one before."""
    return lower - 0.5 if lower > 0 else 0.0


def year_in(text):
    m = re.search(r"(20\d\d)\s*-\s*(\d\d)", clean(text))
    if not m:
        return None
    first, last = int(m.group(1)), int(m.group(2))
    if last != (first + 1) % 100:
        return None
    return f"{first}-{m.group(2)}" if first >= FIRST_YEAR else None


RATE = r"([\d.]+)\s*c(?:ents?)? for each \$1 over \$([\d,]+)"


def bracket(cells):
    """One row of a rates table as {start, base, rate, over, total}: the repayment or tax is
    base + rate x (income - over), or rate x income when total is true."""
    start, text = start_of(lower_bound(cells[0])), cells[1]
    row = {"start": start, "base": 0.0, "rate": 0.0, "over": 0.0, "total": False}
    if text.lower() == "nil":
        return row
    if m := re.fullmatch(rf"(?:\$([\d,]+) plus )?{RATE}", text):
        row.update(base=money(m.group(1)) if m.group(1) else 0.0, rate=float(m.group(2)) / 100,
                   over=money(m.group(3)))
    elif m := re.fullmatch(r"([\d.]+)\s*% of your total repayment income", text) or re.fullmatch(r"([\d.]+)\s*%", text):
        row.update(rate=float(m.group(1)) / 100, total=True)
    else:
        raise RatesError(f"cannot read the rate {text!r}")
    return row


def check(rows, what):
    if not rows or rows[0]["start"] != 0 or rows[0]["rate"] != 0:
        raise RatesError(f"{what}: the first bracket should start at $0 with no tax")
    for prev, row in zip(rows, rows[1:], strict=False):
        if row["start"] <= prev["start"]:
            raise RatesError(f"{what}: brackets are not in order")
    for row in rows:
        if not 0 <= row["rate"] <= 0.6:
            raise RatesError(f"{what}: a rate of {row['rate']:.1%} is not a percentage")
    for prev, row in zip(rows, rows[1:], strict=False):
        if prev["total"] or row["total"] or not row["over"]:
            continue
        expected = prev["base"] + prev["rate"] * (row["over"] - prev["over"])
        if abs(row["base"] - expected) > ROUNDING and row["base"]:
            raise RatesError(f"{what}: ${row['base']:,.0f} does not follow from the bracket before it")


def by_year(page, caption_word, parse_rows, what):
    found = {}
    for t in tables(page):
        if caption_word not in t["caption"].lower():
            continue
        year = year_in(t["caption"])
        if year is None:
            continue
        rows = parse_rows(t["rows"][1:])
        check(rows, f"{what} {year}")
        if year in found and found[year] != rows:
            raise RatesError(f"{what} {year} appears twice with different figures")
        found[year] = rows
    if not found:
        raise RatesError(f"no {what} tables found: has the ATO page changed?")
    return found


def parse_resident(page):
    return by_year(page, "resident tax rates", lambda rows: [bracket(r) for r in rows], "income tax")


def parse_help(page):
    return by_year(page, "repayment", lambda rows: [bracket(r) for r in rows], "HELP")


def parse_medicare(page):
    for t in tables(page):
        if "lower threshold" not in " ".join(t["head"]).lower():
            continue
        for row in t["rows"][1:]:
            if row[0].lower().startswith("all other taxpayers"):
                amounts = [money(a) for a in re.findall(r"\$([\d,]+)", " ".join(row[1:3]))]
                year = re.search(r"\bIn (20\d\d)\s*-\s*(\d\d)\b", text_of(page))
                if len(amounts) != 2 or amounts[0] >= amounts[1] or not year:
                    break
                return {"year": f"{year.group(1)}-{year.group(2)}", "lower": amounts[0], "upper": amounts[1]}
    raise RatesError("no Medicare levy thresholds found: has the ATO page changed?")


def parse_medicare_family(page):
    """The family income thresholds for people without the seniors and pensioners offset."""
    text = text_of(page)
    patterns = {
        "lower": r"lower family income threshold is either: \$([\d,]+) \$[\d,]+ if you.re entitled to the SAPTO",
        "upper": r"upper family income threshold is either: \$([\d,]+) \$[\d,]+ if you.re entitled to SAPTO",
        "lower_child": r"lower family income threshold increases by \$([\d,]+) for each dependent child",
        "upper_child": r"upper income threshold increases by \$([\d,]+) for each dependent child",
    }
    out = {}
    for key, pattern in patterns.items():
        m = re.search(pattern, text)
        if not m:
            raise RatesError("no Medicare levy family thresholds found: has the ATO page changed?")
        out[key] = money(m.group(1))
    year = re.search(r"\bin (20\d\d)\s*-\s*(\d\d)\b", text, re.I)
    if not year or not 0 < out["lower"] < out["upper"] or not 0 < out["lower_child"] < out["upper_child"]:
        raise RatesError("the Medicare levy family thresholds do not look right: has the ATO page changed?")
    return {"year": f"{year.group(1)}-{year.group(2)}", **out}


def parse_lito(page):
    text = text_of(page)
    top = re.search(r"maximum offset of \$([\d,]+)", text)
    steps = re.findall(r"between \$([\d,]+) and \$([\d,]+), you will get \$([\d,]+) minus ([\d.]+) cents? "
                       r"for every \$1 above \$([\d,]+)", text)
    if not top or len(steps) != 2:
        raise RatesError("no low income tax offset steps found: has the ATO page changed?")
    out = [{"start": 0.0, "base": money(top.group(1)), "rate": 0.0, "over": 0.0}]
    for low, _high, base, cents, over in steps:
        out.append({"start": start_of(money(low)), "base": money(base), "rate": float(cents) / 100,
                    "over": money(over)})
    out.append({"start": money(steps[-1][1]) + 0.5, "base": 0.0, "rate": 0.0, "over": 0.0})
    if not out[0]["base"] >= out[1]["base"] > out[2]["base"] > 0:
        raise RatesError("the low income tax offset steps do not step down")
    return out


def parse_ev_charging(page):
    """The ATO's EV home charging rate (PCG 2024/2, Table 2) as {FBT year it applies from: cents per km}, the
    year being the one it starts in (2026 is the FBT year from 1 April 2026). Each rate holds until the next."""
    found = {}
    for t in tables(page):
        if "cents per kilometre rate" not in t["caption"].lower():
            continue
        for row in t["rows"][1:]:
            if len(row) < 2:
                continue
            when = re.search(r"commencing on and after 1 April (20\d\d)", row[0])
            cents = re.fullmatch(r"(\d{1,2}\.\d{1,2}) cents per km(?:\s*\[\d+\])?", row[1])
            if not when or not cents:
                raise RatesError(f"cannot read the home charging rate row {row!r}")
            year, value = int(when.group(1)), cents.group(1)
            if not 2000 <= year <= 2200 or not 0 < float(value) < 100:
                raise RatesError(f"the home charging rate {value} from {year} does not look right")
            found[year] = value
    if not found:
        raise RatesError("no EV home charging rate table found: has the ATO page changed?")
    return found


# ---- using them (the workbook does the same sums; these let the tests check the reading) -------

def row_for(income, rows):
    return [r for r in rows if r["start"] <= income][-1]


def tax_on(income, rows):
    r = row_for(income, rows)
    return r["base"] + r["rate"] * (income - r["over"])


def help_on(income, rows):
    r = row_for(income, rows)
    return r["rate"] * income if r["total"] else r["base"] + r["rate"] * (income - r["over"])


# ---- all of it --------------------------------------------------------------------

def fetch_all(fetch=get, now=None):
    return {
        "fetched": now or date.today().isoformat(),
        "sources": {key: URLS[key] for key in WORKBOOK_PAGES},
        "tax": parse_resident(fetch(URLS["resident"])),
        "help": parse_help(fetch(URLS["help"])),
        "medicare": parse_medicare(fetch(URLS["medicare"])),
        "medicare_family": parse_medicare_family(fetch(URLS["medicare_family"])),
        "lito": parse_lito(fetch(URLS["lito"])),
    }
