"""The server control checkout advances only through safe Gitee-style updates."""

import fcntl
import json
import subprocess

import pytest

from prepare import control_update, deployment_config
from prepare.control_update import (
    REQUEST_SCHEMA,
    oldest_forward_request,
    update_control,
)
from prepare.runtime import EnvironmentManager


@pytest.fixture(autouse=True)
def isolated_server(monkeypatch):
    validate = deployment_config.validate_deployment_config
    monkeypatch.setattr(EnvironmentManager, "_docker", lambda *args, **kwargs: b"")
    # These Git/installation tests run without the CI server's mounted dependencies.
    monkeypatch.setattr(deployment_config, "validate_deployment_config", lambda config: None)
    return validate


def git(root, *arguments):
    return subprocess.check_output(["git", *arguments], cwd=root, text=True).strip()


def commit(root, message, content, *, config=None):
    (root / "control.txt").write_text(content)
    paths = ["control.txt"]
    if config is not None:
        source = root / deployment_config.CONFIG_SOURCE
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(json.dumps(config))
        paths.append(deployment_config.CONFIG_SOURCE)
    subprocess.run(["git", "add", *paths], cwd=root, check=True)
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
    control = tmp_path / "control"
    config = {
        "schema": "triton-anchor-local-ci-config",
        "control_root": str(control),
        "state_dir": str(tmp_path / "state"),
        "control_repo_url": str(source),
        "control_branch": "local-ci-unified",
        "runtime": {
            "kind": "docker-rootless",
            "endpoint": "unix:///run/user/1001/docker.sock",
            "context": "rootless",
        },
        "poll_interval_seconds": 60,
    }
    subprocess.run(["git", "init", "-q", "-b", "local-ci-unified"], cwd=source, check=True)
    old = commit(source, "old", "old", config=config)
    subprocess.run(["git", "clone", "-q", str(source), str(control)], check=True)
    new = commit(source, "new", "new", config={**config, "poll_interval_seconds": 30})
    (tmp_path / "local-ci.json").write_text(json.dumps(config))
    (tmp_path / "local-ci.json").chmod(0o600)
    return config, control, old, new


def test_control_checkout_fast_forwards_and_restarts_once(tmp_path, monkeypatch):
    config, control, old, new = fixture_checkout(tmp_path)
    marker = tmp_path / "state/control-update.json"
    marker.parent.mkdir(parents=True)
    marker.write_text(json.dumps({"revision": new}))
    restarts = []
    def docker(self, *arguments, **kwargs):
        assert arguments == ("ps", "-aq", "--filter", "label=local-ci.owner=" + self.owner,
                             "--format", '{{.Label "local-ci.kind"}}')
        return b"task\n"
    monkeypatch.setattr(EnvironmentManager, "_docker", docker)
    deferred = update_control(
        config,
        config_path=tmp_path / "local-ci.json", apply=True, allow_local=True, restart_worker=lambda: restarts.append("worker"),
        expected_revision=new,
    )
    assert deferred["state"] == "deferred-active-task"
    assert not deferred["changed"] and not deferred["restarted"]
    assert git(control, "rev-parse", "HEAD") == old
    assert not restarts
    # Environment probes no longer mount the control checkout and need not block it.
    monkeypatch.setattr(EnvironmentManager, "_docker", lambda *args, **kwargs: b"image-validation\n")
    result = update_control(
        config,
        config_path=tmp_path / "local-ci.json",
        apply=True,
        allow_local=True,
        expected_revision=new,
        restart_worker=lambda: restarts.append("worker"),
    )
    assert result["previous_revision"] == old
    assert result["revision"] == new
    assert result["changed"] and result["restarted"]
    assert result["config_changed"]
    assert git(control, "rev-parse", "HEAD") == new
    assert restarts == ["worker"]
    config_path = tmp_path / "local-ci.json"
    assert json.loads(config_path.read_text())["poll_interval_seconds"] == 30
    assert config_path.stat().st_mode & 0o777 == 0o600
    copied = config_path.stat()
    monkeypatch.setattr(
        EnvironmentManager, "_docker",
        lambda *args, **kwargs: pytest.fail("An unchanged revision needs no Docker check"),
    )
    again = update_control(
        config,
        config_path=tmp_path / "local-ci.json",
        apply=True,
        allow_local=True,
        expected_revision=new,
        restart_worker=lambda: restarts.append("worker"),
    )
    assert not again["changed"] and not again["restarted"]
    assert not again["config_changed"]
    assert config_path.stat().st_ino == copied.st_ino
    assert config_path.stat().st_mtime_ns == copied.st_mtime_ns
    assert restarts == ["worker"]


