"""Our entry on TeslaMate's "Projects using TeslaMate" page must keep describing the app, and the
pull request that proposes it is only ever opened by the owner, after reviewing it."""

import importlib.util
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("teslamate_listing", ROOT / "scripts" / "teslamate_listing.py")
listing = importlib.util.module_from_spec(spec)
spec.loader.exec_module(listing)
SCRIPT = ROOT / "scripts" / "teslamate-listing.sh"
DISCLAIMER = ("This project is an unofficial community tool and is not affiliated with, endorsed by, or "
              "supported by the official TeslaMate project.")

UPSTREAM = """---
title: Projects using TeslaMate
---

Intro.

## [Other one](https://example.test/a)

Other text.

LINK: [a](https://example.test/a)

## [Last one](https://example.test/b)

Last text.
"""


def test_the_entry_was_reviewed_against_the_current_feature_list():
    recorded, _ = listing.entry_parts()
    assert recorded == listing.feature_hash(), (
        "The feature list in README.md changed. Check .github/teslamate-listing.md still describes the "
        "app (storage options, dashboard, how it reads TeslaMate), edit it if not, then set its first "
        f"line to <!-- readme-features: {listing.feature_hash()} -->. `scripts/teslamate-listing.sh preview` "
        "shows what proposing it to TeslaMate would send.")


def test_a_changed_feature_changes_the_hash():
    readme = (ROOT / "README.md").read_text()
    assert listing.feature_hash(readme) != listing.feature_hash(readme.replace("OneDrive", "Local"))
    assert listing.feature_hash(readme) == listing.feature_hash(readme.replace("\n  ", "\n      "))   # indentation is not content


def test_the_entry_is_one_section_that_fits_the_page():
    _, entry = listing.entry_parts()
    assert entry.startswith("## [GarageBooks](https://github.com/mattew124/garagebooks)")
    assert entry.count("\n## ") == 0 and "LINK: [github.com/mattew124/garagebooks]" in entry


def test_the_entry_follows_teslamates_trademark_policy():
    """The name carries no TeslaMate mark, and the policy's disclaimer is there word for word."""
    _, entry = listing.entry_parts()
    heading = entry.splitlines()[0]
    assert "teslamate" not in heading.lower()
    assert DISCLAIMER in " ".join(entry.split())


def test_the_entry_says_what_the_app_does_and_how_it_reads_teslamate():
    _, entry = listing.entry_parts()
    for word in ("expense", "invoice", "OneDrive", "Grafana", "directly", "MQTT"):
        assert word.lower() in entry.lower(), word
    assert (ROOT / "garagebooks" / "onedrive.py").exists()   # the OneDrive claim is still true


def test_the_pull_request_text_is_kept_here_for_review():
    title, body = listing.pull_request()
    assert title and "TeslaMate" not in title.replace("projects.md", "")
    assert "GarageBooks" in title and "#5965" in body and DISCLAIMER in " ".join(body.split())


def test_merge_appends_when_the_page_has_no_entry():
    _, entry = listing.entry_parts()
    merged = listing.merge(UPSTREAM, entry)
    assert merged.startswith(UPSTREAM.rstrip("\n")) and merged.endswith(entry.strip() + "\n")
    assert merged.count("## [GarageBooks](https://github.com/mattew124") == 1


def test_merge_replaces_only_our_section_and_is_idempotent():
    _, entry = listing.entry_parts()
    middle = UPSTREAM.replace("## [Last one]", entry.strip() + "\n\n## [Last one]")   # our entry in the middle
    new = entry.replace("Expense tracking", "Expense tracking and local folders")
    replaced = listing.merge(middle, new)
    assert "and local folders" in replaced and replaced.count("## [GarageBooks]") == 1
    assert "Other text." in replaced and "Last text." in replaced and "## [Last one]" in replaced
    assert listing.merge(replaced, new) == replaced


def test_merge_replaces_an_entry_under_the_old_name():
    old = "## [TeslaMate Expenses](https://github.com/mattew124/teslamate-expenses)\n\nOld text.\n"
    _, entry = listing.entry_parts()
    merged = listing.merge(UPSTREAM.replace("## [Last one]", old + "\n## [Last one]"), entry)
    assert "Old text." not in merged and "TeslaMate Expenses" not in merged
    assert merged.count("## [GarageBooks]") == 1 and "## [Last one]" in merged


def test_propose_only_runs_for_a_person_at_a_terminal():
    """No CI job, scheduled task or agent session can open the pull request: it needs a terminal and a typed yes."""
    text = SCRIPT.read_text()
    assert "[ -t 0 ] && [ -t 1 ]" in text and '"${CI:-}"' in text and '= yes ]' in text
    env = {**os.environ, "CI": ""}
    out = subprocess.run([str(SCRIPT), "propose"], cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
                         capture_output=True, text=True)
    assert out.returncode == 2 and "terminal" in out.stderr


def test_no_workflow_runs_the_listing_script():
    for workflow in (ROOT / ".github" / "workflows").glob("*.yml"):
        text = workflow.read_text()
        assert "teslamate-listing" not in text and "teslamate_listing" not in text, workflow.name
