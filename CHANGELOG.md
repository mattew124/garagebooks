# Changelog

All notable changes to garagebooks. Version numbers and the candidate-to-stable
steps are in [RELEASING.md](RELEASING.md). Newest first.

Each entry says what an installer must do to upgrade. Where nothing is said, pull the new image.

## Unreleased

- **Faster tests.** The suite runs in parallel (`pytest -n auto`, one throwaway Postgres per worker), about three times
  faster on four cores, and the route property tests use 15 examples locally (40 in CI, as before). The `admin`
  fixture makes one round trip instead of a dozen. CI and the TeslaMate compatibility check use `-n auto`. Nothing
  changes for installers.

## 0.4.0-rc3 - 2026-11-19

**TeslaMate 4.5.0.** Tested with TeslaMate 4.5.0 (it was 4.4.0). Nothing to do on upgrade: pull the new image. A TeslaMate database up to that release no longer has its figures held back.

- **A notice when TeslaMate is newer than tested.** An optional box under Notifications on the Settings page
  (off by default) sends one notice for each new TeslaMate migration the app holds its figures back on. The log
  line for the same event is a `WARNING` (`ERROR` with `ALLOW_UNTESTED_TESLAMATE=1`), so a log alert can use it too.
  The *TeslaMate compatibility* workflow now runs daily (nothing runs unless there is a newer TeslaMate release) and,
  when the tests pass, records the release in `compat.py`, the README and this file in a pull request, runs CI on it,
  merges it and tags the next release candidate, which the Release workflow builds for you to test. It never tags a
  stable release.

## 0.4.0-rc2 - 2026-11-19

This candidate adds notifications, scheduled jobs, cost after tax for a novated lease, kilometre pace, a change
history, and lease form import, and makes TeslaMate optional. It is worth testing if you want a notice sent
when a reminder falls due or the OneDrive sign-in stops working, or to see what a leased car costs after tax.

**To upgrade:** back up TeslaMate's database, as for any release that changes the schema, then pull the new
image. The app adds its new tables, columns and functions itself on start. If you use the restricted database
role (`SKIP_MIGRATE=1`), run `scripts/provision-db.sh` first. Nothing is sent and no tax is calculated until you
set a notification address or a marginal tax rate in Settings.

**Notifications.** The app adds the new table and columns itself. If you use the restricted database role
(`SKIP_MIGRATE=1`), run `scripts/provision-db.sh` again so the role can use the new `notification` table.
Nothing is sent until you set an address, so nothing changes if you don't.

- **Notifications.** A **Notifications** section on the Settings page takes an address (ntfy, an Apprise
  API or a Home Assistant webhook) and sends a notice, once each, when a reminder comes due soon and when
  it is overdue, 30 and 7 days before the OneDrive client secret expires and when it has, and when the
  OneDrive sign-in stops working. A **Send a test notice** button checks the address. The address is a
  secret: it is never shown back, logged or put in an error. A new scheduled job, `notifications`, runs
  every 15 minutes. New invoices waiting for review and a lease budget heading for a shortfall come with
  the features they report on. See [Notifications](docs/notifications.md).


**Scheduled jobs.** Nothing to do on upgrade: the app adds the new table itself and runs the jobs in the background.
With the restricted role from `scripts/provision-db.sh` and `SKIP_MIGRATE=1`, run the script again so the role can
write the new `garagebooks.job` table.

- **Scheduled jobs.** A small scheduler runs periodic work: it looks at TeslaMate again every hour and rewrites the
  Grafana dashboard every six hours. The **Settings** page has a **Scheduled jobs** section with each job's
  last run, how it went and when it is next due. `SCHEDULER` says where they run: `app` (the default), `worker`
  or `off`. For `worker`, start the optional second container with `docker compose --profile worker up -d`
  (it is the same image, started with `python3 -m garagebooks worker`). See the Scheduled jobs page of the docs.

