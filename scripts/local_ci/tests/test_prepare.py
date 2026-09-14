"""Rootless environment setup, owned work cleanup and deployment behavior."""
import copy
import json
import os
import stat
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from prepare import runtime_probe as probe, install, container_fs as fs
from prepare.runtime import EnvironmentManager, identities, shared_image, validate_shared_profile, resolve_task_profile
from prepare.artifacts import tree_digest
from prepare.artifacts import EnvironmentError
from prepare.control_mount import bind_control, mount_arguments, mounts, verify_mount, CONTROL_TARGET

LOCAL = Path(__file__).resolve().parents[1]


def config(tmp_path):
    return {
        "state_dir": str(tmp_path / "state"),
        "control_root": str(LOCAL.parents[1]),
        "docker_bin": "fixture-docker",
        "codex_bin": "/usr/local/bin/codex",
        "runtime": {
            "kind": "docker-rootless",
            "endpoint": "unix:///run/user/1001/docker.sock",
            "context": "ci-rootless",
            "service": "docker.service",
        },
        "resources": {"cpus": 2, "memory_bytes": 134217728, "pids_limit": 64},
        "identities": dict(probe.DEFAULT_IDENTITIES),
        "monitor_services": [],
        "profiles": {
            "triton_v3.0": {
                "name": "triton-3.0",
                "image": "sha256:" + "a" * 64,
                "daily_calendar": "*-*-* 02:00:00 Asia/Shanghai",
            }
        },
    }


def test_every_docker_call_uses_fixed_host_and_drops_ambient_context(tmp_path):
    settings = config(tmp_path)
    with (
        patch.dict(
            os.environ,
            {
                "DOCKER_CONTEXT": "system-default",
                "DOCKER_HOST": "unix:///var/run/docker.sock",
            },
        ),
        patch.object(
            probe.subprocess,
            "run",
            return_value=SimpleNamespace(returncode=0, stdout="ok"),
        ) as run,
    ):
        assert probe.docker(settings, "info") == "ok"
    argv = run.call_args.args[0]
    assert argv == ["fixture-docker", "--host", settings["runtime"]["endpoint"], "info"]
    assert "DOCKER_CONTEXT" not in run.call_args.kwargs["env"]
    assert "DOCKER_HOST" not in run.call_args.kwargs["env"]


@pytest.mark.parametrize(
    "kind,cgroup,driver,context_ok",
    [
        ("rootful", "2", "systemd", True),
        ("rootless", "1", "systemd", True),
        ("rootless", "2", "none", True),
        ("rootless", "2", "systemd", False),
    ],
)
def test_runtime_identity_and_controllers_are_checked(
    tmp_path, kind, cgroup, driver, context_ok
):
    settings = config(tmp_path)

    def docker(_config, *args):
        if args[0] == "context":
            return json.dumps(
                [
                    {
                        "Endpoints": {
                            "docker": {
                                "Host": settings["runtime"]["endpoint"]
                                if context_ok
                                else "unix:///var/run/docker.sock"
                            }
                        }
                    }
                ]
            )
        return json.dumps(
            {
                "SecurityOptions": ["name=" + kind],
                "CgroupVersion": cgroup,
                "CgroupDriver": driver,
                "ID": "fixture-daemon",
            }
        )

    with (
        patch.object(probe.os, "geteuid", return_value=1001),
        patch.object(probe.os, "getuid", return_value=1001),
        patch.object(Path, "is_symlink", return_value=False),
        patch.object(
            Path,
            "stat",
            return_value=SimpleNamespace(st_mode=stat.S_IFSOCK, st_uid=1001),
        ),
        patch.object(probe, "docker", side_effect=docker),
        pytest.raises(ValueError),
    ):
        probe.runtime_status(settings)


