# Changelog

All notable changes to garagebooks. Version numbers and the candidate-to-stable
steps are in [RELEASING.md](RELEASING.md). Newest first.

Each entry says what an installer must do to upgrade. Where nothing is said, pull the new image.

## Unreleased

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
