import os
import unittest
from unittest.mock import patch

from abi_autopilot.shell import _resolved_exec_args, run_capture


class ShellExecTests(unittest.TestCase):
    def test_missing_command_is_diagnostic_when_check_false(self):
        p = run_capture(["__abi_autopilot_command_that_does_not_exist__", "--version"], check=False)
        self.assertEqual(p.returncode, 127)
        self.assertTrue(p.stderr)

    def test_windows_cmd_wrapper_uses_comspec(self):
        with patch("abi_autopilot.shell.os.name", "nt"), \
             patch("abi_autopilot.shell.which") as mocked_which, \
             patch.dict(os.environ, {"COMSPEC": r"C:\\Windows\\System32\\cmd.exe"}):
            mocked_which.side_effect = lambda name: r"C:\\Program Files\\nodejs\\npm.cmd" if name == "npm" else None
            argv = _resolved_exec_args(["npm", "--version"])
        self.assertEqual(argv[:3], [r"C:\\Windows\\System32\\cmd.exe", "/d", "/c"])
        self.assertEqual(argv[3], r"C:\\Program Files\\nodejs\\npm.cmd")
        self.assertEqual(argv[4], "--version")


if __name__ == "__main__":
    unittest.main()
