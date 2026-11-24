"""Novated lease facts that are data, and the read-only figures worked out from a lease's details.

Pure functions, no database or Flask. What a lease record says comes from the provider's statement
(db.py stores it); this module only names the fixed lists (pools, pay frequencies, FBT methods) and
works out figures to show beside the entry, so a typing mistake is visible. It records what the
statement says and is not tax advice.
"""

from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP, localcontext

# The provider's budget pools, in the order a statement lists them: identifier -> label.
POOLS = {
    "lease": "Lease payments",
    "fuel_charging": "Fuel or charging",
    "insurance": "Insurance",
    "registration": "Registration and CTP",
    "maintenance": "Maintenance",
    "tyres": "Tyres",
    "other": "Other running costs",
}

# How often a payment comes out of pay, and how many pays that is in a year.
FREQUENCIES = {"weekly": ("Weekly", 52), "fortnightly": ("Fortnightly", 26), "monthly": ("Monthly", 12)}

FBT_METHODS = {"statutory": "Statutory formula", "operating_cost": "Operating cost"}
DEFAULT_STATUTORY_PERCENT = Decimal("20")   # the schema default; stored per lease, never used to calculate in code

TAX_TREATMENTS = {"employee_contribution": "Employee contribution method", "other": "Other"}

# ATO data, in one place. The minimum residual (balloon) payment, as a percentage of the car's cost,
# for a lease of 1 to 5 whole years: 75% less 75% / 8 for each year of the term (see
# docs/lease-calculations.md). Source: the ATO's minimum residual values for a lease of a car,
# as set out on that page. Update them here, and only here, if the ATO changes them.
MIN_RESIDUAL_PERCENT = {
    1: Decimal("65.625"),
    2: Decimal("56.25"),
    3: Decimal("46.875"),
    4: Decimal("37.5"),
    5: Decimal("28.125"),
}

CENTS = Decimal("0.01")


def pays_per_year(frequency):
    return FREQUENCIES[frequency][1]


def total_per_pay(lease):
    """Pre-tax part + post-tax part + the provider's admin fee, for one pay."""
    return lease["pretax_payment"] + lease["posttax_payment"] + lease["admin_fee"]


def yearly_payments(lease):
    return (total_per_pay(lease) * pays_per_year(lease["pay_frequency"])).quantize(CENTS, ROUND_HALF_UP)


def yearly_before_admin(lease):
    """What comes out of pay in a year for the budgets: the pre-tax and post-tax parts, without the admin fee."""
    return ((lease["pretax_payment"] + lease["posttax_payment"]) * pays_per_year(lease["pay_frequency"])).quantize(CENTS, ROUND_HALF_UP)


def budget_total(budgets):
    """The yearly total of a set of budgets (amounts as stored)."""
    return sum((Decimal(b["yearly_amount"]) for b in budgets), Decimal(0))


def budget_on(budgets, pool, on):
    """The yearly budget for `pool` that applies on the date `on`: the latest row for that pool that
    has taken effect by then, or None. A later re-budget never changes an earlier date's answer."""
    rows = [b for b in budgets if b["pool"] == pool and b["effective_from"] <= on]
    return max(rows, key=lambda b: b["effective_from"])["yearly_amount"] if rows else None


def initial_budgets(lease, budgets):
    """The first set (effective from the lease's start): what the lease form shows and edits."""
    return [b for b in budgets if b["effective_from"] == lease["start_date"]]


def residual_figures(lease):
    """-> (amount, percent), each None when it can't be worked out. Whichever the statement gave is
    kept; the other follows from the car's cost when that is known."""
    amount, percent, cost = lease["residual_amount"], lease["residual_percent"], lease["car_cost"]
    if amount is None and percent is not None and cost:
        amount = (cost * percent / 100).quantize(CENTS, ROUND_HALF_UP)
    if percent is None and amount is not None and cost:
        percent = (amount / cost * 100).quantize(Decimal("0.001"), ROUND_HALF_UP)
    return amount, percent


