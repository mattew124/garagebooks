# Roadmap

Where GarageBooks is heading, in priority order. Nothing here is a promise or has a date: it is
the order the work would be done in, and why.

GarageBooks today is a Flask app with server-rendered pages and a little plain JavaScript (no
build step), a `garagebooks` schema inside the TeslaMate database, a Grafana dashboard provisioned
from a file, and invoices in OneDrive or a folder on your own server. It has no login of its own and
sits behind a forward-auth gate. Every item below is written to fit that stack, not replace it.

## How items are ranked

1. **Accuracy of the numbers first.** A total cost of ownership that is wrong, or that can't be
   recovered after a mistake, undermines everything else. That is why the basics of a novated lease
   are in **Now**: for a car paid for from pre-tax salary, the invoices alone overstate what it costs.
2. **Value for the work.** Small changes that close a visible gap come before large ones.
3. **Dependencies.** Some items are foundations for others (a scheduler before notifications, an
   API before a phone app); they are ranked so the foundation comes first.

Sizes are rough: **S** is a day or two, **M** a week or so, **L** several weeks. **T1** is the
TeslaMate independence work, first in Now. Items numbered **L1** to **L8** are the novated lease
work, described in its [own section](#novated-leases-and-salary-packaging).

## Design rule: TeslaMate is optional

Today TeslaMate is where GarageBooks gets its list of cars, odometer readings and charging costs, and
its tables are in the same database. From **T1** on, TeslaMate is one optional source of data among
others, and everything below is built to that rule. Each item says what it does with TeslaMate:

- **Absent** (never installed, or a car TeslaMate doesn't track): it works, from what you enter by
  hand, with nothing to configure.
- **Unreachable** (down, or a database it can't read): the figures that need it say "unavailable".
  Nothing shows a wrong zero, and nothing else stops.
- **On a version the app hasn't been tested with:** the TeslaMate figures are held back with the
  reason, and the rest carries on.

Reading stays read-only, and only the few tables the app reads now. New work that wants something
from TeslaMate asks for it through the source interface that T1 builds, never by querying its tables
directly.

## Now

Everything that was here has shipped (see Already in place). The next items are under Next.

## Next

- **9. Status page** (size S–M)
  One page showing the app version, the TeslaMate version and compatibility check (when there is a TeslaMate), the OneDrive connection and secret expiry, whether the invoice folder is mounted, database size, the app's memory use, and the time of the last backup if you point it at one. It also shows the kilometre pace (L4) for each car with a lease estimate.
  *Depends on:* —

- **10. Charging and energy figures** (size M)
  Cost per kWh, energy added, sessions per month and kWh per 100 km, from `charging_processes` and `positions` (the tables the app already reads), in the app and on the dashboard. With no TeslaMate the page says "not configured", and charging entries made by hand fill it instead.
  *Depends on:* T1

- **11. Value over time** (size S–M)
  Dated valuations of each car (a trade-in quote, say), and a depreciation line on the dashboard.
  *Depends on:* 2

- **12. Import from other apps** (size M)
  Import costs from aCar and fuel-ups from Fuelly CSV exports, with the same preview as the invoice import, duplicate detection (already used by the Add form) and a choice of car before anything is saved.
  *Depends on:* —

- **13. Watch the invoice folder** (size L)
  Check each car's OneDrive or server folder on a schedule. Each new PDF becomes a draft expense (read the way the import page already reads them), with its category taken from the subfolder it is in. Drafts wait in a review queue until you confirm or edit them, and a notice (item 8) says when new ones are waiting.
  *Depends on:* 7, 8

- **14. Your own lists and fields** (size M)
  Editable lists of vendors and payment methods, as categories are now. Extra fields per category (tyre tread depth, say) defined in Settings, stored as JSONB, shown on the form and queryable in Grafana, with no code change.
  *Depends on:* —

- **15. Small-server footprint** (size S)
  Measure and publish the app's memory use; a setting for the number of web workers so it fits beside TeslaMate on a 1 GB machine.
  *Depends on:* —

- **16. PostgreSQL upgrade testing** (size S)
  Extend the daily TeslaMate compatibility check to run the test suite on each new PostgreSQL major version TeslaMate supports, so a database upgrade is known safe before you do it.
  *Depends on:* —

## Later

- **17. A JSON API** (size M)
  A versioned, read and write API (`/api/v1/`) for expenses, cars and summaries, with every summary figure for a car in one response. It stays behind forward-auth: machine clients use whatever token your gate issues (an Authentik app token, a Cloudflare Access service token). A `/v2` can arrive later without breaking `/v1`.
  *Depends on:* —

- **L6. Read the provider's statements** (size M)
  Fill in L1 and L3 from the provider's PDFs instead of by hand.
  *Depends on:* L1, L3

- **L7. Lease what-if calculator** (size L)
  Compare lease terms, cars and ways of paying before signing.
  *Depends on:* L1, L2

- **L8. Lease end** (size S)
  Residual payout, refinancing, or handing the car back.
  *Depends on:* L1, 2, 4

- **18. Capture on the phone** (size M)
  The app already installs to a home screen. Add photographing a receipt from the Add form, reading it on the device (no outside service ever sees the image) and showing the fields for review before saving.
  *Depends on:* 17 for offline use

- **19. Android app** (size L)
  A native app: set the server address and token (kept in the device's encrypted storage), pick a car, quick entry, on-device receipt reading, history and charts, reminders. Distributed as a signed APK on each GitHub release. iOS after that.
  *Depends on:* 17

- **20. Cars outside TeslaMate** (size L)
  Petrol, diesel, plug-in hybrid and hybrid cars, on the car table T1 builds. Fuel entries (litres, price per litre, fuel type, full tank or not), the odometer on expenses, and L/100 km on the dashboard. TeslaMate cars are still added automatically, never overwriting what you have entered. A package that holds an EV and a petrol car needs this.
  *Depends on:* T1

- **21. Standalone install** (size S–M)
  A compose file and setup page for running GarageBooks with its own PostgreSQL and no TeslaMate. The behaviour comes from T1; this is the packaging, and a release check that the image starts and works that way.
  *Depends on:* T1

- **22. Invoices from email** (size L)
  Read attachments from a chosen mail folder and file them into the car's invoice folder, where item 13 picks them up. The mail account is signed in separately from the storage account.
  *Depends on:* 13

- **23. OneDrive sign-in by code** (size M)
  A second way to connect OneDrive: enter a short code at microsoft.com instead of being sent back to the app. It avoids the return trip through the forward-auth gate, which some setups find fragile. Still your own app registration.
  *Depends on:* —

- **24. More than one person** (size L)
  Read the signed-in user from the forward-auth gate's identity header (accepted only from the proxy), give each car an owner, and show each person their own cars. A household on one install.
  *Depends on:* 20

- **25. TeslaMate event notices** (size S–M)
  Optional notices for TeslaMate events (charging finished, say), read from its MQTT broker and sent through item 8. Probably better as its own small tool, since it isn't about costs.
  *Depends on:* 8

- **26. Fuelly sync** (size M)
  Ongoing two-way sync with Fuelly, after the one-off import (12).
  *Depends on:* 12, 20

- **L3. Lease budgets against what was spent** (size M)
  Each budget pool's money in and money out, and the projected balance at the end of the FBT year.
  *Why later:* Lease budgets are now optional and off by default, and are only worth tracking for an owner whose provider budgets each running cost.
  *Depends on:* L1, L2

## Novated leases and salary packaging

In Australia a car can be leased through an employer's salary packaging provider (a novated lease).
The lease payment and a budget for each running cost come out of pay every pay period, mostly before
tax. The provider pays the lease and fuel card directly and reimburses other running costs when you
claim them. Fringe Benefits Tax (FBT) is counted over its own year, 1 April to 31 March, and an
eligible electric car is exempt from it. This section is written from a real provider's statement
and a household's own lease calculations. Only the rules are taken from them, none of the figures.

The formulas, and an example workbook with invented figures, are on
[Novated lease calculations](lease-calculations.md). Nothing here gives tax advice. Rates, thresholds and residual percentages are published by the ATO
and change; the app holds them as data you can update, never as numbers in the code.

### L1. Lease details per car (Done, M)

One lease record per car, entered from the provider's statement:

- Financier, lease start and end dates, refinance date if refinanced, and term.
- Payment amount and frequency (weekly, fortnightly, monthly), split into the pre-tax and post-tax
  parts, and the provider's admin fee per pay.
- Residual (balloon) value, as an amount and a percentage of the car's cost.
- FBT: base value, valuation method (statutory formula or operating cost), statutory percentage
  (20%), whether the car is exempt as an eligible electric car, and the tax treatment (for
  example the employee contribution method).
- The provider's estimated km per year, and the yearly budget for each running cost: lease, fuel
  or charging, insurance, registration and CTP, maintenance, tyres and other running costs.
- Registration expiry, and whether the provider renews it, so item 4 can remind you.

Added by the review (#85): the provider's lease form or package confirmation fills in a lease's details,
the lease form and lease agreement PDFs are kept for each lease period, a car can have several lease
periods, an associate lease option has no financier and no residual, and the lease end is worked out from
the start and the term. Lease budgets are optional and off by default.

More than one car can sit in one package, each with its own lease. Each GarageBooks category maps to
one of the provider's budget pools.

### L2. FBT year and taxable value (Done, S)

- No year to pick: each expense's date decides its FBT year (and its income tax year), so every figure
  falls in the right year by itself. Dashboards group and filter by it, built from the dates, and
  Grafana's own time range still works.
- Days the car was held in each FBT year (for the year it starts or ends part-way), and the taxable
  value under the statutory formula: base value × 20% × days held ÷ days in the FBT year.
- The post-tax contribution that brings FBT to nil under the employee contribution method, and nil
  for an exempt electric car. The contributions actually made (L1's post-tax part) against it.
- A per-FBT-year summary report: running costs by pool, contributions, taxable value.

### L3. Lease budgets against what was spent (Later, M)

- Each pool's deductions to date (payment × pays so far) against what was spent: expenses in
  GarageBooks, and charging cost for the fuel or charging pool (from TeslaMate, or entered by hand).
- Each expense marked as **paid by the provider** (lease, fuel card), **claimed** (awaiting or
  reimbursed) or **paid personally**, so nothing is counted twice and unclaimed costs stand out.
- The projected balance of each pool at the end of the FBT year, and a warning through notifications
  (item 8 is in place; this notice is added here) when one is heading for a shortfall or a large unused surplus.
- Budget changes from a refinance or a re-budget take effect from their date, without rewriting
  earlier periods.

*Progress:* the **claimed** part is in place. With a [claims connector](claims-connector.md) configured,
an expense in a claimable pool is claimed from the provider, the claim (reference, status, approved
amount, paid date) is kept against it, and an expense with a claim recorded isn't offered again. Still
to do: paid by the provider and paid personally, the pool balances and the projection.

### L4. Kilometre pace (Done, S)

From the lease's estimated km and the odometer (TeslaMate's, or the odometer on the latest expense): the reading at the start of the FBT year,
the km expected by its end (pro rata if the car is held only part of the year), the km per day
needed to get there, and the km per day actually driven. In the app (the FBT year report and the lease page)
and on the dashboard. The status page (item 9) will show it too once there is one.

### L5. Cost after tax (Done, M)

A packaged expense costs less than its invoice: the GST is claimed back, and the rest comes from
pre-tax salary. With your marginal tax rate entered once in Settings (the rate, not your salary),
each packaged expense and the totals show both the invoice amount and the effective cost. Expenses paid
personally are unaffected. Nothing here is tax advice.

Built (#91): a **Paid from** choice on an expense (automatic, the package or personally), a **GST-free** tick
per category, the marginal rate and the ATO's home charging rates by FBT year in Settings, an **After tax**
part on the FBT year report, two dashboard panels, and, for a battery electric car whose lease has it ticked,
the home charging reimbursement: the ATO's cents-per-km rate (kept as data by FBT year: 4.20 cents per km for the
FBT years to 31 March 2026 and 5.47 cents from 1 April 2026) times the km driven, and the tax saved on it. The
formulas and their simplifications are on [Novated lease calculations](lease-calculations.md#cost-after-tax-l5).
The reimbursement needs the odometer, so it is in the app only. The existing TCO figures keep their meaning:
the after-tax figures sit beside them.

### L6. Read the provider's statements (Later, M)

The first part is done: the lease form fills in a lease's details (#85). What is left is reading the
statements of what the provider paid out, to mark claims as reimbursed and correct pool balances in L3.

The activity statements are read with the same PDF reader the invoice import uses, showing what it found for review.
Personal details on those statements (names,
employer, payroll and bank numbers) are never stored.

### L7. Lease what-if calculator (Later, L)

Compare choices before signing, for one or more cars at once, over 1 to 5 year terms:

- Lease payments from the purchase price, a lease rate, and the ATO's minimum residual for the
  term (65.625%, 56.25%, 46.875%, 37.5% and 28.125% for 1 to 5 years).
- Running costs from km per year × fuel or energy use × price, plus maintenance, registration and
  insurance; and the car's value falling over the term.
- FBT for the part of the year the car is held, nil for an exempt electric car.
- The effect on take-home pay against paying for the car from after-tax income: income tax
  brackets, Medicare levy, the low income tax offset and HELP repayments. It can also model a car
  leased through a household member's own small business (depreciation as an expense, capped at
  the ATO's car limit, and the small business income tax offset). The business buys the car outside
  the lease, as an associate lease does, so only the lease payments and the depreciation count.
- **Rates read live from the ATO.** The app reads the income tax, HELP, Medicare levy and low income
  tax offset tables from the ATO's own pages, checks them (brackets in order, rates that are
  percentages, tax figures that add up) and keeps the last good copy with the date it was read, so
  a changed page shows an error and the last rates, never a wrong number. A scheduled job (item 7)
  refreshes them. The tax year is never chosen: dates decide it. The
  [example workbook](lease-calculations.md#live-rates-from-the-ato) already does this at build time.
- **Choices, not salaries.** Each person picks a tax bracket from the chosen year's list, and
  whether they are exempt from the Medicare levy (not exempt, half, or exempt), instead of typing
  a salary. An exact figure can still be entered to override the bracket.
- **Medicare, both ways.** The low-income phase-in (the levy is nil up to a threshold and rises to the
  full 2% over a range), and the exemption rule from the ATO's table: full if there are no dependants,
  or the partner is in an exemption category or pays the levy; full for shared children only if the
  partner pays (or by a family agreement); half if another dependant pays no levy. The compare works
  the partner's levy out separately with and without the lease, so it shows when a lease lifts the
  partner over the threshold and turns a half exemption into a full one, and when that reverses in a
  later year. It also applies the nil levy at or under the family income threshold (raised for each
  dependent child) and flags a family between the thresholds, where the ATO reduces the levy by an
  amount its pages don't give.
- The net benefit of each option. Salaries entered for a comparison are used for that calculation
  only and never saved.

### L8. Lease end (Later, S)

Already in place: a **Lease ends** reminder appears by itself, with a lead time of 90 days, from the
lease's end date (item 4). The rest is not built.

Before the lease ends: a reminder through item 4, the residual to pay, and the choice to pay it out,
refinance (a new lease from that date, L1) or hand the car back. Paying out or selling links to
item 2, so the TCO covers the whole time the car was owned.

## Already in place

These are in main since 2026-11-16 and not yet in a release:

- **T1. TeslaMate independence.** GarageBooks runs with TeslaMate absent, unreachable or on an untested version, with its own car table (#79).
- **1. Backup and restore guide.** A docs page and a script to dump and restore the `garagebooks` schema (#75).
- **2. Selling the car.** A sale date and price per car; TCO and cost per day stop at the sale (#80).
- **L1. Lease details per car.** The novated lease behind a car (#81).
- **L2. FBT year and taxable value.** Costs by FBT year, and the car's taxable value and contribution (#82).
- **L4. Kilometre pace.** The km the lease estimate expects this FBT year against the odometer, in the app and on the dashboard (#88).
- **3. Change history.** An append-only log of every add, edit and delete, with a page to view it (#76).
- **4. Reminders by date or odometer.** Recurring reminders, resolved by linking the expense that did the job (#83).
- **5. Payment calculator.** Work out a payment made of several parts on the amount field (#77).
- **6. Stricter quality gates.** A higher coverage floor, property-based tests and an SBOM on each release (#78).
- **7. Scheduled jobs.** A small scheduler for periodic work, run by the app or by an optional worker container from the same image (`python3 -m garagebooks worker`), with each job's last run shown on Settings (#90).
- **8. Notifications.** Push notices through an address you set (ntfy, Apprise or a Home Assistant webhook): a reminder coming due or overdue, the OneDrive client secret nearing its expiry date, and the OneDrive sign-in lost (#89). Two notices wait for the features they report on: new invoices waiting for review (with item 13) and a lease budget heading for a shortfall (with L3).
- **L5. Cost after tax.** What a packaged expense costs after the GST and pre-tax salary, beside its invoice amount, and the home charging reimbursement (#91).
- **Review changes.** Lease form import and documents per lease period, several lease periods per car, associate leases, optional lease budgets, packaging per car, the purchase price on the Cars page, a list of standard service reminders, and the Sold panel only once a car is sold (#85). The manual odometer readings were removed.

These were part of the original plan and are done: the dashboard as a version-controlled file that
Grafana provisions, in the same Grafana as TeslaMate, with a car selector; TCO, purchase vs running
vs charging, cost per km, per day and per month, spend by category and cumulative cost; an editable
category list; an invoice link on each expense; reading invoices with learning from your
corrections, and a preview before anything is saved; OneDrive tokens encrypted at rest; images for
amd64 and arm64 by tag; CodeQL, Bandit and Trivy in CI; no JavaScript build step; no login of its
own; the MIT licence; and never changing TeslaMate's own tables.

## What changed from the original plan

The first plan for GarageBooks was written for a different stack. Every requirement it had is
somewhere above; these are the ones adapted rather than taken as written.

- **FastAPI backend, HTMX pages:** Flask and server-rendered pages, as built. HTMX can still be added as a single file, with no build step.

- **A separate `garageBooks` database:** The `garagebooks` schema in TeslaMate's database, so Grafana needs no second datasource. With no TeslaMate (T1, 21) it runs on a PostgreSQL of its own.

- **Cloudflare Access as the only gate:** Any forward-auth gate (Authentik, Authelia, Cloudflare Access). Publishing shows the Cloudflare route.

- **OneDrive by device code only:** The usual sign-in with a return address now; sign-in by code added as an option (23).

- **A Flutter app first:** A phone-friendly web capture first (18), the native app after the API it needs (17, 19).

- **Redis and a background worker profile:** An optional worker container from the same image (7); no Redis unless a feature needs it.

- **Reading TeslaMate's `drives` table:** Energy figures from `charging_processes` and `positions`, which the app already reads; widening that list would be a separate decision.

- **Lease reporting left open, four lines:** Written up from a real statement and calculations (L1 to L8), with the first two moved to **Now** (both done by 2026-11-16).

- **No dependency on TeslaMate:** The original plan's first system requirement, and the one most easily lost as features pile up. It is now T1, first in Now, with a design rule that every other item follows.

- **Re-ranked on 2026-11-16:** The Now items shipped (see Already in place). Cost after tax (L5, with the charging reimbursement) was first in Now because you asked for it, and is now built; kilometre pace (L4), scheduled jobs (7) and notifications (8) follow. Lease budgets (L3) moved to Later because budgets are now optional.

- **Hosting, secrets and deployment pipeline:** The installer's choice, not part of this project.

