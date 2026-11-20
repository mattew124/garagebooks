# Development

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q     # starts a throwaway postgres:18 container
.venv/bin/python -m pytest -q -n auto   # the same, in parallel: about three times faster on four cores
```

With `-n auto` (pytest-xdist) each worker starts its own Postgres container, so tests never share tables. CI and
`scripts/teslamate-compat.sh` use it. A single test is quicker without it, as each worker pays the container's start-up.

The tests let the real TeslaMate image (the release in `garagebooks/compat.py`) create its tables in a
throwaway database, so docker pulls `teslamate/teslamate` the first time. They then start the app so
it creates its own schema, and also cover the optional `scripts/provision-db.sh` role.

TeslaMate is optional, so the suite runs both ways, and CI runs both (the `test` and `no-teslamate`
jobs). A feature isn't done until its tests pass in each:

```bash
TEST_TESLAMATE=0 .venv/bin/python -m pytest -q   # plain PostgreSQL, no TeslaMate tables at all
```

Mark a test that needs TeslaMate's real tables `@pytest.mark.teslamate` (skipped in that run), and use
the `empty_db` fixture for one about a database with no TeslaMate, which then runs every time. New code
never queries TeslaMate's tables itself: it asks `garagebooks/sources.py` ("which cars?", "what was the
odometer?", "what did charging cost?") and handles "no answer".

## Reuse before you build

A second page that shows something an existing page already shows uses that page's code, not a copy that
drifts. Before writing a list, a form, a filter, a table or a query, look for the one that exists, and extend
it so both places use it. For example, the expense list (its filters, sort links, rows and tiles, and the live
filtering in `app.js`) is the partial `templates/_expense_list.html`, built by `list_context()` in
`garagebooks/__init__.py`: the All page and the Claims page both render it, and a new filter (a car, say) is
added there once and appears on both. Put a new column or state behind a flag that the partial's callers set,
the way `ledger` adds the claim column. If the existing code can't do what you need without bending it out of
shape, change it first, in its own commit, then use it. Reviewers will ask where the duplicate came from.

## Quality gates

CI fails a pull request that does not clear these, and you can run each of them yourself first:

- **Coverage floor of 90%.** `pytest -q --cov` fails under the `fail_under` value in `pyproject.toml`
  (`.venv/bin/coverage report` lists the lines that are not covered). Write a test for the behaviour, rather
  than excluding the code.
- **Property-based tests.** [Hypothesis](https://hypothesis.readthedocs.io) generates the input, so the tests
  in `tests/test_properties.py` (every parser in `validation.py`, the invoice text reading, release tags)
  and `tests/test_properties_routes.py` (every route that takes a form, a query string, JSON or an
  upload) check what must hold for *any* input: a parser returns a value or its `ValueError` and nothing
  else, what it accepts has the shape the app relies on, a value written the way the app writes it reads back
  unchanged, no route answers with a server error, and nothing invalid reaches the database. Add a new input
  to the matching file when you add a field or a route. When Hypothesis finds a failing example, keep it as an
  ordinary test too (as `test_amount_rejected` does), then fix the code. A local run uses 50 examples
  per property; CI sets `HYPOTHESIS_PROFILE=ci` for 200 (the route tests use 15 locally and 40 in CI, as each is a real
  request on a real database). The `.hypothesis/` folder is a cache of what it has found: leave it out of commits.
- **Vulnerability scans.** Ruff and Bandit lint the code, CodeQL analyses it, and Trivy scans the
  dependencies and the image (`.github/workflows/security.yml`, weekly as well as on each change).
- **A software bill of materials.** The release workflow lists what is in each published image (every
  operating system and Python package, with its version) as SPDX JSON, one file per platform, and attaches
  both to the GitHub release as `garagebooks-<tag>-<platform>.spdx.json`.

## Testing against a TeslaMate release

TeslaMate can change its tables in any release, so each release of this app records the one it was
tested with (`TESTED_TESLAMATE` and `TESTED_MIGRATION` in `garagebooks/compat.py`) and holds
TeslaMate's figures back on a newer database. To try a new TeslaMate, with nothing but docker and the `.venv`:

```bash
scripts/teslamate-compat.sh 4.5.0            # or `latest`; defaults to the recorded release
scripts/teslamate-compat.sh 4.5.0 --record   # after it passes: update compat.py
```

It starts a throwaway Postgres, lets the real `teslamate/teslamate` image migrate it, runs the
whole suite on that schema, and compares the
newest migration with the recorded one. Exit status 0 means tested, 1 means a failure,
2 means it passes but is newer than recorded. `--if-new` exits 0 at once when the release is already the
recorded one. A daily *TeslaMate compatibility* workflow runs it with `--if-new` against the latest release
(so nothing runs unless there is a new one). On a failure (exit 1) it opens an issue. When the tests pass on a
newer release (exit 2) it has already run `--record` in that same run, which updated `compat.py`, the README line and the CHANGELOG, opens a
pull request and runs CI on it. When the next tag is clear (`scripts/auto_rc.py`: the next `-rcN` of a version that
has only had candidates, or `-rc1` of a PATCH after a stable release when the only unreleased change is the
compatibility entry), it then merges the pull request, renames the CHANGELOG entry, tags that candidate and
starts the Release workflow, so the image is built for you to test. It never tags a stable release. If the tag
isn't clear, the pull request is left open. It needs *Allow GitHub Actions to create pull requests* turned on in
the repository's Actions settings.


## Tax rates for the lease workbook

The [novated lease calculations](lease-calculations.md) workbook takes its tax rates from the ATO's
pages. To rebuild it with today's rates (needs the `.venv`, which has `openpyxl`):

```bash
.venv/bin/python scripts/build_lease_workbook.py   # rewrites docs/assets/lease-calculator-example.xlsx
.venv/bin/python scripts/ato_rates.py              # just print the rates it read, as JSON
```

The tests give both scripts made-up pages and never use the network. Commit the rebuilt workbook
only if the ATO's rates changed.

## The TeslaMate project listing

TeslaMate's "Projects using TeslaMate" page can describe this app. The text we want there is
`.github/teslamate-listing.md`, and its first line records a hash of the README's feature list
that it was written against. A pull request that changes a feature bullet in the README fails
`tests/test_listing.py` until you have checked the listing still describes the app (a new storage
option, say), edited it if not, and set the hash the failure message gives. The pull request's
title and body are `.github/teslamate-listing-pr.md`, reviewed here like any other change.

```bash
scripts/teslamate-listing.sh check     # how TeslaMate's page differs from our entry (read-only)
```

```bash
scripts/teslamate-listing.sh preview   # everything `propose` would send (sends nothing)
```

```bash
scripts/teslamate-listing.sh propose   # shows the preview, then opens or refreshes the pull request if you type yes
```

Proposing it publishes under your GitHub account on `teslamate-org/teslamate`, so it only happens
when you decide to: `propose` refuses to run without a terminal or under CI, and no workflow runs it
(a test checks). It updates the same branch and pull request each time. The entry follows
TeslaMate's trademark policy: no TeslaMate in the name, and its disclaimer word for word (a test
checks both).

## CodeQL before you push

CI runs CodeQL on pull requests (`.github/workflows/codeql.yml`). To get the same answer locally:

```bash
scripts/codeql.sh setup   # once: downloads the CodeQL bundle (about 1 GB) to ~/.local/share/codeql
scripts/codeql.sh         # analyses python and actions in about 30 seconds; exits 1 on findings
```

It needs the `gh` CLI and `zstd`. Databases and SARIF results go to `.dev/codeql`. Set
`CODEQL_HOME` to install elsewhere, or `CODEQL_VERSION` (a `codeql-bundle-v…` tag) to pin a version.

## Dev stack

`scripts/dev.sh` runs the UI and Grafana on their own test database, not on the TeslaMate
network, so it can't touch live data. It listens on localhost unless `dev.env` says otherwise
(see `dev.env.example`). The scheduled jobs run in the app; set `SCHEDULER=worker` in `dev.env` to run them in a
`dev-worker` container instead, as in production (see [Scheduled jobs](scheduled-jobs.md)).

```bash
scripts/dev.sh up             # http://localhost:8089/garagebooks-dev/ and :8090/garagebooks-dev/grafana/
scripts/dev.sh test-browser   # Playwright in a container; screenshots in .dev/screenshots
scripts/dev.sh refresh        # re-copy TeslaMate data, keeping your test expenses
scripts/dev.sh reset          # wipe the test database
scripts/dev.sh demo up         # second stack of invented data, for screenshots (see below)
scripts/dev.sh sample-expenses  # add made-up expenses to try the app with; `clear` removes them
scripts/dev.sh sample-invoices  # write made-up invoice PDFs into the stack's invoice folder
scripts/dev.sh setup          # in a new worktree: create .venv
scripts/dev.sh down           # stop the stack, keeping its data
scripts/dev.sh logs           # follow the app's log (and the worker's, with SCHEDULER=worker in dev.env)
scripts/dev.sh psql           # a psql shell on the test database
scripts/dev.sh onedrive-save  # save a OneDrive sign-in for the dev stack to reuse
```

Each git worktree gets its own stack and test database at `/garagebooks-dev/<worktree>/`, so
parallel branches never share running code. `scripts/dev.sh info` shows a checkout's path and
ports, `list` shows every running dev stack, and `destroy` removes one (its containers, test database, image and `.dev` folder). `.worktreeinclude`
copies your `dev.env` into new Claude Code worktrees.

Stacks add up when several branches are in flight, and each holds a database, Grafana and the app in
memory. Two commands clear away the ones nobody needs:

```bash
scripts/dev.sh prune --dry-run   # what prune would remove, and why
scripts/dev.sh prune             # destroy the stacks of merged branches, and of checkouts that are gone
scripts/dev.sh auto-prune on     # run prune every hour from cron; `off` stops it, no argument says which
```

`prune` looks at every dev stack on the machine, not only this checkout's. A worktree's stack (and its
demo stack) goes once the worktree's last commit has been merged into `origin/main` (fetched first) with a
merge commit, as pull requests are here; a squash or rebase merge isn't recognised, and that stack is kept.
A branch just made from `main`, with nothing on it yet, is not merged. A merged worktree that still has
uncommitted changes is kept and named in the output. A stack whose checkout has gone (removed with
`git worktree remove` before `destroy`) is taken down by name, with its test database and its image. The
main checkout's own stacks are never pruned. `prune` removes stacks only: the worktree folders stay, as with
`destroy`. `auto-prune on` adds one entry to your crontab that runs the main checkout's `dev.sh`, so it
outlives every worktree, and logs to `~/.cache/garagebooks/dev-prune.log`.

Each dev stack creates its own Docker network. Docker's default address pool starts at
`172.17.0.0/16` and moves on to `192.168.0.0/16` after about ten networks, and a network
there (even a stopped one) captures routing for the whole range, which VPN clients and home
LANs commonly use. Keep to `172.16.0.0/12` by setting `default-address-pools` in
`/etc/docker/daemon.json` (for example `{"base": "172.28.0.0/14", "size": 20}`, then restart
Docker), never set a `subnet:` in `192.168.0.0/16`, `10.0.0.0/8` or `100.64.0.0/10`, and run
`scripts/dev.sh destroy` (or `prune`) for stacks you no longer need. `ip route | grep 192.168` should print
nothing.

The invoice folder is on in the dev stack too: `.dev/invoices` in the checkout stands in for a mounted
share (the app runs as you there, so its files are yours), and `destroy` removes it. Drop PDFs into it
to try the import, or let `sample-invoices` write some made-up ones into `.dev/invoices/Sample invoices`;
then choose that folder for a car on the Invoice storage page. To try it on a real
network share instead, mount the share on the dev machine and set `DEV_INVOICES_SHARE` in `dev.env`
to where it is mounted (see `dev.env.example`). Each stack then keeps its own folder in the share, named
after the stack (`<share>/<worktree name>`, `main` for the main checkout), so worktrees never mix
their test PDFs, and the app writes there as you. `up` makes that folder, but never the share itself:
a share that isn't mounted, only its empty mount point, is refused. `destroy` leaves the folder on
the share, so delete it yourself when you no longer need the test PDFs. `DEV_INVOICES_DIR` names exactly one
folder instead, for every stack. The demo stack never uses either.

OneDrive is on in the dev stack with a throwaway token key in `.dev/env`. To sign in there,
add the dev stack's callback address (shown in the Microsoft account card of its Invoice storage page) as a second redirect URI on
your app registration. A sign-in writes to your real OneDrive, so give each car a test folder.

The claims feature is on in the dev stack against a stand-in connector (`tests/fake_connector.py`, invented
answers, on the stack's private network). The app signs each request to it with a new Ed25519 key pair that `up` makes
at every start (the stand-in is given only the public half, so nothing there is a secret). To try a real connector
instead, set `DEV_CLAIMS_CONNECTOR_URL` and `DEV_CLAIMS_CONNECTOR_KEY_FILE` (the path of a private key file) in
`dev.env` (see `dev.env.example`). Give it a key pair of its own that you can revoke, not your live one, and remember
that submitting there sends a real claim: preview and sync only, unless you mean it. The address must be reachable
from the stack's containers. `up` says which connector it uses and refuses a real one with no key file, or one the
user running it can't read. The browser tests submit claims, so they refuse to run while a real
connector is set: use `STAND_IN=1 scripts/dev.sh up` and `STAND_IN=1 scripts/dev.sh test-browser` for them.

The seed only reads from TeslaMate. It copies all of `cars` and `charging_processes`, and one
`positions` row per hour with latitude, longitude and elevation blanked. `up` warns if
TeslaMate's tables no longer match the committed schema. Grafana also lists TeslaMate's bundled
dashboards, which won't work there because the test database only has these three tables.

## Releasing

Pushing a `v*` tag runs the tests and publishes a multi-arch image to
`ghcr.io/mattew124/garagebooks`. Pre-release tags such as `v0.2.0-rc1` don't move `:latest`.
The run summary shows the digest, so what you test is what you deploy:

```bash
git tag v0.2.0-rc1 && git push origin v0.2.0-rc1
scripts/dev.sh up --image ghcr.io/mattew124/garagebooks@sha256:...
scripts/dev.sh test-browser
```

Then pin production to that digest. Schema changes apply when the new version starts.

## Documentation and screenshots

This site is built from `docs/` by [Zensical](https://zensical.org) and published to GitHub Pages
each time `main` changes. To preview it:

```bash
.venv/bin/pip install -r requirements-docs.txt
```

```bash
.venv/bin/zensical serve
```

A pull request that changes what a user or installer sees updates the pages it affects. A new or
visibly changed screen also needs a screenshot in the docs.

**Screenshots must never show real data, so they can't come from the dev stack**, which copies your
real cars and charging costs. They come from the demo stack instead, whose data is invented.

**They must not show the OneDrive logo either**: Microsoft's brand terms don't allow it in published
material. The app shows the logo to its own users, so take care with the pages that carry it (the
**Invoice storage** page's OneDrive card, the OneDrive button on the Add form), and leave those out or
crop them. Grafana's logo, which Grafana licenses for this, may appear.

```bash
scripts/dev.sh demo up
```

```bash
scripts/dev.sh demo screenshots
```

`demo up` builds a separate stack (its own database, app and Grafana, at `<path>-demo/`). Each time it is
built, the empty `cars`, `positions` and `charging_processes` tables are created from a fresh schema-only
dump of your live TeslaMate, so the demo can't fall behind TeslaMate's schema; no row is read. The rows
come from `scripts/demo-seed.sql` (one made-up car, three years of odometer readings and charging
sessions) plus the sample expenses. `demo reset` rebuilds it, `demo destroy` removes it. Files land in
`.dev/demo-screenshots`; copy the ones you want into `docs/images/` and look at each first, as the app
shows your own addresses on some screens (`tests/browser/screenshots.py` swaps those for example ones).
`dev.sh screenshots` works only after `demo`, as above. A test fails if an image in `docs/` isn't under `docs/images/`, except the logo and icons in
`docs/assets/`.

Where a page needs a screenshot that hasn't been taken yet, add a marker on its own line, at the place
in the page it belongs: an HTML comment holding `screenshot:`, a short name, a dash and what it should
show, as in `<!-- screenshot: settings-page - The Settings page -->`. Markers don't appear on the
site. To list the screenshots still to take:

```bash
git grep -n '^<!-- screenshot:' -- docs
```

Version numbering and the candidate-to-stable steps are in
[RELEASING.md](https://github.com/mattew124/garagebooks/blob/main/RELEASING.md).

### Keeping the docs in step with the code

Three things check this, from cheapest to most thorough:

- `tests/test_docs_drift.py` fails when the app reads a setting, or `scripts/dev.sh` offers a command, that no page mentions.
- The *Docs sync* workflow reads each pull request's code changes and, if a page has fallen behind, commits a Markdown-only fix to the branch. It needs a `CLAUDE_CODE_OAUTH_TOKEN` repository secret (from `claude setup-token`), and it skips drafts, forks and bots.
- `/docs-sync` in Claude Code sweeps the whole repo for drift on demand.