class FakeDocker:
    def __init__(self, limits=None, ownership=True):
        self.commands, self.labels = [], {}
        self.limits = limits or {
            "cpu.max": "200000 100000",
            "memory.max": "134217728",
            "pids.max": "64",
        }
        self.ownership = ownership

    def __call__(self, config, *args, **kwargs):
        self.commands.append(args)
        if args[0] == "create":
            self.labels = dict(
                args[index + 1].split("=", 1)
                for index, arg in enumerate(args)
                if arg == "--label"
            )
            return "b" * 64
        if args[0] == "start":
            return json.dumps(self.limits)
        if args[0] == "inspect":
            return json.dumps(
                [
                    {
                        "Id": "b" * 64,
                        "Config": {"Labels": self.labels if self.ownership else {}},
                    }
                ]
            )
        if args[0] == "rm":
            return ""
        raise AssertionError(args)


def test_canary_checks_real_resource_limits(tmp_path):
    settings, docker = config(tmp_path), FakeDocker()
    with (
        patch.object(probe, "runtime_status", return_value={"daemon_id": "fixture"}),
        patch.object(probe, "active_images", return_value={"triton_v3.0": "sha256:" + "c" * 64}),
        patch.object(probe, "docker", docker),
    ):
        assert probe.probe_runtime(settings)["status"] == "pass"
        assert [args[0] for args in docker.commands] == ["create", "start", "inspect", "rm"]
        assert "none" in docker.commands[0] and "--privileged" not in docker.commands[0]
        docker.limits["pids.max"] = "max"
        with pytest.raises(ValueError, match="not effectively enforced"):
            probe.probe_runtime(settings)
        assert docker.commands[-1][0] == "rm"


def test_active_image_selection_uses_real_public_registry_and_rejects_wrong_release(
    tmp_path,
):
    settings = config(tmp_path)
    settings["profiles"]["triton_v3.0"]["llvm_hash"] = "d" * 40
    manager = probe.EnvironmentManager(settings, settings["state_dir"])
    registry = {
        "active_images": {"triton_v3.0": "release-1"},
        "images": {
            "release-1": {
                "release_id": "release-1",
                "target_branch": "triton_v3.0",
                "llvm_hash": "d" * 40,
                "image_id": "sha256:" + "c" * 64,
                "state": "ready",
                "validated": True,
            }
        },
        "attempts": {},
        "leases": {},
        "events": [],
    }
    manager.registry.write_text(json.dumps(registry))
    assert probe.active_images(settings) == {"triton_v3.0": "sha256:" + "c" * 64}
    for field, value in (
        ("validated", False),
        ("state", "quarantined"),
        ("llvm_hash", "e" * 40),
    ):
        changed = copy.deepcopy(registry)
        changed["images"]["release-1"][field] = value
        manager.registry.write_text(json.dumps(changed))
        with pytest.raises(ValueError, match="Prepare the shared image"):
            probe.active_images(settings)



def test_probe_never_removes_replaced_or_unowned_container(tmp_path):
    settings, docker = config(tmp_path), FakeDocker(ownership=False)
    with (
        patch.object(probe, "runtime_status", return_value={}),
        patch.object(
            probe, "active_images", return_value={"triton_v3.0": "sha256:" + "c" * 64}
        ),
        patch.object(probe, "docker", docker),
        pytest.raises(ValueError, match="ownership changed"),
    ):
        probe.probe_runtime(settings)
    assert not any(command[0] == "rm" for command in docker.commands)


def test_only_one_nonroot_execution_identity():
    assert identities({}) == ({"task": 11001}, {"task": 11001})
    for config in (
        {"identities": {"task": 0}},
        {"identities": {"candidate": 11001}},
        {"identities": {"gid": True}},
    ):
        with pytest.raises(EnvironmentError):
            identities(config)