**Cost after tax (L5).** Nothing to do on upgrade: the app adds the new columns, the rates table and the
`garagebooks.expense_after_tax(tz)` function itself. If you use the restricted database role (`SKIP_MIGRATE=1`),
run `scripts/provision-db.sh` again first. Nothing changes until you enter a marginal tax rate in Settings.

- **Cost after tax.** With your **marginal tax rate** entered in Settings (a percentage, Medicare levy included;
  never a salary), an expense paid from a lease package shows what it costs after tax beside its invoice amount:
  the GST is claimed back and the rest comes from pre-tax salary. An expense on the **Add expense** form has a
  **Paid from** choice (automatic, the package or personally), shown for a car with a lease; automatic means a
  lease covers its date, its category has a budget pool, and it isn't a purchase, so existing expenses need no
  changes. The Categories page has a **GST-free** tick (Registration starts ticked). The expense list, the FBT
  year report (a new **After tax** part) and the dashboard (**Running costs after tax** and **After tax by FBT
  year**, written only while a rate is entered and a car has a lease) show it. The formulas and their
  simplifications are on the Novated lease calculations page. Not tax advice.
- **Home charging reimbursement.** A battery electric car's lease has a tick for home charging reimbursed at the
  ATO's cents-per-km rate. The FBT year report then shows the km driven, the rate used, the reimbursement and the
  tax saved on it. The ATO's rates by FBT year are in Settings (4.20 cents per km to FBT 2025–26, 5.47 from
  1 April 2026), and can be changed or added to. With no odometer reading the report says *not known*.

**Lease and car page changes.** Nothing to do on upgrade: the app adds the new columns itself.

- **Dev stacks.** `scripts/dev.sh prune` destroys the dev stacks of merged branches and of removed worktrees, and `scripts/dev.sh auto-prune on` runs it hourly from cron.
- **Kilometre pace (roadmap L4).** For a lease with an estimated km per year: the FBT year report has a **Kilometre pace**
  section (the km expected by the end of the year, pro rata for the days held, the odometer at the start and needed at the
  end, the km per day needed, and what a statement can't show: km driven so far, km per day driven, how far ahead of or behind
  the pace the car is, and the km per day needed from today), and the lease page has a short version. The dashboard has a
  **Kilometre pace** table (panel 21), left out until a lease has an estimate. The odometer is TeslaMate's, or your latest
  reading, and says *not known* rather than 0 when there is none. New in the database: the functions
  `garagebooks.odometer_at()` and `garagebooks.build_odometer_at()`, which the app creates itself. With
  `scripts/provision-db.sh` (`SKIP_MIGRATE=1`), run it again so the app's role can switch the function.
- **Roadmap.** Updated for what has shipped: the Now items are in place, cost after tax (L5) is next, and lease budgets (L3) move to Later.
- **Lease form import.** The Add a lease page starts with a **Documents** card. Choose the provider's lease
  form or package confirmation (from the computer, by dropping it, or from the car's OneDrive or invoice
  folder) and the boxes are filled in from it: provider, start, end, term, pay frequency, the pre-tax
  payment per pay, FBT base value, electric car exemption, estimated km per year and, with budgets on, the
  yearly budgets. Only the lease's own figures are read, never names, employer, payroll, bank or
  registration details. Check the boxes before saving.
- **Lease documents.** A **Lease agreement PDF** can be kept for reference (nothing is read from it). Chosen
  PDFs are saved in a **Lease** subfolder of the car's folder and linked from the lease page. With no
  folder chosen for the car, the lease still saves and says the PDF wasn't kept.
- **Lease periods.** A car's lease page has **Add a lease period** at the top, and the periods other than
  the current one are folded to a line each. Every period keeps its own lease form and lease agreement.
- **Associate leases.** A new **Associate lease** tick box, for a car owned outright and leased to the
  employer by an associate. No financier is needed, and the residual boxes and the minimum-residual check
  go away.
- **Salary packaging provider.** *Package or provider name* is now **Salary packaging provider**, and
  packaging is recorded per car.
