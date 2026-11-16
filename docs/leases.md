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

## The FBT year

Fringe benefits tax is counted over its own year, **1 April to 31 March** (the income tax year is 1 July
to 30 June). You never pick a year: each expense's date, in the app's time zone, decides the FBT year it
falls in, and its income tax year too. A year is named by the year it starts in, so 1 April 2025 to
31 March 2026 is **FBT 2025–26**.

Choose **Costs, taxable value and contributions by FBT year** on a car's lease page. It opens the current
FBT year, with links to the previous and next years that have a lease or an expense. It shows:

- **Days held**: the days the car was held under a lease in that year, out of the days in it (365, or 366
  when the year holds 29 February). Both the first and last day count. A lease that starts or ends part-way
  counts only its part of the year, and a refinanced car with two leases in one year adds each lease's days.
- **Taxable value** under the statutory formula: the lease's FBT base value × its statutory percentage ×
  days held ÷ days in the FBT year. The base value and percentage are the ones entered on the lease, never
  fixed in the app. It is rounded to the cent once, at the end. It is nil for an exempt electric car. For
  the operating cost method the page says the statutory figure doesn't apply and that GarageBooks doesn't
  work that method out: use the figure on your provider's statement.
- **Contribution required**: the post-tax contribution that brings FBT to nil under the employee
  contribution method equals the taxable value (nil for an exempt electric car). Against it, the
  contributions **made so far**, **expected by the end of the year**, and the **shortfall or excess**.
  They are the lease's post-tax payment times the pays in the part of the year the car was held. The app
  counts pay dates from the lease's start date, stepping by its pay frequency (weekly, fortnightly, or the
  same day each month), so your provider's own pay dates may differ a little.
- **Running costs by pool**: the year's expenses grouped by their category's budget pool. A category in no
  pool has its own **Not in a pool** line, so nothing is hidden. Purchases are left out and listed apart.
  For a car TeslaMate tracks, TeslaMate's charging cost for the year is added to **Fuel or charging**. If
  TeslaMate can't be read, that pool and the total say *unavailable* with the reason instead of a smaller
  figure.
- Each pool's **yearly budget** in force, pro-rated by the days held, for reference. This release doesn't track
  what each pool has paid out.

These are the statement's figures worked through the published formula. They are not tax advice, and no
personal details are stored.

<!-- screenshot: fbt-report - The FBT year report for a car: days held, taxable value, contributions and running costs by pool -->

## Categories and budget pools

Each category belongs to at most one of the provider's budget pools, so a later release can set what
you spend against what was budgeted. The standard categories start with sensible pools (Insurance to
insurance, Registration to registration and CTP, the tyre categories to tyres, the filters to
maintenance, Charging and Fuel to fuel or charging, Initial purchase to none). Change a category's pool
on the **Categories** page; your choice is kept when the app is upgraded.

<!-- screenshot: category-pools - The Categories page, with the lease budget pool beside each category -->

## Reminders for registration and the end of the lease

The registration date and the lease's end date become [reminders](reminders.md#registration-and-lease-end)
by themselves. Change a date here and the reminder follows; delete the lease and they go.

## Upgrading

The app adds its lease tables and the new column on categories itself when it starts. If you use the
restricted database role (`SKIP_MIGRATE=1`), run `scripts/provision-db.sh` again first: this release also adds the
`garagebooks.fbt_year(date)` function, which the dashboard and the app share.
