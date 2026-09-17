"""Gate de release: integrado != entregado.

Estos tests reproducen el incidente que motivó el cambio: issues cerradas como
terminadas cuyo código vivía únicamente en la rama de integración y nunca era
alcanzable desde la rama de despliegue (`main`), que es la que se despliega.
"""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from abi_autopilot.config import default_config, load_config
from abi_autopilot.gitops import (
    branch_divergence,
    branch_release_status,
    commit_is_released,
    is_ancestor,
)


def git(repo, *args, check=True):
    return subprocess.run(
        ["git", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=check,
    )


def commit_file(repo, name, content, message):
    (Path(repo) / name).write_text(content, encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-m", message)
    return git(repo, "rev-parse", "HEAD").stdout.strip()


class ReleaseGateRepoTests(unittest.TestCase):
    """Escenarios sobre un repositorio git real, no mocks."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name) / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "config", "user.email", "t@example.com")
        git(self.repo, "config", "user.name", "T")
        commit_file(self.repo, "seed.txt", "seed\n", "seed")
        # `integration` arranca desde el mismo punto que `main`.
        git(self.repo, "branch", "integration")

    def tearDown(self):
        self.tmp.cleanup()

    # ── Caso 1 ──────────────────────────────────────────────────────────────
    def test_merged_to_integration_but_not_in_main_is_not_released(self):
        """Mergear a integración no entrega: la issue queda pendiente."""
        git(self.repo, "checkout", "-q", "-b", "agent/issue-71-router")
        sha = commit_file(self.repo, "router.py", "routes\n", "fix(issue-71): router")
        git(self.repo, "checkout", "-q", "integration")
        git(self.repo, "merge", "--no-ff", "-m", "merge 71", "agent/issue-71-router")

        # Está en integración…
        self.assertTrue(is_ancestor(self.repo, sha, "integration"))
        # …y no en la rama de despliegue.
        self.assertFalse(is_ancestor(self.repo, sha, "main"))
        self.assertFalse(commit_is_released(self.repo, sha, "main"))

        status = branch_release_status(self.repo, "agent/issue-71-router", "main")
        self.assertFalse(status["released"])
        self.assertEqual(len(status["pending"]), 1)

    # ── Caso 2 ──────────────────────────────────────────────────────────────
    def test_cherry_picked_to_main_counts_as_released_despite_new_sha(self):
        """Promover por cherry-pick cambia el SHA pero entrega el mismo trabajo."""
        git(self.repo, "checkout", "-q", "-b", "agent/issue-95-chat")
        sha = commit_file(self.repo, "chat.py", "chat\n", "fix(issue-95): chat")
        git(self.repo, "checkout", "-q", "main")
        # `main` avanza por su cuenta primero: así el cherry-pick cuelga de otro
        # padre y necesariamente tiene otro SHA, que es el caso real.
        commit_file(self.repo, "unrelated.txt", "x\n", "commit propio de main")
        git(self.repo, "cherry-pick", sha)
        new_sha = git(self.repo, "rev-parse", "HEAD").stdout.strip()

        self.assertNotEqual(sha, new_sha)
        # Comparar SHAs diría que no está: por eso no alcanza.
        self.assertFalse(is_ancestor(self.repo, sha, "main"))
        # La equivalencia de parche sí lo reconoce.
        self.assertTrue(commit_is_released(self.repo, sha, "main"))
        self.assertTrue(branch_release_status(self.repo, "agent/issue-95-chat", "main")["released"])

    def test_partially_promoted_branch_is_not_released(self):
        """Si queda un commit propio sin promover, la issue no está entregada."""
        git(self.repo, "checkout", "-q", "-b", "agent/issue-97-polling")
        first = commit_file(self.repo, "a.py", "a\n", "fix(issue-97): part 1")
        commit_file(self.repo, "b.py", "b\n", "fix(issue-97): part 2")
        git(self.repo, "checkout", "-q", "main")
        commit_file(self.repo, "unrelated.txt", "x\n", "commit propio de main")
        git(self.repo, "cherry-pick", first)

        status = branch_release_status(self.repo, "agent/issue-97-polling", "main")
        self.assertFalse(status["released"])
        self.assertEqual(len(status["pending"]), 1)
        self.assertEqual(len(status["equivalent"]), 1)

    def test_message_match_finds_work_promoted_with_conflict_resolution(self):
        """Una promoción con conflictos resueltos cambia el parche, no el trabajo.

        `git cherry` la marca `+` porque el patch-id difiere. El prefijo de
        commit de Autopilot sigue identificándola: es el caso real de #71, que
        se promovió por cherry-pick resolviendo tres conflictos.
        """
        from abi_autopilot.gitops import find_issue_commits_on_branch

        git(self.repo, "checkout", "-q", "-b", "agent/issue-71-router")
        sha = commit_file(self.repo, "router.py", "routes v1\n", "fix(issue-71): router")
        git(self.repo, "checkout", "-q", "main")
        # Promoción con el contenido adaptado (conflicto resuelto).
        commit_file(self.repo, "router.py", "routes v1 adaptado\n", "fix(issue-71): router")

        # El patch-id ya no coincide…
        self.assertFalse(commit_is_released(self.repo, sha, "main"))
        # …pero el trabajo es identificable en la rama de despliegue.
        self.assertEqual(len(find_issue_commits_on_branch(self.repo, "main", 71)), 1)
        # y el prefijo no levanta falsos positivos de otras issues.
        self.assertEqual(find_issue_commits_on_branch(self.repo, "main", 99), [])

    # ── Caso 4 ──────────────────────────────────────────────────────────────
    def test_divergence_is_reported_in_both_directions(self):
        """main e integración con trabajo exclusivo cada una."""
        git(self.repo, "checkout", "-q", "main")
        commit_file(self.repo, "hotfix.txt", "fix\n", "hotfix sólo en main")
        commit_file(self.repo, "hotfix2.txt", "fix2\n", "otro hotfix en main")
        git(self.repo, "checkout", "-q", "integration")
        commit_file(self.repo, "feature.txt", "feat\n", "feature sólo en integración")

        left, right = branch_divergence(self.repo, "main", "integration")
        self.assertEqual((left, right), (2, 1))

    def test_no_divergence_when_integration_is_behind_only(self):
        """Integración atrasada no es divergencia: no hay trabajo exclusivo suyo."""
        git(self.repo, "checkout", "-q", "main")
        commit_file(self.repo, "only-main.txt", "m\n", "commit sólo en main")

        left, right = branch_divergence(self.repo, "main", "integration")
        self.assertEqual(left, 1)
        self.assertEqual(right, 0)


class ReleaseConfigTests(unittest.TestCase):
    # ── Caso 3 ──────────────────────────────────────────────────────────────
    def test_deployment_defaults_to_base_branch(self):
        """Sin rama de despliegue explícita se conserva el flujo de una sola rama."""
        cfg = default_config(Path("."), "owner/repo", "main")
        self.assertEqual(cfg["deployment_branch"], "main")

    def test_explicit_deployment_branch_is_kept(self):
        cfg = default_config(Path("."), "owner/repo", "integration/x", "main")
        self.assertEqual(cfg["base_branch"], "integration/x")
        self.assertEqual(cfg["deployment_branch"], "main")

    def test_legacy_config_without_deployment_branch_loads(self):
        """Una config vieja (pre-separación) sigue cargando y no cambia de flujo."""
        import json

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(
                json.dumps(
                    {
                        "repo_path": tmp,
                        "repo_slug": "owner/repo",
                        "base_branch": "main",
                        "worktree_root": tmp,
                    }
                ),
                encoding="utf-8",
            )
            cfg = load_config(path)
            self.assertEqual(cfg["deployment_branch"], "main")


class ReleaseGateManagerTests(unittest.TestCase):
    """El gate en `IntegrationManager`, sin tocar red ni GitHub."""

    def _manager(self, base, deployment, require=True):
        from abi_autopilot.integration import IntegrationManager

        cfg = default_config(Path(os.getcwd()), "owner/repo", base, deployment)
        cfg["release"]["require_deployment_branch"] = require
        return IntegrationManager(Path(os.getcwd()), cfg)

    def test_same_branch_keeps_single_step_flow(self):
        """Caso 3: integración == despliegue ⇒ cerrar al integrar, como antes."""
        mgr = self._manager("main", "main")
        self.assertTrue(mgr._deployment_is_integration())
        self.assertEqual(mgr.release_drift(), (0, 0))

    def test_separate_branches_require_promotion(self):
        mgr = self._manager("integration/x", "main")
        self.assertFalse(mgr._deployment_is_integration())
        self.assertEqual(mgr._deployment_ref(), "origin/main")

    def test_opt_out_restores_legacy_behaviour(self):
        """Un proyecto puede desactivar el gate explícitamente."""
        mgr = self._manager("integration/x", "main", require=False)
        self.assertTrue(mgr._deployment_is_integration())


if __name__ == "__main__":
    unittest.main()
