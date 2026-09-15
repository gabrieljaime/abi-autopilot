import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from abi_autopilot.github import CompletionEvidence, Issue
from abi_autopilot.integration import IntegrationManager
from abi_autopilot.gitops import remote_branch_sha, cherry_pick_in_progress


def run(cwd: Path, *args):
    return subprocess.run(list(args), cwd=cwd, check=True, capture_output=True, text=True)


class IntegrationConflictTests(unittest.TestCase):
    def test_conflict_is_preserved_without_abort_or_remote_move(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            bare = root / "origin.git"
            repo = root / "repo"
            run(root, "git", "init", "--bare", str(bare))
            run(root, "git", "init", str(repo))
            run(repo, "git", "config", "user.email", "test@example.com")
            run(repo, "git", "config", "user.name", "Test")
            run(repo, "git", "checkout", "-b", "integration/test")
            (repo / "same.txt").write_text("base\n", encoding="utf-8")
            run(repo, "git", "add", "same.txt")
            run(repo, "git", "commit", "-m", "base")
            base = run(repo, "git", "rev-parse", "HEAD").stdout.strip()
            run(repo, "git", "remote", "add", "origin", str(bare))
            run(repo, "git", "push", "-u", "origin", "integration/test")

            # Issue branch from the old base.
            run(repo, "git", "checkout", "-b", "agent/issue-77-conflict", base)
            (repo / "same.txt").write_text("issue\n", encoding="utf-8")
            run(repo, "git", "add", "same.txt")
            run(repo, "git", "commit", "-m", "fix(issue-77): conflict")
            issue_commit = run(repo, "git", "rev-parse", "HEAD").stdout.strip()
            run(repo, "git", "push", "origin", "agent/issue-77-conflict")

            # Remote base moves with a conflicting edit.
            run(repo, "git", "checkout", "integration/test")
            (repo / "same.txt").write_text("base moved\n", encoding="utf-8")
            run(repo, "git", "add", "same.txt")
            run(repo, "git", "commit", "-m", "base moved")
            run(repo, "git", "push", "origin", "integration/test")
            remote_before = remote_branch_sha(str(repo), "integration/test")

            cfg = {
                "repo_path": str(repo), "repo_slug": "x/y",
                "base_branch": "integration/test",
                "worktree_root": str(root / "worktrees"),
                "runtime_dir": str(root / "runtime"),
                "process_env": {"PLAYWRIGHT_HTML_OPEN": "never"},
                "integration": {"comment_updates": False},
                "protected_paths": [".env", "backend/data/"],
                "workspace_bootstrap": [],
                "validation": {"targeted": [], "fast": [], "full": []},
            }
            manager = IntegrationManager(root, cfg)
            issue = Issue(77, "Conflict", "", "OPEN", "https://example/77", ["agent:done", "risk:medium"])
            evidence = CompletionEvidence(
                branch="agent/issue-77-conflict", commit=issue_commit, base=base,
                reviewer_pass=True, targeted_fast_pass=True, full_pass=True, body="ok",
            )
            with patch("abi_autopilot.integration.ghx.get_issue", return_value=issue), \
                 patch("abi_autopilot.integration.ghx.get_completion_evidence", return_value=evidence):
                result = manager.integrate_issue(77)

            self.assertEqual(result.status, "conflict")
            self.assertEqual(remote_branch_sha(str(repo), "integration/test"), remote_before)
            wt = Path(result.worktree)
            self.assertTrue(wt.exists())
            self.assertTrue(cherry_pick_in_progress(wt))
            state = manager._load_state(77)
            self.assertEqual(state["stage"], "conflict")


if __name__ == "__main__":
    unittest.main()
