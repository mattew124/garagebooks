# Novated lease calculations

The calculations behind the [roadmap's](roadmap.md#novated-leases-and-salary-packaging) lease
items, L1 to L8. They come from two sources: the figures a salary packaging provider prints on a
package statement, and a household's own spreadsheet comparing lease terms. Every number on this
page and in the example workbook is invented.

**[Download the example workbook](assets/lease-calculator-example.xlsx)** (`.xlsx`, Excel or
LibreOffice). Blue cells on the **Inputs** sheet are the ones to change. **Scenario** works out
three years side by side, **Rates** shows the tax tables for each year of the lease, and **RateData**
holds every year. The tax tables are read from the ATO's own pages when the workbook is built
([how](#live-rates-from-the-ato)), and each person picks a
[tax bracket and Medicare status](#tax-year-bracket-and-medicare-status) from a list.

!!! warning "Not tax advice"
    This is a model for comparing options, not a tax return. The tax rates are those the ATO had
    published on the date shown on the **Rates** sheet, and they change most years: build the workbook
    again for a newer set. Ask your provider or a tax adviser before signing anything.

## From the provider's statement

A package statement lists, for each car, the figures below. GarageBooks records them per car (L1).

| Figure | What it is |
|---|---|
| FBT base value | The car's cost for FBT purposes. |
| Statutory percentage | 20% for any lease under the statutory formula method. |
| Estimated annual taxable value | Base value × 20%, before the days held are taken into account. |
| Tax treatment | Usually the *employee contribution method*: you pay part of the package from after-tax pay, which cancels the FBT. |
| Estimated contribution required | The after-tax amount needed to bring FBT to nil (equal to the taxable value). |
| Annual FBT liability | Nil when the contribution is made, or when the car is an exempt electric vehicle. |
| Annual budgets | Only when budgets are turned on in Settings. Lease payment (inc GST), fuel or charging, insurance, registration, maintenance, tyres, other running costs, and their total. |
| Per-pay deductions | Each budget's share per pay, marked pre-tax or post-tax, plus the provider's admin fee. |
| Lease dates | Start, end, and refinance date if the lease was refinanced. |

### FBT year and taxable value (L2)

The FBT year runs from 1 April to 31 March.

```text
days held        = days the car was held in the FBT year (inclusive)
taxable value    = base value × 20% × days held ÷ days in the FBT year
                   (nil for an exempt electric car)
contribution     = taxable value      (employee contribution method: FBT becomes nil)
```

GarageBooks works these out for each FBT year on the [FBT year report](leases.md#the-fbt-year), from the lease's own
base value and percentage.

### Kilometre pace (L4)

The provider's statement works out how far the car must go, from an estimated annual distance:

| | Figure | Formula |
|---|---|---|
| (a) | Minimum annual km for the estimate's range | from the statement |
| (b) | Start date in this FBT year | 1 April, or the lease start |
| (c) | Odometer at (b) | **TeslaMate** |
| (d) | End date in this FBT year | 31 March, or the lease end |
| (e) | Days in the FBT year | 365 or 366 |
| (f) | Days held this FBT year | d − b |
| (g) | Km to travel by (d) | a × f ÷ e |
| (h) | Odometer needed at (d) | c + g |
| (i) | Km per day needed | g ÷ f |

GarageBooks shows (a) to (i) for each lease with an estimate, on the FBT year report and the lease page, and adds what the
statement can't show, from the odometer (TeslaMate's, or the latest reading entered by hand on or before the moment):

| | Figure | Formula |
|---|---|---|
| (j) | Km driven so far | odometer now − (c); for a year that is over, the odometer at (d) − (c) |
| (k) | Days so far | whole days from (b) to today, up to (f) (today still counts as to drive) |
| (l) | Km per day actually driven | j ÷ k |
| (m) | Km ahead (+) or behind (−) the pro-rata pace | j − a × k ÷ e |
| (n) | Km per day needed from today | (g − j) ÷ (f − k), at least 0; blank when no days remain |

Days held count both ends of the period, so (f) is one more than d − b. Each figure is rounded to 0.1 km once,
at the end.

**An FBT year with two leases.** When one lease ends part-way through an FBT year and the next starts, each lease
works out its own (a) to (n) over its own part of the year. The FBT year as a whole adds them up: (f), (g), (j), (k)
and the pro-rata km a × k ÷ e are summed over the leases, (c) is the first lease's, and (i), (l), (m) and (n) are
worked out from the sums. With one estimate of 15,000 km for both leases, the year expects 15,000 km, the same as one
lease held all year. The FBT year report shows each lease and then the year in total. The lease page and the
dashboard show the year in total. A lease still to start in the year adds its expected km but nothing driven. The
total's distance is *not known* if any lease's odometer isn't known. A figure that needs an odometer that isn't known is blank (shown as *not known*), never 0. The dashboard's
**Kilometre pace** panel works out the same figures in SQL, using `garagebooks.odometer_at(car, moment)`, which answers like the
app does with TeslaMate present, absent and held back.

### Cost after tax (L5)

An expense paid from the package costs less than its invoice: the GST is claimed back, and the rest comes
out of pre-tax salary, so it saves the tax you would otherwise have paid on that part of your pay. This is
arithmetic on what you enter, with deliberate simplifications, and **it is not tax advice**.

Two things are entered once: in Settings, for each **income tax year** (1 July to 30 June), the
**tax bracket** your taxable income falls in and your **Medicare levy** status (the bracket only, never a
salary: see [Tax year, bracket and Medicare status](#tax-year-bracket-and-medicare-status)); and, for each
lease, its pre-tax and post-tax payments. An expense's date decides its income year, and its **rate** is that
year's:

```text
rate        = the bracket's rate + the Medicare levy (2%, 1% half exempt, 0 exempt)
```

A year with no choice of its own uses the latest earlier year's, for the income its bracket stands for (its
middle, or $10,000 into the top bracket), in that year's brackets: a choice of $45,001 – $120,000 (32.5%) in
2022–23 stands for $82,500.50, which is in $45,001 – $135,000 (30%) from 2024–25. A year the ATO hasn't
published uses its newest brackets. Before the first year chosen there is no rate. For an expense paid from the
package, on a lease that covers its date:

```text
ex GST      = invoice amount − its GST            if the expense has a GST read from the document
            = invoice amount                      else if the category is GST-free
            = invoice amount × 10 ÷ 11            otherwise
post share  = post-tax payment ÷ (pre-tax payment + post-tax payment)      (0 when the two add to nothing)
after tax   = ex GST × ((1 − post share) × (1 − rate) + post share)
```

An expense paid personally costs its invoice amount. A rebate (a negative amount) follows the same formula.
The result is rounded half up to the cent, once, at the end. A packaged expense on a date no lease covers
(you chose *The package* by hand) has a post share of 0.

**Which expenses are packaged.** On the expense form, **Paid from** is *Automatic*, *The package* or
*Personally*. *Automatic* means paid from the package when a lease of that car covers the expense's date (in
the app's time zone), the category has a budget pool, and the category isn't a purchase. The other two choices
override that. Existing expenses are all *Automatic*, so nothing has to be entered again.

**Simplifications.** GST is taken to be a flat 1/11 of an invoice that isn't GST-free (the Categories page has
a **GST-free** tick, ticked for Registration to start with). The part of the package paid from after-tax pay is
taken to be the lease's own post-tax share of its payments, applied to every packaged expense. Payroll
timing, the cap on a package, and anything about your tax return other than the marginal rate are not
modelled.

**Worked example** (invented figures). An insurance invoice of $1,100.00 (GST included), dated in 2025–26, on a
lease paying $300.00 pre-tax and $20.00 post-tax a pay. For 2025–26 the bracket chosen is $45,001 – $135,000
(30%), not exempt from the Medicare levy, so the rate is 32%:

```text
ex GST      = 1,100.00 × 10 ÷ 11                        = 1,000.00
post share  = 20.00 ÷ (300.00 + 20.00)                  = 0.0625
after tax   = 1,000.00 × (0.9375 × 0.68 + 0.0625)       = 700.00
```

The same $1,100.00 on a GST-free category is 1,100.00 × 0.7 = $770.00.

**Home charging reimbursement.** For a battery electric car on a lease that has *Home charging is reimbursed at
the ATO's cents-per-km rate* ticked, the FBT year report adds, for each FBT year:

```text
km             = odometer at the end of the days held in the year (or now, if earlier)
                 − odometer at their start
reimbursement  = km × cents per km ÷ 100
tax saved      = reimbursement × rate        (each day at its income year's rate, weighted by days)
after-tax cost of the year = after-tax running costs (and TeslaMate's charging, paid personally) − tax saved
```

The reimbursement and the tax saved are each rounded to the cent. The odometer is TeslaMate's, or the latest
odometer reading entered by hand or on an expense, as everywhere else in GarageBooks. When either reading isn't
known, the reimbursement is shown as *not known* and the year's after-tax total says *unavailable*: a missing
distance is never treated as nothing. An FBT year runs across two income years (April to June is the end of
one), so the tax saved takes each day's rate. The ATO's rate is data, by FBT year, shown in **Settings**
(4.20 cents per km from 1 April 2022 and 5.47 cents from 1 April 2026 to start with). The `ato_rates` job reads it
from the ATO's guideline (below) each week. A year with no rate of its own uses the latest earlier year's, and
the report says so.

The same definition of an expense's cost after tax is in the database as `garagebooks.expense_after_tax(tz)`, so
the dashboard and the app agree, and a test holds the SQL and the Python together. The rate of a date is
`garagebooks.marginal_rate('employee', date)`, from `garagebooks.tax_position(person, income year)`, and the app
reads it from there too.

## Tax year, bracket and Medicare status

There is no tax year to pick: the **lease start date** on the **Inputs** sheet decides it. Each of
the three years of the lease uses the income tax and HELP rates for the income tax year (1 July to
30 June) its start falls in, so a lease starting on 1 October 2024 uses 2024-25, 2025-26 and 2026-27
in turn. A year the ATO hasn't published yet uses the newest year it has, and the **Rates** sheet says
so beside it. (In GarageBooks proper the same is true of every expense: its date decides its year, and the
Settings page has one row per income year, with a bracket and a Medicare levy status to choose for each.
GarageBooks offers *Not exempt*, *Half exempt* and *Exempt*; the workbook's ATO rule below is not in the app.)

These choices on the **Inputs** sheet, one set for each person, set up the rest:

- **Tax bracket.** Pick the bracket that person's taxable income falls in, as the ATO lists them for
  the lease's first year, such as `$45,001 - $135,000 (30%)`. No salary is needed: the workbook
  uses the middle of the bracket (or $10,000 into the top one). To use an exact figure instead,
  type it into **Gross salary**, which overrides the bracket. A bracket gives a good answer unless
  the package would move someone across a bracket's edge, and then a typed salary is better.
- **Medicare levy**, one for each person: *Not exempt*, *Half exempt*, *Exempt*, or *Exempt (full or
  half by ATO rule)*. An exempt person pays no levy, and a half exemption pays half. The last choice
  works out whether the exemption is full or half from the next two choices, by the ATO's rule for a
  category 1 exemption (see its
  [medical exemption](https://www.ato.gov.au/individuals-and-families/medicare-and-private-health-insurance/medicare-levy/medicare-levy-exemption/medical-exemption-from-medicare-levy)
  page, and its definition of a
  [dependant](https://www.ato.gov.au/individuals-and-families/medicare-and-private-health-insurance/medicare-levy/medicare-levy-exemption/dependants-for-medicare-levy-exemption)).
  Both people start as *Not exempt*.
- **Dependants**, one for each person. The rule is the ATO's table:
    - *No dependants*: a full exemption.
    - *Partner only*: **full if the partner is in an exemption category or has to pay the levy, half
      if not.** The partner has to pay the levy when their taxable income is over the low-income
      threshold. Any choice but *Not exempt* puts the partner in an exemption category.
    - *Partner and shared children* (children who are also the partner's dependants): full only if
      the partner has to pay the levy, or by a family agreement (next choice); otherwise half.
    - *Partner and other dependants* (any dependant who is not exempt and pays no levy, and isn't a
      child shared with the partner): half.
- **Family agreement**, one for each person: *None*, *Claim full* or *Claim half*. A written family
  agreement settles which of two exempt partners with shared children takes the full exemption and
  which the half. Choose *Claim full* for one of you and *Claim half* for the other; the workbook warns
  if you both claim full.
- **Dependent children**, one number for the household: raises the family income thresholds below.

Scenario works the exemption out separately without and with the lease, from the partner's taxable
income each time. That is why the levy's low-income phase-in matters: a lease that lifts the
partner's taxable income over the lower threshold (their lease payments received, less depreciation)
can turn a half exemption into a full one, and the saving is worth having in the comparison. It can
turn back in a later year, as the car's value and so the payments and depreciation fall.

**Family income.** For someone with a spouse whose own taxable income is over the single upper
threshold, the ATO uses family income (both taxable incomes added together) as well:

- At or under the **family threshold** (raised for each dependent child) there is no levy, and the
  workbook applies that.
- Between the family thresholds the ATO reduces the levy, but its pages don't give the amount, so the
  workbook leaves the levy unreduced and says so in the **Family Medicare check** rows under the
  results ("Family reduction may apply"). Check those years with the ATO's
  [Medicare levy calculator](https://www.ato.gov.au/calculators-and-tools/medicare-levy-calculator).
  The levy shown is then a little high, never low.

Not modelled: the seniors and pensioners tax offset thresholds, and single or separated parents with
shared-care children (whose exemption is by days).

The brackets are those of the lease's first year, and the income they give is used in every year of
the lease. If you change the start date to a year where a bracket's rate differs (the lowest rate
fell from 16% to 15% in 2026-27), its label changes, and the bracket you had picked shows "Pick a
bracket from the list for the first year" until you choose again. Meanwhile the workbook uses the
middle bracket.

## Live rates from the ATO

The ATO publishes its rates as web pages, not as data, so GarageBooks reads the tables on them.
`garagebooks/ato.py` does the reading and checks what it found: brackets in order, rates
that are percentages, and tax figures that add up from one bracket to the next. If a page has
changed shape it stops and says why, rather than carry a wrong rate into a comparison.

The app keeps the income tax brackets (2020–21 on) and the home charging rate in the database, and its
`ato_rates` [scheduled job](scheduled-jobs.md) reads them again each week from the first page below and the
ATO's guideline on the home charging rate,
[PCG 2024/2](https://www.ato.gov.au/law/view/document?docid=COG/PCG20242/NAT/ATO/00001) (its Table 2). If
either page can't be read or doesn't look right, nothing is changed and the job says why on the Settings page:
the last good set stays in use. The workbook reads five pages, through `scripts/ato_rates.py`:

- **Income tax rates**, every year since 2020-21: [Tax rates: Australian residents](https://www.ato.gov.au/tax-rates-and-codes/tax-rates-australian-residents)
- **HELP repayment rates**, the same years: [Study and training loan repayment thresholds and rates](https://www.ato.gov.au/tax-rates-and-codes/study-and-training-support-loans-rates-and-repayment-thresholds)
- **Medicare levy family thresholds**: [Medicare levy reduction: family income](https://www.ato.gov.au/individuals-and-families/medicare-and-private-health-insurance/medicare-levy/medicare-levy-reduction/medicare-levy-reduction-family-income)
- **Medicare levy low-income thresholds**: [Medicare levy reduction for low-income earners](https://www.ato.gov.au/individuals-and-families/medicare-and-private-health-insurance/medicare-levy/medicare-levy-reduction/medicare-levy-reduction-for-low-income-earners)
- **Low income tax offset**: [Low income tax offset](https://www.ato.gov.au/individuals-and-families/income-deductions-offsets-and-records/tax-offsets/low-income-tax-offset)

`scripts/build_lease_workbook.py` reads them and builds the workbook. Run it again whenever the ATO
publishes new rates:

```bash
.venv/bin/python scripts/build_lease_workbook.py
```

That rewrites `docs/assets/lease-calculator-example.xlsx`. Add `--out my-lease.xlsx` for a copy of
your own. `scripts/ato_rates.py --out rates.json` saves the rates, and `--rates rates.json` builds
from a saved copy.

The workbook is a snapshot: Excel does not fetch anything when you open it, and the **Rates** sheet
says the date it was built. Some figures are not on the ATO's pages in a form that can be read, so
they are typed in and marked blue for you to check: the car limit, the small business income tax
offset (rate and cap), the Medicare levy rate (2%) and its phase-in rate (10 cents for each $1, which fits the ATO's published thresholds), and
your own lease rates. The Medicare thresholds are the single year the ATO's page shows, used for
every year. They only matter at low incomes.

## The what-if model (L7)

The household spreadsheet compares owning the cars outright against leasing them. Here Person A
packages the cars through their employer, and Person B leases the cars to Person A through
Person B's own business. One column per car, and one per person, for each of three years.

### Per car, each year

```text
value at start     = year 1: entered; later years: last year's value − last year's depreciation
fuel or energy     = km per year ÷ 100 × L or kWh per 100 km × price per L or kWh
running costs      = fuel or energy + maintenance + registration and CTP + insurance   (inc GST)
lease payments     = value at start × lease rate for the term
FBT taxable value  = base value × 20% × days held ÷ days in FBT year × (0 if exempt EV)
depreciation       = min(value at start, car limit) × 25%   (diminishing value: 200% ÷ 8-year effective life)
```

The **lease rate** for each term (1 to 5 years) comes from your own quote: yearly payments divided
by the amount financed. The example uses made-up rates.

The **residual** at the end of the term is at least the ATO's minimum, 75% − (75% ÷ 8) × years:

| Term | 1 year | 2 years | 3 years | 4 years | 5 years |
|---|---|---|---|---|---|
| Minimum residual | 65.625% | 56.25% | 46.875% | 37.5% | 28.125% |

### Without the lease, per person

```text
taxable income  = gross salary − other pre-tax deductions
net income      = taxable income − income tax − Medicare levy + low income tax offset − HELP repayment
cash in hand    = net income − running costs (Person A pays them from after-tax pay)
```

### With the lease

Person A, the employee:

```text
pre-tax package  = lease payments + running costs ÷ 1.1 − post-tax contribution
                   + admin fee × pays per year
taxable income   = salary − other package fees − pre-tax package − GST on the post-tax contribution
post-tax contribution = FBT taxable value                       (employee contribution method)
cash in hand     = taxable income − tax − Medicare + LITO − HELP
                   − post-tax contribution + running costs ÷ 11  (GST reimbursed)
```

Person B, leasing the cars through their own business:

```text
business income  = lease payments received − depreciation
taxable income   = salary + lease payments received − depreciation
small business income tax offset = min(business income ÷ taxable income × income tax × 16%, $1,000)
cash in hand     = salary + lease payments − tax − Medicare + LITO − HELP + small business offset
                   (depreciation lowers the tax but isn't cash)
```

**Net benefit** = both people's cash in hand with the lease − both without it.

### In GarageBooks: an associate lease after tax

For a lease marked **Associate lease**, GarageBooks works the household's side of this model out for each
income year (1 July to 30 June) the lease is held in, from what is entered: the lease's payments and dates,
the car's **value for depreciation** (on the lease form; blank uses the car's cost), and in Settings a tax
bracket and Medicare status for you and for the **Associate** (the associate's columns appear once a car has
an associate lease). A bracket stands for an income without the lease (its middle, or $10,000 into the top
one), as in the workbook. No salary is asked for.

```text
payments            = (pre-tax + post-tax payment) × pay dates in the year       (received by the associate)
depreciation        = value at the start of the year × 25% × days held ÷ days in the year
                      (first year: the lower of the value for depreciation and the car limit of the
                       income year the lease starts in; later years: that less the depreciation before)
business income     = payments − depreciation
associate's tax     = tax on (their income + business income) − tax on their income   (the year's brackets)
Medicare            = their levy (2%, 1%, 0) × business income
small business offset = min(16% × tax on (income + business income) × business income ÷ that income, $1,000)
                        (0 for a loss)
associate's extra tax = associate's tax + Medicare − small business offset
your tax saved      = (pre-tax payment + admin fee) × pay dates × your marginal rate
household cost      = admin fees − your tax saved + associate's extra tax       (negative: a saving)
```

For a lease that isn't an associate lease the household cost is the payments plus admin fees less your tax
saved. Each figure is rounded to the cent. Without the associate's bracket (or the car's value), their
figures and the household figure are *unavailable*, never 0. Not modelled here: the low income tax offset,
the Medicare levy's low-income phase-in and family thresholds, HELP, and GST on the payments. The ATO's car
limit is data in `garagebooks.car_limit` ($69,674 for 2024–25 and 2025–26; a later year uses the latest
earlier one: check the ATO's figure).

**Worked example** (invented figures). A car valued at $40,000 leased to the employer from 1 July 2026 to
30 June 2028 for $1,000 a month, all pre-tax. You: $135,001 – $190,000 (37%), not exempt, so 39%. The
associate: $45,001 – $135,000 (30%), not exempt, which stands for $90,000.50.

```text
2026–27  payments 12,000.00; depreciation 40,000 × 25% × 365 ÷ 365 = 10,000.00; business income 2,000.00
         associate's tax = tax on 92,000.50 − tax on 90,000.50 = 600.00; Medicare 2% × 2,000 = 40.00
         small business offset = 16% × 18,120.15 × 2,000 ÷ 92,000.50 = 63.03
         associate's extra tax = 600.00 + 40.00 − 63.03 = 576.97
         your tax saved = 12,000.00 × 39% = 4,680.00
         household = 576.97 − 4,680.00 = −4,103.03, a saving of $4,103.03
2027–28  depreciation 30,000 × 25% = 7,500.00; business income 4,500.00
         associate's extra tax = 1,350.00 + 90.00 − 143.77 = 1,296.23; household −3,383.77
```

The FBT year report shows these for the two income years the FBT year runs across. The one definition is `garagebooks.lease_income_year()` in the database.

### Tax tables

- **Income tax:** The year's five brackets from the ATO: a base amount plus a rate on income over the bracket's start.
- **Medicare levy:** 2% of taxable income, except for low incomes (nil at or under the family threshold too, and phased in otherwise): nil up to the lower threshold, then 10 cents for each $1 over it, until that reaches the full 2% at the upper threshold. Multiplied by the share payable (1, or 0.5 or 0 for a half or full exemption). The 10 cents is why the upper threshold is the lower one × 1.25; it only matters to an income between the two thresholds, about $28,000 to $35,000.
- **Low income tax offset:** $700, reduced by 5% of income over $37,500 and then 1.5% over $45,000; it can't exceed the tax.
- **HELP repayment:** From 2025-26 a marginal rate over each threshold (15c, then 17c, then 10% of all income). Before that, a rate for the band the income falls in times all income. Capped at the debt.
- **Small business income tax offset:** 16% of the tax on the business share of income, at most $1,000 a year.

## Points to check in the original spreadsheet

These were found while turning the spreadsheet into the example. The example workbook corrects
items 1 to 5; items 6 and 7 are kept as they were and flagged here.

1. **HELP repayment.** Without the lease, the rate was looked up from the *debt* instead of the
   income. With the lease, the rate was multiplied by the *debt* instead of the income. Both should
   use repayment income, and the repayment can't exceed the debt.
2. **Medicare for the second person** was a typed-in number, not a formula, so it didn't change
   with income. The example works it out, including the low-income reduction. The first person's levy
   was set to nil or 1% depending on whether the partner paid it (a full or half exemption). The
   example keeps that rule as a choice, and works out whether the partner pays from their taxable
   income, with and without the lease, instead of from a typed figure.
3. **Days held** and **percentage of the FBT year** were worked out but not used, so the taxable
   value was always the full year's. The days were also divided by 366 in every year, and end date −
   start date leaves out one day. The example uses days held ÷ days in that FBT year.
4. **Hard-coded figures:** the yearly admin fee was typed as 26 (a $1 fee × 26 pays), and the first
   income tax bracket's cumulative tax referred to an empty cell. The example takes both from inputs,
   and takes the tax tables from the ATO.
5. **Depreciation limit.** Depreciation of a car is limited to the ATO's car limit for the year it
   was bought. A car within the limit is unaffected, but the example now applies the limit, from a
   blue input per car.
6. **Year labels and rates.** The original labelled its years July to June, while the FBT year runs
   April to March, and its tax tables were one year's. The example numbers its years 1 to 3, and
   each uses its own income tax year's rates, from the lease start date. Where a year isn't
   published yet, the newest is used, so a longer comparison is an estimate.
7. **Small business income tax offset.** The 16% rate and $1,000 cap are typed in, not read from the
   ATO: check the current figures.

The GST handling was also checked and is right: running costs enter the pre-tax package without GST
(÷ 1.1), and the GST (÷ 11) comes back to cash in hand once, as the provider reimburses it.

The cars are bought outside the lease (an associate lease), so Person B's cost of buying them is not
part of the comparison. Only the lease payments received and the depreciation claimed are, and that
is how the model treats it.
