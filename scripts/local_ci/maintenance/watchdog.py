#!/usr/bin/env python3
"""External watchdog: public Gitee snapshots to Gitee incident Issues."""

from __future__ import annotations
import argparse
import base64
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from maintenance.gitee_issues import GiteeIssueError, repository_coordinates, sync_issues
from maintenance.health import public_snapshot

SCHEMA = "triton-anchor-local-ci-watchdog"


def timestamp(value):
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return result.astimezone(timezone.utc) if result.tzinfo else None
    except (AttributeError, ValueError):
        return None


def evaluate(
    document,
    previous=None,
    *,
    now=None,
    stale_seconds=1200,
    upload_seconds=1200,
    progress_seconds=1800,
    disk_free_bytes=5 * 1024**3,
    **unused,
):
    now = now or datetime.now(timezone.utc)
    previous = previous or {"active": {}, "history": []}
    unreadable = set(document.get("unreadable_workers", []))
    observed, unknown, workers = {}, set(unreadable), []
    source_error = bool(document.get("source_error"))
    expected = document.get("expected_workers", [])
    if source_error:
        unknown.update(expected)
        unknown.update(r.get("worker_id") for r in previous.get("active", {}).values())
    snapshots = document.get("workers", [document] if document.get("worker_id") else [])
    for raw in snapshots:
        worker = public_snapshot(raw)
        workers.append(worker)
        ident = worker["worker_id"]
        collected = timestamp(worker.get("collected_at"))

        def incident(code):
            key = ident + ":" + code
            observed[key] = {"key": key, "worker_id": ident, "code": code}

        if collected is None or (now - collected).total_seconds() > stale_seconds:
            incident("snapshot_stale")
            unknown.add(ident)
            continue
        if not worker["poller"]["alive"] or worker["poller"]["heartbeat_stale"]:
            incident("poller_unavailable")
        if not worker["runtime"]["available"]:
            incident("runtime_unavailable")
        if worker["poller"]["last_poll_status"] == "error":
            incident("relay_poll_failed")
        if worker["environments"]["unavailable"]:
            incident("environment_unavailable")
        if any(row["state"] == "failed" for row in worker["images"]):
            incident("environment_update_failed")
        if any(
            (row.get("filesystem_free_bytes") or 0) < disk_free_bytes
            for row in worker["storage"]
        ):
            incident("disk_space_low")
        for row in worker["uploads"]:
            queued = timestamp(row.get("queued_at"))
            if queued is None or (now - queued).total_seconds() > upload_seconds:
                incident("delivery_pending")
        active = worker.get("active_task")
        if active and active["stage"] == "running":
            progress = timestamp(active.get("last_progress_at"))
            if progress and (now - progress).total_seconds() > progress_seconds:
                incident("task_no_progress")
    if not source_error:
        seen = {w["worker_id"] for w in workers}
        for ident in set(expected) - seen - unreadable:
            unknown.add(ident)
            observed[ident + ":snapshot_stale"] = {
                "key": ident + ":snapshot_stale",
                "worker_id": ident,
                "code": "snapshot_stale",
            }
    at = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    active, history = {}, list(previous.get("history", []))
    for key, row in observed.items():
        prior = previous.get("active", {}).get(key)
        active[key] = {
            **row,
            "first_detected_at": prior["first_detected_at"] if prior else at,
            "last_seen_at": at,
        }
        if not prior:
            history.append({"at": at, "key": key, "transition": "opened"})
    for key, prior in previous.get("active", {}).items():
        if key in observed:
            continue
        if prior.get("worker_id") in unknown:
            active[key] = prior
        else:
            history.append({"at": at, "key": key, "transition": "recovered"})
    source_state = "unknown" if source_error else "partial" if unreadable else "readable"
    return {
        "schema": SCHEMA,
        "updated_at": at,
        "source_state": source_state,
        "unreadable_workers": sorted(worker for worker in unreadable if worker),
        "unknown_workers": sorted(worker for worker in unknown if worker),
        "active": active,
        "history": history[-100:],
        "worker_health": workers,
        "healthy": not active and not unknown and not source_error,
    }


def configured_workers(config):
    workers = config.get("health_workers")
    if workers is None:
        workers = [config.get("worker_id", "local-ci")]
    if (
        not isinstance(workers, list)
        or not workers
        or len(set(workers)) != len(workers)
        or not all(isinstance(row, str) and re.fullmatch(r"[A-Za-z0-9_.-]+", row) for row in workers)
    ):
        raise ValueError("health_workers must be a non-empty list of unique safe worker IDs")
    return workers


def read_snapshot(config):
    owner, name = repository_coordinates(config["health_repo_url"])
    repo = owner + "/" + name
    expected, workers, unreadable, missing = configured_workers(config), [], [], []
    for worker_id in expected:
        branch = "snapshot/" + worker_id
        url = (
            "https://gitee.com/api/v5/repos/"
            + repo
            + "/contents/worker-health.json?"
            + urllib.parse.urlencode({"ref": branch})
        )
        try:
            with urllib.request.urlopen(url, timeout=30) as response:
                value = json.load(response)
            if value.get("encoding") == "base64":
                value = json.loads(base64.b64decode(value["content"]))
            if value.get("schema") != "triton-anchor-worker-health":
                raise ValueError("Unexpected worker health schema")
            if value.get("worker_id") != worker_id:
                raise ValueError("Worker health snapshot is on the wrong branch")
            workers.append(value)
        except urllib.error.HTTPError as exc:
            (missing if exc.code == 404 else unreadable).append(worker_id)
        except (OSError, ValueError):
            unreadable.append(worker_id)
    return {
        "workers": workers,
        "expected_workers": expected,
        "unreadable_workers": unreadable,
        "source_error": bool(unreadable) and not workers and not missing,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--input")
    parser.add_argument("--sync-issues", action="store_true")
    args = parser.parse_args()
    config = json.loads(Path(args.config).read_text())
    document = (
        json.loads(Path(args.input).read_text())
        if args.input
        else read_snapshot(config)
    )
    state = evaluate(
        document, stale_seconds=config.get("health_stale_seconds", 1200)
    )
    if args.sync_issues:
        state["issue_sync"] = sync_issues(config, state)
    print(json.dumps(state, ensure_ascii=False, indent=2))
    return 2 if state["unreadable_workers"] else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, GiteeIssueError) as exc:
        print(f"External Local CI watchdog failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
