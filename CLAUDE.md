# garagebooks

Expense tracking and total cost of ownership for TeslaMate: a Flask UI
(`garagebooks/`), an `garagebooks` schema in the TeslaMate database (`db/`), and a
Grafana dashboard (`grafana/`). See README.md and `docs/`, the documentation site.

## Rules

- This is a public repository. Nothing specific to one installation goes in a
  tracked file: no hostnames, IPs, personal data or real invoices. Local settings
  belong in `dev.env` or `CLAUDE.local.md` (both gitignored).
- Never write to a real TeslaMate database while developing. The dev stack has its
  own test database; `scripts/dev.sh` only ever reads from TeslaMate.
- Test data is invented. When a real invoice, policy, registration notice or lease form shows how a document
  is laid out, copy its layout and labels only: never its file name, plate, VIN, invoice, policy, account or
  customer numbers, ABN, ACN or licence numbers, names, addresses, dates or amounts. Use invented values in the
  same format (ABC123, ABN 11 111 111 111, INV12345, Example Tyres Pty Ltd, round amounts). This applies to
  tests, fixtures, sample data, docs and commit messages.
- Location history stays out of the test database. `dev.sh` blanks latitude,
  longitude and elevation; don't copy more of `positions` without blanking them.
- The app has no login, so it must only ever be published behind forward-auth.
- TeslaMate is optional. Nothing queries its tables except `garagebooks/sources.py`: a feature asks
  there and handles "no answer" (never a zero standing in for it). No foreign key points at its tables,
  and the suite must pass both ways (`TEST_TESLAMATE=0 .venv/bin/python -m pytest -q` is the plain run).
- The app connects as TeslaMate's own database user and creates its `garagebooks` schema itself
  on start (`garagebooks/schema.sql`, idempotent, no setup scripts). It must only ever touch
  `expenses.*` and read `cars`, `positions` and `charging_processes`. `scripts/provision-db.sh`
  and `db/grants.sql` are the optional least-privilege path (`SKIP_MIGRATE=1`): keep them working.
- Invoice files on disk go through `garagebooks/storage.py` (`LocalFolder`), which keeps every path inside
  `INVOICES_DIR`. Never open a path built from a request any other way, never create `INVOICES_DIR`
  itself, and never overwrite or delete a file there. While a car uses it, a folder without its
  `.garagebooks` marker is an unmounted share: read and write nothing in it.
- No TeslaMate schema or code is committed (TeslaMate is AGPL-3.0, this repository MIT). Tests and the dev stack
  get its tables by running the real `teslamate/teslamate` image's migrations; never add a copy of its DDL.
- Write the failing test first, then the fix. Australian English in docs and UI.
- Linter findings: fix the code if a small change removes the finding (a clearer name, say). If it's
  a reviewed exception, excuse that one line and give the reason: `# nosec B603 - why` or
  `# noqa: B023 - why`; in a Dockerfile, `# hadolint ignore=DL3008` directly above the instruction with
  the reason on the comment line before it. Don't add project-wide ignores for this: they'd also hide
  future findings.
- Docs change with the code: a pull request that changes what a user or installer sees updates `docs/`.
  A new or visibly changed screen also gets a screenshot marker in the page it belongs on (see
  "Documentation and screenshots" in `docs/development.md`). Never publish a screenshot from the dev
  stack: it holds real cars and charging costs. The docs site is public too.

## Commands

```bash
.venv/bin/python -m pytest -q    # needs docker; starts a throwaway Postgres
scripts/dev.sh up                # dev stack; `refresh`, `reset`, `down`, `logs`, `sample-expenses`
scripts/codeql.sh                # CodeQL as in CI; `setup` first, once
scripts/dev.sh test-browser      # Playwright, real Chromium; screenshots in .dev/screenshots
.venv/bin/zensical serve         # docs preview; needs requirements-docs.txt
```

First-time setup: `python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt`.

Run both test suites before calling a change done, and look at the screenshots
after any UI or dashboard change.

## Working in a worktree

Parallel sessions each get a git worktree under `.claude/worktrees/<name>/`, and
`scripts/dev.sh` gives each worktree its own dev stack and test database at
`/garagebooks-dev/<name>/`, so you never test another session's code.

1. Start with `scripts/dev.sh setup` (creates `.venv`), then `scripts/dev.sh up`.
   `scripts/dev.sh info` shows this checkout's stack, path and ports.
