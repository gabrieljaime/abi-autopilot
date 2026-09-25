"""Ciclo de vida implementado → integrado → entregado, fuera de `integrate`.

`test_release_gate.py` cubre el gate de `integrate`/`integrate-done`. Estos
tests cubren los caminos que v1.8.0 dejó abiertos:

- `finalize` con `auto_integrate` + `auto_close` hacía fast-forward a la rama
  de integración y cerraba la issue sin mirar la rama de despliegue.
- `agent:done` seguía marcando "implementado, esperando integración".
- `consistency_check` no auditaba nada con integración == despliegue.
"""
import io
import json
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from abi_autopilot import github as ghx
from abi_autopilot.config import default_config
from abi_autopilot.orchestrator import Orchestrator, finalize_lifecycle


def issue(number, labels, state="OPEN", title="t"):
    return ghx.Issue(number, title, "", state, f"https://x/{number}", list(labels))


def orchestrator(base="main", deployment="main", require=True):
    """Orchestrator sin tocar disco, red ni GitHub."""
    cfg = default_config(Path("."), "owner/repo", base, deployment)
    cfg["release"]["require_deployment_branch"] = require
    orch = Orchestrator.__new__(Orchestrator)
    orch.cfg = cfg
    orch.repo_slug = cfg["repo_slug"]
    orch.repo_path = "."
    orch.base_branch = base
    orch.deployment_branch = deployment
    return orch


class FinalizeLifecycleTests(unittest.TestCase):
    # ── Caso 1 por el camino automático ─────────────────────────────────────
    def test_auto_integrated_into_integration_branch_does_not_close(self):
        """El bypass real: fast-forward a integración con auto_close activo."""
        self.assertEqual(
            finalize_lifecycle(integrated=True, gate_active=True, released=False),
            (ghx.INTEGRATED_LABEL, False),
        )

    def test_not_integrated_is_implemented_not_done(self):
        """`agent:done` ya no significa "esperando integración"."""
        label, close = finalize_lifecycle(integrated=False, gate_active=True, released=False)
        self.assertEqual(label, ghx.IMPLEMENTED_LABEL)
        self.assertFalse(close)
        label, close = finalize_lifecycle(integrated=False, gate_active=False, released=False)
        self.assertEqual(label, ghx.IMPLEMENTED_LABEL)
        self.assertFalse(close)

    def test_integrated_and_already_released_closes(self):
        self.assertEqual(
            finalize_lifecycle(integrated=True, gate_active=True, released=True),
            (ghx.DONE_LABEL, True),
        )

    # ── Caso 3 ──────────────────────────────────────────────────────────────
    def test_single_branch_integrating_is_delivering(self):
        self.assertEqual(
            finalize_lifecycle(integrated=True, gate_active=False, released=False),
            (ghx.DONE_LABEL, True),
        )

    def test_gate_follows_config(self):
        self.assertTrue(orchestrator("integration/x", "main")._release_gate_active())
        self.assertFalse(orchestrator("main", "main")._release_gate_active())
        self.assertFalse(orchestrator("integration/x", "main", require=False)._release_gate_active())


class IntegrationCandidatesTests(unittest.TestCase):
    def test_implemented_and_legacy_open_done_are_candidates(self):
        by_label = {
            ghx.IMPLEMENTED_LABEL: [issue(5, [ghx.IMPLEMENTED_LABEL]), issue(9, [ghx.IMPLEMENTED_LABEL])],
            ghx.DONE_LABEL: [issue(7, [ghx.DONE_LABEL]), issue(9, [ghx.DONE_LABEL])],
        }
        with mock.patch.object(ghx, "_list_with_label", side_effect=lambda repo, label, limit=100: by_label.get(label, [])):
            got = ghx.list_integration_candidates("owner/repo")
        self.assertEqual([i.number for i in got], [5, 7, 9])

    def test_implemented_label_is_a_state_label(self):
        """`set_state` debe poder quitarla al avanzar de etapa."""
        self.assertIn(ghx.IMPLEMENTED_LABEL, ghx.AGENT_STATE_LABELS)
        self.assertLess(
            ghx.AGENT_STATE_LABELS.index(ghx.IMPLEMENTED_LABEL),
            ghx.AGENT_STATE_LABELS.index(ghx.INTEGRATED_LABEL),
        )


