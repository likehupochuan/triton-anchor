#!/usr/bin/env python3
"""Synchronize Local CI health transitions to Gitee Issues."""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
import uuid


API_ROOT = "https://gitee.com/api/v5"
MARKER_RE = re.compile(
    r"<!-- triton-anchor-local-ci-health:([A-Za-z0-9_.-]+):([a-z_]+) -->"
)
INCIDENT_NAMES = {
    "snapshot_stale": "health snapshot is stale",
    "poller_unavailable": "relay poller is unavailable",
    "runtime_unavailable": "container runtime is unavailable",
    "relay_poll_failed": "relay polling failed",
    "environment_unavailable": "task environment is unavailable",
    "environment_update_failed": "environment update failed",
    "disk_space_low": "disk space is low",
    "delivery_pending": "result delivery is pending",
    "task_no_progress": "task has stopped making progress",
}


class GiteeIssueError(RuntimeError):
    """A sanitized Gitee Issue API failure."""


def repository_coordinates(repository: str) -> tuple[str, str]:
    parsed = urllib.parse.urlparse(repository)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "gitee.com"
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Issue synchronization requires a credential-free HTTPS Gitee URL")
    parts = parsed.path.removesuffix(".git").strip("/").split("/")
    if len(parts) != 2 or not all(re.fullmatch(r"[A-Za-z0-9_.-]+", p) for p in parts):
        raise ValueError("Health repository must identify one Gitee owner and repository")
    return parts[0], parts[1]


def _multipart(fields: dict[str, object]) -> tuple[bytes, str]:
    boundary = "local-ci-" + uuid.uuid4().hex
    chunks = []
    for name, value in fields.items():
        chunks.extend(
            [
                "--" + boundary,
                f'Content-Disposition: form-data; name="{name}"',
                "",
                str(value),
            ]
        )
    chunks.extend(["--" + boundary + "--", ""])
    return "\r\n".join(chunks).encode(), "multipart/form-data; boundary=" + boundary


class GiteeIssues:
    def __init__(self, repository: str, token: str, *, opener=None):
        self.owner, self.repo = repository_coordinates(repository)
        if not token:
            raise ValueError("Gitee Issue credential is unavailable")
        self.token = token
        self.opener = opener or urllib.request.urlopen

    def _request(self, method: str, path: str, payload=None, *, multipart=False):
        headers = {"Accept": "application/json"}
        url = API_ROOT + path
        data = None
        if method == "GET":
            query = {**(payload or {}), "access_token": self.token}
            url += "?" + urllib.parse.urlencode(query)
        elif multipart:
            data, headers["Content-Type"] = _multipart(
                {**(payload or {}), "access_token": self.token}
            )
        else:
            data = json.dumps({**(payload or {}), "access_token": self.token}).encode()
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with self.opener(request, timeout=30) as response:
                raw = response.read()
            return json.loads(raw) if raw else None
        except urllib.error.HTTPError as exc:
            raise GiteeIssueError(
                f"Gitee Issue API {method} failed with HTTP {exc.code}"
            ) from None
        except (OSError, ValueError):
            raise GiteeIssueError(f"Gitee Issue API {method} returned no usable response") from None

    def open_issues(self) -> list[dict]:
        result = []
        for page in range(1, 101):
            rows = self._request(
                "GET",
                f"/repos/{self.owner}/{self.repo}/issues",
                {"state": "open", "page": page, "per_page": 100},
            )
            if not isinstance(rows, list):
                raise GiteeIssueError("Gitee Issue API returned an invalid issue list")
            result.extend(row for row in rows if isinstance(row, dict))
            if len(rows) < 100:
                return result
        raise GiteeIssueError("Gitee Issue scan exceeded 100 pages")

    def create(self, title: str, body: str) -> dict:
        result = self._request(
            "POST",
            f"/repos/{self.owner}/issues",
            {"repo": self.repo, "title": title, "body": body},
        )
        if not isinstance(result, dict) or not result.get("number"):
            raise GiteeIssueError("Gitee Issue API did not return the created issue number")
        return result

    def close(self, number: str, body: str) -> None:
        self._request(
            "PATCH",
            f"/repos/{self.owner}/issues/{urllib.parse.quote(str(number), safe='')}",
            {"repo": self.repo, "state": "closed", "body": body},
            multipart=True,
        )


def issue_marker(worker_id: str, code: str) -> str:
    return f"<!-- triton-anchor-local-ci-health:{worker_id}:{code} -->"


def incident_body(repository: str, incident: dict) -> str:
    worker = incident["worker_id"]
    code = incident["code"]
    return "\n".join(
        [
            issue_marker(worker, code),
            "## Local CI health incident",
            "",
            f"- Worker: `{worker}`",
            f"- Incident: `{code}`",
            f"- First detected: `{incident['first_detected_at']}`",
            f"- Last observed: `{incident['last_seen_at']}`",
            f"- Snapshot branch: `snapshot/{worker}` (`worker-health.json`)",
            f"- Health repository: {repository.removesuffix('.git')}",
            "",
            "This Issue is maintained automatically. Subscribe to the Issue or watch the health repository for state-change notifications.",
        ]
    )


def sync_issues(config: dict, state: dict, *, client=None) -> dict:
    """Open and recover managed Issues without changing unknown workers."""
    repository = config["health_repo_url"]
    repository_coordinates(repository)
    if client is None:
        token = os.environ.get(config.get("health_token_env", "GITEE_HEALTH_TOKEN"), "")
        client = GiteeIssues(repository, token)
    managed = {}
    duplicates = []
    for issue in client.open_issues():
        match = MARKER_RE.search(str(issue.get("body") or ""))
        if not match:
            continue
        if not issue.get("number"):
            raise GiteeIssueError("Gitee Issue API returned a managed issue without a number")
        key = match.group(1) + ":" + match.group(2)
        if key in managed:
            duplicates.append(str(issue.get("number")))
        else:
            managed[key] = issue

    opened, recovered, preserved = [], [], []
    active = state.get("active", {})
    unknown = set(state.get("unknown_workers", []))
    for key, incident in active.items():
        worker, code = incident.get("worker_id"), incident.get("code")
        if (
            not isinstance(worker, str)
            or not re.fullmatch(r"[A-Za-z0-9_.-]+", worker)
            or code not in INCIDENT_NAMES
            or key != worker + ":" + code
        ):
            raise ValueError("Watchdog state contains an invalid incident")
        if key in managed:
            continue
        name = INCIDENT_NAMES[code]
        created = client.create(
            f"[Local CI][{incident['worker_id']}] {name}",
            incident_body(repository, incident),
        )
        opened.append({"key": key, "number": str(created["number"])})

    for key, issue in managed.items():
        if key in active:
            continue
        worker, _code = key.rsplit(":", 1)
        if worker in unknown:
            preserved.append({"key": key, "number": str(issue.get("number"))})
            continue
        body = str(issue.get("body") or issue_marker(worker, _code))
        body += "\n\n---\nRecovered automatically at `" + state["updated_at"] + "`."
        client.close(str(issue["number"]), body)
        recovered.append({"key": key, "number": str(issue["number"])})
    return {
        "opened": opened,
        "recovered": recovered,
        "preserved_unknown": preserved,
        "duplicate_managed_issues": duplicates,
    }
