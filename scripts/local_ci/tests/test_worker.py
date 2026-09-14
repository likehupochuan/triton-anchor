"""Task lifecycle: execute once, stop before collecting, retry publication only."""

import fcntl
import hashlib
import json
from pathlib import Path

import pytest

from agent_ci.protocol import TASK_SCHEMA, atomic_json, metadata_digest, task_id
from agent_ci.worker import Worker, scan_once, trigger_control_update


def manifest():
    value = {
        "schema": TASK_SCHEMA,
        "repository": "likehupochuan/triton-anchor",
        "event_kind": "pull_request",
        "pr_number": 7,
        "target_branch": "main",
        "tested_sha": "a" * 40,
        "base_sha": "b" * 40,
        "head_sha": "c" * 40,
        "worker_revision_sha": "d" * 40,
        "llvm_hash": "e" * 40,
        "full": False,
        "draft": False,
        "state": "open",
        "title": "Fix behavior",
        "description": "Implementation and verification",
        "labels": [],
        "captured_at": "2026-09-11T00:00:00Z",
    }
    value["metadata_digest"] = metadata_digest(value)
    value["task_id"] = task_id(value)
    prefix = f"ci/pr-7/{value['task_id']}"
    value.update(
        task_ref=prefix + "/tested",
        base_task_ref=prefix + "/base",
        head_task_ref=prefix + "/head",
    )
    return value


def revised_manifest(revision, captured_at):
    value = manifest()
    value["worker_revision_sha"] = revision
    value["captured_at"] = captured_at
    value["task_id"] = task_id(value)
    prefix = f"ci/pr-7/{value['task_id']}"
    value.update(
        task_ref=prefix + "/tested",
        base_task_ref=prefix + "/base",
        head_task_ref=prefix + "/head",
    )
    return value


@pytest.mark.parametrize("cancelled", [False, True])
def test_run_stops_collects_and_publishes_without_reexecuting_on_network_failure(
    tmp_path, monkeypatch, cancelled
):
    task = manifest()
    events = []
    calls = 0
    uploads = 0

    class Relay:
        def refresh(self):
            pass

        def validity(self, task):
            return True, ""

        def tasks(self):
            return [task]

        def publish_result(self, task, run_id, directory):
            nonlocal uploads
            uploads += 1
            if uploads == 1:
                raise OSError("temporary relay outage")
            return hashlib.sha256((directory / "result.json").read_bytes()).hexdigest()

    class Manager:
        def generations(self):
            return {}

        def collect_retired(self):
            pass

        def acquire_task(self, task, run_id):
            return {
                "run_id": run_id,
                "profile": "fixture",
                "backend_enabled": False,
                "environment_fingerprint": "fixture",
                "llvm_hash": task["llvm_hash"],
                "image_id": "fixture",
            }

        def stop_task(self, handle):
            events.append("stop")

        def collect_artifacts(self, handle):
            assert events[-1] == "stop"
            events.append("collect")

        def destroy_task(self, handle):
            events.append("destroy")

    class Executor:
        def __init__(self, config, state_dir, generation, task, relay, *, manager):
            self.run_dir = (
                Path(state_dir) / "runs" / task["task_id"] / generation["run_id"]
            )

        def prepare(self, variant="candidate"):
            return tmp_path

        def write_context(self, policy, changes):
            pass

    class Driver:
        def redact(self, text):
            return text

        def run(self, executor, **kwargs):
            nonlocal calls
            calls += 1
            atomic_json(
                executor.run_dir / "artifacts/agent-result.json",
                {
                    "status": "pass",
                    "summary": "Behavior verified",
                    "checks": [],
                    "reviews": [
                        {"kind": kind, "status": "pass", "summary": "Verified"}
                        for kind in ("pr_info", "architecture")
                    ],
                },
            )
            if cancelled:
                worker.active.cancel("PR closed")
            return {"exit_code": 0, "reason": ""}

    monkeypatch.setattr(
        "agent_ci.worker.changed_files", lambda *args: [{"path": "README.md"}]
    )
    monkeypatch.setattr(
        "agent_ci.worker.minimum_checks",
        lambda *args, **kwargs: {"required_checks": []},
    )
    worker = Worker(
        {"state_dir": str(tmp_path), "simulation": True, "retry_delay_seconds": 0},
        relay=Relay(),
        manager=Manager(),
        driver=Driver(),
        executor_factory=Executor,
    )
    worker.scan()
    row = worker.journal.task(task["task_id"])
    assert row["phase"] == "publish_pending"
    result = json.loads(
        (worker.journal.run_dir(task["task_id"]) / "sealed/result.json").read_text()
    )
    assert result["status"] == ("cancelled" if cancelled else "pass")
    assert events[-3:] == ["stop", "collect", "destroy"]
    worker.scan()
    assert calls == 1 and uploads == 2
    assert worker.journal.task(task["task_id"])["phase"] == "published"
    assert worker.journal.task(task["task_id"])["run_id"] == row["run_id"]


