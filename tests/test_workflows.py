"""Workflows that act on what is released. The repository's default branch is develop, where pull requests
merge; a scheduled run checks out the default branch unless told otherwise, so these say main."""

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


def checkouts(name):
    jobs = yaml.safe_load((ROOT / ".github" / "workflows" / name).read_text())["jobs"]
    return [s for job in jobs.values() for s in job["steps"] if s.get("uses", "").startswith("actions/checkout@")]


def test_the_teslamate_check_records_and_tags_from_main_not_develop():
    """It branches from what it checked out, opens a pull request into main and can tag a candidate: from
    develop that would carry all its unreleased work into a release."""
    (step,) = checkouts("teslamate-compat.yml")
    assert step["with"]["ref"] == "main"


def test_the_weekly_security_scan_looks_at_the_released_code():
    (step,) = checkouts("security.yml")
    assert "schedule" in step["with"]["ref"] and "main" in step["with"]["ref"]
