from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
GATEWAY = ROOT / ".github/workflows/ci-gateway.yml"
MANIFEST = ROOT / ".github/ci-gateway-manifest.json"

CONTRACT_INPUTS = {
    "gateway_contract_version",
    "mode",
    "pr_number",
    "expected_head_sha",
    "comparison_base_sha",
    "tested_sha",
    "requested_sha",
    "worker_revision_sha",
    "authorization_source",
    "source_branch",
    "target_branch",
    "task_ref",
    "context",
    "attempt",
    "started_at",
    "wait_seconds",
    "cancellation_reason",
    "delete_task_ref",
    "run_title",
}


def workflow_dispatch_inputs(text: str) -> set[str]:
    lines = text.splitlines()
    start = lines.index("  workflow_dispatch:")
    inputs_line = lines.index("    inputs:", start)
    names: set[str] = set()
    for line in lines[inputs_line + 1 :]:
        if line and not line.startswith("      "):
            break
        if line.startswith("      ") and not line.startswith("        "):
            names.add(line.strip().removesuffix(":"))
    return names


def workflow_step_script(text: str, name: str) -> str:
    marker = f"      - name: {name}\n"
    start = text.index(marker)
    body_start = text.index("        run: |\n", start) + len("        run: |\n")
    body_end = text.index("\n      - name:", body_start)
    return "\n".join(
        line[10:] if line.startswith("          ") else line
        for line in text[body_start:body_end].splitlines()
    )


def commit(repo: Path, message: str) -> str:
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", message], cwd=repo, check=True)
    return subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=repo, text=True
    ).strip()


