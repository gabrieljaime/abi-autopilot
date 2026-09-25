import tempfile
import unittest
from pathlib import Path
from abi_autopilot.validator import CheckResult, looks_like_workspace_dependency_failure

class ValidatorEnvTests(unittest.TestCase):
    def test_jsdom_missing_is_environment(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/'x.log'
            p.write_text("Cannot find package 'jsdom' imported from C:\\Users\\x\\npm-cache\\_npx\\abc")
            r=CheckResult('vitest','npm test',td,1,str(p))
            self.assertTrue(looks_like_workspace_dependency_failure([r]))
