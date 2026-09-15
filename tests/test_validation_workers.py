import unittest

from abi_autopilot.validator import _with_worker_limits


class ValidationWorkerTests(unittest.TestCase):
    def test_vitest_uses_bounded_workers(self):
        cfg = {"validation_workers": {"vitest": 4, "pytest": 1, "playwright": 1}}
        self.assertEqual(
            _with_worker_limits("npm test", "frontend-vitest", cfg),
            "npm test -- --maxWorkers=4",
        )
        self.assertEqual(
            _with_worker_limits('npm test -- "src/App.test.tsx"', "auto-vitest", cfg),
            'npm test -- "src/App.test.tsx" --maxWorkers=4',
        )

    def test_pytest_parallelism_is_opt_in(self):
        serial = {"validation_workers": {"pytest": 1}}
        parallel = {"validation_workers": {"pytest": 2}}
        self.assertEqual(
            _with_worker_limits("python -m pytest -q", "backend-pytest", serial),
            "python -m pytest -q",
        )
        self.assertEqual(
            _with_worker_limits("python -m pytest -q", "backend-pytest", parallel),
            "python -m pytest -q -n 2",
        )

    def test_playwright_defaults_to_explicit_single_worker(self):
        cfg = {"validation_workers": {"playwright": 1}}
        self.assertEqual(
            _with_worker_limits("npm run test:e2e", "frontend-e2e", cfg),
            "npm run test:e2e -- --workers=1",
        )


if __name__ == "__main__":
    unittest.main()