def test_shared_image_rejects_different_profile_images_and_legacy_layers():
    image = "sha256:" + "a" * 64
    assert shared_image({"image": image, "profiles": {"a": {}, "b": {}}}) == image
    assert shared_image({"profiles": {"a": {"image": image}, "b": {"image": image}}}) == image
    with pytest.raises(EnvironmentError, match="one shared image"):
        shared_image({"image": image, "profiles": {"a": {"image": "sha256:" + "b" * 64}}})
    for profile in ({"llvm": {"mode": "source"}},
                    {"llvm": {"mode": "mount"}, "prepare_commands": [["pip", "install", "x"]]}):
        with pytest.raises(EnvironmentError):
            validate_shared_profile(profile)


def test_unmapped_branch_selects_unique_llvm_profile_without_overriding_routing():
    settings = {
        "profiles": {
            "triton_v3.0": {"llvm_hash": "a" * 40, "llvm": {"revisions": {"c" * 40: {}}}},
            "triton_v3.3": {"llvm_hash": "b" * 40},
        },
        "branch_profiles": {"CI_dev": "triton_v3.0"},
    }
    assert resolve_task_profile(settings, "local-ci-unified", "a" * 40) == "triton_v3.0"
    assert resolve_task_profile(settings, "feature/change", "c" * 40) == "triton_v3.0"
    # Explicit selections stay authoritative, even if later LLVM validation fails.
    assert resolve_task_profile(settings, "CI_dev", "b" * 40) == "triton_v3.0"
    assert resolve_task_profile(settings, "triton_v3.0", "b" * 40) == "triton_v3.0"
    with pytest.raises(EnvironmentError, match="No configured profile supports LLVM"):
        resolve_task_profile(settings, "feature/change", "d" * 40)
    settings["profiles"]["alternate"] = {"llvm_hash": "a" * 40}
    with pytest.raises(EnvironmentError, match="Multiple profiles.*branch_profiles"):
        resolve_task_profile(settings, "local-ci-unified", "a" * 40)
    settings["branch_profiles"]["local-ci-unified"] = "triton_v3.0"
    assert resolve_task_profile(settings, "local-ci-unified", "a" * 40) == "triton_v3.0"


def test_profiles_use_same_image_without_build_or_control_sha_revalidation(tmp_path):
    cfg = config(tmp_path)
    cfg["image"] = "sha256:" + "a" * 64
    deps = tmp_path / "deps"
    deps.mkdir(mode=0o755)
    cfg["dependency_root"] = str(deps)
    cfg["profiles"] = {}
    for branch, digit, version in (("branch-a", "b", "3.3"), ("branch-b", "c", "3.6")):
        llvm = deps / ("llvm-" + digit * 40)
        llvm.mkdir(mode=0o755)
        (llvm / "include").mkdir(mode=0o755)
        (llvm / "lib").mkdir(mode=0o755)
        cfg["profiles"][branch] = {
            "name": branch, "llvm_hash": digit * 40, "triton_version": version,
            "llvm": {"mode": "mount", "commit": digit * 40},
            "backend_enabled": False,
            "env": {"PYTHON_VENV_ACTIVATE": "/opt/venv/bin/activate"},
            "mounts": [{"source": str(llvm), "target": "/opt/local-ci/runtime/deps/" + llvm.name,
                        "read_only": True, "sha256": tree_digest(llvm)}],
            # Old full-Wheel commands must never be executed at task startup.
            "validation_commands": {"frontend_build": ["must-not-build-wheel"]},
        }
    manager = EnvironmentManager(cfg, cfg["state_dir"])
    with (
        patch.object(manager, "_daemon", return_value="daemon"),
        patch.object(manager, "_control_revision", return_value="d" * 40) as revision,
        patch.object(manager, "_inspect", return_value={"Id": cfg["image"]}),
        patch.object(manager, "_docker") as docker,
        patch.object(manager, "_validate_image", return_value={"checks": ["environment"]}) as probe,
    ):
        a = manager.ensure_image("branch-a", "b" * 40)
        b = manager.ensure_image("branch-b", "c" * 40)
        assert a["image_id"] == b["image_id"] == cfg["image"]
        assert a["environment_fingerprint"] != b["environment_fingerprint"]
        revision.return_value = "e" * 40
        again = manager.ensure_image("branch-a", "b" * 40)
        assert again["image_release_id"] == a["image_release_id"]
        assert probe.call_count == 2
        docker.assert_not_called()
        state = manager._load()
        state["active_images"].clear()
        for row in state["images"].values():
            row["created_at"] = "2000-01-01T00:00:00Z"
        manager._save(state)
        assert len(manager.collect_retired()["removed"]) == 2
        docker.assert_not_called()  # Retiring profiles never deletes the shared image.


