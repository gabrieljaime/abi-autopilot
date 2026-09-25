import subprocess
import tempfile
import unittest
from pathlib import Path

from abi_autopilot.integration import IntegrationManager
from abi_autopilot.gitops import remote_branch_sha
from abi_autopilot.shell import CommandError


def run(cwd: Path, *args):
    return subprocess.run(list(args), cwd=cwd, check=True, capture_output=True, text=True)


class IntegrationLocalCandidateTests(unittest.TestCase):
    def _setup(self, td: str):
        root = Path(td)
        bare = root / "origin.git"
        repo = root / "repo"
        run(root, "git", "init", "--bare", str(bare))
        run(root, "git", "init", str(repo))
        run(repo, "git", "config", "user.email", "test@example.com")
        run(repo, "git", "config", "user.name", "Test")
        run(repo, "git", "checkout", "-b", "integration/test")
        (repo / "base.txt").write_text("base\n", encoding="utf-8")
        run(repo, "git", "add", "base.txt")
        run(repo, "git", "commit", "-m", "base")
        run(repo, "git", "remote", "add", "origin", str(bare))
        run(repo, "git", "push", "-u", "origin", "integration/test")
        cfg = {
            "repo_path": str(repo),
            "repo_slug": "x/y",
            "base_branch": "integration/test",
            "worktree_root": str(root / "worktrees"),
            "runtime_dir": str(root / "runtime"),
            "process_env": {"PLAYWRIGHT_HTML_OPEN": "never"},
            "integration": {},
        }
        return repo, IntegrationManager(root, cfg)

    def test_adopts_exact_single_local_issue_commit(self):
        with tempfile.TemporaryDirectory() as td:
            repo, manager = self._setup(td)
            remote = remote_branch_sha(str(repo), "integration/test")
            (repo / "x.txt").write_text("x\n", encoding="utf-8")
            run(repo, "git", "add", "x.txt")
            run(repo, "git", "commit", "-m", "fix(issue-27): deep link")
            local = run(repo, "git", "rev-parse", "HEAD").stdout.strip()
            self.assertEqual(manager._matching_local_candidate(27, remote), local)

    def test_refuses_unrelated_local_ahead_commit(self):
        with tempfile.TemporaryDirectory() as td:
            repo, manager = self._setup(td)
            remote = remote_branch_sha(str(repo), "integration/test")
            (repo / "x.txt").write_text("x\n", encoding="utf-8")
            run(repo, "git", "add", "x.txt")
            run(repo, "git", "commit", "-m", "unrelated local work")
            with self.assertRaises(CommandError):
                manager._matching_local_candidate(27, remote)


if __name__ == "__main__":
    unittest.main()
