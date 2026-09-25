import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from abi_autopilot import cli
from abi_autopilot.detect import detect_project, parse_github_slug


def write(path: Path, text: str = ""):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def names(det, phase):
    return [c["name"] for c in det.validation[phase]]


class DetectTests(unittest.TestCase):
    def test_npm_project_uses_scripts_and_shared_cache(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            write(root / "package.json", json.dumps({"scripts": {
                "test": "vitest run", "lint": "eslint .", "typecheck": "tsc --noEmit",
                "build": "vite build", "test:e2e": "playwright test"}}))
            write(root / "package-lock.json", "{}")
            write(root / ".nvmrc", "v20.11.0\n")
            det = detect_project(root)
        self.assertEqual(names(det, "fast"), ["vitest", "typecheck", "lint"])
        self.assertEqual(names(det, "full"), ["build", "playwright"])
        self.assertEqual(det.validation["fast"][0]["command"], "npm test")
        self.assertEqual(det.validation["fast"][0]["when"], "always")
        self.assertEqual(det.bootstrap[0]["strategy"], "npm_shared_cache")
        self.assertEqual(det.toolchain["expected_node_major"], 20)

    def test_placeholder_npm_test_is_ignored_and_other_e2e_runner_gets_neutral_name(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            write(root / "package.json", json.dumps({"scripts": {
                "test": "echo \"Error: no test specified\" && exit 1", "e2e": "cypress run"}}))
            write(root / "yarn.lock")
            det = detect_project(root)
        self.assertEqual(names(det, "fast"), [])
        self.assertEqual(names(det, "full"), ["e2e-tests"])
        self.assertEqual(det.validation["full"][0]["command"], "yarn run e2e")
        self.assertEqual(det.bootstrap[0]["command"], "yarn install --frozen-lockfile")

    def test_monorepo_frontend_backend(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            write(root / "frontend" / "package.json", json.dumps({"scripts": {"test": "jest"}}))
            write(root / "frontend" / "pnpm-lock.yaml")
            write(root / "backend" / "pyproject.toml", "[tool.ruff]\n[tool.pytest.ini_options]\n")
            write(root / "node_modules" / "x" / "package.json", "{}")
            det = detect_project(root)
        fast = {c["name"]: c for c in det.validation["fast"]}
        self.assertEqual(set(fast), {"frontend-jest", "backend-pytest", "backend-ruff"})
        self.assertEqual(fast["frontend-jest"]["command"], "pnpm run test")
        self.assertEqual(fast["backend-pytest"]["when"], "changed:backend/")
        self.assertIn("frontend/node_modules", det.done_artifacts)

    def test_go_and_rust(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            write(root / "go.mod", "module x\n")
            write(root / "tool" / "Cargo.toml", "[package]\n")
            det = detect_project(root)
        self.assertIn("go-test", names(det, "fast"))
        self.assertIn("tool-cargo-test", names(det, "fast"))

    def test_nothing_detected(self):
        with tempfile.TemporaryDirectory() as td:
            det = detect_project(td)
        self.assertFalse(det.found)

    def test_parse_github_slug(self):
        self.assertEqual(parse_github_slug("https://github.com/gabrieljaime/abi-autopilot.git"), "gabrieljaime/abi-autopilot")
        self.assertEqual(parse_github_slug("git@github.com:owner/repo.git"), "owner/repo")
        self.assertIsNone(parse_github_slug("https://gitlab.com/owner/repo.git"))


class InitCommandTests(unittest.TestCase):
    def _git_repo(self, root: Path):
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        subprocess.run(["git", "-C", str(root), "remote", "add", "origin", "https://github.com/acme/shop.git"], check=True)

    def test_non_interactive_init_infers_slug_and_detects_checks(self):
        with tempfile.TemporaryDirectory() as td:
            home, repo = Path(td) / "home", Path(td) / "shop"
            home.mkdir()
            self._git_repo(repo)
            write(repo / "package.json", json.dumps({"scripts": {"test": "vitest"}}))
            with mock.patch.dict(os.environ, {}, clear=False):
                cli.main(["init", "--repo", str(repo), "--base-branch", "main", "--non-interactive"], home=home, prog="test")
            cfg = json.loads((home / "config.local.json").read_text(encoding="utf-8"))
        self.assertEqual(cfg["repo_slug"], "acme/shop")
        self.assertEqual([c["name"] for c in cfg["validation"]["fast"]], ["vitest"])
        self.assertEqual(cfg["protected_paths"], [".env"])
        self.assertFalse(cfg["dependency_cache"]["enabled"])

    def test_interactive_init_asks_for_missing_values(self):
        with tempfile.TemporaryDirectory() as td:
            home, repo = Path(td) / "home", Path(td) / "shop"
            home.mkdir()
            self._git_repo(repo)
            answers = iter([str(repo), "", "develop", "main", "y"])
            with mock.patch("sys.stdin.isatty", return_value=True), \
                 mock.patch("builtins.input", side_effect=lambda _q: next(answers)):
                cli.main(["init"], home=home, prog="test")
            cfg = json.loads((home / "config.local.json").read_text(encoding="utf-8"))
        self.assertEqual(cfg["repo_slug"], "acme/shop")
        self.assertEqual(cfg["base_branch"], "develop")
        self.assertEqual(cfg["deployment_branch"], "main")

    def test_init_refuses_non_git_folder(self):
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(SystemExit):
                cli.main(["init", "--repo", td, "--repo-slug", "a/b", "--base-branch", "main", "--non-interactive"], home=Path(td), prog="test")


if __name__ == "__main__":
    unittest.main()
