#!/usr/bin/env python3
"""Which release candidate the TeslaMate compatibility workflow may tag by itself.

  scripts/auto_rc.py record VERSION PREVIOUS   adds the TeslaMate compatibility entry to Unreleased.
  scripts/auto_rc.py prepare   renames CHANGELOG.md's Unreleased entry for the next candidate, and prints
                               its tag (v0.4.0-rc2). Prints nothing, and changes nothing, when the next tag
                               isn't unambiguous: the owner releases then.

The rule (RELEASING.md): fixes found while testing a candidate go in the next candidate of the same
version, so after a candidate it is the next rcN. A PATCH only follows a stable release, and only a
compatibility entry may go out as one: anything else in Unreleased is a feature or a fix the owner
sizes, so the pull request is left for them."""

import re
import subprocess
import sys
from datetime import date
from pathlib import Path

CHANGELOG = Path(__file__).resolve().parent.parent / "CHANGELOG.md"
TAG = re.compile(r"^v(\d+)\.(\d+)\.(\d+)(?:-rc(\d+))?$")
COMPAT_ENTRY = "**TeslaMate "     # what `teslamate-compat.sh --record` writes


def unreleased_body(text):
    match = re.search(r"^## Unreleased\n(.*?)(?=^## )", text, re.M | re.S)
    body = match.group(1).strip() if match else ""
    return body + "\n" if body else ""


def next_tag(tags, unreleased):
    """The tag to release, or None when it needs the owner's decision."""
    if not unreleased.strip():
        return None
    parsed = []
    for tag in tags:
        m = TAG.match(tag)
        if m:       # a stable release sorts after its own candidates
            parsed.append(((int(m[1]), int(m[2]), int(m[3])), float("inf") if m[4] is None else int(m[4])))
    if not parsed:
        return None
    (major, minor, patch), rc = max(parsed)
    if rc != float("inf"):
        return f"v{major}.{minor}.{patch}-rc{rc + 1}"
    paragraphs = [p for p in re.split(r"\n\s*\n", unreleased.strip()) if p.strip()]
    if all(p.startswith(COMPAT_ENTRY) for p in paragraphs):
        return f"v{major}.{minor}.{patch + 1}-rc1"
    return None


def add_compat_entry(text, version, previous):
    """The entry `teslamate-compat.sh --record` adds at the top of Unreleased (making that heading when the
    last release took it). Once only."""
    if f"**TeslaMate {version}.**" in text:
        return text
    entry = (f"{COMPAT_ENTRY}{version}.** Tested with TeslaMate {version} (it was {previous}). Nothing to do on upgrade: "
             f"pull the new image. A TeslaMate database up to that release no longer has its figures held back.\n\n")
    if "## Unreleased\n" not in text:
        text = re.sub(r"^## ", "## Unreleased\n\n## ", text, count=1, flags=re.M)
    return re.sub(r"^## Unreleased\n\n?", lambda m: m.group(0).rstrip("\n") + "\n\n" + entry, text, count=1, flags=re.M)


def rename_unreleased(text, tag, today):
    """Unreleased becomes the tag's entry, and an empty Unreleased stays above it."""
    return text.replace("## Unreleased\n", f"## Unreleased\n\n## {tag.removeprefix('v')} - {today.isoformat()}\n", 1)


def main(argv):
    if len(argv) == 4 and argv[1] == "record":
        CHANGELOG.write_text(add_compat_entry(CHANGELOG.read_text(), argv[2], argv[3]))
        return
    if argv[1:] != ["prepare"]:
        sys.exit(__doc__)
    tags = subprocess.run(["git", "tag"], capture_output=True, text=True, check=True).stdout.split()
    text = CHANGELOG.read_text()
    tag = next_tag(tags, unreleased_body(text))
    if tag:
        CHANGELOG.write_text(rename_unreleased(text, tag, date.today()))
        print(tag)


if __name__ == "__main__":
    main(sys.argv)