def test_runtime_probe_does_not_execute_wheel_validation_commands(tmp_path):
    cfg = config(tmp_path)
    manager = EnvironmentManager(cfg, cfg["state_dir"])
    profile = {"control_revision": "a" * 40, "mounts": [], "backend_enabled": False,
               "validation_commands": {"frontend_build": ["must-not-build-wheel"]}}
    commands = []
    def docker(*args, **kwargs):
        commands.append(args)
        return b"container" if args[0] == "create" else b""
    with (
        patch.object(manager, "_inspect", return_value={}),
        patch.object(manager, "_stop_owned"),
        patch.object(manager, "_docker", side_effect=docker),
    ):
        proof = manager._validate_image("sha256:" + "a" * 64, profile,
                                        {"LLVM_BUILD_DIR": "/llvm", "SEED_PYTHON": "/opt/venv/bin/python"})
    assert proof["checks"] == ["environment"]
    assert all("must-not-build-wheel" not in command for command in commands)
    assert all("validate_environment.py" not in " ".join(command) for command in commands)
    assert all(CONTROL_TARGET not in " ".join(command) for command in commands)
    create = commands[0]
    assert create[create.index("--user") + 1] == "11001:11001"
    assert create[create.index("--entrypoint") + 1] == "/bin/sh"


def test_task_mounts_expose_only_work_artifacts_and_readonly_control(tmp_path):
    cfg = {
        "runtime": {
            "kind": "docker-rootless",
            "endpoint": "unix:///run/user/1001/docker.sock",
        },
        "resources": {"cpus": 2, "memory_bytes": 1024**3, "pids_limit": 100},
        "profiles": {"triton_v3.0": {"llvm_hash": "b" * 40}},
    }
    manager = EnvironmentManager(cfg, tmp_path)
    image = dict(
        profile="test",
        image_release_id="r1",
        image_id="sha256:" + "a" * 64,
        llvm_hash="b" * 40,
        backend_enabled=False,
        env={},
        environment_fingerprint="f",
        daemon_id="d",
    )
    calls = []

    def docker(*args, **kwargs):
        calls.append(args)
        return ("c" * 64).encode() if args[0] == "create" else b""

    task = dict(
        task_id="a" * 64,
        head_sha="d" * 40,
        pr_number=7,
        target_branch="main",
        llvm_hash="b" * 40,
        worker_revision_sha="c" * 40,
    )
    with (
        patch.object(manager, "_control_revision", return_value="c" * 40),
        patch.object(manager, "ensure_image", return_value=image) as ensure,
        patch.object(
            manager,
            "_control_mount",
            return_value={"source": "/control_anchor", "revision": "c" * 40,
                          "paths": ["scripts", "api_contract", "envsetup.sh"]},
        ),
        patch.object(manager, "_verify"),
        patch.object(manager, "_helper", return_value={}),
        patch.object(manager, "_docker", side_effect=docker),
    ):
        handle = manager.acquire_task(task, "run-1")
    ensure.assert_called_once_with("triton_v3.0", "b" * 40)
    assert handle["target_branch"] == "main"
    assert handle["profile_branch"] == "triton_v3.0"
    create = next(c for c in calls if c[0] == "create")
    mounts = [create[i + 1] for i, x in enumerate(create) if x == "--mount"]
    assert len(mounts) == 5
    assert mounts[0].endswith(",target=/task")
    assert mounts[1].endswith(",target=/task/artifacts")
    assert handle["artifacts_host"] == str(
        tmp_path / "runs/pr/branch-main/pr-7" / task["head_sha"] / "run-1/artifacts"
    )
    assert handle["workspace_host"] == str(tmp_path / "work" / task["head_sha"] / "run-1")
    assert all("/sealed" not in m and "/logs" not in m for m in mounts)
    assert all(
        m == "type=bind,source=/control_anchor/" + path + ",target=" + CONTROL_TARGET
        + "/" + path + ",readonly,bind-recursive=disabled"
        for m, path in zip(mounts[2:], ["scripts", "api_contract", "envsetup.sh"])
    )
    assert create[create.index("--user") + 1] == "11001:11001"
    assert handle["attempt_id"] == handle["run_id"] == "run-1"
    assert handle["uids"] == {"task": 11001}


