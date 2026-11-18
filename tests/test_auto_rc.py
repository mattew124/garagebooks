"""scripts/auto_rc.py: which release candidate the TeslaMate compatibility workflow may tag by itself, and
the CHANGELOG edit that goes with it. It only acts when the next tag is unambiguous; otherwise it leaves
the pull request for the owner."""
import importlib.util
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("auto_rc", ROOT / "scripts" / "auto_rc.py")
rc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rc)

COMPAT = "**TeslaMate 4.5.0.** Tested with TeslaMate 4.5.0 (it was 4.4.0). Nothing to do on upgrade.\n\n"
OTHER = "- **A feature.** Something else.\n\n"


def log(body):
    return f"# Changelog\n\nintro\n\n## Unreleased\n\n{body}## 0.4.0-rc1 - 2026-11-15\n\nold\n"


def test_the_next_candidate_follows_the_latest_candidate_of_an_unreleased_version():
    assert rc.next_tag(["v0.3.0-rc1", "v0.4.0-rc1", "v0.2.0"], OTHER + COMPAT) == "v0.4.0-rc2"


def test_candidates_sort_by_number_not_text():
    assert rc.next_tag(["v0.4.0-rc9", "v0.4.0-rc10"], OTHER) == "v0.4.0-rc11"


def test_after_a_stable_release_only_a_compat_entry_may_be_released_as_a_patch():
    assert rc.next_tag(["v0.4.0-rc1", "v0.4.0"], COMPAT) == "v0.4.1-rc1"
    assert rc.next_tag(["v0.4.0-rc1", "v0.4.0"], COMPAT + OTHER) is None      # features need a MINOR: the owner decides


def test_a_stable_release_outranks_its_candidates():
    assert rc.next_tag(["v0.4.0", "v0.4.0-rc3"], COMPAT) == "v0.4.1-rc1"


def test_with_no_tags_or_nothing_unreleased_there_is_nothing_to_do():
    assert rc.next_tag([], COMPAT) is None
    assert rc.next_tag(["v0.4.0-rc1"], "") is None


def test_the_changelog_entry_is_renamed_and_an_empty_unreleased_stays_on_top():
    out = rc.rename_unreleased(log(OTHER + COMPAT), "v0.4.0-rc2", date(2026, 11, 20))
    assert out.index("## Unreleased") < out.index("## 0.4.0-rc2 - 2026-11-20") < out.index("## 0.4.0-rc1")
    assert out.count("## Unreleased") == 1
    assert out.split("## 0.4.0-rc2 - 2026-11-20")[1].startswith("\n\n- **A feature.**")


def test_unreleased_body_is_read_between_its_heading_and_the_next():
    assert rc.unreleased_body(log(OTHER)) == OTHER.strip() + "\n"
