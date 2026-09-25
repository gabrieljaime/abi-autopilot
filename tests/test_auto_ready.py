import unittest
from unittest.mock import patch

from abi_autopilot.github import Issue
from abi_autopilot.orchestrator import Orchestrator


def _cfg():
    return {
        "repo_path": "C:\\repo",
        "repo_slug": "x/y",
        "base_branch": "integration/test",
        "worktree_root": "C:\\repo-worktrees",
        "runtime_dir": "runtime",
        "autopilot": {"skip_epics": True, "auto_ready_default_risk": "medium"},
        "process_env": {},
    }


class AutoReadyTests(unittest.TestCase):
    def _orch(self, tmp_path):
        return Orchestrator(tmp_path, _cfg())

    def test_skips_epics_and_needs_human(self, tmp_path=None):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as td:
            orch = Orchestrator(Path(td), _cfg())
            epic = Issue(1, "[EPIC] Bigger thing", "", "OPEN", "u", [])
            blocked = Issue(2, "Needs product call", "", "OPEN", "u", ["needs:product"])
            with patch("abi_autopilot.orchestrator.ghx.list_untriaged", return_value=[epic, blocked]):
                rows = orch.auto_ready_scan()
            self.assertEqual(rows[0]["action"], "skipped")
            self.assertEqual(rows[0]["reason"], "epic")
            self.assertEqual(rows[1]["action"], "skipped")
            self.assertIn("needs:human/needs:product", rows[1]["reason"])

    def test_skips_open_dependencies(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as td:
            orch = Orchestrator(Path(td), _cfg())
            issue = Issue(3, "Follow-up", "## Dependencias\n- #2\n", "OPEN", "u", [])
            with patch("abi_autopilot.orchestrator.ghx.list_untriaged", return_value=[issue]), \
                 patch("abi_autopilot.orchestrator.ghx.deps_status", return_value=(False, [("#2", "OPEN")])):
                rows = orch.auto_ready_scan()
            self.assertEqual(rows[0]["action"], "skipped")
            self.assertIn("open dependencies", rows[0]["reason"])

    def test_marks_ready_when_eligible(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as td:
            orch = Orchestrator(Path(td), _cfg())
            issue = Issue(4, "Small fix", "", "OPEN", "u", [])
            after_risk = Issue(4, "Small fix", "", "OPEN", "u", ["risk:medium"])
            with patch("abi_autopilot.orchestrator.ghx.list_untriaged", return_value=[issue]), \
                 patch("abi_autopilot.orchestrator.ghx.deps_status", return_value=(True, [])), \
                 patch("abi_autopilot.orchestrator.ghx.set_risk") as set_risk, \
                 patch("abi_autopilot.orchestrator.ghx.get_issue", return_value=after_risk), \
                 patch("abi_autopilot.orchestrator.ghx.set_state") as set_state:
                rows = orch.auto_ready_scan()
            self.assertEqual(rows[0], {"issue": 4, "action": "marked-ready", "risk": "medium"})
            set_risk.assert_called_once_with("x/y", issue, "medium")
            set_state.assert_called_once_with("x/y", after_risk, "agent:ready", remove_special=True)

    def test_keeps_existing_risk_label(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as td:
            orch = Orchestrator(Path(td), _cfg())
            issue = Issue(5, "Small fix", "", "OPEN", "u", ["risk:high"])
            with patch("abi_autopilot.orchestrator.ghx.list_untriaged", return_value=[issue]), \
                 patch("abi_autopilot.orchestrator.ghx.deps_status", return_value=(True, [])), \
                 patch("abi_autopilot.orchestrator.ghx.set_risk") as set_risk, \
                 patch("abi_autopilot.orchestrator.ghx.get_issue", return_value=issue), \
                 patch("abi_autopilot.orchestrator.ghx.set_state"):
                rows = orch.auto_ready_scan()
            self.assertEqual(rows[0]["risk"], "high")
            set_risk.assert_called_once_with("x/y", issue, "high")


if __name__ == "__main__":
    unittest.main()
