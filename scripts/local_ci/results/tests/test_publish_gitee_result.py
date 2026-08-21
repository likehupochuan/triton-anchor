from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT / "scripts" / "local_ci" / "results"))

import publish_gitee_result as publisher
import bridge_gitee_to_github_status as bridge


class PublishedArtifactTests(unittest.TestCase):
    def test_codex_only_fallback_is_accepted_as_explicitly_skipped_ci(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run_dir = root / "run"
            target_dir = root / "published"
            run_dir.mkdir()
            sha = "a" * 40
            head_sha = "b" * 40
            (run_dir / "local-ci.log").write_text(
                "Skipping deterministic Local CI for documentation-only PR.\n",
                encoding="utf-8",
            )
            (run_dir / "result.json").write_text(
                json.dumps(
                    {
                        "target_sha": sha,
                        "execution_mode": "codex_only",
                        "codex_ai_ci_status": "pass",
                        "codex_ai_ci_mode": "docs_only",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            args = SimpleNamespace(
                exit_code=0,
                source_branch="ci/pr-42/feature/demo",
                sha=sha,
                run_id="run-1",
                context="local-ci/test",
            )
            rel_dir = (
                Path("runs/ci_pr/ci_pr-42_feature_demo")
                / f"h-{head_sha[:12]}_m-{sha[:12]}"
                / args.run_id
            )

            publisher.write_fallback_results(run_dir, target_dir, args, rel_dir)

            summary = (target_dir / "delivery-summary.txt").read_text(
                encoding="utf-8"
            )
            self.assertIn("execution_mode: codex_only\n", summary)
            for _, summary_key, _, _ in bridge.REPORTABLE_STAGES:
                self.assertIn(f"{summary_key}: skipped\n", summary)

            target = bridge.Target(
                "feature/demo",
                args.source_branch,
                sha,
                "PR #42 feature/demo",
                head_sha=head_sha,
            )

            def content(
                owner: str,
                repo: str,
                path: str,
                ref: str,
                token: str,
            ) -> str | None:
                del owner, repo, ref, token
                if path.endswith("/latest.txt"):
                    return args.run_id + "\n"
                published = target_dir / Path(path).name
                return (
                    published.read_text(encoding="utf-8")
                    if published.is_file()
                    else None
                )

            bridge_args = SimpleNamespace(
                gitee_owner="owner",
                gitee_repo="results",
                gitee_results_branch="local-ci-results",
                gitee_web_url="https://gitee.example/results",
            )
            with mock.patch.object(bridge, "gitee_content", side_effect=content):
                result = bridge.read_local_ci_result(bridge_args, target, "token")

            self.assertIsNotNone(result)
            assert result is not None
            self.assertEqual(result.exit_code, 0)

            push_target = bridge.Target(
                "feature/demo",
                "ci/push/feature/demo",
                sha,
                "feature/demo",
            )
            with mock.patch.object(bridge, "gitee_content", side_effect=content):
                push_result = bridge.read_local_ci_result(
                    bridge_args, push_target, "token"
                )
            self.assertIsNotNone(push_result)
            assert push_result is not None
            self.assertEqual(push_result.exit_code, 1)

    def test_candidate_result_does_not_duplicate_base_performance_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run_dir = root / "run"
            artifact_dir = root / "artifacts"
            target_dir = root / "published"
            run_dir.mkdir()
            artifact_dir.mkdir()
            (run_dir / "local-ci.log").write_text(
                f"Artifact dir: {artifact_dir}\n", encoding="utf-8"
            )
            (artifact_dir / "delivery-summary.txt").write_text(
                "schema: triton-anchor-local-ci/v2\nstatus: 0\n",
                encoding="utf-8",
            )
            (artifact_dir / "compile-benchmark.json").write_text("{}\n")
            for name in (
                "compile-benchmark-base.json",
                "pass-profile-base.json",
                "ir-serialization-base.json",
            ):
                (artifact_dir / name).write_text("{}\n")

            args = SimpleNamespace(
                exit_code=0,
                source_branch="ci/pr-42/feature/demo",
                sha="a" * 40,
                run_id="run-1",
                context="local-ci/test",
            )
            published = publisher.copy_results(
                run_dir,
                target_dir,
                args,
                Path("runs/ci_pr/ci_pr-42_feature_demo") / args.sha / args.run_id,
            )

            self.assertEqual(published, target_dir)
            self.assertTrue((target_dir / "compile-benchmark.json").is_file())
            for name in (
                "compile-benchmark-base.json",
                "pass-profile-base.json",
                "ir-serialization-base.json",
            ):
                self.assertFalse((target_dir / name).exists())
            manifest = json.loads(
                (target_dir / "publish-manifest.json").read_text(encoding="utf-8")
            )
            self.assertFalse(
                any(name.endswith("-base.json") for name in manifest["copied_files"])
            )


if __name__ == "__main__":
    unittest.main()