def test_control_mount_uses_checkout_and_repairs_only_tracked_permissions(tmp_path):
    control = tmp_path / "control_anchor"
    (control / "scripts/tools").mkdir(parents=True, mode=0o700)
    (control / "scripts").chmod(0o700)
    (control / ".git").mkdir(mode=0o700)
    tool = control / "scripts/tools/check.py"
    tool.write_text("print('check')")
    tool.chmod(0o600)
    setup = control / "envsetup.sh"
    setup.write_text("#!/bin/sh\n")
    setup.chmod(0o700)
    credentials = control / "credentials.env"
    credentials.write_text("private")
    credentials.chmod(0o600)
    with patch("subprocess.check_output", return_value=b"scripts/tools/check.py\0envsetup.sh\0") as run:
        descriptor = bind_control(control, "a" * 40, run)
    assert run.call_count == 1 and "ls-tree" in run.call_args.args[0]
    assert descriptor["source"] == str(control)
    assert descriptor["paths"] == ["envsetup.sh", "scripts"]
    assert stat.S_IMODE(tool.stat().st_mode) == 0o644
    assert stat.S_IMODE(setup.stat().st_mode) == 0o755
    assert stat.S_IMODE(tool.parent.stat().st_mode) == 0o755
    assert stat.S_IMODE((control / "scripts").stat().st_mode) == 0o755
    assert stat.S_IMODE(credentials.stat().st_mode) == 0o600
    assert stat.S_IMODE((control / ".git").stat().st_mode) == 0o700
    info = {"Mounts": [dict(Type="bind", Source=m["source"], Destination=m["target"], RW=False)
                       for m in mounts(descriptor)]}
    verify_mount(info, descriptor)
    info["Mounts"][0]["RW"] = True
    with pytest.raises(EnvironmentError, match="readonly"):
        verify_mount(info, descriptor)
    # Pre-upgrade stopped runs still have a single exported root to clean up.
    legacy = {"source": "/state/environments/control-revisions/" + "b" * 40}
    assert mount_arguments(legacy) == ["--mount", "type=bind,source=" + legacy["source"]
                                       + ",target=" + CONTROL_TARGET + ",readonly,bind-recursive=disabled"]


def test_workspace_rejects_partial_and_changed_seed(tmp_path, monkeypatch):
    root = tmp_path / "candidate"
    (root / "venv/bin").mkdir(parents=True)
    (root / "venv/bin/python").write_text("existing")
    monkeypatch.setattr(fs, "TASK", tmp_path)
    monkeypatch.setattr(
        fs,
        "manifest",
        lambda: {"uids": {"task": os.getuid()}, "gids": {"task": os.getgid()}},
    )
    with pytest.raises(ValueError, match="Partial or different"):
        fs.prepare_workspace({"environment_fingerprint": "image-1"})
    (root / "venv/.local-ci-environment.json").write_text(
        json.dumps({"environment_fingerprint": "other"})
    )
    with pytest.raises(ValueError, match="Partial or different"):
        fs.prepare_workspace({"environment_fingerprint": "image-1"})


