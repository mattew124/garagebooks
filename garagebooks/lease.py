"""Novated lease facts that are data, and the read-only figures worked out from a lease's details.

Pure functions, no database or Flask. What a lease record says comes from the provider's statement
(db.py stores it); this module only names the fixed lists (pools, pay frequencies, FBT methods) and
works out figures to show beside the entry, so a typing mistake is visible. It records what the
statement says and is not tax advice.
"""

from decimal import Decimal, ROUND_HALF_UP

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

