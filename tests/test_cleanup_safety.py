import subprocess
import tempfile
import unittest
from pathlib import Path

from abi_autopilot.bootstrap import cleanup_done_artifacts


def _git(root: Path, *args):
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True)


class CleanupSafetyTests(unittest.TestCase):
    def _repo(self, root: Path):
        _git(root, "init")
        _git(root, "config", "user.email", "test@example.com")
        _git(root, "config", "user.name", "Test")

    def test_cleanup_never_removes_tracked_path(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._repo(root)
            p = root / ".coverage"
            p.write_text("tracked", encoding="utf-8")
            _git(root, "add", ".coverage")
            _git(root, "commit", "-m", "tracked fixture")
            cfg = {"dependency_cache": {"cleanup_done_artifacts": True, "done_artifacts": [".coverage"]}}
            msgs = cleanup_done_artifacts(cfg, root)
            self.assertTrue(p.exists())
            self.assertIn("SKIP tracked path: .coverage", msgs)

    def test_cleanup_removes_untracked_generated_file(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._repo(root)
            p = root / ".coverage"
            p.write_text("generated", encoding="utf-8")
            cfg = {"dependency_cache": {"cleanup_done_artifacts": True, "done_artifacts": [".coverage"]}}
            msgs = cleanup_done_artifacts(cfg, root)
            self.assertFalse(p.exists())
            self.assertIn("removed .coverage", msgs)


if __name__ == "__main__":
    unittest.main()