def test_task_waits_for_automatic_control_update(tmp_path):
    task = manifest()

    class Relay:
        def refresh(self):
            pass

        def tasks(self):
            return [task]

        def validity(self, task):
            return True, ""

    class Manager:
        def generations(self):
            return {}

        def collect_retired(self):
            pass

        def current_control_revision(self):
            return "f" * 40

    worker = Worker(
        {"state_dir": str(tmp_path), "simulation": True},
        relay=Relay(),
        manager=Manager(),
        driver=object(),
        control_request_selector=lambda config, current, requests, **kwargs: requests[0],
    )
    request = worker.scan()
    assert worker.journal.tasks() == []
    assert request == {
        "revision": task["worker_revision_sha"],
        "task_id": task["task_id"],
        "captured_at": task["captured_at"],
    }
    health = json.loads((tmp_path / "health/worker.json").read_text())
    assert health["control_update"] == "required"


def test_control_update_request_is_single_atomic_file_and_nonblocking(tmp_path):
    task = manifest()
    calls = []
    unit = trigger_control_update(
        {"state_dir": str(tmp_path)},
        {"revision": task["worker_revision_sha"], "task_id": task["task_id"]},
        run=lambda argv, **kwargs: calls.append((argv, kwargs)),
    )
    request = json.loads((tmp_path / "control-update/request.json").read_text())
    assert request["revision"] == task["worker_revision_sha"]
    assert request["task_id"] == task["task_id"]
    assert unit == "triton-anchor-local-ci-control-update.service"
    assert calls == [
        (
            [
                "systemctl",
                "--user",
                "start",
                "--no-block",
                "triton-anchor-local-ci-control-update.service",
            ],
            {"check": True, "timeout": 30},
        )
    ]

    first_requested_at = request["requested_at"]
    trigger_control_update(
        {"state_dir": str(tmp_path)},
        {"revision": task["worker_revision_sha"], "task_id": task["task_id"]},
        run=lambda argv, **kwargs: None,
    )
    repeated = json.loads((tmp_path / "control-update/request.json").read_text())
    assert repeated["requested_at"] == first_requested_at