class GatewayV3ContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.gateway = GATEWAY.read_text(encoding="utf-8")
        cls.manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        cls.security = (ROOT / ".github/workflows/security-gate.yml").read_text()
        cls.dispatcher = (ROOT / ".github/workflows/dispatch-local-ci.yml").read_text()
        cls.receiver = (ROOT / ".github/workflows/receive-local-ci-result.yml").read_text()
        cls.pages = (ROOT / ".github/workflows/backend-status-pages.yml").read_text()
        cls.poller = (ROOT / "scripts/local_ci/poll_gitee_and_run.sh").read_text()

    def test_contract_v3_interface(self) -> None:
        self.assertEqual(workflow_dispatch_inputs(self.gateway), CONTRACT_INPUTS)
        self.assertIn('GATEWAY_CONTRACT_VERSION: "3"', self.gateway)
        self.assertNotIn("expected_base_sha", self.gateway)
        self.assertNotIn("inputs.sha", self.gateway)

    def test_manifest_describes_merge_result_worker(self) -> None:
        self.assertEqual(self.manifest["schema_version"], 1)
        self.assertEqual(self.manifest["gateway_contract_version"], "3")
        self.assertEqual(self.manifest["role"], "worker")
        self.assertEqual(self.manifest["tested_revision"], "merge-result")
        self.assertTrue(
            {
                "security-scan", "codeql", "dispatch", "receive", "pages",
                "cancel", "cross-branch-pr", "cross-branch-push", "merge-result",
            }.issubset(self.manifest["capabilities"])
        )

    def test_merge_result_is_frozen_and_revalidated(self) -> None:
        self.assertGreaterEqual(self.gateway.count("pull/${prNumber}/merge"), 3)
        self.assertIn("parents[0]", self.gateway)
        self.assertIn("parents[1]", self.gateway)
        self.assertIn("tested_sha: process.env.TESTED_SHA", self.gateway)
        self.assertIn("TESTED_SHA_KIND=\"pr_merge\"", self.dispatcher)
        self.assertIn("refs/pull/${PR_NUMBER}/merge", self.dispatcher)

    def test_dispatch_metadata_v2_records_text_truncation(self) -> None:
        self.assertIn(
            "title_truncated:(($title|length)>500)", self.dispatcher
        )
        self.assertIn(
            "description_truncated:(($description|length)>8000)",
            self.dispatcher,
        )

    def test_dispatch_classifies_only_safe_documentation_changes_as_codex_only(
        self,
    ) -> None:
        script = workflow_step_script(self.dispatcher, "Classify PR execution mode")
        cases = (
            ("docs/guide.md", False, "codex_only"),
            ("README.md", False, "codex_only"),
            ("README", False, "full"),
            ("LICENSE", False, "full"),
            ("NOTICE", False, "full"),
            ("python/triton_anchor/example.py", False, "full"),
            (".github/workflows/ci.yml", False, "full"),
            (
                "scripts/local_ci/codex_ai/prompts/codex_ai_success.md",
                False,
                "full",
            ),
            ("docs/example.md", True, "full"),
        )
        for path, rename_code, expected in cases:
            with self.subTest(path=path, rename_code=rename_code):
                with tempfile.TemporaryDirectory() as directory:
                    repo = Path(directory)
                    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
                    subprocess.run(
                        ["git", "config", "user.name", "local-ci-test"],
                        cwd=repo,
                        check=True,
                    )
                    subprocess.run(
                        [
                            "git",
                            "config",
                            "user.email",
                            "local-ci-test@example.invalid",
                        ],
                        cwd=repo,
                        check=True,
                    )
                    (repo / "README.md").write_text("base\n", encoding="utf-8")
                    source = repo / "python/triton_anchor/example.py"
                    source.parent.mkdir(parents=True)
                    source.write_text("VALUE = 1\n", encoding="utf-8")
                    base_sha = commit(repo, "base")

                    destination = repo / path
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    if rename_code:
                        source.rename(destination)
                    else:
                        destination.write_text("changed\n", encoding="utf-8")
                    head_sha = commit(repo, "candidate")

                    output = repo / "github-output"
                    env = os.environ.copy()
                    env.update(
                        {
                            "BASE_SHA": base_sha,
                            "HEAD_SHA": head_sha,
                            "GITHUB_OUTPUT": str(output),
                        }
                    )
                    subprocess.run(
                        ["bash", "-c", script], cwd=repo, env=env, check=True
                    )
                    self.assertEqual(
                        output.read_text(encoding="utf-8").strip(),
                        f"execution_mode={expected}",
                    )

    def test_codex_only_mode_skips_deterministic_ci_but_runs_codex_review(self) -> None:
        self.assertIn('execution_mode="full"', self.poller)
        self.assertIn(
            "Skipping deterministic Local CI for documentation-only PR",
            self.poller,
        )
        self.assertIn(
            '"${execution_mode}" == "codex_only"',
            self.poller,
        )
        self.assertIn("run_codex_ai_ci_for_run", self.poller)

    def test_receiver_accepts_explicitly_skipped_stage_results(self) -> None:
        self.assertIn(
            'case "${status}" in pass|success|warning|skipped) exit 0 ;; esac',
            self.receiver,
        )

    def test_external_fork_requires_live_maintainer_authorization(self) -> None:
        self.assertIn("getCollaboratorPermissionLevel", self.gateway)
        self.assertIn("write', 'maintain', 'admin", self.gateway)
        self.assertIn("approve-external-fork:", self.gateway)
        self.assertIn("local-ci-fork-approval", self.gateway)
        self.assertIn("external-fork-environment", self.gateway)
        self.assertIn("manual-maintainer:", self.gateway)
        self.assertIn("pull.head.sha !== process.env.EXPECTED_HEAD_SHA", self.gateway)

    def test_merge_result_base_comes_from_first_parent(self) -> None:
        self.assertIn("const comparisonBaseSha = parents[0]", self.gateway)
        self.assertIn("core.setOutput('base_sha', comparisonBaseSha)", self.gateway)
        self.assertIn(
            "h:${pull.head.sha.slice(0, 7)} m:${testedSha.slice(0, 7)} | dispatch",
            self.gateway,
        )
        self.assertNotIn(
            "parents[0].toLowerCase() !== pull.base.sha.toLowerCase()",
            self.gateway,
        )

    def test_security_gate_is_reusable_and_blocks_dispatch(self) -> None:
        self.assertIn("workflow_call:", self.security)
        self.assertNotIn("pull_request_target:", self.security)
        self.assertIn("trusted_ref:", self.security)
        self.assertIn("CodeQL", self.security)
        self.assertNotIn("authorize-local-ci", self.security)
        security_at = self.gateway.index("\n  security-gate:")
        dispatch_at = self.gateway.index("\n  dispatch:", security_at)
        self.assertLess(security_at, dispatch_at)
        self.assertIn("- security-gate", self.gateway[dispatch_at:])
        self.assertNotIn("workflow_run:", self.dispatcher)

    def test_fallback_is_only_for_missing_manifest(self) -> None:
        self.assertIn("let worker = await inspectWorker(pull.base.ref, true)", self.gateway)
        self.assertIn("if (worker === null)", self.gateway)
        self.assertIn("invalid JSON", self.gateway)
        self.assertIn("incompatible manifest", self.gateway)

    def test_fallback_switches_default_to_enabled(self) -> None:
        self.assertIn(
            "FALLBACK_PR_ENABLED: ${{ vars.LOCAL_CI_FALLBACK_PR_ENABLED || 'true' }}",
            self.gateway,
        )
        self.assertIn(
            "FALLBACK_PUSH_ENABLED: ${{ vars.LOCAL_CI_FALLBACK_PUSH_ENABLED || 'true' }}",
            self.gateway,
        )
        self.assertIn("PR fallback is disabled", self.gateway)
        self.assertIn("Cross-branch push fallback is disabled", self.gateway)

    def test_manual_push_and_receiver_use_explicit_sha_fields(self) -> None:
        self.assertIn("REQUESTED_SHA: ${{ inputs.requested_sha }}", self.gateway)
        self.assertIn("TESTED_SHA: ${{ inputs.tested_sha }}", self.gateway)
        pr_match_at = self.gateway.index("if (prMatch) {")
        distinct_sha_at = self.gateway.index(
            "PR receiver must distinguish head SHA from merge-result SHA"
        )
        self.assertLess(pr_match_at, distinct_sha_at)
        self.assertIn("mode=receive", self.dispatcher)
        self.assertIn("--status-sha", self.receiver)
        self.assertIn("--comparison-base-sha", self.receiver)

    def test_direct_push_dispatch_title_omits_full_sha(self) -> None:
        run_name = self.dispatcher.splitlines()[1]
        self.assertIn("format('Push {0} | dispatch'", run_name)
        self.assertNotIn("inputs.commit_sha || github.sha", run_name)

    def test_required_statuses_target_the_tested_revision(self) -> None:
        self.assertIn("`${process.env.STATUS_CONTEXT}/routing`", self.gateway)
        self.assertIn("sha: process.env.TESTED_SHA", self.gateway)
        self.assertNotIn(
            "sha: process.env.EXPECTED_HEAD_SHA || process.env.TESTED_SHA",
            self.gateway,
        )
        self.assertGreaterEqual(
            self.dispatcher.count(
                "STATUS_SHA: ${{ steps.meta.outputs.tested_sha }}"
            ),
            2,
        )
        self.assertNotIn(
            "STATUS_SHA: ${{ steps.meta.outputs.head_sha }}", self.dispatcher
        )
        self.assertIn('--status-sha "${TESTED_SHA}"', self.receiver)
        self.assertNotIn("EXPECTED_HEAD_SHA:-${TESTED_SHA}", self.receiver)

    def test_pages_are_branch_isolated(self) -> None:
        guard = "github.ref_name == (vars.LOCAL_CI_PAGES_BRANCH || 'CI_dev')"
        self.assertIn(guard, self.pages)
        self.assertIn("Cross-branch result only updates commit status", self.receiver)

    def test_pages_use_gitee_username_for_authentication(self) -> None:
        self.assertIn(
            "GITEE_USERNAME: ${{ vars.GITEE_USERNAME || 'likehupochuan' }}",
            self.pages,
        )
        self.assertIn(
            '*Username*) printf \'%s\\n\' "${GITEE_USERNAME}"', self.pages
        )
        self.assertNotIn(
            '*Username*) printf \'%s\\n\' "${GITEE_RESULTS_OWNER}"', self.pages
        )

    def test_cancellation_removes_every_pr_ref(self) -> None:
        for prefix in ("ci/base/", "ci/head/", "ci/meta/"):
            self.assertIn(prefix, self.gateway)

    def test_api_compatible_resolves_old_comment(self) -> None:
        notify = (ROOT / ".github/workflows/api-breaking-notify.yml").read_text()
        self.assertIn("Resolved: the latest public API compatibility result is compatible.", notify)


if __name__ == "__main__":
    unittest.main()
