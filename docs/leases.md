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
provider's statement reads, in groups, with **Edit** and **Delete** (which asks first). Lease periods that
have ended, or have not started yet, are listed below it, each folded to one line: open one to see it. All figures on this page are invented.

<!-- screenshot: lease-page - A car's lease page: finance, payments, FBT, budgets and registration, with the worked-out figures -->

## What is recorded

Choose **Add a lease period**. The page starts with a **Documents** card, then the boxes below. Copy the
figures from the statement, or let the lease form fill them in. Leave a box empty when the statement
doesn't give it.

<!-- screenshot: lease-form-documents - The Add a lease page: the Documents card with the lease form and agreement PDFs, above the lease boxes -->

### Documents

- **Lease form PDF**: the provider's lease form or package confirmation. Choose it first, from the
  computer, by dropping it on the box, or from the car's OneDrive or invoice folder. The boxes are then
  filled in from it: provider, start, end, term, pay frequency, the pre-tax payment per pay, the FBT base
  value, whether the electric car exemption applies, the estimated km per year, and the yearly budgets when
  [budgets are turned on](#lease-budgets). Only the lease's own figures are read. Names, employer,
  payroll, bank and registration details are never read or stored. Check every box before saving.
- **Lease agreement PDF**: kept with the lease for reference. Nothing is read from it.

Chosen PDFs are saved in a **Lease** subfolder of the car's folder when the lease is saved, and linked from
the lease page. If no folder is chosen for the car, the lease still saves and says the PDF wasn't kept;
the lease form can still be read to fill in the boxes.

### The boxes

| Group | What you enter |
|---|---|
| Finance | Whether it is an associate lease, the salary packaging provider, the financier, start and end dates, the refinance date if there was one, the term in months, the car's cost (or financed amount), and the residual (balloon) as an amount or a percentage of the cost. For an associate lease, the car's value for depreciation at the lease start (blank uses its cost). |
| Payments | How often it is paid (weekly, fortnightly or monthly), the pre-tax and post-tax parts of each payment, and the provider's admin fee for each pay. |
| Fringe benefits tax | The FBT base value, the valuation method (statutory formula or operating cost), the statutory percentage (20 unless your statement says otherwise), whether the car is exempt as an eligible electric car, the tax treatment (for example the employee contribution method) and the provider's estimated km per year. |
| Yearly budgets | Only when [budgets are turned on](#lease-budgets). The yearly budget for each running cost: lease payments, fuel or charging, insurance, registration and CTP, maintenance, tyres and other running costs. They take effect from the lease's start date. |
| Registration | When registration expires, and whether the provider renews it. |

The lease end is filled in from the start date and the term: the day before the same date that many
months on. Leave the term empty and it is worked out from the two dates.

Enter the residual as either an amount or a percentage: with the car's cost, the other is worked out.
If you enter both, they must agree.

**Associate lease.** Tick this for a car you own outright and lease to your employer through an associate.
There is no financier, so that box is not needed, and the residual (balloon) boxes and the ATO
minimum-residual check go away.

A car can have more than one lease period over time: a lease that is renewed every two years, say, or
one that is refinanced with a new one starting from that date. Add each period as its own lease, with its
own lease form and lease agreement. Their dates may not overlap, so a car has at most one current lease. Two cars in
the same package each get their own lease. Packaging is recorded per car.

## Figures worked out for you

These are shown on the lease page, read-only, from what you entered:

- **Total per pay**: the pre-tax part, the post-tax part and the admin fee together.
- **Pays per year**: 52 for weekly, 26 for fortnightly and 12 for monthly, and **yearly payments**,
  the total per pay times that.
- **Total of budgets** (when budgets are on), set beside what the pre-tax and post-tax payments come to in a year (before
  the admin fee). The two normally match, so a difference is worth checking against the statement.
- **Residual** (not for an associate lease) as an amount and a percentage, held against the ATO's minimum for the term (65.625%,
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
- **Running costs by pool** (with budgets on; with them off, **Running costs** lists the year's expenses by
  category instead): the year's expenses grouped by their category's budget pool. A category in no
  pool has its own **Not in a pool** line, so nothing is hidden. Purchases are left out and listed apart.
  For a car TeslaMate tracks, TeslaMate's charging cost for the year is added to **Fuel or charging**. If
  TeslaMate can't be read, that pool and the total say *unavailable* with the reason instead of a smaller
  figure.
- Each pool's **yearly budget** in force, pro-rated by the days held, for reference. This release doesn't track
  what each pool has paid out.

- **After tax**: the year's running costs on the invoices and what they cost after tax (TeslaMate's charging
  counted as paid personally), and the year's after-tax cost of running the car. The breakdown by pool or
  category, and every year side by side, are on the [dashboard](dashboard.md#cost-after-tax). For an electric car
  whose lease has its home charging reimbursed it adds the
  [reimbursement](#cost-after-tax): the km driven, the ATO's rate used, the reimbursement and the tax saved on it.
  With no tax bracket chosen in Settings it says so and shows no after-tax figure.

- **Associate lease, for the household** (an associate lease only): for the income years the FBT year runs
  across, the payments the associate receives, their depreciation, their extra tax (net of the small business
  income tax offset), the tax you save on the pre-tax payments, and what the lease costs or saves the household.
  It needs the associate's tax bracket in Settings and the car's value (the lease form's **Value for
  depreciation**, or its cost). The arithmetic is on
  [Novated lease calculations](lease-calculations.md#in-garagebooks-an-associate-lease-after-tax).

These are the statement's figures worked through the published formula. They are not tax advice, and no
personal details are stored.

<!-- screenshot: fbt-report - The FBT year report for a car: days held, taxable value, contributions and running costs by pool -->
<!-- screenshot: fbt-associate - The Associate lease part of the FBT year report: depreciation, the associate's extra tax and the household figure -->

## Cost after tax

A packaged expense costs less than its invoice: the GST is claimed back and the rest comes from pre-tax
salary. GarageBooks can show both figures, from two things you enter once. Nothing here is tax advice, and it
never asks for a salary.

1. In **Settings**, the **Cost after tax** table has one row per income year (1 July to 30 June). For each,
   choose the **tax bracket** your taxable income falls in (as the ATO lists them, such as
   `$45,001 – $135,000 (30%)`) and your **Medicare levy** status (*Not exempt*, *Half exempt* or *Exempt*). An
   expense's rate is its income year's bracket plus the levy (2%, 1% or nothing). A year left on *Same as the year
   before* uses the latest earlier year's choice, and the table says which. Once a car has an associate lease,
   the table also asks for the **Associate's** bracket and levy status in the same way. With no year chosen no after-tax
   figure is shown anywhere: the pages say so and link to Settings. Only the bracket is kept, never a salary.
2. On the **Categories** page, tick **GST-free** for a category whose invoices carry no GST. Registration
   starts ticked. Anything else is taken to carry GST at 1/11 of the invoice.

Then, for a car with a lease:

- On the **Add expense** form, **Paid from** (shown only for a car with a lease) is *Automatic* by default: an
  expense in a category with a budget pool, dated within a lease, is treated as paid from the package, and a
  purchase never is. Choose *The package (pre-tax salary)* or *Personally* when that is wrong.
- The expense list shows a packaged expense's cost after tax in small type beside its amount, and an
  after-tax total beside the invoice total. A car with no lease, or no tax bracket chosen, shows nothing extra.
- The FBT year report has an **After tax** part, and the dashboard has two panels below the others (see
  [the dashboard](dashboard.md#cost-after-tax)).

For an electric car on a novated or associate lease whose provider reimburses home charging at the ATO's
cents-per-km rate, tick **Home charging is reimbursed at the ATO's cents-per-km rate** on the lease form (it is
offered for a battery electric car only: the ATO's rate doesn't cover a plug-in hybrid). The FBT year report then
shows the reimbursement (the km driven in the days held, times the rate) and the tax saved on it. The ATO's rates
by FBT year are listed in **Settings**, with where each came from and when it was last checked: the `ato_rates`
[scheduled job](scheduled-jobs.md) reads them from the ATO each week, so there is nothing to type. The distance needs the odometer, so with no
reading it says *not known* rather than counting it as nothing.

The formulas, their simplifications and a worked example are on
[Novated lease calculations](lease-calculations.md#cost-after-tax-l5).

<!-- screenshot: settings-after-tax - The Settings page: the tax bracket and Medicare levy for each income year, and the ATO's cents-per-km rates by FBT year -->
<!-- screenshot: expense-paid-from - The Add expense form for a car with a lease, with the Paid from choice -->
<!-- screenshot: list-after-tax - The expense list, with a packaged expense's cost after tax beside its amount and the after-tax total -->
<!-- screenshot: fbt-after-tax - The After tax part of the FBT year report, with the home charging reimbursement -->

## Kilometre pace

When a lease has an **Estimated km per year**, the FBT year report has a **Kilometre pace** section for each lease
held in the year shown, and the car's lease page has a short version for the current FBT year. When a lease ends
part-way through an FBT year and the next one starts, the report shows each lease's part and then **the FBT year in
total**: both parts added up, so the year expects the whole year's distance. The lease page's short version counts
the whole FBT year too, and says so (*both leases held in it*); with one lease that started during the year, it says
the figures run from the lease's start. They work out:

- the **odometer at the start** (1 April, or the lease's start if later) and the **km expected by the end**
  (31 March, or the lease's end if earlier): the estimate × days held ÷ days in the FBT year;
- the **odometer needed at the end** (the start reading plus the km expected) and the **km per day needed** for
  the whole period;
- what a provider's statement can't show: the **km driven so far**, the **km per day actually driven**, how far
  the car is **ahead of or behind** the pro-rata pace today, and the **km per day needed from today** to land on
  the estimate.

A year that is over counts the whole period, to its end date. A year that hasn't started shows only what is
expected. The odometer is TeslaMate's where it is available, otherwise your latest reading (an expense's odometer
counts) on or before the moment: the start of the first day held, and now (or the end of the last day held).
A figure that needs an odometer that isn't known says **not known**, with the reason (TeslaMate's figures are
held back, or no reading is on or before that date), never 0. A lease with no estimate says so, with a link to
edit it. The same figures are on the dashboard's [Kilometre pace](dashboard.md#kilometre-pace) panel. See
[Lease calculations](lease-calculations.md#kilometre-pace-l4) for the arithmetic.

<!-- screenshot: km-pace - The Kilometre pace section of the FBT year report -->
<!-- screenshot: km-pace-two-leases - The Kilometre pace section of an FBT year with two leases: each lease, then the FBT year in total -->

## Lease budgets

Budgets are optional and off by default. They suit a salary-packaged car whose provider sets a yearly
budget for each running cost. Turn on **Track lease budgets** in **Settings** to add the yearly budgets to a
car's lease, a budget pool to each category, and the budgets to the FBT year report. While it is off, the
lease pages, the Categories page and the FBT year report leave budgets out, and the FBT report lists
running costs by category. Budgets already entered are kept, and come back when you turn it on.

## Categories and budget pools

With budgets on, each category belongs to at most one of the provider's budget pools, so a later release can set what
you spend against what was budgeted. The standard categories start with sensible pools (Insurance to
insurance, Registration to registration and CTP, the tyre categories to tyres, the filters to
maintenance, Charging and Fuel to fuel or charging, Initial purchase to none). Change a category's pool
on the **Categories** page (shown only while budgets are on); your choice is kept when the app is upgraded.

<!-- screenshot: category-pools - The Categories page, with the lease budget pool beside each category -->

## Claiming an expense

Running costs that the provider reimburses (fuel or charging, tyres, maintenance, insurance,
registration and other costs, but not the lease payments themselves) are claimed back from the
provider. If you run a [claims connector](claims-connector.md) for your provider, GarageBooks can send
the claim for you: an expense with a **Claim** link beside it, because it is in one of those pools,
is more than $0, falls inside a lease of the car and has not been claimed yet.

1. Choose **Claim** (on the list, the **Claims** page or the expense's Edit page). The page asks the
   connector what it would claim and shows the provider's own summary, the amount, the GST and the
   amount without GST. This can take up to a minute.
2. If the provider needs more, such as a description or the dates of an insurance policy, the page
   shows those boxes with the provider's own labels. Fill them in and choose **Check again**. You can
   also type the GST included in the amount, or leave it empty and let the provider work it out.
3. If the provider has signed you out, the page offers **Sign in**, and a box for the code it sends you.
4. A warning, such as one saying the invoice was already claimed, is shown in red and **Submit claim**
   is withheld. Otherwise choose **Submit claim**. This sends a real claim to the provider.
5. You return to the list with a message giving the provider's reference. The expense now shows a small
   badge with that reference and its status, and its Edit page shows the details.

The invoice sent is the PDF kept in the car's folder on the server. An expense whose invoice is only a
link (OneDrive, say) has to have the file attached first: Edit it and choose the PDF. The file can't be
larger than 10 MB.

<!-- screenshot: claim-page - The Claim page: what will be claimed, the amounts and the Submit claim button -->

<!-- screenshot: claims-page - The Claims page: the provider status, the expense list with where each expense stands on claiming, and the provider history lined up with them -->

**Claims** in the header is the ledger. At the top it says whether the provider is signed in (with **Sign in**
when it isn't) and when its history was last synced. Below is the same expense list as the **All** page, with the
same filters (category, car, dates, sort) and a **Claims** filter, and an extra **Claim** column saying where each
expense stands: claimed (with its reference, status, approved amount and paid date, and whether the provider has
**reimbursed it in full**, part paid it, or is still to pay), ready to claim, or why it can't be claimed (its
category has no claimable pool, it is a refund, or no lease covers its date). **Provider history** is what the
provider's own transaction list says, with the expense each payment belongs to (through its claim, so part
payments work), or **No matching expense**. The one exception is an electric car's per-kilometre charging
reimbursement, which has no expense. **Sync provider history** fetches the claims and transactions through the
connector and keeps a copy in the database. The provider's site is slow, so it runs in the background (the page shows
it and updates itself), and after the first sync it only fetches what is new (**Full resync** goes back to your first
expense). **Refresh claim statuses** asks the provider about each claim that
isn't yet marked paid. A claimed expense can't be deleted by mistake: remove its claim record first (Edit, then
**Remove claim record**), which doesn't cancel the claim at the provider.

## Reminders for registration and the end of the lease

The registration date and the lease's end date become [reminders](reminders.md#registration-and-lease-end)
by themselves. Change a date here and the reminder follows; delete the lease and they go.

## Upgrading

The app adds its lease tables and the new column on categories itself when it starts. If you use the
restricted database role (`SKIP_MIGRATE=1`), run `scripts/provision-db.sh` again first: this release also adds the
`garagebooks.fbt_year(date)` function, which the dashboard and the app share, and the cost-after-tax columns, the
`garagebooks.ev_charging_rate` table and the `garagebooks.expense_after_tax(tz)` function. The tax rate by income
year adds the `garagebooks.tax_bracket` and `garagebooks.tax_choice` tables and the `income_year`,
`tax_table_year`, `tax_position` and `marginal_rate` functions. A marginal rate entered in an earlier release
becomes, for every income year up to the current one, the bracket whose rate plus the 2% levy is nearest it
(30% for 32, say): check the table in Settings after upgrading.
