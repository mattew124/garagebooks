"""scripts/sweep.sh: what the weekly sweep removes, and what it only reports. Each test builds a scratch
repository with an origin, a stale merged branch, unmerged work, a locked worktree and so on, and runs the
script in it. Docker is a stub on PATH. Dry run is the default: nothing changes without --apply."""
import os
import shutil
import stat
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "sweep.sh"
GIT_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
    "GIT_CONFIG_NOSYSTEM": "1", "HOME": "/nonexistent",
}


def git(cwd, *args):
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, env={**os.environ, **GIT_ENV})
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


def commit(cwd, name):
    (cwd / name).write_text(name)
    git(cwd, "add", name)
    git(cwd, "commit", "-m", name)


def make_repo(tmp_path):
    origin = tmp_path / "origin.git"
    git(tmp_path, "init", "--bare", "-b", "develop", str(origin))
    repo = tmp_path / "repo"
    git(tmp_path, "clone", str(origin), str(repo))
    git(repo, "checkout", "-b", "develop")
    commit(repo, "base.txt")
    git(repo, "push", "-u", "origin", "develop")
    # Local branch with nothing of its own: merged into develop.
    git(repo, "branch", "stale-merged")
    # Local branch with a commit develop lacks: must be kept.
    git(repo, "checkout", "-q", "-b", "unique-work")
    commit(repo, "unique.txt")
    git(repo, "checkout", "-q", "develop")
    git(repo, "fetch", "-q", "origin")
    return repo


def add_worktree(repo, tmp_path, name, branch, locked=False):
    path = tmp_path / name
    git(repo, "worktree", "add", "-b", branch, str(path), "develop")
    if locked:
        git(repo, "worktree", "lock", "--reason", "claude agent test", str(path))
    return path


def sweep(repo, tmp_path, *args, now=None, docker_dangling="", docker=True):
    env = {**os.environ, **GIT_ENV, "SWEEP_MIN_AGE_DAYS": "3", "SWEEP_DOCKER": "1" if docker else "0"}
    if now is not None:
        env["SWEEP_NOW"] = str(now)
    stubs = tmp_path / "stubs"
    stubs.mkdir(exist_ok=True)
    log = tmp_path / "docker.log"
    stub = stubs / "docker"
    stub.write_text(
        "#!/bin/sh\n"
        'echo "docker $*" >> "$DOCKER_LOG"\n'
        'case "$*" in *"volume ls"*) printf "%s" "$DOCKER_DANGLING";; esac\n'
    )
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    env["PATH"] = f"{stubs}{os.pathsep}{env['PATH']}"
    env["DOCKER_LOG"] = str(log)
    env["DOCKER_DANGLING"] = docker_dangling
    r = subprocess.run(["bash", str(SCRIPT), *args], cwd=repo, capture_output=True, text=True, env=env)
    docker_calls = log.read_text() if log.exists() else ""
    return r, docker_calls


def branches(repo):
    return set(git(repo, "for-each-ref", "--format=%(refname:short)", "refs/heads").split())


def test_dry_run_deletes_nothing(tmp_path):
    repo = make_repo(tmp_path)
    r, _ = sweep(repo, tmp_path, docker=False)
    assert r.returncode == 0, r.stderr
    assert "stale-merged" in branches(repo)
    assert "would delete branch stale-merged" in r.stdout


def test_apply_deletes_merged_branch_and_keeps_unique_work(tmp_path):
    repo = make_repo(tmp_path)
    r, _ = sweep(repo, tmp_path, "--apply", docker=False)
    assert r.returncode == 0, r.stderr
    assert "stale-merged" not in branches(repo)
    assert "unique-work" in branches(repo)
    assert "develop" in branches(repo)


def test_remote_head_pointer_is_not_treated_as_a_branch(tmp_path):
    repo = make_repo(tmp_path)
    r, _ = sweep(repo, tmp_path, docker=False)
    assert r.returncode == 0, r.stderr
    assert "remote branch origin:" not in r.stdout
    assert "would delete remote branch origin" not in r.stdout


def test_recent_worktree_is_kept(tmp_path):
    repo = make_repo(tmp_path)
    wt = add_worktree(repo, tmp_path, "fresh", "fresh-branch", locked=True)
    r, _ = sweep(repo, tmp_path, "--apply", docker=False)
    assert r.returncode == 0, r.stderr
    assert wt.exists()
    assert "fresh-branch" in branches(repo)


def test_stale_clean_merged_worktree_is_unlocked_and_removed(tmp_path):
    repo = make_repo(tmp_path)
    wt = add_worktree(repo, tmp_path, "old", "old-branch", locked=True)
    later = int(time.time()) + 10 * 86400
    r, _ = sweep(repo, tmp_path, "--apply", now=later, docker=False)
    assert r.returncode == 0, r.stderr
    assert not wt.exists()
    assert "old-branch" not in branches(repo)


def test_dirty_worktree_is_kept_even_when_stale(tmp_path):
    repo = make_repo(tmp_path)
    wt = add_worktree(repo, tmp_path, "dirty", "dirty-branch", locked=False)
    (wt / "notes.txt").write_text("unsaved")
    later = int(time.time()) + 10 * 86400
    r, _ = sweep(repo, tmp_path, "--apply", now=later, docker=False)
    assert r.returncode == 0, r.stderr
    assert wt.exists()
    assert "uncommitted" in r.stdout


def test_worktree_with_unmerged_commits_is_kept(tmp_path):
    repo = make_repo(tmp_path)
    wt = add_worktree(repo, tmp_path, "ahead", "ahead-branch", locked=False)
    commit(wt, "ahead.txt")
    later = int(time.time()) + 10 * 86400
    r, _ = sweep(repo, tmp_path, "--apply", now=later, docker=False)
    assert r.returncode == 0, r.stderr
    assert wt.exists()
    assert "ahead-branch" in branches(repo)


def test_current_worktree_is_never_touched(tmp_path):
    repo = make_repo(tmp_path)
    here = add_worktree(repo, tmp_path, "here", "here-branch", locked=True)
    later = int(time.time()) + 10 * 86400
    r, _ = sweep(here, tmp_path, "--apply", now=later, docker=False)
    assert r.returncode == 0, r.stderr
    assert here.exists()


def test_docker_removes_only_dangling_anonymous_volumes(tmp_path):
    repo = make_repo(tmp_path)
    anon = "a" * 64
    dangling = f"{anon}\nmyproj_data\n"
    r, calls = sweep(repo, tmp_path, "--apply", docker_dangling=dangling)
    assert r.returncode == 0, r.stderr
    assert f"docker volume rm {anon}" in calls
    assert "myproj_data" not in calls


def test_docker_dry_run_removes_nothing(tmp_path):
    repo = make_repo(tmp_path)
    anon = "b" * 64
    r, calls = sweep(repo, tmp_path, docker_dangling=f"{anon}\n")
    assert r.returncode == 0, r.stderr
    assert "docker volume rm" not in calls
    assert f"would remove volume {anon}" in r.stdout
