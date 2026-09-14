"""Retention and public health behavior, without remote attachment lifecycle."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from maintenance import health
from maintenance.retention import retain_local
from maintenance.health import public_snapshot
from maintenance.watchdog import evaluate


RUN_LAYOUTS = ["", "pr/branch-main/pr-56", "push/branch-release%2F3.0"]


def make_run(root, run, phase, published, layout=""):
    path = root / "runs" / layout / ("a" * 64) / run
    (path / "artifacts").mkdir(parents=True)
    (path / "logs").mkdir()
    (path / "inputs/candidate/checkout").mkdir(parents=True)
    (path / "inputs/candidate/checkout/source.py").write_text("temporary source")
    (path / "sealed").mkdir()
    (path / "artifacts/report.json").write_text("result")
    (path / "logs/command.log").write_text("private log")
    (path / "sealed/result.json").write_text('{"verdict":"pass"}')
    (path / "state.json").write_text(
        json.dumps({"phase": phase, "delivery": {"published": published}})
    )
    return path


def test_sha_directory_health_and_retention_keep_internal_task_identity(tmp_path):
    run = make_run(tmp_path, "pending", "publish_pending", None, "push/branch-main")
    sha_parent = run.parent.with_name("b" * 40)
    run.parent.rename(sha_parent)
    run = sha_parent / run.name
    record = json.loads((run / "state.json").read_text())
    record.update(task_id="a" * 64, head_sha="b" * 40)
    (run / "state.json").write_text(json.dumps(record))
    config = {"state_dir": str(tmp_path)}
    report = retain_local(config, apply=False)
    assert report["protected"] == [{"task_id": "a" * 64, "run_id": "pending"}]
    snapshot = health.collect(
        {**config, "monitor_services": []}, manager=SimpleNamespace(health=lambda: {}),
    )
    assert snapshot["active_task"]["task_id"] == "a" * 64


@pytest.mark.parametrize("layout", RUN_LAYOUTS, ids=["legacy", "pr", "push"])
def test_retention_expires_only_published_and_preserves_summary(tmp_path, layout):
    now = 40 * 86400
    expired = make_run(tmp_path, "old", "published", 86400, layout)
    # Earlier cleanup left the input clones behind even after recording expiry.
    (expired / "retention.json").write_text("{}")
    pending = make_run(tmp_path, "pending", "publish_pending", 86400, layout)
    fresh = make_run(tmp_path, "fresh", "published", now - 86400, layout)
    report = retain_local(
        {"state_dir": str(tmp_path), "state_min_free_bytes": 0}, now=now
    )
    assert len(report["expired"]) == 1 and not report["pause_intake"]
    assert not (expired / "artifacts").exists()
    assert not (expired / "inputs").exists()
    assert (expired / "sealed/result.json").exists()
    assert (pending / "artifacts/report.json").exists()
    assert (pending / "inputs/candidate/checkout/source.py").exists()
    assert (fresh / "logs/command.log").exists()


def test_retention_rejects_linked_branch_directory(tmp_path):
    external = tmp_path / "external"
    run = make_run(external, "old", "published", 86400)
    branch = tmp_path / "runs/pr/branch-main"
    branch.mkdir(parents=True)
    (branch / "pr-56").symlink_to(external / "runs", target_is_directory=True)
    report = retain_local(
        {"state_dir": str(tmp_path), "state_min_free_bytes": 0}, now=40 * 86400
    )
    assert report["errors"] == [{"run": "old", "reason": "symlink"}]
    assert (run / "artifacts/report.json").exists()


@pytest.mark.parametrize("layout", RUN_LAYOUTS, ids=["legacy", "pr", "push"])
def test_health_reports_active_runs_in_each_layout(tmp_path, layout):
    make_run(tmp_path, "active", "running", 0, layout)
    make_run(tmp_path, "pending", "publish_pending", 0, layout)
    make_run(tmp_path, "done", "published", 1, layout)
    report = health.collect(
        {"state_dir": str(tmp_path), "monitor_services": []},
        manager=SimpleNamespace(health=lambda: {}),
    )
    assert {row["run_id"] for row in report["tasks"]} == {"active", "pending"}
    assert report["active_task"]["task_id"] == "a" * 64
    assert report["active_task"]["run_id"] == "active"
    assert report["uploads"][0]["task_id"] == "a" * 64


def test_retention_budget_pauses_without_deleting_required_pending(tmp_path):
    pending = make_run(tmp_path, "pending", "publish_pending", 0)
    report = retain_local(
        {"state_dir": str(tmp_path), "evidence_max_bytes": 1, "state_min_free_bytes": 0}
    )
    assert report["pause_intake"]
    assert (pending / "artifacts/report.json").exists()
    assert (pending / "inputs/candidate/checkout/source.py").exists()


def test_health_whitelist_removes_nested_private_configuration():
    private = "PRIVATE_SENTINEL"
    result = public_snapshot(
        {
            "worker_id": "worker-1",
            "collected_at": "2026-09-10T00:00:00Z",
            "config": private,
            "runtime": {"endpoint": private, "available": True, "env": private},
            "environments": {
                "images": [{"env": private}],
                "runtime": {"endpoint": private},
            },
            "tasks": [
                {
                    "task_id": "a" * 64,
                    "run_id": "run-1",
                    "stage": "running",
                    "config": private,
                }
            ],
            "images": [
                {
                    "release_id": "image-1",
                    "image_id": "sha256:" + "a" * 64,
                    "state": "ready",
                    "env": private,
                    "log_path": private,
                }
            ],
            "storage": [{"filesystem_free_bytes": 10, "path": private}],
        }
    )
    assert private not in json.dumps(result)
    assert result["runtime"]["available"]


def test_health_defaults_do_not_monitor_removed_control_update_timer(tmp_path):
    manager = SimpleNamespace(health=lambda: {"images": [], "attempts": []})
    shown = []

    def show_service(argv, **kwargs):
        shown.append(argv[3])
        return SimpleNamespace(
            returncode=0,
            stdout="LoadState=loaded\nActiveState=active\nSubState=running\nResult=success\n",
        )

    with patch.object(health.subprocess, "run", side_effect=show_service):
        snapshot = health.collect(
            {"state_dir": str(tmp_path)}, now=1, manager=manager
        )
    assert "triton-anchor-local-ci-control-update.timer" not in shown
    assert "triton-anchor-local-ci-control-update.service" in shown
    assert snapshot["control_update"]["state"] == "idle"


def test_health_reports_failed_pending_control_update(tmp_path):
    manager = SimpleNamespace(health=lambda: {"images": [], "attempts": []})
    request = tmp_path / "control-update/request.json"
    request.parent.mkdir(parents=True)
    request.write_text(
        json.dumps(
            {
                "schema": "triton-anchor-local-ci-control-update-request",
                "revision": "a" * 40,
                "task_id": "b" * 64,
                "requested_at": 10,
            }
        )
    )
    request.chmod(0o600)

    def show_service(argv, **kwargs):
        failed = argv[3] == "triton-anchor-local-ci-control-update.service"
        return SimpleNamespace(
            returncode=0,
            stdout=(
                "LoadState=loaded\n"
                + ("ActiveState=failed\nSubState=failed\nResult=exit-code\n" if failed else "ActiveState=active\nSubState=running\nResult=success\n")
            ),
        )

    with patch.object(health.subprocess, "run", side_effect=show_service):
        snapshot = health.collect(
            {"state_dir": str(tmp_path)}, now=20, manager=manager
        )
    assert snapshot["control_update"] == {
        "state": "failed",
        "requested_revision": "a" * 40,
        "task_id": "b" * 64,
        "requested_at": "1970-01-01T00:00:10Z",
    }


def test_health_reports_worker_blocked_control_update(tmp_path):
    worker = tmp_path / "health/worker.json"
    worker.parent.mkdir(parents=True)
    worker.write_text(
        json.dumps(
            {
                "heartbeat_at": 10,
                "pid": os.getpid(),
                "control_update": "blocked",
            }
        )
    )
    manager = SimpleNamespace(health=lambda: {"images": [], "attempts": []})
    snapshot = health.collect(
        {"state_dir": str(tmp_path), "monitor_services": []},
        now=20,
        manager=manager,
    )
    assert snapshot["control_update"]["state"] == "blocked"


def snapshot(now):
    return {
        "worker_id": "worker-1",
        "collected_at": now.isoformat(),
        "state": "healthy",
        "poller": {"alive": True, "heartbeat_stale": False},
        "runtime": {"available": True, "rootless": True},
    }


def test_watchdog_unknown_does_not_clear_incident_or_claim_offline():
    now = datetime(2026, 9, 10, tzinfo=timezone.utc)
    broken = snapshot(now)
    broken["poller"]["alive"] = False
    first = evaluate({"workers": [broken]}, now=now)
    unknown = evaluate(
        {"source_error": True, "expected_workers": ["worker-1"]}, first, now=now
    )
    assert unknown["source_state"] == "unknown"
    assert unknown["active"] == first["active"]
    assert not any(r["code"] == "snapshot_stale" for r in unknown["active"].values())
    recovered = evaluate({"workers": [snapshot(now)]}, unknown, now=now)
    assert not recovered["active"]
    assert recovered["history"][-1]["transition"] == "recovered"
    assert (
        len(
            evaluate(
                {"workers": [snapshot(now)]},
                {"active": {}, "history": [{}] * 110},
                now=now,
            )["history"]
        )
        == 100
    )


def test_retention_protects_unstarted_null_delivery(tmp_path):
    run = make_run(tmp_path, "preparing", "preparing", 0)
    (run / "state.json").write_text(
        json.dumps({"phase": "preparing", "delivery": None})
    )
    report = retain_local({"state_dir": str(tmp_path), "state_min_free_bytes": 0})
    assert report["protected"] == [{"task_id": "a" * 64, "run_id": "preparing"}]
    assert not report["errors"]
