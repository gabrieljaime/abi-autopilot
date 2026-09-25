import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from abi_autopilot.github import get_completion_evidence


class CompletionEvidenceTests(unittest.TestCase):
    def test_parses_latest_structured_pass_comment(self):
        body = """Autopilot completó #27.\n\n- Branch: `agent/issue-27-x`\n- Commit: `3f578236e4471f73654fd92b0355eed3f8c504fa`\n- Base inicial: `5251dce0d8a79869503f9a05a85cf1d4adb05005`\n- Integrado a `integration/test`: no\n- Reviewer: PASS\n- Targeted/Fast validation: PASS\n- Full validation: PASS"""
        payload = {"comments": [{"body": "old"}, {"body": body}]}
        with patch("abi_autopilot.github._gh", return_value=SimpleNamespace(stdout=json.dumps(payload))):
            ev = get_completion_evidence("x/y", 27)
        self.assertIsNotNone(ev)
        self.assertEqual(ev.branch, "agent/issue-27-x")
        self.assertTrue(ev.reviewer_pass and ev.targeted_fast_pass and ev.full_pass)

    def test_rejects_unstructured_comment(self):
        payload = {"comments": [{"body": "Autopilot completó #27. pero sin evidencia"}]}
        with patch("abi_autopilot.github._gh", return_value=SimpleNamespace(stdout=json.dumps(payload))):
            self.assertIsNone(get_completion_evidence("x/y", 27))


if __name__ == "__main__":
    unittest.main()
