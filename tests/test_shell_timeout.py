import sys
import tempfile
import time
import unittest
from pathlib import Path

from abi_autopilot.shell import run_shell_stream, shell_join


class ShellTimeoutTests(unittest.TestCase):
    def test_silent_process_has_real_wall_clock_timeout(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            log = root / "timeout.log"
            cmd = shell_join([sys.executable, "-c", "import time; time.sleep(4)"])
            start = time.monotonic()
            rc = run_shell_stream(cmd, root, log, timeout=1)
            elapsed = time.monotonic() - start
            self.assertEqual(rc, 124)
            self.assertLess(elapsed, 3.5)
            self.assertIn("TIMEOUT", log.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