- **Lease end** is filled in from the start date and the term in months. Leave the term empty and it is
  worked out from the two dates.
- **Lease budgets are optional and off by default.** Turn on **Track lease budgets** in Settings to see
  them on the lease pages, the Categories page and the FBT year report. While off, the FBT year report
  lists running costs by category. Budgets already entered are kept.
- **Cars page.** The name is optional (a car with none is called by its make and model). Purchase price,
  date bought and odometer are edited on the Cars page. The **Odometer readings** section is gone: the
  odometer comes from TeslaMate or from the odometer on expenses. Recording a sale asks for the final
  odometer (required for a car TeslaMate doesn't track).
- **Standard reminders.** The Add a reminder form has a **Standard reminder** list (engine oil, tyre
  rotation, registration renewal and others) that fills in the title, how often it repeats and the first
  due date or odometer. Engine items are not offered for a battery electric car.
- **Dashboard.** The **Sold** panel is only on the dashboard once a car has been sold.

**Change history.** A new **History** page lists every add, edit and delete of an expense or category:
when, who, and what changed (old and new values). It is written by triggers in the database, so changes
made by other tools are recorded too, into an append-only `garagebooks.change_log` table. Who made a
change is only known when you set the new optional `AUTH_USER_HEADER` to the header your forward-auth
gate sets with the signed-in user (and the gate removes it from incoming requests); otherwise it shows
*unknown*. Nothing is recorded for changes made before this version. **To upgrade:** pull the new image.
If you use the restricted database role (`SKIP_MIGRATE=1`), re-run `scripts/provision-db.sh` first: it
creates the log and grants the role `SELECT` and `INSERT` on it.
A payment calculator on the Add and Edit form's amount field works out the true cost of a payment made of several
parts, such as a discounted gift card plus cash. Only the amount you choose to use is saved. Nothing to do on upgrade.

The rename to GarageBooks reaches the last places the old name was kept: the database schema, the
dashboard's address and the settings. The app upgrades your database itself on its first start, and
your data, saved links and OneDrive sign-in carry over.

**To upgrade:** back up TeslaMate's database, as for any release that changes the schema, then pull
the new image. On first start the app renames its `expenses` schema to `garagebooks`, renames the
setting for its own address, and moves a saved dashboard link to the new address. If you move to
the `/garagebooks` path in the same upgrade (by setting `BASE_PATH`, or by using the default), the saved
address of the app moves with it, keeping your host: nothing to change on the Settings page. Grafana
drops the old dashboard and its folder by itself. If you use the
restricted database role (`SKIP_MIGRATE=1`), re-run `scripts/provision-db.sh` first: it does the same,
and keeps your existing role when you set `GARAGEBOOKS_DB_USER` (or the old `EXPENSES_DB_USER`) to it.

- **TeslaMate is optional.** GarageBooks now keeps its own list of cars (the new **Cars** page) and
  runs with TeslaMate absent, unreadable or on an untested version. The cars TeslaMate tracks are added
  automatically with the same ids, so no expense moves; any other car is added by hand, with odometer
  readings entered on the Cars page and charging or fuel costs entered as expenses in the new
  **Charging** and **Fuel** categories (counted with TeslaMate's charging costs on the dashboard).
  The app no longer stops on a TeslaMate newer than it was tested with: it holds TeslaMate's figures
  back, shows them as *unavailable* with the reason, and carries on. `ALLOW_UNTESTED_TESLAMATE=1`
  still uses them anyway. The dashboard is written to match, and follows within a minute when
  TeslaMate appears or changes. The app's tables no longer hold a foreign key onto TeslaMate's `cars`,
  so TeslaMate can delete a car. If you use the restricted database role, re-running
  `scripts/provision-db.sh` (already needed for this release) also lets it read
  `charging_processes(car_id, start_date, cost)`. A grant you added yourself on the
  `garagebooks.ownership` view needs granting again: the view is rebuilt once. The dashboard's car
  list is now in id order.
- **Lease details per car.** Each car on the **Cars** page has a **Lease** link: the novated lease
  behind it, entered from the provider's statement (financier, dates, term, payments split pre-tax and
  post-tax, residual, FBT details, the provider's estimated km, registration, and a yearly budget for
  each running cost), with add, edit and delete, and past leases kept. The page shows figures worked
  out from what you entered (total per pay, yearly payments, budgets against payments, the residual
  against the ATO's minimum, days remaining) so a typing mistake stands out. Each category can be
  given the budget pool it is paid from, on the **Categories** page. It records the statement and is
  not tax advice; no personal details are stored. See [Leases](docs/leases.md). The app adds the new
  `garagebooks.lease` and `garagebooks.lease_budget` tables and `category.budget_pool` on start; with
  the restricted database role, re-run `scripts/provision-db.sh` first.
- **Selling a car.** Record a sale date and price on the **Cars** page (**Sold this car?**). A sold car's
  total cost of ownership is net of the sale price, its time owned, per-day and per-month figures stop at
  the sale date, and its distance stops at the last odometer reading on or before it. The dashboard adds
  a **Sold** tile and names the car *(sold)* in the Car box; the Add form lists sold cars last. The
  `garagebooks.car` table gains `sold_at` and `sale_price`, and the `garagebooks.ownership` view gains
  `sale_price`, `sold_at` and `owned_until` (its `tco` and `current_km` change for a sold car). Both apply
  themselves on start; with the restricted database role, nothing more is needed.
- **FBT year and taxable value.** A car's lease page links to a report for each FBT year (1 April to
  31 March), chosen by the dates of its expenses, never by you: days held, the taxable value under the
  statutory formula, the contribution that cancels it against what the post-tax payments make, and running
  costs by budget pool (with TeslaMate's charging, or *unavailable* when it can't be read). The dashboard has a
  new **Running costs per FBT year** panel, and the database a `garagebooks.fbt_year(date)` function. It is not
  tax advice. See [Leases](docs/leases.md). The app adds the function on start; with the restricted database
  role, re-run `scripts/provision-db.sh` first.
- **Reminders.** A new **Reminders** page (and a count in the header when something is overdue or due
  soon) keeps track of what a car needs next: a reminder falls due on a date, an odometer reading
  (TeslaMate's, or your latest reading) or whichever comes first, with a lead time in days and km.
  Repeating ones (a tyre rotation every 10,000 km) make the next when resolved, and are resolved by
  linking the expense that did the job, from the reminder or after saving the expense. Resolved
  reminders are kept, and can be reopened. A lease's registration date and end date appear as reminders
  by themselves. The dashboard has a **Reminders** table for the selected car. Due-ness is worked out
  when a page or the dashboard is viewed; nothing is sent. See [Reminders](docs/reminders.md). The app
  adds the new `garagebooks.reminder` table and `garagebooks.reminder_due` view on start; with the
  restricted database role, re-run `scripts/provision-db.sh` first.
- **Backup and restore:** a new guide ([Backup and restore](https://mattew124.github.io/garagebooks/backup/)) and
  `scripts/backup.sh`, which dumps and restores only the `garagebooks` schema, and checks whether an existing
  TeslaMate backup includes it. It never writes to the database except for `restore`, which refuses to replace a
  schema that has tables unless you add `--replace`. Nothing to do to upgrade.
- **Schema:** `expenses` is now `garagebooks`. Grafana panels or queries of your own that name
  `expenses.` need the new name.
- **Dashboard address:** the uid is now `garagebooks-tco` (was `tm-expenses-tco`), so its address is
  `/d/garagebooks-tco`. The link in the app's header moves by itself; bookmarks to the old address don't.
- **Settings:** `EXPENSES_ENCRYPTION_KEY` is now `GARAGEBOOKS_ENCRYPTION_KEY` (the old name still works),
  `EXPENSES_IMAGE` is `GARAGEBOOKS_IMAGE`, `EXPENSES_UID`/`EXPENSES_GID` are `GARAGEBOOKS_UID`/`GARAGEBOOKS_GID`,
  and `scripts/provision-db.sh` takes `GARAGEBOOKS_DB_USER`/`GARAGEBOOKS_DB_PASS` (old names still work).
  Its default role is now `garagebooks_app`.
- **Fixed:** `docker-compose.invoices.yml` still named the service `expenses` after 0.4.0-rc1 renamed
  it, so the invoice folder override started a second, broken container. It names `garagebooks` again.
- **Fixed:** an amount such as `1e999999999`, or a `NUL` character anywhere in a field or an address, made
  the app answer with a server error. Both are now refused with a message. Found by the new property-based
  tests.
- **Quality gates:** the coverage floor is now 90% (it was 85%), the parsers and every route that takes input
  are tested with generated input ([Hypothesis](https://hypothesis.readthedocs.io)), and each release
  carries a software bill of materials (SPDX JSON, one file per platform) on its GitHub release page. Nothing
  changes in the image.
- **Docs:** a [roadmap](https://mattew124.github.io/garagebooks/roadmap/) of what comes next, in priority
  order, and [novated lease calculations](https://mattew124.github.io/garagebooks/lease-calculations/) with an
  example workbook (invented figures) that reads its tax rates from the ATO's pages. Two development scripts,
  `scripts/ato_rates.py` and `scripts/build_lease_workbook.py`, build it; nothing in the app or image changes.

## 0.4.0-rc1 - 2026-11-15

**The project is now called GarageBooks** (it was teslamate-expenses). TeslaMate's trademark policy reserves
the TeslaMate name for tools that use only its MQTT interface, and this app reads the database, so it takes its
own name and says plainly that it is not affiliated with TeslaMate. Nothing about how it works has changed.

**To upgrade**, if you started from this repository's compose files:

- **Image:** now `ghcr.io/<owner>/garagebooks`. The old `teslamate-expenses` image gets no more releases, so
  change the `image:` line.
- **Address:** the default path is now `/garagebooks` (was `/expenses`). To keep the old address, set
  `BASE_PATH=/expenses` in `.env`. If you move to the new one, update **This app's URL** on the Settings page,
  or the dashboard's link back to the app goes to the old address.
- **Grafana:** the dashboards volume is now `garagebooks-dashboards`, mounted at `/dashboards-garagebooks`, and
  `grafana/provider.yml` puts the dashboard in a **GarageBooks** folder. Change both lines in Grafana's
  `volumes:` as [Setup](https://mattew124.github.io/garagebooks/setup/) shows, or the dashboard stops updating.
  The old, empty **Expenses** folder can be deleted.
- **Compose service:** `expenses` is now `garagebooks` (so `docker compose logs garagebooks`), and the Traefik
  router `tm-expenses` is now `garagebooks`.

Unchanged: the `expenses` schema and your data, the `EXPENSES_*` settings, the dashboard's address
(`/d/tm-expenses-tco`) and invoice folders. A folder marked with `.teslamate-expenses` keeps working.

Also in this release: a new logo and icon (app header, browser tab, home-screen icon, README and documentation
site), and the repository no longer holds a copy of TeslaMate's table definitions (they are AGPL-3.0, this
project MIT): tests and the dev stack get them by running the real TeslaMate image.

## 0.3.0-rc1 - 2026-11-13

Two changes make this release worth having. **Your invoices no longer have to live in OneDrive:** keep them
in a folder on your own server (a NAS share, say) and the app saves them there, opens them from the
expense list behind the same login as every other page, and can import the ones you already have. That
makes the app usable without a Microsoft account, and keeps your records on hardware you control.
**TeslaMate can change its database in any release, and this app reads that database directly,** so it now
knows which TeslaMate it was tested against (4.4.0) and refuses to start on a newer one, instead of quietly
showing wrong totals. Every release is also tested against TeslaMate's own schema, not a copy of it.

**To upgrade:** pull the new image. The app adds two columns to its own tables on start. If you use the
restricted database role (`SKIP_MIGRATE=1`), re-run `scripts/provision-db.sh` first: the app now reads
TeslaMate's `schema_migrations` table to check the version. The server folder is off until you turn it on.

### Added

- **Invoices in a folder on the server**, as an alternative to OneDrive. Each car uses one or the other.
  - Set up on one **Invoice storage** page (it replaces the OneDrive page; old links still arrive), always
    in the header: the folder's path, then the folder each car uses, chosen with the same picker as OneDrive.
  - Uploads go into the category's subfolder; the Add form can pick a PDF already in the folder; **Import
    existing invoices** works there with the same preview and learning (#12).
  - Safe by design: a file is never overwritten, nothing outside the mounted folder is read or written,
    and a folder that has gone missing is an error, never recreated.
  - The Grafana table links to stored invoices through **Settings → This app's URL**.
  - With `docker-compose.invoices.yml` the app doesn't run without the folder (`INVOICES_REQUIRED=1`): an
    unmounted share stops it with the reason in `docker compose logs`, and the container turns unhealthy
    if the folder goes away later. `INVOICES_REQUIRED=0` keeps it running instead.
  - To turn it on, set `INVOICES_HOST_DIR` in `.env` and add `docker-compose.invoices.yml`
    (docs, "Invoice folder on the server").
- **A TeslaMate version check.** The app records the TeslaMate release it was tested with and refuses to
  start if the database is from a newer one, naming both. `ALLOW_UNTESTED_TESLAMATE=1` starts it anyway and
  logs an error.
- `scripts/teslamate-compat.sh` tests this project against any TeslaMate release in a throwaway database,
  and a weekly workflow does so for the latest release.
- Docs and README: screenshots of the expense list, form, settings, Grafana dashboard, the Invoice storage
  page, the invoice folder window and the import preview.

### Changed

- GitHub release notes now open with this changelog's entry and group the merged pull requests (new
  features, fixes, documentation, dependencies, internal), like TeslaMate's own. Nothing to do to upgrade.

## 0.2.0-rc5 - 2026-11-11

The fifth candidate for 0.2.0. Everything under 0.2.0-rc1 to 0.2.0-rc4 below applies to
anyone upgrading to it, including their action-needed items.

### Added

- A PDF can be dropped onto the invoice box of the Add expense form, as well as chosen with the button.
- Column widths in the rows layout can be changed by dragging the edge of a heading (double-click for the
  usual widths); they are remembered in the browser.
- Docs: a worked example of publishing the app through Traefik and a Cloudflare Tunnel, gated by
  Cloudflare Access (Publishing with Traefik and Cloudflare). Nothing to do to upgrade.

### Changed

- A new look for every page: a tinted background with content in cards, a header that marks the page
  you are on, categories as coloured chips, refunds in green, and a dark theme that follows the device.
  The expense list has a heading with the total beside it, and its tiles lead with the vendor and amount.
  The form is grouped into Invoice, Details, and Link and notes. Nothing to do when upgrading.
- The expense list shows the date without the time of day (the time is the cell's tooltip, and still
  on the edit form).
- The header's Dashboards link shows the Grafana logo instead of an arrow. Like the OneDrive logo it is
  fetched once by the server from the pinned selfh.st/icons collection, with a plain drawing as the
  fallback; `PROVIDER_ICONS=0` switches both requests off.

### Fixed

- Going Back then Forward quickly on the expense list could leave the older list showing.
- The Rows and Tiles buttons did nothing in a browser that still held the previous version's cached
  files. Stylesheet and script addresses now carry a version, so an upgrade always loads matching files.

## 0.2.0-rc4 - 2026-11-11

The fourth candidate for 0.2.0. Everything under 0.2.0-rc1 to 0.2.0-rc3 below applies to
anyone upgrading to it, including their action-needed items.

### Added

- Negative amounts, for rebates and refunds (a state EV rebate, an insurance refund when a policy
  ends early). Enter the amount with a minus sign, such as `-3000`; it lowers the totals. The app drops
  the old `amount >= 0` check from `expenses.expense` itself on start; with the least-privilege role,
  re-run `scripts/provision-db.sh` first.
- The amount field works out sums such as `120.50+89.95` or `400/3`, with operator keys under it.
- The expense list sorts by any column, in either direction.
- A rows/tiles toggle on the expense list; the choice is remembered in the browser and applied before the page draws.
- Filtering and sorting the list update it in place, with no page reload; the filter boxes and table columns keep fixed sizes, so nothing shifts.

### Changed

- Editing, saving, cancelling or deleting an expense returns to the category and sort order you came from,
  instead of the unfiltered list.
- Dashboard: the pie and bar charts give Charging and the starter categories fixed, clearly different
  colours, and the bars no longer fade with a gradient.

## 0.2.0-rc3 - 2026-11-10

The third candidate for 0.2.0. Everything under 0.2.0-rc1 and 0.2.0-rc2 below applies to
anyone upgrading to it, including their action-needed items.

### Changed

- Importing from OneDrive reads every waiting PDF in one go, instead of ten at a time.
- Dashboard: thousands separators and decimal marks that follow the browser's language, with fixed
  decimals, and cost and distance per day. A Period box (week, month or year) for the cost charts, a time range that starts at your earliest
  data instead of a fixed five years, a Category filter for the headline figures, and "Months" is now "Time owned", in years and months.
  The timezone box is gone: the timezone comes from `APP_TZ`.

### Added

- A **Skip** tick on each import row, for files that never need importing, and a prompt on Import to
  skip whatever was left unticked. The app creates its new
  `expenses.import_skip` table itself; with the least-privilege role, re-run `scripts/provision-db.sh`.
- Add and Edit expense: **Choose from OneDrive**, a button under the PDF field, opens a window over the form to pick a PDF that is
  already in OneDrive. It starts in the car's folder and can go up, to the top of the drive. The file's
  link becomes the invoice link and the form fills in from it, as for a chosen file. Nothing is
  copied. The button shows OneDrive's logo, fetched once from `cdn.jsdelivr.net` and kept in memory rather
  than shipped with the app (from selfh.st/icons, CC BY 4.0); `PROVIDER_ICONS=0` stops that (a plain cloud is shown). Needs nothing to upgrade.

## 0.2.0-rc2 - 2026-11-10

The second candidate for 0.2.0. Once it has been tested on production, the same commit is
tagged `v0.2.0`, the first stable release. Everything under 0.2.0-rc1 below applies to
anyone upgrading to it, including its action-needed items.

### Added

- A documentation site (built from `docs/`, published to GitHub Pages) covering setup, OneDrive,
  importing, the dashboard and reference. The README is now a short overview that links to it.
- Every page's footer shows the installed version and links to the documentation. A released
  image also asks `api.github.com`, in the background and at most every six hours, whether a
  newer release exists, and links to its release notes. Local builds never ask. Set
  `UPDATE_CHECK=0` to switch the check off.
- The release workflow creates a GitHub Release for each tag, with notes generated from the
  merged pull requests. Candidates are marked pre-releases.

### Changed

- Settings: the invoice date format and file name fields now hold their defaults as real,
  editable text instead of a placeholder that vanishes as you type. Saving the default
  unchanged stores "default", so it keeps following the default.
- The OneDrive page's note about file names now shows the name your current Settings
  produce, and links to Settings. It used to show a fixed example that didn't match.
- OneDrive: the page says that a newly added redirect URI can take a few minutes to work.

### Documentation

- `CONTRIBUTING.md`, plus pull request and issue templates with checklists for contributors.
- README setup follows a real deployment: back up first, set `FQDN_TM` before starting,
  check the app started, and recreate Grafana.
- README: the app creates its own tables when OneDrive is turned on, so there is no
  separate provisioning step.

### Upgrading

No schema change since 0.2.0-rc1. One new optional variable, `UPDATE_CHECK` (default on; `0`
switches off the release check, the only time the app contacts GitHub). Anyone coming from
earlier than rc1 should read its action-needed items below.

## 0.2.0-rc1 - 2026-11-09

### Action needed before upgrading

- **`ONEDRIVE_TOKEN_KEY` is now `EXPENSES_ENCRYPTION_KEY`.** Rename it in your environment.
  The old name still works but logs a warning pointing at the new one. Keep this key out
  of database backups: it is what protects the stored OneDrive sign-in.
- **Restricted database role** (`SKIP_MIGRATE=1`): re-run `scripts/provision-db.sh` first.
  This release adds two tables and a column, which the app won't create itself in that mode.

### Added

- Import existing invoices from OneDrive, reading the PDFs for vendor, date, amount and
  odometer. The import page opens at once and shows progress while it reads.
- The importer learns labels and vendors from your corrections (labels and categories
  only, never amounts or document text).
- Odometer from TeslaMate on import, and an optional rename of the OneDrive file.
- The Add page prefills from the invoice PDF, and the PDF field now comes first.
- Uploads go into per-category subfolders of the car's OneDrive folder, editable on the
  Categories page.
- Settings: an invoice file name template with `%date %category %vendor %car %amount`
  aliases.
- Settings: which figure to take when an invoice shows several amounts (6 or 12 months):
  the largest (default), the smallest, or leave blank.
- A warning about likely duplicates before an expense is saved.
- `LOG_LEVEL` (debug, info, warning, error) for the app and gunicorn. File names, which
  can hold a vendor, are logged at debug only.
- The app writes its Grafana dashboard to a shared volume for Grafana to provision.
- A site icon, and a README header with badges.
- Pre-release tags also publish the floating `:rc` image tag.

### Changed

- The vendor is the trading name, not the legal entity.
- The PDF's own contents are trusted over its file name.
- The import page calls its template flag "scanning", which is what it means.
- Developer tooling: a private file outside the repository holds the dev OneDrive sign-in,
  and worktrees run real OneDrive checks against a test folder only.

### Fixed

- A crash on PDFs over 500 KB, and more real-world invoice layouts are read correctly.
- The header's icon sits beside the Expenses link, which goes home.

### Security and quality

- CodeQL, Trivy, OpenSSF Scorecard, Dependabot, Ruff, Bandit, Hadolint and coverage in CI,
  on Python 3.15. Reviewed lint exceptions are excused where they occur, with the reason.
- GitHub Actions dependencies updated.

## 0.1.0-rc1 - 2026-11-08

The first candidate.

### Added

- Expense tracking and total cost of ownership for TeslaMate: a Flask UI, an `expenses`
  schema in the TeslaMate database, and a Grafana dashboard.
- The app connects as TeslaMate's own database user and creates its `expenses` schema on
  start. `scripts/provision-db.sh` remains as the optional least-privilege path.
- Upload invoice PDFs to OneDrive, into a folder per car. Sign in on Microsoft's page,
  choose folders by browsing, and create a new folder from the browser. Settings include
  the date format in uploaded file names (default `yymmdd`).
- Links to the UI and Grafana from the URLs set in TeslaMate's Settings, kept on the app's
  own Settings page and pre-filled from `FQDN_TM` on first start.
- A step-by-step Microsoft app registration walkthrough in the README and on the OneDrive
  page.
- A separate dev stack per git worktree, for parallel sessions.
- Release process in RELEASING.md.

### Fixed

- The header layout on phones.
- OneDrive Connect now goes to Microsoft by navigation, not a form redirect.

`0.1.0-rc1` was superseded by `0.2.0-rc1` and never released as stable. The version number
jumped because 0.2.0-rc1 added tables and renamed an environment variable.
