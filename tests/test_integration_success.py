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


class IntegrationSuccessTests(unittest.TestCase):
    def test_adopted_local_cherry_pick_is_validated_pushed_and_closed(self):
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

            # Approved issue branch has its own commit SHA.
            run(repo, "git", "checkout", "-b", "agent/issue-27-deep-link", base)
            (repo / "docs.md").write_text("issue\n", encoding="utf-8")
            run(repo, "git", "add", "docs.md")
            run(repo, "git", "commit", "-m", "fix(issue-27): deep link")
            issue_commit = run(repo, "git", "rev-parse", "HEAD").stdout.strip()
            run(repo, "git", "push", "origin", "agent/issue-27-deep-link")

            # Recreate the operator's current situation: same change cherry-picked
            # locally onto the base, but not pushed yet, producing a different SHA.
            run(repo, "git", "checkout", "integration/test")
            (repo / "docs.md").write_text("issue\n", encoding="utf-8")
            run(repo, "git", "add", "docs.md")
            run(repo, "git", "commit", "--author", "Operator <operator@example.com>", "-m", "fix(issue-27): deep link")
            local_candidate = run(repo, "git", "rev-parse", "HEAD").stdout.strip()
            self.assertNotEqual(local_candidate, issue_commit)
            self.assertEqual(remote_branch_sha(str(repo), "integration/test"), base)

            cfg = {
                "repo_path": str(repo),
                "repo_slug": "x/y",
                "base_branch": "integration/test",
                "worktree_root": str(root / "worktrees"),
                "runtime_dir": str(root / "runtime"),
                "process_env": {"PLAYWRIGHT_HTML_OPEN": "never"},
                "integration": {
                    "comment_updates": False,
                    "manual_close_after_success": True,
                    "cleanup_generated": True,
                    "generated_artifacts": [".coverage"],
                    "sync_clean_local_base_after_push": True,
                },
                "protected_paths": [".env", "backend/data/"],
                "workspace_bootstrap": [],
                "validation": {"targeted": [], "fast": [], "full": []},
            }
            manager = IntegrationManager(root, cfg)
            open_issue = Issue(27, "Deep link", "", "OPEN", "https://example/27", ["agent:done", "risk:low"])
            closed_issue = Issue(27, "Deep link", "", "CLOSED", "https://example/27", ["agent:done", "risk:low"])
            evidence = CompletionEvidence(
                branch="agent/issue-27-deep-link",
                commit=issue_commit,
                base=base,
                reviewer_pass=True,
                targeted_fast_pass=True,
                full_pass=True,
                body="ok",
            )

            # One fetch at eligibility, then live fetch before closure and final verification.
            with patch("abi_autopilot.integration.ghx.get_issue", side_effect=[open_issue, open_issue, closed_issue]), \
                 patch("abi_autopilot.integration.ghx.get_completion_evidence", return_value=evidence), \
                 patch("abi_autopilot.integration.ghx.close_issue") as close_issue:
                result = manager.integrate_issue(27)

            self.assertEqual(result.status, "integrated")
            self.assertTrue(result.closed)
            self.assertEqual(result.integration_commit, local_candidate)
            self.assertEqual(remote_branch_sha(str(repo), "integration/test"), local_candidate)
            close_issue.assert_called_once_with("x/y", 27)


if __name__ == "__main__":
    unittest.main()
