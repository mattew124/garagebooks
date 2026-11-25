"""scripts/teslamate-compat.sh: its exit codes and what --record changes, with docker and the test run stood in
for (a stub `docker` that reports a chosen newest migration, a `.venv/bin/python` that answers `-m pytest` with a
chosen status), in a scratch copy of the files it edits. The workflow relies on these: 0 = nothing to do,
1 = failed, 2 = passes and is newer than recorded (also after --record, so one run can test and record)."""
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RECORDED, NEWER = 20261014120000, 20261102130000


def make_tree(tmp_path):
    tree = tmp_path / "tree"
    (tree / "scripts").mkdir(parents=True)
    (tree / "garagebooks").mkdir()
    (tree / ".venv" / "bin").mkdir(parents=True)
    (tree / "garagebooks" / "__init__.py").write_text("")
    (tree / "garagebooks" / "compat.py").write_text(f'TESTED_TESLAMATE = "4.4.0"\nTESTED_MIGRATION = {RECORDED}\n')
    for name in ("teslamate-compat.sh", "auto_rc.py"):
        shutil.copy(ROOT / "scripts" / name, tree / "scripts" / name)
    (tree / "README.md").write_text("Tested with TeslaMate 4.4.0. More text.\n")
    (tree / "CHANGELOG.md").write_text("# Changelog\n\n## Unreleased\n\n## 0.4.0-rc3 - 2026-11-19\n\nold\n")
    py = tree / ".venv" / "bin" / "python"
    py.write_text(f'#!/bin/sh\nif [ "$1" = -m ] && [ "$2" = pytest ]; then echo "pytest ran" >> "$STUB_LOG"; exit "${{FAKE_PYTEST:-0}}"; fi\nexec {sys.executable} "$@"\n')
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    docker = stubs / "docker"
    docker.write_text('#!/bin/sh\necho "docker $*" >> "$STUB_LOG"\ncase "$*" in *psql*) echo "$FAKE_NEWEST";; esac\nexit 0\n')
    for f in (py, docker, tree / "scripts" / "teslamate-compat.sh"):
        f.chmod(f.stat().st_mode | stat.S_IEXEC)
    return tree, stubs


def run(tmp_path, *args, newest=NEWER, pytest_status=0):
    tree, stubs = make_tree(tmp_path)
    log = tmp_path / "log"
    log.write_text("")
    env = {**os.environ, "PATH": f"{stubs}:{os.environ['PATH']}", "STUB_LOG": str(log),
           "FAKE_NEWEST": str(newest), "FAKE_PYTEST": str(pytest_status)}
    result = subprocess.run([str(tree / "scripts" / "teslamate-compat.sh"), *args], cwd=tree, env=env,
                            capture_output=True, text=True, check=False)
    return result, tree, log.read_text()


def compat(tree):
    return (tree / "garagebooks" / "compat.py").read_text()


def test_a_newer_release_that_passes_exits_2_and_changes_nothing(tmp_path):
    result, tree, _ = run(tmp_path, "4.5.0")
    assert result.returncode == 2, result.stdout + result.stderr
    assert 'TESTED_TESLAMATE = "4.4.0"' in compat(tree) and "4.5.0" not in (tree / "README.md").read_text()


def test_record_keeps_exit_2_so_one_run_can_test_and_record(tmp_path):
    result, tree, log = run(tmp_path, "4.5.0", "--record")
    assert result.returncode == 2, result.stdout + result.stderr
    assert log.count("pytest ran") == 1              # the suite runs once
    assert 'TESTED_TESLAMATE = "4.5.0"' in compat(tree) and f"TESTED_MIGRATION = {NEWER}" in compat(tree)
    assert (tree / "README.md").read_text().startswith("Tested with TeslaMate 4.5.0.")
    assert "**TeslaMate 4.5.0.**" in (tree / "CHANGELOG.md").read_text()


def test_a_failing_suite_exits_1_and_records_nothing(tmp_path):
    result, tree, _ = run(tmp_path, "4.5.0", "--record", pytest_status=1)
    assert result.returncode == 1
    assert 'TESTED_TESLAMATE = "4.4.0"' in compat(tree) and "4.5.0" not in (tree / "CHANGELOG.md").read_text()


def test_a_release_with_the_recorded_migration_is_recorded_and_exits_0(tmp_path):
    result, tree, _ = run(tmp_path, "4.4.1", "--record", newest=RECORDED)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'TESTED_TESLAMATE = "4.4.1"' in compat(tree)


def test_if_new_does_nothing_for_the_recorded_release(tmp_path):
    result, _, log = run(tmp_path, "4.4.0", "--if-new", "--record")
    assert result.returncode == 0 and "nothing new" in result.stdout
    assert log == ""                                  # no docker, no tests


def test_the_throwaway_database_is_on_disk_and_removed_with_its_volume(tmp_path):
    """Not tmpfs, which holds the database in RAM. The postgres image makes an anonymous volume, so every removal needs -v."""
    _, _, log = run(tmp_path, "4.5.0")
    calls = log.splitlines()
    assert not any("--tmpfs" in c for c in calls)
    removals = [c.split() for c in calls if c.startswith("docker rm ")]
    assert removals and all("-fv" in c for c in removals), removals


def test_the_test_suites_database_is_on_disk_and_removed_with_its_volume():
    source = (ROOT / "tests" / "conftest.py").read_text()
    assert '"--tmpfs"' not in source
    assert '"docker", "rm", "-fv", name' in source