def test_control_checkout_refuses_local_changes(tmp_path):
    config, control, _, new = fixture_checkout(tmp_path)
    (control / "local-note.txt").write_text("do not overwrite")
    with pytest.raises(ValueError, match="local changes"):
        update_control(
            config,
            config_path=tmp_path / "local-ci.json",
            apply=True,
            allow_local=True,
            expected_revision=new,
            restart_worker=lambda: None,
        )


def test_applied_control_update_requires_an_exact_requested_revision(tmp_path):
    config, _, _, _ = fixture_checkout(tmp_path)
    with pytest.raises(ValueError, match="exact requested revision"):
        update_control(
            config,
            config_path=tmp_path / "local-ci.json", apply=True, allow_local=True, restart_worker=lambda: None
        )


def test_oldest_forward_request_uses_control_history_not_timestamp_or_task_id(
    tmp_path,
):
    config, _, current, older = fixture_checkout(tmp_path)
    newer = commit(tmp_path / "source", "newer", "newer")
    requests = [
        {
            "revision": newer,
            "task_id": "0" * 64,
            "captured_at": "2026-09-11T00:00:00Z",
        },
        {
            "revision": older,
            "task_id": "f" * 64,
            "captured_at": "2026-09-11T00:00:00Z",
        },
    ]
    selected = oldest_forward_request(
        config, current, requests, allow_local=True
    )
    assert selected["revision"] == older


def test_oldest_forward_request_ignores_divergent_candidate(tmp_path):
    config, _, current, forward = fixture_checkout(tmp_path)
    source = tmp_path / "source"
    subprocess.run(["git", "switch", "-q", "-c", "side", current], cwd=source, check=True)
    side = commit(source, "side", "side")
    subprocess.run(
        ["git", "switch", "-q", "local-ci-unified"], cwd=source, check=True
    )
    requests = [
        {
            "revision": forward,
            "task_id": "a" * 64,
            "captured_at": "2026-09-11T00:00:00Z",
        },
        {
            "revision": side,
            "task_id": "b" * 64,
            "captured_at": "2026-09-11T00:00:00Z",
        },
    ]
    selected = oldest_forward_request(config, current, requests, allow_local=True)
    assert selected["revision"] == forward


def test_oldest_forward_request_skips_stale_revision_without_blocking_newer(tmp_path):
    config, control, stale, current = fixture_checkout(tmp_path)
    update_control(
        config,
        config_path=tmp_path / "local-ci.json",
        apply=True,
        allow_local=True,
        expected_revision=current,
        restart_worker=lambda: None,
    )
    newer = commit(tmp_path / "source", "newer", "newer")
    requests = [
        {
            "revision": stale,
            "task_id": "a" * 64,
            "captured_at": "2026-09-10T00:00:00Z",
        },
        {
            "revision": newer,
            "task_id": "b" * 64,
            "captured_at": "2026-09-11T00:00:00Z",
        },
    ]
    selected = oldest_forward_request(config, current, requests, allow_local=True)
    assert git(control, "rev-parse", "HEAD") == current
    assert selected["revision"] == newer

    with pytest.raises(ValueError, match="no usable forward"):
        oldest_forward_request(config, current, requests[:1], allow_local=True)


def test_task_requested_revision_can_precede_remote_branch_tip(tmp_path):
    config, control, old, requested = fixture_checkout(tmp_path)
    remote_tip = commit(
        tmp_path / "source", "newer", "newer",
        config={**config, "poll_interval_seconds": 10},
    )
    config_path = tmp_path / "local-ci.json"
    before = config_path.read_bytes()
    preview = update_control(
        config,
        config_path=config_path,
        allow_local=True,
        expected_revision=requested,
        restart_worker=lambda: pytest.fail("Preview must not restart the Worker"),
    )
    assert preview["config_changed"]
    assert preview["config_fields"] == ["poll_interval_seconds"]
    assert config_path.read_bytes() == before
    assert git(control, "rev-parse", "HEAD") == old
    restarts = []

    def restart():
        assert git(control, "rev-parse", "HEAD") == requested
        assert json.loads(config_path.read_text())["poll_interval_seconds"] == 30
        restarts.append("worker")

    result = update_control(
        config,
        config_path=tmp_path / "local-ci.json",
        apply=True,
        allow_local=True,
        expected_revision=requested,
        restart_worker=restart,
    )
    assert result["previous_revision"] == old
    assert result["revision"] == requested
    assert result["remote_revision"] == remote_tip
    assert git(control, "rev-parse", "HEAD") == requested
    assert restarts == ["worker"]


