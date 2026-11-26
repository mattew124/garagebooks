#!/usr/bin/env bash
# Sweep what merged work leaves behind: worktrees, local and remote branches, and dangling anonymous
# Docker volumes. Dry run unless --apply is given; nothing changes on a dry run.
#
#   scripts/sweep.sh            report what would be removed
#   scripts/sweep.sh --apply    remove it
#
# A worktree goes only when its branch is merged into the base, it has no uncommitted or untracked files,
# and its last activity (its HEAD reflog) is older than SWEEP_MIN_AGE_DAYS (default 3). The age check keeps a
# session that has just made its worktree and not committed yet. A lock is cleared only for such a worktree,
# so a session still running in its own checkout is never removed. The current checkout and the main
# checkout are never touched. A remote branch goes only when it is merged, older than the same limit, and has
# no open pull request (this needs gh; without gh remote branches are skipped). Docker volumes go only when
# they are dangling (no container uses them) and named by a 64-character hash, so other projects' named volumes
# are never touched.
#
# Environment: SWEEP_BASE (default origin/develop), SWEEP_MIN_AGE_DAYS, SWEEP_NOW (epoch seconds, for tests),
# SWEEP_DOCKER=0 to skip Docker.
set -euo pipefail

apply=0
case "${1:-}" in
    --apply) apply=1 ;;
    "") ;;
    *) echo "usage: scripts/sweep.sh [--apply]" >&2; exit 2 ;;
esac

base="${SWEEP_BASE:-origin/develop}"
min_days="${SWEEP_MIN_AGE_DAYS:-3}"
now="${SWEEP_NOW:-$(date +%s)}"
min_age=$((min_days * 86400))
current="$(git rev-parse --show-toplevel)"
main="$(dirname "$(git rev-parse --path-format=absolute --git-common-dir)")"

say() { printf '%s\n' "$*"; }

if [ "$apply" = 1 ]; then mode="applied"; else mode="dry run, nothing changed (rerun with --apply)"; fi
say "sweep: $mode"
git fetch --prune -q origin 2>/dev/null || say "sweep: fetch failed, using the last fetched state"

# ---- worktrees ---------------------------------------------------------------------------------------
handle_worktree() {
    local path="$1" branch="$2" head="$3" locked="$4" ref last idle_days
    if [ "$path" = "$current" ] || [ "$path" = "$main" ]; then
        say "keep worktree $path: this checkout"
        return
    fi
    if [ ! -d "$path" ]; then
        say "keep worktree $path: folder missing (git worktree prune will drop it)"
        return
    fi
    ref="${branch:-$head}"
    if ! git merge-base --is-ancestor "$ref" "$base"; then
        say "keep worktree $path: $ref has commits not on $base"
        return
    fi
    if [ -n "$(git -C "$path" status --porcelain)" ]; then
        say "keep worktree $path: uncommitted or untracked changes"
        return
    fi
    last="$(git -C "$path" reflog -1 --format=%ct HEAD 2>/dev/null || true)"
    if [ -z "$last" ]; then
        say "keep worktree $path: no activity record"
        return
    fi
    if [ $((now - last)) -lt "$min_age" ]; then
        say "keep worktree $path: active in the last $min_days days"
        return
    fi
    idle_days=$(((now - last) / 86400))
    if [ "$apply" = 1 ]; then
        if [ "$locked" = 1 ]; then git worktree unlock "$path"; fi
        if git worktree remove "$path"; then
            say "deleted worktree $path (merged, clean, idle ${idle_days}d)"
        else
            say "keep worktree $path: git refused to remove it"
        fi
    else
        say "would delete worktree $path (merged, clean, idle ${idle_days}d)"
    fi
}

worktrees="$(git worktree list --porcelain)"
path="" branch="" head="" locked=0
while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in
        "worktree "*) path="${line#worktree }" ;;
        "HEAD "*) head="${line#HEAD }" ;;
        "branch refs/heads/"*) branch="${line#branch refs/heads/}" ;;
        locked*) locked=1 ;;
        "")
            if [ -n "$path" ]; then handle_worktree "$path" "$branch" "$head" "$locked"; fi
            path="" branch="" head="" locked=0 ;;
    esac
done <<< "$worktrees"$'\n'

# Branches still checked out after the worktree pass.
checked_out="$(git worktree list --porcelain | sed -n 's|^branch refs/heads/||p')"

# ---- local branches ----------------------------------------------------------------------------------
while IFS= read -r b; do
    [ -n "$b" ] || continue
    case "$b" in develop|main) continue ;; esac
    if printf '%s\n' "$checked_out" | grep -qxF "$b"; then
        say "keep branch $b: checked out in a worktree"
        continue
    fi
    if ! git merge-base --is-ancestor "$b" "$base"; then
        say "keep branch $b: commits not on $base"
        continue
    fi
    if [ "$apply" = 1 ]; then
        git branch -D "$b" >/dev/null
        say "deleted branch $b (merged into $base)"
    else
        say "would delete branch $b (merged into $base)"
    fi
done < <(git for-each-ref --format='%(refname:short)' refs/heads)

# ---- remote branches ---------------------------------------------------------------------------------
if ! command -v gh >/dev/null 2>&1; then
    say "skip remote branches: gh not found, so open pull requests can't be checked"
else
    while IFS= read -r r; do
        b="${r#origin/}"
        # refs/remotes/origin/HEAD shortens to "origin": it's a pointer, not a branch.
        case "$b" in origin|HEAD|develop|main|"") continue ;; esac
        if ! git merge-base --is-ancestor "$r" "$base"; then
            say "keep remote branch $b: commits not on $base"
            continue
        fi
        tip="$(git log -1 --format=%ct "$r")"
        if [ $((now - tip)) -lt "$min_age" ]; then
            say "keep remote branch $b: pushed in the last $min_days days"
            continue
        fi
        open="$(gh pr list --head "$b" --state open --json number --jq 'length' 2>/dev/null || echo 1)"
        if [ "$open" != 0 ]; then
            say "keep remote branch $b: has an open pull request"
            continue
        fi
        if [ "$apply" = 1 ]; then
            git push -q origin --delete "$b"
            say "deleted remote branch $b (merged into $base)"
        else
            say "would delete remote branch $b (merged into $base)"
        fi
    done < <(git for-each-ref --format='%(refname:short)' refs/remotes/origin)
fi

# ---- Docker: dangling anonymous volumes --------------------------------------------------------------
if [ "${SWEEP_DOCKER:-1}" != 0 ] && command -v docker >/dev/null 2>&1; then
    while IFS= read -r v; do
        [[ "$v" =~ ^[0-9a-f]{64}$ ]] || continue
        if [ "$apply" = 1 ]; then
            docker volume rm "$v" >/dev/null
            say "deleted volume $v (dangling, anonymous)"
        else
            say "would remove volume $v (dangling, anonymous)"
        fi
    done < <(docker volume ls -q -f dangling=true 2>/dev/null)
fi
