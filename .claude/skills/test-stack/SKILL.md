---
name: test-stack
description: Bring up the main garagebooks-dev stack on the latest develop, check it, and report what there is to test. Use when asked to create, start, rebuild or update the garagebooks-dev (or main dev) stack, or to get the latest changes ready for testing. Not for a worktree's own stack.
---

Get the main dev stack (`/garagebooks-dev/`, from the main checkout) running the latest `develop`, ready for a
person to test. This request covers fast-forwarding the main checkout and running `scripts/dev.sh` there, even
when the session is in a worktree. `CLAUDE.md` and `CLAUDE.local.md` still apply.

## 1. Update the main checkout

```bash
main="$(dirname "$(git rev-parse --path-format=absolute --git-common-dir)")"
git -C "$main" status -sb
git -C "$main" fetch origin develop
```

It must be on `develop`, with no changes to tracked files (untracked files are fine). Then:

```bash
git -C "$main" pull --ff-only origin develop
```

If it's on another branch, has changes, or can't fast-forward, stop and say why. Don't stash, reset or switch
branches: that work may be another session's.

## 2. Find what isn't in it

```bash
gh pr list --state open --json number,title,isDraft,baseRefName,headRefName,mergeable
git -C "$main" worktree list
```

"All the changes" means what has merged into `develop`. Merge an open pull request only under the project's
merge rules. Leave drafts, conflicting ones and those another session is working on (a locked worktree), and
list each in the report with its reason. Dependency updates and pull requests into `main` aren't features to test.

## 3. Check the host

Look at `free -h`, `df -h` and `scripts/dev.sh list`. Other stacks belong to other sessions: don't take them
down. If memory or disk is too short to build, say which stacks are running and stop.

## 4. Start it

Run these from `$main`:

```bash
scripts/dev.sh setup
scripts/dev.sh up
scripts/dev.sh refresh
```

- `up` builds `develop` and keeps the test database. `refresh` re-copies TeslaMate's data (read-only) and keeps
  the test expenses.
- Never `reset` or `destroy` here unless asked: both wipe the test data that testing has built up.
- Keep `dev.env`'s settings. Don't set `STAND_IN=1` unless asked to run the browser tests, which are not part
  of this task. If you do run them, run `up` again without it afterwards.
- If `up` refuses (an unmounted invoice share, an unreadable claims key), report its message. Don't work around it.
- With a real claims connector, only preview and sync. Never submit a claim.

## 5. Check it

OneDrive, if the pull requests being tested need it:

```bash
scripts/dev.sh psql -At -c "SELECT count(*) FROM garagebooks.onedrive_account WHERE refresh_token IS NOT NULL"
scripts/dev.sh psql -At -c "SELECT car_id, backend, folder_path FROM garagebooks.car_folder"
```

If it's signed in, leave it. `scripts/dev.sh onedrive-link` **removes every car's folder choice**, so run it only
when the stack isn't signed in and has no folders chosen; otherwise say so in the report.

Then, from `$main`:

```bash
app="$(docker ps -q --filter label=com.docker.compose.project=garagebooks-dev --filter label=com.docker.compose.service=garagebooks-dev)"
docker exec -i "$app" python - < .claude/skills/test-stack/smoke.py
docker logs --since 10m "$app" 2>&1 | grep -iE 'error|traceback'
ip route | grep 192.168
```

Every page must load, and the log search should find nothing. `ip route` should show no Docker bridge in
`192.168.0.0/16` (see "Working in a worktree" in `CLAUDE.md`). Request each URL `up` printed with `curl`: behind
forward-auth, a redirect to the sign-in page is the expected answer, and shows the route works, not the page.

## 6. Report

- The URLs, as links, and the commit the stack is running.
- What's new to test: the pull requests merged since the commit the main checkout was on before, in a few
  plain lines grouped by area, from their titles and the top of `CHANGELOG.md`.
- What isn't included, and why.
- The state of the test data and OneDrive, whether the claims connector is real or the stand-in, and anything
  that failed or was skipped.

The stack stays up. `scripts/dev.sh down` in the main checkout stops it and keeps the test data.