def test_scan_releases_control_lock_before_trigger(tmp_path):
    task = manifest()
    events = []

    class FixtureWorker:
        config = {"state_dir": str(tmp_path)}

        def scan(self):
            events.append("scan")
            return {"revision": task["worker_revision_sha"], "task_id": task["task_id"]}

    with (tmp_path / "control.lock").open("w") as control_lock:
        def trigger(config, request):
            fcntl.flock(control_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            events.append("trigger-after-unlock")
            fcntl.flock(control_lock, fcntl.LOCK_UN)

        scan_once(FixtureWorker(), control_lock, trigger=trigger)

    assert events == ["scan", "trigger-after-unlock"]


def test_oldest_waiting_task_deterministically_requests_control_update(tmp_path):
    newer = revised_manifest(
        "1" * 40,
        "2026-09-12T00:00:00Z",
    )
    older = revised_manifest(
        "2" * 40,
        "2026-09-11T00:00:00Z",
    )

    class Relay:
        def refresh(self):
            pass

        def tasks(self):
            return [newer, older]

        def validity(self, task):
            return True, ""

    class Manager:
        def generations(self):
            return {}

        def collect_retired(self):
            pass

        def current_control_revision(self):
            return "f" * 40

    worker = Worker(
        {"state_dir": str(tmp_path), "simulation": True},
        relay=Relay(),
        manager=Manager(),
        driver=object(),
        control_request_selector=lambda config, current, requests, **kwargs: min(
            requests, key=lambda row: (row["captured_at"], row["task_id"])
        ),
    )
    assert worker.scan()["revision"] == older["worker_revision_sha"]


def test_cancelled_and_published_tasks_do_not_block_newer_control_update(tmp_path):
    cancelled = revised_manifest("1" * 40, "2026-09-09T00:00:00Z")
    published = revised_manifest("2" * 40, "2026-09-10T00:00:00Z")
    waiting = revised_manifest("3" * 40, "2026-09-11T00:00:00Z")

    class Relay:
        def refresh(self):
            pass

        def tasks(self):
            return [cancelled, published, waiting]

        def validity(self, task):
            return (False, "cancelled") if task is cancelled else (True, "")

    class Manager:
        def generations(self):
            return {}

        def collect_retired(self):
            pass

        def current_control_revision(self):
            return "f" * 40

    worker = Worker(
        {"state_dir": str(tmp_path), "simulation": True},
        relay=Relay(),
        manager=Manager(),
        driver=object(),
        control_request_selector=lambda config, current, requests, **kwargs: requests[0],
    )
    worker.journal.register(published)
    payload = tmp_path / "published-result.json"
    payload.write_text('{"status":"pass"}')
    worker.journal.queue_result(
        published["task_id"], payload, hashlib.sha256(payload.read_bytes()).hexdigest()
    )
    worker.journal.published(published["task_id"])

    assert worker.scan()["revision"] == waiting["worker_revision_sha"]


def test_worker_uses_startup_revision_even_if_checkout_head_moves(tmp_path):
    task = revised_manifest("e" * 40, "2026-09-11T00:00:00Z")

    class Relay:
        def refresh(self):
            pass

        def tasks(self):
            return [task]

        def validity(self, task):
            return True, ""

    class Manager:
        revision = "d" * 40

        def generations(self):
            return {}

        def collect_retired(self):
            pass

        def current_control_revision(self):
            return self.revision

    manager = Manager()
    worker = Worker(
        {"state_dir": str(tmp_path), "simulation": True},
        relay=Relay(),
        manager=manager,
        driver=object(),
        control_request_selector=lambda config, current, requests, **kwargs: requests[0],
    )
    manager.revision = task["worker_revision_sha"]
    trigger_control_update(
        {"state_dir": str(tmp_path)},
        {"revision": task["worker_revision_sha"], "task_id": task["task_id"]},
        run=lambda argv, **kwargs: None,
    )
    request = worker.scan()
    assert worker.running_control_revision == "d" * 40
    assert request["revision"] == task["worker_revision_sha"]
    assert worker.journal.tasks() == []


def test_restarted_worker_clears_already_satisfied_request(tmp_path):
    revision = "d" * 40
    task = manifest()

    class Relay:
        def refresh(self):
            pass

        def tasks(self):
            return []

    class Manager:
        def generations(self):
            return {}

        def collect_retired(self):
            pass

        def current_control_revision(self):
            return revision

    trigger_control_update(
        {"state_dir": str(tmp_path)},
        {"revision": revision, "task_id": task["task_id"]},
        run=lambda argv, **kwargs: None,
    )
    worker = Worker(
        {"state_dir": str(tmp_path), "simulation": True},
        relay=Relay(),
        manager=Manager(),
        driver=object(),
    )
    assert worker.scan() is None
    assert not (tmp_path / "control-update/request.json").exists()


def test_control_history_fetch_failure_is_reported_as_blocked(tmp_path):
    task = manifest()

    class Relay:
        def refresh(self):
            pass

        def tasks(self):
            return [task]

        def validity(self, task):
            return True, ""

    class Manager:
        def generations(self):
            return {}

        def collect_retired(self):
            pass

        def current_control_revision(self):
            return "f" * 40

    def unavailable(*args, **kwargs):
        raise RuntimeError("control mirror unavailable")

    worker = Worker(
        {"state_dir": str(tmp_path), "simulation": True},
        relay=Relay(),
        manager=Manager(),
        driver=object(),
        control_request_selector=unavailable,
    )
    assert worker.scan() is None
    heartbeat = json.loads((tmp_path / "health/worker.json").read_text())
    assert heartbeat["control_update"] == "blocked"
