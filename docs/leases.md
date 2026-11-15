# Leases

If a car is paid for through a novated lease, GarageBooks can hold what the provider's package
confirmation says about it. This is the first part of the
[novated lease work on the roadmap](roadmap.md#novated-leases-and-salary-packaging): the later parts
(the FBT year, budgets against what was spent, kilometre pace) read from what you enter here.

!!! note "A record, not tax advice"
    GarageBooks records what your statement says and shows a few sums from it so that a typing mistake
    stands out. It doesn't work out tax, and nothing here replaces your provider or a tax adviser.
    No personal details are stored: no names, employer, payroll or bank numbers, and the form has no
    place for them.

Open **Cars** and choose **Lease** on a car. The page shows that car's current lease laid out the way a
provider's statement reads, in groups, with **Edit** and **Delete** (which asks first). Leases that
have ended, or have not started yet, are listed below it. All figures on this page are invented.

<!-- screenshot: lease-page - A car's lease page: finance, payments, FBT, budgets and registration, with the worked-out figures -->

## What is recorded

Choose **Add a lease** and copy the figures from the statement. Leave a box empty when the statement
doesn't give it.

| Group | What you enter |
|---|---|
| Finance | The financier, an optional package name, start and end dates, the refinance date if there was one, the term in months, the car's cost (or financed amount), and the residual (balloon) as an amount or a percentage of the cost. |
| Payments | How often it is paid (weekly, fortnightly or monthly), the pre-tax and post-tax parts of each payment, and the provider's admin fee for each pay. |
| Fringe benefits tax | The FBT base value, the valuation method (statutory formula or operating cost), the statutory percentage (20 unless your statement says otherwise), whether the car is exempt as an eligible electric car, the tax treatment (for example the employee contribution method) and the provider's estimated km per year. |
| Yearly budgets | The yearly budget for each running cost: lease payments, fuel or charging, insurance, registration and CTP, maintenance, tyres and other running costs. They take effect from the lease's start date. |
| Registration | When registration expires, and whether the provider renews it. |

Enter the residual as either an amount or a percentage: with the car's cost, the other is worked out.
If you enter both, they must agree.

A car can have more than one lease over time, for example when a lease is refinanced and a new one
starts from that date. Their dates may not overlap, so a car has at most one current lease. Two cars in
the same package each get their own lease; give them the same package name and they are easy to
recognise as one.

## Figures worked out for you

These are shown on the lease page, read-only, from what you entered:

- **Total per pay**: the pre-tax part, the post-tax part and the admin fee together.
- **Pays per year**: 52 for weekly, 26 for fortnightly and 12 for monthly, and **yearly payments**,
  the total per pay times that.
- **Total of budgets**, set beside what the pre-tax and post-tax payments come to in a year (before
  the admin fee). The two normally match, so a difference is worth checking against the statement.
- **Residual** as an amount and a percentage, held against the ATO's minimum for the term (65.625%,
  56.25%, 46.875%, 37.5% and 28.125% for 1 to 5 years, as on
  [Novated lease calculations](lease-calculations.md)). A residual below the minimum is flagged. The
  comparison is shown for terms of whole years only.
- **Days remaining** until the lease ends (or how long ago it ended).

## Categories and budget pools

Each category belongs to at most one of the provider's budget pools, so a later release can set what
you spend against what was budgeted. The standard categories start with sensible pools (Insurance to
insurance, Registration to registration and CTP, the tyre categories to tyres, the filters to
maintenance, Charging and Fuel to fuel or charging, Initial purchase to none). Change a category's pool
on the **Categories** page; your choice is kept when the app is upgraded.

<!-- screenshot: category-pools - The Categories page, with the lease budget pool beside each category -->

## Upgrading

The app adds its lease tables and the new column on categories itself when it starts. If you use the
restricted database role (`SKIP_MIGRATE=1`), run `scripts/provision-db.sh` again first.