def cleanup_manager(tmp_path, *, container_id="missing"):
    settings = {
        "runtime": {
            "kind": "docker-rootless",
            "endpoint": "unix:///run/user/1001/docker.sock",
        },
        "resources": {"cpus": 1, "memory_bytes": 1024**3, "pids_limit": 100},
    }
    manager = EnvironmentManager(settings, tmp_path)
    work = tmp_path / "work" / ("a" * 64) / "run-1"
    work.mkdir(parents=True)
    (work / "scratch").write_text("temporary")
    artifacts = tmp_path / "runs" / ("a" * 64) / "run-1/artifacts"
    artifacts.mkdir(parents=True)
    (artifacts / "evidence").write_text("durable")
    handle = {
        "task_id": "a" * 64,
        "run_id": "run-1",
        "attempt_id": "run-1",
        "state": "stopped",
        "container": "local-ci-task-exact",
        "container_id": container_id,
        "image_id": "sha256:" + "a" * 64,
        "control_revision": "b" * 40,
        "control_snapshot": {},
        "daemon_id": "rootless-daemon",
        "workspace_host": str(work),
        "artifacts_host": str(artifacts),
    }
    state = manager._load()
    state["attempts"]["run-1"] = dict(handle)
    state["leases"][handle["task_id"]] = {"generation": "run-1"}
    manager._save(state)
    return manager, handle, work, artifacts


def test_missing_container_removes_only_owned_scratch(tmp_path):
    manager, handle, work, artifacts = cleanup_manager(tmp_path)
    with (
        patch.object(manager, "_daemon", return_value="rootless-daemon"),
        patch.object(manager, "_docker", return_value=b""),
    ):
        manager.destroy_task(handle)
    assert not work.exists()
    assert not work.parent.exists()
    assert (artifacts / "evidence").read_text() == "durable"
    assert manager.generation("run-1")["state"] == "removed"


@pytest.mark.parametrize("sibling", [False, True])
def test_sha_scratch_cleanup_prunes_only_empty_commit_directory(tmp_path, sibling):
    manager, handle, work, artifacts = cleanup_manager(tmp_path)
    head = "b" * 40
    parent = work.parent.with_name(head)
    work.parent.rename(parent)
    work = parent / work.name
    handle.update(head_sha=head, workspace_host=str(work))
    if sibling:
        (parent / "other-run").mkdir()
        (parent / "other-run/keep").write_text("active")
    with patch.object(manager, "_docker", return_value=b""):
        manager._remove_scratch(handle)
    assert not work.exists()
    assert parent.exists() == sibling
    assert (artifacts / "evidence").exists()
    if sibling:
        assert (parent / "other-run/keep").read_text() == "active"
    # Retry cleanup of an already absent run is harmless.
    with patch.object(manager, "_docker", return_value=b""):
        manager._remove_scratch(handle)


def test_failed_missing_scratch_cleanup_stays_pending(tmp_path):
    manager, handle, work, artifacts = cleanup_manager(tmp_path)
    with (
        patch.object(manager, "_daemon", return_value="rootless-daemon"),
        patch.object(manager, "_docker", return_value=b""),
        patch.object(
            manager,
            "_remove_scratch",
            side_effect=PermissionError("still owned by subuid"),
        ),
    ):
        with pytest.raises(PermissionError):
            manager.destroy_task(handle)
    assert manager.generation("run-1")["state"] != "removed"
    assert work.exists()


