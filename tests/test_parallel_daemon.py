import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from abi_autopilot.locks import LockTimeout, file_lock
from abi_autopilot.orchestrator import Orchestrator


class FakeProc:
    """Finishes after `ticks` polls with return code `rc`."""
    _pid = 1000

    def __init__(self, ticks: int, rc: int = 0):
        FakeProc._pid += 1
        self.pid = FakeProc._pid
        self.ticks = ticks
        self.rc = rc

    def poll(self):
        if self.ticks > 0:
            self.ticks -= 1
            return None
        return self.rc


def make_orch(root: Path, max_parallel: int) -> Orchestrator:
    cfg = {
        "repo_slug": "owner/repo",
        "repo_path": str(root / "repo"),
        "base_branch": "main",
        "worktree_root": str(root / "wt"),
        "runtime_dir": str(root / "runtime"),
        "autopilot": {"poll_seconds": 1, "max_parallel": max_parallel},
    }
    return Orchestrator(root, cfg)


def issue(n):
    return SimpleNamespace(number=n, title=f"issue {n}", labels=["agent:ready"], risk="low")


class ParallelDaemonTests(unittest.TestCase):
    def test_once_starts_up_to_max_parallel_and_waits_for_all(self):
        with tempfile.TemporaryDirectory() as td:
            orch = make_orch(Path(td), max_parallel=2)
            started = []
            procs = {}

            def fake_popen(cmd, **kwargs):
                n = int(cmd[-1])
                started.append(n)
                procs[n] = FakeProc(ticks=2, rc=0 if n == 1 else 3)
                return procs[n]

            with mock.patch.object(orch, "eligible_queue", return_value=[issue(1), issue(2), issue(3)]), \
                 mock.patch.object(Orchestrator, "_popen", staticmethod(fake_popen)), \
                 mock.patch("abi_autopilot.orchestrator.time.sleep"):
                orch.daemon(once=True)

            self.assertEqual(started, [1, 2])
            self.assertTrue(all(p.poll() is not None for p in procs.values()))
            logs = sorted(p.name for p in (Path(td) / "runtime" / "daemon").iterdir())
            self.assertEqual(len(logs), 2)

    def test_child_command_runs_single_issue(self):
        with tempfile.TemporaryDirectory() as td:
            orch = make_orch(Path(td), max_parallel=3)
            seen = {}

            def fake_popen(cmd, **kwargs):
                seen["cmd"], seen["cwd"] = cmd, kwargs["cwd"]
                return FakeProc(ticks=0)

            with mock.patch.object(orch, "eligible_queue", return_value=[issue(7)]), \
                 mock.patch.object(Orchestrator, "_popen", staticmethod(fake_popen)), \
                 mock.patch("abi_autopilot.orchestrator.time.sleep"):
                orch.daemon(once=True)
            self.assertEqual(seen["cmd"][1:], ["-m", "abi_autopilot", "run", "--issue", "7"])
            self.assertEqual(seen["cwd"], str(Path(td)))

    def test_running_issue_is_not_started_twice(self):
        with tempfile.TemporaryDirectory() as td:
            orch = make_orch(Path(td), max_parallel=2)
            started = []
            scans = {"n": 0}

            def queue():
                scans["n"] += 1
                if scans["n"] > 3:
                    raise KeyboardInterrupt  # stop the endless daemon
                return [issue(5)]

            def fake_popen(cmd, **kwargs):
                started.append(int(cmd[-1]))
                return FakeProc(ticks=10_000)

            clock = iter(range(0, 10_000, 5))
            with mock.patch.object(orch, "eligible_queue", side_effect=queue), \
                 mock.patch.object(Orchestrator, "_popen", staticmethod(fake_popen)), \
                 mock.patch("abi_autopilot.orchestrator.time.sleep"), \
                 mock.patch("abi_autopilot.orchestrator.time.monotonic", side_effect=lambda: next(clock)):
                with self.assertRaises(KeyboardInterrupt):
                    orch.daemon(once=False)
            self.assertEqual(started, [5])

    def test_max_parallel_one_keeps_sequential_path(self):
        with tempfile.TemporaryDirectory() as td:
            orch = make_orch(Path(td), max_parallel=1)
            with mock.patch.object(orch, "next_issue", return_value=issue(9)), \
                 mock.patch.object(orch, "run_issue") as run_issue, \
                 mock.patch.object(Orchestrator, "_popen") as popen:
                orch.daemon(once=True)
            run_issue.assert_called_once_with(9)
            popen.assert_not_called()


class FileLockTests(unittest.TestCase):
    def test_lock_is_exclusive(self):
        with tempfile.TemporaryDirectory() as td:
            inside = []
            overlap = []

            def worker():
                with file_lock(td, "git-repo", poll=0.01):
                    inside.append(1)
                    if len(inside) > 1:
                        overlap.append(True)
                    time.sleep(0.05)
                    inside.pop()

            threads = [threading.Thread(target=worker) for _ in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            self.assertEqual(overlap, [])
            self.assertEqual(list(Path(td).iterdir()), [])

    def test_timeout_and_stale_reclaim(self):
        with tempfile.TemporaryDirectory() as td:
            with file_lock(td, "x"):
                with self.assertRaises(LockTimeout):
                    with file_lock(td, "x", timeout=0.05, poll=0.01):
                        pass
                # A lock older than stale_seconds is reclaimed.
                with file_lock(td, "x", timeout=1, stale_seconds=0, poll=0.01):
                    pass


if __name__ == "__main__":
    unittest.main()