def test_invalid_target_configuration_preserves_checkout_and_copy(
    tmp_path, monkeypatch, isolated_server
):
    config, control, old, _ = fixture_checkout(tmp_path)
    invalid = commit(
        tmp_path / "source", "invalid configuration", "invalid",
        config={**config, "resources": {"cpus": -1}},
    )
    monkeypatch.setattr(
        deployment_config, "validate_deployment_config", isolated_server
    )
    config_path = tmp_path / "local-ci.json"
    before = config_path.read_bytes()
    with pytest.raises(ValueError, match="Invalid deployment configuration"):
        update_control(
            config,
            config_path=config_path,
            apply=True,
            allow_local=True,
            expected_revision=invalid,
            restart_worker=lambda: pytest.fail("Invalid configuration must not restart"),
        )
    assert git(control, "rev-parse", "HEAD") == old
    assert config_path.read_bytes() == before


@pytest.mark.parametrize("restart_fails", [False, True])
def test_same_revision_repairs_drift_and_retries_failed_restart(
    tmp_path, monkeypatch, restart_fails
):
    config, control, _, requested = fixture_checkout(tmp_path)
    config_path = tmp_path / "local-ci.json"
    update_control(
        config, config_path=config_path, apply=True, allow_local=True,
        expected_revision=requested, restart_worker=lambda: None,
    )
    desired = json.loads(config_path.read_text())
    config_path.write_text(json.dumps({**desired, "poll_interval_seconds": 99}))
    before = config_path.read_bytes()
    restarts = []

    def restart():
        assert json.loads(config_path.read_text()) == desired
        restarts.append("worker")
        if restart_fails and len(restarts) == 1:
            raise RuntimeError("Restart failed")

    options = dict(
        config_path=config_path, apply=True, allow_local=True,
        expected_revision=requested, restart_worker=restart,
    )
    monkeypatch.setattr(EnvironmentManager, "_docker", lambda *args, **kwargs: b"task-cleanup\n")
    deferred = update_control(config, **options)
    assert deferred["state"] == "deferred-active-task"
    assert not deferred["restarted"] and not deferred["changed"]
    assert config_path.read_bytes() == before
    assert not restarts
    monkeypatch.setattr(EnvironmentManager, "_docker", lambda *args, **kwargs: b"")
    if restart_fails:
        with pytest.raises(RuntimeError, match="Restart failed"):
            update_control(config, **options)
        assert json.loads(config_path.read_text()) == desired
    result = update_control(config, **options)
    assert not result["changed"] and result["restarted"]
    assert result["config_changed"] is not restart_fails
    assert restarts == ["worker"] * (2 if restart_fails else 1)
    assert git(control, "rev-parse", "HEAD") == requested
    # JSON formatting/key order do not constitute drift or require a rewrite.
    config_path.write_text(json.dumps(dict(reversed(list(desired.items())))))
    before = config_path.read_bytes()
    monkeypatch.setattr(
        EnvironmentManager, "_docker",
        lambda *args, **kwargs: pytest.fail("Current config needs no Docker check"),
    )
    again = update_control(config, **options)
    assert not again["changed"] and not again["config_changed"] and not again["restarted"]
    assert config_path.read_bytes() == before
    assert restarts == ["worker"] * (2 if restart_fails else 1)


def test_requested_revision_must_be_on_branch_and_must_not_downgrade(tmp_path):
    config, control, old, current = fixture_checkout(tmp_path)
    update_control(
        config,
        config_path=tmp_path / "local-ci.json",
        apply=True,
        allow_local=True,
        expected_revision=current,
        restart_worker=lambda: None,
    )
    source = tmp_path / "source"
    subprocess.run(["git", "switch", "-q", "-c", "side", old], cwd=source, check=True)
    side = commit(source, "side", "side")
    subprocess.run(["git", "switch", "-q", "local-ci-unified"], cwd=source, check=True)

    with pytest.raises(ValueError, match="not reachable"):
        update_control(
            config,
            config_path=tmp_path / "local-ci.json",
            apply=True,
            allow_local=True,
            expected_revision=side,
            restart_worker=lambda: None,
        )
    with pytest.raises(ValueError, match="only fast-forward"):
        update_control(
            config,
            config_path=tmp_path / "local-ci.json",
            apply=True,
            allow_local=True,
            expected_revision=old,
            restart_worker=lambda: None,
        )
    assert git(control, "rev-parse", "HEAD") == current