def test_subuid_scratch_uses_only_offline_work_management_mount(tmp_path):
    import shutil

    manager, handle, work, artifacts = cleanup_manager(tmp_path)
    real_remove = shutil.rmtree
    calls = []
    first = True

    def remove(path):
        nonlocal first
        if first:
            first = False
            raise PermissionError("subuid")
        real_remove(path)

    def docker(*args, **kwargs):
        calls.append(args)
        return b"cleanup-id" if args[0] == "create" else b""

    with (
        patch("prepare.runtime.shutil.rmtree", side_effect=remove),
        patch("prepare.runtime.control_mount_arguments", return_value=[]),
        patch.object(manager, "_docker", side_effect=docker),
        patch.object(manager, "_stop_owned", return_value={"verified": True}),
    ):
        manager._remove_scratch(handle)
    create = next(c for c in calls if c[0] == "create")
    assert create[create.index("--network") + 1] == "none"
    mounts = [create[i + 1] for i, x in enumerate(create) if x == "--mount"]
    assert mounts == ["type=bind,source=" + str(work) + ",target=/task"]
    assert not work.exists() and (artifacts / "evidence").exists()


@pytest.mark.parametrize("other_path", ["work", "work/other/run-1", "runs"])
def test_cleanup_rejects_any_path_except_this_task_run(tmp_path, other_path):
    manager, handle, work, artifacts = cleanup_manager(tmp_path)
    handle["workspace_host"] = str(tmp_path / other_path)
    with patch.object(manager, "_docker") as docker:
        with pytest.raises(EnvironmentError, match="configured path"):
            manager._remove_scratch(handle)
    docker.assert_not_called()
    assert (work / "scratch").exists() and (artifacts / "evidence").exists()


def test_install_prepares_then_starts_user_services(tmp_path):
    settings = config(tmp_path)
    config_file, credentials = tmp_path / "config.json", tmp_path / "credentials.env"
    config_file.write_text(json.dumps(settings))
    credentials.write_text("FIXTURE=private\n")
    credentials.chmod(0o600)
    unit_dir = tmp_path / "config/systemd/user"
    unit_dir.mkdir(parents=True)
    for name in install.OBSOLETE_UNITS:
        (unit_dir / name).write_text("old periodic control update")
    events = []
    systemd_calls = []

    def systemd(argv, **kwargs):
        events.append(argv)
        systemd_calls.append((argv, kwargs))

    with (
        patch.dict(os.environ, {"XDG_CONFIG_HOME": str(tmp_path / "config")}),
        patch.object(sys, "argv", ["install.py", "--config", str(config_file),
                                  "--credentials-env", str(credentials), "--apply"]),
        patch.object(install.os, "geteuid", return_value=1001),
        patch.object(install, "load_environment", side_effect=lambda *a: events.append("env")),
        patch.object(install, "prepare_environments", side_effect=lambda *a: events.append("prepare")),
        patch.object(install.subprocess, "run", side_effect=systemd),
    ):
        assert install.main() == 0
    assert events[:2] == ["env", "prepare"]
    assert ["systemctl", "--user", "daemon-reload"] in events
    assert any(isinstance(e, list) and "restart" in e and "triton-anchor-local-ci.service" in e for e in events)
    assert ["systemctl", "--user", "disable", "--now", *install.OBSOLETE_UNITS] in events
    disable = next(
        kwargs
        for argv, kwargs in systemd_calls
        if argv[:4] == ["systemctl", "--user", "disable", "--now"]
    )
    assert disable["check"] is True
    assert (tmp_path / "config/systemd/user/triton-anchor-local-ci.service").is_file()
    assert not any(
        (tmp_path / "config/systemd/user" / name).exists()
        for name in install.OBSOLETE_UNITS
    )


def test_rendered_worker_units_include_local_watchdog_and_control_update(tmp_path):
    units = install.render_units(
        config(tmp_path), tmp_path / "config.json", tmp_path / "credentials.env"
    )
    assert "triton-anchor-local-ci-watchdog.timer" in units
    assert "triton-anchor-local-ci-health.timer" in units
    assert "triton-anchor-local-ci-retention.timer" in units
    assert "triton-anchor-local-ci-control-update.timer" not in units
    assert "control_update.py" in units["triton-anchor-local-ci-control-update.service"]
    assert "--request-file" in units["triton-anchor-local-ci-control-update.service"]


