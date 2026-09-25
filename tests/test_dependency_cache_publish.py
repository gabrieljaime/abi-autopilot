"""Robustez de la publicación del dependency cache (incidente #211, WinError 5)."""
import contextlib
import io
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from abi_autopilot import bootstrap
from abi_autopilot.bootstrap import prepare_workspace


def _winerror5() -> PermissionError:
    e = PermissionError(13, "Acceso denegado", None, 5)
    # Outside Windows, OSError drops the winerror argument; set it so the test
    # reproduces the Windows failure on every platform.
    if getattr(e, "winerror", None) is None:
        e.winerror = 5
    return e


class CachePublishTestBase(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.root = Path(self._td.name)
        self.cfg = {
            "repo_path": str(self.root / "repo"),
            "dependency_cache": {
                "enabled": True,
                "root": str(self.root / "deps"),
                "min_free_gb": 0,
                "lock_timeout_seconds": 20,
                "stale_lock_seconds": 3600,
            },
            "workspace_bootstrap": [{
                "name": "frontend-npm-ci",
                "cwd": "frontend",
                "strategy": "npm_shared_cache",
                "command": "npm ci --no-audit --no-fund",
                "key_files": ["package-lock.json", "package.json"],
                "cache_probe": "node_modules/jsdom/package.json",
                "link_path": "node_modules",
                "timeout": 30,
            }],
        }
        self.wt = self._worktree("issue-1")
        self.run_dir = self.root / "run"
        self.run_dir.mkdir()

    def _worktree(self, name: str) -> Path:
        wt = self.root / name
        fe = wt / "frontend"
        fe.mkdir(parents=True)
        (fe / "package.json").write_text('{"name":"x"}', encoding="utf-8")
        (fe / "package-lock.json").write_text('{"lockfileVersion":3}', encoding="utf-8")
        return wt

    @staticmethod
    def fake_npm(command, cwd, log_path, timeout):
        probe = Path(cwd) / "node_modules" / "jsdom" / "package.json"
        probe.parent.mkdir(parents=True, exist_ok=True)
        probe.write_text('{"name":"jsdom"}', encoding="utf-8")
        Path(log_path).write_text("fake npm ci ok\n", encoding="utf-8")
        return 0

    def prepare(self, wt=None, run_dir=None):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            results = prepare_workspace(self.cfg, wt or self.wt, [], run_dir or self.run_dir)
        self.stdout = out.getvalue()
        return results[0]

    @property
    def npm_dir(self) -> Path:
        return self.root / "deps" / "npm"

    @property
    def key(self) -> str:
        return bootstrap._hash_dependency_inputs(
            self.wt / "frontend", self.cfg["workspace_bootstrap"][0]
        )[0]

    @property
    def final(self) -> Path:
        return self.npm_dir / self.key

    def make_valid_cache(self, dest: Path, key: str | None = None) -> None:
        probe = dest / "node_modules" / "jsdom" / "package.json"
        probe.parent.mkdir(parents=True)
        probe.write_text("{}", encoding="utf-8")
        bootstrap._write_ready_marker(dest, key or self.key, "frontend-npm-ci", ["package-lock.json"], "npm ci")

    def building_dirs(self):
        return sorted(self.npm_dir.glob(f".{self.key}.building-*"))

    @contextlib.contextmanager
    def failing_publish(self, failures, on_first=None):
        """Hace fallar `os.replace(staging, final)` `failures` veces (None = siempre)."""
        real = os.replace
        state = {"n": 0}
        final = str(self.final)

        def fake(src, dst, *a, **kw):
            if str(dst) == final and ".building-" in str(src):
                state["n"] += 1
                if on_first and state["n"] == 1:
                    on_first()
                if failures is None or state["n"] <= failures:
                    raise _winerror5()
            return real(src, dst, *a, **kw)

        sleeps: list[float] = []
        with patch("abi_autopilot.bootstrap.os.replace", side_effect=fake), \
                patch("abi_autopilot.bootstrap._sleep", side_effect=sleeps.append):
            self.sleeps = sleeps
            self.publish_calls = state
            yield


class Issue211Repro(CachePublishTestBase):
    """npm ci OK + rename building->final con WinError 5 no debe bloquear la issue."""

    def test_transient_permission_error_is_retried_and_bootstrap_continues(self):
        with patch("abi_autopilot.bootstrap.run_shell_stream", side_effect=self.fake_npm) as run, \
                self.failing_publish(failures=2):
            result = self.prepare()
        self.assertTrue(result.passed, result.reason)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(self.sleeps, list(bootstrap.PUBLISH_RETRY_DELAYS[:2]))
        self.assertTrue((self.wt / "node_modules" / "jsdom" / "package.json").exists())
        self.assertEqual(self.building_dirs(), [])

    def test_permanent_permission_error_reports_publish_failed_and_preserves_build(self):
        with patch("abi_autopilot.bootstrap.run_shell_stream", side_effect=self.fake_npm) as run, \
                self.failing_publish(failures=None):
            result = self.prepare()

        self.assertFalse(result.passed)
        self.assertEqual(result.status, bootstrap.STATUS_CACHE_PUBLISH_FAILED)
        self.assertEqual(result.returncode, bootstrap.RC_CACHE_PUBLISH_FAILED)
        self.assertIn("CACHE_BUILD_OK", result.reason)
        self.assertIn("CACHE_PUBLISH_FAILED", result.reason)
        self.assertNotIn("npm ci falló", result.reason)
        self.assertEqual(self.sleeps, list(bootstrap.PUBLISH_RETRY_DELAYS))
        self.assertEqual(self.publish_calls["n"], len(bootstrap.PUBLISH_RETRY_DELAYS) + 1)
        self.assertFalse(self.final.exists())
        staged = self.building_dirs()
        self.assertEqual(len(staged), 1)
        self.assertTrue(bootstrap._cache_valid(staged[0], self.key, "node_modules/jsdom/package.json"))
        self.assertIn("WinError5", self.stdout)
        self.assertIn("building preserved", self.stdout)

        # Reintento posterior (rename ya funciona): adopta el build sin reinstalar.
        with patch("abi_autopilot.bootstrap.run_shell_stream", side_effect=self.fake_npm) as run2:
            again = self.prepare()
        self.assertTrue(again.passed, again.reason)
        self.assertEqual(run.call_count + run2.call_count, 1)
        self.assertEqual(self.building_dirs(), [])

    def test_non_transient_error_is_not_retried(self):
        real = os.replace

        def fake(src, dst, *a, **kw):
            if str(dst) == str(self.final) and ".building-" in str(src):
                raise OSError(22, "invalid argument")
            return real(src, dst, *a, **kw)

        with patch("abi_autopilot.bootstrap.run_shell_stream", side_effect=self.fake_npm), \
                patch("abi_autopilot.bootstrap.os.replace", side_effect=fake), \
                patch("abi_autopilot.bootstrap._sleep") as sleep:
            result = self.prepare()
        self.assertEqual(result.status, bootstrap.STATUS_CACHE_PUBLISH_FAILED)
        sleep.assert_not_called()


class CacheSemanticsTests(CachePublishTestBase):
    def test_miss_builds_validates_publishes_then_hit_skips_install(self):
        with patch("abi_autopilot.bootstrap.run_shell_stream", side_effect=self.fake_npm) as run:
            first = self.prepare()
            self.assertTrue(first.passed)
            self.assertIn("built", first.reason)
            self.assertIn("install=PASS", self.stdout)
            self.assertIn("validation=PASS", self.stdout)
            self.assertIn("publish=PASS", self.stdout)
            second = self.prepare(self._worktree("issue-2"), self.run_dir)
            self.assertIn("hit", second.reason)
        self.assertEqual(run.call_count, 1)
        marker = __import__("json").loads((self.final / bootstrap.READY_MARKER).read_text(encoding="utf-8"))
        self.assertEqual(marker["cache_key"], self.key)
        self.assertEqual(marker["kind"], "frontend-npm-ci")
        self.assertEqual(marker["schema_version"], 1)

    def test_valid_prebuilt_cache_never_runs_install(self):
        self.make_valid_cache(self.final)
        with patch("abi_autopilot.bootstrap.run_shell_stream") as run:
            result = self.prepare()
        self.assertTrue(result.passed)
        run.assert_not_called()

    def test_legacy_marker_without_schema_version_is_still_valid(self):
        probe = self.final / "node_modules" / "jsdom" / "package.json"
        probe.parent.mkdir(parents=True)
        probe.write_text("{}", encoding="utf-8")
        (self.final / bootstrap.READY_MARKER).write_text(
            '{"version": 1, "key": "%s"}' % self.key, encoding="utf-8")
        with patch("abi_autopilot.bootstrap.run_shell_stream") as run:
            self.assertTrue(self.prepare().passed)
        run.assert_not_called()

    def test_concurrent_publish_of_valid_cache_uses_existing(self):
        def other_process_publishes():
            self.make_valid_cache(self.final)

        with patch("abi_autopilot.bootstrap.run_shell_stream", side_effect=self.fake_npm) as run, \
                self.failing_publish(failures=None, on_first=other_process_publishes):
            result = self.prepare()

        self.assertTrue(result.passed, result.reason)
        self.assertEqual(run.call_count, 1)
        self.assertIn("published concurrently by another process", self.stdout)
        self.assertEqual(self.building_dirs(), [])
        self.assertTrue(bootstrap._cache_valid(self.final, self.key, "node_modules/jsdom/package.json"))

    def test_corrupt_final_cache_is_not_used_and_is_quarantined(self):
        cases = {
            "sin marker": lambda d: None,
            "marker de otra key": lambda d: (d / bootstrap.READY_MARKER).write_text(
                '{"schema_version":1,"cache_key":"deadbeef"}', encoding="utf-8"),
            "marker ilegible": lambda d: (d / bootstrap.READY_MARKER).write_text("{no json", encoding="utf-8"),
        }
        for label, mutate in cases.items():
            with self.subTest(label):
                if self.final.exists():
                    import shutil
                    shutil.rmtree(self.final)
                probe = self.final / "node_modules" / "jsdom" / "package.json"
                probe.parent.mkdir(parents=True)
                probe.write_text("{}", encoding="utf-8")
                mutate(self.final)
                self.assertFalse(bootstrap._cache_valid(self.final, self.key, "node_modules/jsdom/package.json"))

                with patch("abi_autopilot.bootstrap.run_shell_stream", side_effect=self.fake_npm) as run:
                    result = self.prepare()
                self.assertTrue(result.passed, result.reason)
                self.assertEqual(run.call_count, 1)
                self.assertTrue(bootstrap._cache_valid(self.final, self.key, "node_modules/jsdom/package.json"))
                self.assertTrue(list(self.npm_dir.glob(f".{self.key}.corrupt-*")))

    def test_interruption_never_leaves_final_cache_or_ready_staging(self):
        with patch("abi_autopilot.bootstrap.run_shell_stream", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.prepare()
        self.assertFalse(self.final.exists())
        for staged in self.building_dirs():
            self.assertFalse((staged / bootstrap.READY_MARKER).exists())
        self.assertFalse(list(self.npm_dir.glob(".*.lock")), "el lock propio debe liberarse")

        # Y el siguiente bootstrap se recupera con normalidad.
        with patch("abi_autopilot.bootstrap.run_shell_stream", side_effect=self.fake_npm):
            self.assertTrue(self.prepare().passed)

    def test_install_failure_is_distinguished_from_cache_failures(self):
        with patch("abi_autopilot.bootstrap.run_shell_stream", return_value=1):
            result = self.prepare()
        self.assertEqual(result.status, bootstrap.STATUS_INSTALL_FAILED)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.building_dirs(), [])

    def test_missing_probe_is_validation_failure(self):
        def npm_without_probe(command, cwd, log_path, timeout):
            Path(log_path).write_text("ok", encoding="utf-8")
            return 0

        with patch("abi_autopilot.bootstrap.run_shell_stream", side_effect=npm_without_probe):
            result = self.prepare()
        self.assertEqual(result.status, bootstrap.STATUS_CACHE_VALIDATE_FAILED)

    def test_attach_failure_is_reported_as_such(self):
        with patch("abi_autopilot.bootstrap.run_shell_stream", side_effect=self.fake_npm), \
                patch("abi_autopilot.bootstrap._create_directory_link", side_effect=RuntimeError("mklink")):
            result = self.prepare()
        self.assertEqual(result.status, bootstrap.STATUS_CACHE_ATTACH_FAILED)
        self.assertEqual(result.returncode, 6)
        self.assertTrue(bootstrap._cache_valid(self.final, self.key, "node_modules/jsdom/package.json"))


class StaleBuildingTests(CachePublishTestBase):
    def _make_building(self, age_seconds: float) -> Path:
        d = self.npm_dir / f".{self.key}.building-999-abcd1234"
        (d / "node_modules").mkdir(parents=True)
        (d / "node_modules" / "partial.txt").write_text("x", encoding="utf-8")
        old = __import__("time").time() - age_seconds
        os.utime(d, (old, old))
        return d

    def test_stale_building_is_cleaned_without_blocking_build(self):
        self.npm_dir.mkdir(parents=True)
        stale = self._make_building(age_seconds=10 * 3600)
        with patch("abi_autopilot.bootstrap.run_shell_stream", side_effect=self.fake_npm):
            result = self.prepare()
        self.assertTrue(result.passed, result.reason)
        self.assertFalse(stale.exists())

    def test_active_building_of_another_process_is_not_deleted(self):
        self.npm_dir.mkdir(parents=True)
        active = self._make_building(age_seconds=5)
        with patch("abi_autopilot.bootstrap.run_shell_stream", side_effect=self.fake_npm):
            result = self.prepare()
        self.assertTrue(result.passed, result.reason)
        self.assertTrue(active.exists())
        self.assertTrue((active / "node_modules" / "partial.txt").exists())

    def test_incomplete_building_is_not_adopted(self):
        self.npm_dir.mkdir(parents=True)
        self._make_building(age_seconds=5)  # sin marker: install a medias
        with patch("abi_autopilot.bootstrap.run_shell_stream", side_effect=self.fake_npm) as run:
            self.prepare()
        self.assertEqual(run.call_count, 1)

    def test_lock_of_another_owner_is_not_released(self):
        # Si nuestro lock fue recuperado por stale y otro proceso lo re-tomó,
        # el finally no debe borrar el lock ajeno.
        self.npm_dir.mkdir(parents=True)
        lock_dir = self.npm_dir / f".{self.key}.lock"
        real_acquire = bootstrap._acquire_cache_lock

        def acquire_then_steal(*a, **kw):
            token = real_acquire(*a, **kw)
            (lock_dir / "owner.json").write_text('{"token": "someone-else"}', encoding="utf-8")
            return token

        with patch("abi_autopilot.bootstrap.run_shell_stream", side_effect=self.fake_npm), \
                patch("abi_autopilot.bootstrap._acquire_cache_lock", side_effect=acquire_then_steal):
            self.assertTrue(self.prepare().passed)
        self.assertTrue(lock_dir.exists())


class WindowsPathTests(CachePublishTestBase):
    def test_windows_style_root_and_long_names(self):
        long_root = self.root / ("a" * 30) / ("cache-" + "b" * 30)
        self.cfg["dependency_cache"]["root"] = str(long_root)
        with patch("abi_autopilot.bootstrap.run_shell_stream", side_effect=self.fake_npm) as run:
            first = self.prepare()
            self.assertTrue(first.passed, first.reason)
            self.assertTrue(self.prepare().passed)
        self.assertEqual(run.call_count, 1)
        self.assertTrue((long_root / "npm" / self.key / "node_modules" / "jsdom" / "package.json").exists())
        if os.name == "nt":
            self.assertRegex(str(self.wt), r"^[A-Za-z]:\\")


class ConcurrencyTests(CachePublishTestBase):
    def test_two_publishers_racing_for_the_same_key_yield_one_valid_cache(self):
        self.npm_dir.mkdir(parents=True)
        probe = "node_modules/jsdom/package.json"
        outcomes: list[str] = []
        errors: list[BaseException] = []
        barrier = threading.Barrier(2)

        def publisher(i: int):
            try:
                staging = self.npm_dir / f".{self.key}.building-{i}-x"
                (staging / "node_modules" / "jsdom").mkdir(parents=True)
                (staging / probe).write_text("{}", encoding="utf-8")
                bootstrap._write_ready_marker(staging, self.key, "frontend-npm-ci", [], "npm ci")
                barrier.wait()
                outcomes.append(bootstrap._publish_cache(staging, self.final, self.key, probe))
            except BaseException as e:  # pragma: no cover - se reporta abajo
                errors.append(e)

        with patch("abi_autopilot.bootstrap._sleep"):
            threads = [threading.Thread(target=publisher, args=(i,)) for i in range(2)]
            [t.start() for t in threads]
            [t.join(30) for t in threads]

        self.assertEqual(errors, [])
        self.assertEqual(sorted(outcomes), ["concurrent", "published"])
        self.assertTrue(bootstrap._cache_valid(self.final, self.key, probe))
        self.assertEqual(self.building_dirs(), [])
        self.assertEqual(list(self.npm_dir.glob(f".{self.key}.corrupt-*")), [])

    def test_two_full_bootstraps_same_key_install_once_and_neither_blocks(self):
        results: dict[int, object] = {}
        wts = {i: self._worktree(f"concurrent-{i}") for i in range(2)}
        runs = {i: self.root / f"run-{i}" for i in range(2)}
        for r in runs.values():
            r.mkdir()
        installs = []

        def slow_npm(command, cwd, log_path, timeout):
            installs.append(cwd)
            __import__("time").sleep(0.3)
            return self.fake_npm(command, cwd, log_path, timeout)

        def go(i):
            results[i] = prepare_workspace(self.cfg, wts[i], [], runs[i])[0]

        # El polling del lock duerme 2s; se acorta para no alargar el test.
        real_sleep = __import__("time").sleep
        with patch("abi_autopilot.bootstrap.run_shell_stream", side_effect=slow_npm), \
                patch("abi_autopilot.bootstrap.time.sleep", side_effect=lambda s: real_sleep(min(s, 0.05))), \
                contextlib.redirect_stdout(io.StringIO()):
            threads = [threading.Thread(target=go, args=(i,)) for i in range(2)]
            [t.start() for t in threads]
            [t.join(60) for t in threads]

        self.assertTrue(all(results[i].passed for i in range(2)), results)
        self.assertEqual(len(installs), 1)
        finals = [p for p in self.npm_dir.iterdir() if not p.name.startswith(".")]
        self.assertEqual(finals, [self.final])
        self.assertTrue(bootstrap._cache_valid(self.final, self.key, "node_modules/jsdom/package.json"))
        self.assertEqual(self.building_dirs(), [])
        for i in range(2):
            self.assertTrue((wts[i] / "node_modules" / "jsdom" / "package.json").exists())


if __name__ == "__main__":
    unittest.main()
