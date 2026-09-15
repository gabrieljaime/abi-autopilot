import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from abi_autopilot.bootstrap import prepare_workspace, cleanup_done_artifacts


class DependencyCacheTests(unittest.TestCase):
    def _cfg(self, root: Path):
        return {
            "repo_path": str(root / "repo"),
            "worktree_root": str(root / "worktrees"),
            "dependency_cache": {
                "enabled": True,
                "root": str(root / "deps"),
                "min_free_gb": 0,
                "lock_timeout_seconds": 5,
                "stale_lock_seconds": 1,
                "cleanup_done_artifacts": True,
                "done_artifacts": ["frontend/.next", "frontend/node_modules", "frontend/playwright-report", "frontend/test-results"],
            },
            "workspace_bootstrap": [{
                "name": "frontend-npm-ci",
                "cwd": "frontend",
                "strategy": "npm_shared_cache",
                "command": "npm ci --no-audit --no-fund",
                "manifest": "package.json",
                "lockfile": "package-lock.json",
                "key_files": ["package-lock.json", "package.json"],
                "cache_probe": "node_modules/jsdom/package.json",
                "link_path": "node_modules",
                "timeout": 30,
            }],
        }

    def _worktree(self, root: Path, name: str, lock_text: str = '{"lockfileVersion":3}') -> Path:
        wt = root / name
        fe = wt / "frontend"
        fe.mkdir(parents=True)
        (fe / "package.json").write_text('{"name":"x","scripts":{"test":"echo ok"}}', encoding="utf-8")
        (fe / "package-lock.json").write_text(lock_text, encoding="utf-8")
        return wt

    def _fake_npm(self, command, cwd, log_path, timeout):
        cwd = Path(cwd)
        probe = cwd / "node_modules" / "jsdom" / "package.json"
        probe.parent.mkdir(parents=True, exist_ok=True)
        probe.write_text('{"name":"jsdom"}', encoding="utf-8")
        (cwd / "node_modules" / ".bin").mkdir(parents=True, exist_ok=True)
        Path(log_path).write_text("fake npm ci ok\n", encoding="utf-8")
        return 0

    def test_two_worktrees_reuse_one_cache(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cfg = self._cfg(root)
            wt1 = self._worktree(root, "issue-1")
            wt2 = self._worktree(root, "issue-2")
            run1 = root / "run1"; run1.mkdir()
            run2 = root / "run2"; run2.mkdir()

            with patch("abi_autopilot.bootstrap.run_shell_stream", side_effect=self._fake_npm) as run:
                r1 = prepare_workspace(cfg, wt1, [], run1)
                r2 = prepare_workspace(cfg, wt2, [], run2)

            self.assertTrue(r1[0].passed)
            self.assertTrue(r2[0].passed)
            self.assertEqual(run.call_count, 1)
            self.assertTrue((wt1 / "node_modules" / "jsdom" / "package.json").exists())
            self.assertTrue((wt2 / "node_modules" / "jsdom" / "package.json").exists())
            cache_entries = [p for p in (root / "deps" / "npm").iterdir() if p.is_dir() and not p.name.startswith('.')]
            self.assertEqual(len(cache_entries), 1)

    def test_manifest_change_builds_new_cache_and_repoints(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cfg = self._cfg(root)
            wt = self._worktree(root, "issue-1")
            run1 = root / "run1"; run1.mkdir()
            run2 = root / "run2"; run2.mkdir()

            with patch("abi_autopilot.bootstrap.run_shell_stream", side_effect=self._fake_npm) as run:
                self.assertTrue(prepare_workspace(cfg, wt, [], run1)[0].passed)
                (wt / "frontend" / "package-lock.json").write_text('{"lockfileVersion":3,"packages":{"x":{}}}', encoding="utf-8")
                self.assertTrue(prepare_workspace(cfg, wt, ["frontend/package-lock.json"], run2)[0].passed)

            self.assertEqual(run.call_count, 2)
            cache_entries = [p for p in (root / "deps" / "npm").iterdir() if p.is_dir() and not p.name.startswith('.')]
            self.assertEqual(len(cache_entries), 2)
            self.assertTrue((wt / "node_modules" / "jsdom" / "package.json").exists())

    def test_old_frontend_node_modules_is_removed(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cfg = self._cfg(root)
            wt = self._worktree(root, "issue-1")
            old = wt / "frontend" / "node_modules" / "oldpkg"
            old.mkdir(parents=True)
            (old / "x.txt").write_text("old", encoding="utf-8")
            run_dir = root / "run"; run_dir.mkdir()

            with patch("abi_autopilot.bootstrap.run_shell_stream", side_effect=self._fake_npm):
                result = prepare_workspace(cfg, wt, [], run_dir)

            self.assertTrue(result[0].passed)
            self.assertFalse((wt / "frontend" / "node_modules").exists())
            self.assertTrue((wt / "node_modules" / "jsdom" / "package.json").exists())

    def test_cleanup_done_artifacts_keeps_dependency_link(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cfg = self._cfg(root)
            wt = self._worktree(root, "issue-1")
            for rel in ["frontend/.next", "frontend/playwright-report", "frontend/test-results"]:
                p = wt / rel
                p.mkdir(parents=True)
                (p / "x.txt").write_text("x", encoding="utf-8")
            deps = wt / "node_modules"
            deps.mkdir()
            (deps / "keep.txt").write_text("keep", encoding="utf-8")

            msgs = cleanup_done_artifacts(cfg, wt)

            self.assertEqual(len(msgs), 3)
            self.assertFalse((wt / "frontend" / ".next").exists())
            self.assertFalse((wt / "frontend" / "playwright-report").exists())
            self.assertFalse((wt / "frontend" / "test-results").exists())
            self.assertTrue((wt / "node_modules" / "keep.txt").exists())


if __name__ == "__main__":
    unittest.main()
