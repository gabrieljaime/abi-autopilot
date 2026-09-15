import tempfile
import unittest
from pathlib import Path

from abi_autopilot.validator import discover_targeted_specs


class TargetedBackendTests(unittest.TestCase):
    def test_maps_backend_service_to_pytest_and_disables_global_coverage(self):
        with tempfile.TemporaryDirectory() as td:
            wt = Path(td)
            (wt / "backend/app/services").mkdir(parents=True)
            (wt / "backend/tests").mkdir(parents=True)
            (wt / "backend/app/services/resume_target.py").write_text("x=1\n", encoding="utf-8")
            (wt / "backend/tests/test_resume_target.py").write_text("def test_x(): assert True\n", encoding="utf-8")
            specs = discover_targeted_specs(
                wt,
                ["backend/app/services/resume_target.py"],
                {"validation": {"targeted_backend_no_cov": True}},
            )
            self.assertEqual(len(specs), 1)
            self.assertEqual(specs[0]["name"], "auto-pytest")
            self.assertIn("--no-cov", specs[0]["command"])
            self.assertIn("tests/test_resume_target.py", specs[0]["command"])

    def test_changed_backend_test_is_included_directly(self):
        with tempfile.TemporaryDirectory() as td:
            wt = Path(td)
            (wt / "backend/tests").mkdir(parents=True)
            (wt / "backend/tests/test_student_nudge.py").write_text("def test_x(): assert True\n", encoding="utf-8")
            specs = discover_targeted_specs(wt, ["backend/tests/test_student_nudge.py"], {})
            self.assertEqual(len(specs), 1)
            self.assertIn("tests/test_student_nudge.py", specs[0]["command"])


if __name__ == "__main__":
    unittest.main()
