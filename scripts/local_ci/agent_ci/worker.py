#!/usr/bin/env python3
"""Poll Gitee, prepare a task for Codex, then publish its selected results."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

LOCAL_ROOT = Path(__file__).resolve().parents[1]
if str(LOCAL_ROOT) not in sys.path:
    sys.path.insert(0, str(LOCAL_ROOT))

from agent_ci.codex import CodexDriver
from agent_ci.delivery import seal_result
from agent_ci.executor import DockerExecutor
from agent_ci.policy import changed_files, minimum_checks
from agent_ci.protocol import ID, SHA, ContractError, atomic_json, is_legacy_task, validate_task
from agent_ci.relay import GitRelay
from agent_ci.state import Journal
from prepare.control_update import (
    REQUEST_SCHEMA,
    oldest_forward_request,
    read_update_request,
    update_request_lock_path,
    update_request_path,
)


CONTROL_UPDATE_UNIT = "triton-anchor-local-ci-control-update.service"


def trigger_control_update(config: dict, request: dict, *, run=subprocess.run) -> str:
    """Start the exact-revision updater after the Worker releases its control lock."""
    import fcntl

    revision = request.get("revision")
    task_id = request.get("task_id")
    if (
        not isinstance(revision, str)
        or not SHA.fullmatch(revision)
        or not isinstance(task_id, str)
        or not ID.fullmatch(task_id)
    ):
        raise ValueError("Control update trigger requires an exact revision")
    request_path = update_request_path(config)
    request_lock = update_request_lock_path(config)
    request_lock.parent.mkdir(parents=True, exist_ok=True)
    with request_lock.open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        requested_at = time.time()
        try:
            existing = read_update_request(request_path)
            if (
                existing["revision"] == revision
                and existing["task_id"] == task_id
                and type(existing.get("requested_at")) in (int, float)
                and existing["requested_at"] >= 0
            ):
                requested_at = existing["requested_at"]
        except (OSError, ValueError):
            pass
        atomic_json(
            request_path,
            {
                "schema": REQUEST_SCHEMA,
                "revision": revision,
                "task_id": task_id,
                "requested_at": requested_at,
            },
        )
    run(
        ["systemctl", "--user", "start", "--no-block", CONTROL_UPDATE_UNIT],
        check=True,
        timeout=30,
    )
    return CONTROL_UPDATE_UNIT


def complete_current_control_request(config: dict, current_revision: str | None) -> bool:
    """Remove a request already satisfied by this running Worker revision."""
    import fcntl

    if not isinstance(current_revision, str) or not SHA.fullmatch(current_revision):
        return False
    request_path = update_request_path(config)
    request_lock = update_request_lock_path(config)
    request_lock.parent.mkdir(parents=True, exist_ok=True)
    with request_lock.open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if not request_path.exists():
            return False
        try:
            request = read_update_request(request_path)
        except (OSError, ValueError):
            return False
        if request["revision"] != current_revision:
            return False
        request_path.unlink()
        return True


class ActiveTask:
    def __init__(self, task, manager):
        self.task, self.manager = task, manager
        self.cancelled = threading.Event()
        self.generation = None
        self.reason = ""

    def cancel(self, reason):
        self.reason = reason
        self.cancelled.set()
        if self.generation is not None:
            self.manager.stop_task(self.generation)


class Worker:
    def __init__(
        self,
        config,
        *,
        relay=None,
        manager=None,
        driver=None,
        executor_factory=None,
        control_request_selector=oldest_forward_request,
    ):
        self.config, self.state_dir = config, Path(config["state_dir"])
        self.journal = Journal(self.state_dir)
        self.relay = relay or GitRelay(
            config["gitee_repo_url"],
            self.state_dir / "relay",
            allow_local=config.get("simulation", False),
        )
        if manager is None:
            from prepare.runtime import EnvironmentManager

            manager = EnvironmentManager(config, self.state_dir)
        self.manager = manager
        self.running_control_revision = getattr(
            manager, "current_control_revision", lambda: None
        )()
        self.driver = driver or CodexDriver(config, self.state_dir)
        self.executor_factory = executor_factory or DockerExecutor
        self.control_request_selector = control_request_selector
        self.stop_event = threading.Event()
        self.active = None

    def heartbeat(self, **extra):
        atomic_json(
            self.state_dir / "health/worker.json",
            {
                "schema": "triton-anchor-worker-health",
                "worker_id": self.config.get("worker_id", "local-ci"),
                "heartbeat_at": time.time(),
                "pid": os.getpid(),
                "tasks": [
                    {k: r[k] for k in ("task_id", "phase", "updated")}
                    for r in self.journal.tasks()
                ],
                **extra,
            },
        )

    def watch(self, active, done):
        while not done.wait(self.config.get("poll_interval_seconds", 60)):
            try:
                self.relay.refresh()
                valid, reason = self.relay.validity(active.task)
                if not valid:
                    active.cancel(reason)
                self.heartbeat(active_task=active.task["task_id"])
            except Exception as exc:
                self.journal.event(
                    active.task["task_id"], "poll_error", {"error": str(exc)}
                )
                self.heartbeat(control_channel="unreachable")

    def queue_sealed(self, task_id, path):
        self.journal.queue_result(
            task_id, path, hashlib.sha256(path.read_bytes()).hexdigest()
        )

    def process(self, task):
        if is_legacy_task(task):
            return
        validate_task(
            task,
            tuple(self.config.get("repositories", ["likehupochuan/triton-anchor"])),
        )
        valid, _ = self.relay.validity(task)
        if not valid:
            return
        row = self.journal.register(task)
        if row["phase"] in {"publish_pending", "published"}:
            return
        run_dir = self.journal.run_dir(task["task_id"])
        sealed = run_dir / "sealed" / "result.json"
        if sealed.is_file():
            self.queue_sealed(task["task_id"], sealed)
            return
        if row["phase"] == "running" or json.loads(row["detail"]).get("started"):
            row = self.journal.restart(task["task_id"])
            run_dir = self.journal.run_dir(task["task_id"])
        self.journal.phase(task["task_id"], "preparing", {"started": True})
        active = ActiveTask(task, self.manager)
        self.active = active
        self.manager.cancel_event = active.cancelled
        done = threading.Event()
        watcher = threading.Thread(target=self.watch, args=(active, done), daemon=True)
        watcher.start()
        generation = None
        policy, environment = {}, {}
        report = {"status": "infra_error", "summary": "任务未完成"}
        try:
            generation = self.manager.acquire_task(task, row["run_id"])
            active.generation = generation
            if active.cancelled.is_set():
                raise InterruptedError(active.reason)
            executor = self.executor_factory(
                self.config,
                self.state_dir,
                generation,
                task,
                self.relay,
                manager=self.manager,
            )
            checkout = executor.prepare()
            changes = changed_files(checkout, task["base_sha"], task["tested_sha"])
            if not changes and task["event_kind"] != "pull_request":
                changes = [
                    {
                        "path": "branch-validation",
                        "old_path": "branch-validation",
                        "mode": "100644",
                    }
                ]
            policy = minimum_checks(
                changes,
                backend_enabled=generation["backend_enabled"],
                full=task["full"],
            )
            environment = {
                key: generation[key]
                for key in (
                    "profile",
                    "llvm_hash",
                    "backend_enabled",
                    "environment_fingerprint",
                    "image_id",
                )
            }
            # Both source identities are available offline; Codex chooses whether to build a baseline.
            executor.prepare("base")
            executor.write_context(policy, changes)
            self.journal.phase(task["task_id"], "running")
            deadline = time.monotonic() + self.config.get(
                "codex_timeout_seconds", 21600
            )
            completed = False
            for attempt in range(self.config.get("codex_attempts", 3)):
                if active.cancelled.is_set():
                    break
                try:
                    outcome = self.driver.run(
                        executor,
                        cancelled=active.cancelled,
                        deadline=deadline,
                        recovery="Resume the task. Inspect the running processes, saved plan and existing results before retrying work."
                        if attempt
                        else "",
                    )
                    self.journal.event(task["task_id"], "codex_exit", outcome)
                    if outcome["reason"] == "timeout":
                        raise TimeoutError("Codex task time budget exhausted")
                    if (
                        outcome["exit_code"] == 0
                        and (run_dir / "artifacts/agent-result.json").is_file()
                    ):
                        completed = True
                        break
                except TimeoutError:
                    raise
                except Exception as exc:
                    self.journal.event(
                        task["task_id"], "codex_error", {"error": str(exc)}
                    )
                if attempt + 1 < self.config.get("codex_attempts", 3):
                    active.cancelled.wait(self.config.get("retry_delay_seconds", 30))
            if not completed:
                raise ContractError(
                    "Codex未完成任务或未生成最终结果；请查看本机Codex日志"
                )
            report = None
            self.relay.refresh()
            valid, reason = self.relay.validity(task)
            if not valid:
                active.cancel(reason)
        except Exception as exc:
            report = {"status": "infra_error", "summary": str(exc)}
            self.journal.event(task["task_id"], "task_error", {"error": str(exc)})
        finally:
            done.set()
            watcher.join(timeout=5)
            self.manager.cancel_event = None
            try:
                if generation is not None:
                    self.manager.stop_task(generation)
                    self.manager.collect_artifacts(generation)
                    self.manager.destroy_task(generation)
            finally:
                shutil.rmtree(run_dir / "inputs", ignore_errors=True)
                self.active = None
        # Shutdown leaves an interrupted run for the next Worker start, not a PR failure.
        if self.stop_event.is_set():
            return
        if active.cancelled.is_set():
            report = {"status": "cancelled", "summary": active.reason}
        elif report is None:
            try:
                report = json.loads(
                    (run_dir / "artifacts/agent-result.json").read_text()
                )
            except (ValueError, OSError) as exc:
                report = {
                    "status": "infra_error",
                    "summary": "Codex结果无法读取：" + str(exc),
                }
        try:
            seal_result(
                task,
                row["run_id"],
                report,
                policy,
                environment,
                run_dir,
                run_dir / "sealed",
                redact=self.driver.redact,
            )
        except (ValueError, OSError, TypeError) as exc:
            seal_result(
                task,
                row["run_id"],
                {"status": "infra_error", "summary": "无法完成结果汇总：" + str(exc)},
                policy,
                environment,
                run_dir,
                run_dir / "sealed",
                redact=self.driver.redact,
            )
        self.queue_sealed(task["task_id"], run_dir / "sealed/result.json")
        self.heartbeat()

    def retry_delivery(self, row):
        box = self.journal.delivery(row["task_id"])
        path = Path(box["payload_path"])
        try:
            if hashlib.sha256(path.read_bytes()).hexdigest() != box["digest"]:
                raise ContractError("Saved result changed before publication")
            task = json.loads(row["manifest"])
            digest = self.relay.publish_result(task, row["run_id"], path.parent)
            if digest != box["digest"]:
                raise ContractError("Published result differs from saved result")
            self.journal.published(row["task_id"])
        except Exception as exc:
            self.journal.publication_failure(row["task_id"])
            self.journal.event(row["task_id"], "publication_error", {"error": str(exc)})

    def scan(self):
        complete_current_control_request(
            self.config, self.running_control_revision
        )
        installed_revision = getattr(
            self.manager, "current_control_revision", lambda: None
        )()
        if (
            self.running_control_revision
            and installed_revision
            and installed_revision != self.running_control_revision
        ):
            try:
                pending = read_update_request(update_request_path(self.config))
            except (OSError, ValueError):
                pending = None
            if pending and pending["revision"] == installed_revision:
                request = {
                    "revision": pending["revision"],
                    "task_id": pending["task_id"],
                }
                self.heartbeat(
                    control_revision=self.running_control_revision,
                    installed_control_revision=installed_revision,
                    requested_control_revision=pending["revision"],
                    control_update="restart_required",
                )
                return request
            self.heartbeat(
                control_revision=self.running_control_revision,
                installed_control_revision=installed_revision,
                control_update="blocked",
                error="Installed control changed without a matching update request",
            )
            return None
        # Publication retries do not depend on Docker or rebuild the tested code.
        attempted = set()
        for row in self.journal.tasks():
            task = json.loads(row["manifest"])
            if not is_legacy_task(task) and row["phase"] == "publish_pending":
                self.retry_delivery(row)
                attempted.add(row["task_id"])
        for handle in self.manager.generations().values():
            if handle["state"] != "removed":
                self.manager.destroy_task(handle)
        self.manager.collect_retired()
        from maintenance.retention import retain_local

        if retain_local(self.config)["pause_intake"]:
            self.heartbeat(runtime="disk_budget_exceeded")
            return
        self.relay.refresh()
        waiting = []
        current_revision = self.running_control_revision
        for task in self.relay.tasks():
            if self.stop_event.is_set():
                break
            if is_legacy_task(task):
                continue
            try:
                validate_task(
                    task,
                    tuple(
                        self.config.get(
                            "repositories", ["likehupochuan/triton-anchor"]
                        )
                    ),
                )
                try:
                    local = self.journal.task(task["task_id"])
                except ContractError:
                    local = None
                if local and local["phase"] in {"publish_pending", "published"}:
                    continue
                valid, _ = self.relay.validity(task)
                if not valid:
                    continue
                if current_revision and task.get("worker_revision_sha") != current_revision:
                    waiting.append(
                        {
                            "revision": task["worker_revision_sha"],
                            "task_id": task["task_id"],
                            "captured_at": task["captured_at"],
                        }
                    )
                    continue
                self.process(task)
                if not self.journal.has_task(task["task_id"]):
                    continue
                row = self.journal.task(task["task_id"])
                if (
                    row["phase"] == "publish_pending"
                    and row["task_id"] not in attempted
                ):
                    self.retry_delivery(row)
            except Exception as exc:
                self.journal.event(
                    task.get("task_id", "invalid"), "task_error", {"error": str(exc)}
                )
                self.heartbeat(error=str(exc))
        if waiting:
            try:
                request = self.control_request_selector(
                    self.config,
                    current_revision,
                    waiting,
                    allow_local=self.config.get("simulation", False),
                )
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
                self.heartbeat(
                    control_revision=current_revision,
                    control_update="blocked",
                    error=str(exc),
                )
                return None
            self.heartbeat(
                control_revision=current_revision,
                requested_control_revision=request["revision"],
                control_update="required",
            )
            return request
        else:
            self.heartbeat()
            return None


def scan_once(worker: Worker, control_lock, *, trigger=trigger_control_update):
    """Scan under a shared lock, then trigger any required update after unlocking."""
    import fcntl

    while True:
        try:
            fcntl.flock(control_lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
            break
        except BlockingIOError:
            # The updater holds the lock while restarting this Worker.
            if worker.stop_event.wait(1):
                return None
    try:
        request = worker.scan()
    finally:
        fcntl.flock(control_lock, fcntl.LOCK_UN)
    if request:
        trigger(worker.config, request)
    return request


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--config",
        default=os.environ.get("LOCAL_CI_CONFIG_JSON", "/opt/local-ci/config.json"),
    )
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--resume", metavar="TASK_ID")
    args = parser.parse_args(argv)
    worker = Worker(json.loads(Path(args.config).read_text()))
    import fcntl

    with (worker.state_dir / "poll.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("Another worker owns the poll lock", file=sys.stderr)
            return 2
        if args.resume:
            worker.journal.resume(args.resume)
            return 0

        def stop(signum, frame):
            worker.stop_event.set()
            if worker.active:
                worker.active.cancel("worker_shutdown")

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        with (worker.state_dir / "control.lock").open("w") as control_lock:
            while not worker.stop_event.is_set():
                try:
                    scan_once(worker, control_lock)
                except Exception as exc:
                    worker.heartbeat(error=str(exc))
                if args.once:
                    break
                worker.stop_event.wait(worker.config.get("poll_interval_seconds", 60))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
