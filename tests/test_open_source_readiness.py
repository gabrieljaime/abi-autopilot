import unittest
from pathlib import Path
from unittest import mock

from abi_autopilot import github as ghx
from abi_autopilot.agents import _load_prompt
from abi_autopilot.config import default_config
from abi_autopilot.notice import FOOTER_MARKER, with_footer
from abi_autopilot.validator import _condition_matches, is_code_change

BASE_DIR = Path(__file__).resolve().parents[1]


class CommentFooterTests(unittest.TestCase):
    def test_comment_carries_copyright_and_disclaimer(self):
        with mock.patch.object(ghx, "_gh") as gh:
            ghx.comment("owner/repo", 7, "Autopilot completed #7.")
        body = gh.call_args.args[1][-1]
        self.assertTrue(body.startswith("Autopilot completed #7."))
        self.assertIn("Copyright (c) 2026 Gabriel Jaime", body)
        self.assertIn("MIT License", body)
        self.assertIn("without warranty", body)

    def test_footer_is_not_duplicated(self):
        once = with_footer("hello")
        self.assertEqual(with_footer(once), once)
        self.assertEqual(once.count(FOOTER_MARKER), 1)

    def test_completion_evidence_still_parses_with_footer(self):
        body = with_footer(
            "Autopilot completed #5.\n\n- Branch: `autopilot/issue-5`\n"
            "- Commit: `abcdef1`\n- Base: `1234567`\n"
            "- Reviewer: PASS\n- Targeted/Fast validation: PASS\n- Full validation: PASS"
        )
        payload = '{"comments": [{"body": %s}]}' % __import__("json").dumps(body)
        with mock.patch.object(ghx, "_gh", return_value=mock.Mock(stdout=payload)):
            ev = ghx.get_completion_evidence("owner/repo", 5)
        self.assertIsNotNone(ev)
        self.assertTrue(ev.full_pass)


class GenericRepoLayoutTests(unittest.TestCase):
    def test_any_non_doc_change_counts_as_code_by_default(self):
        self.assertTrue(is_code_change(["src/app.py"], {}))
        self.assertTrue(is_code_change(["README.md", "pkg/main.go"], {}))
        self.assertFalse(is_code_change(["README.md", "docs/guide.txt"], {}))

    def test_code_paths_restrict_what_counts(self):
        cfg = {"integration": {"code_paths": ["backend/", "frontend/"]}}
        self.assertFalse(is_code_change(["scripts/x.sh"], cfg))
        self.assertTrue(is_code_change(["backend/a.py"], cfg))

    def test_changed_prefix_condition(self):
        self.assertTrue(_condition_matches("changed:src/", ["src/a.ts"]))
        self.assertFalse(_condition_matches("changed:src/", ["docs/a.md"]))

    def test_defaults_do_not_pin_project_toolchain(self):
        cfg = default_config(Path("/tmp/repo"), "owner/repo", "main")
        self.assertEqual(cfg["toolchain"]["expected_python_major_minor"], "")
        self.assertIsNone(cfg["toolchain"]["expected_node_major"])
        self.assertEqual(cfg["dependency_ref_prefixes"], [])


class PromptProtectedPathsTests(unittest.TestCase):
    def test_prompts_list_configured_protected_paths(self):
        cfg = {"protected_paths": [".env", "secrets/"]}
        for name in ("implementer.md", "fixer.md"):
            text = _load_prompt(BASE_DIR, name, cfg)
            self.assertIn("`secrets/`", text)
            self.assertNotIn("{{PROTECTED_PATHS}}", text)
            self.assertNotIn("backend/data/", text)


if __name__ == "__main__":
    unittest.main()
