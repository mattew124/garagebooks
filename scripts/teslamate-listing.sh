#!/usr/bin/env bash
# =============================================================================
# teslamate-listing.sh — our entry on TeslaMate's "Projects using TeslaMate" page.
#
#   scripts/teslamate-listing.sh check     how TeslaMate's page differs from
#                                          .github/teslamate-listing.md (exit 1 if it does)
#   scripts/teslamate-listing.sh preview   everything `propose` would send: the page change,
#                                          the pull request's title and body. Sends nothing.
#   scripts/teslamate-listing.sh propose   show the preview, then, only after you type yes,
#                                          open (or refresh) the pull request on teslamate-org/teslamate
#
# `propose` publishes under your GitHub account: it forks TeslaMate, pushes one branch to the
# fork and opens a pull request. So it runs only when you ask for it, at a terminal, after
# you've read what it will send: never from CI, a scheduled task or an agent session (it refuses
# without a terminal or with CI set). The entry is .github/teslamate-listing.md and the pull
# request's text .github/teslamate-listing-pr.md: review changes to them in a pull request here.
# =============================================================================
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

UPSTREAM=teslamate-org/teslamate
PAGE=website/docs/projects.md
BRANCH=update-garagebooks-listing
LISTING=(python3 -I scripts/teslamate_listing.py)

if [ "${1:-}" = propose ] && { [ -n "${CI:-}" ] || ! { [ -t 0 ] && [ -t 1 ]; }; }; then
    echo "listing: 'propose' only runs for a person at a terminal, after reviewing what it sends." >&2
    echo "listing: run 'scripts/teslamate-listing.sh preview' to see it without sending anything." >&2
    exit 2
fi

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

fetch_page() { gh api "repos/$UPSTREAM/contents/$PAGE" --jq .content | base64 -d > "$1"; }

# The page as it is, and as it would be with our entry: returns 1 when they differ.
page_diff() {
    fetch_page "$work/upstream.md"
    "${LISTING[@]}" merge "$work/upstream.md" > "$work/new.md"
    diff -u --label "TeslaMate's page" --label "with our entry" "$work/upstream.md" "$work/new.md"
}

preview() {
    local me
    me="$(gh api user --jq .login)"
    echo "== The change to $UPSTREAM/$PAGE"
    if page_diff; then
        echo "listing: TeslaMate's page already matches .github/teslamate-listing.md: nothing to propose"
        return 1
    fi
    echo
    echo "== The pull request, opened by $me from $me:$BRANCH"
    echo "Title: $("${LISTING[@]}" title)"
    echo
    "${LISTING[@]}" body
    existing="$(gh pr list -R "$UPSTREAM" --head "$me:$BRANCH" --state open --json number --jq '.[0].number // empty')"
    echo
    if [ -n "$existing" ]; then
        echo "== It would update the open pull request $UPSTREAM#$existing"
    else
        echo "== It would open a new pull request"
    fi
}

case "${1:-}" in
    check)
        if page_diff; then
            echo "listing: TeslaMate's page already matches .github/teslamate-listing.md"
        else
            echo "listing: TeslaMate's page differs (diff above). 'preview' shows what proposing it would send." >&2
            exit 1
        fi ;;
    preview)
        preview || true ;;
    propose)
        preview || exit 0
        echo
        read -r -p "Send this to $UPSTREAM under your GitHub account? Type yes to send: " answer
        if [ "$answer" = yes ]; then :; else
            echo "listing: nothing sent"
            exit 0
        fi
        me="$(gh api user --jq .login)"
        gh repo fork "$UPSTREAM" --clone=false >/dev/null 2>&1 || true   # already forked is fine
        gh repo sync "$me/teslamate" --source "$UPSTREAM" >/dev/null
        # git gets its password from gh (GH_TOKEN, or the account gh is logged in to)
        auth=(-c 'credential.helper=!gh auth git-credential')   # for these commands only
        git "${auth[@]}" clone -q --depth 1 "https://github.com/$me/teslamate.git" "$work/fork"
        "${LISTING[@]}" merge "$work/fork/$PAGE" > "$work/new.md"
        cp "$work/new.md" "$work/fork/$PAGE"
        title="$("${LISTING[@]}" title)"
        "${LISTING[@]}" body > "$work/body.md"
        cd "$work/fork"
        git checkout -q -B "$BRANCH"
        git -c "user.name=$me" -c "user.email=$me@users.noreply.github.com" commit -q -am "$title"
        git "${auth[@]}" push -q --force origin "$BRANCH"
        existing="$(gh pr list -R "$UPSTREAM" --head "$me:$BRANCH" --state open --json number --jq '.[0].number // empty')"
        if [ -n "$existing" ]; then
            gh pr edit "$existing" -R "$UPSTREAM" --title "$title" --body-file "$work/body.md" >/dev/null
            echo "listing: updated the open pull request $UPSTREAM#$existing"
        else
            gh pr create -R "$UPSTREAM" --head "$me:$BRANCH" --base main --title "$title" --body-file "$work/body.md"
        fi ;;
    *) sed -n '2,16p' "$0"; exit 2 ;;
esac
