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

    def request(self, path, method="GET", data=None):
        if path == "contents/triton/cmake?ref=" + self.tested:
            return [{"type": "file", "path": name} for name in self.cmake_files]
        if path == "environments/local-ci-fork-approval":
            return self.environment
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

    def reconcile_legacy_statuses(self, *args):
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
        snapshot = json.loads((self.root / "dashboard/tasks.json").read_text())
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
        snapshot = json.loads((self.root / "dashboard/tasks.json").read_text())
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

    def test_check_updates_exact_task_without_relabeling_another_task(self):
        gh = g.GitHub(g.REPOSITORY, token="fixture")
        exact = {
            "id": 11, "name": "local-ci/basic", "status": "queued",
            "external_id": f"triton-anchor-local-ci:basic:{self.task['task_id']}",
            "app": {"slug": "github-actions"},
        }
        other = {**exact, "id": 12, "external_id": "triton-anchor-local-ci:basic:other"}
        writes = []

        def request(path, method="GET", data=None):
            if method == "GET":
                self.assertIn("filter=all", path)
                return {"check_runs": [exact, other]}
            writes.append((path, data))
            return {}

        with patch.object(gh, "request", side_effect=request):
            gh.check(self.task, "basic", "completed", "success", "Passed", "Evidence")
            self.assertEqual(writes[-1][0], "check-runs/11")
            exact["external_id"] = "triton-anchor-local-ci:basic:older"
            gh.check(self.task, "basic", "queued", None, "Queued", "New task")
            self.assertEqual(writes[-1][0], "check-runs")

    def test_preflight_rerun_creates_new_check_after_completed_check(self):
        gh = g.GitHub(g.REPOSITORY, token="fixture")
        old = {
            "id": 11, "name": "local-ci/basic", "status": "completed",
            "conclusion": "success",
            "external_id": f"triton-anchor-local-ci:basic:{self.task['task_id']}",
            "app": {"slug": "github-actions"},
        }
        with patch.object(gh, "request", return_value={"check_runs": [old]}) as request:
            gh.check(self.task, "basic", "queued", None, "Queued", "Rerun")
        self.assertEqual(request.call_args.args[:2], ("check-runs", "POST"))
        self.assertNotIn("conclusion", request.call_args.args[2])

    def test_new_task_cancels_only_superseded_trusted_pending_checks(self):
        gh = g.GitHub(g.REPOSITORY, token="fixture")
        old = {"id": 11, "name": "local-ci/basic", "status": "queued",
               "external_id": "triton-anchor-local-ci:basic:old-task",
               "app": {"slug": "github-actions"}}
        runs = [old, {**old, "id": 12, "status": "completed", "conclusion": "failure"},
                {**old, "id": 13, "app": {"slug": "other"}}]
        with patch.object(gh, "request", return_value={"check_runs": runs}) as request:
            gh.check(self.task, "basic", "queued", None, "等待执行", "新任务")
        writes = [call for call in request.call_args_list if len(call.args) > 1]
        self.assertEqual([call.args[:2] for call in writes], [("check-runs/11", "PATCH"), ("check-runs", "POST")])
        self.assertEqual(writes[0].args[2]["conclusion"], "cancelled")

    def test_returning_task_identity_gets_newest_check_instead_of_reusing_old_wait(self):
        gh = g.GitHub(g.REPOSITORY, token="fixture")
        old = {"id": 11, "name": "local-ci/basic", "status": "queued",
               "external_id": f"triton-anchor-local-ci:basic:{self.task['task_id']}",
               "app": {"slug": "github-actions"}}
        newer = {**old, "id": 12, "external_id": "triton-anchor-local-ci:basic:other-task"}
        with patch.object(gh, "request", return_value={"check_runs": [old, newer]}) as request:
            gh.check(self.task, "basic", "queued", None, "等待执行", "重新请求")
        writes = [call for call in request.call_args_list if len(call.args) > 1]
        self.assertEqual([call.args[:2] for call in writes], [
            ("check-runs/11", "PATCH"), ("check-runs/12", "PATCH"), ("check-runs", "POST")])

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

    def test_late_stage_completion_cannot_overwrite_a_new_task(self):
        with patch.object(self.gh, "owns_task", return_value=False), patch.object(self.gh, "check") as check:
            self.assertFalse(g.sync_preflight(self.gh, self.task, {"basic": "success"}))
            check.assert_not_called()
        self.gh.pull["draft"] = True
        with patch.object(self.gh, "check") as check:
            self.assertFalse(g.sync_preflight(self.gh, self.task, {"security": "cancelled"}))
            check.assert_not_called()

    def test_late_stage_completion_cannot_advance_a_finished_preflight(self):
        with patch.object(self.gh, "latest_dispatch", return_value={"status": "completed", "conclusion": "success"}), patch.object(self.gh, "check") as check:
            self.assertFalse(g.sync_preflight(self.gh, self.task, {"basic": "success"}))
            check.assert_not_called()

    def test_duplicate_predecessor_cannot_reopen_completed_successor(self):
        gh = g.GitHub(g.REPOSITORY, token="fixture")
        row = {"id": 21, "name": "local-ci/api", "status": "completed", "conclusion": "success",
               "external_id": f"triton-anchor-local-ci:api:{self.task['task_id']}",
               "app": {"slug": "github-actions"}}
        with patch.object(gh, "request", return_value={"check_runs": [row]}) as request:
            self.assertFalse(gh.check(self.task, "api", "in_progress", None, "Running", "Previous stage passed"))
            self.assertTrue(all(call.args[1:] == () for call in request.call_args_list))

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
        runs = [github_check({"name": "local-ci/basic", "status": "queued",
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

    def test_control_push_keeps_summary_without_duplicate_preflight_checks(self):
        from types import SimpleNamespace

        task = {**self.task, "pr_number": 0, "event_kind": "push",
                "target_branch": "local-ci-unified", "tested_sha": self.head,
                "worker_revision_sha": self.head}
        task["task_id"] = g.compute_task_id(task)
        manual = {**task, "event_kind": "manual"}
        manual["task_id"] = g.compute_task_id(manual)
        gh = g.GitHub(g.REPOSITORY, token="fixture")
        statuses, checks = [], []
        run_url = f"https://github.com/{g.REPOSITORY}/actions/runs/"
        native_run = {"event": "push", "head_branch": "local-ci-unified", "head_sha": self.head,
                      "path": ".github/workflows/ci-gateway.yml"}
        native_runs = {"100": dict(native_run), "300": dict(native_run)}

        def request(path, method="GET", data=None):
            if method == "POST" and path == f"statuses/{self.head}":
                statuses.insert(0, data)
                return {}
            if method == "GET" and "/statuses?" in path:
                return statuses
            if method == "GET" and "/check-runs?" in path:
                return {"check_runs": [row for row in checks
                                       if "check_name=" + g.quote(row["name"], safe="") + "&" in path]}
            if method == "GET" and path.startswith("actions/runs/"):
                return native_runs[path.rsplit("/", 1)[1]]
            if method == "POST" and path == "check-runs":
                checks.append(github_check(data, len(checks) + 1))
                return {}
            if method == "PATCH" and path.startswith("check-runs/"):
                check_id = int(path.split("/")[-1])
                next(row for row in checks if row["id"] == check_id).update(github_check(data, check_id))
                return {}
            raise AssertionError((path, method))

        with patch.object(g, "is_current", return_value=True), \
                patch.object(gh, "request", side_effect=request), patch.dict(g.os.environ, {
                    "GITHUB_RUN_ID": "100", "GITHUB_REPOSITORY": g.REPOSITORY,
                    "GITHUB_SERVER_URL": "https://github.com",
                }):
            self.assertFalse(gh.owns_task(task))  # No ownership evidence yet.
            checks.append({"id": 1, "name": "Prepare exact task", "status": "in_progress",
                           "details_url": run_url + "100/job/1", "app": {"slug": "github-actions"}})
            g.begin_checks(gh, task)
            self.assertTrue(gh.owns_task(task, workflow=True))
            self.assertTrue(gh.task_start(task)["native"])
            for key in g.CHECK_NAMES:
                self.assertFalse(g.sync_preflight(gh, task, {key: "success"}))
            g.finalize_preflight(gh, task, {"prepare": "success", "basic": "failure"})
            self.assertEqual(statuses, [])
            self.assertEqual([row["name"] for row in checks], ["Prepare exact task"])
            g.finalize_preflight(gh, task, {"enqueue": "success"})
            self.assertEqual(len(statuses), 0)

            # New manual preflight owns the same SHA before its Gitee enqueue.
            with patch.dict(g.os.environ, {"GITHUB_RUN_ID": "200"}):
                g.begin_checks(gh, manual)
                self.assertTrue(gh.owns_task(manual, workflow=True))
            self.assertEqual(len(checks), 2)
            self.assertEqual(checks[-1]["name"], "local-ci/basic")
            self.assertTrue(gh.owns_task(manual))
            self.assertFalse(gh.owns_task(task))
            before = copy.deepcopy((checks, statuses))
            with self.assertRaisesRegex(ValueError, "newer workflow"):
                g.begin_checks(gh, task)
            self.assertEqual((checks, statuses), before)
            g.finalize_preflight(gh, task, {"basic": "failure"})
            self.assertEqual(len(statuses), 0)
            control = SimpleNamespace(get=lambda path: {"task_id": task["task_id"]}
                                      if path.startswith("current/") else None)
            self.assertFalse(g.current_task(gh, control, task))

            # A new native task can also supersede manual checks left on this SHA.
            checks.append({"id": 3, "name": "Prepare exact task", "status": "in_progress",
                           "details_url": run_url + "300/job/3", "app": {"slug": "github-actions"}})
            for changed in ({"event": "workflow_dispatch"}, {"head_branch": "main"},
                            {"head_sha": "e" * 40}, {"path": ".github/workflows/other.yml"}):
                with self.subTest(native=changed), patch.dict(gh.run_identities, {}, clear=True):
                    native_runs["300"] = {**native_run, **changed}
                    self.assertEqual(gh.task_start(task)["id"], 2)
            native_runs["300"] = dict(native_run)
            with patch.dict(g.os.environ, {"GITHUB_RUN_ID": "300"}):
                g.begin_checks(gh, task)
                self.assertTrue(gh.owns_task(task, workflow=True))
            self.assertTrue(gh.owns_task(task))
            self.assertFalse(gh.owns_task(manual))
            g.finalize_preflight(gh, manual, {"basic": "failure"})
            self.assertEqual(len(statuses), 0)
            self.assertEqual(len(checks), 3)
            self.assertEqual(checks[1]["conclusion"], "cancelled")
            self.assertEqual(gh.latest_dispatch(task), {"status": "not_started"})
            for url in ("https://github.com/example/repo/actions/runs/1",
                        "https://gitee.com/example/results/blob/results/result.json", ""):
                gh.status(task, "error", "Local CI: retry publication", url)
                self.assertTrue(gh.owns_task(task))
                self.assertTrue(statuses[0]["target_url"].startswith(url or "https://"))

        for change in ({"target_branch": "main"}, {"event_kind": "manual"},
                       {"pr_number": 7, "event_kind": "pull_request"},
                       {"worker_revision_sha": self.base}):
            with self.subTest(change=change), \
                    patch.object(gh, "request", return_value={"check_runs": []}) as request:
                self.assertTrue(gh.check({**task, **change}, "basic", "completed",
                                         "success", "Passed", "Evidence"))
                self.assertEqual(request.call_args.args[:2], ("check-runs", "POST"))
                self.assertEqual(request.call_args.args[2]["head_sha"], self.head)

    def test_stage_completion_rejects_unknown_or_nonterminal_outcomes(self):
        for stages in (None, [], ["basic"], {}, {"summary": "success"},
                       {"api": "in_progress"}, {"api": ["success"]}):
            with self.subTest(stages=stages), self.assertRaises(ValueError):
                g.sync_preflight(self.gh, self.task, stages)

    def test_checks_use_english_while_pr_feedback_remains_chinese(self):
        with patch.object(self.gh, "check") as check:
            g.begin_checks(self.gh, self.task)
        self.assertTrue(all(call.args[4].isascii() and call.args[5].isascii() for call in check.call_args_list))
        for state in g.GITHUB_STATES:
            self.assertTrue(g.publication_description(state, "a" * 64).isascii())
        stages = {key: "success" for key in ("prepare", *g.CHECK_NAMES)}
        g.finalize_preflight(self.gh, self.task, {**stages, "approval": "failure"})
        self.assertTrue(self.gh.check_calls[-1][4].isascii())
        self.assertEqual(self.gh.comments, [])

    def test_failed_card_is_not_reported_as_a_rejected_approval(self):
        stages = {key: "success" for key in ("prepare", *g.CHECK_NAMES)}
        g.finalize_preflight(self.gh, self.task, {**stages, "card": "failure", "approval": "skipped"})
        self.assertIn("card publication failed", self.gh.check_calls[-1][4])
        self.assertEqual(self.gh.comments, [])

    def test_approval_card_has_frozen_identity_evidence_and_admission_boundary(self):
        stages = {key: "success" for key in ("prepare", *g.CHECK_NAMES)}
        with patch.dict(g.os.environ, {"GITHUB_RUN_ID": "12345"}):
            card = g.approval_card(self.task, stages, True)
        for field in ("head_sha", "base_sha", "tested_sha"):
            self.assertIn(self.task[field], card)
        self.assertNotIn("控制版本", card)
        self.assertNotIn(self.task["task_id"], card)
        for text in ("基础检查 | 通过", "API 兼容性 | 通过", "安全检查 | 通过",
                     "等待维护者审批", "local-ci-fork-approval", "/actions/runs/12345"):
            self.assertIn(text, card)
        blocked = g.approval_card(self.task, {**stages, "basic": "failure"}, False)
        self.assertEqual(blocked, "")
        config_error = g.approval_card(self.task, stages, False, "缺少 required reviewers")
        self.assertIn("缺少 required reviewers", config_error)
        internal = g.approval_card({**self.task, "external_fork": False}, stages, True)
        self.assertEqual(internal, "")
        url = f"https://github.com/{g.REPOSITORY}/actions/runs/12345"
        with patch.object(g, "GitHub", return_value=self.gh), \
                patch.object(g, "load_task", return_value=self.task), \
                patch.object(g, "output") as output, patch.dict(g.os.environ, {
                    "GITHUB_RUN_ID": "12345", "GITHUB_REPOSITORY": g.REPOSITORY,
                    "GITHUB_SERVER_URL": "https://github.com", "GITHUB_STEP_SUMMARY": "",
                }):
            with patch.object(g.sys, "argv", ["gateway.py", "card", "--stages", json.dumps(stages)]):
                self.assertEqual(g.main(), 0)
            waiting = self.gh.check_calls[-1]
            self.assertEqual(waiting[1:4], ("approve", "in_progress", None))
            self.assertEqual(waiting[6], url)
            self.assertIn(f"[Open approval controls and workflow evidence]({url})", waiting[5])
            output.assert_called_once_with("eligible", True)
            with patch.object(g.sys, "argv", ["gateway.py", "approval"]):
                self.assertEqual(g.main(), 0)
            self.assertEqual(self.gh.check_calls[-1][1:4], ("approve", "completed", "success"))
            self.assertEqual(self.gh.check_calls[-1][6], url)
        self.gh.check_calls = []
        self.gh.comments = []
        internal = {**self.task, "external_fork": False}
        with patch.object(g, "GitHub", return_value=self.gh), \
                patch.object(g, "load_task", return_value=internal), \
                patch.object(g, "output") as output, patch.dict(g.os.environ, {
                    "GITHUB_RUN_ID": "12345", "GITHUB_REPOSITORY": g.REPOSITORY,
                    "GITHUB_SERVER_URL": "https://github.com", "GITHUB_STEP_SUMMARY": "",
                }), patch.object(g.sys, "argv", ["gateway.py", "card", "--stages", json.dumps(stages)]):
            self.assertEqual(g.main(), 0)
            output.assert_called_once_with("eligible", True)
        self.assertFalse(any(call[1] == "approve" for call in self.gh.check_calls))
        self.assertEqual(self.gh.comments, [])
        self.assertEqual(self.gh.statuses, [])

    def test_failed_prerequisites_never_publish_card_or_verify_approval(self):
        passed = {key: "success" for key in ("prepare", *g.CHECK_NAMES)}
        summary_path = self.root / "step-summary.md"
        for key in passed:
            for outcome in ("failure", "cancelled", "skipped", None):
                stages = {**passed, key: outcome}
                with (
                    self.subTest(stage=key, outcome=outcome),
                    patch.object(g.sys, "argv", ["gateway.py", "card", "--stages", json.dumps(stages)]),
                    patch.object(g, "GitHub", return_value=self.gh),
                    patch.object(g, "load_task", return_value=self.task),
                    patch.object(g, "validate_approval_environment") as approval,
                    patch.object(g, "output") as output,
                    patch.dict(g.os.environ, {"GITHUB_STEP_SUMMARY": str(summary_path)}),
                ):
                    self.assertEqual(g.main(), 0)
                    approval.assert_not_called()
                    output.assert_called_once_with("eligible", False)
                    self.assertEqual(g.approval_card(self.task, stages, False), "")
                    self.assertEqual(self.gh.comments, [])
                    self.assertFalse(summary_path.exists())
                g.finalize_preflight(self.gh, self.task, {**stages, "card": "skipped"})
                self.assertEqual(self.gh.comments, [])

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

    def test_result_comment_is_chinese_scoped_to_run_and_separates_unexecuted_checks(self):
        result = self.result()
        result["summary"] = "仅调整 CI；已核对任务协议。"
        result["checks"][0].update(summary="通过定向验证", evidence=["validation.md"])
        result["checks"].append({"tool_id": "frontend_build", "status": "not_selected", "summary": "无需构建"})
        result["checks"].append({"tool_id": "backend_build", "status": "not_applicable", "summary": "当前版本不支持后端"})
        result["checks"].append({"tool_id": "frontend_tests", "status": "skipped", "summary": "本次无需执行"})
        result["findings"] = [{"summary": "<script> @owner [点此](https://evil.invalid)", "blocking": False}]
        rendered = g.result_comment(result, "https://gitee.com/example/result.json",
                                    {"validation.md": "https://gitee.com/example/validation.md"})
        for text in ("本次要求的检查已通过", self.head, self.tested,
                     "CI 流程验证 | 通过", "PR 意图与属性核对 | 通过", "架构契约审查 | 通过",
                     "合入阻塞与重要限制", "| 前端构建 | 本次未选择 | 无需构建 |",
                     "| 后端构建 | 不适用 | 当前版本不支持后端 |", "| 前端测试 | 未执行 | 本次无需执行 |"):
            self.assertIn(text, rendered)
        limitations = rendered.split("### 合入阻塞与重要限制", 1)[1]
        for text in ("前端构建", "后端构建", "前端测试", "无需构建", "当前版本不支持后端", "本次无需执行"):
            self.assertNotIn(text, limitations)
        self.assertNotIn("<script>", rendered)
        self.assertNotIn("@owner", rendered)
        self.assertNotIn("[点此](https://evil.invalid)", rendered)
        self.assertNotIn("| pass |", rendered)
        for text in (result["run_id"], self.task["task_id"], "证据 1", "validation.md", "/README.md"):
            self.assertNotIn(text, rendered)
        self.assertIn(f"PR 提交：`{self.head}`\n\n合并后验证提交：`{self.tested}`", rendered)
        again = g.result_comment({**result, "run_id": "retry-2"})
        self.assertNotEqual(rendered, again)

    def test_preflight_is_restored_on_tested_commit_before_summary(self):
        client = g.GitHub(g.REPOSITORY, token="fixture")
        writes = []
        migrated = {}

        def request(path, method="GET", data=None):
            if method == "POST":
                writes.append((path, data))
                if path == "check-runs":
                    migrated[data["name"]] = github_check(data, 100)
                return {}
            key = next(key for key in g.CHECK_NAMES if f"%2F{key}" in path)
            name = g.CHECK_NAMES[key]
            if f"commits/{self.tested}/" in path:
                return {"check_runs": [migrated[name]] if name in migrated else []}
            self.assertIn(f"commits/{self.head}/", path)
            return {"check_runs": [{"id": 1, "name": name, "status": "completed",
                "started_at": "2026-09-16T01:00:00Z",
                "conclusion": "success", "app": {"slug": "github-actions"},
                "external_id": f"triton-anchor-local-ci:{key}:{self.task['task_id']}",
                "output": {"title": "Passed", "summary": "Verified"}}]}

        with patch.object(client, "request", side_effect=request), \
                patch.dict(g.os.environ, {"GITHUB_RUN_ID": "999"}):
            client.restore_preflight(self.task)
            client.restore_preflight(self.task)  # Retry must not duplicate the migrated checks.
            self.assertEqual(client.task_start(self.task), {})  # Migration is not a new workflow.
            self.assertTrue(all(g.check_workflow_id(row) == "" for row in migrated.values()))
            self.assertEqual(migrated["local-ci/basic"]["started_at"], "2026-09-16T01:00:00Z")
            client.status(self.task, "success", "Local CI: pass")
        self.assertEqual(len(writes), 4)
        self.assertEqual([data["head_sha"] for path, data in writes[:3]], [self.tested] * 3)
        self.assertEqual(writes[-1][0], f"statuses/{self.tested}")
        with patch.object(client, "check_runs", return_value=[]), patch.object(client, "check") as check:
            with self.assertRaisesRegex(ValueError, "Missing preflight"):
                client.restore_preflight(self.task)
            check.assert_not_called()
        failure = {"id": 1, "name": "local-ci/basic", "status": "completed", "conclusion": "failure", "app": {"slug": "github-actions"},
                   "external_id": f"triton-anchor-local-ci:basic:{self.task['task_id']}"}
        with patch.object(client, "check_runs", return_value=[failure]), patch.object(client, "check") as check:
            with self.assertRaisesRegex(ValueError, "not successful"):
                client.restore_preflight(self.task)
            check.assert_not_called()

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

    def test_legacy_head_statuses_follow_current_verdict_without_new_contexts(self):
        client = g.GitHub(g.REPOSITORY, token="fixture")
        snapshots = {self.head: [
            {"context": "local-ci/summary", "state": "pending", "creator": {"login": "github-actions[bot]"}},
            {"context": "local-ci/sophgo-cmodel", "state": "error", "creator": {"login": "github-actions[bot]"}},
            {"context": "external/build", "state": "failure", "creator": {"login": "another-bot"}},
        ], self.tested: [{"context": "local-ci/summary", "state": "success"}]}
        writes = []
        def request(path, method="GET", data=None):
            if path == "pulls/7":
                return self.gh.pull
            if method == "GET":
                return snapshots[path.split("/")[1]]
            writes.append((path, data))
            snapshots[path.split("/")[1]].insert(0, {**data, "creator": {"login": "github-actions[bot]"}})
            return {}
        with patch.object(client, "request", side_effect=request):
            client.reconcile_legacy_statuses(self.task, "success", "Local CI: pass", "https://gitee.com/report")
            self.assertEqual(len(writes), 2)
            self.assertTrue(all(path == f"statuses/{self.head}" and data["state"] == "success" for path, data in writes))
            self.assertIn("Retired context", writes[1][1]["description"])
            client.reconcile_legacy_statuses(self.task, "success", "Local CI: pass", "https://gitee.com/report")
            self.assertEqual(len(writes), 2)
            snapshots[self.tested][0]["state"] = "failure"
            client.reconcile_legacy_statuses(self.task, "failure", "Local CI: preflight failed", "https://gitee.com/report")
            self.assertTrue(all(data["state"] == "failure" for path, data in writes[2:]))
            before = len(writes)
            snapshots[self.tested][0]["target_url"] = "https://gitee.com/report#local-ci-task=" + "f" * 64
            client.reconcile_legacy_statuses(self.task, "failure", "foreign ownership", "https://gitee.com/report")
            self.assertEqual(len(writes), before)
            self.gh.pull["head"]["sha"] = "f" * 40
            before = len(writes)
            client.reconcile_legacy_statuses(self.task, "success", "stale pass", "https://gitee.com/report")
            self.assertEqual(len(writes), before)

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
            for text in ("contributor", "2 个文件，+5 / -1", "CI 与结果展示 1 个文件", "依赖与构建配置 1 个文件", "/pull/7/files", "| PR 信息 | 通过 |"):
                self.assertIn(text, card)
            self.assertNotIn("审批关注", card)
            self.assertNotIn(".github/workflows/test.yml", card)
            self.assertNotIn("pyproject.toml", card)
            self.assertNotIn("贡献者说明", card)
            self.assertNotIn("任务范围与审批边界", card)
            self.assertLess(card.index("本次审批对应的固定版本"), card.index("本次改动概览"))
            pull["head"] = {**pull["head"], "sha": "e" * 40}
            with self.assertRaisesRegex(ValueError, "PR changed"):
                client.approval_context(self.task)

    def test_collapsible_review_details_preserve_table_rows_and_show_finding_risk(self):
        result = self.result()
        result["findings"] = [{"summary": "Need benchmark", "severity": "medium", "blocking": False},
                              {"summary": "Unrated", "blocking": False}]
        body = g.result_comment(result)
        details = body.split("### 查看审查详情\n\n", 1)[1].split("</details>", 1)[0]
        self.assertTrue(details.startswith("<details>\n<summary>展开检查与审查记录</summary>\n\n"))
        self.assertTrue(details.split("</summary>\n\n", 1)[1].startswith("| 检查 | 结果 | 说明 |"))
        self.assertLess(body.index("### 变更意图与审查结论"), body.index("### 查看审查详情"))
        self.assertLess(body.index("</details>"), body.index("### 合入阻塞与重要限制"))
        self.assertNotIn("本次记录", body)
        self.assertIn("【风险：中】Need benchmark", body)
        self.assertIn("【风险：未标注】Unrated", body)
        self.assertIn("| 架构契约审查 | 通过 | Compatible |", body)

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
        limitations = rendered.split("### 合入阻塞与重要限制", 1)[1]
        for reason in [*result["blocking_reasons"], result["findings"][0]["summary"]]:
            self.assertIn(g.feedback_text(reason), limitations)
        self.assertIn("| CI 流程验证 | 未执行 | 执行被中断 |", rendered)

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
            self.assertEqual(events, [("basic", "queued")])
            self.assertEqual(check.call_args.kwargs, {"restart": True})
            for stage, visible in (("basic", ["basic", "api"]),
                                   ("api", ["basic", "api", "security"]),
                                   ("security", ["basic", "api", "security"])):
                self.assertTrue(g.sync_preflight(self.gh, self.task, {stage: "success"}))
                self.assertEqual(list(dict.fromkeys(key for key, _ in events)), visible)
            with patch.object(g.sys, "argv", ["gateway.py", "card", "--stages", json.dumps(stages)]):
                self.assertEqual(g.main(), 0)
            self.assertEqual(events[-1], ("approve", "in_progress"))
            self.assertEqual(list(dict.fromkeys(key for key, _ in events)), ["basic", "api", "security", "approve"])
            with patch.object(g.sys, "argv", ["gateway.py", "approval"]):
                self.assertEqual(g.main(), 0)
            self.assertEqual(events[-1], ("approve", "completed"))
            g.enqueue(self.task, self.gh, control, self.source)
            self.assertEqual(events[-3:], [("dispatch", "in_progress"), ("summary", "pending"), ("dispatch", "completed")])
            self.assertEqual(list(dict.fromkeys(key for key, _ in events)),
                             ["basic", "api", "security", "approve", "dispatch", "summary"])

    def test_same_sha_rerun_resets_checks_and_rejects_late_workflow_updates(self):
        client = g.GitHub(g.REPOSITORY, token="fixture")
        run_url = f"https://github.com/{g.REPOSITORY}/actions/runs/"
        checks = [
            {"id": index, "name": name,
             "status": "in_progress" if key in {"api", "security", "approve"} else "completed",
             "conclusion": None if key in {"api", "security", "approve"} else "success",
             "external_id": f"triton-anchor-local-ci:{key}:{self.task['task_id']}",
             "details_url": f"https://github.com/{g.REPOSITORY}/runs/{index}",
             "output": {"summary": "<!-- local-ci-workflow:100 -->"},
             "app": {"slug": "github-actions"}}
            for index, (key, name) in enumerate(g.ALL_CHECK_NAMES.items(), 1)
        ]
        statuses = [{"context": "local-ci/summary", "state": "success",
                     "creator": {"login": "github-actions[bot]"}}]
        writes = []

        def request(path, method="GET", data=None):
            if path == "pulls/7":
                return self.gh.pull
            if method == "GET" and "/check-runs?" in path:
                return {"check_runs": [row for row in checks
                                       if "check_name=" + g.quote(row["name"], safe="") + "&" in path]}
            if method == "GET" and "/statuses?" in path:
                return statuses if path.startswith(f"commits/{self.tested}/") else []
            writes.append((path, method, copy.deepcopy(data)))
            if method == "POST" and path == "check-runs":
                checks.append(github_check(data, len(checks) + 1))
            elif method == "PATCH" and path.startswith("check-runs/"):
                check_id = int(path.rsplit("/", 1)[1])
                next(row for row in checks if row["id"] == check_id).update(github_check(data, check_id))
            elif method == "POST" and path == f"statuses/{self.tested}":
                statuses.insert(0, {**data, "creator": {"login": "github-actions[bot]"}})
            else:
                raise AssertionError((path, method))
            return {}

        with patch.object(client, "request", side_effect=request), patch.dict(g.os.environ, {
            "GITHUB_SERVER_URL": "https://github.com", "GITHUB_REPOSITORY": g.REPOSITORY,
            "GITHUB_RUN_ID": "200",
        }):
            g.begin_checks(client, self.task)
            self.assertEqual(len(checks), 5)
            self.assertEqual(checks[0]["name"], "local-ci/basic")
            self.assertEqual(checks[0]["status"], "queued")
            self.assertEqual(g.check_workflow_id(checks[0]), "200")
            self.assertTrue(all(row["status"] == "completed" and row["conclusion"] == "cancelled"
                                for row in checks[1:4]))
            self.assertIsNone(checks[0]["conclusion"])
            self.assertEqual(checks[4]["conclusion"], "success")
            self.assertEqual([(path, data.get("name", data.get("context"))) for path, _, data in writes],
                             [("check-runs/1", "local-ci/basic"), ("check-runs/2", None),
                              ("check-runs/3", None), ("check-runs/4", None),
                              (f"statuses/{self.tested}", "local-ci/summary")])
            self.assertEqual(statuses[0]["state"], "error")
            self.assertTrue(client.owns_task(self.task, workflow=True))
            self.assertEqual(client.latest_dispatch(self.task), {"status": "not_started"})
            with patch.object(g, "GitHub", return_value=client), \
                    patch.object(g, "load_task", return_value=self.task), \
                    patch.object(g.sys, "argv", ["gateway.py", "info"]):
                self.assertEqual(g.main(), 0)  # PR47 previously failed here.
                before_repeat = copy.deepcopy(writes)
                self.assertEqual(g.main(), 0)
                self.assertEqual(writes, before_repeat)  # URL rewriting must not force a PATCH.
            before = copy.deepcopy(writes)
            with patch.dict(g.os.environ, {"GITHUB_RUN_ID": "100"}):
                self.assertTrue(client.owns_task(self.task))
                self.assertFalse(client.owns_task(self.task, workflow=True))
                self.assertFalse(g.sync_preflight(client, self.task, {"basic": "success"}))
                g.finalize_preflight(client, self.task, {"prepare": "success", "basic": "failure"})
                with self.assertRaisesRegex(ValueError, "freshness"):
                    g.enqueue(self.task, client, None, self.source)
            self.assertEqual(writes, before)
            self.assertTrue(g.sync_preflight(client, self.task, {"basic": "success"}))
            api = next(row for row in checks if row["name"] == "local-ci/api")
            self.assertEqual((api["id"], api["status"]), (2, "in_progress"))
            self.assertIsNone(checks[1]["conclusion"])
            self.assertEqual(client.latest_dispatch(self.task), {"status": "not_started"})
            g.finalize_preflight(client, self.task, {"prepare": "success", "basic": "success", "api": "failure"})
            self.assertTrue(all(row["status"] == "completed" for row in checks))
            self.assertEqual(api["conclusion"], "failure")
            self.assertEqual(g.check_workflow_id(api), "200")
            self.assertEqual([row["name"] for row in checks], list(g.ALL_CHECK_NAMES.values()))
            # Re-run failed jobs keeps its run ID; the receiver has a different one.
            with patch.dict(g.os.environ, {"GITHUB_RUN_ATTEMPT": "2"}):
                self.assertTrue(g.sync_preflight(client, self.task, {"api": "success"}))
                self.assertTrue(g.sync_preflight(client, self.task, {"security": "success"}))
                client.check(self.task, "approve", "completed", "success", "Approved", "Approved")
                control = self.store(g.CONTROL_BRANCH)
                g.enqueue(self.task, client, control, self.source)
            with patch.dict(g.os.environ, {"GITHUB_RUN_ID": "999"}):
                self.assertTrue(g.current_task(client, control, self.task))
                self.assertFalse(client.owns_task(self.task, workflow=True))

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
        dispatch = {"id": 10, "name": "local-ci/dispatch", "status": "completed", "conclusion": "success",
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

    def test_dispatch_ownership_precedes_and_retires_legacy_preflight(self):
        client = g.GitHub(g.REPOSITORY, token="fixture")
        dispatch = {"id": 10, "name": "local-ci/dispatch", "status": "queued",
                    "external_id": f"triton-anchor-local-ci:dispatch:{self.task['task_id']}",
                    "app": {"slug": "github-actions"}}
        legacy = {"id": 20, "name": "local-ci/preflight", "status": "in_progress",
                  "external_id": f"triton-anchor-local-ci:preflight:{self.task['task_id']}",
                  "app": {"slug": "github-actions"}}
        other = {**legacy, "id": 30, "external_id": "triton-anchor-local-ci:preflight:other-task"}
        rows = {"dispatch": [dispatch], "preflight": [legacy, other]}
        with patch.object(client, "check_runs", side_effect=lambda task, key: rows.get(key, [])), \
                patch.object(client, "request") as request:
            self.assertTrue(client.owns_task(self.task))
            self.assertFalse(client.owns_task({**self.task, "task_id": "other-task"}))
            client.retire_open_checks(self.task, superseded=True)
            self.assertEqual([call.args[0] for call in request.call_args_list], ["check-runs/20", "check-runs/30"])
            self.assertTrue(all(call.args[1] == "PATCH" and call.args[2]["conclusion"] == "cancelled"
                                for call in request.call_args_list))
            rows["dispatch"] = []
            self.assertFalse(client.owns_task(self.task))  # Legacy ownership only applies without dispatch.
            rows["preflight"] = [legacy]
            self.assertTrue(client.owns_task(self.task))

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
        newer = {"id": 12, "name": "local-ci/basic", "status": "queued",
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

    def test_finalizer_closes_rejected_approval_without_overwriting_new_owner(self):
        stages = {key: "success" for key in ("prepare", *g.CHECK_NAMES)}
        stages.update(approval="failure", enqueue="skipped")
        url = f"https://github.com/{g.REPOSITORY}/actions/runs/12345"
        for outcome, wording in (("failure", "approval rejected or verification failed"),
                                 ("cancelled", "approval cancelled")):
            with self.subTest(outcome=outcome), patch.dict(g.os.environ, {
                "GITHUB_RUN_ID": "12345", "GITHUB_REPOSITORY": g.REPOSITORY,
                "GITHUB_SERVER_URL": "https://github.com",
            }):
                g.finalize_preflight(self.gh, self.task, {**stages, "approval": outcome})
            final = self.gh.check_calls[-1]
            self.assertEqual(final[1:4], ("approve", "completed", outcome))
            self.assertIn(wording, final[4])
            self.assertIn(f"[Open approval controls and workflow evidence]({url})", final[5])
            self.assertEqual(final[6], url)
            self.assertEqual(self.gh.statuses, [])
        self.assertEqual(self.gh.comments, [])
        before = len(self.gh.statuses)
        with patch.object(self.gh, "owns_task", return_value=False):
            g.finalize_preflight(self.gh, self.task, stages)
        self.assertEqual(len(self.gh.statuses), before)
        g.finalize_preflight(self.gh, self.task, {**stages, "enqueue": "success"})
        self.assertEqual(len(self.gh.statuses), before)

    def test_internal_finalizer_never_reports_an_approval_failure(self):
        task = {**self.task, "external_fork": False}
        stages = {key: "success" for key in ("prepare", *g.CHECK_NAMES)}
        stages.update(card="failure", approval="skipped", enqueue="skipped")
        g.finalize_preflight(self.gh, task, stages)
        final = self.gh.check_calls[-1]
        self.assertEqual(final[1:4], ("dispatch", "completed", "failure"))
        self.assertEqual(final[4], "Local CI: task dispatch failed; see workflow")

    def test_finalizer_closes_only_current_pending_after_partial_dispatch_failure(self):
        control = self.store(g.CONTROL_BRANCH)
        stages = dict.fromkeys(("prepare", *g.CHECK_NAMES, "card", "approval"), "success")
        stages["enqueue"] = "failure"
        pending = {"state": "pending", "target_url": f"https://github.com/run#local-ci-task={self.task['task_id']}"}
        for failure in ("dispatch", "legacy", "receiver"):
            events = []

            def check(task, key, status, conclusion, *args):
                events.append((key, status, conclusion))
                if failure == "dispatch" and key == "dispatch" and conclusion == "success":
                    raise RuntimeError("Dispatch completion update failed")

            def reconcile(task, state, *args):
                events.append(("legacy", state))
                if failure == "legacy":
                    raise RuntimeError("Legacy synchronization failed")

            with self.subTest(failure=failure), patch.object(self.gh, "check", side_effect=check), \
                    patch.object(self.gh, "reconcile_legacy_statuses", side_effect=reconcile), \
                    patch.object(self.gh, "status", side_effect=lambda task, state, *args: events.append(("summary", state))):
                if failure == "receiver":
                    g.enqueue(self.task, self.gh, control, self.source)
                else:
                    with self.assertRaises(RuntimeError):
                        g.enqueue(self.task, self.gh, control, self.source)
            self.assertEqual(events[:3], [("dispatch", "in_progress", None),
                                          ("summary", "pending"), ("dispatch", "completed", "success")])
            if failure != "dispatch":
                self.assertEqual(events[3:], [("legacy", "pending")])
            dispatch = ({"status": "in_progress"} if failure == "dispatch" else
                        {"status": "completed", "conclusion": "success"})
            for label, previous in (
                ("current", pending), ("missing", None),
                ("foreign", {**pending, "target_url": "https://github.com/run#local-ci-task=another-task"}),
                ("unmarked", {**pending, "target_url": "https://github.com/run"}),
                ("completed", {**pending, "state": "success"}),
            ):
                with self.subTest(failure=failure, summary=label), \
                        patch.object(self.gh, "latest_dispatch", return_value=dispatch), \
                        patch.object(self.gh, "latest_summary", return_value=previous), \
                        patch.object(self.gh, "status") as status, \
                        patch.object(self.gh, "reconcile_legacy_statuses") as legacy:
                    g.finalize_preflight(self.gh, self.task, stages)
                    if label == "current":
                        status.assert_called_once()
                        self.assertEqual(status.call_args.args[:2], (self.task, "error"))
                        legacy.assert_called_once_with(*status.call_args.args)
                    else:
                        status.assert_not_called()
                        legacy.assert_not_called()

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
                return [{"context": "local-ci/summary", "state": "pending"}]
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

    def test_http_status_and_idempotent_comment_writeback(self):
        calls, comments, statuses = [], [], {}

        class Response:
            def __init__(self, payload):
                self.payload = json.dumps(payload).encode()

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return self.payload

        def transport(request, timeout):
            self.assertEqual(timeout, 30)
            method = request.get_method()
            path = request.full_url.split(f"/repos/{g.REPOSITORY}/", 1)[1]
            if path == "forbidden":
                raise g.HTTPError(request.full_url, 403, "permission denied", {}, None)
            if method == "GET":
                if path.startswith("commits/"):
                    sha = path.split("commits/", 1)[1].split("/", 1)[0]
                    return Response(statuses.get(sha, []))
                return Response(comments)
            data = json.loads(request.data)
            calls.append((method, path, data))
            if method == "POST" and path.endswith("/comments"):
                comments.append({"id": len(comments) + 11, "user": {"type": "Bot", "login": "github-actions[bot]"}, **data})
            elif method == "POST" and path.startswith("statuses/"):
                statuses.setdefault(path.rsplit("/", 1)[1], []).insert(0, data)
            elif method == "PATCH":
                comments[0].update(data)
            return Response({})

        with patch.object(g, "urlopen", side_effect=transport):
            client = g.GitHub(
                g.REPOSITORY, "http://127.0.0.1", token="fake-local-token"
            )
            client.status(self.task, "pending", "Queued")
            client.comment(self.task, "first report")
            client.comment(self.task, "first report")
            client.comment(self.task, "updated report")
            self.assertEqual(len(calls), 3)
            self.assertEqual(calls[-1][0], "POST")
            self.assertTrue(comments[0]["body"].startswith(g.MARKER))
            self.assertTrue(comments[0]["body"].endswith("first report"))
            client.comment(self.task, "first report")
            self.assertEqual(len(calls), 3)  # Retry after a later event is also idempotent.
            client.comment({**self.task, "task_id": "a" * 64}, "first report")
            self.assertEqual(len(comments), 3)  # Another task never reuses an old comment.
            self.assertFalse(any(call[0] == "PATCH" for call in calls))
            self.assertTrue(
                client.status_matches(self.task, "pending", "Queued"), statuses
            )
            self.assertFalse(client.status_matches(self.task, "success", "Queued"))
            self.assertFalse(
                client.status_matches(self.task, "pending", "Different result")
            )
            self.assertEqual(calls[0][1], f"statuses/{self.tested}")
            self.assertEqual(calls[0][2]["context"], "local-ci/summary")
            self.assertNotIn(self.head, statuses)
            statuses[self.tested].insert(
                0,
                {
                    "context": "local-ci/summary",
                    "state": "error",
                    "description": "Newer failure",
                },
            )
            self.assertFalse(client.status_matches(self.task, "pending", "Queued"))
            with self.assertRaisesRegex(
                g.GitHubAPIError, "HTTP 403: permission denied"
            ) as error:
                client.request("forbidden")
            self.assertNotIn("token", str(error.exception))

    def test_prepare_error_comment_does_not_require_a_frozen_task(self):
        gh = g.GitHub(g.REPOSITORY, token="fixture")
        context = {"head_sha": self.head, "tested_sha": self.head, "pr_number": 7}
        with patch.object(gh, "request", return_value=[]) as request:
            self.assertTrue(gh.comment(context, "CI 准备未完成"))
        self.assertEqual(request.call_args.args[:2], ("issues/7/comments", "POST"))


class WorkflowStructureTests(unittest.TestCase):
    def test_workflow_gates_and_task_driven_receiver(self):
        import yaml

        worker_text = (ROOT / ".github/workflows/ci-gateway.yml").read_text()
        data = yaml.load(worker_text, Loader=yaml.BaseLoader)
        # A plain YAML scalar treats the space before '#' in 'PR #{0}' as
        # a comment, silently cutting the Actions expression before its close.
        self.assertIn("format('PR #{0}', inputs.pr_number)", data["run-name"])
        self.assertIn("format('PR #{0}', github.event.pull_request.number)", data["run-name"])
        self.assertEqual(data["name"], "CI Gateway")
        self.assertIn("inputs.run_title", data["run-name"])
        self.assertIn("format(' {0}', inputs.receiver_round)", data["run-name"])
        self.assertEqual(data["run-name"].count("${{"), data["run-name"].count("}}"))
        jobs = data["jobs"]
        cancel = next(step for step in jobs["cancel-obsolete"]["steps"] if step.get("run", "").endswith("gateway.py cancel"))
        self.assertEqual(cancel["env"]["SOURCE_BRANCH"], "${{ inputs.source_branch || github.ref_name }}")
        collect = next(step for step in jobs["publish"]["steps"] if step.get("id") == "collect")
        self.assertEqual(collect["env"]["RECEIVER_TASK_ID"], "${{ inputs.task_id }}")
        self.assertEqual(jobs["basic"]["needs"], "prepare")
        self.assertIn("basic", jobs["api"]["needs"])
        self.assertIn("api", jobs["security"]["needs"])
        self.assertNotIn("review-card", jobs)
        self.assertIn("security-result", jobs["approve-external-fork"]["needs"])
        self.assertIn("needs.security-result.outputs.eligible", jobs["approve-external-fork"]["if"])
        self.assertIn("approve-external-fork", jobs["enqueue"]["needs"])
        for name in (
            "local-ci-basic-checks.yml",
            "local-ci-api-compatibility.yml",
            "local-ci-security.yml",
            "local-ci-preflight-result.yml",
        ):
            workflow = yaml.load(
                (ROOT / ".github/workflows" / name).read_text(), Loader=yaml.BaseLoader
            )
            self.assertEqual(set(workflow["on"]), {"workflow_call"})
        for stage in ("basic", "api", "security"):
            completion = jobs[stage + "-result"]
            expected_needs = ["prepare", stage]
            if stage == "security":
                expected_needs.extend(["basic-result", "api-result"])
            self.assertEqual(completion["needs"], expected_needs)
            self.assertIn("always()", completion["if"])
            self.assertIn("needs.prepare.outputs.task_id", completion["if"])
            self.assertEqual(completion["with"]["stage"], stage)
            self.assertEqual(completion["with"]["outcome"], "${{ needs." + stage + ".result }}")
            self.assertEqual(completion["permissions"]["checks"], "write")
            self.assertNotIn("checks", jobs[stage]["permissions"])
        self.assertEqual(jobs["security-result"]["with"]["external_fork"], "${{ needs.prepare.outputs.external_fork == 'true' }}")
        self.assertIn("needs.basic-result.result", jobs["security-result"]["with"]["review_card"])
        self.assertIn("needs.api-result.result", jobs["security-result"]["with"]["review_card"])
        for publisher in ("basic-result", "api-result", "security-result"):
            self.assertIn(publisher, jobs["enqueue"]["needs"])
            self.assertIn(f"needs.{publisher}.result == 'success'", jobs["enqueue"]["if"])
        sync = yaml.load((ROOT / ".github/workflows/local-ci-preflight-result.yml").read_text(), Loader=yaml.BaseLoader)
        steps = sync["jobs"]["sync"]["steps"]
        self.assertEqual(steps[0]["with"]["ref"], "${{ inputs.worker_revision_sha }}")
        self.assertEqual(steps[0]["with"]["persist-credentials"], "false")
        checks_step = next(step for step in steps if step.get("run") == "python3 scripts/ci/gateway.py checks")
        self.assertEqual(checks_step["env"]["EXPECTED_TASK_DIGEST"], "${{ inputs.task_digest }}")
        self.assertEqual(steps[-1]["id"], "eligibility")
        self.assertEqual(sync["permissions"]["statuses"], "read")
        self.assertEqual(sync["permissions"]["pull-requests"], "write")
        self.assertIn("external_fork", sync["on"]["workflow_call"]["inputs"])
        self.assertIn("review_card", sync["on"]["workflow_call"]["inputs"])
        self.assertIn("eligible", sync["on"]["workflow_call"]["outputs"])
        self.assertTrue(jobs["deploy-dashboard"]["name"].isascii())
        self.assertEqual(jobs["prepare"]["permissions"]["checks"], "write")
        self.assertEqual(jobs["approve-external-fork"]["permissions"]["checks"], "write")
        self.assertEqual(jobs["receive"]["permissions"]["checks"], "read")
        self.assertIn("always()", jobs["finalize-preflight"]["if"])
        self.assertIn("approve-external-fork", jobs["finalize-preflight"]["needs"])
        self.assertEqual(jobs["finalize-preflight"]["steps"][-1]["run"], "python3 scripts/ci/gateway.py finalize")
        self.assertNotIn("schedule", data["on"])
        self.assertIn("worker_revision_sha", data["on"]["workflow_dispatch"]["inputs"])
        self.assertFalse((ROOT / ".github/workflows/ci-receiver.yml").exists())
        self.assertIn("receive", data["on"]["workflow_dispatch"]["inputs"]["mode"]["options"])
        self.assertIn("inputs.mode == 'receive'", jobs["receive"]["if"])
        self.assertGreater(
            int(jobs["receive"]["timeout-minutes"]) * 60, g.RECEIVER_WAIT_SECONDS
        )
        self.assertNotIn("environment", jobs["receive"])
        self.assertEqual(jobs["publish"]["needs"], "receive")
        self.assertIn("needs.receive.outputs.state == 'ready'", jobs["publish"]["if"])
        self.assertNotIn("environment", jobs["publish"])
        self.assertNotIn("id-token", jobs["publish"]["permissions"])
        self.assertEqual(jobs["deploy-dashboard"]["environment"], {
            "name": "github-pages", "url": "${{ steps.deployment.outputs.page_url }}"
        })
        self.assertEqual(jobs["deploy-dashboard"]["needs"], "publish")
        self.assertIn("dashboard_changed", jobs["deploy-dashboard"]["if"])
        self.assertEqual(jobs["finalize-preflight"]["permissions"]["pull-requests"], "read")
        self.assertEqual(jobs["publish"]["concurrency"]["cancel-in-progress"], "false")
        self.assertIn("inputs.task_id", data["concurrency"]["group"])
        self.assertIn("inputs.mode != 'receive'", data["concurrency"]["cancel-in-progress"])
        enqueue_steps = jobs["enqueue"]["steps"]
        dispatch = enqueue_steps[-1]
        self.assertTrue(enqueue_steps[-2]["run"].endswith("gateway.py enqueue"))
        self.assertNotIn("if", dispatch)
        self.assertIn("mode: 'receive'", dispatch["with"]["script"])
        self.assertEqual(dispatch["env"]["TASK_ID"], "${{ needs.prepare.outputs.task_id }}")
        self.assertIn("run_title: runTitle", dispatch["with"]["script"])
        retry = jobs["publish"]["steps"][-1]
        self.assertEqual(retry["env"]["RUN_TITLE"], "${{ inputs.run_title }}")
        self.assertIn("run_title: process.env.RUN_TITLE", retry["with"]["script"])
        for step in jobs["publish"]["steps"]:
            if "pages@" in step.get("uses", "") or "pages-artifact@" in step.get("uses", ""):
                self.assertEqual(step["if"], "steps.dashboard.outputs.changed == 'true'")
        self.assertEqual(
            set(g.REQUIRED_CONTEXTS),
            {"local-ci/basic", "local-ci/api", "local-ci/security", "local-ci/dispatch", "local-ci/summary"},
        )


if __name__ == "__main__":
    unittest.main()
