# Reference

## Invoice links

**Invoice links** must be `https://` (rendered as a link, so `javascript:`/`data:` are refused). Whoever opens one needs access to it, and it lasts as long as its sharing settings. An uploaded invoice's link is its plain OneDrive address, not a share link: it opens for you, signed in to that Microsoft account, and for nobody else.

An invoice kept in the [invoice folder on the server](local-folder.md) has no link at all. The expense stores the file's path below that folder, and the app serves it at `/invoice/<expense id>`, behind the same forward-auth as every page: shown in the browser's PDF viewer, or saved as a file with `?download=1`. Only PDFs inside the mounted folder are ever served.

## Version and updates

The footer of every page links to this documentation and shows the installed version. A released
image also says whether it is the latest, and links to the release notes of the newer release when
there is one.

<!-- screenshot: footer-version - The footer, showing the version and an update notice -->

The version is the release tag the image was built from. A build without one (`docker compose build`,
the dev stack) shows *Development build* and never checks.

To find out whether a newer release exists, the app makes one anonymous request to the public
releases list on `api.github.com`, at most every six hours, in the background (a page never waits for
it). GitHub sees your server's address and a `User-Agent` of `garagebooks/<version>`; nothing
from your data is sent. If GitHub can't be reached, the footer simply shows the version and tries
again later. Set `UPDATE_CHECK=0` in `.env` and restart to switch the check off.

The OneDrive button on the expense form shows OneDrive's logo, and the header's Dashboards link shows
Grafana's. The logos aren't shipped with the app: the
first time one is needed, the server asks `cdn.jsdelivr.net` once for a pinned copy from the
[selfh.st/icons](https://selfh.st/icons) collection (licensed [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/)), checks that what comes back is a plain drawing, keeps it in memory and passes it to browsers. Nothing
from your data is sent (jsDelivr sees your server's address). If it can't be reached, a plain drawing
that comes with the app (a cloud, a bar chart) is shown instead, and the server tries again later. Set `PROVIDER_ICONS=0` in
`.env` and restart to never ask. The drawing is shown as the collection supplies it, except that the
app rebuilds it from the parts it checked and writes its inline styles as plain attributes. OneDrive is a
trademark of Microsoft Corporation; Grafana is a trademark of Grafana Labs.

An installation on a release candidate (`0.3.0-rc1`) is told about newer candidates and the stable
release that replaces them. One on a stable release is only told about stable releases.

## Database connection

The app connects as `DATABASE_USER` with `DATABASE_PASS` to `DATABASE_NAME` on `DATABASE_HOST` (default `database`, the TeslaMate service name) and `DATABASE_PORT` (default `5432`). Set the last two in `.env` only if TeslaMate's database runs under another name or port. `DATABASE_URL` (a full `postgresql://` address) replaces all of these when set.

## TeslaMate version

Each release is tested against one TeslaMate release (named in the README and in `garagebooks/compat.py`). TeslaMate has no stable database schema, so on start the app compares the newest migration in TeslaMate's `schema_migrations` table with the one it was tested against. If the database is **newer**, the app still runs, and holds TeslaMate's figures back: the odometer look-up, TeslaMate's charging costs and the totals that need them say *unavailable* for the cars TeslaMate tracks, with the reason on the Cars page, at the top of the dashboard and in the log. Everything you entered is unaffected. Upgrade GarageBooks to a release that lists a newer TeslaMate and the figures return by themselves. `ALLOW_UNTESTED_TESLAMATE=1` (in `.env`, restart to apply) uses them anyway and logs an error; use it only for a short while, since a changed table can make the figures wrong. An older TeslaMate is not held back. If the role cannot read `schema_migrations`, the app warns and carries on.

## Without TeslaMate

TeslaMate is optional. GarageBooks keeps its own list of cars (the Cars page): the cars TeslaMate tracks are added to it automatically, and any other car, electric or not, is added by hand. What TeslaMate would supply is then entered by hand: the odometer on your expenses, and charging or fuel costs as expenses in the **Charging** and **Fuel** categories.

The app looks at the database once a minute and is in one of four states, shown on the Cars page:

| State | When | What happens |
| --- | --- | --- |
| Connected | TeslaMate's tables are there, readable, and a tested version | Its odometer and charging costs are used for the cars it tracks |
| Not configured | No TeslaMate in this database | Nothing is missing: everything comes from what you enter |
| Untested | A newer TeslaMate than this release was tested with | Its figures are held back, as above |
| Unavailable | Its tables can't be read (permissions, or a table that changed) | The same, with the error as the reason |

Held-back figures are shown as *unavailable*, never as zero, and nothing else stops. When TeslaMate appears, is repaired or is upgraded past, the app notices within a minute, with no restart: it adds TeslaMate's cars, and rewrites the dashboard and the `garagebooks.ownership` view to match. GarageBooks only ever reads TeslaMate's `cars`, `positions` and `charging_processes` tables, and none of its own tables points at them, so TeslaMate can be removed, or delete a car, without touching your expenses.

## Scheduled jobs

`SCHEDULER` (in `.env`, restart to apply) says where the periodic work runs: `app` (the default, a background
thread in the app), `worker` (a second container from the same image, started with `docker compose --profile
worker up -d`) or `off`. See [Scheduled jobs](scheduled-jobs.md).

## Logs

`docker logs` shows gunicorn's request lines plus the app's own events. `LOG_LEVEL` (`debug`, `info` the default, `warning`, `error`; set in `.env`, restart to change) sets both. `info` records startup, the update check's result, schema changes, expenses saved, updated or deleted, OneDrive sign-in and upload results, and anything the invoice folder on the server refuses (a missing folder, no permission). `debug` adds detail such as retries. Tokens, secrets, vendors, notes and amounts are never logged, except that file names, which can contain a vendor, appear at `debug`.

## Change history

A trigger in the `garagebooks` schema writes a row to `garagebooks.change_log` for every insert, update
and delete of an expense or a category, with the time, the whole row before and after (as JSON) and who
made the change. Because it is in the database, changes made by other tools straight in the database
are recorded too. An update that changes nothing (saving a form untouched) is not recorded. The log is
append-only: a second trigger refuses any update, delete or truncate of it, and the restricted database
role from `scripts/provision-db.sh` is granted only `SELECT` and `INSERT` on it. Nothing is recorded for
changes made before this version first started. The **History** page shows 50 entries at a time, in
`APP_TZ`.

The log holds the same vendors, notes and amounts as the expenses, so a database backup holds them too.
Deleting an expense does not remove its history.

**Who** comes from `AUTH_USER_HEADER`, the name of the request header your forward-auth gate sets to the
signed-in user (`X-authentik-email` for Authentik, `Cf-Access-Authenticated-User-Email` for Cloudflare
Access). Set it in `.env` and restart. It is unset by default, and then every change shows as *unknown*.
Set it **only** when the proxy removes that header from requests it receives from clients and
sets it itself, as the gates above do: otherwise anyone who can reach the app can put any name in the
log. The value is trimmed to 200 characters, with control characters dropped. Changes made outside the
app have no name either way.

## Timezone

Date and time are entered in `APP_TZ` and stored as `timestamptz`. TeslaMate's `positions.date` is naive UTC; the odometer lookup converts before comparing.

## CSRF protection

POSTs must carry a same-origin `Origin`/`Referer` header (CSRF guard). Plain `curl` needs `-H 'Origin: https://your-host'`.

## Not covered yet

Depreciation or resale value (so "TCO" is spend, not net cost), deleting invoice files from the app (it removes the link; the file stays), deleting categories.