2. One feature or fix per worktree. Commit on its branch, push it, and open a pull
   request so CI runs. Merging and releasing happen from the main checkout.
3. Anything touching OneDrive needs a real-account run before it's done, and you run it yourself, without
   asking: `scripts/dev.sh onedrive-link`, then `scripts/dev.sh test-browser --onedrive`. The link loads a saved sign-in from a private file outside
   the repository (`onedrive-save` writes it; `ONEDRIVE_DEV_FILE` moves it) and points this stack at the
   `/Expenses test` folder. Never commit that file, and never point a test at a folder holding real invoices.
4. When the branch is merged or abandoned, run `scripts/dev.sh destroy` before the
   worktree is removed. `scripts/dev.sh list` shows every dev stack still running, and
   `scripts/dev.sh prune` destroys those of merged branches and removed worktrees
   (`scripts/dev.sh auto-prune on` runs it hourly). Take a stack `down` while its branch waits for review:
   a host running many stacks at once runs out of memory.
   Each stack creates a Docker network, and Docker's default address pool runs into
   `192.168.0.0/16` once enough exist. A leftover bridge there, even a stopped one,
   hijacks routing to that whole range (VPN clients and other LANs use it). Never pin a
   `subnet:` in `192.168.0.0/16`, `10.0.0.0/8` or `100.64.0.0/10`, and after `dev.sh up`
   check `ip route | grep 192.168` prints nothing.

## Releasing

A `v*` tag publishes the image to ghcr.io. Test with a candidate tag first, by digest:
`scripts/dev.sh up --image <ref>`, then `scripts/dev.sh test-browser`. Pin
production to the digest that passed. Version numbering and the
candidate-to-stable steps are in RELEASING.md.

## Several branches at once

Parallel work (sub-agents, stacked pull requests) collides in the same few files. To keep merges small:

- Put new schema in one contiguous block, appended at the end of `schema.sql` (or after the block it
  depends on). Never reformat existing lines. Add view columns at the end only.
- Dashboard panel ids are unique across the file. Before adding a panel, list the ids on `main` and the
  open branches (`git grep '"id":' origin/<branch> -- grafana`), and take the next free number. Add new
  panels as a row below existing ones, and don't move existing panels.
- A CHANGELOG entry goes at the top of **Unreleased**. Expect to resolve it by hand when branches merge.
- A branch that depends on another is stacked on it (`gh pr create --base <that branch>`), says so at
  the top of its pull request, and is retargeted to `main` when the base merges.
- Give each `gh pr create` its own body file (`--body-file`) with a unique name: `/tmp/pr_body.md` is
  shared between sessions, and one session's text ended up in another's pull request.
- To try several branches together, merge them into a throwaway local `integration/*` branch and run
  `scripts/dev.sh up`. Don't push it.

## Tests

- Failing test first means it was run and seen to fail. Say so in the pull request. Writing tests
  and code together doesn't count.
- A test run is only finished when all three pass: `pytest -q --cov`, `TEST_TESLAMATE=0 pytest -q`
  (plain PostgreSQL, once the suite supports it), and `scripts/dev.sh test-browser` (after
  `scripts/dev.sh up`, so the stack runs the latest code).
- Browser tests: select within one form (`form[action$="/readings"] input[name=date]`), not
  `.car input[name=date]`, because a card holds several forms. Clean up what a test adds in a
  `finally`, and make the clean-up sweep what an aborted earlier run left. Grafana only draws panels in
  view, so keep the dashboard test's viewport tall enough for the whole dashboard.
- A new panel, view or page is not done until a test runs its SQL or renders it with TeslaMate
  present, absent, and held back.
- A test that inserts into a shared table must undo it in `finally`, or in the `admin` fixture. One
  leftover row fails other tests far from the cause.

## Delegating to sub-agents

- Brief each agent with the files to read first, the rules it must follow (this file), the exact checks
  to run and report, and the branch and pull request base to use. Self-contained items go to cheaper
  models. Anything the rest is built on (a schema or interface change) is done by the main session.
- An agent's report is a claim: review the diff of anything the rest builds on.
- Screenshots in the docs come from `scripts/dev.sh demo screenshots`, never the dev stack. Agents can
  only add the markers, so list the outstanding ones in the final report.