class ConsistencyCheckTests(unittest.TestCase):
    def _run(self, orch, closed, released_by_issue):
        payload = json.dumps([
            {"number": i.number, "title": i.title, "body": "", "state": "CLOSED",
             "url": i.url, "labels": [{"name": l} for l in i.labels]}
            for i in closed
        ])
        gh = mock.Mock(return_value=mock.Mock(stdout=payload))

        def release_state(i):
            released = released_by_issue[i.number]
            return {"issue": i.number, "branch_known": released is not None,
                    "released": released, "pending_commits": [] if released else ["abc"]}

        with mock.patch.object(ghx, "_gh", gh), \
             mock.patch("abi_autopilot.orchestrator.git_fetch"), \
             mock.patch.object(orch, "issue_release_state", side_effect=release_state):
            return orch.consistency_check(), gh

    def test_single_branch_still_audits_closed_issues(self):
        """Antes devolvía [] sin mirar nada cuando base == deployment."""
        closed = [issue(71, [ghx.DONE_LABEL], "CLOSED"), issue(72, [ghx.DONE_LABEL], "CLOSED")]
        errors, _ = self._run(orchestrator("main", "main"), closed, {71: False, 72: True})
        self.assertEqual([e["issue"] for e in errors], [71])

    def test_unknown_branch_is_not_an_error(self):
        closed = [issue(80, [ghx.DONE_LABEL], "CLOSED")]
        errors, _ = self._run(orchestrator("integration/x", "main"), closed, {80: None})
        self.assertEqual(errors, [])

    def test_audit_limit_covers_old_issues(self):
        """200 dejaba afuera las issues viejas en un repo con 300+."""
        _, gh = self._run(orchestrator(), [], {})
        args = gh.call_args[0][1]
        self.assertEqual(args[args.index("--limit") + 1], "1000")


class ObserveReleaseSectionTests(unittest.TestCase):
    def _print(self, orch, overview):
        base = {"integration_branch": orch.base_branch, "deployment_branch": orch.deployment_branch,
                "separate": orch.base_branch != orch.deployment_branch,
                "drift_deployment_only": 0, "drift_integration_only": 0,
                "implemented": 0, "released_closed": 0, "rows": []}
        base.update(overview)
        out = io.StringIO()
        with mock.patch.object(orch, "release_overview", return_value=base), redirect_stdout(out):
            orch.print_release_section()
        return out.getvalue()

    # ── Caso 4 ──────────────────────────────────────────────────────────────
    def test_divergence_shows_release_drift_alert(self):
        out = self._print(orchestrator("integration/x", "main"),
                          {"drift_deployment_only": 15, "drift_integration_only": 21})
        self.assertIn("RELEASE DRIFT", out)
        self.assertIn("main unique commits: 15", out)
        self.assertIn("integration/x unique commits: 21", out)

    def test_integration_behind_only_is_not_drift(self):
        out = self._print(orchestrator("integration/x", "main"), {"drift_deployment_only": 4})
        self.assertNotIn("RELEASE DRIFT", out)

    def test_stages_are_counted_separately(self):
        """El ejemplo #71: integrado sí, entregado no, y a la vista."""
        rows = [
            {"issue": 71, "title": "router", "labels": [ghx.INTEGRATED_LABEL], "released": False},
            {"issue": 95, "title": "chat", "labels": [ghx.INTEGRATED_LABEL], "released": True},
        ]
        out = self._print(orchestrator("integration/x", "main"),
                          {"implemented": 3, "released_closed": 40, "rows": rows})
        self.assertIn("IMPLEMENTED        3", out)
        self.assertIn("INTEGRATED         2", out)
        self.assertIn("RELEASED           40", out)
        self.assertIn("PENDING PROMOTION  1", out)
        self.assertRegex(out, r"71\s+agent:integrated\s+NO")
        self.assertIn("READY TO CLOSE     1", out)


if __name__ == "__main__":
    unittest.main()
