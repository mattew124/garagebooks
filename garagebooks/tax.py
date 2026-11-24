"""The tax rate by income year: the ATO's brackets as data, and what a person has chosen for each year.

A person picks, for each income year (1 July to 30 June), the bracket their taxable income falls in and their
Medicare levy status, on the Settings page. A bracket stands for an income (its middle, or $10,000 into the top
one), so no salary is ever asked for or kept. The one definition of what that gives (the marginal rate of a
date, a year with no choice following the latest earlier one) is SQL: garagebooks.tax_position() and
garagebooks.marginal_rate(), which the app and the dashboard share. This module names the choices, labels the
brackets for the page, and refreshes the brackets and the home charging rate from the ATO (ato.py reads the
pages). Not tax advice.
"""

from decimal import Decimal

from . import ato, db, jobs

PEOPLE = {"employee": "You", "associate": "Associate"}
MEDICARE = {"not_exempt": "Not exempt", "half_exempt": "Half exempt", "exempt": "Exempt"}
LEVY = {"not_exempt": Decimal(2), "half_exempt": Decimal(1), "exempt": Decimal(0)}
TOP_BRACKET_INCOME = 10000       # the income a top bracket stands for: this far into it


def income_year(day):
    """The income year (1 July to 30 June) a date falls in, as the year it starts in (as garagebooks.income_year)."""
    return day.year if day.month >= 7 else day.year - 1


def year_label(year):
    return f"{year}–{(year + 1) % 100:02d}"


def _percent(rate):
    text = f"{Decimal(rate):f}"
    return (text.rstrip("0").rstrip(".") if "." in text else text) + "%"


def year_brackets(rows, year):
    """One year's brackets from db.tax_brackets rows, each with its label: '$45,001 – $135,000 (30%)'."""
    mine = [r for r in rows if r["income_year"] == year]
    out = []
    for i, row in enumerate(mine):
        low = f"${row['lower_income']:,}"
        span = (f"{low} – ${mine[i + 1]['lower_income'] - 1:,}" if i + 1 < len(mine) else f"{low} and over")
        out.append({**row, "label": f"{span} ({_percent(row['rate'])})"})
    return out


def table_year(years, year):
    """The ATO year whose brackets an income year uses (as garagebooks.tax_table_year): its own, the newest
    earlier one, or the oldest. None with no brackets at all."""
    if not years:
        return None
    earlier = [y for y in years if y <= year]
    return max(earlier) if earlier else min(years)


def settings_years(rows, today):
    """The income years the Settings page lists, newest first: every year with brackets, and up to this one."""
    years = {r["income_year"] for r in rows}
    if not years:
        return []
    return list(range(max(max(years), income_year(today)), min(years) - 1, -1))


# ---- reading the ATO's pages (the ato_rates job) -------------------------------------------------------------

def _brackets_of(parsed):
    """ato.parse_resident()'s rows for one year as tax_bracket rows."""
    return [{"lower_income": 0 if r["start"] == 0 else round(r["start"] + 0.5),
             "threshold": Decimal(str(r["over"])).quantize(Decimal("0.01")),
             "base_tax": Decimal(str(r["base"])).quantize(Decimal("0.01")),
             "rate": Decimal(str(round(r["rate"] * 100, 2))).quantize(Decimal("0.01"))} for r in parsed]


def refresh(conn, fetch=ato.get, now=None):
    """Read the income tax brackets and the EV home charging rate from the ATO and keep them. Both pages are read
    and checked before anything is written: a page that can't be read or has changed shape raises jobs.JobError,
    and the last good set stays as it is. -> a few words for the Settings page."""
    try:
        years = {int(label[:4]): _brackets_of(rows) for label, rows in ato.parse_resident(fetch(ato.URLS["resident"])).items()}
        ev = ato.parse_ev_charging(fetch(ato.URLS["ev_charging"]))
    except ato.RatesError as exc:
        raise jobs.JobError(f"The ATO's rates were not read, so the last ones are kept: {exc}") from None
    changed = [y for y, rows in sorted(years.items()) if db.replace_tax_year(conn, y, rows, now)]
    known = db.ev_charging_rates(conn)
    for year, cents in sorted(ev.items()):
        db.set_ev_charging_rate(conn, year, Decimal(cents), ato.URLS["ev_charging"], now)
    moved = [y for y, cents in ev.items() if known.get(y) != Decimal(cents)]
    said = f"Tax brackets for {year_label(min(years))} to {year_label(max(years))}"
    said += f" ({', '.join(year_label(y) for y in changed)} updated)" if changed else ", unchanged"
    said += "; home charging rate " + ("updated" if moved else "unchanged")
    return said


# ---- the Settings page --------------------------------------------------------------------------------------

def field(person, year, part):
    return f"tax_{person}_{year}_{part}"


def parse_choices(form, rows, today, people):
    """The tax choices a Settings form sends: -> (changes {(person, year): (bracket's first dollar or None, medicare)},
    raw values {field: text}, errors {field: message}). A year whose bracket field isn't in the form is left as it
    is; a blank bracket clears the year's choice (it then follows the latest earlier one)."""
    years = {r["income_year"] for r in rows}
    changes, values, errors = {}, {}, {}
    for person in people:
        for year in settings_years(rows, today):
            name, medicare_name = field(person, year, "bracket"), field(person, year, "medicare")
            if name not in form:
                continue
            raw, medicare = form.get(name, "").strip(), form.get(medicare_name, "not_exempt").strip()
            values[name], values[medicare_name] = raw, medicare
            if not raw:
                changes[(person, year)] = (None, None)
                continue
            lowers = {b["lower_income"] for b in rows if b["income_year"] == table_year(years, year)}
            lower = int(raw) if raw.isdigit() and len(raw) < 10 else None
            if lower not in lowers:
                errors[name] = f"Choose one of the {year_label(year)} tax brackets."
            elif medicare not in MEDICARE:
                errors[medicare_name] = "Choose a Medicare levy status from the list."
            else:
                changes[(person, year)] = (lower, medicare)
    return changes, values, errors


def settings_rows(rows, choices, positions, today, people):
    """What the Settings page's table shows, one row per income year, newest first: for each person the year's own
    choice (or None), the brackets to choose from, and a note when the year follows an earlier one or has no
    brackets of its own. `positions` is {person: db.tax_positions(...)}."""
    years = sorted({r["income_year"] for r in rows})
    out = []
    for year in settings_years(rows, today):
        source = table_year(years, year)
        row = {"year": year, "label": year_label(year), "brackets": year_brackets(rows, source),
               "table_note": "" if source == year else f"The ATO hasn't published {year_label(year)}'s brackets yet: "
                                                       f"these are {year_label(source)}'s.", "people": {}}
        for person in people:
            own = choices.get((person, year))
            found = positions.get(person, {}).get(year)
            if own:
                note = ""
            elif found:
                note = f"Uses {year_label(found['chosen_year'])}'s choice: {_percent(found['marginal_rate'])} with Medicare."
            else:
                note = "Not chosen, nor any earlier year: no after-tax figure for this year."
            row["people"][person] = {"choice": own, "note": note, "position": found}
        out.append(row)
    return out
