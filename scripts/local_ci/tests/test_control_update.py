"""The server control checkout advances only through safe Gitee-style updates."""

import subprocess

import pytest

from prepare.control_update import update_control


def git(root, *arguments):
    return subprocess.check_output(["git", *arguments], cwd=root, text=True).strip()


def commit(root, message, content):
    (root / "control.txt").write_text(content)
    subprocess.run(["git", "add", "control.txt"], cwd=root, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "-q",
            "-m",
            message,
        ],
        cwd=root,
        check=True,
    )
    return git(root, "rev-parse", "HEAD")


def fixture_checkout(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "local-ci-unified"], cwd=source, check=True)
    old = commit(source, "old", "old")
    control = tmp_path / "control"
    subprocess.run(["git", "clone", "-q", str(source), str(control)], check=True)
    new = commit(source, "new", "new")
    config = {
        "control_root": str(control),
        "state_dir": str(tmp_path / "state"),
        "control_repo_url": str(source),
        "control_branch": "local-ci-unified",
    }
    return config, control, old, new


def test_control_checkout_fast_forwards_and_restarts_once(tmp_path):
    config, control, old, new = fixture_checkout(tmp_path)
    restarts = []
    result = update_control(
        config,
        apply=True,
        allow_local=True,
        restart_worker=lambda: restarts.append("worker"),
    )
    assert result["previous_revision"] == old
    assert result["revision"] == new
    assert result["changed"] and result["restarted"]
    assert git(control, "rev-parse", "HEAD") == new
    assert restarts == ["worker"]
    again = update_control(
        config,
        apply=True,
        allow_local=True,
        restart_worker=lambda: restarts.append("worker"),
    )
    assert not again["changed"] and not again["restarted"]
    assert restarts == ["worker"]


def test_control_checkout_refuses_local_changes(tmp_path):
    config, control, _, _ = fixture_checkout(tmp_path)
    (control / "local-note.txt").write_text("do not overwrite")
    with pytest.raises(ValueError, match="local changes"):
        update_control(config, apply=True, allow_local=True, restart_worker=lambda: None)
