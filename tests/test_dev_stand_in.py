"""The browser tests submit claims, so they must only ever run against the stand-in connector.

Checking the settings of the `test-browser` command is not enough: another command (`onedrive-link` restarts
the app) can leave the running app pointed at a real connector. What counts is what the running app uses.
docker is a stand-in that reports one app container and records what was run."""
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
STAND_IN = "http://dev-claims-connector:8765"


def run_browser_tests(tmp_path, connector):
    tree = tmp_path / "tree"
    (tree / "scripts").mkdir(parents=True)
    shutil.copy(ROOT / "scripts" / "dev.sh", tree / "scripts" / "dev.sh")
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tree, check=True)
    stubs, log = tmp_path / "stubs", tmp_path / "log"
    stubs.mkdir()
    docker = stubs / "docker"
    running = "" if connector is None else "app1"
    docker.write_text(f'''#!/bin/sh
echo "$*" >> "{log}"
case "$*" in
  *" ps -q garagebooks-dev"*) echo "{running}" ;;
  inspect*) printf 'APP_TZ=UTC\\nCLAIMS_CONNECTOR_URL={connector}\\n' ;;
esac
''')
    docker.chmod(docker.stat().st_mode | stat.S_IEXEC)
    env = {k: v for k, v in os.environ.items() if not k.startswith("DEV_") and k not in ("PUBLIC_HOST", "CLAUDE_LOCAL_SOURCE")}
    env.update(PATH=f"{stubs}:{os.environ['PATH']}", STAND_IN="1")
    result = subprocess.run(["bash", "scripts/dev.sh", "test-browser"], cwd=tree, env=env, capture_output=True,
                            text=True, check=False)
    ran = [line for line in (log.read_text() if log.exists() else "").splitlines() if line.startswith("run ")]
    return result, ran


@pytest.mark.parametrize("connector", ["http://claims.example.test:8765", "", None])
def test_they_refuse_unless_the_running_app_uses_the_stand_in(tmp_path, connector):
    result, ran = run_browser_tests(tmp_path, connector)
    assert result.returncode != 0 and ran == [], result.stdout + result.stderr
    assert "stand-in" in result.stderr


def test_they_run_when_the_running_app_uses_the_stand_in(tmp_path):
    result, ran = run_browser_tests(tmp_path, STAND_IN)
    assert result.returncode == 0, result.stdout + result.stderr
    assert len(ran) == 1