def minimum_residual_percent(term_months):
    """The ATO's minimum for this term, or None when the term isn't a whole number of years from 1 to 5."""
    if term_months % 12:
        return None
    return MIN_RESIDUAL_PERCENT.get(term_months // 12)


def days_remaining(lease, today):
    """Days from `today` to the lease's end (0 on the last day, negative once it has ended)."""
    return (lease["end_date"] - today).days


def status(lease, today):
    """'upcoming' before it starts, 'current' while it runs (both dates inclusive), else 'past'."""
    if today < lease["start_date"]:
        return "upcoming"
    return "current" if today <= lease["end_date"] else "past"


def derived(lease, budgets, today):
    """The read-only figures shown beside a lease, so an entry error is visible."""
    amount, percent = residual_figures(lease)
    minimum = minimum_residual_percent(lease["term_months"])
    first = initial_budgets(lease, budgets)
    yearly = yearly_payments(lease)
    before_admin = yearly_before_admin(lease)
    total = budget_total(first)
    return {
        "pays_per_year": pays_per_year(lease["pay_frequency"]),
        "total_per_pay": total_per_pay(lease),
        "yearly_payments": yearly,
        "budget_total": total,
        "yearly_before_admin": before_admin,
        "budget_gap": total - before_admin,
        "has_budgets": bool(first),
        "residual_amount": amount,
        "residual_percent": percent,
        "minimum_residual_percent": minimum,
        "residual_below_minimum": percent is not None and minimum is not None and percent < minimum,
        "days_remaining": days_remaining(lease, today),
        "days_to_start": (lease["start_date"] - today).days,
        "status": status(lease, today),
    }


def overlaps(start, end, other_start, other_end):
    """Whether two date ranges, each including both ends, share a day."""
    return start <= other_end and other_start <= end


# ---- the FBT year and the car's taxable value (roadmap L2) -----------------------------------------
# The FBT year runs 1 April to 31 March and the income tax year 1 July to 30 June. Nobody picks one:
# a date decides both. A year is named by the calendar year it starts in (2025 is "FBT 2025–26"). The
# same rule is in SQL as garagebooks.fbt_year(date), so the dashboard, the app and a query of your own
# agree; a test holds the two together.

def fbt_year(day):
    """The FBT year a date falls in, as the year it starts in."""
    return day.year if day.month >= 4 else day.year - 1


def fbt_start(year):
    return date(year, 4, 1)


def fbt_end(year):
    return date(year + 1, 3, 31)


def days_in_fbt_year(year):
    return (fbt_end(year) - fbt_start(year)).days + 1


def _span(year):
    return f"{year}–{(year + 1) % 100:02d}"


def fbt_label(year):
    return f"FBT {_span(year)}"


def income_tax_year(day):
    """The income tax year (1 July to 30 June) a date falls in, as the year it starts in."""
    return day.year if day.month >= 7 else day.year - 1


def income_tax_label(year):
    return _span(year)


def _held_range(lease, year):
    return max(lease["start_date"], fbt_start(year)), min(lease["end_date"], fbt_end(year))


def days_held(lease, year):
    """Days the car was held in an FBT year under this lease: its dates clipped to the year, both ends counted."""
    first, last = _held_range(lease, year)
    return max((last - first).days + 1, 0)


def _add_months(start, months):
    """The date `months` after `start`, on the same day of the month, or the month's last day if it has none."""
    year, month = divmod(start.year * 12 + start.month - 1 + months, 12)
    month += 1
    last = (date(year + month // 12, month % 12 + 1, 1) - timedelta(days=1)).day
    return date(year, month, min(start.day, last))


def pay_dates(lease, first, last):
    """The pay dates from `first` to `last` (both included): stepping from the lease's start date by its pay
    frequency. An assumption: the provider's own pay dates may differ a little."""
    frequency = lease["pay_frequency"]
    step = {"weekly": 7, "fortnightly": 14}.get(frequency)
    found, n = [], 0
    while True:
        day = lease["start_date"] + timedelta(days=n * step) if step else _add_months(lease["start_date"], n)
        if day > last:
            return found
        if day >= first:
            found.append(day)
        n += 1


def fbt_figures(lease, year, today):
    """The FBT figures of one lease for one FBT year: days held, taxable value (statutory formula), the
    post-tax contribution that cancels it, and the contributions made against it. Money is rounded to
    cents once, at the end. `taxable_value` and `required` are None, with a `reason`, when they can't
    be worked out here."""
    held, in_year = days_held(lease, year), days_in_fbt_year(year)
    first, last = _held_range(lease, year)
    pays = pay_dates(lease, first, last) if held else []
    so_far = [p for p in pays if p <= today]
    posttax = lease["posttax_payment"]
    out = {"year": year, "days_held": held, "days_in_year": in_year, "exempt": bool(lease["ev_exempt"]),
           "method": lease["fbt_method"], "taxable_value": None, "required": None, "reason": "",
           "pays_in_year": len(pays), "pays_so_far": len(so_far),
           "made": (posttax * len(so_far)).quantize(CENTS, ROUND_HALF_UP),
           "expected": (posttax * len(pays)).quantize(CENTS, ROUND_HALF_UP), "difference": None}
    if lease["ev_exempt"]:
        out["taxable_value"] = out["required"] = Decimal("0.00")
    elif lease["fbt_method"] != "statutory":
        out["reason"] = ("The statutory figure doesn't apply: the operating cost method is not worked out by "
                         "GarageBooks. Use the figure on your provider's statement.")
    elif lease["fbt_base_value"] is None:
        out["reason"] = "No FBT base value is entered for this lease, so the taxable value can't be worked out."
    else:
        value = lease["fbt_base_value"] * lease["statutory_percent"] / 100 * held / in_year
        out["taxable_value"] = out["required"] = value.quantize(CENTS, ROUND_HALF_UP)
    if out["required"] is not None:
        out["difference"] = out["expected"] - out["required"]
    return out


def fbt_years(leases, expense_years):
    """Every FBT year with something to show: those a lease spans, and those with an expense. Oldest first."""
    years = set(expense_years)
    for row in leases:
        years.update(range(fbt_year(row["start_date"]), fbt_year(row["end_date"]) + 1))
    return sorted(years)


NO_POOL = "none"        # the report's own line for categories that belong to no pool, so nothing is hidden


def _sum(values):
    return sum(values, Decimal(0)).quantize(CENTS, ROUND_HALF_UP)


def fbt_report(leases, costs, charging, year, today):
    """Everything the per-FBT-year report shows, worked out (no database): `leases` is a list of (lease row,
    its budgets) for the car, `costs` the year's expenses by category (db.fbt_costs), and `charging`
    TeslaMate's charging cost for the year (sources.Answer) or None when the car has none to ask for.
    A figure that depends on something unknown is None with the reason, never a number standing in for it."""
    held = [(row, budgets, fbt_figures(row, year, today)) for row, budgets in sorted(leases, key=lambda x: x[0]["start_date"])
            if days_held(row, year)]
    figures = [f for _, _, f in held]
    out = {"year": year, "label": fbt_label(year), "income_tax_label": income_tax_label(year), "leases": held,
           "days_held": sum(f["days_held"] for f in figures), "days_in_year": days_in_fbt_year(year)}
    worked = all(f["taxable_value"] is not None for f in figures)
    out.update(
        exempt=bool(figures) and all(f["exempt"] for f in figures),
        taxable_value=_sum(f["taxable_value"] for f in figures) if worked and figures else (Decimal("0.00") if not figures else None),
        reasons=[f["reason"] for f in figures if f["reason"]],
        pays_in_year=sum(f["pays_in_year"] for f in figures), pays_so_far=sum(f["pays_so_far"] for f in figures),
        made=_sum(f["made"] for f in figures), expected=_sum(f["expected"] for f in figures))
    out["required"] = out["taxable_value"]
    out["difference"] = None if out["required"] is None else out["expected"] - out["required"]
    # Running costs by pool. Purchases are listed apart; a category in no pool keeps its own line.
    spent, names = {}, {}
    for row in costs:
        if row["kind"] == "purchase":
            continue
        key = row["budget_pool"] or NO_POOL
        spent[key] = spent.get(key, Decimal(0)) + row["total"]
        names.setdefault(key, []).append((row["category"], row["total"]))
    out["purchases"] = [(r["category"], r["total"]) for r in costs if r["kind"] == "purchase"]
    out["teslamate_charging"] = None
    if charging is not None and (charging.value is None or charging.value != 0):
        out["teslamate_charging"] = {"value": charging.value, "reason": charging.reason}
    lines = []
    for key, label in [*POOLS.items(), (NO_POOL, "Not in a pool")]:
        budget = None
        for row, budgets, _ in held:
            part = budget_for_year(row, budgets, key, year)
            if part is not None:
                budget = (budget or Decimal(0)) + part
        amount, reason = spent.get(key, Decimal(0)), ""
        if key == "fuel_charging" and out["teslamate_charging"]:
            if charging.value is None:
                amount, reason = None, charging.reason
            else:
                amount += Decimal(charging.value)
        if amount or amount is None or budget is not None:
            lines.append({"key": key, "label": label, "spent": amount, "reason": reason, "budget": budget,
                          "categories": names.get(key, [])})
    out["pools"] = lines
    out["pool_total"] = None if any(line["spent"] is None for line in lines) else _sum(line["spent"] for line in lines)
    out["budget_total"] = _sum(line["budget"] for line in lines if line["budget"] is not None) if any(
        line["budget"] is not None for line in lines) else None
    return out


def budget_for_year(lease, budgets, pool, year):
    """What the yearly budget for `pool` comes to over the days this lease was held in an FBT year: the budget
    in force on each day, pro-rated by days. None when the pool has no budget in that time. For reference only."""
    first, last = _held_range(lease, year)
    if first > last:
        return None
    changes = sorted({b["effective_from"] for b in budgets if b["pool"] == pool and first < b["effective_from"] <= last})
    total, seen = Decimal(0), False
    for begin, end in zip([first, *changes], [*(c - timedelta(days=1) for c in changes), last], strict=True):
        yearly = budget_on(budgets, pool, begin)
        if yearly is not None:
            seen = True
            total += Decimal(yearly) * ((end - begin).days + 1) / days_in_fbt_year(year)
    return total.quantize(CENTS, ROUND_HALF_UP) if seen else None


# ---- cost after tax (roadmap L5) ----------------------------------------------------------------------
# What an expense paid from pre-tax salary costs once the GST is claimed back. Deliberate simplifications,
# set out in docs/lease-calculations.md: GST is a flat 1/11 of an invoice that is not GST-free, and the
# post-tax share is the lease's own split of payments. Not tax advice. The same definition is in SQL as
# garagebooks.expense_after_tax(); a test holds the two together.

def is_packaged(choice, covered, pool, kind):
    """Whether an expense is paid from the package. `choice` is the owner's (True, False, or None for
    automatic); automatic means a lease covers its date, its category has a budget pool, and the category
    is not a purchase."""
    if choice is not None:
        return bool(choice)
    return bool(covered) and pool is not None and kind != "purchase"


def effective_cost(amount, packaged, gst_free, pretax, posttax, rate_percent, gst_amount=None):
    """What an expense costs after tax, to the cent (half away from zero). `pretax` and `posttax` are the
    covering lease's payments (None with no lease; payments summing to nothing count as no post-tax share);
    `rate_percent` is the marginal rate as a percentage (None: not entered). An expense paid personally costs
    its invoice. A packaged one with no rate has no after-tax cost: None, never the invoice standing in.
    `gst_amount` is the GST the document states, when it was read: it replaces the GST rule (`gst_free`, or one
    eleventh), so what is left is `amount - gst_amount`."""
    if not packaged:
        return amount
    if rate_percent is None:
        return None
    pretax, posttax = pretax or Decimal(0), posttax or Decimal(0)
    if pretax + posttax <= 0:
        pretax, posttax = Decimal(1), Decimal(0)
    with localcontext() as ctx:
        ctx.prec = 60            # one division, at the end, so the only rounding is to the cent
        if gst_amount is not None:
            numerator = (amount - gst_amount) * (pretax * (100 - rate_percent) + 100 * posttax)
            denominator = 100 * (pretax + posttax)
            return (numerator / denominator).quantize(CENTS, ROUND_HALF_UP)
        numerator = amount * (1 if gst_free else 10) * (pretax * (100 - rate_percent) + 100 * posttax)
        denominator = (1 if gst_free else 11) * 100 * (pretax + posttax)
        return (numerator / denominator).quantize(CENTS, ROUND_HALF_UP)


def ev_rate(rates, year):
    """The ATO's home charging rate for an FBT year: -> (the year whose rate it is, cents per km), the year's
    own or else the latest earlier year's, or None for a year before the first. `rates` is {year: cents}."""
    earlier = [y for y in rates if y <= year]
    return (max(earlier), rates[max(earlier)]) if earlier else None


def rate_over_days(spans, rate_of_year):
    """The marginal rate across some day ranges (first, last; both included), each day at the rate of the income year
    (1 July to 30 June) it falls in, `rate_of_year(year)` (None: not known). -> the rate weighted by days, or None when
    any day's rate isn't known or there are no days."""
    total, weighted = 0, Decimal(0)
    for first, last in spans:
        day = first
        while day <= last:
            year = day.year if day.month >= 7 else day.year - 1
            end = min(last, date(year + 1, 6, 30))
            rate = rate_of_year(year)
            if rate is None:
                return None
            days = (end - day).days + 1
            total, weighted = total + days, weighted + Decimal(rate) * days
            day = end + timedelta(days=1)
    if not total:
        return None
    with localcontext() as ctx:
        ctx.prec = 60
        return weighted / total


def charging_reimbursement(km, cents_per_km, rate_percent):
    """The home charging reimbursement for `km` driven at `cents_per_km`, and the tax saved on it (at the
    marginal rate). None when the distance or the rate isn't known, or the readings run backwards: never 0."""
    if km is None or cents_per_km is None or rate_percent is None or km < 0:
        return None
    amount = (km * cents_per_km / 100).quantize(CENTS, ROUND_HALF_UP)
    return {"km": km, "cents_per_km": cents_per_km, "amount": amount,
            "tax_saved": (amount * rate_percent / 100).quantize(CENTS, ROUND_HALF_UP)}


def reimbursed_spans(leases, year, today):
    """The day ranges (first, last), both included, of an FBT year in which a lease that has its home charging
    reimbursed was held, up to `today`. Days to come are not counted yet."""
    spans = []
    for row in leases:
        if not row["charging_reimbursed"] or not days_held(row, year):
            continue
        first, last = _held_range(row, year)
        last = min(last, today)
        if first <= last:
            spans.append((first, last))
    return sorted(spans)


def after_tax_lines(rows, key):
    """The year's after-tax part of the report, from its expenses (db.after_tax_facts, each with the marginal rate
    of its date's income year): `key(row)` names the line each belongs to.
    -> {line: {"invoice": total, "effective": total or None, "packaged": n}}. A line's effective total is None while
    any of its packaged expenses has none (no bracket chosen for its year)."""
    lines = {}
    for row in rows:
        packaged = is_packaged(row["packaged"], row["lease_id"] is not None, row["budget_pool"], row["kind"])
        cost = effective_cost(row["amount"], packaged, row["gst_free"], row["pretax_payment"], row["posttax_payment"],
                              row["marginal_rate"], gst_amount=row["gst_amount"])
        line = lines.setdefault(key(row), {"invoice": Decimal(0), "effective": Decimal(0), "packaged": 0})
        line["invoice"] += row["amount"]
        line["packaged"] += 1 if packaged else 0
        line["effective"] = None if cost is None or line["effective"] is None else line["effective"] + cost
    return lines


# ---- kilometre pace (roadmap L4) -------------------------------------------------------------------
# The provider's statement works out how far the car must go in an FBT year from the lease's estimated
# km per year, pro rata for the days held. These figures add what a statement can't show: how far it
# has gone, and the pace needed from here. Pure arithmetic: the caller reads the odometers
# (sources.odometer_at) and passes them in, and a figure that depends on a missing input is None, never 0.

TENTH = Decimal("0.1")
PACE_FIGURES = ("expected_km", "end_km_needed", "per_day_needed", "driven", "per_day_driven", "expected_so_far",
                "ahead", "per_day_to_go")


def _tenth(value):
    return None if value is None else value.quantize(TENTH, ROUND_HALF_UP)


def _decimal(value):
    return None if value is None else Decimal(str(value))


def km_pace(lease, year, today, start_km, current_km):
    """The kilometre pace of one lease in one FBT year, as a dict.

    `start_km` is the odometer at the start of the days held (1 April, or the lease start if later) and
    `current_km` the odometer now, or at the end of the days held when that is already past. Either may be
    None (not known). Days so far are whole days gone before `today`, so today still counts as to drive.
    `ahead` is the km driven less the pro-rata km expected by now: positive is ahead, negative behind.
    `per_day_to_go` is what must be driven each remaining day to land on the estimate (None when none
    remain). Km figures are rounded to 0.1 once, at the end; `state` is not_held, upcoming, current or past."""
    first, last = _held_range(lease, year)
    held, in_year = days_held(lease, year), days_in_fbt_year(year)
    estimate = lease.get("estimated_km_per_year")
    out = {"year": year, "first": first, "last": last, "days_held": held, "days_in_year": in_year,
           "estimate": estimate, "start_km": None, "elapsed_days": 0, "remaining_days": held,
           **dict.fromkeys(PACE_FIGURES)}
    if not held:
        out["state"] = "not_held"
        return out
    out["state"] = "upcoming" if today < first else "past" if today > last else "current"
    elapsed = max((min(today, last + timedelta(days=1)) - first).days, 0)
    out["elapsed_days"], out["remaining_days"] = elapsed, held - elapsed
    start = None if out["state"] == "upcoming" else _decimal(start_km)
    now = None if out["state"] == "upcoming" else _decimal(current_km)
    out["start_km"] = _tenth(start)
    if estimate is None:
        return out
    yearly = Decimal(estimate)
    expected = yearly * held / in_year
    out["expected_km"], out["per_day_needed"] = _tenth(expected), _tenth(yearly / in_year)
    if start is not None:
        out["end_km_needed"] = _tenth(start + expected)
    if start is None or now is None:
        return out
    driven = max(now - start, Decimal(0))
    so_far = yearly * elapsed / in_year
    out.update(driven=_tenth(driven), expected_so_far=_tenth(so_far), ahead=_tenth(driven - so_far))
    if elapsed:
        out["per_day_driven"] = _tenth(driven / elapsed)
    if out["remaining_days"]:
        out["per_day_to_go"] = _tenth(max(expected - driven, Decimal(0)) / out["remaining_days"])
    return out



def km_pace_year(parts, year, today):
    """The kilometre pace of a whole FBT year for a car, across every lease held in it: a lease that ends part-way
    and the next one each count their own days and their own estimate. `parts` is a list of (lease, start_km,
    current_km), the odometers as km_pace takes them. A lease with no estimate, or not held in the year, is left
    out; None when none is left. `leases` is how many count. A distance needs every counted lease's odometers
    (one still to start has driven nothing yet): else it is None, never a part standing in for the whole.
    Rounded to 0.1 once, at the end, so one lease gives exactly what km_pace gives."""
    in_year = days_in_fbt_year(year)
    counted = []
    for row, start_km, current_km in sorted(parts, key=lambda p: p[0]["start_date"]):
        pace = km_pace(row, year, today, start_km, current_km)
        if pace["state"] != "not_held" and pace["estimate"] is not None:
            counted.append((pace, _decimal(start_km), _decimal(current_km)))
    if not counted:
        return None
    held = sum(p["days_held"] for p, _, _ in counted)
    elapsed = sum(p["elapsed_days"] for p, _, _ in counted)
    expected = sum((Decimal(p["estimate"]) * p["days_held"] / in_year for p, _, _ in counted), Decimal(0))
    so_far = sum((Decimal(p["estimate"]) * p["elapsed_days"] / in_year for p, _, _ in counted), Decimal(0))
    states = {p["state"] for p, _, _ in counted}
    state = states.pop() if len(states) == 1 else "current"
    first_pace, first_start, _ = counted[0]
    start = None if first_pace["state"] == "upcoming" else first_start
    driven = Decimal(0)
    for pace, begin, now in counted:
        if pace["state"] == "upcoming":
            continue
        if begin is None or now is None:
            driven = None
            break
        driven += max(now - begin, Decimal(0))
    if state == "upcoming":
        driven = None
    out = {"year": year, "leases": len(counted), "first": first_pace["first"], "last": counted[-1][0]["last"],
           "days_held": held, "days_in_year": in_year, "elapsed_days": elapsed, "remaining_days": held - elapsed,
           "state": state, "start_km": _tenth(start), **dict.fromkeys(PACE_FIGURES)}
    out["expected_km"], out["per_day_needed"] = _tenth(expected), _tenth(expected / held)
    if start is not None:
        out["end_km_needed"] = _tenth(start + expected)
    if driven is None:
        return out
    out.update(driven=_tenth(driven), expected_so_far=_tenth(so_far), ahead=_tenth(driven - so_far))
    if elapsed:
        out["per_day_driven"] = _tenth(driven / elapsed)
    if out["remaining_days"]:
        out["per_day_to_go"] = _tenth(max(expected - driven, Decimal(0)) / out["remaining_days"])
    return out
