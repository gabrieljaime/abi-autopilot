import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from abi_autopilot.github import CompletionEvidence, Issue
from abi_autopilot.integration import IntegrationManager
from abi_autopilot.gitops import remote_branch_sha


def run(cwd: Path, *args):
    return subprocess.run(list(args), cwd=cwd, check=True, capture_output=True, text=True)


class BatchIntegrationTests(unittest.TestCase):
    def test_batch_pushes_once_after_all_issues_and_closes_after_final_gate(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            bare = root / "origin.git"
            repo = root / "repo"
            run(root, "git", "init", "--bare", str(bare))
            run(root, "git", "init", str(repo))
            run(repo, "git", "config", "user.email", "test@example.com")
            run(repo, "git", "config", "user.name", "Test")
            run(repo, "git", "checkout", "-b", "integration/test")
            (repo / "README.md").write_text("base\n", encoding="utf-8")
            run(repo, "git", "add", "README.md")
            run(repo, "git", "commit", "-m", "base")
            base = run(repo, "git", "rev-parse", "HEAD").stdout.strip()
            run(repo, "git", "remote", "add", "origin", str(bare))
            run(repo, "git", "push", "-u", "origin", "integration/test")

            evidence = {}
            for number, filename in ((28, "a.md"), (29, "b.md")):
                branch = f"agent/issue-{number}-x"
                run(repo, "git", "checkout", "-B", branch, base)
                (repo / filename).write_text(f"issue {number}\n", encoding="utf-8")
                run(repo, "git", "add", filename)
                run(repo, "git", "commit", "-m", f"fix(issue-{number}): docs")
                sha = run(repo, "git", "rev-parse", "HEAD").stdout.strip()
                run(repo, "git", "push", "-u", "origin", branch)
                evidence[number] = CompletionEvidence(
                    branch=branch,
                    commit=sha,
                    base=base,
                    reviewer_pass=True,
                    targeted_fast_pass=True,
                    full_pass=True,
                    body="ok",
                )
            run(repo, "git", "checkout", "integration/test")

            cfg = {
                "repo_path": str(repo),
                "repo_slug": "x/y",
                "base_branch": "integration/test",
                "worktree_root": str(root / "worktrees"),
                "runtime_dir": str(root / "runtime"),
                "process_env": {"PLAYWRIGHT_HTML_OPEN": "never"},
                "batch_validation": {"enabled": True, "full_suite_once_at_end": True},
                "validation_workers": {"vitest": 1, "pytest": 1, "playwright": 1},
                "integration": {
                    "comment_updates": False,
                    "manual_close_after_success": True,
                    "cleanup_generated": True,
                    "generated_artifacts": [".coverage"],
                    "sync_clean_local_base_after_push": True,
                    "validate_targeted": True,
                },
                "baseline": {"enabled": False},
                "protected_paths": [".env", "backend/data/"],
                "workspace_bootstrap": [],
                "validation": {
                    "targeted": [], "fast": [], "full": [],
                    "batch_fast": [], "batch_full": [],
                },
            }
            manager = IntegrationManager(root, cfg)
            issues = [
                Issue(28, "A", "", "OPEN", "https://example/28", ["agent:done", "risk:medium"]),
                Issue(29, "B", "", "OPEN", "https://example/29", ["agent:done", "risk:medium"]),
            ]
            counts = {28: 0, 29: 0}

            def get_issue(_slug, number):
                counts[number] += 1
                state = "CLOSED" if counts[number] >= 3 else "OPEN"
                return Issue(number, str(number), "", state, f"https://example/{number}", ["agent:done", "risk:medium"])

            with patch("abi_autopilot.integration.ghx.get_issue", side_effect=get_issue), \
                 patch("abi_autopilot.integration.ghx.get_completion_evidence", side_effect=lambda _slug, n: evidence[n]), \
                 patch("abi_autopilot.integration.ghx.close_issue") as close_issue:
                results = manager._integrate_batch(issues, close=True)

            self.assertEqual([r["status"] for r in results], ["integrated", "integrated"])
            self.assertTrue(all(r["closed"] for r in results))
            final = remote_branch_sha(str(repo), "integration/test")
            self.assertEqual(results[-1]["batch_head"], final)
            log = run(repo, "git", "log", "origin/integration/test", "--format=%s", "-3").stdout
            self.assertIn("fix(issue-28): docs", log)
            self.assertIn("fix(issue-29): docs", log)
            self.assertEqual(close_issue.call_count, 2)


if __name__ == "__main__":
    unittest.main()
