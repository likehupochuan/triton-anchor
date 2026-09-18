from __future__ import annotations

import copy
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import runpy
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location(
    "gateway", ROOT / "scripts/ci/gateway.py"
)
g = importlib.util.module_from_spec(spec)
spec.loader.exec_module(g)


def git(root, *args):
    return (
        subprocess.check_output(["git", *args], cwd=root, stderr=subprocess.DEVNULL)
        .decode()
        .strip()
    )


def body(
    summary="Correct documented behavior",
    scope="README",
    validation="Reviewed the actual implementation",
):
    fields = {"summary": summary, "scope": scope, "validation": validation}
    return "\n".join(
        "<!-- field:" + key + " -->\n" + value for key, value in fields.items()
    )


def github_check(payload, check_id):
    # GITHUB_TOKEN-created checks do not retain the requested details_url.
    return {**payload, "id": check_id, "app": {"slug": "github-actions"},
            "details_url": f"https://github.com/{g.REPOSITORY}/runs/{check_id}"}


class FakeGitHub:
    repository = g.REPOSITORY

    def __init__(self, base, head, tested):
        self.base, self.head, self.tested = base, head, tested
        self.pull = {
            "state": "open",
            "draft": False,
            "user": {"login": "contributor"},
            "title": "Document the public behavior",
            "body": body(),
            "labels": [{"name": "docs"}],
            "head": {
                "sha": head,
                "ref": "docs-topic",
                "repo": {"full_name": "anteloper-c/triton-anchor"},
            },
            "base": {"ref": "main", "sha": base},
            "mergeable": True,
            "merge_commit_sha": tested,
        }
        self.statuses, self.comments = [], []
        self.latest_statuses, self.writes = {}, []
        self.cmake_files = {"triton/cmake/llvm-hash.txt": b"a" * 40 + b"\n"}
        self.content_reads = []
        self.environment = {
            "protection_rules": [
                {
                    "type": "required_reviewers",
                    "reviewers": [
                        {"type": "User", "reviewer": {"login": "maintainer"}}
                    ],
                }
            ]
        }
        self.approvals = []

    def request(self, path, method="GET", data=None):
        if path == "contents/triton/cmake?ref=" + self.tested:
            return [{"type": "file", "path": name} for name in self.cmake_files]
        if path == "environments/local-ci-fork-approval":
            return self.environment
        if path == "actions/runs/12345/approvals":
            return copy.deepcopy(self.approvals)
        if path == "pulls/7":
            return copy.deepcopy(self.pull)
        if path == "git/commits/" + self.tested:
            return {"parents": [{"sha": self.base}, {"sha": self.head}]}
        if path == "branches/main":
            return {"commit": {"sha": self.head}}
        raise AssertionError(path)

    optional = request

    def gitlinks(self, ref):
        return []

    def content(self, path, ref):
        assert ref == self.tested
        self.content_reads.append(path)
        return self.cmake_files[path]

    def status(self, task, state, description, url=""):
        self.statuses.append((task["task_id"], state))
        self.latest_statuses[task["task_id"]] = (state, description)
        self.writes.append("status")

    def status_matches(self, task, state, description):
        return self.latest_statuses.get(task["task_id"]) == (state, description)

    def check(self, *args, **_kwargs):
        if not hasattr(self, "check_calls"):
            self.check_calls = []
        self.check_calls.append(args)
        return False

    def owns_task(self, task, **kwargs):
        return True

    def task_start(self, task):
        return {}

    def retire_open_checks(self, task, **kwargs):
        pass

    def latest_dispatch(self, task):
        return {}

    def reset_existing_summary(self, task):
        pass

    def latest_summary(self, task):
        return None

    def restore_preflight(self, task):
        pass

    def approval_context(self, task):
        return {}

    def comment(self, task, content, **kwargs):
        if self.comments and self.comments[-1] == content:
            return False
        self.comments.append(content)
        self.writes.append("comment")
        return True


class GatewayBehaviorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        git(self.source, "init", "-q")
        git(self.source, "config", "user.name", "Test")
        git(self.source, "config", "user.email", "test@example.invalid")
        version = self.source / "triton/python/triton/__init__.py"
        version.parent.mkdir(parents=True)
        version.write_text("__version__ = '3.0.0'\n")
        (self.source / "README.md").write_text("old\n")
        git(self.source, "add", ".")
        git(self.source, "commit", "-qm", "base")
        self.base = git(self.source, "rev-parse", "HEAD")
        (self.source / "README.md").write_text("new\n")
        git(self.source, "add", ".")
        git(self.source, "commit", "-qm", "head")
        self.head = git(self.source, "rev-parse", "HEAD")
        tree = git(self.source, "rev-parse", "HEAD^{tree}")
        self.tested = (
            subprocess.check_output(
                ["git", "commit-tree", tree, "-p", self.base, "-p", self.head],
                cwd=self.source,
                input=b"merge\n",
            )
            .decode()
            .strip()
        )
        git(self.source, "checkout", "--detach", self.tested)
        self.remote = self.root / "gitee.git"
        git(self.root, "init", "--bare", "-q", str(self.remote))
        self.gh = FakeGitHub(self.base, self.head, self.tested)
        self.task = g.prepare_task(self.gh, self.base, 7)
        receiver_env = patch.dict(g.os.environ, {"RECEIVER_TASK_ID": self.task["task_id"]})
        receiver_env.start()
        self.addCleanup(receiver_env.stop)
        self.stores = []

    def tearDown(self):
        for store in self.stores:
            store.close()
        self.tmp.cleanup()

    def store(self, branch):
        result = g.GitStore(str(self.remote), branch)
        self.stores.append(result)
        return result

    def result(self):
        return {
            "schema": g.RESULT_SCHEMA,
            "task": self.task,
            "run_id": "20260907T120000Z-1",
            "status": "pass",
            "summary": "Documentation and architecture review completed.",
            "checks": [{
                "tool_id": "control_plane", "status": "pass",
                "summary": "Validated", "evidence": [],
            }],
            "reviews": [
                {"kind": "pr_info", "status": "pass", "summary": "Clear", "evidence": []},
                {
                "kind": "architecture", "status": "pass",
                "summary": "Compatible", "evidence": ["README.md"],
            },
            ],
            "findings": [],
            "blocking_reasons": [],
            "environment": {"profile": "local"},
            "policy": {"required_checks": ["control_plane"]},
            "artifacts": [],
            "completed_at": "2026-09-07T12:00:00Z",
        }

    def test_exact_identity_metadata_and_worker_revision(self):
        self.assertEqual(g.validate_task(self.task), self.task)
        self.assertTrue(self.task["external_fork"])
        for field, value in (
            ("title", "different"),
            ("full", True),
        ):
            changed = {**self.task, field: value}
            with self.subTest(field=field), self.assertRaises(ValueError):
                g.validate_task(changed)
        self.assertEqual(self.task["control_policy"], "worker")
        changed = {**self.task, "worker_revision_sha": "f" * 40}
        self.assertEqual(g.compute_task_id(changed), self.task["task_id"])
        g.validate_task(changed)
        legacy = dict(self.task)
        legacy.pop("control_policy")
        self.assertNotEqual(g.compute_task_id(legacy),
                            g.compute_task_id({**legacy, "worker_revision_sha": "f" * 40}))
        self.gh.pull["head"]["sha"] = "e" * 40
        with self.assertRaises(ValueError):
            g.prepare_task(self.gh, self.base, 7, requested_sha=self.head)
        task_file = self.root / "task.json"
        changed = {**self.task, "llvm_hash": "e" * 40}
        task_file.write_bytes(g.canonical(changed))
        with self.assertRaises(ValueError):
            g.load_task(task_file, g.digest(self.task))

    def test_prepare_reads_llvm_metadata_without_reading_unrelated_files(self):
        llvm_json = json.dumps({"llvm_hash": "a" * 40, "build_number": 123}).encode()
        for files in (
            {"llvm-info.json": llvm_json},
            {"llvm-info": llvm_json},
            {"llvm-info.txt": b"a" * 40 + b"\n"},
            {"llvm-hash.txt": b"a" * 40, "llvm-info.json": llvm_json},
        ):
            with self.subTest(files=tuple(files)):
                self.gh.cmake_files = {"triton/cmake/" + name: value for name, value in files.items()}
                expected_reads = set(self.gh.cmake_files)
                self.gh.cmake_files.update({
                    "triton/cmake/amd-llvm-info.json": b"invalid",
                    "triton/cmake/llvm-build-info.json": b"invalid",
                    "vendor/llvm-info.json": b"invalid",
                })
                self.gh.content_reads.clear()
                task = g.prepare_task(self.gh, self.base, 7)
                self.assertEqual(task["llvm_hash"], "a" * 40)
                self.assertEqual(set(self.gh.content_reads), expected_reads)

    def test_prepare_rejects_missing_invalid_or_conflicting_llvm_metadata(self):
        for files in (
            {},
            {"llvm-info.json": b'{"llvm_hash": "not-a-sha"}'},
            {"llvm-info.json": b'{"version": "3.3.0"}'},
            {"llvm-info": b"revision: " + b"a" * 40},
            {"llvm-hash.txt": b"a" * 40, "llvm-info.json": json.dumps({"llvm_hash": "b" * 40}).encode()},
        ):
            with self.subTest(files=tuple(files)):
                self.gh.cmake_files = {"triton/cmake/" + name: value for name, value in files.items()}
                with self.assertRaises(ValueError):
                    g.prepare_task(self.gh, self.base, 7)

    def test_prepare_uses_documented_pr_merge_result_and_checks_its_identity(self):
        self.assertEqual(self.task["tested_sha"], self.gh.pull["merge_commit_sha"])
        for branch in ("main", "CI_dev_forPR", "release/next"):
            with self.subTest(branch=branch):
                self.gh.pull["base"]["ref"] = branch
                task = g.prepare_task(self.gh, self.base, 7)
                self.assertEqual(task["target_branch"], branch)
        self.gh.pull["base"]["ref"] = "main"
        self.gh.pull["mergeable"] = False
        with self.assertRaisesRegex(ValueError, "cannot be merged cleanly"):
            g.prepare_task(self.gh, self.base, 7)

        self.gh.pull["mergeable"] = True
        self.gh.pull["merge_commit_sha"] = None
        with self.assertRaisesRegex(ValueError, "merge result is not ready"):
            g.prepare_task(self.gh, self.base, 7)
        self.gh.pull["merge_commit_sha"] = self.tested
        self.gh.pull["base"]["sha"] = "f" * 40
        with self.assertRaisesRegex(ValueError, "Merge parents do not match"):
            g.prepare_task(self.gh, self.base, 7)

    def test_preinstalled_flaggems_and_other_submodule_mirrors(self):
        links = [{"path": "FlagGems", "sha": "c" * 40}]
        with patch.object(self.gh, "gitlinks", return_value=links):
            with patch.dict(g.os.environ, {"GITEE_SUBMODULE_MIRRORS": "{}"}):
                task = g.prepare_task(self.gh, self.base, 7)
                self.assertEqual(task["submodules"], [])
            for path in ("OtherDependency", "vendor/FlagGems"):
                with self.subTest(path=path):
                    links[:] = [
                        {"path": "FlagGems", "sha": "c" * 40},
                        {"path": path, "sha": "d" * 40},
                    ]
                    with patch.dict(
                        g.os.environ, {"GITEE_SUBMODULE_MIRRORS": "{}"}
                    ):
                        with self.assertRaisesRegex(
                            ValueError, "Gitee submodule mirror"
                        ):
                            g.prepare_task(self.gh, self.base, 7)
                    with patch.dict(
                        g.os.environ,
                        {
                            "GITEE_SUBMODULE_MIRRORS": json.dumps(
                                {path: "https://gitee.com/test/dependency.git"}
                            )
                        },
                    ):
                        task = g.prepare_task(self.gh, self.base, 7)
                    self.assertEqual(
                        {row["path"] for row in task["submodules"]}, {path}
                    )
                    self.assertEqual(
                        {row["variant"] for row in task["submodules"]},
                        {"candidate", "base"},
                    )
                    self.assertTrue(
                        all(
                            task["task_id"] in row["task_ref"]
                            for row in task["submodules"]
                        )
                    )

    def test_three_required_pr_sections_without_type_specific_fields(self):
        self.assertEqual(g.validate_pr_info(self.task), [])
        heading_body = """## 变更概述 / Summary
Document the behavior
## 影响范围 / Scope
README only
## 验证情况 / Validation
未运行：纯文档变更
"""
        self.assertEqual(
            g.validate_pr_info({**self.task, "description": heading_body}), []
        )
        for headings in (("变更概述", "影响范围", "验证情况"), ("Summary", "Scope", "Validation"),
                         ("变更概述 / Summary", "影响范围 / Scope", "验证情况 / Validation")):
            description = "\n".join(f"### {heading}\n{value}" for heading, value in
                                    zip(headings, ("Clarify behavior", "Docs only", "Reviewed diff")))
            description += "\n### 自定义字段 / Custom notes\nRollout details\n"
            with self.subTest(headings=headings):
                self.assertEqual(g.validate_pr_info({**self.task, "description": description, "labels": []}), [])
                self.assertEqual(g.pr_fields(description)["validation"], "Reviewed diff")
        template = (ROOT / ".github/PULL_REQUEST_TEMPLATE.md").read_text(encoding="utf-8")
        self.assertEqual(len(g.validate_pr_info({**self.task, "description": template})), 3)
        for field in g.FIELD_NAMES:
            missing = body().replace(f"<!-- field:{field} -->", "<!-- field:unused -->")
            with self.subTest(field=field):
                self.assertTrue(
                    g.validate_pr_info({**self.task, "description": missing})
                )
        for placeholder in ("TODO", "TBD", "待填写", "..."):
            with self.subTest(placeholder=placeholder):
                self.assertTrue(
                    g.validate_pr_info(
                        {**self.task, "description": body(summary=placeholder)}
                    )
                )
        for title in ("WIP", "todo", "TBD"):
            with self.subTest(title=title):
                self.assertTrue(g.validate_pr_info({**self.task, "title": title}))
        for title in ("test", "Update", "更新"):
            with self.subTest(title=title):
                self.assertEqual(g.validate_pr_info({**self.task, "title": title}), [])

    def test_external_fork_cannot_use_an_unprotected_environment(self):
        g.validate_approval_environment(self.gh)
        for environment in (
            {},
            {"protection_rules": []},
            {"protection_rules": [{"type": "required_reviewers", "reviewers": []}]},
        ):
            self.gh.environment = environment
            with self.subTest(environment=environment), self.assertRaises(ValueError):
                g.validate_approval_environment(self.gh)

    def test_real_git_enqueue_manifest_last_and_idempotent_retry(self):
        control = self.store(g.CONTROL_BRANCH)
        g.enqueue(self.task, self.gh, control, self.source)
        self.assertEqual(
            control.get("tasks/" + self.task["task_id"] + ".json"), self.task
        )
        self.assertEqual(
            control.get("current/" + g.current_key(self.task) + ".json")["task_id"],
            self.task["task_id"],
        )
        for key, ref in (
            ("tested_sha", "task_ref"),
            ("base_sha", "base_task_ref"),
            ("head_sha", "head_task_ref"),
        ):
            actual = git(
                self.root,
                "--git-dir=" + str(self.remote),
                "rev-parse",
                "refs/heads/" + self.task[ref],
            )
            self.assertEqual(actual, self.task[key])
        retry = {**self.task, "captured_at": "2099-01-01T00:00:00Z", "worker_revision_sha": "f" * 40}
        g.enqueue(retry, self.gh, control, self.source)
        self.assertEqual(
            control.get("tasks/" + self.task["task_id"] + ".json"), self.task
        )

    def test_legacy_current_does_not_block_new_dispatch_or_collection(self):
        control = self.store(g.CONTROL_BRANCH)
        legacy = {
            **self.task,
            "schema": g.TASK_SCHEMA + "/v4",
            "pr_number": 8,
            "worker_revision_sha": "f" * 40,
            "task_ref": "ci/pr-8/docs-topic",
            "base_task_ref": "ci/base/pr-8/docs-topic",
            "head_task_ref": "ci/head/pr-8/docs-topic",
        }
        legacy["task_id"] = g.digest({key: legacy[key] for key in g.IDENTITY_FIELDS})
        control.put(
            {
                f"tasks/{legacy['task_id']}.json": legacy,
                f"current/{g.current_key(legacy)}.json": {
                    "task_id": legacy["task_id"]
                },
            }
        )
        results = self.store(g.RESULTS_BRANCH)
        before = git(control.root, "rev-parse", "HEAD")
        self.assertEqual(g.cancel_obsolete(self.gh, control, 8), 0)
        self.assertEqual(
            g.collect_results(self.gh, control, results, self.root / "dashboard"), []
        )
        self.assertEqual(git(control.root, "rev-parse", "HEAD"), before)
        self.assertEqual(self.gh.writes, [])

        g.enqueue(self.task, self.gh, control, self.source)
        result = self.result()
        results.put(
            {f"runs/{self.task['task_id']}/{result['run_id']}/result.json": result}
        )
        self.assertEqual(g.cancel_obsolete(self.gh, control, 7), 0)
        published = g.collect_results(
            self.gh, control, results, self.root / "dashboard"
        )
        self.assertEqual([row["task_id"] for row in published], [self.task["task_id"]])
        self.assertEqual(self.gh.statuses[-1][1], "success")
        snapshot = json.loads((self.root / "dashboard/tasks.json").read_text(encoding="utf-8"))
        self.assertEqual(
            [row["task"]["task_id"] for row in snapshot["tasks"]],
            [self.task["task_id"]],
        )
        self.assertEqual(control.get(f"tasks/{legacy['task_id']}.json"), legacy)
        self.assertIsNone(control.get(f"cancel/{legacy['task_id']}.json"))

    def test_damaged_current_is_not_ignored_as_legacy(self):
        control = self.store(g.CONTROL_BRANCH)
        results = self.store(g.RESULTS_BRANCH)
        for mutation in (
            {"task_ref": self.task["task_ref"] + "-damaged"},
            {"task_ref": "ci/pr-7/docs-topic"},
            {"title": "Metadata changed without updating its identity"},
        ):
            with self.subTest(mutation=mutation):
                damaged = {**self.task, **mutation}
                control.put(
                    {
                        f"tasks/{self.task['task_id']}.json": damaged,
                        f"current/{g.current_key(self.task)}.json": {
                            "task_id": self.task["task_id"]
                        },
                    }
                )
                with self.assertRaises(ValueError):
                    g.cancel_obsolete(self.gh, control, 7)
                with self.assertRaises(ValueError):
                    g.collect_results(
                        self.gh, control, results, self.root / "dashboard"
                    )
        self.assertEqual(self.gh.writes, [])

    def test_branch_cancellation_ignores_unrelated_current_records(self):
        gh = FakeGitHub(self.base, self.tested, self.tested)
        task = g.prepare_task(gh, self.base, branch="main")
        control = self.store(g.CONTROL_BRANCH)
        g.enqueue(task, gh, control, self.source)
        unrelated = [
            {**task, "target_branch": "other"},
            self.task,
            {**task, "repository": "anteloper-c/triton-anchor"},
        ]
        damaged = {
            f"current/{g.current_key(subject)}.json": {"damaged": True}
            for subject in unrelated
        }
        control.put(damaged)
        gh.head = "e" * 40
        with patch.object(gh, "request", wraps=gh.request) as request:
            self.assertEqual(g.cancel_obsolete(gh, control, branch="main"), 1)
        self.assertEqual([call.args[0] for call in request.call_args_list], ["branches/main"])
        self.assertIsNotNone(control.get(f"cancel/{task['task_id']}.json"))
        self.assertEqual(g.cancel_obsolete(gh, control, branch="missing"), 0)
        for path, row in damaged.items():
            self.assertEqual(control.get(path), row)
        with self.assertRaises(ValueError):
            g.cancel_obsolete(gh, control)

    def test_collect_cancels_only_the_receivers_branch_and_requires_a_task_scope(self):
        gh = FakeGitHub(self.base, self.tested, self.tested)
        task = g.prepare_task(gh, self.base, branch="main")
        other = {**task, "target_branch": "other"}
        other["task_id"] = g.digest({key: other[key] for key in g.IDENTITY_FIELDS})
        for field, suffix in (("task_ref", "tested"), ("base_task_ref", "base"), ("head_task_ref", "head")):
            other[field] = f"ci/branch/{other['task_id']}/{suffix}"
        g.validate_task(other)
        control = self.store(g.CONTROL_BRANCH)
        documents = {}
        for item in (task, other, self.task):
            documents[f"tasks/{item['task_id']}.json"] = item
            documents[f"current/{g.current_key(item)}.json"] = {"task_id": item["task_id"]}
        control.put(documents)
        gh.head = "e" * 40
        request = gh.request
        store = g.GitStore
        for received_id in ("", task["task_id"]):
            with (
                self.subTest(received_id=received_id),
                patch("sys.argv", ["gateway.py", "collect"]),
                patch.dict(g.os.environ, {
                    "GITEE_RESULTS_REPO_URL": "https://gitee.com/test/results.git",
                    "RECEIVER_TASK_ID": received_id,
                    "PR_NUMBER": "0", "SOURCE_BRANCH": "other",
                }),
                patch.object(g, "GitHub", return_value=gh),
                patch.object(g, "GitStore", side_effect=lambda _url, branch: store(str(self.remote), branch)),
                patch.object(gh, "request", side_effect=lambda path: {"commit": {"sha": gh.head}} if path == "branches/other" else request(path)),
                patch.object(g, "collect_results") as collect,
            ):
                self.assertEqual(g.main(), 0)
                collect.assert_called_once()
            control.refresh()
            self.assertEqual(bool(control.get(f"cancel/{task['task_id']}.json")), bool(received_id))
            for item in (other, self.task):
                self.assertIsNone(control.get(f"cancel/{item['task_id']}.json"))

    def test_lifecycle_cancellation_reaches_gitee(self):
        control = self.store(g.CONTROL_BRANCH)
        g.enqueue(self.task, self.gh, control, self.source)
        for update in (
            {"draft": True},
            {"state": "closed"},
            {"body": body("Different summary")},
        ):
            self.gh.pull.update(update)
            self.assertFalse(g.is_current(self.gh, self.task))
        self.assertEqual(g.cancel_obsolete(self.gh, control, 7), 1)
        self.assertEqual(g.cancel_obsolete(self.gh, control, 7), 0)
        self.assertEqual(self.gh.comments, [])
        self.assertEqual(self.gh.statuses[-1][1], "error")
        self.assertTrue(self.gh.latest_statuses[self.task["task_id"]][1].isascii())
        self.assertTrue(control.get("cancel/" + self.task["task_id"] + ".json")["github_notified"])
        self.assertEqual(
            control.get("cancel/" + self.task["task_id"] + ".json")["task_id"],
            self.task["task_id"],
        )

    def test_status_comment_dashboard_order_has_no_return_channel(self):
        control = self.store(g.CONTROL_BRANCH)
        g.enqueue(self.task, self.gh, control, self.source)
        results = self.store(g.RESULTS_BRANCH)
        result = self.result()
        name = (
            "runs/" + self.task["task_id"] + "/" + result["run_id"] + "/result.json"
        )
        results.put({name: result})
        control_revision = git(control.root, "rev-parse", "HEAD")
        result_revision = git(results.root, "rev-parse", "HEAD")
        self.gh.writes.clear()
        write_bytes = Path.write_bytes

        def record_dashboard(path, data):
            if path.name == "tasks.json":
                self.gh.writes.append("dashboard")
            return write_bytes(path, data)

        with patch.object(Path, "write_bytes", record_dashboard):
            published = g.collect_results(
                self.gh, control, results, self.root / "dashboard"
            )
        self.assertEqual(self.gh.writes, ["status", "comment", "dashboard"])
        self.assertEqual(
            published[0]["result_digest"],
            hashlib.sha256((results.root / name).read_bytes()).hexdigest(),
        )
        self.assertEqual(self.gh.statuses[-1][1], "success")
        self.assertIn("Local CI", self.gh.comments[-1])
        before = (len(self.gh.statuses), len(self.gh.comments))
        again = g.collect_results(self.gh, control, results, self.root / "dashboard")
        self.assertEqual(again, [])
        self.assertEqual(before, (len(self.gh.statuses), len(self.gh.comments)))
        self.assertEqual(control_revision, git(control.root, "rev-parse", "HEAD"))
        self.assertEqual(result_revision, git(results.root, "rev-parse", "HEAD"))
        self.gh.pull["draft"] = True
        self.assertEqual(
            g.collect_results(self.gh, control, results, self.root / "dashboard"), []
        )

    def test_comment_failure_retries_same_uploaded_result(self):
        control = self.store(g.CONTROL_BRANCH)
        g.enqueue(self.task, self.gh, control, self.source)
        results = self.store(g.RESULTS_BRANCH)
        result = self.result()
        results.put(
            {
                "runs/"
                + self.task["task_id"]
                + "/"
                + result["run_id"]
                + "/result.json": result
            }
        )
        with patch.object(
            self.gh, "comment", side_effect=RuntimeError("comment unavailable")
        ):
            self.assertEqual(
                g.collect_results(self.gh, control, results, self.root / "dashboard"),
                [],
            )
        snapshot = json.loads((self.root / "dashboard/tasks.json").read_text(encoding="utf-8"))
        self.assertEqual(snapshot["tasks"][0]["status"], "pass")
        self.assertIn("receiver_error", snapshot["tasks"][0])
        self.assertEqual(self.gh.statuses[-1][1], "success")
        result_revision = git(results.root, "rev-parse", "HEAD")
        self.assertEqual(
            len(g.collect_results(self.gh, control, results, self.root / "dashboard")),
            1,
        )
        self.assertEqual(self.gh.statuses[-1][1], "success")
        self.assertEqual(len(self.gh.comments), 1)
        self.assertEqual(result_revision, git(results.root, "rev-parse", "HEAD"))

    def test_infrastructure_failure_reports_but_cancelled_results_do_not(self):
        control = self.store(g.CONTROL_BRANCH)
        g.enqueue(self.task, self.gh, control, self.source)
        results = self.store(g.RESULTS_BRANCH)
        result = self.result()
        result.update(
            status="infra_error",
            checks=[],
            reviews=[],
            blocking_reasons=["environment failed"],
        )
        results.put(
            {
                "runs/"
                + self.task["task_id"]
                + "/"
                + result["run_id"]
                + "/result.json": result
            }
        )
        published = g.collect_results(
            self.gh, control, results, self.root / "dashboard"
        )
        self.assertEqual(len(published), 1)
        self.assertEqual(self.gh.statuses[-1][1], "error")
        control.put(
            {
                "cancel/" + self.task["task_id"] + ".json": {
                    "task_id": self.task["task_id"],
                    "reason": "cancel after collection",
                }
            }
        )
        before = (len(self.gh.statuses), len(self.gh.comments))
        self.assertEqual(
            g.collect_results(self.gh, control, results, self.root / "dashboard"), []
        )
        self.assertEqual(before, (len(self.gh.statuses), len(self.gh.comments)))

    def test_failed_dashboard_is_rebuilt_without_repeating_completed_writeback(self):
        control = self.store(g.CONTROL_BRANCH)
        g.enqueue(self.task, self.gh, control, self.source)
        results = self.store(g.RESULTS_BRANCH)
        result = self.result()
        results.put(
            {f"runs/{self.task['task_id']}/{result['run_id']}/result.json": result}
        )
        dashboard = self.root / "dashboard"
        dashboard.write_text("not a directory")
        with self.assertRaises(OSError):
            g.collect_results(self.gh, control, results, dashboard)
        before = (len(self.gh.statuses), len(self.gh.comments))
        self.assertEqual(self.gh.statuses[-1][1], "success")
        dashboard.unlink()
        self.assertEqual(g.collect_results(self.gh, control, results, dashboard), [])
        self.assertEqual(before, (len(self.gh.statuses), len(self.gh.comments)))
        self.assertTrue((dashboard / "tasks.json").is_file())

    def test_status_match_does_not_skip_missing_comment_repair(self):
        control = self.store(g.CONTROL_BRANCH)
        g.enqueue(self.task, self.gh, control, self.source)
        results = self.store(g.RESULTS_BRANCH)
        result = self.result()
        path = f"runs/{self.task['task_id']}/{result['run_id']}/result.json"
        results.put({path: result})
        raw_digest = hashlib.sha256((results.root / path).read_bytes()).hexdigest()
        self.gh.status(
            self.task, "success", g.publication_description("pass", raw_digest)
        )
        status_count = len(self.gh.statuses)
        self.assertEqual(
            len(g.collect_results(self.gh, control, results, self.root / "dashboard")),
            1,
        )
        self.assertEqual(len(self.gh.statuses), status_count)
        self.assertEqual(len(self.gh.comments), 1)

    def test_head_change_during_status_write_skips_old_comment(self):
        control = self.store(g.CONTROL_BRANCH)
        g.enqueue(self.task, self.gh, control, self.source)
        results = self.store(g.RESULTS_BRANCH)
        result = self.result()
        results.put(
            {f"runs/{self.task['task_id']}/{result['run_id']}/result.json": result}
        )
        status = self.gh.status

        def update_head(*args, **kwargs):
            status(*args, **kwargs)
            self.gh.pull["head"]["sha"] = "e" * 40

        with patch.object(self.gh, "status", side_effect=update_head):
            self.assertEqual(
                g.collect_results(self.gh, control, results, self.root / "dashboard"),
                [],
            )
        self.assertFalse(self.gh.comments)
        self.assertEqual(g.cancel_obsolete(self.gh, control, 7), 1)
        self.assertEqual(self.gh.statuses[-1][1], "error")

    def test_latest_invalid_result_does_not_reuse_an_old_pass(self):
        control = self.store(g.CONTROL_BRANCH)
        g.enqueue(self.task, self.gh, control, self.source)
        results = self.store(g.RESULTS_BRANCH)
        result = self.result()
        prefix = f"runs/{self.task['task_id']}"
        results.put({f"{prefix}/{result['run_id']}/result.json": result})
        newer = {**result, "run_id": "20260908T120000Z-2", "blocking_reasons": ["Unresolved"]}
        results.put({f"{prefix}/{newer['run_id']}/result.json": newer})
        self.assertEqual(g.collect_results(self.gh, control, results, self.root / "dashboard"), [])
        self.assertEqual(self.gh.statuses[-1][1], "error")
        self.assertFalse(self.gh.comments)

    def test_sha_results_are_selected_by_task_not_just_latest_run(self):
        results = self.store(g.RESULTS_BRANCH)
        result = self.result()
        prefix = g.result_task_prefixes(self.task)[0]
        other = copy.deepcopy(result)
        other["run_id"] = "20990101T000000Z-other"
        other["task"]["task_id"] = "f" * 64
        own_path = f"{prefix}/{result['run_id']}/result.json"
        results.put({own_path: result, f"{prefix}/{other['run_id']}/result.json": other})
        self.assertEqual(g.latest_result(self.task, results), results.root / own_path)

    def test_grouped_task_id_results_remain_readable(self):
        results = self.store(g.RESULTS_BRANCH)
        result = self.result()
        prefix = g.result_task_prefixes(self.task)[1]
        path = f"{prefix}/{result['run_id']}/result.json"
        results.put({path: result})
        self.assertEqual(g.latest_result(self.task, results), results.root / path)

    def test_invalid_results_cannot_claim_success(self):
        for mutate in (
            lambda result: result.update(blocking_reasons=["deterministic regression"]),
            lambda result: result.update(run_id="../../escape"),
            lambda result: result["task"].update(worker_revision_sha="e" * 40),
        ):
            result = copy.deepcopy(self.result())
            mutate(result)
            with self.subTest(mutation=mutate), self.assertRaises(ValueError):
                g.validate_result(result, self.task)

    def test_receiver_reads_selected_files_from_same_git_result(self):
        result = self.result()
        result["artifacts"] = [{"path": "report.txt", "size": 2}]
        directory = self.root / result["run_id"]
        directory.mkdir()
        path = directory / "result.json"
        path.write_bytes(g.canonical(result))
        with self.assertRaises(ValueError):
            g.read_result(path, self.task, None)
        (directory / "artifacts").mkdir()
        (directory / "artifacts/report.txt").write_text("ok")
        self.assertEqual(g.read_result(path, self.task, None)[0], result)

    def test_receiver_finishes_as_soon_as_its_result_arrives(self):
        control = self.store(g.CONTROL_BRANCH)
        g.enqueue(self.task, self.gh, control, self.source)
        results = self.store(g.RESULTS_BRANCH)
        result = self.result()

        def arrive(_seconds):
            results.put(
                {f"runs/{self.task['task_id']}/{result['run_id']}/result.json": result}
            )

        with patch.object(g.time, "sleep", side_effect=arrive) as sleep:
            self.assertEqual(
                g.receive_result(self.gh, str(self.remote), self.task["task_id"]),
                "ready",
            )
        sleep.assert_called_once_with(g.RECEIVER_POLL_SECONDS)
        self.assertIsNone(control.get(f"cancel/{self.task['task_id']}.json"))

    def test_receiver_stops_immediately_when_task_is_obsolete(self):
        control = self.store(g.CONTROL_BRANCH)
        g.enqueue(self.task, self.gh, control, self.source)
        self.gh.pull["head"]["sha"] = "e" * 40
        with patch.object(g.time, "sleep") as sleep:
            self.assertEqual(
                g.receive_result(self.gh, str(self.remote), self.task["task_id"]),
                "obsolete",
            )
        sleep.assert_not_called()

    def test_receiver_continuations_are_bounded_and_keep_the_same_task(self):
        control = self.store(g.CONTROL_BRANCH)
        g.enqueue(self.task, self.gh, control, self.source)
        before = git(control.root, "rev-parse", "HEAD")
        request = self.gh.request
        dispatched = []

        def dispatch(path, method="GET", data=None):
            if method == "POST":
                self.assertEqual(path, "actions/workflows/ci-gateway.yml/dispatches")
                dispatched.append(data)
                return None
            return request(path, method, data)

        for round_number in range(1, g.RECEIVER_MAX_ROUNDS + 1):
            elapsed = [0]

            def advance(seconds):
                elapsed[0] += seconds

            with (
                self.subTest(round_number=round_number),
                patch.object(self.gh, "request", side_effect=dispatch),
                patch.object(g, "RECEIVER_WAIT_SECONDS", 1),
                patch.object(g.time, "monotonic", side_effect=lambda: elapsed[0]),
                patch.object(g.time, "sleep", side_effect=advance),
            ):
                if round_number < g.RECEIVER_MAX_ROUNDS:
                    self.assertEqual(
                        g.receive_result(
                            self.gh, str(self.remote), self.task["task_id"], round_number
                        ),
                        "continued",
                    )
                else:
                    with self.assertRaisesRegex(ValueError, "timed out"):
                        g.receive_result(
                            self.gh, str(self.remote), self.task["task_id"], round_number
                        )
        self.assertEqual(
            dispatched,
            [
                {"ref": "main", "inputs": {
                    "mode": "receive", "task_id": self.task["task_id"],
                    "receiver_round": str(round_number),
                    "run_title": f"PR #7 | h:{self.head[:7]} m:{self.tested[:7]}",
                }}
                for round_number in (2, 3)
            ],
        )
        self.assertEqual(self.gh.statuses[-1][1], "error")
        control.refresh()
        self.assertEqual(git(control.root, "rev-parse", "HEAD"), before)
        self.assertIsNone(control.get(f"cancel/{self.task['task_id']}.json"))

    def test_receiver_retries_transient_transport_without_another_task(self):
        control = self.store(g.CONTROL_BRANCH)
        g.enqueue(self.task, self.gh, control, self.source)
        results = self.store(g.RESULTS_BRANCH)
        result = self.result()
        results.put(
            {f"runs/{self.task['task_id']}/{result['run_id']}/result.json": result}
        )
        before = git(control.root, "rev-parse", "HEAD")
        store = g.GitStore
        attempts = []

        def connect(*args, **kwargs):
            attempts.append(args)
            if len(attempts) == 1:
                raise OSError("temporary Gitee connection failure")
            return store(*args, **kwargs)

        with (
            patch.object(g, "GitStore", side_effect=connect),
            patch.object(g.time, "sleep") as sleep,
        ):
            self.assertEqual(
                g.receive_result(self.gh, str(self.remote), self.task["task_id"]),
                "ready",
            )
        sleep.assert_called_once_with(g.RECEIVER_POLL_SECONDS)
        control.refresh()
        self.assertEqual(git(control.root, "rev-parse", "HEAD"), before)

    def test_preflight_reports_cancelled_and_unexecuted_without_passing_them(self):
        with patch.object(self.gh, "check", return_value=True) as check:
            g.publish_preflight_checks(self.gh, self.task,
                                      {"basic": "failure", "api": "skipped", "security": "cancelled"}, False)
        self.assertEqual([call.args[3] for call in check.call_args_list],
                         ["failure"])
        self.assertTrue(all(call.args[4].isascii() and call.args[5].isascii() for call in check.call_args_list))
        stages = {key: "cancelled" for key in ("prepare", *g.CHECK_NAMES)}
        with patch.object(self.gh, "check") as check, patch.object(self.gh, "retire_open_checks") as retire:
            g.finalize_preflight(self.gh, self.task, stages)
            check.assert_not_called()
            retire.assert_called_once_with(self.task)
            g.finalize_preflight(self.gh, self.task, {
                "prepare": "success", "basic": "failure", "api": "skipped", "security": "skipped",
                "card": "skipped", "approval": "skipped", "enqueue": "skipped",
            })
            self.assertEqual([call.args[1:4] for call in check.call_args_list], [("basic", "completed", "failure")])
        self.assertEqual(self.gh.statuses, [])

    def test_stage_completion_advances_only_after_success_without_summary_or_comments(self):
        outcomes = {"success": "success", "failure": "failure",
                    "cancelled": "cancelled", "skipped": "action_required"}
        for stage in g.CHECK_NAMES:
            for outcome, conclusion in outcomes.items():
                with self.subTest(stage=stage, outcome=outcome), patch.object(self.gh, "check", return_value=True) as check:
                    changed = g.sync_preflight(self.gh, self.task, {stage: outcome})
                    if outcome == "skipped":
                        self.assertFalse(changed)
                        check.assert_not_called()
                        continue
                    self.assertTrue(changed)
                    self.assertEqual(check.call_args_list[0].args[1:4], (stage, "completed", conclusion))
                    advances = outcome == "success" and stage != "security"
                    self.assertEqual(check.call_count, 2 if advances else 1)
                    if advances:
                        successor = "api" if stage == "basic" else "security"
                        self.assertEqual(check.call_args.args[1:4], (successor, "in_progress", None))
        self.assertEqual(self.gh.writes, [])

    def test_dispatch_success_follows_pending_summary_for_worker(self):
        control = self.store(g.CONTROL_BRANCH)
        events = []
        with patch.object(self.gh, "check", side_effect=lambda *args: events.append((args[1], args[2], args[3]))), \
                patch.object(self.gh, "status", side_effect=lambda *args: events.append(("summary", args[1]))):
            g.enqueue(self.task, self.gh, control, self.source)
        self.assertEqual(events, [("dispatch", "in_progress", None),
                                  ("summary", "pending"), ("dispatch", "completed", "success")])

    def test_push_checks_use_tested_commit_and_cannot_overwrite_a_new_owner(self):
        task = {**self.task, "pr_number": 0, "tested_sha": self.head, "event_kind": "push"}
        gh = g.GitHub(g.REPOSITORY, token="fixture")
        runs = [github_check({"name": g.CHECK_NAMES["basic"], "status": "queued",
                              "external_id": f"triton-anchor-local-ci:basic:{task['task_id']}",
                              "output": {"summary": "<!-- local-ci-workflow:1234 -->"}}, 1)]
        writes = []

        def request(path, method="GET", data=None):
            if method != "GET":
                writes.append((path, method, data))
                return {}
            if path.startswith(f"commits/{task['head_sha']}/check-runs?"):
                return {"check_runs": runs}
            if "/statuses?" in path:
                return []
            return self.gh.request(path)

        with patch.object(gh, "request", side_effect=request), patch.dict(g.os.environ, {
            "GITHUB_SERVER_URL": "https://github.com", "GITHUB_REPOSITORY": g.REPOSITORY,
            "GITHUB_RUN_ID": "1234",
        }):
            self.assertTrue(g.sync_preflight(gh, task, {"basic": "success"}))
            self.assertEqual(len(writes), 2)
            self.assertEqual(writes[0][:2], ("check-runs/1", "PATCH"))
            path, method, payload = writes[1]
            self.assertEqual((path, method), ("check-runs", "POST"))
            self.assertEqual(payload["head_sha"], task["head_sha"])
            self.assertEqual(payload["details_url"], f"https://github.com/{g.REPOSITORY}/actions/runs/1234")
            self.assertEqual(writes[0][2]["conclusion"], "success")
            runs.append({
                **writes[0][2], "id": 12, "app": {"slug": "github-actions"},
                "external_id": "triton-anchor-local-ci:basic:newer-task",
            })
            writes.clear()
            self.assertFalse(g.sync_preflight(gh, task, {"basic": "failure"}))
            g.finalize_preflight(gh, task, {"prepare": "success", "basic": "failure"})
            self.assertEqual(writes, [])

    def test_stage_completion_rejects_unknown_or_nonterminal_outcomes(self):
        for stages in (None, [], ["basic"], {}, {"summary": "success"},
                       {"api": "in_progress"}, {"api": ["success"]}):
            with self.subTest(stages=stages), self.assertRaises(ValueError):
                g.sync_preflight(self.gh, self.task, stages)

    def test_successful_github_checks_have_no_redundant_title(self):
        with patch.object(self.gh, "check") as check:
            g.publish_preflight_checks(
                self.gh,
                self.task,
                {"prepare": "success", **dict.fromkeys(g.CHECK_NAMES, "success")},
                True,
            )
        self.assertTrue(all(call.args[4] == "" for call in check.call_args_list))

    def test_approval_card_shows_frozen_identity_and_readable_file_scope(self):
        stages = {key: "success" for key in ("prepare", *g.CHECK_NAMES)}
        context = {"author": "contributor", "source": "fork/repo", "branch": "topic",
                   "files": ["tests/test_api.py"], "complete": True,
                   "attention": ["1 个测试文件"], "additions": 3, "deletions": 1}
        with patch.dict(g.os.environ, {"GITHUB_RUN_ID": "12345"}):
            card = g.approval_card(self.task, stages, True, context=context)
        self.assertIn(f"PR 提交：`{self.head}`", card)
        self.assertIn(f"被测合并提交：`{self.tested}`", card)
        self.assertIn("改动范围（按文件路径归类）：1 个测试文件", card)
        self.assertIn("等待维护者审批", card)

    def test_failed_prerequisites_never_publish_card_or_verify_approval(self):
        passed = {key: "success" for key in ("prepare", *g.CHECK_NAMES)}
        stages = {**passed, "security": "failure"}
        self.assertEqual(g.approval_card(self.task, stages, False), "")

    def test_pr_information_failure_has_friendly_actionable_feedback(self):
        errors = ["请补充影响范围", "请说明已完成的验证"]
        with (
            patch.object(g.sys, "argv", ["gateway.py", "info"]),
            patch.object(g, "GitHub", return_value=self.gh),
            patch.object(g, "load_task", return_value=self.task),
            patch.object(g, "validate_pr_info", return_value=errors),
        ):
            self.assertEqual(g.main(), 1)
        self.assertEqual(len(self.gh.comments), 1)
        message = self.gh.comments[0]
        for text in ("感谢您的贡献", *errors, "直接更新 PR 描述", "PR 信息检查通过后会进入后续检查与必要验证"):
            self.assertIn(text, message)
        self.assertEqual(self.gh.statuses, [])
        self.assertEqual(self.gh.check_calls[-1][1:4], ("basic", "completed", "failure"))
        self.assertTrue(self.gh.check_calls[-1][4].isascii())

    def test_result_comment_lists_only_executed_checks_and_links_dashboard_after_table(self):
        result = self.result()
        result["checks"].append({"tool_id": "frontend_build", "status": "not_selected", "summary": "无需构建"})
        rendered = g.result_comment(result)
        details = rendered.split("### 查看审查详情", 1)[1].split("### 限制说明", 1)[0]
        self.assertIn("CI 流程验证 | 通过", details)
        self.assertNotIn("前端构建", details)
        self.assertLess(details.index("</details>"), details.index("在 Dashboard 查看本次任务详情"))

    def test_result_comment_separates_evidence_delivery_from_execution(self):
        result = self.result()
        result["evidence_delivery"] = {
            "status": "incomplete", "omitted": [{"path": "report.txt"}]
        }
        rendered = g.result_comment(result)
        self.assertIn("执行通过，证据发布不完整", rendered)
        self.assertNotIn("环境或执行异常", rendered)

    def test_dashboard_history_restores_modern_and_legacy_data_without_gate_writes(self):
        from types import SimpleNamespace
        store = SimpleNamespace(root=self.root / "archive", url="https://gitee.com/example/results.git", branch="results")
        folder = store.root / "runs/pr/branch-main/pr-7" / self.head / "run-1"
        folder.mkdir(parents=True)
        result = self.result()
        (folder / "result.json").write_text(json.dumps(result))
        legacy = store.root / "runs/ci_full/main" / self.head / "20260724T112410Z-old"
        legacy.mkdir(parents=True)
        (legacy / "delivery-summary.txt").write_text(
            f"target_sha: {self.head}\nbranch: old-main\nstatus: 1\nbackend_profile: sophgo-cmodel\n"
            "flaggems_test_mode: full\nflaggems_status: fail\ncompile_time_status: pass\n"
            "backend_rebuild_status: pass\nprivate_path: /private/credentials\n")
        (legacy / "flaggems-summary.json").write_text(json.dumps({"mode": "full", "results": [{"op": "add", "test_status": "失败"}]}))
        (legacy / "compile-benchmark.json").write_text(json.dumps({"summary": {"add": {"compile_est": {"median_ms": 12}}}}))
        rows = g.history_rows(store, [])
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(row["historical"] for row in rows))
        self.assertNotIn("private", json.dumps(rows))
        old = next(row for row in rows if row["task"]["target_branch"] == "old-main")
        self.assertEqual(old["result"]["checks"][1]["details"]["flaggems-summary"]["mode"], "full")
        self.assertTrue(old["artifact_urls"]["flaggems-summary.json"].startswith("https://gitee.com/"))
        self.assertEqual(len(g.history_rows(store, [{"task": self.task, "result": result}])), 1)

    def test_result_comment_retries_ignore_presentation_and_recognize_legacy_report(self):
        client = g.GitHub(g.REPOSITORY, token="fixture")
        comments, writes = [], []
        def request(path, method="GET", data=None):
            if method == "GET":
                return comments
            self.assertEqual(method, "POST")
            writes.append(data)
            comments.append({**data, "user": {"login": "github-actions[bot]"}})
            return {}
        identity = {"kind": "result", "task_id": self.task["task_id"], "run_id": "run-1", "result_digest": "a" * 64}
        url = "https://gitee.com/example/results/blob/results/run-1/result.json"
        with patch.object(client, "request", side_effect=request):
            self.assertTrue(client.comment(self.task, "first rendering", event_key=identity))
            self.assertFalse(client.comment(self.task, "<details>new rendering</details>", event_key=identity))
            self.assertTrue(client.comment(self.task, "first rendering", event_key={**identity, "run_id": "run-2"}))
            linked_body = "## Local CI 审查反馈\n[报告](" + url + ")"
            linked_identity = {**identity, "run_id": "linked"}
            self.assertTrue(client.comment(self.task, linked_body, event_key=linked_identity, legacy_result_url=url))
            self.assertTrue(client.comment(self.task, linked_body, event_key={**linked_identity, "result_digest": "b" * 64}, legacy_result_url=url))
            comments.append({"user": {"login": "github-actions[bot]"},
                             "body": g.MARKER + "\n<!-- old marker -->\n## Local CI 审查反馈\n[完整报告](" + url + ")"})
            self.assertFalse(client.comment(self.task, "new heading", event_key={**identity, "run_id": "old"}, legacy_result_url=url))
            comments[-1]["user"]["login"] = "contributor"
            self.assertTrue(client.comment(self.task, "new heading", event_key={**identity, "run_id": "old"}, legacy_result_url=url))
        self.assertEqual(len(writes), 5)

    def test_collection_only_writes_selected_task_and_readonly_collection_is_quiet(self):
        control, results = self.store(g.CONTROL_BRANCH), self.store(g.RESULTS_BRANCH)
        other = {**self.task, "pr_number": 8}
        other["task_id"] = g.compute_task_id(other)
        for field, suffix in (("task_ref", "tested"), ("base_task_ref", "base"), ("head_task_ref", "head")):
            other[field] = f"ci/pr-8/{other['task_id']}/{suffix}"
        for task in (self.task, other):
            g.validate_task(task)
            control.put({f"current/{g.current_key(task)}.json": {"task_id": task["task_id"]},
                         f"tasks/{task['task_id']}.json": task})
            result = {**self.result(), "task": task}
            results.put({f"runs/{task['task_id']}/{result['run_id']}/result.json": result})
        with patch.object(g, "current_task", return_value=True):
            published = g.collect_results(self.gh, control, results, self.root / "dashboard", self.task["task_id"])
            self.assertEqual([row["task_id"] for row in published], [self.task["task_id"]])
            self.assertEqual([row[0] for row in self.gh.statuses], [self.task["task_id"]])
            self.assertEqual(len(self.gh.comments), 1)
            self.gh.writes.clear()
            self.assertEqual(g.collect_results(self.gh, control, results, self.root / "dashboard", ""), [])
            self.assertEqual(self.gh.writes, [])
        feed = json.loads((self.root / "dashboard/tasks.json").read_bytes())
        self.assertEqual(len(feed["tasks"]), 2)

    def test_pr_checks_and_summary_are_published_on_head_sha(self):
        client = g.GitHub(g.REPOSITORY, token="fixture")
        snapshots = {self.head: [
            {"context": g.SUMMARY_CONTEXT, "state": "pending", "creator": {"login": "github-actions[bot]"}},
            {"context": "local-ci/sophgo-cmodel", "state": "error", "creator": {"login": "github-actions[bot]"}},
            {"context": "external/build", "state": "failure", "creator": {"login": "another-bot"}},
        ], self.tested: []}
        writes = []
        def request(path, method="GET", data=None):
            if method == "GET":
                if "/check-runs?" in path:
                    return {"check_runs": []}
                return snapshots[path.split("/")[1]]
            writes.append((path, data))
            if path.startswith("statuses/"):
                snapshots[path.split("/")[1]].insert(0, {**data, "creator": {"login": "github-actions[bot]"}})
            return {}
        with patch.object(client, "request", side_effect=request):
            client.status(self.task, "success", "Local CI: pass", "https://gitee.com/report")
            client.check(self.task, "basic", "in_progress", None, "", "Running", run_id="123")
            self.assertEqual(writes[0][0], f"statuses/{self.head}")
            self.assertEqual(writes[0][1]["state"], "success")
            self.assertEqual(writes[1][0], "check-runs")
            self.assertEqual(writes[1][1]["head_sha"], self.head)
            self.assertNotIn(self.tested, json.dumps(writes))
            self.assertNotIn("sophgo", json.dumps(writes))
            client.status(self.task, "success", "Local CI: pass", "https://gitee.com/other-report")
            self.assertEqual(len(writes), 2)

    def test_reopen_closes_pending_tested_sha_check_and_retired_status(self):
        client = g.GitHub(g.REPOSITORY, token="fixture")
        snapshots = {
            self.head: [{"context": "local-ci/sophgo-cmodel", "state": "pending",
                         "creator": {"login": "github-actions[bot]"},
                         "target_url": "https://github.com/example/run/1"}],
            self.tested: [],
        }
        writes = []

        def request(path, method="GET", data=None):
            if method == "GET":
                if "/check-runs?" in path:
                    if path.startswith(f"commits/{self.tested}/") and f"check_name={g.quote(g.CHECK_NAMES['basic'])}&" in path:
                        return {"check_runs": [{
                            "id": 42, "name": g.CHECK_NAMES["basic"], "status": "queued",
                            "app": {"slug": "github-actions"},
                            "external_id": f"triton-anchor-local-ci:basic:{self.task['task_id']}",
                        }]}
                    return {"check_runs": []}
                if "/statuses?" in path:
                    return snapshots[path.split("/")[1]]
                raise AssertionError(path)
            writes.append((path, data))
            if path.startswith("statuses/"):
                snapshots[path.split("/")[1]].insert(0, {**data, "creator": {"login": "github-actions[bot]"}})
            return {}

        with patch.object(client, "request", side_effect=request):
            client.retire_open_checks(self.task)
        self.assertEqual([path for path, _ in writes], ["check-runs/42", f"statuses/{self.head}"])
        self.assertEqual(writes[0][1]["conclusion"], "cancelled")
        self.assertEqual(writes[1][1]["context"], "local-ci/sophgo-cmodel")
        self.assertEqual(writes[1][1]["state"], "error")

    def test_approval_context_is_frozen_and_displays_contributor_claims_safely(self):
        client = g.GitHub(g.REPOSITORY, token="fixture")
        pull = {**self.gh.pull, "user": {"login": "contributor"}, "changed_files": 2}
        files = [{"filename": ".github/workflows/test.yml", "additions": 3, "deletions": 1},
                 {"filename": "pyproject.toml", "additions": 2, "deletions": 0}]
        def request(path, method="GET", data=None):
            return files if "/files?" in path else pull
        with patch.object(client, "request", side_effect=request):
            context = client.approval_context(self.task)
            card = g.approval_card(self.task, dict.fromkeys(("prepare", *g.CHECK_NAMES), "success"), True, context=context)
            for text in ("1 个 CI 与结果展示文件", "1 个依赖与构建配置文件"):
                self.assertIn(text, card)
            pull["head"] = {**pull["head"], "sha": "e" * 40}
            with self.assertRaisesRegex(ValueError, "PR changed"):
                client.approval_context(self.task)

    def test_result_comment_preserves_reported_blockers_and_important_limitations(self):
        result = self.result()
        result["status"] = "fail"
        result["checks"][0].update(status="skipped", summary="执行被中断")
        result["blocking_reasons"] = [
            "最低必检未通过：control_plane — 执行被中断",
            "性能结果不可比：两次测量使用的 LLVM 版本不同",
        ]
        result["findings"] = [{"summary": "架构契约遭到破坏", "blocking": True}]
        rendered = g.result_comment(result)
        limitations = rendered.split("### 限制说明", 1)[1]
        for reason in [*result["blocking_reasons"], result["findings"][0]["summary"]]:
            self.assertIn(g.feedback_text(reason), limitations)

    def test_review_evidence_links_only_safe_paths_on_the_frozen_revision(self):
        self.assertEqual(g.feedback_evidence({"kind": "architecture", "evidence": [
            "../secret", "/absolute/path", "https://evil.invalid",
        ]}, self.task, {}), "")
        link = g.feedback_evidence({"kind": "architecture", "evidence": ["src/file.py:17"]}, self.task, {})
        self.assertIn(f"/blob/{self.tested}/src/file.py#L17", link)

    def test_checks_appear_only_as_their_stage_is_reached(self):
        control = self.store(g.CONTROL_BRANCH)
        stages = {key: "success" for key in ("prepare", *g.CHECK_NAMES)}
        events = []
        with patch.object(self.gh, "check", side_effect=lambda *args, **kwargs: events.append((args[1], args[2])) or True) as check, \
                patch.object(self.gh, "status", side_effect=lambda *args: events.append(("summary", args[1]))), \
                patch.object(g, "GitHub", return_value=self.gh), \
                patch.object(g, "load_task", return_value=self.task), patch.object(g, "output"), \
                patch.dict(g.os.environ, {"GITHUB_STEP_SUMMARY": ""}):
            g.begin_checks(self.gh, self.task)
            self.assertEqual(events[:2], [("basic", "queued"), ("summary", "pending")])
            self.assertEqual(check.call_args.kwargs, {"restart": True})
            for stage, visible in (("basic", ["basic", "api"]),
                                   ("api", ["basic", "api", "security"]),
                                   ("security", ["basic", "api", "security"])):
                self.assertTrue(g.sync_preflight(self.gh, self.task, {stage: "success"}))
                self.assertEqual(list(dict.fromkeys(key for key, _ in events if key != "summary")), visible)
            with patch.object(g.sys, "argv", ["gateway.py", "card", "--stages", json.dumps(stages)]):
                self.assertEqual(g.main(), 0)
            self.assertEqual(events[-1], ("approve", "in_progress"))
            self.assertEqual(list(dict.fromkeys(key for key, _ in events if key != "summary")), ["basic", "api", "security", "approve"])
            with patch.object(g.sys, "argv", ["gateway.py", "approval"]):
                self.assertEqual(g.main(), 0)
            self.assertEqual(events[-1], ("approve", "completed"))
            g.enqueue(self.task, self.gh, control, self.source)
            self.assertEqual(events[-3:], [("dispatch", "in_progress"), ("summary", "pending"), ("dispatch", "completed")])
            self.assertEqual(list(dict.fromkeys(key for key, _ in events if key != "summary")),
                             ["basic", "api", "security", "approve", "dispatch"])

    def test_same_task_receiver_waits_for_successful_dispatch(self):
        control = self.store(g.CONTROL_BRANCH)
        g.enqueue(self.task, self.gh, control, self.source)
        results = self.store(g.RESULTS_BRANCH)
        result = self.result()
        results.put({f"runs/{self.task['task_id']}/{result['run_id']}/result.json": result})
        before = (list(self.gh.statuses), list(self.gh.comments))
        self.assertTrue(g.current_task(self.gh, control, self.task))  # Legacy tasks have no dispatch Check Run.
        for status, conclusion in (("not_started", None), ("queued", None), ("in_progress", None), ("completed", "failure")):
            with self.subTest(status=status), patch.object(self.gh, "latest_dispatch", return_value={
                "status": status, "conclusion": conclusion,
            }), patch.object(g.time, "sleep") as sleep:
                self.assertFalse(g.current_task(self.gh, control, self.task))
                self.assertEqual(g.collect_results(self.gh, control, results, self.root / "dashboard"), [])
                self.assertEqual(g.receive_result(self.gh, str(self.remote), self.task["task_id"]), "obsolete")
                sleep.assert_not_called()
            self.assertEqual((self.gh.statuses, self.gh.comments), before)
        with patch.object(self.gh, "latest_dispatch", return_value={"status": "completed", "conclusion": "success"}):
            self.assertTrue(g.current_task(self.gh, control, self.task))
            self.assertEqual(len(g.collect_results(self.gh, control, results, self.root / "dashboard")), 1)
        client = g.GitHub(g.REPOSITORY, token="fixture")
        url = f"https://github.com/{g.REPOSITORY}/runs/"
        start = {"id": 20, "details_url": url + "20", "started_at": "2026-09-17T00:10:00Z",
                 "output": {"summary": "<!-- local-ci-workflow:200 -->"}}
        dispatch = {"id": 10, "name": g.ALL_CHECK_NAMES["dispatch"], "status": "completed", "conclusion": "success",
                    "external_id": f"triton-anchor-local-ci:dispatch:{self.task['task_id']}",
                    "details_url": url + "10", "app": {"slug": "github-actions"}}
        for completed_at, run_id, current in (
            ("2026-09-17T00:09:00Z", "200", False),
            ("2026-09-17T00:11:00Z", "200", True),
            ("2026-09-17T00:11:00Z", "100", False),
            ("2026-09-17T00:11:00Z", "", False),
        ):
            row = {**dispatch, "completed_at": completed_at,
                   "output": {"summary": f"<!-- local-ci-workflow:{run_id} -->"}}
            with self.subTest(completed_at=completed_at, run=run_id), \
                    patch.dict(g.os.environ, {"GITHUB_RUN_ID": "999"}), \
                    patch.object(client, "task_start", return_value=start), \
                    patch.object(client, "check_runs", return_value=[row]), \
                    patch.object(self.gh, "latest_dispatch", side_effect=client.latest_dispatch):
                self.assertEqual(client.latest_dispatch(self.task), row if current else {"status": "not_started"})
                self.assertEqual(g.current_task(self.gh, control, self.task), current)

    def test_prepare_exception_comments_without_writing_summary_on_head(self):
        writes = []

        def urlopen(request, timeout=30):
            path = request.full_url.split(f"/repos/{g.REPOSITORY}/", 1)[1]
            if request.get_method() == "GET" and path == "pulls/7":
                payload = self.gh.pull
            elif request.get_method() == "GET" and path.startswith("issues/7/comments?"):
                payload = []
            elif request.get_method() == "POST" and path == "issues/7/comments":
                writes.append((path, json.loads(request.data)))
                payload = {}
            else:
                raise AssertionError((path, request.get_method()))
            return io.BytesIO(json.dumps(payload).encode())

        original = Path.cwd()
        try:
            g.os.chdir(self.root)
            with patch("urllib.request.urlopen", side_effect=urlopen), patch.object(g.sys, "argv", [
                "gateway.py", "prepare", "--worker-sha", "invalid",
            ]), patch.dict(g.os.environ, {
                "GH_TOKEN": "fixture", "PR_NUMBER": "7", "REQUESTED_SHA": self.head,
                "GITHUB_REPOSITORY": g.REPOSITORY, "GITHUB_RUN_ID": "200",
            }), self.assertRaises(SystemExit) as stopped:
                runpy.run_path(str(ROOT / "scripts/ci/gateway.py"), run_name="__main__")
            self.assertEqual(stopped.exception.code, 1)
        finally:
            g.os.chdir(original)
        self.assertEqual([path for path, _ in writes], ["issues/7/comments"])
        self.assertIn("Invalid trusted worker revision", writes[0][1]["body"])

    def test_new_preflight_blocks_old_result_before_new_gitee_enqueue(self):
        control = self.store(g.CONTROL_BRANCH)
        g.enqueue(self.task, self.gh, control, self.source)
        results = self.store(g.RESULTS_BRANCH)
        result = self.result()
        results.put({f"runs/{self.task['task_id']}/{result['run_id']}/result.json": result})
        gh = g.GitHub(g.REPOSITORY, token="fixture")
        newer = {"id": 12, "name": g.CHECK_NAMES["basic"], "status": "queued",
                 "external_id": "triton-anchor-local-ci:basic:new-task",
                 "app": {"slug": "github-actions"}}
        before = len(self.gh.statuses)
        with (
            patch.object(gh, "request", side_effect=lambda path:
                         [] if "/statuses?" in path else {"check_runs": [newer]}),
            patch.object(self.gh, "owns_task", side_effect=gh.owns_task),
        ):
            self.assertEqual(g.collect_results(self.gh, control, results, self.root / "dashboard"), [])
        self.assertEqual(len(self.gh.statuses), before)

    def test_rejected_approval_notifies_contributor_with_commit_and_review(self):
        stages = {key: "success" for key in ("prepare", *g.CHECK_NAMES)}
        stages.update(card="success", approval="failure", enqueue="skipped")
        self.gh.approvals = [{
            "state": "rejected",
            "comment": "Please remove @generated files\n before retrying.",
            "environments": [{"name": "local-ci-fork-approval"}],
            "user": {"login": "maintainer"},
        }]
        with patch.dict(g.os.environ, {
            "GITHUB_RUN_ID": "12345", "GITHUB_REPOSITORY": g.REPOSITORY,
            "GITHUB_SERVER_URL": "https://github.com",
        }):
            g.finalize_preflight(self.gh, self.task, stages)
        comment = self.gh.comments[0]
        self.assertTrue(comment.startswith("@contributor   **进入 Local CI 审批未通过**"))
        self.assertIn(f"PR 提交：`{self.head}`", comment)
        self.assertIn("审核批注：\n\n> Please remove ＠generated files before retrying.", comment)
        self.assertIn("如有疑问可进一步联系审核者进行处理，感谢您的贡献！", comment)

    def test_closed_pr_finishes_pending_checks_without_dispatched_task(self):
        gh = g.GitHub(g.REPOSITORY, token="fixture")
        writes = []
        pending = {"name": "local-ci/basic", "status": "queued", "id": 12,
                   "external_id": "triton-anchor-ci-v4:basic:old",
                   "app": {"slug": "github-actions"}}
        completed = {**pending, "id": 13, "status": "completed"}

        def request(path, method="GET", data=None):
            if method != "GET":
                writes.append((path, data))
                return {}
            if path == "pulls/7":
                return {**self.gh.pull, "state": "closed"}
            if "/statuses?" in path:
                return [{"context": g.SUMMARY_CONTEXT, "state": "pending"}]
            if "/check-runs?" in path:
                return {"check_runs": [pending, completed]}
            raise AssertionError(path)

        with patch.object(gh, "request", side_effect=request):
            self.assertTrue(gh.finish_inactive_pr(7))
        self.assertEqual([path for path, _ in writes], [f"statuses/{self.tested}", "check-runs/12", f"statuses/{self.head}"])
        self.assertEqual(writes[0][1]["state"], "error")
        self.assertEqual(writes[1][1]["conclusion"], "cancelled")

    def test_optimistic_control_writes_preserve_other_writer(self):
        first, second = self.store(g.CONTROL_BRANCH), self.store(g.CONTROL_BRANCH)
        first.put({"a.json": {"a": 1}})
        second.put({"b.json": {"b": 2}})
        first.refresh()
        self.assertEqual(first.get("a.json"), {"a": 1})
        self.assertEqual(first.get("b.json"), {"b": 2})
        with self.assertRaises(ValueError):
            first.put({"a.json": {"a": 3}}, ("a.json",))

    def test_security_scans_real_git_diff(self):
        (self.source / "unsafe.py").write_text("import socket\n")
        git(self.source, "add", ".")
        git(self.source, "commit", "-qm", "unsafe")
        tested = git(self.source, "rev-parse", "HEAD")
        with tempfile.TemporaryDirectory() as output:
            original = Path.cwd()
            try:
                import os

                os.chdir(output)
                self.assertEqual(g.security_diff(self.source, self.base, tested), 1)
            finally:
                os.chdir(original)

    def test_sarif_severity_gate_executes(self):
        folder = self.root / "sarif"
        folder.mkdir()
        document = {
            "runs": [
                {
                    "tool": {
                        "driver": {
                            "rules": [
                                {"id": "r", "properties": {"security-severity": "7.5"}}
                            ]
                        }
                    },
                    "results": [{"ruleId": "r", "level": "warning"}],
                }
            ]
        }
        (folder / "result.sarif").write_text(json.dumps(document))
        self.assertEqual(len(g.sarif_failures(folder)), 1)
        document["runs"][0]["results"] = []
        (folder / "result.sarif").write_text(json.dumps(document))
        self.assertEqual(g.sarif_failures(folder), [])

    def test_production_transport_allowlists(self):
        with self.assertRaises(ValueError):
            g.GitHub("RACE-org/triton-anchor")
        with self.assertRaises(ValueError):
            g.GitStore("https://github.com/RACE-org/triton-anchor", g.CONTROL_BRANCH)

    def test_preflight_failure_comment_keeps_github_and_unknown_causes_distinct(self):
        pull = {**self.gh.pull, "user": {"login": "contributor"}}
        github_message = g.preflight_failure_comment(
            ValueError("PR merge result is not ready; retry after GitHub finishes computing it"),
            "https://github.com/example/run/1",
            pull,
        )
        self.assertIn("@contributor", github_message)
        self.assertIn("GitHub 尚未完成 PR 合并状态准备", github_message)
        self.assertIn("不能据此判断 PR 代码或工作流逻辑失败", github_message)
        self.assertIn("查看工作流证据", github_message)

        unknown_message = g.preflight_failure_comment(RuntimeError("unexpected runtime"), pull=pull)
        self.assertIn("原因待确认", unknown_message)
        self.assertIn("现有信息不足以把原因归于 GitHub、工作流、Gitee 或 PR 代码", unknown_message)
        self.assertNotIn("修复对应检查或中转配置", unknown_message)

    def test_prepare_error_comment_does_not_require_a_frozen_task(self):
        gh = g.GitHub(g.REPOSITORY, token="fixture")
        context = {"head_sha": self.head, "tested_sha": self.head, "pr_number": 7}
        with patch.object(gh, "request", return_value=[]) as request:
            self.assertTrue(gh.comment(context, "CI 准备未完成"))
        self.assertEqual(request.call_args.args[:2], ("issues/7/comments", "POST"))

if __name__ == "__main__":
    unittest.main()
