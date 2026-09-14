"""Task lifecycle: execute once, stop before collecting, retry publication only."""

import fcntl
import hashlib
import json
from pathlib import Path
import select
import subprocess
import sys

import pytest

from agent_ci.protocol import TASK_SCHEMA, atomic_json, metadata_digest, task_id
from agent_ci.worker import Worker, scan_once, trigger_control_update
from agent_ci.state import Journal, local_run_dir


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
                "artifacts_host": str(local_run_dir(tmp_path, task, run_id) / "artifacts"),
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
            self.run_dir = Path(generation["artifacts_host"]).parent

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
    worker.journal = Journal(tmp_path)
    assert worker.journal.register(task)["run_id"] == row["run_id"]
    directory = worker.journal.run_dir(task["task_id"])
    assert directory == tmp_path / "runs/pr/branch-main/pr-7" / task["head_sha"] / row["run_id"]
    # Simulate a pre-upgrade run: reload and retry its saved upload in place.
    legacy = tmp_path / "runs" / task["task_id"]
    directory.parent.rename(legacy)
    record_path = legacy / row["run_id"] / "state.json"
    record = json.loads(record_path.read_text())
    record["delivery"]["payload_path"] = str(record_path.parent / "sealed/result.json")
    atomic_json(record_path, record)
    worker.journal = Journal(tmp_path)
    worker.scan()
    assert calls == 1 and uploads == 2
    assert worker.journal.task(task["task_id"])["phase"] == "published"
    assert worker.journal.task(task["task_id"])["run_id"] == row["run_id"]
    assert worker.journal.run_dir(task["task_id"]) == record_path.parent
    worker.journal = Journal(tmp_path)
    worker.scan()
    assert calls == 1 and uploads == 2  # Published old runs remain deduplicated.


def test_sha_layout_keeps_distinct_tasks_and_restart_history(tmp_path):
    first = manifest()
    second = revised_manifest("f" * 40, "2026-09-12T00:00:00Z")
    journal = Journal(tmp_path)
    row1, row2 = journal.register(first), journal.register(second)
    directory1 = journal.run_dir(first["task_id"])
    directory2 = journal.run_dir(second["task_id"])
    assert directory1.parent == directory2.parent
    assert directory1 != directory2
    journal.phase(first["task_id"], "published")
    journal = Journal(tmp_path)
    assert journal.task(first["task_id"])["phase"] == "published"
    assert journal.task(second["task_id"])["run_id"] == row2["run_id"]
    assert journal.task(second["task_id"])["head_sha"] == first["head_sha"]
    with pytest.raises(ValueError, match="another task"):
        journal.run_dir(first["task_id"], row2["run_id"])
    journal.restart(second["task_id"])
    assert journal.run_dir(second["task_id"]) != directory2
    assert Journal(tmp_path).task(first["task_id"])["run_id"] == row1["run_id"]


@pytest.mark.parametrize("legacy", ["flat", "grouped"])
def test_new_attempt_uses_sha_but_old_run_remains_readable(tmp_path, legacy):
    task = manifest()
    journal = Journal(tmp_path)
    row = journal.register(task)
    directory = journal.run_dir(task["task_id"])
    old_parent = (tmp_path / "runs" if legacy == "flat" else directory.parent.parent) / task["task_id"]
    directory.parent.rename(old_parent)
    journal = Journal(tmp_path)
    assert journal.run_dir(task["task_id"]) == old_parent / row["run_id"]
    restarted = journal.restart(task["task_id"])
    assert journal.run_dir(task["task_id"]).parent.name == task["head_sha"]
    journal = Journal(tmp_path)
    assert journal.task(task["task_id"])["run_id"] == restarted["run_id"]
    assert journal.run_dir(task["task_id"], row["run_id"]) == old_parent / row["run_id"]


def test_heartbeat_exposes_head_without_replacing_task_identity(tmp_path):
    from types import SimpleNamespace
    task = manifest()
    worker = Worker({"state_dir": str(tmp_path), "simulation": True},
                    relay=object(), manager=object(), driver=object())
    worker.journal.register(task)
    worker.active = SimpleNamespace(task=task)
    worker.heartbeat()
    health = json.loads((tmp_path / "health/worker.json").read_text())
    assert health["head_sha"] == task["head_sha"]
    assert health["tasks"][0]["head_sha"] == task["head_sha"]
    assert health["tasks"][0]["task_id"] == task["task_id"]


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


def test_worker_exits_on_sigterm_while_control_update_holds_lock(tmp_path):
    import fcntl

    config = tmp_path / "config.json"
    config.write_text(json.dumps({"state_dir": str(tmp_path)}))
    program = """
import fcntl, sys, threading
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from agent_ci import worker

class IdleWorker:
    def __init__(self, config):
        self.config = config
        self.state_dir = Path(config['state_dir'])
        self.stop_event = threading.Event()
        self.active = None
    def scan(self):
        raise AssertionError('Worker must not scan during a control update')

flock = fcntl.flock
def observed_flock(stream, operation):
    if operation & fcntl.LOCK_SH:
        print('waiting', flush=True)
    return flock(stream, operation)
fcntl.flock = observed_flock
worker.Worker = IdleWorker
raise SystemExit(worker.main(['--config', sys.argv[2]]))
"""
    with (tmp_path / "control.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        child = subprocess.Popen(
            [
                sys.executable, "-c", program,
                str(Path(__file__).resolve().parents[1]), str(config),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            assert select.select([child.stdout], [], [], 5)[0], (
                "Worker did not reach the control lock"
            )
            assert child.stdout.readline().strip() == "waiting"
            child.terminate()
            _, error = child.communicate(timeout=5)
            assert child.returncode == 0, error
        finally:
            if child.poll() is None:
                child.kill()
                child.communicate()
