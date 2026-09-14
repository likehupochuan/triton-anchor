"""Retention and public health behavior, without remote attachment lifecycle."""
from datetime import datetime, timezone
import base64
from io import BytesIO
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import urllib.error

import pytest
from maintenance import health
from maintenance.gitee_issues import (
    GiteeIssueError,
    GiteeIssues,
    incident_body,
    issue_marker,
    sync_issues,
)
from maintenance.retention import retain_local
from maintenance.health import public_snapshot
from maintenance.watchdog import evaluate, read_snapshot


def make_run(root, run, phase, published):
    path = root / "runs" / ("a" * 64) / run
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


def test_retention_expires_only_published_and_preserves_summary(tmp_path):
    now = 40 * 86400
    expired = make_run(tmp_path, "old", "published", 86400)
    # Earlier cleanup left the input clones behind even after recording expiry.
    (expired / "retention.json").write_text("{}")
    pending = make_run(tmp_path, "pending", "publish_pending", 86400)
    fresh = make_run(tmp_path, "fresh", "published", now - 86400)
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


def snapshot(now):
    return {
        "schema": "triton-anchor-worker-health",
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


class FakeIssues:
    def __init__(self, opened=()):
        self.opened = list(opened)
        self.created = []
        self.closed = []

    def open_issues(self):
        return list(self.opened)

    def create(self, title, body):
        self.created.append((title, body))
        return {"number": "I" + str(len(self.created))}

    def close(self, number, body):
        self.closed.append((number, body))


def watchdog_state(*, active=None, unknown=()):
    return {
        "updated_at": "2026-09-10T00:05:00Z",
        "source_state": "partial" if unknown else "readable",
        "unknown_workers": list(unknown),
        "active": active or {},
    }


def incident(code="poller_unavailable"):
    key = "worker-1:" + code
    return key, {
        "key": key,
        "worker_id": "worker-1",
        "code": code,
        "first_detected_at": "2026-09-10T00:00:00Z",
        "last_seen_at": "2026-09-10T00:05:00Z",
    }


def test_issue_sync_opens_once_and_recovery_closes_with_one_update():
    key, row = incident()
    config = {"health_repo_url": "https://gitee.com/example/health.git"}
    first = FakeIssues()
    actions = sync_issues(config, watchdog_state(active={key: row}), client=first)
    assert actions["opened"] == [{"key": key, "number": "I1"}]
    assert len(first.created) == 1
    assert issue_marker("worker-1", "poller_unavailable") in first.created[0][1]
    assert "PRIVATE_SENTINEL" not in first.created[0][1]

    current = {"number": "I1", "body": first.created[0][1]}
    steady = FakeIssues([current])
    sync_issues(config, watchdog_state(active={key: row}), client=steady)
    assert not steady.created and not steady.closed

    recovery = FakeIssues([current])
    actions = sync_issues(config, watchdog_state(), client=recovery)
    assert actions["recovered"] == [{"key": key, "number": "I1"}]
    assert recovery.closed[0][0] == "I1"
    assert "Recovered automatically" in recovery.closed[0][1]


def test_issue_sync_preserves_incident_when_worker_snapshot_is_unknown():
    key, row = incident()
    body = incident_body("https://gitee.com/example/health.git", row)
    api = FakeIssues([{"number": "I7", "body": body}])
    actions = sync_issues(
        {"health_repo_url": "https://gitee.com/example/health.git"},
        watchdog_state(unknown=["worker-1"]),
        client=api,
    )
    assert actions["preserved_unknown"] == [{"key": key, "number": "I7"}]
    assert not api.created and not api.closed


def test_gitee_issue_api_error_never_exposes_token():
    token = "PRIVATE_TOKEN_SENTINEL"

    def forbidden(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 403, "denied", {}, None)

    api = GiteeIssues(
        "https://gitee.com/example/health.git", token, opener=forbidden
    )
    with pytest.raises(GiteeIssueError) as error:
        api.open_issues()
    assert "HTTP 403" in str(error.value)
    assert token not in str(error.value)


class JsonResponse(BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


def test_external_watchdog_reads_multiple_workers_and_preserves_unreadable_one():
    now = datetime(2026, 9, 10, tzinfo=timezone.utc)
    encoded = base64.b64encode(json.dumps(snapshot(now)).encode()).decode()

    def open_url(url, timeout):
        assert timeout == 30
        if "snapshot%2Fworker-1" in url:
            return JsonResponse(json.dumps({"encoding": "base64", "content": encoded}).encode())
        raise urllib.error.URLError("temporary")

    config = {
        "health_repo_url": "https://gitee.com/example/health.git",
        "health_workers": ["worker-1", "worker-2"],
    }
    with patch("maintenance.watchdog.urllib.request.urlopen", side_effect=open_url):
        document = read_snapshot(config)
    state = evaluate(document, now=now)
    assert [row["worker_id"] for row in state["worker_health"]] == ["worker-1"]
    assert state["source_state"] == "partial"
    assert state["unreadable_workers"] == ["worker-2"]
    assert state["unknown_workers"] == ["worker-2"]
    assert "worker-2:snapshot_stale" not in state["active"]
    assert not state["healthy"]


def test_retention_protects_unstarted_null_delivery(tmp_path):
    run = make_run(tmp_path, "preparing", "preparing", 0)
    (run / "state.json").write_text(
        json.dumps({"phase": "preparing", "delivery": None})
    )
    report = retain_local({"state_dir": str(tmp_path), "state_min_free_bytes": 0})
    assert report["protected"] == [{"task_id": "a" * 64, "run_id": "preparing"}]
    assert not report["errors"]
