"""scripts/dev.sh setup: creates the venv in a new checkout, and installs the requirements again when they have
changed since (a merge of main can add a dependency, and a venv from before it fails with ModuleNotFoundError).
python3 is a stand-in that makes a venv whose pip only records what it was asked to install."""
import os
import shutil
import stat
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def make_checkout(tmp_path):
    tree = tmp_path / "tree"
    (tree / "scripts").mkdir(parents=True)
    shutil.copy(ROOT / "scripts" / "dev.sh", tree / "scripts" / "dev.sh")
    (tree / "requirements.txt").write_text("flask==1\n")
    (tree / "requirements-dev.txt").write_text("-r requirements.txt\npytest==1\n")
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tree, check=True)
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    python3 = stubs / "python3"
    # `python3 -m venv .venv`: a venv whose python exists and whose pip appends its arguments to $STUB_LOG.
    python3.write_text('#!/bin/sh\nmkdir -p "$3/bin"\nprintf "#!/bin/sh\\n" > "$3/bin/python"\n'
                       'printf "#!/bin/sh\\necho \\"pip \\$*\\" >> \\"$STUB_LOG\\"\\n" > "$3/bin/pip"\n'
                       'chmod +x "$3/bin/python" "$3/bin/pip"\n')
    python3.chmod(python3.stat().st_mode | stat.S_IEXEC)
    return tree, stubs


def setup(tree, stubs, log):
    env = {**os.environ, "PATH": f"{stubs}:{os.environ['PATH']}", "STUB_LOG": str(log)}
    for key in ("CLAUDE_LOCAL_SOURCE", "PUBLIC_HOST"):
        env.pop(key, None)
    result = subprocess.run(["bash", "scripts/dev.sh", "setup"], cwd=tree, env=env, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    return log.read_text() if log.exists() else ""


def test_a_new_checkout_gets_its_venv_and_the_requirements(tmp_path):
    tree, stubs = make_checkout(tmp_path)
    log = tmp_path / "log"
    assert setup(tree, stubs, log).count("pip install -q -r requirements-dev.txt") == 1


def test_a_second_setup_installs_nothing_while_the_requirements_are_unchanged(tmp_path):
    tree, stubs = make_checkout(tmp_path)
    log = tmp_path / "log"
    setup(tree, stubs, log)
    assert setup(tree, stubs, log).count("pip install") == 1


def test_changed_requirements_are_installed_again(tmp_path):
    tree, stubs = make_checkout(tmp_path)
    log = tmp_path / "log"
    setup(tree, stubs, log)
    time.sleep(1.1)
    (tree / "requirements.txt").write_text("flask==1\nPyJWT==2\n")      # a merge added a dependency
    assert setup(tree, stubs, log).count("pip install") == 2
    assert setup(tree, stubs, log).count("pip install") == 2            # and then it is up to date again


def test_a_venv_from_before_the_stamp_existed_is_installed_into_once(tmp_path):
    tree, stubs = make_checkout(tmp_path)
    log = tmp_path / "log"
    setup(tree, stubs, log)
    (tree / ".venv" / ".requirements-installed").unlink()
    assert setup(tree, stubs, log).count("pip install") == 2