def test_active_task_defers_requested_update_without_losing_revision(tmp_path):
    config, control, old, requested = fixture_checkout(tmp_path)
    config_path = tmp_path / "local-ci.json"
    before = config_path.read_bytes()
    state = tmp_path / "state"
    state.mkdir()
    with (state / "control.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_SH)
        result = update_control(
            config,
            config_path=tmp_path / "local-ci.json",
            apply=True,
            allow_local=True,
            expected_revision=requested,
            restart_worker=lambda: None,
        )
        fcntl.flock(lock, fcntl.LOCK_UN)
    assert result["state"] == "deferred-active-task"
    assert result["revision"] == requested
    assert git(control, "rev-parse", "HEAD") == old
    assert config_path.read_bytes() == before


def test_request_file_supplies_exact_revision_and_is_removed_after_success(
    tmp_path, monkeypatch
):
    request_path = tmp_path / "state/control-update/request.json"
    request_path.parent.mkdir(parents=True)
    request = {
        "schema": REQUEST_SCHEMA,
        "revision": "a" * 40,
        "task_id": "b" * 64,
        "requested_at": 1,
    }
    request_path.write_text(json.dumps(request))
    request_path.chmod(0o600)
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps({"state_dir": str(tmp_path / "state")})
    )
    calls = []
    monkeypatch.setattr(control_update.os, "geteuid", lambda: 1001)

    def update(config, **kwargs):
        calls.append(
            {
                "apply": kwargs["apply"],
                "expected_revision": kwargs["expected_revision"],
                "config_path": kwargs["config_path"],
            }
        )
        request_path.write_text(json.dumps({**request, "requested_at": 2}))
        request_path.chmod(0o600)
        kwargs["on_success"]()
        return {"state": "updated", "revision": kwargs["expected_revision"]}

    monkeypatch.setattr(
        control_update,
        "update_control",
        update,
    )
    assert (
        control_update.main(
            [
                "--config",
                str(config_path),
                "--request-file",
                str(request_path),
                "--apply",
            ]
        )
        == 0
    )
    assert calls == [{
        "apply": True, "expected_revision": "a" * 40, "config_path": config_path,
    }]
    assert not request_path.exists()


def test_deferred_request_file_remains_for_the_next_worker_scan(tmp_path, monkeypatch):
    request_path = tmp_path / "state/control-update/request.json"
    request_path.parent.mkdir(parents=True)
    request_path.write_text(
        json.dumps(
            {
                "schema": REQUEST_SCHEMA,
                "revision": "a" * 40,
                "task_id": "b" * 64,
            }
        )
    )
    request_path.chmod(0o600)
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"state_dir": str(tmp_path / "state")}))
    monkeypatch.setattr(control_update.os, "geteuid", lambda: 1001)
    monkeypatch.setattr(
        control_update,
        "update_control",
        lambda config, **kwargs: {
            "state": "deferred-active-task",
            "revision": kwargs["expected_revision"],
        },
    )
    assert (
        control_update.main(
            [
                "--config",
                str(config_path),
                "--request-file",
                str(request_path),
                "--apply",
            ]
        )
        == 0
    )
    assert request_path.exists()


def test_request_file_rejects_symlink_at_configured_path(tmp_path, monkeypatch):
    state = tmp_path / "state"
    request_path = state / "control-update/request.json"
    request_path.parent.mkdir(parents=True)
    outside = tmp_path / "outside.json"
    outside.write_text(
        json.dumps(
            {
                "schema": REQUEST_SCHEMA,
                "revision": "a" * 40,
                "task_id": "b" * 64,
                "requested_at": 1,
            }
        )
    )
    outside.chmod(0o600)
    request_path.symlink_to(outside)
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"state_dir": str(state)}))
    monkeypatch.setattr(
        control_update,
        "update_control",
        lambda config, **kwargs: pytest.fail("symlink request must not be applied"),
    )
    assert (
        control_update.main(
            ["--config", str(config_path), "--request-file", str(request_path)]
        )
        == 1
    )