def test_install_backup_supports_obsolete_control_timer_removal_and_rollback(tmp_path):
    destination, backup = tmp_path / "units", tmp_path / "backup"
    destination.mkdir()
    old = destination / "triton-anchor-local-ci-control-update.timer"
    old.write_text("old periodic control update")
    units = {"triton-anchor-local-ci.service": "new worker"}
    manifest = install.install_units(units, destination, backup)
    assert manifest["units"][old.name] == {"existed": True, "removed": True}
    assert old.exists()  # The caller stops the loaded timer before unlinking it.
    old.unlink()
    assert install.rollback_units(backup, apply=True)["scope"] == "user"
    assert old.read_text() == "old periodic control update"


def test_install_preserves_obsolete_timer_file_when_disable_fails(tmp_path):
    settings = config(tmp_path)
    config_file, credentials = tmp_path / "config.json", tmp_path / "credentials.env"
    config_file.write_text(json.dumps(settings))
    credentials.write_text("FIXTURE=private\n")
    credentials.chmod(0o600)
    unit_dir = tmp_path / "config/systemd/user"
    unit_dir.mkdir(parents=True)
    old = unit_dir / install.OBSOLETE_UNITS[0]
    old.write_text("old periodic control update")

    def systemd(argv, **kwargs):
        if argv[:4] == ["systemctl", "--user", "disable", "--now"]:
            raise install.subprocess.CalledProcessError(1, argv)

    with (
        patch.dict(os.environ, {"XDG_CONFIG_HOME": str(tmp_path / "config")}),
        patch.object(
            sys,
            "argv",
            [
                "install.py",
                "--config",
                str(config_file),
                "--credentials-env",
                str(credentials),
                "--apply",
            ],
        ),
        patch.object(install.os, "geteuid", return_value=1001),
        patch.object(install, "load_environment"),
        patch.object(install, "prepare_environments"),
        patch.object(install.subprocess, "run", side_effect=systemd),
    ):
        assert install.main() == 1
    assert old.read_text() == "old periodic control update"


def test_install_failure_does_not_start_services(tmp_path):
    settings = config(tmp_path)
    config_file, credentials = tmp_path / "config.json", tmp_path / "credentials.env"
    config_file.write_text(json.dumps(settings))
    credentials.write_text("FIXTURE=private\n")
    credentials.chmod(0o600)
    with (
        patch.dict(os.environ, {"XDG_CONFIG_HOME": str(tmp_path / "config")}),
        patch.object(sys, "argv", ["install.py", "--config", str(config_file),
                                  "--credentials-env", str(credentials), "--apply"]),
        patch.object(install.os, "geteuid", return_value=1001),
        patch.object(install, "load_environment"),
        patch.object(install, "prepare_environments", side_effect=ValueError("environment unavailable")),
        patch.object(install.subprocess, "run") as run,
    ):
        assert install.main() == 1
    assert not run.called
    assert not (tmp_path / "config/systemd/user").exists()


def test_artifact_collection_makes_files_readable_without_following_links(tmp_path, monkeypatch):
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir(mode=0o700)
    report = artifacts / "result.json"
    report.write_text("{}")
    report.chmod(0o600)
    external = tmp_path / "private"
    external.write_text("private")
    external.chmod(0o600)
    (artifacts / "outside").symlink_to(external)
    monkeypatch.setattr(fs, "TASK", tmp_path)
    assert fs.collect_artifacts()["artifact_dir"] == str(artifacts)
    assert report.stat().st_mode & 0o777 == 0o644
    assert external.stat().st_mode & 0o777 == 0o600
