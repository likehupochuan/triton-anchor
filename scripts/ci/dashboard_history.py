"""Read published evidence for historical display; never publish GitHub statuses."""

import hashlib
import json
import re
from datetime import datetime, timezone
from urllib.parse import quote

from agent_ci.protocol import RESULT_SCHEMA, validate_result


def _read(path):
    if path.is_symlink() or path.stat().st_size > 2 * 1024 * 1024:
        raise ValueError("Invalid historical result file")
    data = json.loads(path.read_bytes())
    if not isinstance(data, dict):
        raise ValueError("Historical report must be an object")
    return data


def _url(results, path):
    base = results.url.removesuffix(".git").rstrip("/")
    if not base.startswith("https://gitee.com/"):
        return ""
    return base + "/blob/" + results.branch + "/" + quote(path.relative_to(results.root).as_posix(), safe="/")


def history_rows(results, current):
    """Include completed older runs, including pre-unification delivery reports."""
    seen = {(row["task"]["task_id"], (row.get("result") or {}).get("run_id")) for row in current}
    rows = []
    root = results.root / "runs"
    paths = (path for path in root.rglob("result.json")
             if len(path.relative_to(root).parts) <= 6
             and "artifacts" not in path.relative_to(root).parts)
    for path in paths:
        try:
            result = _read(path)
            if result.get("schema") != RESULT_SCHEMA:
                continue
            validate_result(result)
            key = (result["task"]["task_id"], result["run_id"])
            if key in seen:
                continue
            seen.add(key)
            rows.append({"task": result["task"], "result": result, "status": result["status"],
                         "historical": True, "result_url": _url(results, path),
                         "artifact_urls": {item["path"]: _url(results, path.parent / "artifacts" / item["path"])
                                           for item in result["artifacts"] if not item.get("omitted")}})
        except (OSError, ValueError, TypeError, KeyError):
            continue
    # Legacy reports are displayed as historical measurements, not accepted as CI gates.
    for path in root.rglob("delivery-summary.txt"):
        if "artifacts" in path.relative_to(root).parts or path.is_symlink():
            continue
        try:
            if path.stat().st_size > 64 * 1024:
                continue
            fields = dict(line.split(": ", 1) for line in path.read_text(encoding="utf-8").splitlines() if ": " in line)
            sha = fields.get("target_sha", "")
            if not re.fullmatch(r"[a-f0-9]{40}", sha):
                continue
            run_id = path.parent.name
            date = datetime.strptime(run_id[:16], "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc).isoformat()
            identifier = hashlib.sha256(path.relative_to(root).as_posix().encode()).hexdigest()
            checks, artifacts, links = [], [], {}
            files = {"flaggems": "flaggems-summary.json", "compile_time": "compile-benchmark.json",
                     "pass_profile": "pass-profile.json", "ir_serialization": "ir-serialization.json"}
            stages = {"backend_rebuild": "backend_rebuild_status", "backend_tests": "backend_smoke_jit_status",
                      **{name: name + "_status" for name in files}}
            for name, field in stages.items():
                value = fields.get(field)
                if value not in {"pass", "fail", "error", "skipped", "timeout"}:
                    continue
                details = {}
                file = path.parent / files.get(name, "unused")
                if file.is_file():
                    data = _read(file)
                    details = ({"flaggems-summary": data} if name == "flaggems" else
                               {"candidate": {"summary": data.get("summary", {})}})
                    artifacts.append({"path": file.name, "size": file.stat().st_size})
                    links[file.name] = _url(results, file)
                checks.append({"tool_id": name, "status": {"error": "infra_error", "timeout": "infra_error"}.get(value, value),
                               "summary": "历史执行记录", "details": details})
            if not checks:
                continue
            task = {"task_id": identifier, "repository": "likehupochuan/triton-anchor",
                    "pr_number": 0, "head_sha": sha, "tested_sha": sha, "captured_at": date,
                    "target_branch": fields.get("branch", "历史运行"), "full": fields.get("flaggems_test_mode") == "full"}
            outcome = "pass" if fields.get("status") == "0" else "fail"
            backend_profile = fields.get("backend_profile", "").strip()
            candidate = {
                "backend_profile": backend_profile,
                "backend_enabled": bool(backend_profile),
                "profile": fields.get("triton_profile", ""),
                "triton_version": fields.get("triton_version", ""),
            }
            result = {"run_id": run_id, "completed_at": date, "status": outcome, "checks": checks,
                      "reviews": [], "findings": [], "blocking_reasons": [], "artifacts": artifacts,
                      "summary": "历史版本的测试与性能记录", "environment": {"variants": {"candidate": candidate}}}
            rows.append({"task": task, "result": result, "status": outcome, "historical": True,
                         "result_url": _url(results, path), "artifact_urls": links})
        except (OSError, ValueError, TypeError, KeyError):
            continue
    return rows
