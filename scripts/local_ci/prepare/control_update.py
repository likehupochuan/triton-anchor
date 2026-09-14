#!/usr/bin/env python3
"""Safely fast-forward the dedicated Local CI control checkout from Gitee."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.parse
from pathlib import Path
from typing import Callable

LOCAL_ROOT = Path(__file__).resolve().parents[1]
if str(LOCAL_ROOT) not in sys.path:
    sys.path.insert(0, str(LOCAL_ROOT))

from prepare.artifacts import SHA_RE, atomic_json, safe_source, utc_now


UPDATE_SCHEMA = "triton-anchor-local-ci-control-update"
WORKER_SERVICE = "triton-anchor-local-ci.service"


def validate_control_source(value: object, *, allow_local: bool = False) -> str:
    source = safe_source(value, "control_repo_url")
    parsed = urllib.parse.urlparse(source)
    if parsed.scheme == "https":
        if parsed.hostname != "gitee.com":
            raise ValueError("control_repo_url must be a credential-free Gitee HTTPS URL")
    elif not allow_local or parsed.scheme not in {"", "file"}:
        raise ValueError("Local control repositories are allowed only in tests")
    return source


def _environment(state_dir: Path) -> dict[str, str]:
    environment = {
        **os.environ,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    if os.getenv("GITEE_TOKEN"):
        private = state_dir / "control-update"
        private.mkdir(parents=True, exist_ok=True)
        private.chmod(0o700)
        askpass = private / "askpass.py"
        askpass.write_text(
            "#!/usr/bin/env python3\n"
            "import os,sys\n"
            "print(os.environ.get('GITEE_USERNAME','oauth2') "
            "if 'Username' in sys.argv[1] else os.environ['GITEE_TOKEN'])\n"
        )
        askpass.chmod(0o700)
        environment["GIT_ASKPASS"] = str(askpass)
    return environment


def _git(
    root: Path,
    arguments: list[str],
    environment: dict[str, str],
    *,
    check: bool = True,
) -> subprocess.CompletedProcess:
    result = subprocess.run(
        [
            "git",
            "-c",
            f"safe.directory={root.resolve()}",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "core.fsmonitor=false",
            *arguments,
        ],
        cwd=root,
        env=environment,
        text=True,
        capture_output=True,
        timeout=180,
    )
    if check and result.returncode:
        detail = result.stderr[-2000:]
        for name, value in environment.items():
            if any(word in name.upper() for word in ("TOKEN", "PASSWORD", "SECRET")):
                if len(value) > 3:
                    detail = detail.replace(value, "[redacted]")
        raise RuntimeError(
            f"Control git {arguments[0]} failed with exit {result.returncode}: "
            f"{detail.strip()}"
        )
    return result


def _restart_worker() -> None:
    subprocess.run(
        ["systemctl", "--user", "restart", WORKER_SERVICE],
        check=True,
        timeout=120,
    )


def update_control(
    config: dict,
    *,
    apply: bool = False,
    allow_local: bool = False,
    restart_worker: Callable[[], None] = _restart_worker,
) -> dict:
    """Update only a clean checkout and never cross a non-fast-forward boundary."""
    import fcntl

    root = Path(config["control_root"])
    state_dir = Path(config["state_dir"])
    source = validate_control_source(
        config.get("control_repo_url"), allow_local=allow_local
    )
    branch = config.get("control_branch", "local-ci-unified")
    if not isinstance(branch, str) or not branch or re.search(r"[\x00-\x20~^:?*\\\[]", branch):
        raise ValueError("control_branch is not a safe Git branch name")
    if not root.is_dir() or root.is_symlink():
        raise ValueError("control_root must be an existing dedicated directory")
    state_dir.mkdir(parents=True, exist_ok=True)
    environment = _environment(state_dir)
    if _git(root, ["rev-parse", "--is-inside-work-tree"], environment).stdout.strip() != "true":
        raise ValueError("control_root must be a Git checkout")
    current = _git(root, ["rev-parse", "HEAD^{commit}"], environment).stdout.strip()
    if not SHA_RE.fullmatch(current):
        raise ValueError("control_root HEAD is not an exact commit")
    dirty = _git(
        root, ["status", "--porcelain=v1", "--untracked-files=all"], environment
    ).stdout.strip()
    if dirty:
        raise ValueError("control_root has local changes; automatic update refused")

    remote = _git(
        root, ["ls-remote", "--exit-code", source, f"refs/heads/{branch}"], environment
    ).stdout.split()
    if len(remote) < 2 or not SHA_RE.fullmatch(remote[0]):
        raise ValueError("Configured control branch did not resolve to one commit")
    target = remote[0]
    result = {
        "schema": UPDATE_SCHEMA,
        "checked_at": utc_now(),
        "branch": branch,
        "previous_revision": current,
        "revision": target,
        "changed": False,
        "restarted": False,
        "applied": apply,
    }
    if not apply:
        return result

    lock_path = state_dir / "control.lock"
    marker_path = state_dir / "control-update.json"
    with lock_path.open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            result["state"] = "deferred-active-task"
            return result

        # Recheck mutable local state after acquiring the deployment lock.
        current = _git(root, ["rev-parse", "HEAD^{commit}"], environment).stdout.strip()
        dirty = _git(
            root, ["status", "--porcelain=v1", "--untracked-files=all"], environment
        ).stdout.strip()
        if dirty:
            raise ValueError("control_root changed while waiting for the update lock")
        result["previous_revision"] = current
        if target != current:
            _git(
                root,
                [
                    "fetch",
                    "--quiet",
                    "--no-tags",
                    "--force",
                    source,
                    f"refs/heads/{branch}:refs/local-ci/control-update",
                ],
                environment,
            )
            fetched = _git(
                root, ["rev-parse", "refs/local-ci/control-update^{commit}"], environment
            ).stdout.strip()
            if fetched != target:
                raise RuntimeError("Control branch moved during fetch; retry next timer run")
            ancestor = _git(
                root, ["merge-base", "--is-ancestor", current, target], environment, check=False
            )
            if ancestor.returncode:
                raise ValueError("control_repo_url would replace history; only fast-forward updates are allowed")
            _git(root, ["checkout", "--quiet", "--detach", target], environment)
            if _git(root, ["rev-parse", "HEAD^{commit}"], environment).stdout.strip() != target:
                raise RuntimeError("Control checkout did not reach the fetched commit")
            result["changed"] = True

        try:
            marker = json.loads(marker_path.read_text())
        except (FileNotFoundError, OSError, ValueError):
            marker = {}
        if marker.get("revision") != target:
            restart_worker()
            result["restarted"] = True
            atomic_json(marker_path, {**result, "restarted_at": utc_now()})
        result["state"] = "updated" if result["changed"] else "current"
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.apply and os.geteuid() == 0:
            raise ValueError("Control update must run as the ordinary CI user")
        config = json.loads(Path(args.config).read_text())
        print(json.dumps(update_control(config, apply=args.apply), indent=2))
        return 0
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"Local CI control update failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
